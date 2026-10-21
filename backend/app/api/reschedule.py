"""调课 API - 上传数据 + 查询空闲时段"""

import os
import json
import re
import threading
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, UploadFile, File, HTTPException
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from app.utils.excel_parser import ScheduleDatabase
from app.agent.reschedule_agent import RescheduleAgent
from app.agent.student_schedule import StudentSchedule
from app.security import check_admin_credentials, issue_admin_token, require_admin
from app.ratelimit import rate_limit

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
#   - /upload、/upload-student   替换排课表/学生课表（写）
#   - /student-status            吐真实学生姓名与学号（PII）
#   - GET/POST/DELETE /requests  工单读写与清空（写，DELETE 会清全表）
#   - /requests/{id}/approve     审批并自动应用调课，直接改排课结果（写）
#
# 注意：这些写接口前端**已不再调用**（调课/审批走云数据库 + RLS），
# 因此加门禁不影响任何演示流程，只是把"匿名可写"改成"需管理员会话"。
#
# 第二道边界是**限流**（见 app/ratelimit.py）：免鉴权的演示接口里有几个
# 带真实资源代价——/chat、/course-select、/experiment-tutor 会调外部 LLM
# （烧额度），/auto-schedule、/parallel/solve 是 CP-SAT 求解（单次最多 10 秒 CPU），
# /admin-login 可被爆破。它们不能加鉴权（会废掉演示），所以按 IP + 全站双维度限流。
# 阈值刻意放宽，真人演示碰不到，脚本刷量会立刻 429。
# ---------------------------------------------------------------------------
_ADMIN_ONLY = [Depends(require_admin)]

# 全局排课数据库 + 学生课表
_db = ScheduleDatabase()
_student = StudentSchedule()
_agent = RescheduleAgent(_db, student=_student)

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
# 学生个人身份（姓名 + 学号）来自独立数据源，其接口已挂管理员门禁（见 _ADMIN_ONLY）。
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
_DATA_DIR = os.path.join(_APP_DIR, "data")

_EXCEL_SUFFIXES = (".xlsx", ".xls")
_STUDENT_SUFFIXES = (".xlsx", ".xls", ".pdf")


def _safe_upload_name(filename: str, allowed: tuple, prefix: str = "") -> str:
    """把调用方给的文件名压成安全文件名（只保留白名单后缀 + 清洗过的主干）。

    保留中文主干是有意的：`upload-student` 会用 `os.path.splitext(filename)[0]`
    当作学生姓名，全换成随机串会把这个信息丢掉。
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


@router.post("/upload", summary="上传实验安排表 Excel", dependencies=_ADMIN_ONLY)
async def upload_schedule(file: UploadFile = File(...)):
    """上传实验安排表 Excel 文件（需管理员会话）"""
    # 保存上传的文件（文件名经净化，杜绝路径穿越）
    save_path = _upload_target(file.filename, _EXCEL_SUFFIXES)

    content = await file.read()
    with open(save_path, "wb") as f:
        f.write(content)

    try:
        _db.load_from_excel(save_path)
        return {
            "success": True,
            "message": f"✅ 成功加载 {len(_db.entries)} 条排课记录",
            "total_entries": len(_db.entries),
            "rooms": _db.get_all_rooms()[:10],
            "teachers": _db.get_all_teachers(),
            "week_range": list(_db.get_week_range()),
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"文件解析失败：{str(e)}")


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
            "total": result.get("total", 0),
            "need_info": result.get("need_info", False),
            "followup_questions": result.get("followup_questions", []),
            "context": result.get("context") or {},
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
        "ai": parser.describe() if parser is not None else {"configured": False},
    }


@router.post("/upload-student", summary="上传学生课表 Excel/PDF", dependencies=_ADMIN_ONLY)
async def upload_student_schedule(file: UploadFile = File(...)):
    """上传学生课表文件
    - Excel（.xlsx/.xls）：表头 [课程名, 星期, 节次, 周次]
    - PDF（.pdf）：教务系统导出的课表，自动解析（尽力）
    """
    # 文件名净化后再落盘：切掉目录部分并只保留白名单后缀。
    # 学生姓名取自**净化后**的主干，避免把 `..`/路径片段写进展示字段。
    safe_name = _safe_upload_name(file.filename, _STUDENT_SUFFIXES, prefix="student_")
    save_path = _upload_target(file.filename, _STUDENT_SUFFIXES, prefix="student_")

    content = await file.read()
    with open(save_path, "wb") as f:
        f.write(content)

    try:
        if safe_name.lower().endswith('.pdf'):
            from app.utils.pdf_schedule_parser import PdfScheduleParser
            parser = PdfScheduleParser()
            courses = parser.parse(save_path)
            if not courses:
                raise ValueError("PDF 解析出 0 门课程，请确认是教务系统导出的课表")
            _student.name = os.path.splitext(safe_name)[0].removeprefix("student_")
            _student.courses = courses
            count = len(courses)
        else:
            count = _student.load_from_excel(save_path)

        return {
            "success": True,
            "message": f"✅ 成功加载 {count} 门课程，已更新学生课表",
            "total_courses": count,
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"文件解析失败：{str(e)}")


@router.get("/student-status", summary="查看学生课表状态", dependencies=_ADMIN_ONLY)
async def get_student_status():
    """查看学生课表信息"""
    return {
        "name": _student.name,
        "student_id": _student.student_id,
        "semester": _student.semester,
        "total_courses": len(_student.courses),
        "sample_courses": _student.courses[:5],
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

    _db.load_from_excel(DEFAULT_SCHEDULE_PATH)
    # 重放已应用的调课记录（applied_moves），保证重启后调整仍然生效
    replay_applied, replay_skipped = 0, 0
    try:
        conn = _get_db()
        rows = conn.execute("SELECT request_data FROM applied_moves ORDER BY id").fetchall()
        conn.close()
        for r in rows:
            try:
                rec = json.loads(r["request_data"])
                res = _apply_reschedule(rec, persist=False)
                replay_applied += res["applied"]
                replay_skipped += res["skipped"]
            except Exception:
                pass
    except Exception:
        pass

    msg = f"✅ 成功加载 {len(_db.entries)} 条排课记录"
    if replay_applied:
        msg += f"（已重放 {replay_applied} 条审批通过的调课）"
    return msg, len(_db.entries)


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
    """
    if _db.loaded:
        return
    with _load_lock:
        if _db.loaded:  # 双检：等锁期间可能已被别的请求加载完
            return
        _load_default_schedule()


@router.post("/load-default", summary="加载默认实验安排表",
             dependencies=[Depends(rate_limit("load-default"))])
async def load_default():
    """加载已有的 schedule.xlsx，并重放已审批通过的调课（重启不丢调整）

    免鉴权（教秘主路径），但它会**整表重解析**，属单请求真开销接口，
    故挂限流；`_ensure_loaded()` 的自愈是进程内直调，不经过本路由、不受影响。
    """
    # 冷启动时这是 433 行 Excel 的整表解析（本地 ~1s，冷沙箱 3.8s）——同样不能占着事件循环
    msg, total = await run_in_threadpool(_load_default_schedule)
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
        f"- 涉及学生课表：{len(_student.students)}人（多学生课表）"
    )
    return context


@router.post("/course-select", response_model=ConsultResponse, summary="选课咨询（四大模块之一）",
             dependencies=[Depends(rate_limit("course-select"))])
async def course_select_consult(req: ConsultRequest):
    """选课咨询：LLM 回答选课资格/先修课程/学分要求等问题"""
    if not req.question.strip():
        return ConsultResponse(success=False, message="请输入问题")

    # 构建学生课表 + 实验知识源上下文
    context = (
        f"当前学生：{_student.name}（{_student.semester}），课表共 {len(_student.courses)} 门课程。\n"
        + _build_knowledge_context()
    )
    result = await run_in_threadpool(_agent.llm_parser.consult, req.question,
                                     context=context, role="course_select")

    if "error" in result:
        return ConsultResponse(success=False, message=result["error"])
    return ConsultResponse(success=True, message="选课咨询完成", reply=result.get("reply", ""))


@router.post("/experiment-tutor", response_model=ConsultResponse, summary="实验辅导（四大模块之一）",
             dependencies=[Depends(rate_limit("experiment-tutor"))])
async def experiment_tutor_consult(req: ConsultRequest):
    """实验辅导：LLM 回答实验原理/数据处理/误差分析/报告规范等问题"""
    if not req.question.strip():
        return ConsultResponse(success=False, message="请输入问题")

    # 实验辅导基于实验知识源上下文
    context = _build_knowledge_context()
    result = await run_in_threadpool(_agent.llm_parser.consult, req.question,
                                     context=context, role="experiment_tutor")

    if "error" in result:
        return ConsultResponse(success=False, message=result["error"])
    return ConsultResponse(success=True, message="实验辅导完成", reply=result.get("reply", ""))


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
        raise HTTPException(status_code=401, detail="用户名或密码错误")
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
    weekday: Optional[str] = None  # 上课星期（默认沿用历史）
    start_week: int = 2
    weeks: int = 10


class ParallelExportRequest(BaseModel):
    """并行排课方案导出请求"""
    class_name: str
    schedule: list  # [{project, group, week, weekday, time_slot, room, teacher}]


@router.get("/parallel/overview", summary="并行排课-课程总览（项目×分组）")
async def parallel_overview():
    """按班级聚合：每门课的全部实验项目、学生组别（并行排课入口数据）"""
    _ensure_loaded()
    from app.engine.parallel_scheduler import ParallelScheduler
    ps = ParallelScheduler(_db, student=_student)
    return {"success": True, "data": {"courses": ps.get_course_overview()}}


@router.post("/parallel/solve", summary="并行排课-一键求解（访谈核心需求）",
             dependencies=[Depends(rate_limit("parallel-solve"))])
async def parallel_solve(req: ParallelSolveRequest):
    """并行排课：一门课全部实验项目×学生组别，CP-SAT 一键统一分配（周次/时段/场地）

    求解是 CPU 密集且同步（实测首次 0.9–1.7s）—— 放线程池，别让一键排课把
    同一时刻在刷沙盘/看板的人一起冻住。
    """
    _ensure_loaded()
    from app.engine.parallel_scheduler import ParallelScheduler
    ps = ParallelScheduler(_db, student=_student)
    result = await run_in_threadpool(
        ps.solve, req.class_name,
        weekday=req.weekday, start_week=req.start_week, weeks=req.weeks,
    )
    return result


@router.post("/parallel/export", summary="并行排课方案导出 Excel",
             dependencies=[Depends(rate_limit("parallel-export"))])
async def parallel_export(req: ParallelExportRequest):
    """把并行排课方案导出为 Excel（贴合排课员真实工作流）"""
    from io import BytesIO
    import openpyxl
    from openpyxl.styles import Font, Alignment
    from fastapi.responses import StreamingResponse
    from urllib.parse import quote

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "并行排课方案"
    headers = ["周次", "星期", "时段", "场地", "学生组", "实验项目", "教师"]
    ws.append(headers)
    for c in ws[1]:
        c.font = Font(bold=True)
        c.alignment = Alignment(horizontal="center")
    for s in req.schedule:
        ws.append([s.get("week"), s.get("weekday"), s.get("time_slot"),
                   s.get("room"), s.get("group"), s.get("project"), s.get("teacher")])
    for col, w in zip("ABCDEFG", [8, 8, 14, 10, 18, 30, 12]):
        ws.column_dimensions[col].width = w

    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    filename = quote(f"并行排课方案_{req.class_name}.xlsx")
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{filename}"},
    )


@router.get("/data-audit", summary="数据体检报告（扫描现有安排表冲突）")
async def data_audit():
    """一键审计现有实验安排表：教室冲突 / 教师时间冲突 / 数据覆盖情况"""
    _ensure_loaded()

    from collections import defaultdict

    room_map = defaultdict(list)     # (week, weekday, slot, room) -> entries
    teacher_map = defaultdict(list)  # (week, weekday, slot, teacher) -> entries
    weeks, teachers, rooms = set(), set(), set()
    missing_room = []                # 无场地条目：不能参与"同室"判定，但要作为数据缺口报出来

    for e in _db.entries:
        weeks.add(e.week)
        if e.teacher:
            teachers.add(e.teacher)
        if (e.room or '').strip():
            rooms.add(e.room)
            room_map[(e.week, e.weekday, e.time_slot, e.room)].append(e)
        else:
            # ⚠️ 曾经无条件放进 room_map：于是一堆"空场地"被归到同一个 key
            # (week, weekday, slot, '')，凡两条以上就报"教室冲突（同室同段多班）"，
            # 而 place 是空串 —— 界面上显示成"第1周 周五 18:30-20:45 ·"，场地空着。
            # 没填场地不可能撞场地，这是纯假阳性；真正该说的是"这些条没场地"。
            missing_room.append(e)
        if e.teacher:
            teacher_map[(e.week, e.weekday, e.time_slot, e.teacher)].append(e)

    def _find_conflicts(source):
        found = []
        for key, entries in source.items():
            parties = {f"{e.class_name}/{e.group}" for e in entries if e.class_name}
            if len(parties) > 1:  # 同一时段同一资源被多个班级/组使用
                found.append({
                    "week": key[0], "weekday": key[1], "time_slot": key[2],
                    "place": key[3],
                    "involved": sorted(parties),
                    "detail": [f"{e.class_name}/{e.group} {e.experiment}（{e.teacher or '未指派教师'}）"
                               for e in entries],
                })
        found.sort(key=lambda c: (c["week"], c["weekday"], str(c["time_slot"])))
        return found

    room_conflicts = _find_conflicts(room_map)
    teacher_conflicts = _find_conflicts(teacher_map)

    # 数据缺口（不是冲突）：这些条目没填场地，教室冲突扫描覆盖不到它们。
    # 单独报出来 —— 沉默地少扫 16 条，比报一个假冲突更危险。
    missing_samples = [
        f"{e.class_name}/{e.group} {e.experiment}（第{e.week}周 {e.weekday}）"
        for e in missing_room[:5]
    ]

    return {
        "success": True,
        "data": {
            "healthy": not room_conflicts and not teacher_conflicts,
            "total_entries": len(_db.entries),
            "teacher_count": len(teachers),
            "room_count": len(rooms),
            "week_min": min(weeks) if weeks else 0,
            "week_max": max(weeks) if weeks else 0,
            "room_conflict_count": len(room_conflicts),
            "teacher_conflict_count": len(teacher_conflicts),
            "missing_room_count": len(missing_room),
            "missing_room_samples": missing_samples,
            "room_conflicts": room_conflicts[:30],
            "teacher_conflicts": teacher_conflicts[:30],
        },
    }


@router.get("/stats", summary="统计面板（教室利用率/教师工作量）")
async def stats():
    """统计现有安排表：教室利用率、教师工作量均衡度"""
    _ensure_loaded()

    from collections import defaultdict, Counter

    weeks = {e.week for e in _db.entries}
    n_weeks = (max(weeks) - min(weeks) + 1) if weeks else 0
    total_slots = n_weeks * 5 * 3  # 5 天 × 3 时段

    room_sessions = Counter()
    room_weeks = defaultdict(set)
    teacher_sessions = Counter()
    teacher_experiments = defaultdict(set)
    teacher_weeks = defaultdict(set)

    for e in _db.entries:
        if e.room:
            room_sessions[e.room] += 1
            room_weeks[e.room].add(e.week)
        if e.teacher:
            teacher_sessions[e.teacher] += 1
            teacher_experiments[e.teacher].add(e.experiment)
            teacher_weeks[e.teacher].add(e.week)

    rooms_stat = [
        {
            "room": rm,
            "sessions": n,
            "utilization": round(n / total_slots * 100, 1) if total_slots else 0,
        }
        for rm, n in room_sessions.most_common()
    ]
    teachers_stat = [
        {
            "teacher": t,
            "sessions": n,
            "experiments": len(teacher_experiments[t]),
            "active_weeks": len(teacher_weeks[t]),
        }
        for t, n in teacher_sessions.most_common()
    ]
    loads = [t["sessions"] for t in teachers_stat]
    avg_load = sum(loads) / len(loads) if loads else 0
    balance = round((1 - (max(loads) - min(loads)) / max(loads)) * 100) if loads and max(loads) > 0 else 100

    return {
        "success": True,
        "data": {
            "n_weeks": n_weeks,
            "room_count": len(rooms_stat),
            "teacher_count": len(teachers_stat),
            "avg_room_utilization": round(sum(r["utilization"] for r in rooms_stat) / len(rooms_stat), 1) if rooms_stat else 0,
            "busiest_room": rooms_stat[0] if rooms_stat else None,
            "avg_teacher_load": round(avg_load, 1),
            "teacher_load_balance": balance,
            "top_rooms": rooms_stat[:10],
            "top_teachers": teachers_stat[:10],
        },
    }


@router.post("/impact-analysis", summary="冲突影响分析（差异化功能）",
             dependencies=[Depends(rate_limit("impact-analysis"))])
async def impact_analysis(req: ChatRequest):
    """调课影响分析：把某课调到第N周，会影响哪些班级/教室/教师

    基于三重冲突筛选结果，聚合出影响范围清单。
    """
    _ensure_loaded()

    # 解析请求（复用调课智能体解析）
    parsed = _agent.parse_request(req.message)
    if not parsed["target_week"]:
        raise HTTPException(status_code=400, detail="无法确定目标周次，请说明如'调到第5周'")

    target_week = parsed["target_week"]
    teacher_name = parsed.get("teacher_name")

    # 周次范围校验（R-32 的另一半）：影响分析同样按 `e.week == target_week` 过滤，
    # 周次落在表外时"冲突数 = 0"，而 0 冲突会被读成"这周完全没影响"——
    # 比报错更危险。这里直接 400，把"查不了"和"没影响"分开。
    _bounds = _agent.week_bounds()
    if _bounds and not (_bounds[0] <= target_week <= _bounds[1]):
        raise HTTPException(
            status_code=400,
            detail=(
                f"第{target_week}周不在实验安排表范围内"
                f"（本表覆盖第{_bounds[0]}–{_bounds[1]}周），无法做影响分析"
            ),
        )

    # 冲突筛选（含原因）
    result = _agent.find_slots_with_reasons(target_week, teacher_name=teacher_name)
    excluded = result["excluded"]

    # 聚合影响范围
    #
    # ⚠️ 这里**不允许**再从 `reason` 文案里正则反解字段（历史实现用
    # `re.search(r"（([^）]+) ", e["reason"])` 捞括号内容，结果把班级名塞进了
    # 「受影响教师」——因为 teacher_busy 的文案里本来就没有教师名）。
    # 文案是给人读的，改个标点就静默解析失败；数据必须来自结构化字段。
    affected_classes = set()      # 受影响班级（教室被占）
    affected_rooms = set()        # 受影响教室
    affected_teachers = set()     # 受影响教师（占着那间教室的教师）
    affected_students = set()     # 学生课表里被占用的课程
    self_busy = []                # 教师本人该时段已有课 —— 是「自身冲突」，不是「别人受影响」
    conflict_slots = []

    for e in excluded:
        reason_type = e.get("reason_type", "")
        conflict_slots.append({
            "weekday": e["weekday"], "time_slot": e["time_slot"],
            "room": e.get("room", ""), "date": e.get("date", ""),
            "reason_type": reason_type, "reason": e["reason"],
        })
        if reason_type == "classroom_busy":
            affected_rooms.add(e.get("room", ""))
            if e.get("class_name"):
                affected_classes.add(e["class_name"])
            if e.get("teacher"):
                affected_teachers.add(e["teacher"])
        elif reason_type == "teacher_busy":
            self_busy.append({
                "weekday": e["weekday"], "time_slot": e["time_slot"],
                "class_name": e.get("class_name", ""),
                "experiment": e.get("experiment", ""),
            })
        elif reason_type == "student_busy":
            courses = [c for c in (e.get("courses") or []) if c]
            if courses:
                affected_students.update(courses)
            else:
                affected_students.add("学生课表（该时段有课）")

    return {
        "success": True,
        "target_week": target_week,
        "message": f"将课程调到第{target_week}周的影响分析：",
        "data": {
            "conflict_count": len(conflict_slots),
            "affected_classes": sorted(a for a in affected_classes if a),
            "affected_rooms": sorted(r for r in affected_rooms if r),
            "affected_teachers": sorted(t for t in affected_teachers if t),
            "affected_students": sorted(affected_students),
            "self_busy_count": len(self_busy),
            "self_busy_slots": self_busy[:5],
            "conflict_slots": conflict_slots[:10],
        },
    }


@router.post("/auto-schedule", summary="排课自动生成（P1）",
             dependencies=[Depends(rate_limit("auto-schedule"))])
async def auto_schedule():
    """用约束求解器基于现有数据自动生成完整排课方案"""
    _ensure_loaded()

    from app.engine.auto_scheduler import AutoScheduler
    scheduler = AutoScheduler(_db, student=_student)
    result = await run_in_threadpool(scheduler.solve, max_sections=30)

    # 定位说明：自动排课是"辅助初稿生成"，非替代学校已排定的完整安排表
    real_entries = len(_db.entries)
    note = (
        f"📌 定位说明：自动排课为辅助初稿生成（本次简化演示 12 个教学任务），\n"
        f"    学校已排定的完整实验安排表共 {real_entries} 条（含多学院轮转、教师/教室/设备约束），"
        f"可在「我的课程」中查看使用。"
    )
    result["summary_text"] = note + "\n\n" + scheduler.summary(result)

    return {
        "success": result.get("success", False),
        "message": result.get("summary_text", result.get("error", "排课失败")),
        "data": {
            "total_sections": result.get("total_sections", 0),
            "solved": result.get("solved", 0),
            "conflicts": result.get("conflicts", []),
            "schedule": result.get("schedule", []),
            "real_entries": real_entries,
        },
    }


@router.get("/teachers", summary="教师列表（F1 身份选择）")
async def get_teachers():
    """返回全部教师列表（含角色/职称），供身份选择页展示（F1）"""
    _ensure_loaded()

    teachers = _db.get_all_teachers()
    roles = _db.get_teacher_roles()  # 从教师联系表提取角色备注
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
    """按教师提取课程卡片，标注可调状态（F2）"""
    _ensure_loaded()

    courses = _db.get_teacher_courses(teacher_name)
    if not courses:
        raise HTTPException(status_code=404, detail=f"未找到教师「{teacher_name}」的课程")

    return {
        "success": True,
        "teacher": teacher_name,
        "courses": courses,
        "total": len(courses),
    }


# ==================== 调课申请单（F6 审批流，SQLite 持久化） ====================

class RescheduleRequestCreate(BaseModel):
    """教师提交调课申请"""
    teacher: str
    course_name: str
    class_name: str = ""
    original_time: str = ""
    target_week: int
    reason: str = ""
    source_week: int = 0  # 原上课周（0=自动选取目标周之前最近的一周）


class RescheduleRequestApprove(BaseModel):
    """管理员审批"""
    approve: bool  # True=同意, False=驳回
    comment: str = ""


# SQLite 存储（重启不丢数据）
import sqlite3

_DB_PATH = os.path.join(_APP_DIR, "data", "requests.db")
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
    conn.commit()
    conn.close()


_init_db()


def _parse_original_time(text: str) -> dict:
    """从 original_time（如 '周四 13:30-15:45 · A504'）解析 星期/时段/教室"""
    import re
    out = {"weekday": "", "time_slot": "", "room": ""}
    if not text:
        return out
    m = re.search(r"周[一二三四五]", text)
    if m:
        out["weekday"] = m.group(0)
    # 时段：优先时间区间（归一化），其次节次数字（如 567）
    m = re.search(r"(\d{1,2}:\d{2}\s*[-~]\s*\d{1,2}:\d{2})", text)
    if m:
        try:
            from app.utils.excel_parser import TIME_SLOT_NORMALIZE
            key = re.sub(r"\s", "", m.group(1))
            out["time_slot"] = TIME_SLOT_NORMALIZE.get(key, TIME_SLOT_NORMALIZE.get(m.group(1), ""))
        except Exception:
            out["time_slot"] = ""
    if not out["time_slot"]:
        m = re.search(r"(?<!\d)([1-9])[1-9]{1,2}(?!\d)", text)
        if m:
            out["time_slot"] = m.group(0)
    if "·" in text:
        out["room"] = text.split("·")[-1].strip()
    return out


def _apply_reschedule(record: dict, persist: bool = True) -> dict:
    """审批通过后自动应用调课：把匹配的排课记录移动到目标周

    匹配条件：教师 + （班级或课程名模糊匹配）+ original_time 解析出的 星期/时段/教室（非空才校验）。
    源周：record.source_week > 0 时用之；否则自动选目标周之前最近的一个匹配周。
    目标周该时段被其他班/教师占用 → 跳过并说明。
    """
    parsed = _parse_original_time(record.get("original_time", ""))
    target_week = record.get("target_week")
    teacher = record.get("teacher", "")
    class_name = record.get("class_name", "")
    course_name = record.get("course_name", "")

    def _match(e) -> bool:
        if teacher and teacher not in (e.teacher or ""):
            return False
        if parsed["weekday"] and e.weekday != parsed["weekday"]:
            return False
        if parsed["time_slot"] and e.time_slot != parsed["time_slot"]:
            return False
        if parsed["room"] and e.room != parsed["room"]:
            return False
        if class_name:
            return class_name in (e.class_name or "") or (e.class_name or "") in class_name
        if course_name:
            return course_name in (e.experiment or "") or (e.experiment or "") in course_name
        return True

    candidates = [e for e in _db.entries if _match(e)]
    if not candidates:
        return {"applied": 0, "skipped": 0, "details": ["未找到匹配的排课记录（可能已在教务系统中人工调整）"]}

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

    applied, skipped, details = 0, 0, []
    for e in src_entries:
        conflict = any(
            o is not e and o.week == target_week and o.weekday == e.weekday
            and o.time_slot == e.time_slot and (o.room == e.room or (o.teacher and o.teacher == e.teacher))
            for o in _db.entries
        )
        if conflict:
            skipped += 1
            details.append(f"第{e.week}周 {e.weekday} {e.time_slot} {e.group or e.class_name}：目标周该时段已有安排，跳过")
            continue
        details.append(f"{e.weekday} {e.time_slot} {e.room} {e.experiment}（{e.class_name}/{e.group}）：第{e.week}周 → 第{target_week}周")
        e.week = target_week
        applied += 1

    if persist and applied > 0:
        conn = _get_db()
        conn.execute(
            "INSERT INTO applied_moves (request_data, applied_at) VALUES (?, ?)",
            (json.dumps(record, ensure_ascii=False), datetime.now().isoformat(timespec="seconds")),
        )
        conn.commit()
        conn.close()

    return {"applied": applied, "skipped": skipped, "details": details}


@router.post("/requests", summary="教师提交调课申请（F6）", dependencies=_ADMIN_ONLY)
async def create_request(req: RescheduleRequestCreate):
    """教师提交调课申请，状态初始为 pending（SQLite 持久化）"""
    conn = _get_db()
    # 保留上限：超过 200 条时清理最早的已完成记录，避免演示数据膨胀
    try:
        count_row = conn.execute("SELECT COUNT(*) as c FROM requests").fetchone()
        if count_row["c"] >= 200:
            conn.execute(
                "DELETE FROM requests WHERE status != 'pending' "
                "AND id NOT IN (SELECT id FROM requests ORDER BY id DESC LIMIT 100)"
            )
            conn.commit()
    except Exception:
        pass  # 清理失败不影响主流程

    cur = conn.execute(
        "INSERT INTO requests (teacher, course_name, class_name, original_time, target_week, reason, status, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, 'pending', '2026-2027-1')",
        (req.teacher, req.course_name, req.class_name, req.original_time, req.target_week, req.reason),
    )
    conn.commit()
    record = dict(cur.execute("SELECT * FROM requests WHERE id = ?", (cur.lastrowid,)).fetchone())
    conn.close()
    return {"success": True, "message": "✅ 调课申请已提交，等待管理员审批", "request": record}


@router.delete("/requests", summary="清空调课申请（调试/演示用）", dependencies=_ADMIN_ONLY)
async def clear_requests():
    """清空全部调课申请（调试/演示重置用）"""
    conn = _get_db()
    conn.execute("DELETE FROM requests")
    conn.commit()
    conn.close()
    return {"success": True, "message": "✅ 已清空全部调课申请"}


@router.get("/requests", summary="调课申请列表（F6）", dependencies=_ADMIN_ONLY)
async def list_requests(status: str = ""):
    """查看调课申请列表，可按状态过滤（pending/approved/rejected）"""
    conn = _get_db()
    if status:
        rows = conn.execute("SELECT * FROM requests WHERE status = ? ORDER BY id DESC", (status,)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM requests ORDER BY id DESC").fetchall()
    conn.close()
    items = [dict(r) for r in rows]
    return {"success": True, "requests": items, "total": len(items)}


@router.post("/requests/{request_id}/approve", summary="管理员审批（F6）", dependencies=_ADMIN_ONLY)
async def approve_request(request_id: int, req: RescheduleRequestApprove):
    """管理员审批调课申请：同意 → approved；驳回 → rejected（填写原因）"""
    conn = _get_db()
    row = conn.execute("SELECT * FROM requests WHERE id = ?", (request_id,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail=f"未找到申请单 #{request_id}")

    if row["status"] != "pending":
        conn.close()
        raise HTTPException(status_code=400, detail=f"该申请已审批（状态：{row['status']}），不能重复操作")

    if req.approve:
        status = "approved"
        comment = req.comment or "同意调课"
        # 审批通过 → 自动应用调课（移动排课记录到目标周，冲突时段跳过）
        record_dict = dict(row)
        apply_result = {"applied": 0, "skipped": 0, "details": []}
        try:
            if _db.loaded:
                apply_result = _apply_reschedule(record_dict)
        except Exception as e:
            apply_result = {"applied": 0, "skipped": 0, "details": [f"自动应用失败：{e}（可人工在教务系统调整）"]}
        if apply_result["applied"] > 0:
            message = (f"✅ 已同意并自动应用调课：{apply_result['applied']} 条记录移至第{record_dict['target_week']}周"
                       + (f"，{apply_result['skipped']} 条因冲突跳过" if apply_result["skipped"] else ""))
        elif apply_result["skipped"] > 0:
            message = (f"✅ 已同意调课申请，但目标周该时段已有安排，{apply_result['skipped']} 条记录未自动移动"
                       f"（详见下方，请在教务系统中人工协调）")
        else:
            message = "✅ 已同意调课申请（未找到可自动移动的记录，请在教务系统中人工修改）"
        if apply_result["details"]:
            message += "\n" + "\n".join(apply_result["details"][:6])
    else:
        status = "rejected"
        comment = req.comment or "未说明原因"
        message = f"❌ 已驳回调课申请（原因：{comment}）"

    conn.execute("UPDATE requests SET status = ?, admin_comment = ? WHERE id = ?", (status, comment, request_id))
    conn.commit()
    record = dict(conn.execute("SELECT * FROM requests WHERE id = ?", (request_id,)).fetchone())
    conn.close()
    return {"success": True, "message": message, "request": record}
