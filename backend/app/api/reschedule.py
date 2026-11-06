"""调课 API - 上传数据 + 查询空闲时段"""

import os
import json
import re
import hashlib
import sqlite3
import threading
import functools
from datetime import datetime
from collections import Counter
from typing import List, Optional, Tuple

from fastapi import APIRouter, Depends, UploadFile, File, HTTPException, Request
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from app.utils.excel_parser import (
    ScheduleDatabase,
    SheetTooLargeError,
    same_label as _label_matches,
    same_teacher as _teacher_matches,
)
from app.agent.reschedule_agent import RescheduleAgent
from app.master_data import MasterData, is_schedulable_project
from app.paths import DATA_DIR as _RESOLVED_DATA_DIR
from app.security import check_admin_credentials, issue_admin_token, require_admin
from app.ratelimit import rate_limit
from app import plan_landing

# R54：XLSX 导出助手在 `app/api/xlsx_helpers.py`（纯函数，无 IO、无共享状态）。
# R78：本模块**最后一个使用者**也搬走了（导出族 → `app/api/export.py`），于是这里
# 不再值导入，改成文件末尾 `__getattr__` 惰性转发到 xlsx_helpers。
# `reschedule._safe_sheet_name` / `api._xlsx_response` 这类「模块属性直调」写法照旧
# 零改动，而且拿到的仍是**同一个对象** —— `test_xlsx_helpers_contract.py` 钉的是 `is`。
#
# 为什么不干脆留一行 `from ... import (...)` 再压个 noqa 注释：那等于用注释把
# "这个模块已经不用它了"藏起来，下一个读代码的人会以为还在用。转发是显式的。

router = APIRouter()

# ---------------------------------------------------------------------------
# 接口鉴权边界
#
# 本服务发布在公网单端口上。只读的演示类接口（/status、/teachers、/chat、
# /parallel/**、/stats、/data-audit、/impact-analysis、/load-default）刻意保持
# **免鉴权**——它们是教秘演示的主路径，数据源是排课表（默认真实表，
# 缺失时回退脱敏表）。
#
# 而下列接口挂 `Depends(require_admin)`，因为它们要么能改数据、要么吐真实师生身份：
#   - /upload                   替换排课表（写）
#   - GET/POST/DELETE /requests  工单读写与清空（写，DELETE 会清全表）
#   - /requests/{id}/approve     审批并自动应用调课，直接改排课结果（写）
#   - POST /parallel/land        把排课方案写回安排表（写，真实排课数据）
#   - POST /parallel/land/undo   撤销落地（写）
# （/parallel/land/preview 与 /parallel/land/status 是只读，随 /parallel/** 免鉴权）
#
# 注意：这些写接口前端**已不再调用**（调课/审批走云数据库 + RLS），
# 因此加门禁不影响任何演示流程，只是把"匿名可写"改成"需管理员会话"。
#
# 第二道边界是**限流**（见 app/ratelimit.py）：免鉴权的演示接口里有几个
# 带真实资源代价——/chat、/experiment-tutor 会调外部 LLM
# （烧额度），/auto-schedule、/parallel/solve 是 CP-SAT 求解（单次最多 10 秒 CPU），
# /admin-login 可被爆破。它们不能加鉴权（会废掉演示），所以按 IP + 全站双维度限流。
# 阈值刻意放宽，真人演示碰不到，脚本刷量会立刻 429。
# ---------------------------------------------------------------------------
_ADMIN_ONLY = [Depends(require_admin)]


# ---------------------------------------------------------------------------
# 整表读取/导出端点的免鉴权开关（2026-10-24 交付评审 P1-2）
#
# 背景：评审方实测 48 个端点里 25 个免鉴权，其中 6 个能匿名整表拉走
# （433 条真实排课 + 13 位真实教师姓名）：四个 /export/*、/auto-schedule/export、
# /parallel/land/preview。"教秘演示主路径免登录"是已文档化的产品取舍
# （docs/安全风险清单 门禁边界一节），但**匿名批量下载整表**超出了演示的
# 合理边界，且"客户以为有鉴权"的概率很高。
#
# 处置：默认行为不变（PUBLIC_READONLY 缺省 = 开，演示主路径照旧免登录）；
# `PUBLIC_READONLY=0` 一键把这 6 个端点收紧为需管理员会话（改后重启生效）。
# 这给验收方一个**不用改代码**的收紧路径，也让"接受风险"成为显式动作。
# 锁死见 backend/tests/test_api_auth.py（默认放行 + 收紧即 401 + 带令牌照常）。
# ---------------------------------------------------------------------------

def _public_readonly_enabled() -> bool:
    return os.getenv("PUBLIC_READONLY", "1").strip().lower() not in ("0", "false", "no", "off")


async def export_gate(request: Request):
    """整表读取/导出门禁：默认放行；PUBLIC_READONLY=0 时要求管理员会话。

    在**请求时**读开关（不是路由定义时）——否则测试里改环境变量不生效，
    这个开关就没法被自动化验证。收紧态复用 require_admin 的候选令牌逻辑
    （边缘代理注入 Authorization 的坑已在 security.py 处理）。
    """
    if _public_readonly_enabled():
        return None
    return await require_admin(request)

# 全局排课数据库
_db = ScheduleDatabase()
# 主数据（教学大纲清单）里的待排任务：与"上一次排完的课表行"并列的一类输入。
# 有了它，"排一个去年不存在的新实验项目"不再需要先伪造一行假课表 —— 这是
# 导师座谈后换轨的核心（见 docs/战略复盘-20261022.md）。
_master_tasks: List = []
# 主数据实体库（课程/实验项目/教师/场地/班级）：导师一号行动项"重构数据库"的落地。
# 基线反推自安排表；教秘的会话内修改（CRUD/灰箱补全）落盘到 master_store.json，
# 重启自愈恢复 —— 落盘失效的唯一条件是安排表本身被替换（指纹变化，见 master_store）。
_master_data = MasterData()
# 主数据初始化/反推的互斥锁（R-70）。
# 为什么现在必须有：R-70 把 5 个导出接口的建表工作下沉到线程池（`run_in_threadpool`），
# 于是 `_ensure_master_data()` 会**并发地**从多个工作线程被调用。在此之前它只跑在
# 事件循环线程上，"检查 initialized → 反推"是天然的原子段；下沉之后若无锁，
# 两个导出同时进来会各反推一次，`_master_data` 的五个实体字典被两个线程同时改写
# —— 这正是 R-68-A「脏落盘把主数据清空」同一族的状态竞争（那次是重启，这次是并发）。
_master_lock = threading.RLock()


def _master_write(fn):
    """主数据**写入口**统一持 `_master_lock`。

    为什么：导出已经跑在工作线程上（R-70），它读主数据时在锁内取快照；
    写入口若不持同一把锁，教秘的 CRUD 就可能正好插进"遍历 experiments"的那几微秒，
    抛 `RuntimeError: dictionary changed size during iteration` —— 表现为导出 500，
    且只在"有人正好在改主数据时点了导出"才复现。

    为什么是 RLock 而不是 Lock：这些 handler 内部还会调 `_ensure_master_data()`，
    它同样要取这把锁；不可重入的锁会当场死锁。

    不加锁的读路径（`/master/overview`、沙盘等）保持无锁 —— 它们只做字典 `.get`，
    CPython 下单次查找是原子的，加锁只会让所有只读接口互相排队。
    """
    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        with _master_lock:
            return await fn(*args, **kwargs)

    return wrapper
_schedule_fp: Optional[str] = None  # 当前安排表指纹（路径+mtime），落盘失效判定用
_agent = RescheduleAgent(_db)

# 落地方案存储（landed_plans 表，与 requests.db 同库同目录同 gitignore）。
# 懒建 + 跟随 _DB_PATH：测试会 monkeypatch _DB_PATH 到临时库，store 必须跟着走。
_landing_store: Optional[plan_landing.LandingStore] = None
_land_lock = threading.Lock()

# R28：所有"改 `_db.entries`"的写路径必须用**同一把**锁。
#
# 会改 entries 的路径有三条：
#   · `land_commit`         → `apply_plan(_db, plan)`            （原先拿 _land_lock）
#   · `apply_move`          → `_apply_reschedule(record, ...)`    （原先无锁）
#   · `/requests/{id}/approve` → 同样调 `_apply_reschedule`        （原先无锁）
#
# 而 `_apply_reschedule` 是"读 entries 挑候选 → classify_move 判定 → 写 e.week"
# 三步流程。两个线程都基于**旧** entries 判定时，双方都看不到对方即将占用那一格
# ⇒ 同一间教室同一时段被排进两门课，而两个接口都返回 success。
#
# 实测（判定与写入之间注入 10ms，模拟真实表遍历耗时）：
#     无锁 → 20/20 次双占同一间教室
#     加锁 →  0/20 次
#
# 注意 `_land_lock` 只保护"落地方案"这条路径，名字带有误导性 ——
# 它实际是 entries 的写锁。保留名字是为了不动既有引用，但语义按写锁来理解。
_DB_WRITE_LOCK = _land_lock


def _landings() -> plan_landing.LandingStore:
    global _landing_store
    if _landing_store is None or _landing_store.path != _DB_PATH:
        _landing_store = plan_landing.LandingStore(_DB_PATH)
        _landing_store.init_schema()
    return _landing_store

# 默认实验安排表路径解析
# 优先级：SCHEDULE_PATH 环境变量 → 仓库根 schedule.xlsx（真实表）→ schedule.demo.xlsx（回退）
#
# 为什么默认用真实表：本系统面向**教学秘书 / 实验教师**，教师姓名与排课编排
# 是教秘日常工作的业务主体数据，必须真实呈现（化名会直接破坏演示可信度）。
# 换用真实表不会经接口外泄联系方式，因为：
#   - 主表只解析前 8 列（周次/周别/时段/班级/组别/项目/场地/教师），
#     "实验筹备人员""备注"等列不进入任何响应；
#   - 教师联系表只取「姓名 + 备注」两个字段（见 ScheduleDatabase.get_teacher_roles），
#     手机号与邮箱列从不被读取；
#   - "学生所在系系办电话"整张表不在解析范围内。
# 学生个人身份数据已随学生事务硬边界整体移除（2026-10-23 用户拍板"永远不管
# 学生事务"：本工具只做教务给定时间范围内的实验项目×组 → 教师×场地分配）。
# 真实表缺失时回退脱敏表，保证任何环境都能启动。相对路径一律按仓库根解析。
def _find_repo_root(module_file: str) -> str:
    """从模块文件向上定位仓库根（同时含 backend/app 的那一层）。

    1) 必须先 abspath 规范化：os.path.dirname 不会消解 '..'，
       若模块是经 ``backend/tests/../app/...`` 这类路径导入的（测试里很常见），
       四层 dirname 连乘会把根算成 ``backend/tests``，进而静默回退到脱敏表
       —— 表现为线上/测试里教师名变成「教师A」，极难排查。
    2) 再以 marker（backend/app）向上探测，比写死层数更能抗目录调整。
    3) 探测失配时退回「规范化后的四层向上」，保证任何部署布局仍能启动。
    """
    start = os.path.abspath(module_file)
    cur = os.path.dirname(start)
    for _ in range(6):
        if os.path.isdir(os.path.join(cur, "backend", "app")):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(start))))


_REPO_ROOT = _find_repo_root(__file__)


def _resolve_default_schedule_path() -> str:
    override = os.getenv("SCHEDULE_PATH")
    if override:
        return override if os.path.isabs(override) else os.path.join(_REPO_ROOT, override)
    real = os.path.join(_REPO_ROOT, "schedule.xlsx")
    if os.path.isfile(real):
        return real
    return os.path.join(_REPO_ROOT, "schedule.demo.xlsx")


DEFAULT_SCHEDULE_PATH = _resolve_default_schedule_path()


class ChatRequest(BaseModel):
    message: str
    fast: bool = False  # 快速模式：跳过 LLM 候选方案生成（沙盘秒回）
    # 上一轮响应回传的槽位（B2 多轮追问）。前端原样带回，服务端无状态合并。
    context: Optional[dict] = None


class ConsultRequest(BaseModel):
    """选课咨询 / 实验辅导请求"""
    question: str


class AdminLoginRequest(BaseModel):
    """管理员登录"""
    username: str
    password: str


class ConsultResponse(BaseModel):
    success: bool
    message: str
    reply: str = ""
    data: dict = {}


class ChatResponse(BaseModel):
    success: bool
    message: str
    data: dict = {}


# ---------------------------------------------------------------------------
# 上传文件落盘：文件名净化
#
# 修复前的写法是 `os.path.join(data_dir, file.filename)`，**未做任何净化**：
# 传入 `filename="../../../schedule.demo.xlsx"` 就能从 `backend/app/data`
# 上溯三层写回仓库根，覆盖演示排课表。而 `dependencies=_ADMIN_ONLY` 之外
# 还有一层历史问题——该接口原先完全免鉴权。
#
# 这里刻意**不用 `os.path.basename`**：它只认当前平台的分隔符，
# 在 POSIX 上 `..\..\x.xlsx` 会被当成一个普通文件名原地保留。
# 直接按两种分隔符切分 + 字符白名单最省心，且 `..` 不可能构造出来。
# ---------------------------------------------------------------------------

_APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_DATA_DIR = _RESOLVED_DATA_DIR

_EXCEL_SUFFIXES = (".xlsx", ".xls")


def _safe_upload_name(filename: str, allowed: tuple, prefix: str = "") -> str:
    """把调用方给的文件名压成安全文件名（只保留白名单后缀 + 清洗过的主干）。

    保留中文主干是有意的：`/master/import-preview` 等入口的文件名会原样
    出现在解析报告里，全换成随机串会让教秘对不上号。
    """
    raw = str(filename or "")
    # 两种分隔符都切，取最后一段——反斜杠路径（`..\..\x.xlsx`）同样被切掉。
    leaf = raw.replace("\\", "/").rsplit("/", 1)[-1]
    stem, ext = os.path.splitext(leaf)
    ext = ext.lower()
    if ext not in allowed:
        raise HTTPException(
            status_code=400,
            detail=f"仅支持 {'/'.join(allowed)} 格式的文件",
        )
    # `\w` 含字母数字下划线；额外放行中文与连字符。`.` 与 `/` 不在白名单内，
    # 因此 `..` 无法构造，"x/../../y" 也会退化成 "x_.._.._y"。
    stem = re.sub(r"[^\w\u4e00-\u9fff-]", "_", stem)[:64] or "upload"
    return f"{prefix}{stem}{ext}"


def _upload_target(filename: str, allowed: tuple, prefix: str = "") -> str:
    """返回上传文件的安全落盘路径，并确保目标目录存在。"""
    os.makedirs(_DATA_DIR, exist_ok=True)
    return os.path.join(_DATA_DIR, _safe_upload_name(filename, allowed, prefix))


# ---------------------------------------------------------------------------
# 上传体积上限
# ---------------------------------------------------------------------------
# 为什么必须有：`await file.read()` 会把整个上传体一次性读进内存。
# 没有上限时，一个 500MB 的请求就能把进程内存顶爆（且解析失败发生在
# **读完之后**，所以"文件不合法"这道关根本起不到保护作用）。
#
# 这里在读取**过程中**累加并断言，而不是读完再判大小 —— 后者对
# 内存的伤害已经造成了。
#
# 默认 32MB：一份几百行的排课表 Excel 通常 < 2MB，
# 留足余量又不至于让滥用者用少量请求占满内存。
# 可用环境变量覆盖（大表单位需放宽时）。
MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_MB", "32")) * 1024 * 1024
_UPLOAD_CHUNK = 1024 * 1024  # 1MB


async def _read_upload_limited(file: UploadFile, limit: int | None = None) -> bytes:
    """分块读取上传内容，超过上限立即 413，不做完整读入。

    与 `await file.read()` 的区别：
      - 读的过程中累加，超限立刻停，内存占用被限制在 limit + 1 块
      - 返回 413（Payload Too Large）而不是 400，语义上区分"太大"与"格式错"
    """
    cap = MAX_UPLOAD_BYTES if limit is None else limit
    buf = bytearray()
    while True:
        chunk = await file.read(_UPLOAD_CHUNK)
        if not chunk:
            break
        buf.extend(chunk)
        if len(buf) > cap:
            raise HTTPException(
                status_code=413,
                detail=(
                    # 不给运维话术（"设置环境变量…重启服务"）：教秘既做不到，
                    # 也会误以为是自己少配了什么。只说"怎么办"。
                    # 限额值另走 X-Max-Upload-Bytes 头，供前端展示。
                    f"文件超过上限（{cap // (1024 * 1024)} MB）。"
                    "请压缩表格或拆分成多个文件后重新上传。"
                ),
                headers={"X-Max-Upload-Bytes": str(cap)},
            )
    return bytes(buf)


@router.post("/upload", summary="上传实验安排表 Excel", dependencies=_ADMIN_ONLY)
async def upload_schedule(file: UploadFile = File(...)):
    """上传实验安排表 Excel 文件（需管理员会话）。

    ⚠️ 整表解析必须在线程池里（2026-10-29 起，R-89）：
    实测 `load_workbook` **343ms**、冷解析整表 **495ms**，而它是 `async def` 里的一次
    普通调用 —— 那段时间整个事件循环停摆，别人的页面/`/health` 全部排队。

    此前这里有一份 `KNOWN_BLOCKING` 豁免，理由是"挪进线程池会让并发读看见空表"。
    那个理由在 R-87（构建到暂存器、一次提交）之后**已经不成立**；
    而 R-89 又给整表加载加了 `_LOAD_LOCK`，并发上传也不会互相覆盖。
    所以本轮把豁免撤掉，改成与 `/load-default`、`/demo/reset` 一致。
    """
    save_path = _upload_target(file.filename, _EXCEL_SUFFIXES)

    content = await _read_upload_limited(file)   # 32MB 上限（读本身是 async，留在循环上）
    # 落盘 + 整表解析 + 主数据/落地方案失效判定：全是同步重活，一起交给线程池
    return await run_in_threadpool(_apply_uploaded_schedule, save_path, content)


def _apply_uploaded_schedule(save_path: str, content: bytes) -> dict:
    """（同步、纯 CPU/IO）把上传的字节写盘并整表重载。端点只做响应包装。"""
    with open(save_path, "wb") as f:
        f.write(content)

    try:
        # 先构建**新实例**，全部成功后再整体替换 —— 不原地清空 _db。
        #
        # 为什么（R4 竞态实测）：`load_from_excel` 是原地改写（先 loaded=False、
        # entries=[]，再逐行填充）。实测在清空后插入 300ms 延迟，并发读取方
        # **100% 读到 0 条**（21,572,523 次全部异常）。真实全量解析约 157ms，
        # 窗口虽窄但确实存在——这期间 /teachers、/parallel/overview 会返回空数据。
        #
        # 改成"新实例 + 原子引用替换"后，读取方要么看到旧表、要么看到新表，
        # 不会看到中间态。解析失败时 _db 保持原样，也不会把旧数据搞丢。
        new_db = ScheduleDatabase()
        new_db.load_from_excel(save_path)      # 225ms，锁外做，不占写锁
        rep = new_db.load_report
        # 解析成功后才切换全局引用（下行 `global _db` 在函数内需先声明）
        #
        # ⚠️ R29：切换必须持 `_DB_WRITE_LOCK`。
        # 换表时 `_landings().clear()` 会清掉旧表上的落地方案。若此刻有
        # `land_commit` 正在 `_landings().save(...)`，两条语句交错就会留下
        # 一条**属于旧表**的落地记录 —— 新表上飘着一份旧方案，下次重启重放
        # 会把它当成新表的调整硬塞进去。
        #
        # 实测（save 与 clear 之间 5–10ms 窗口）：无锁 30/30 次残留幽灵记录，
        # 持同一把锁 0/30。
        #
        # 解析（225ms）放在锁外：锁只覆盖"替换引用 + 作废旧状态"这一小段，
        # 不把整表解析也锁进去。
        from app import master_store as _ms
        global _db, _schedule_fp
        # 一整段都在锁内：替换引用 + 作废旧表派生的全部状态。
        # 只锁 `_db = new_db` 是不够的 —— `_landings().clear()` 与并发的
        # `land_commit` 里的 `_landings().save()` 交错，才是幽灵记录的来源。
        with _DB_WRITE_LOCK:
            _db = new_db
            # 换了安排表 → 旧表上的主数据落盘全部作废（班级/项目可能已不存在），按新表反推
            _schedule_fp = _ms.schedule_fingerprint(save_path)
            _master_data.__init__()      # 回到未初始化态，下次访问按新表反推
            _master_tasks.clear()
            _ms.clear()
            # 换了安排表 → 旧表上的落地方案一并作废：方案里的单元在新表可能根本不
            # 存在，重放它等于把旧方案强加到新表上。与 master_store 同一失效规则。
            discarded = _landings().clear()
        msg = f"✅ 成功加载 {len(_db.entries)} 条排课记录"
        if discarded:
            msg += f"（已丢弃旧表上的落地方案 {discarded} 份，以新表为基准）"
        if rep.get("pending"):
            msg += f"，另有 {rep['pending']} 条待排任务（没排时间，可直接进并行排课）"
        if rep.get("skipped"):
            msg += f"；⚠️ {rep['skipped']} 条因时段无法识别未纳入冲突扫描"
        return {
            "success": True,
            "message": msg,
            "total_entries": len(_db.entries),
            "rooms": _db.get_all_rooms()[:10],
            "teachers": _db.get_all_teachers(),
            "week_range": list(_db.get_week_range()),
            # 解析账本：进了多少 / 多少待排 / 多少被丢。静默丢弃是本项目反复踩的坑，
            # 界面必须能说出数量与原因，而不是只回一个 total_entries。
            "load_report": rep,
        }
    except SheetTooLargeError as e:
        # 规模超限不是"解析失败"，是主动拒绝：别用"文件解析失败"当前缀，
        # 也别把 "设置环境变量 MAX_SHEET_ROWS" 这类运维话术塞给教秘。
        # detail 只说"发生了什么 + 怎么办"，限额值走头暴露（供前端展示）。
        raise HTTPException(
            status_code=400,
            detail=f"{e!s}。请精简表格（只保留需要的周次/班级）或拆分成多个文件后重新上传。",
            headers={"X-Max-Sheet-Rows": str(e.limit)},
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"文件解析失败：{e!s}")


@router.post("/upload-master", summary="上传教学大纲/主数据清单（从零排课的输入）",
             dependencies=_ADMIN_ONLY)
@_master_write
async def upload_master(file: UploadFile = File(...)):
    """上传「课程 → 实验项目 → 学生组（→ 候选教师/场地）」清单。

    这是"从零排课"的输入口：清单天然没有周次/星期/时段，此前这类行会被主解析
    流程静默丢弃。上传后这些任务直接进入并行排课的总览与求解。
    """
    save_path = _upload_target(file.filename, _EXCEL_SUFFIXES)
    content = await _read_upload_limited(file)
    with open(save_path, "wb") as f:
        f.write(content)

    try:
        from app.utils.excel_parser import parse_master_tasks

        tasks, report = parse_master_tasks(save_path)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"清单解析失败：{e!s}")

    # R49：记下"这次换掉了多少条"再清空。
    #
    # 本端点是**整批替换**（再传一份就覆盖，不累加）。教秘若已有一份清单，
    # 提交后旧的整份消失 —— 这是设计（见 `clear_master_tasks`），不是缺陷；
    # 缺陷在说明：回执只说"已读入 N 条"，教秘无从知道还有 M 条被换掉了。
    # ⚠️ 这个数是**提交时刻**的真实值，不是预览时刻的值 —— 两者不一致就说明
    # 这期间有人传过新清单，而教秘看到的就是这里的实际值（以实际结果为准）。
    replaced = len(_master_tasks)
    _master_tasks.clear()
    _master_tasks.extend(tasks)
    await _persist_master_state_async()
    msg = f"✅ 已读入 {len(tasks)} 条主数据任务（可直接进并行排课）"
    if replaced:
        msg += f"\n♻️ 已整批替换原有 {replaced} 条清单（清单不累加，再传一份会整体覆盖）"
    return {
        "success": True,
        "message": msg,
        "total_tasks": len(tasks),
        "replaced_tasks": replaced,
        "classes": sorted({t.class_name for t in tasks}),
        "report": report,
    }


@router.delete("/master/tasks", summary="清空已导入的教学任务清单（需管理员）",
               dependencies=_ADMIN_ONLY)
@_master_write
async def clear_master_tasks():
    """清空 `_master_tasks`（上传的大纲清单）。

    为什么需要：清单是**整批替换**语义（再传一份就覆盖），但没有"不传新清单、
    只把旧的清掉"的入口 —— 教秘传错一次只能重启进程；探针之间也会互相污染
    （上一个探针留下的清单改了下一个探针的 pending_total）。
    """
    await _ensure_loaded_async()
    _ensure_master_data()
    _master_tasks.clear()
    await _persist_master_state_async()
    return {"success": True, "message": "已清空导入的教学任务清单", "master_tasks": 0}


@router.post("/demo/reset", summary="演示一键复位（仅 DEMO_MODE=1 可用，需管理员）",
             dependencies=_ADMIN_ONLY)
async def demo_reset():
    """把演示态复位到初始：清空申请单 + 撤销所有"已应用"的调课 + 重载安排表。

    为什么需要：审批演示会真的改写课表（调课落库 + applied_moves 重放）。演完
    第一场，第二场就带着"上一场挪过的课"开局 —— 要么手工收拾，要么重启进程。
    这个端点就是"下一场之前按一下"的按钮，且**只在 DEMO_MODE=1 时存在**。
    """
    if os.getenv("DEMO_MODE", "") != "1":
        raise HTTPException(status_code=403, detail="演示复位仅在 DEMO_MODE=1 时可用")
    conn = _get_db()
    moves = conn.execute("SELECT COUNT(*) AS c FROM applied_moves").fetchone()["c"]
    reqs = conn.execute("SELECT COUNT(*) AS c FROM requests").fetchone()["c"]
    conn.execute("DELETE FROM applied_moves")
    conn.execute("DELETE FROM requests")
    conn.commit()
    conn.close()
    # 整表重载是同步重活（缓存命中 14ms，冷解析 545ms）—— 同 R-19/R-70 一族，
    # 别让"演示前按一下复位"把别人的页面一起冻住。
    await run_in_threadpool(_load_default_schedule)
    return {"success": True,
            "message": f"✅ 演示已复位：清空 {reqs} 条申请、撤销 {moves} 笔已应用调课，课表已重载",
            "cleared_requests": reqs, "reverted_moves": moves}


@router.get("/demo-mode", summary="演示模式开关状态（免鉴权）")
async def demo_mode():
    """演示模式（DEMO_MODE=1）：审批台走本机工单 + 本地管理员口令，不依赖云端登录。

    为什么需要：现场网络/手机验证码不可控，而"审批通过 → 课表真的变了"是产品
    最有说服力的一段。演示模式把这段变成**离线可跑**，且默认关闭 —— 生产部署不设
    DEMO_MODE 时行为不变（审批仍走云端身份 + 云表）。
    """
    return {"success": True, "demo_mode": os.getenv("DEMO_MODE", "") == "1"}


@router.get("/master-status", summary="主数据（大纲清单）状态")
async def master_status():
    """当前主数据任务数、覆盖班级，以及安排表里"没排时间"的待排行数。"""
    await _ensure_loaded_async()
    pending_in_table = list(getattr(_db, "pending", None) or [])
    return {
        "success": True,
        "data": {
            "master_tasks": len(_master_tasks),
            "master_classes": sorted({t.class_name for t in _master_tasks}),
            "pending_in_schedule": len(pending_in_table),
            "pending_samples": [repr(t) for t in pending_in_table[:5]],
            "total_units_to_schedule": len(_master_tasks) + len(pending_in_table),
            "load_report": _db.load_report,
        },
    }


@router.post("/chat", response_model=ChatResponse, summary="教师调课咨询",
             dependencies=[Depends(rate_limit("chat"))])
async def reschedule_chat(req: ChatRequest):
    """教师输入调课请求，返回可用时段

    ⚠️ 这里必须 `run_in_threadpool`：`process_request` 在 LLM 可用时会**同步**发起
    HTTP 请求（生成 AI 候选方案，实测 7.6s）。直接在 `async def` 里调用会把整个
    事件循环冻住 —— 实测并发期间 `/stats`（平时 4ms）被拖到 8.67s，也就是**一个人
    点"申请调课"，全站所有人一起卡 8 秒**，连 `/health` 探活都不响应。
    丢到线程池后，慢的只是这一个请求。
    """
    if not req.message.strip():
        return ChatResponse(success=False, message="请输入调课请求")

    # 自愈：沙箱回收后 `_db.loaded=False`，而 `/chat` 是**唯一漏了这一步的入口**。
    # 实测（2026-10-29，进程刚起就说话）：返回「排课数据尚未加载，请先上传实验安排表」——
    # 而前端没有上传入口（`/upload` 要管理员、无 UI），教秘无法自救；
    # 同一个进程里 `/teachers` 却能自己恢复（它走了 `_ensure_loaded_async`）。
    # 这条路径平时测不出来：单测里 `_db.loaded` 总是 True。
    await _ensure_loaded_async()

    result = await run_in_threadpool(_agent.process_request, req.message,
                                     not req.fast, req.context)
    return ChatResponse(
        success=result["success"],
        message=result["message"],
        data={
            "target_week": result.get("target_week"),
            "available_slots": result.get("available_slots", []),
            "excluded_slots": result.get("excluded_slots", []),
            "ai_plans": result.get("ai_plans", []),
            # R64：候选来源（llm / rule / none）。前端据此选文案 ——
            # 规则产物不能显示成「🤖 AI 推荐」。
            "ai_plans_source": result.get("ai_plans_source", "none"),
            "total": result.get("total", 0),
            "need_info": result.get("need_info", False),
            "followup_questions": result.get("followup_questions", []),
            "context": result.get("context") or {},
            # R31：agent 层从 R21 起就返回了这两个字段，但这里**一直没透传** ——
            # `data` 是白名单拼出来的 dict，漏写一个键就等于把这个字段吞掉。
            # 后果：R21 宣称"结构化字段与中文文案并列，下游不要从 message 里
            # 正则反解"，而下游实际拿到的 data 里根本没有它。
            # 是被 R31 的部署新鲜度探测（比对返回体字段）抓出来的。
            "course_name": result.get("course_name"),
            "course_unrecognized": result.get("course_unrecognized", False),
        },
    )


def data_state() -> dict:
    """供 `/health` 深度探活：**进程活着 ≠ 数据在内存**。

    排课数据只存在于进程内存，沙箱回收后接口会退回"未加载"态（R-18）。
    原先 `/health` 恒返回 `{"status":"ok"}`，把这个状态完全掩盖了。
    本函数只读、不触发加载，可被外部监控高频轮询。
    """
    return {"loaded": _db.loaded, "total_entries": len(_db.entries) if _db.loaded else 0}


@router.get("/status", summary="查看系统状态")
async def get_status():
    """查看系统加载状态。

    `ai` 字段是**渠道自述**（provider / model / endpoint / key_source），
    用于线上核验"后端 LLM 到底接的哪个渠道、有没有读到密钥"——
    排查配置问题时不必翻沙箱日志。它**不包含任何密钥值**。
    """
    parser = getattr(_agent, "llm_parser", None)
    return {
        "loaded": _db.loaded,
        "total_entries": len(_db.entries) if _db.loaded else 0,
        "rooms": _db.get_all_rooms() if _db.loaded else [],
        "teachers": _db.get_all_teachers() if _db.loaded else [],
        "week_range": list(_db.get_week_range()) if _db.loaded else [],
        # 冷启动时 loaded=false 是**正常**的：本接口只查状态、不触发加载。
        # 首次访问数据端点（如 /teachers、/parallel/overview）时 `_ensure_loaded()`
        # 才自愈加载。没有这句话时，新部署者看到 loaded=false 会以为服务出问题了。
        "loaded_hint": (
            "已加载"
            if _db.loaded
            else "尚未加载（懒加载）：访问 /api/reschedule/teachers 等数据端点会自动加载默认排课表"
        ),
        "ai": parser.describe() if parser is not None else {"configured": False},
    }


_load_lock = threading.Lock()


def _load_default_schedule() -> tuple[str, int]:
    """把默认排课表读进内存，并重放已审批通过的调课。返回 (提示消息, 记录数)。

    幂等：先整表重载，再按 applied_moves 顺序重放，故重复调用不会累积。
    """
    if not os.path.exists(DEFAULT_SCHEDULE_PATH):
        # 这条一旦出现，说明服务器上连脱敏兜底表都没有 —— 不是用户能自救的情况，
        # 所以文案要指出"服务器缺文件"，而不是原先那句会让用户去找上传入口的"请先上传"
        # （上传接口受管理员门禁保护，且前端没有上传 UI）。
        raise HTTPException(
            status_code=404,
            detail=f"默认排课表不存在：服务器上未找到 {os.path.basename(DEFAULT_SCHEDULE_PATH)}，请联系管理员处理",
        )

    # 这里保持"原地加载"，不做实例替换：
    #   - `_ensure_loaded()` 有双检锁保护，不会并发进入
    #   - `/load-default` 也被 `_load_lock` 覆盖（见该路由）
    #   - 测试与部分调用方持有 `_db` 的实例引用（如 monkeypatch 注入的夹具 db），
    #     替换实例会让它们停在旧对象上
    # 真正的并发风险在 `upload_schedule`（它不走这把锁），那里已改为原子替换。
    _db.load_from_excel(DEFAULT_SCHEDULE_PATH)
    # 安排表指纹：主数据落盘的失效判定依据（换表 → 旧落盘作废）
    global _schedule_fp
    from app import master_store as _ms
    _schedule_fp = _ms.schedule_fingerprint(DEFAULT_SCHEDULE_PATH)
    # 重放写事件：审批调课（applied_moves）+ 落地方案（landed_plans）。
    # 两者都是"重启后不能丢"的状态（R-18 内存态 + R-36 审批闭环 + 落地）。
    # 有落地记录时按**时间戳顺序**混排：后发生的事件赢 —— 调课不会被落地静默
    # 吞掉，落地也不会被后来的调课静默抹掉。无落地记录时逐字走旧路径。
    replay_applied, replay_skipped, replay_landed = 0, 0, 0
    # R62：重放失败必须计数并报出来。
    # 重放的意义是"重启后不丢已审批的调课"；某条重放失败 = 那条调课的效果消失。
    # 而原来这里是 `except Exception: pass` —— 教秘看到的是"✅ 成功加载"，
    # 实际有一节课停在旧周次。丢的是**已审批的变更**，比丢原始行更重。
    replay_failed: list = []
    # R51：外层兜底也必须留痕。
    # `_get_db()` / `_landings().all()` / `events.sort()` 任一失败 → 整段重放被跳过，
    # 而异常发生在所有 `replay_failed.append` 之外，于是消息照样是「✅ 成功加载 N 条」。
    # 此刻「没有调课要重放」与「调课全都没重放」在消息里长得一模一样 —— 这是最危险的时刻。
    #
    # 不复用 `replay_failed`：那是"第几条失败"，外层连有几条都不知道。
    # 塞进去会被渲染成「⚠️ 1 条已审批记录重放失败」，把"未知条数"说成"1 条"，是新的误导。
    replay_aborted = None
    try:
        conn = _get_db()
        moves = conn.execute(
            "SELECT id, request_data, applied_at FROM applied_moves ORDER BY id").fetchall()
        conn.close()
        lands = _landings().all()
        if not lands:
            for r in moves:
                try:
                    rec = json.loads(r["request_data"])
                    res = _apply_reschedule(rec, persist=False)
                    replay_applied += res["applied"]
                    replay_skipped += res["skipped"]
                except Exception as e:
                    replay_failed.append(f"#{r['id']}: {type(e).__name__}: {e}")
        else:
            events = [(r["applied_at"] or "0000-00-00", 0, r["id"], "move",
                       r["request_data"]) for r in moves]
            events += [(L["landed_at"] or "0000-00-00", 1, L["id"], "land",
                        L["plan"]) for L in lands]
            events.sort(key=lambda e: (e[0], e[1], e[2]))
            for _ts, _kind, _i, kind, payload in events:
                try:
                    if kind == "move":
                        res = _apply_reschedule(json.loads(payload), persist=False)
                        replay_applied += res["applied"]
                        replay_skipped += res["skipped"]
                    else:
                        plan_landing.replay_landing(_db, payload)
                        replay_landed += 1
                except Exception as e:
                    replay_failed.append(f"{kind}#{_i}: {type(e).__name__}: {e}")
    except Exception as e:
        # 与内层一样：不阻断启动（数据损坏不该让服务起不来），但绝不静默。
        replay_aborted = f"{type(e).__name__}: {e}"

    msg = f"✅ 成功加载 {len(_db.entries)} 条排课记录"
    if replay_applied:
        msg += f"（已重放 {replay_applied} 条审批通过的调课）"
    if replay_landed:
        msg += f"（已重放落地方案 {replay_landed} 份）"
    if replay_failed:
        # 不阻断启动（数据损坏不该让服务起不来），但**绝不静默**。
        shown = "；".join(replay_failed[:3])
        msg += (f"；⚠️ {len(replay_failed)} 条已审批记录**重放失败**，"
                f"它们的调整未生效（{shown}"
                + ("…" if len(replay_failed) > 3 else "") + "）")
    if replay_aborted:
        # 整段没跑：一条都没重放，而且不知道本应重放多少条。
        # 此时的"成功加载"极易被读成"调课都在"，所以要把话说死。
        msg += (f"；⚠️ 已审批调课的重放**中断**，本期一条都未重放"
                f"（{replay_aborted}）—— 课表可能停在上次落盘的状态")
    # 待排/丢弃都必须说出口：静默丢弃是本项目反复踩的坑（R-35、以及缺周次的整行蒸发）
    rep = _db.load_report
    if rep.get("pending"):
        msg += f"（另有 {rep['pending']} 条待排任务：写了班级与实验项目但没排时间，可直接进并行排课）"
    if rep.get("skipped"):
        msg += f"（⚠️ {rep['skipped']} 条时段无法识别，未纳入冲突扫描）"
    # 数据就位后在后台把四份导出各建一次：让教秘第一次点导出也是热的（R-71）
    #
    # R78：预热搬到 `app/api/export.py`，这里**惰性导入**。不能写成模块级
    # `from app.api import export` —— export 顶部要 `from app.api import reschedule`
    # 取可变状态，两侧模块级互引会随"谁先被导入"分岔成 ImportError（R76 实测过）。
    from app.api import export as _export_mod
    _export_mod.start_export_warmup()
    return msg, len(_db.entries)


def _ensure_master_data() -> None:
    """主数据未初始化时恢复：优先读落盘（会话内修改不丢），否则从安排表反推。

    恢复优先级（自愈链）：
      1. master_store.json 存在且安排表指纹一致 → 整体恢复（含教秘的 CRUD/灰箱补全）；
      2. 落盘不存在 / 指纹变了（安排表被替换）/ 文件损坏 → 从当前安排表反推基线。
    指纹不一致时主动清掉旧落盘 —— 安排表永远是主数据的权威来源，旧表上的会话修改
    对新表没有意义（新表的班级/项目可能根本不存在）。
    """
    if _master_data.initialized:
        return
    with _master_lock:
        if _master_data.initialized:  # 双检：等锁期间别的线程可能已经反推完
            return
        _ensure_master_data_locked()


def _ensure_master_data_locked() -> None:
    """真正的恢复/反推；调用方必须持有 `_master_lock`（见上：并发导出会同时进来）。"""
    global _schedule_fp
    from app import master_store

    store = master_store.load()
    # R50：`load()` 用 `None` 表示三种情况 —— 文件不存在 / JSON 损坏 / 版本过旧。
    # 三者都不恢复，但**损坏的那份要丢弃**：否则它永久留在磁盘上，
    # 每次启动都读一遍、失败一遍。把它和"没有落盘"混在一起，
    # 就等于让坏文件获得了永久豁免。
    if store is None and master_store.consume_corrupt_flag():
        master_store.clear()
    if store and _schedule_fp and store.get("schedule_fingerprint") == _schedule_fp:
        try:
            _master_data.from_dict(store.get("master") or {})
            if _master_data.initialized:
                _master_tasks.clear()
                _master_tasks.extend(master_store.rows_to_tasks(store.get("tasks")))
                print(f"✅ 主数据已从落盘恢复（实验项目 {len(_master_data.experiments)}"
                      f" / 待排任务 {len(_master_tasks)}）")
                return
            # 落盘里是**空主数据**（历史 bug：未初始化时也落盘）→ 不能当权威恢复，
            # 否则一次重启就把 17 个班/25 个项目全部清空（实测踩过）。丢弃后按表反推。
            print("⚠️ 落盘里的主数据为空（历史脏数据），丢弃并按安排表反推")
            master_store.clear()
        except Exception as e:
            print(f"⚠️ 主数据落盘恢复失败（{e}），回退安排表反推基线")
        master_store.clear()
    elif store and _schedule_fp and store.get("schedule_fingerprint") != _schedule_fp:
        print("ℹ️ 安排表已更换，丢弃旧表上的主数据落盘，按新表反推基线")
        master_store.clear()
    if _db.loaded:
        _master_data.build_from_entries(_db.entries)


def _master_courses_snapshot() -> dict:
    """锁内取「项目名 → 课程名」快照（导出用）。

    为什么要快照：导出已跑在工作线程上（R-70），而教秘的 CRUD / 台账导入会同时
    改写 `_master_data.experiments`；`project_courses()` 要**遍历**这个字典，
    边改边遍历会抛 `RuntimeError: dictionary changed size during iteration`
    —— 表现为导出 500，且只在"有人正好在改主数据时点了导出"才复现。
    快照只有 25 条，成本可忽略；绝不能让导出自己成为新的故障源。
    """
    from app.course_bridge import project_courses

    with _master_lock:
        return project_courses(_master_data)


def _master_snapshot():
    """锁内复制一份主数据（给要遍历五张实体表的导出用，理由同上）。"""
    with _master_lock:
        return MasterData().from_dict(_master_data.to_dict())


def _persist_master_state() -> None:
    """把主数据 + 灰箱清单落盘（尽力而为：失败不阻塞排课，内存态仍是权威）。"""
    global _schedule_fp
    if not _master_data.initialized and not _master_tasks:
        return
    from app import master_store

    if _schedule_fp is None and _db.loaded and getattr(_db, "_filepath", None):
        _schedule_fp = master_store.schedule_fingerprint(_db._filepath)
    # 未初始化就不写 master：写一份空表出去，下次启动会把它当权威恢复，
    # 于是"重启一次主数据全没了"。只落清单，恢复时按表反推基线。
    master_store.save({
        "schedule_fingerprint": _schedule_fp,
        "master": _master_data.to_dict() if _master_data.initialized else None,
        "tasks": master_store.tasks_to_rows(_master_tasks),
    })


def _master_derived_tasks() -> List:
    """从主数据派生「某班未排的新增项目」为待排任务。

    主数据反推自安排表，所以其中的项目基本都已排；只有教秘通过 CRUD **新增**的
    项目（或给已有项目新增了适用班级）才会在这里被补成待排任务 —— 这正是
    「新增实验项目不必伪造旧课表行」的完整闭环（R-48 之后，导师一号行动项的收尾）。

    判据按 **(班级, 项目)** 粒度：只要该班排过该项目（无论哪个组），就不派生。
    若按 (班级, 项目, 组) 粒度，真实表里组名写法不一致（如 `24软工2_1` 上过别的
    项目却没上「扭摆法」）会被误当成"漏排"，反推基线会凭空多出待排任务。
    """
    _ensure_master_data()
    if not _master_data.initialized:
        return []
    from app.utils.excel_parser import PendingTask

    scheduled_pairs = {(e.class_name or "", e.experiment or "") for e in _db.entries}
    pending_pairs = {(t.class_name, t.experiment) for t in list(_db.pending) + list(_master_tasks)}
    derived: List = []
    for exp_name, ex in _master_data.experiments.items():
        # ⚠️ 绪论/考试类项目自 R-58 起也登记为实体（台账可见、可指定课程），
        # 但**绝不派生**：绪论课已单独安排、考试类时间与场地逻辑不同（R-48 口径）。
        # 不过滤的话，教秘经 CRUD/台账导入给绪论项目加个班级，求解器就会去排一场绪论课。
        if not is_schedulable_project(exp_name):
            continue
        for cls in ex.get("classes", set()):
            if (cls, exp_name) in scheduled_pairs or (cls, exp_name) in pending_pairs:
                continue
            for g in _master_data.groups_of(cls):
                # 教师/场地留给 _enrich_extra_tasks 统一补全（负载最轻者优先），
                # 不在这里 prefill 第一个 —— 否则一个新项目的所有组全砸给同一位老师。
                derived.append(PendingTask(
                    class_name=cls, experiment=exp_name, group=g,
                    course=ex.get("course") or "", source="master",
                ))
    return derived


def _schedulable_extra_tasks() -> List:
    """可排的待排任务 = 表里没排时间的行 + 上传的大纲清单 + 主数据新增项目，**去掉考试/考核类**。

    考试类（真实表里 3 行「大物实验B/C 考试」）仍留在 `db.pending` 里被报出来，
    只是不参与排课 —— 不静默丢弃的原则同样适用于它们。

    ⚠️ R26：灰箱补全可能触发落盘（28–88 ms 同步文件 IO），必须在**线程池**里调，
    不能直接在 async 路由里用。已改为 `await _schedulable_extra_tasks_async()`。
    """
    return _enrich_extra_tasks()[0]


async def _schedulable_extra_tasks_async() -> List:
    """`async` 路由用的版本：整段（含可能的落盘）丢线程池。"""
    return await run_in_threadpool(_schedulable_extra_tasks)


def _enrich_extra_tasks() -> Tuple[List, List[str]]:
    """灰箱补全：清单/待排任务里**缺什么就从主数据补什么**，并把替用户做的每个
    决定说出口（返回 assumptions）。

    灰箱场景（教秘只给部分信息）：
    - 缺指导教师 → 从主数据该项目的可授教师里挑（本批内负载最轻者优先，平局取字典序）；
    - 缺候选场地 → 从主数据该项目的场地里补第一间；
    - 两处都没有 → 不编造，如实报告「结果中该列为空」。
    补全是幂等的（只填空字段，不覆盖显式给出的值）， assumptions 每次调用重算。
    """
    tasks = _raw_schedulable_tasks()
    assumptions: List[str] = []
    if not tasks:
        return tasks, assumptions

    dirty = False  # 补全是否新填了字段（填了就落盘，重启后分配不重算不漂移）
    md_ready = _master_data.initialized
    teacher_load: Counter = Counter()
    for t in tasks:
        if t.teacher:
            teacher_load[t.teacher] += 1

    for t in tasks:
        ex = _master_data.experiments.get(t.experiment, {}) if md_ready else {}
        # 补全来源标记：任务对象在会话内共享，可能已被上一次接口调用补全过。
        # 只看"字段是否为空"会把那次补全当成"用户显式给的"，假设就静默丢了 ——
        # 所以补全时打标，之后每次调用都从标记还原假设（决定过程必须可持续可见）。
        if not t.teacher:
            pool = sorted(ex.get("teachers") or [])
            if pool:
                t.teacher = min(pool, key=lambda te: (teacher_load[te], te))
                teacher_load[t.teacher] += 1
                t._teacher_enriched = True
                dirty = True
        if getattr(t, "_teacher_enriched", False):
            assumptions.append(
                f"「{t.experiment}」{t.group or '未分组'} 未指定指导教师，"
                f"按主数据可授教师自动分配：{t.teacher}")
        if not t.room:
            pool = sorted(ex.get("rooms") or [])
            if pool:
                t.room = pool[0]
                t._room_enriched = True
                dirty = True
        if getattr(t, "_room_enriched", False):
            assumptions.append(
                f"「{t.experiment}」{t.group or '未分组'} 未指定场地，"
                f"已按主数据登记候选场地 {t.room}（求解时在全院可用场地中统一分配，以结果为准）")
    if dirty:
        _persist_master_state()
    return tasks, assumptions


def _raw_schedulable_tasks() -> List:
    raw = list(getattr(_db, "pending", None) or []) + list(_master_tasks) + _master_derived_tasks()
    # 统一判定「可排」：考试类**和绪论类**都不进求解器（R-48/R-58 口径）。
    # 此前只过滤 looks_like_exam —— R-58 让绪论登记进主数据后，上传清单或
    # pending 里若出现绪论行，灰箱排课就会把一场绪论课排进某个教室。
    return [t for t in raw if is_schedulable_project(getattr(t, "experiment", "") or "")]


def _ensure_loaded() -> None:
    """访问排课数据前，保证内存里已有数据；没有就自动恢复。

    为什么需要：线上由沙箱托管，进程会被平台回收/重启，而排课数据只存在
    **进程内存**里（无持久化）⇒ 重启后 `_db.loaded` 变 False。原先各数据接口
    直接抛 400「请先加载实验安排表（/load-default）」，而前端只在**页面挂载时**
    自动 load-default 一次且无重试：只要那一次失败、或进程在页面打开后重启，
    用户就会看到"加载课程总览失败"且界面上无从自救（必须手动刷新）。
    本地是常驻 uvicorn 不重启，所以看不出这个问题 —— 这正是"线上和本地不一样"
    的真实原因。

    改为此处自愈：默认表存在就直接加载，/load-default 仍可用（幂等）但不再是使用前提。
    并发请求用双检锁，保证 433 行 Excel 只解析一次。

    ⚠️ 本函数是**同步**的，只许在三种位置调用：
      1. 已经在线程池里的同步辅助函数（各导出族的 `_*_bytes()`）；
      2. 同步函数内部（如 `_ensure_master_data_locked`）；
      3. **`async def` 路由里一律用 `await _ensure_loaded_async()`**。

    为什么要有 `_ensure_loaded_async`（2026-10-29，接 R-19/R-70 同一族）：
    `/load-default` 早就下沉线程池，但**自愈路径**没下沉 —— 而它才是线上沙箱被回收后
    用户点到的第一条路。实测冷解析期间心跳被拖 **780ms**（真实表 433 行；文档记录的
    冷沙箱口径是 3.8s），即整站在那段时间不响应任何请求。
    `scripts/audit_async_blocking.py` 按设计查不到它：那个脚本只扫"路由函数体内有没有
    await"，而这条重活藏在 31 个端点共同调用的辅助函数里（调用传递闭包）。
    """
    if _db.loaded:
        return
    with _load_lock:
        if _db.loaded:  # 双检：等锁期间可能已被别的请求加载完
            return
        _load_default_schedule()


def _ensure_loaded_sync() -> None:
    """`await _ensure_loaded_async()` 的阻塞主体（跑在线程池里）。

    ⚠️ R27：加载完**必须在这里**把 `_db.loaded` 置真，不能等回到事件循环再设。

    R25 把加载挪进线程池后留下了一个时序漏洞：`_db.loaded` 由 async 端在
    `await` 返回后才设。于是拿到锁的 worker 加载完返回、下一个 worker 拿锁时
    双检能过（R4 的双检在这一层），但**还在排队、尚未进线程池**的请求
    async 端复检读到的仍是 False → 又排一次队 → 线程池里空转一轮。

    锁内双检挡住了无限膨胀，所以不会雪崩；但首次请求延迟平白翻倍：
    实测 400ms 模拟下 10/40/100/300 并发总耗时都是 ~810ms，加载 2 次。
    真实冷解析 3.8s 时就是 3.8s → 7.6s。
    """
    _ensure_loaded()
    # `_ensure_loaded()` 内部已 `_db.load_from_excel()`，`loaded` 通常已为真；
    # 这里显式兜一次，保证"出锁即已加载"这个不变量不依赖下游实现细节。
    if not _db.loaded:
        _db.loaded = True


async def _ensure_loaded_async() -> None:
    """`async` 路由用的版本：已加载 → 零开销直接返回；未加载 → 丢线程池。

    为什么要先判一次 `_db.loaded` 再进线程池：正常运行时 99.99% 的请求
    走的是"已加载"分支，为它排一次线程池任务纯属浪费（且会引入额外延迟）。
    """
    if _db.loaded:
        return
    await run_in_threadpool(_ensure_loaded_sync)


async def _persist_master_state_async() -> None:
    """`_persist_master_state()` 的异步版本（R26）。

    实测（Windows）：每次落盘 28–88 ms，**与文件体积无关** —— 16 KB 和 512 KB
    一样慢，成本全在单次 write + os.replace 的固定开销上。

    事件循环层面的实测对照（心跳探针，1ms 粒度）：

        | 方式     | 调用耗时 | 事件循环最长卡顿 |
        |----------|----------|------------------|
        | 同步直调 |  88.2 ms |        **87.3 ms** |
        | 线程池   |  88.7 ms |          15.2 ms |

    同步直调期间**整个事件循环停摆**：别人刷数据看板、点审批、
    平台探活 `/health` 全部排在这一次写文件后面。而 admin 的 CRUD
    一次请求里还会连着调 `_ensure_master_data()` + CRUD + 落盘。

    落盘本身是"尽力而为"的自愈层（失败也不阻塞排课），所以丢线程池
    不需要额外的失败处理 —— 语义完全等价。
    """
    await run_in_threadpool(_persist_master_state)


@router.post("/load-default", summary="加载默认实验安排表",
             dependencies=[Depends(rate_limit("load-default"))])
async def load_default():
    """加载已有的 schedule.xlsx，并重放已审批通过的调课（重启不丢调整）

    免鉴权（教秘主路径），但它会**整表重解析**，属单请求真开销接口，
    故挂限流；`_ensure_loaded()` 的自愈是进程内直调，不经过本路由、不受影响。
    """
    # 冷启动时这是 433 行 Excel 的整表解析（本地 ~1s，冷沙箱 3.8s）——同样不能占着事件循环。
    #
    # 必须拿 _load_lock：`_load_default_schedule` 是原地改写（先 loaded=False、
    # entries=[] 再填充），实测并发读取方会 100% 读到 0 条。
    # `_ensure_loaded()` 走双检锁，但本路由是直调，历史上没加锁 —— R4 补上。
    def _locked_load():
        with _load_lock:
            return _load_default_schedule()

    msg, total = await run_in_threadpool(_locked_load)
    return {"success": True, "message": msg, "total_entries": total}


# 轻量知识源：从实验安排表提取实验项目信息（P1-3，不依赖外部文档）
def _build_knowledge_context() -> str:
    """从实验安排表提取实验项目知识（实验名/教室/教师/班级）"""
    if not _db.loaded:
        return ""
    experiments = set()
    rooms = set()
    teachers = set()
    for e in _db.entries:
        if e.experiment:
            experiments.add(e.experiment)
        if e.room:
            rooms.add(e.room)
        if e.teacher:
            teachers.add(e.teacher)
    context = (
        f"本系统实验安排表知识源：\n"
        f"- 实验项目（{len(experiments)}个）：{'、'.join(sorted(experiments)[:15])}\n"
        f"- 实验室（{len(rooms)}间）：{'、'.join(sorted(rooms)[:10])}\n"
        f"- 实验教师（{len(teachers)}位）：{'、'.join(sorted(teachers)[:10])}\n"
    )
    return context


@router.post("/experiment-tutor", response_model=ConsultResponse, summary="实验规范查询（面向排课人员/教师）",
             dependencies=[Depends(rate_limit("experiment-tutor"))])
async def experiment_tutor_consult(req: ConsultRequest):
    """实验规范查询助手：面向排课人员/教师，回答实验项目的学时、所需场地、
    操作步骤、数据处理与误差分析规范、报告规范等问题，服务于排课核对。"""
    if not req.question.strip():
        return ConsultResponse(success=False, message="请输入问题")

    # 自愈：知识源是从**内存里的排课表**提取的（实验名/教室/教师/班级）。
    # 沙箱回收后 `_db.loaded=False` ⇒ `_build_knowledge_context()` 直接返回空串 ⇒
    # 模型在**没有任何本校实验数据**的情况下作答，且返回体里看不出差别
    # （2026-10-29 复查发现的静默降级：不报错，只是答得泛）。
    await _ensure_loaded_async()

    # 实验规范查询基于实验知识源上下文
    context = _build_knowledge_context()
    result = await run_in_threadpool(_agent.llm_parser.consult, req.question,
                                     context=context, role="experiment_tutor")

    if "error" in result:
        return ConsultResponse(success=False, message=result["error"])
    return ConsultResponse(success=True, message="实验规范查询完成", reply=result.get("reply", ""))


# 管理员固定账号（演示用，生产可换数据库）
#
# 凭据已移到 `app/security.py`，支持环境变量覆盖（ADMIN_USER / ADMIN_PASS）。
# 原先这里直接写死 `ADMIN_USER = "admin"` / `ADMIN_PASS = "admin123"`，
# 而前端 bundle 里也有一份同样的明文（用于客户端比对），双重暴露。


@router.post("/admin-login", summary="管理员登录（审批台用）",
             dependencies=[Depends(rate_limit("admin-login"))])
async def admin_login(req: AdminLoginRequest):
    """管理员登录：校验口令并**签发服务端会话令牌**。

    返回值里带 token，调用方后续请求需带 `Authorization: Bearer <token>`。
    不做"只回一句 success 但不发凭据"的事——那样等于没有会话概念，
    后续请求依然是匿名的（这正是修复前的问题）。
    """
    if not check_admin_credentials(req.username, req.password):
        # 生产没配 ADMIN_PASS 时，别说"密码错"——那会让人一直试默认口令。
        from app.security import admin_password_is_default, is_demo_mode
        if admin_password_is_default() and not is_demo_mode():
            raise HTTPException(
                status_code=403,
                detail="生产环境未配置管理员口令（ADMIN_PASS）：默认演示口令已禁用。"
                       "请在部署环境设置 ADMIN_PASS 后重启；本地演示请用 DEMO_MODE=1 启动。",
            )
        # 不区分"账号错/口令错"：区分会告诉爆破者账号是否存在，
        # 这与 test_admin_login_rejects_wrong_username 的意图一致。
        # 但补一句"怎么办"，否则用户不知道是重试还是找管理员。
        raise HTTPException(
            status_code=401,
            detail="用户名或密码错误，请重新输入；多次失败请稍后再试或联系系统管理员。",
        )
    ticket = issue_admin_token(req.username.strip())
    return {
        "success": True,
        "message": "✅ 管理员登录成功",
        "role": "admin",
        "token": ticket["token"],
        "expires_in": ticket["expires_in"],
        "expires_at": ticket["expires_at"],
    }


class ParallelSolveRequest(BaseModel):
    """并行排课求解请求"""
    class_name: str
    weekday: Optional[str] = None   # 上课星期（默认用主数据该班的上课星期）
    start_week: Optional[int] = None  # 起始周（默认用主数据该班的起始周）
    weeks: Optional[int] = None       # 窗口宽度（默认用主数据该班的周次范围）


class AutoScheduleRequest(BaseModel):
    """全校一键排课请求。默认全表分片；传 max_sections 则退回"只排前 N 个"。"""
    max_sections: Optional[int] = None
    chunk_size: int = 60


@router.get("/parallel/overview", summary="并行排课-课程总览（项目×分组）")
async def parallel_overview():
    """按班级聚合：每门课的全部实验项目、学生组别（并行排课入口数据）"""
    await _ensure_loaded_async()
    from app.engine.parallel_scheduler import ParallelScheduler

    # 待排任务（表里没排时间的行 + 上传的大纲清单）与已排记录一起进总览
    extra = await _schedulable_extra_tasks_async()
    ps = ParallelScheduler(_db, extra_tasks=extra)
    return {
        "success": True,
        "data": {
            "courses": ps.get_course_overview(),
            "pending_total": len(extra),
        },
    }


@router.post("/parallel/solve", summary="并行排课-一键求解（访谈核心需求）",
             dependencies=[Depends(rate_limit("parallel-solve"))])
async def parallel_solve(req: ParallelSolveRequest):
    """并行排课：一门课全部实验项目×学生组别，CP-SAT 一键统一分配（周次/时段/场地）

    求解是 CPU 密集且同步（实测首次 0.9–1.7s）—— 放线程池，别让一键排课把
    同一时刻在刷沙盘/看板的人一起冻住。
    """
    await _ensure_loaded_async()
    _ensure_master_data()
    from app.engine.parallel_scheduler import ParallelScheduler

    extra, task_assumptions = _enrich_extra_tasks()
    ps = ParallelScheduler(_db, extra_tasks=extra)

    # 教务给定的「课程总时间范围」：主数据里该班的星期 / 节次 / 周次，是求解器的可用时间空间。
    # 请求显式指定的优先；否则用主数据基线（反推自安排表，教秘可在「主数据」里改）。
    cls_info = _master_data.classes.get(req.class_name, {}) if _master_data.initialized else {}
    md_time_slots = sorted(cls_info.get("time_slots") or []) or None
    md_week_min = cls_info.get("week_min")
    md_week_max = cls_info.get("week_max")

    # 起始周：请求显式指定优先，否则主数据该班起始周（或默认 2）
    start_week = req.start_week if req.start_week is not None else (md_week_min or 2)
    # 周次上限：请求显式传了 weeks（窗口宽度）则走原有窗口逻辑；否则用主数据该班结束周当上限
    week_max_override = None if req.weeks is not None else md_week_max

    result = await run_in_threadpool(
        ps.solve, req.class_name,
        weekday=(req.weekday or cls_info.get("weekday") or None),
        start_week=start_week,
        weeks=(req.weeks if req.weeks is not None else 10),
        time_slots=md_time_slots,
        week_max_override=week_max_override,
    )
    # 灰箱补全假设：任务层（教师/场地自动补全）+ 求解层（星期/节次/周次默认值）。
    # 求解失败时错误信息本身就是结论，不必再叠一层假设。
    if task_assumptions and result.get("success"):
        result["assumptions"] = task_assumptions + list(result.get("assumptions") or [])
    return result


@router.post("/parallel/solve-all", summary="并行排课-全校一键（逐班·按天，组级）",
             dependencies=[Depends(rate_limit("parallel-solve"))])
async def parallel_solve_all():
    """全校一键排课：逐班求解（组级），班与班之间回填同星期的教室/教师占用。

    与 `/auto-schedule` 的分工：
      - `/auto-schedule` 是**教师×课程×班级**粒度的全校初稿（不区分学生组）；
      - 本接口是**班级×项目×学生组**粒度（与安排表同一口径），结果可直接对照/导出班级课表。
    时间范围取主数据该班的「课程总时间范围」（星期可多天、节次、周次）。
    """
    await _ensure_loaded_async()
    _ensure_master_data()
    from app.engine.parallel_scheduler import ParallelScheduler

    extra, task_assumptions = _enrich_extra_tasks()
    ps = ParallelScheduler(_db, extra_tasks=extra)

    class_names = sorted(_master_data.classes) if _master_data.initialized else \
        sorted({e.class_name for e in _db.entries if e.class_name})
    specs = []
    for name in class_names:
        c = _master_data.classes.get(name) or {}
        specs.append({
            "class_name": name,
            "weekday": c.get("weekday") or None,
            "weekdays": sorted(c.get("weekdays") or []) or None,
            "start_week": c.get("week_min") or 2,
            "weeks": 10,
            "time_slots": sorted(c.get("time_slots") or []) or None,
            "week_max": c.get("week_max"),
        })

    result = await run_in_threadpool(ps.solve_school, specs)
    if result.get("success") and task_assumptions:
        result["assumptions"] = task_assumptions
    return result


# 节次顺序 / 表名净化 / 节次文本 / 排序键 / 工作簿序列化 / Response 组装
#   这一组纯函数已在 R54 搬到 `app/api/xlsx_helpers.py`，由文件头统一 import 转出。
#   `_SLOT_ORDER` 与 `_XLSX_MEDIA` 两个常量随函数一起走，**只在新模块定义一份** ——
#   两边各留一份就等于埋了个"改一处、另一处不动"的漂移点。
#   `_write_sheet` 同理，见下方原位置附近的说明。


# `_write_sheet`（写表头 + 列宽）已搬到 `app/api/xlsx_helpers.py`，文件头那个 import
# 已经把它转出，所以下面 6 处 `_write_sheet(...)` 调用一行都不用改。


# R79：统计/体检族（`/data-audit`、`/stats`、`/schedule-grid`、`/impact-analysis`）
# 与专用辅助 `_course_gap_note` 已搬到 `app/api/insights.py`。本模块不再自己
# 定义这一族；文件末尾 `__getattr__` 惰性转发旧名，5 个测试文件里的
# `api.data_audit()` / `api.stats()` / `api.impact_analysis()` 直调照旧可用。


@router.post("/auto-schedule", summary="排课自动生成（P1）",
             dependencies=[Depends(rate_limit("auto-schedule"))])
async def auto_schedule(req: Optional[AutoScheduleRequest] = None):
    """用约束求解器基于现有数据自动生成排课初稿（带周次，避开现有安排表）。

    默认**全表覆盖**：真实表 171 个教学任务分片求解（单次 171 个任务实测 6 秒时限
    内连可行解都拿不到；60 个一片可解）。传 `max_sections` 可回到"只排前 N 个"的
    旧行为（调试/演示用）。
    """
    await _ensure_loaded_async()

    from app.engine.auto_scheduler import AutoScheduler
    extra_tasks, task_assumptions = _enrich_extra_tasks()
    scheduler = AutoScheduler(
        _db,
        extra_tasks=extra_tasks,
    )
    if req and req.max_sections:
        result = await run_in_threadpool(scheduler.solve, max_sections=req.max_sections)
        truncated = True
    else:
        chunk_size = (req.chunk_size if req else 60) or 60
        result = await run_in_threadpool(scheduler.solve_all, chunk_size=chunk_size)
        truncated = False

    real_entries = len(_db.entries)
    total_all = result.get("total_sections_all", 0)
    in_scope = result.get("total_sections", 0)
    solved = result.get("solved", 0)

    # R65：失败时**直接返回原因**，不拼定位说明。
    #
    # `solve()` / `solve_all()` 的空前置分支只返回 `{success: False, error: ...}`
    # （如"没有可排课的教学任务"/"没有可用教室"）。而这里原本**不区分成败**，
    # 无条件拼出一整段定位说明 + summary()，于是教秘看到的是一份
    # 结构完整、术语专业、语气肯定的文档，只是数字全是 0，
    # 真正的原因一个字都没出现 —— 他会怀疑数据没加载，然后反复重试。
    #
    # 与 R64（降级产物标来源）同族：说清楚发生了什么，别给一份看起来正常的产物。
    if not result.get("success"):
        return {
            "success": False,
            "message": result.get("error") or "排课失败（求解器未给出原因）",
            "data": {
                "total_sections": in_scope,
                "total_sections_all": total_all,
                "solved": solved,
                "solved_groups": result.get("solved_groups", solved),
                "total_groups": result.get("total_groups", total_all),
                "truncated": truncated,
                "chunks": result.get("chunks", []),
                "conflicts": result.get("conflicts") or [],
                "weeks": result.get("weeks", []),
            },
        }
    # 组课时口径（R-72）：段是求解单位，组课时才是交付单位。
    # 旧版把两者混成 `solved/total_all`，等于用"压扁后的段"冒充"全部学生组"。
    solved_groups = result.get("solved_groups", solved)
    total_groups = result.get("total_groups", total_all)
    if truncated:
        scope_text = (f"本次取前 {in_scope} 个教学段求解（其余 {max(0, total_all - in_scope)} 个未参与，"
                      f"如需全部请不传 max_sections）")
    elif result.get("chunks"):
        chunk_desc = "、".join(f"第{c['index']}片 {c['solved']}/{c['size']}"
                               for c in result["chunks"])
        scope_text = (f"本次全表覆盖：{in_scope} 个教学段分 {len(result['chunks'])} 片求解"
                      f"（{chunk_desc}），用时 {result.get('elapsed_s', 0)} 秒")
    else:
        scope_text = f"本次求解 {in_scope} 个教学段"

    note = (
        f"📌 定位说明：自动排课为辅助初稿生成。全表共 {total_all} 个教学段"
        f"（= {total_groups} 个「实验项目 × 学生组」课时；"
        f"另有 {result.get('intro_excluded', 0)} 个绪论课任务教室已另行安排，不参与自动排课），\n"
        f"    {scope_text}；共排定 {solved} 段 / {solved_groups} 个组课时。"
        f"方案已逐周避开现有安排表（共 {real_entries} 条记录）占用的教室、教师与班级。\n"
        f"    同一教学段的多个学生组按真实表口径**同周、同教室、连续时段**展开，"
        f"可直接落地为正式安排表。\n"
        f"    未排入的 {len(result.get('conflicts') or [])} 个任务/组已逐条列出原因；"
        f"生成结果仅供教秘做初稿参考，落地前请按「我的课程 / 数据体检」复核。"
    )
    # 灰箱补全：替教秘做的默认决定（自动分配教师/场地）附加在定位说明之后，不静默
    if task_assumptions and result.get("success"):
        note += "\n\n🧩 灰箱补全（信息不全时系统自动做的决定）:\n" + \
                "\n".join(f"  · {a}" for a in task_assumptions)
    result["summary_text"] = note + "\n\n" + scheduler.summary(result)

    return {
        "success": result.get("success", False),
        "message": result.get("summary_text", result.get("error", "排课失败")),
        "data": {
            "total_sections": in_scope,
            "total_sections_all": total_all,
            "intro_excluded": result.get("intro_excluded", 0),
            "solved": solved,
            "solved_groups": solved_groups,
            "total_groups": total_groups,
            "truncated": truncated,
            "coverage": f"{solved_groups}/{total_groups} 组课时（{solved}/{total_all} 段）",
            "chunks": result.get("chunks", []),
            "elapsed_s": result.get("elapsed_s"),
            "weeks": result.get("weeks", []),
            "conflicts": result.get("conflicts", []),
            "schedule": result.get("schedule", []),
            "capacity_checked": result.get("capacity_checked", False),
            "real_entries": real_entries,
        },
    }


@router.get("/teachers", summary="教师列表（F1 身份选择）")
async def get_teachers():
    """返回全部教师列表（含角色/职称），供身份选择页展示（F1）"""
    await _ensure_loaded_async()

    teachers = _db.get_all_teachers()
    # 角色备注在**首次**补填时要开一次工作簿（实测 402ms）——那一下不能占着事件循环。
    # 正常情况走内存（解析时已带出）或解析缓存，是纯字典查找，此时不付线程池成本。
    roles = await run_in_threadpool(_db.get_teacher_roles)  # 从教师联系表提取角色备注
    return {
        "success": True,
        "teachers": [
            {
                "name": name,
                "title": roles.get(name, "实验教师"),
            }
            for name in teachers
        ],
        "total": len(teachers),
    }


@router.get("/teachers/{teacher_name}/courses", summary="教师课程列表（F2）")
async def get_teacher_courses(teacher_name: str):
    """按教师提取课程卡片，标注可调状态（F2）

    R50：课程归属必须真算一遍。此前这张卡片只有实验项目名，教师看不到自己
    上的是哪门课（`course_bridge.resolve_course` 早就存在，只是没接到这里）。
    主数据未初始化时先自愈 —— 否则「项目→课程」映射为空，433 条会全部回落
    默认课程，卡片看起来正常但归属是假的。
    """
    await _ensure_loaded_async()
    _ensure_master_data()

    courses = _db.get_teacher_courses(teacher_name, _master_courses_snapshot())
    if not courses:
        raise HTTPException(status_code=404, detail=f"未找到教师「{teacher_name}」的课程")

    return {
        "success": True,
        "teacher": teacher_name,
        "courses": courses,
        "total": len(courses),
    }




class ApplyMoveRequest(BaseModel):
    """把一条已批准的调课落到排课数据上（云端审批台专用）。

    为什么单独开一个接口，而不是让前端去调 `/requests/{id}/approve`：
    审批台的主数据在**云表** `reschedule_requests`（工单可跨设备、跨会话），
    本地 SQLite 的 `/requests` 前端一行都没调用。两边 id 体系不同，硬接会串号。
    所以这里只做"应用"这一件事：脱离任何工单表，按调课内容移动排课记录。
    """
    teacher: str = ""
    course_name: str = ""
    class_name: str = ""
    original_time: str = ""
    target_week: int
    source_week: int = 0
    key: str = ""  # 幂等键，建议直接传云端工单 id（如 "cloud-123"）


# SQLite 存储（重启不丢数据）
_DB_PATH = os.path.join(_DATA_DIR, "requests.db")
os.makedirs(os.path.dirname(_DB_PATH), exist_ok=True)


def _get_db():
    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _init_db():
    conn = _get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            teacher TEXT NOT NULL,
            course_name TEXT NOT NULL,
            class_name TEXT DEFAULT '',
            original_time TEXT DEFAULT '',
            target_week INTEGER NOT NULL,
            reason TEXT DEFAULT '',
            status TEXT DEFAULT 'pending',
            admin_comment TEXT DEFAULT '',
            created_at TEXT DEFAULT ''
        )
    """)
    try:
        conn.execute("ALTER TABLE requests ADD COLUMN source_week INTEGER DEFAULT 0")
    except Exception:
        pass  # 列已存在
    conn.execute("""
        CREATE TABLE IF NOT EXISTS applied_moves (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_data TEXT NOT NULL,
            applied_at TEXT DEFAULT ''
        )
    """)
    try:
        # 幂等键：同一张云端工单可能被重复应用（网络重试、手滑点两次、审批台刷新）。
        # `_apply_reschedule` 的源周是"目标周之前最近的那个匹配周"，重复执行时目标周
        # 那条已被移走，它会去移**更早一周**的那条 —— 一张工单能连着撬动好几周。
        conn.execute("ALTER TABLE applied_moves ADD COLUMN idem_key TEXT DEFAULT ''")
    except Exception:
        pass  # 列已存在
    # 幂等键唯一索引（2026-10-29）。为什么光有"先查后写"不够：
    # 查重与写入分在两个连接、两次 `execute` 之间没有任何互斥，并发（网络重试 +
    # 审批台刷新同时到达）会双双查到"没有"然后各写一行。实测 8 线程加 barrier
    # 同时进：同一 idem_key 落库 **8 行**（期望 1）；放宽到更接近真实抖动的并发
    # （20 轮 × 6 线程）仍有 1/20 轮重复。
    # 危害要说准：污染的是**账本**（审计轨迹、撤销依据、`/demo/reset` 的计数），
    # 不是课表本身 —— 重放按时间序且源周只匹配一次，不会级联挪多周。
    # 空串不参与唯一（历史行与"本来就没给键"的调用都存 ''，不能互相撞）。
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_applied_moves_idem "
        "ON applied_moves(idem_key) WHERE idem_key != ''"
    )
    # 落地方案表（与 applied_moves 同模式：重启重放的写事件账本）
    _landings().init_schema()
    conn.commit()
    conn.close()


_init_db()


def _label_contains(claim: str, actual: str) -> bool:
    """班级/课程名的**退让档**：包含即可（教秘常只写简称，如「扭摆法」）。

    为什么只有这一个还留在这里：它**不**是通用判据 —— 只用于工单匹配的第二档，
    且必须由调用方判歧义（见 `_match`）。精确档已统一到
    `excel_parser.same_teacher/same_label`（全仓唯一实现，2026-10-29 合并）。

    只在精确档一条都没匹配上时才用，且由调用方负责**歧义即拒绝** —— 见 `_match`。
    """
    a, b = (claim or "").strip(), (actual or "").strip()
    if not a or not b:
        return False
    return a in b or b in a


def _parse_original_time(text: str) -> dict:
    """从 original_time（如 '周四 13:30-15:45 · A504'）解析 星期/时段/教室。

    ⚠️ 这是**兜底**：云端工单表只存了给人读的文案，没有结构化字段，只能反解。
    反解必须保守 —— **认不出就留空**（调用方据此拒绝），绝不能猜。三处实测过的坑：

    · 星期 `周[一二三四五]` 漏掉周末。真实表 433 条里 71 条（16%）在周六/周日，
      一所学校"金工 5、6 周排周六"本就是常态。漏掉之后 "周六 上午 · A412-2" 的
      星期解析为空 ⇒ 星期不参与匹配 ⇒ 第 6 周周六和周日各有一条同名课，
      **一张工单把两天的课一起搬走**（实测 applied=2）。
    · 兜底数字正则 `(?<!\\d)([1-9])[1-9]{1,2}(?!\\d)` 会从**教室号**里抠数字：
      `A412-2` → "412"、"A504" → 不匹配（0 不在 [1-9]）纯属巧合。上午时段的卡片
      文案是 `周六 上午 · A412-2`（没有 HH:MM），于是时段被解析成 "412" ⇒
      一个字都匹配不到 ⇒ 审批"通过"了，课表一个字节没变。
    · 跨节次写法 `14:00-17:00` 映射到主标签 567，判匹配时也只比主标签 ——
      见 `_apply_reschedule` 里的 blocked 匹配。
    """
    import re
    out = {"weekday": "", "time_slot": "", "room": ""}
    if not text:
        return out

    # 教室在最后一个 "·" 之后：先切开，后面所有反解只在**教室之前**那段里找，
    # 教室号就再也不会被当成节次。
    head, sep, tail = text.partition("·")
    if sep:
        out["room"] = tail.strip()

    m = re.search(r"周([一二三四五六日天])", head)
    if m:
        out["weekday"] = "周" + ("日" if m.group(1) == "天" else m.group(1))

    # 时段：① 真实时间区间 → 归一化  ② 明确的节次码  ③ "上午/早上"
    m = re.search(r"(\d{1,2}:\d{2}\s*[-~—]\s*\d{1,2}:\d{2})", head)
    if m:
        from app.utils.excel_parser import TIME_SLOT_NORMALIZE
        key = re.sub(r"\s", "", m.group(1)).replace("—", "-").replace("~", "-")
        out["time_slot"] = TIME_SLOT_NORMALIZE.get(key, "")
    if not out["time_slot"]:
        m = re.search(r"(?<![\dA-Za-z])(567|678|91011|78|34)(?!\d)", head)
        if m:
            from app.utils.excel_parser import TIME_SLOT_NORMALIZE
            out["time_slot"] = TIME_SLOT_NORMALIZE.get(m.group(1), "")
    if not out["time_slot"] and re.search(r"上午|早上", head):
        out["time_slot"] = "MORNING"
    return out


def _find_free_slot(e, target_week):
    """（A 自动改签）在目标周找一个不撞核心冲突、该课可落进去的格子。

    优先级：同天同时段(换教室) > 同天换时段 > 换天同时段 > 换天换时段。
    "空闲"用同一个 `classify_move`（场地/教师/整组三类）判定 —— 与移动前的
    冲突检查同口径，保证改签落点不会制造新的核心冲突。
    同时段候选沿用该记录原来的真实占用节次（跨节次记录占两格）；
    换时段候选只占新时段本身。返回 (weekday, time_slot, room, 占用节次) 或 None。
    """
    from app.conflict_model import classify_move

    days = sorted({x.weekday for x in _db.entries if (x.weekday or "").strip()})
    slots = sorted({x.time_slot for x in _db.entries if (x.time_slot or "").strip()})
    rooms = sorted({x.room for x in _db.entries if (x.room or "").strip()})
    cur_day, cur_slot = e.weekday, e.time_slot
    cur_room = (e.room or "").strip()

    # "无场地"仅在原课本就无场地时才是候选：从真教室改到"无场地"是降格，不是改签
    room_cands = rooms + ([""] if not cur_room else [])
    if cur_room in room_cands:
        room_cands.remove(cur_room)
        room_cands.insert(0, cur_room)

    cands = []
    for rm in room_cands:                                  # 1) 同天同时段，换教室
        cands.append((cur_day, cur_slot, rm))
    for ts in slots:                                       # 2) 同天，换时段
        if ts != cur_slot:
            cands.extend((cur_day, ts, rm) for rm in room_cands)
    for wd in days:                                        # 3) 换天，同时段
        if wd != cur_day:
            cands.extend((wd, cur_slot, rm) for rm in room_cands)
    for wd in days:                                        # 4) 换天，换时段
        if wd != cur_day:
            for ts in slots:
                if ts != cur_slot:
                    cands.extend((wd, ts, rm) for rm in room_cands)

    for wd, ts, rm in cands:
        want = set(getattr(e, "blocked", None) or [cur_slot]) if ts == cur_slot else {ts}
        if not classify_move(
                _db.entries, week=target_week, weekday=wd, slots=want,
                room=rm, teacher=e.teacher, class_name=e.class_name,
                group=e.group, exclude=e):
            return wd, ts, rm, sorted(want)
    return None


# R83：两条闭环共用的幂等键解析。`/apply-move`（云端审批台）与
# `/requests/{id}/approve`（演示模式本机工单）都会调 `_apply_reschedule`，
# 但 R82 只给前者补了内容指纹，后者仍传 ""。库层唯一索引**刻意**放过空串
# （历史行 + 本来就没给键的调用都存 ''，见 test_apply_match.py 的守卫），
# 空串等于没防：探针实测两张内容相同的工单先后批准，第二次会去撬更早一周。
# 指纹只取调课六字段做规范化 —— requests 表整行的 id / status /
# admin_comment / created_at / reason 都不是调课内容；若不排除，两条闭环对
# 同一调课会算出不同的键，跨路径照样重复应用。
# 边界：库层「空串互不冲突」的约定一字未动，helper 只负责给调用方一个非空键；
# 历史账本里已有的 '' 行不会与新 auto: 键互相命中（迁移缺口见 ROUND_LOG）。
_IDEM_KEY_FIELDS = ("teacher", "course_name", "class_name", "original_time",
                    "target_week", "source_week")
# 两个周次字段用 int 归一化：`/apply-move` 的请求体给 int，requests 表整行
# 读出来也是 int；若一边写 0、另一边写 "0"，JSON 里就会一个数字一个字符串，
# 同一调课算出两个键 —— 跨路径幂等在最不起眼的地方漏掉。
_IDEM_INT_FIELDS = frozenset({"target_week", "source_week"})


def _resolve_idem_key(record: dict, explicit: str = "") -> str:
    """显式键优先；否则按调课内容生成稳定的 `auto:` 隐式幂等键。"""
    key = (explicit or "").strip()
    if key:
        return key
    normalized = {
        f: (int(record.get(f) or 0) if f in _IDEM_INT_FIELDS
            else str(record.get(f) or ""))
        for f in _IDEM_KEY_FIELDS
    }
    canonical = json.dumps(normalized, sort_keys=True, ensure_ascii=False,
                           separators=(",", ":"))
    return "auto:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


def _apply_reschedule(record: dict, persist: bool = True, idem_key: str = "") -> dict:
    """审批通过后自动应用调课：把匹配的排课记录移动到目标周

    匹配条件：教师 + （班级或课程名模糊匹配）+ original_time 解析出的 星期/时段/教室（非空才校验）。
    源周：record.source_week > 0 时用之；否则自动选目标周之前最近的一个匹配周。
    目标周该时段被其他班/教师占用 → 跳过并说明。

    `idem_key` 非空时先查重：同一个键只应用一次。云端审批台是**前端直接写云表**，
    没有服务端事务替它挡重复提交，所以幂等必须落在这一步。
    """
    if idem_key:
        conn = _get_db()
        try:
            hit = conn.execute(
                "SELECT applied_at FROM applied_moves WHERE idem_key = ? AND idem_key != '' LIMIT 1",
                (idem_key,),
            ).fetchone()
        finally:
            conn.close()
        if hit:
            # R82：`already_applied` 是给端点看的**结构化信号**。此前端点只能靠
            # applied=0/skipped=0 去猜，于是把"已应用过"误报成"未找到记录，请人工修改"
            # （与 details 里那句"已应用过"自相矛盾）。不许靠中文文案反解状态。
            return {
                "applied": 0, "skipped": 0, "already_applied": True,
                "details": [f"该申请已于 {hit['applied_at']} 应用过，本次未重复移动"],
            }

    parsed = _parse_original_time(record.get("original_time", ""))
    target_week = record.get("target_week")
    teacher = record.get("teacher", "")
    class_name = record.get("class_name", "")
    course_name = record.get("course_name", "")

    def _match(e, strict: bool) -> bool:
        """匹配一条记录。`strict=True` 时班级/课程名按精确档，False 时允许包含档。"""
        # 教师：**只有精确档**，永不退让到子串（子串会让「吴琳」搬走「吴琳琳」的课）
        if teacher and not _teacher_matches(teacher, e.teacher or ""):
            return False
        if parsed["weekday"] and e.weekday != parsed["weekday"]:
            return False
        # 按**真实占住的节次**比，不是主标签相等：`14:00-17:00` 主标签 567，
        # 但它同时占着 678 的前 1 小时 15 分，卡片上回显的就是 "14:00-17:00"。
        if parsed["time_slot"] and \
                parsed["time_slot"] not in set(getattr(e, "blocked", None) or [e.time_slot]):
            return False
        if parsed["room"] and e.room != parsed["room"]:
            return False
        if class_name:
            ok = _label_matches(class_name, e.class_name or "")
            return ok if strict else (ok or _label_contains(class_name, e.class_name or ""))
        if course_name:
            ok = _label_matches(course_name, e.experiment or "")
            return ok if strict else (ok or _label_contains(course_name, e.experiment or ""))
        return True

    # 两档匹配：先精确，再退让到包含。为什么需要退让档 —— 教秘写「扭摆法」而表里是
    # 「扭摆法测物体转动惯量」，只认精确会让真实工单全部落空。
    # 为什么退让档之后要判**歧义**：包含关系可能一次命中多条不同课程/班级
    # （「声速」同时是「声速测量」与别的课的前缀时），那说明这条申请指代不明，
    # 必须拒绝并说清"你说的是哪一个"，而不是挑一条挪走 —— 多动一条他以为系统乱改课表。
    candidates = [e for e in _db.entries if _match(e, strict=True)]
    if not candidates:
        loose = [e for e in _db.entries if _match(e, strict=False)]
        if loose:
            # 歧义判据只看"被包含的那个字段本身"，不看星期/教室 —— 同一条课跨周多条是常态
            key_field = (lambda e: (e.class_name or "", e.experiment or "")) if class_name \
                else (lambda e: (e.experiment or "",))
            distinct = {key_field(e) for e in loose}
            if len(distinct) > 1:
                samples = "、".join(sorted({" / ".join(x for x in k if x) for k in distinct})[:5])
                return {
                    "applied": 0, "skipped": 0,
                    "details": [f"申请里的「{class_name or course_name}」能对上不止一条课程/班级（{samples}），"
                                f"为避免挪错，本次未做任何改动 —— 请写明完整名称。"],
                }
        candidates = loose

    if not candidates:
        # 说清"哪一段没认出来"，否则教秘只会看到"未找到匹配的排课记录"，
        # 以为是自己记错了课，而实际是卡片文案里有系统读不懂的写法。
        unknown = [k for k in ("weekday", "time_slot", "room") if not parsed[k]]
        hint = f"（申请原文：{record.get('original_time', '') or '空'}"
        if unknown:
            hint += f"；其中 {'/'.join({'weekday': '星期', 'time_slot': '时段', 'room': '教室'}[k] for k in unknown)} 未能识别"
        hint += "）"
        return {"applied": 0, "skipped": 0,
                "details": [f"未找到匹配的排课记录（可能已在教务系统中人工调整）{hint}"]}

    # 选源周
    if record.get("source_week", 0) > 0:
        src_entries = [e for e in candidates if e.week == record["source_week"]]
    else:
        weeks = sorted({e.week for e in candidates if e.week != target_week})
        earlier = [w for w in weeks if w < target_week]
        src_week = earlier[-1] if earlier else (weeks[0] if weeks else None)
        src_entries = [e for e in candidates if e.week == src_week] if src_week else []

    if not src_entries:
        return {"applied": 0, "skipped": 0, "details": ["未找到可移动的源周记录"]}

    # ⚠️ 反解不出星期时**不许"全都搬"**。真实表里周六/周日各有一节的同名课是常态
    # （24服工_4 周六、24服工_5 周日，同教师同教室同实验同周次）。原先星期为空 ⇒
    # 星期不参与过滤 ⇒ 一张只申请"周六那节"的工单把周日的也搬走了（实测 applied=2）。
    # 少动一条教秘能自己补，多动一条他会以为系统乱改课表。
    days = sorted({e.weekday for e in src_entries})
    if not parsed["weekday"] and len(days) > 1:
        return {
            "applied": 0, "skipped": len(src_entries),
            "details": [
                f"该申请未写明星期，而第{src_entries[0].week}周「{src_entries[0].experiment}」"
                f"在 {'、'.join(days)} 各有一条（{src_entries[0].class_name}）。"
                f"为避免一次搬走两天的课，本次未做任何改动 —— 请在申请里写明是哪一天。"
            ],
        }

    applied, skipped, rescheduled, details, conflicts = 0, 0, 0, [], []
    for e in src_entries:
        # 目标周该格是否已有人：按**真实占住的节次**比，而不是主标签相等。
        # 源记录是 `14:00-17:00`（主标签 567）时，目标周的 678 若已有课，
        # 主标签比较会判"不冲突"，于是把两条课叠在同一间教室里（R-40）。
        #
        # 2026-10-22 升级为**分级**：旧逻辑只回一个布尔 + 一句"已有安排"，
        # 教秘分不清是教室被占（换一间就行）还是老师本人有课（必须改时间）。
        # 顺带补上原先完全没查的**整组冲突**，并修掉一个潜在假阳性：
        # 旧写法 `o.room == e.room` 在两条都无场地（绪论课）时恒真 ⇒ 凭空报冲突。
        from app.conflict_model import classify_move

        hits = classify_move(
            _db.entries, week=target_week, weekday=e.weekday,
            slots=set(getattr(e, "blocked", None) or [e.time_slot]),
            room=e.room, teacher=e.teacher, class_name=e.class_name,
            group=e.group, exclude=e)
        if hits:
            # A 自动改签（2026-10-30）：撞核心冲突不再一律"跳过"——先在目标周找一个
            # 空闲格（优先级 同天同时段换教室 > 同天换时段 > 换天同时段 > 换天换时段，
            # "空闲"用同一个 classify_move 判定），自动挪过去。"调课=改签"的闭环
            # 这样才真正成立：课真的动了，且明细里说清改签去了哪。确无空闲格才跳过。
            alt = _find_free_slot(e, target_week)
            if alt:
                wd, ts, rm, want = alt
                orig_slot, orig_desc = e.time_slot, f"{e.weekday} {e.time_slot} {e.room or '无场地'}"
                details.append(
                    f"第{e.week}周 {e.group or e.class_name} {e.experiment}："
                    f"原时段 {orig_desc} 撞核心冲突（{'、'.join(dict.fromkeys(h['label'] for h in hits))}）"
                    f" → 已自动改签 第{target_week}周 {wd} {ts} {rm or '无场地'}")
                e.weekday, e.time_slot, e.room, e.week = wd, ts, rm, target_week
                e.blocked = list(want)
                if ts != orig_slot:
                    e.time_raw = ""  # 原时间区间与新时段不再对应，清掉按新时段显示
                conflicts.extend(hits)  # 改签成功也留痕：审计要能解释为什么换了格子
                rescheduled += 1
                applied += 1
                continue
            skipped += 1
            kinds = "；".join(dict.fromkeys(
                f"{h['label']}（{h['fix_hint']}）" for h in hits))
            details.append(
                f"第{e.week}周 {e.weekday} {e.time_slot} {e.group or e.class_name}："
                f"目标周该时段撞上核心冲突 —— {kinds}，且目标周无空闲时段，本次未移动（必须调整后再申请）")
            conflicts.extend(hits)
            continue
        details.append(f"{e.weekday} {e.time_slot} {e.room} {e.experiment}（{e.class_name}/{e.group}）：第{e.week}周 → 第{target_week}周")
        e.week = target_week
        applied += 1

    if persist and applied > 0:
        # 幂等键的唯一索引是**最后一道闸**：并发下两个请求都可能通过上面的"先查"，
        # 但只有一个能写进去。写失败的那个必须如实说"已被应用过"——
        # 而不是抛 500 让教秘以为审批坏了（课表其实已经被另一个请求改好了）。
        conn = _get_db()
        try:
            conn.execute(
                "INSERT INTO applied_moves (request_data, applied_at, idem_key) VALUES (?, ?, ?)",
                # 微秒精度：重启重放按时间戳排序（后发生者赢），秒级精度会让同一秒内的
                # 落地/调课无法区分，排序退化成人为定的 kind 顺序 —— 那就是静默吞事件。
                (json.dumps(record, ensure_ascii=False),
                 datetime.now().isoformat(timespec="microseconds"),
                 idem_key or ""),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            conn.rollback()
            # 内存里这次移动已经做了（幂等地把该记录的 week 设成目标周，与赢家相同），
            # 所以课表状态是对的；只是账本不该有第二行。如实说明，别让教秘以为审批坏了。
            return {
                "applied": 0, "skipped": 0, "already_applied": True,
                "details": ["该申请已由另一个并发请求应用过，课表已是目标周状态，本次未重复记账"],
            }
        finally:
            conn.close()

    return {"applied": applied, "skipped": skipped, "rescheduled": rescheduled,
            "details": details, "conflicts": conflicts}




@router.post("/apply-move", summary="把已批准的调课应用到排课数据（审批台闭环）",
             dependencies=_ADMIN_ONLY)
async def apply_move(req: ApplyMoveRequest):
    """把一条已批准的调课真正落到排课数据上。

    为什么需要它：审批台改的是**云表** status，而排课数据在本地进程内存里。
    在此之前，教秘点"同意"之后排课数据一个字节都没变 —— 沙盘、我的课程、数据体检
    全都看不见这次调课，而这套能移动记录、能检冲突、能持久化重放的逻辑（
    `_apply_reschedule` + `applied_moves`）只挂在 `/requests/{id}/approve` 上，
    而那个接口前端一行都没调用。审批成了纯标记。

    所以这里把"应用"单独暴露出来，由前端在云端审批成功后调用。语义：
      · 不碰任何工单表，只按调课内容移动排课记录；
      · `key` 做幂等（建议传云端工单 id），重复调用不会连着撬动更早的周次；
      · 移动失败/冲突跳过一律如实返回，不假装成功（"同意但没生效"必须让教秘看见）。
    """
    if not _db.loaded:
        await _ensure_loaded_async()

    record = {
        "teacher": req.teacher,
        "course_name": req.course_name,
        "class_name": req.class_name,
        "original_time": req.original_time,
        "target_week": req.target_week,
        "source_week": req.source_week,
    }
    # R82：`key` 可省略（默认 ""），而库层唯一索引**刻意放过空串**
    # （历史行 + 本来就没给键的调用都存 ''，见 test_apply_match.py 的守卫）。
    # 后果是"不传 key"的 /apply-move 连查重都不做：第二次会去匹配目标周之前
    # **更早一周**的同名课（探针实测第二次盯上第 4 周），只是恰好撞冲突被跳过；
    # 目标周若另有空闲格，`_find_free_slot` 会自动改签成功 ⇒ 真的重复撬动。
    # 端点层补一个内容指纹当隐式幂等键：库层"空串互不冲突"的约定一字不动，
    # 显式 key（前端传 cloud-<工单id>）仍优先；内容变了就是另一张工单，指纹随之变。
    # R83：算法抽进 `_resolve_idem_key`，让演示模式审批也用同一把尺子算键 ——
    # 否则 /apply-move 写下的 auto: 键挡不住审批路径，反之亦然。
    key = _resolve_idem_key(record, req.key or "")

    # 与 land_commit 共用写锁（R28）：`_apply_reschedule` 的"读-判定-写"
    # 三步必须原子，否则并发调课会把两条课排进同一间教室。
    def _locked_apply():
        with _DB_WRITE_LOCK:
            return _apply_reschedule(record, True, key)

    result = await run_in_threadpool(_locked_apply)

    conflicts = result.get("conflicts") or []
    applied = result.get("applied", 0)
    skipped = result.get("skipped", 0)
    already_applied = bool(result.get("already_applied"))
    if already_applied:
        # R82：重复提交不是"没找到记录"。探针实测旧文案是
        # 「⚠️ 未找到可自动移动的排课记录（请在教务系统中人工修改）」
        # 紧跟一句「该申请已于 … 应用过」—— 教秘照前半句去教务系统再改一遍，
        # 就是一次重复调课。这里只说实话，也不再叠加 details 里那句重复解释。
        message = "ℹ️ 这条调课此前已应用过，本次未重复移动，无需人工处理"
    elif applied:
        n_resched = result.get("rescheduled", 0)
        message = f"✅ 已移动 {applied} 条排课记录到第{req.target_week}周"
        if n_resched:
            message += f"（其中 {n_resched} 条自动改签到空闲时段）"
        if skipped:
            message += f"，{skipped} 条因目标周无空闲时段未移动"
    elif skipped:
        kinds = "、".join(dict.fromkeys(c["label"] for c in conflicts)) or "已有安排"
        message = (f"⚠️ 调课已批准，但目标周撞上**核心冲突**（{kinds}），"
                   f"{skipped} 条记录未自动移动 —— 核心冲突必须调整，不能强制通过")
    else:
        message = "⚠️ 调课已批准，但未找到可自动移动的排课记录（请在教务系统中人工修改）"

    details = result.get("details") or []
    if details and not already_applied:
        message += "\n" + "\n".join(details[:6])

    return {"success": True,
            "data": {"applied": applied, "skipped": skipped, "details": details,
                     "conflicts": conflicts, "already_applied": already_applied},
            "message": message}


# ---------------------------------------------------------------------------
# R76/R77：落地方案端点族搬到 `app/api/landing.py`，主数据维护端点族搬到
# `app/api/master.py`，旧名在此惰性转发
#
# 测试里有 `api.land_commit` / `api.LandPlanRequest` 这类**模块属性**直调写法
# （`tests/test_event_loop_blocking.py` 用 `asyncio.run(api.land_commit(...))`
# 直接驱动端点，测锁到底在不在工作线程上）。搬一次家若顺手改这些调用点，等于
# 顺手改了守卫本身 —— 而这一族正是 R73 / R74 / R75 三个连续缺陷的守卫所在。
#
# 用模块级 `__getattr__`（PEP 562）而不是文件末尾 `from app.api.landing import ...`：
#   ① 普通 import 会成环 —— `landing` 顶部要 `from app.api import reschedule` 取状态
#      （`_db` / `_land_lock` 是可变模块属性，必须每次现取），本模块再反向 import 它，
#      先被导入的那一侧会拿到半初始化的模块并抛 ImportError（本轮实测过）。
#   ② `__getattr__` 只在**属性不存在时**触发，导入期一次都不跑，环自然不存在。
#   ③ `landing` 先被导入也不会炸：它的 import 会先把本模块完整跑完，
#      `__getattr__` 到调用点才执行。
# 代价：`dir(reschedule)` 与 IDE 补全看不到这几个名字。它们只被测试引用，
# 生产代码没有任何一处 `reschedule.land_*`（本轮全仓 grep 确认）。
# ---------------------------------------------------------------------------
_LANDING_MOVED = frozenset({
    "LandPlanRequest", "_land_plan_or_400", "land_preview",
    "_locked_land_commit", "land_commit", "_locked_land_undo", "land_undo",
    "land_history", "land_status",
})


# R77：主数据维护端点族搬到 `app/api/master.py`。
#
# 与 landing 同一套理由，但多一条**具体的**：这一族有 5 个请求体模型
# （`_CourseIn` / `_ExperimentIn` / `_TeacherIn` / `_RoomIn` / `_ClassIn`）。
# 模型跟着端点走，不留在 reschedule 里 —— 否则"字段定义在这、用它的接口在那"，
# 下一次加字段时改了模型以为生效，实际搬走的那个端点读的是另一份定义。
#
# 当时**没**转发的那三个（`/export/master-workbook` 与 `_master_workbook_bytes` /
# `_master_workbook_build`）留在了本模块 —— 它们属于导出族（缓存 + 线程池 + 门禁），
# 与"维护主数据"是两件事。R78 已按那次说好的做法把导出族整块搬走，见 `_EXPORT_MOVED`。
_MASTER_MOVED = frozenset({
    "_CourseIn", "_ExperimentIn", "_TeacherIn", "_RoomIn", "_ClassIn",
    "master_overview", "master_init",
    "master_upsert_course", "master_delete_course",
    "master_upsert_experiment", "master_delete_experiment",
    "master_upsert_teacher", "master_delete_teacher",
    "master_upsert_room", "master_delete_room",
    "master_upsert_class", "master_delete_class",
    "master_import_preview", "master_tasks_preview", "master_import",
})


# R78：导出族搬到 `app/api/export.py`。
#
# 这一族是**一次搬完的**（5 个端点 + 2 个请求体 + 缓存 + 预热），因为它们之间
# 共享同一份字节缓存：只搬一半会留下"两个模块各有一份 `_EXPORT_CACHE`"，
# 命中率直接腰斩而功能完全正常 —— 是最难在测试里看见的一种退化。
#
# `/requests/export` 当年没跟着搬：它是 SQLite 工单的投影，不读排课表、不走缓存。
# R80 工单族整体搬进 `app/api/requests_board.py` 时它一起走了，但**仍不进导出族**
# —— 归属判据在 `tests/test_export_module_split.py`，别按名字把它扫回来。
_EXPORT_MOVED = frozenset({
    "ParallelExportRequest", "AutoScheduleExportRequest",
    "export_master_workbook", "_master_workbook_bytes", "_master_workbook_build",
    "parallel_export",
    "export_class_schedule", "_class_schedule_bytes", "_class_schedule_build",
    "export_room_schedule", "_room_schedule_bytes", "_room_schedule_build",
    "export_master_data", "_master_data_bytes", "_master_data_build",
    "auto_schedule_export", "_auto_schedule_bytes", "_auto_schedule_build",
    "_EXPORT_CACHE", "_EXPORT_CACHE_MAX", "_EXPORT_FORMAT_VERSION",
    "_entries_digest", "_master_digest", "_cached_export",
    "_EXPORT_WARM_THREAD", "_warm_export_cache", "start_export_warmup",
})


# R54 搬走的 7 个 XLSX 纯函数：R78 之前靠文件头的值导入"转出"，现在本模块
# 一个使用者都不剩了，改成转发到 `xlsx_helpers`（同一个对象，不是拷贝）。
_XLSX_MOVED = frozenset({
    "_entry_sort_key", "_safe_sheet_name", "_slot_text", "_workbook_bytes",
    "_write_sheet", "_xlsx_bytes_response", "_xlsx_response",
})




# R79：统计/体检族搬到 `app/api/insights.py`。
#
# 与前三族同一套理由，但这一族**没有共享状态**可搬：`_db` / `_master_data` /
# `_agent` 都留在本模块，新模块按 `_api.xxx` 每次现取。所以这里的转发清单只含
# 4 个端点函数与 1 个只被本族使用的纯函数 `_course_gap_note`。
#
# R80 更正：这三行曾断言 M50 锚点在 /stats，实测它在
# reschedule.py:929（教师角色补全），从未离开本文件。真正随工单族搬走的是
# M15 / M16，它们的靶文件已改为 app/api/requests_board.py。
_INSIGHTS_MOVED = frozenset({
    "data_audit", "stats", "schedule_grid", "impact_analysis", "_course_gap_note",
})

# R80：工单族搬到 `app/api/requests_board.py`。
#
# 这一族的转发清单只含 5 个端点函数 + 2 个请求体。
# SQLite 基础设施与所有共享状态**不在此列**：它们仍真定义在本模块，
# 新模块一律 `_api.xxx` 现取。若把 `_DB_PATH` 也加进来，
# monkeypatch 会在旧模块上新建属性，新模块则继续读真实库——测试全绿。
_REQUESTS_MOVED = frozenset({
    "RescheduleRequestCreate", "RescheduleRequestApprove",
    "create_request", "clear_requests", "list_requests", "export_requests",
    "approve_request",
})


def __getattr__(name):
    """搬迁后的旧名转发（landing / master / export / insights / requests_board / xlsx_helpers）。

    `from app.api.reschedule import land_commit` 与 `api._cached_export` 同样生效。
    """
    if name in _LANDING_MOVED:
        from app.api import landing
        return getattr(landing, name)
    if name in _MASTER_MOVED:
        from app.api import master
        return getattr(master, name)
    if name in _EXPORT_MOVED:
        from app.api import export
        return getattr(export, name)
    if name in _XLSX_MOVED:
        from app.api import xlsx_helpers
        return getattr(xlsx_helpers, name)
    if name in _INSIGHTS_MOVED:
        from app.api import insights
        return getattr(insights, name)
    if name in _REQUESTS_MOVED:
        from app.api import requests_board
        return getattr(requests_board, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
