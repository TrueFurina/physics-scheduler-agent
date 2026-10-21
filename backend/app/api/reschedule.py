"""调课 API - 上传数据 + 查询空闲时段"""

import os
import json
import re
import threading
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, UploadFile, File, HTTPException
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from app.utils.excel_parser import ScheduleDatabase
from app.agent.reschedule_agent import RescheduleAgent
from app.agent.student_schedule import StudentSchedule
from app.master_data import MasterData
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
# 主数据（教学大纲清单）里的待排任务：与"上一次排完的课表行"并列的一类输入。
# 有了它，"排一个去年不存在的新实验项目"不再需要先伪造一行假课表 —— 这是
# 导师座谈后换轨的核心（见 docs/战略复盘-20261022.md）。
_master_tasks: List = []
# 主数据实体库（课程/实验项目/教师/场地/班级）：导师一号行动项"重构数据库"的落地。
# 与 _db.entries 同构为内存态，进程重启后从安排表反推恢复（见 _ensure_master_data）。
_master_data = MasterData()
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
        rep = _db.load_report
        msg = f"✅ 成功加载 {len(_db.entries)} 条排课记录"
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
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"文件解析失败：{str(e)}")


@router.post("/upload-master", summary="上传教学大纲/主数据清单（从零排课的输入）",
             dependencies=_ADMIN_ONLY)
async def upload_master(file: UploadFile = File(...)):
    """上传「课程 → 实验项目 → 学生组（→ 候选教师/场地）」清单。

    这是"从零排课"的输入口：清单天然没有周次/星期/时段，此前这类行会被主解析
    流程静默丢弃。上传后这些任务直接进入并行排课的总览与求解。
    """
    save_path = _upload_target(file.filename, _EXCEL_SUFFIXES)
    content = await file.read()
    with open(save_path, "wb") as f:
        f.write(content)

    try:
        from app.utils.excel_parser import parse_master_tasks

        tasks, report = parse_master_tasks(save_path)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"清单解析失败：{str(e)}")

    _master_tasks.clear()
    _master_tasks.extend(tasks)
    return {
        "success": True,
        "message": f"✅ 已读入 {len(tasks)} 条主数据任务（可直接进并行排课）",
        "total_tasks": len(tasks),
        "classes": sorted({t.class_name for t in tasks}),
        "report": report,
    }


@router.get("/master-status", summary="主数据（大纲清单）状态")
async def master_status():
    """当前主数据任务数、覆盖班级，以及安排表里"没排时间"的待排行数。"""
    _ensure_loaded()
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


# ---------------------------------------------------------------------------
# 主数据（课程/实验项目/教师/场地/班级）CRUD —— 导师一号行动项「重构数据库」
#
# 只读总览免鉴权（它只是把安排表反推成实体，不含学生 PII）；写操作挂 require_admin。
# 每个写接口都先 `_ensure_master_data()`：主数据从安排表反推，教秘的增删改在其上
# 叠加 —— 若在空主数据上直接增删，会把反推基线整个丢掉。
# ---------------------------------------------------------------------------

class _CourseIn(BaseModel):
    name: str


class _ExperimentIn(BaseModel):
    name: str
    course: Optional[str] = ""
    teachers: Optional[List[str]] = None
    rooms: Optional[List[str]] = None
    classes: Optional[List[str]] = None


class _TeacherIn(BaseModel):
    name: str
    projects: Optional[List[str]] = None


class _RoomIn(BaseModel):
    name: str
    capacity: Optional[int] = None


class _ClassIn(BaseModel):
    name: str
    groups: Optional[List[str]] = None


@router.get("/master/overview", summary="主数据五张实体表总览")
async def master_overview():
    """五张实体表（课程/实验项目/教师/场地/班级）的完整状态。免鉴权：纯实体清单。"""
    _ensure_loaded()
    _ensure_master_data()
    return {"success": True, "data": _master_data.overview()}


@router.post("/master/init", summary="从安排表反推主数据（一键初始化）", dependencies=_ADMIN_ONLY)
async def master_init():
    """幂等：清空主数据后从当前安排表重新反推。"""
    _ensure_loaded()
    _master_data.build_from_entries(_db.entries)
    return {"success": True, "data": _master_data.overview()}


@router.post("/master/courses", summary="新建课程", dependencies=_ADMIN_ONLY)
async def master_upsert_course(req: _CourseIn):
    _ensure_loaded()
    _ensure_master_data()
    try:
        _master_data.upsert_course(req.name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"success": True, "data": _master_data.overview()}


@router.delete("/master/courses/{name}", summary="删除课程", dependencies=_ADMIN_ONLY)
async def master_delete_course(name: str):
    _ensure_master_data()
    _master_data.delete_course(name)
    return {"success": True, "data": _master_data.overview()}


@router.post("/master/experiments", summary="新建/更新实验项目", dependencies=_ADMIN_ONLY)
async def master_upsert_experiment(req: _ExperimentIn):
    _ensure_loaded()
    _ensure_master_data()
    try:
        _master_data.upsert_experiment(
            req.name, course=req.course or "", teachers=req.teachers,
            rooms=req.rooms, classes=req.classes)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"success": True, "data": _master_data.overview()}


@router.delete("/master/experiments/{name}", summary="删除实验项目", dependencies=_ADMIN_ONLY)
async def master_delete_experiment(name: str):
    _ensure_master_data()
    _master_data.delete_experiment(name)
    return {"success": True, "data": _master_data.overview()}


@router.post("/master/teachers", summary="新建/更新教师", dependencies=_ADMIN_ONLY)
async def master_upsert_teacher(req: _TeacherIn):
    _ensure_loaded()
    _ensure_master_data()
    try:
        _master_data.upsert_teacher(req.name, projects=req.projects)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"success": True, "data": _master_data.overview()}


@router.delete("/master/teachers/{name}", summary="删除教师", dependencies=_ADMIN_ONLY)
async def master_delete_teacher(name: str):
    _ensure_master_data()
    _master_data.delete_teacher(name)
    return {"success": True, "data": _master_data.overview()}


@router.post("/master/rooms", summary="新建/更新场地", dependencies=_ADMIN_ONLY)
async def master_upsert_room(req: _RoomIn):
    _ensure_loaded()
    _ensure_master_data()
    try:
        _master_data.upsert_room(req.name, capacity=req.capacity)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"success": True, "data": _master_data.overview()}


@router.delete("/master/rooms/{name}", summary="删除场地", dependencies=_ADMIN_ONLY)
async def master_delete_room(name: str):
    _ensure_master_data()
    _master_data.delete_room(name)
    return {"success": True, "data": _master_data.overview()}


@router.post("/master/classes", summary="新建/更新班级", dependencies=_ADMIN_ONLY)
async def master_upsert_class(req: _ClassIn):
    _ensure_loaded()
    _ensure_master_data()
    try:
        _master_data.upsert_class(req.name, groups=req.groups)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"success": True, "data": _master_data.overview()}


@router.delete("/master/classes/{name}", summary="删除班级", dependencies=_ADMIN_ONLY)
async def master_delete_class(name: str):
    _ensure_master_data()
    _master_data.delete_class(name)
    return {"success": True, "data": _master_data.overview()}


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
    # 待排/丢弃都必须说出口：静默丢弃是本项目反复踩的坑（R-35、以及缺周次的整行蒸发）
    rep = _db.load_report
    if rep.get("pending"):
        msg += f"（另有 {rep['pending']} 条待排任务：写了班级与实验项目但没排时间，可直接进并行排课）"
    if rep.get("skipped"):
        msg += f"（⚠️ {rep['skipped']} 条时段无法识别，未纳入冲突扫描）"
    return msg, len(_db.entries)


def _ensure_master_data() -> None:
    """主数据未初始化时，从已加载的安排表反推填充（幂等，进程重启后自愈）。

    与 `_ensure_loaded` 同族：不引入第二套持久化，主数据永远从安排表派生，
    教秘的增删改在会话内有效，重启后回到「安排表反推」的基线。
    """
    if _master_data.initialized:
        return
    if _db.loaded:
        _master_data.build_from_entries(_db.entries)


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
        for cls in ex.get("classes", set()):
            if (cls, exp_name) in scheduled_pairs or (cls, exp_name) in pending_pairs:
                continue
            for g in _master_data.groups_of(cls):
                derived.append(PendingTask(
                    class_name=cls, experiment=exp_name, group=g,
                    teacher=(sorted(ex["teachers"])[0] if ex["teachers"] else ""),
                    room=(sorted(ex["rooms"])[0] if ex["rooms"] else ""),
                    course=ex.get("course") or "", source="master",
                ))
    return derived


def _schedulable_extra_tasks() -> List:
    """可排的待排任务 = 表里没排时间的行 + 上传的大纲清单 + 主数据新增项目，**去掉考试/考核类**。

    考试类（真实表里 3 行「大物实验B/C 考试」）仍留在 `db.pending` 里被报出来，
    只是不参与排课 —— 不静默丢弃的原则同样适用于它们。
    """
    from app.utils.excel_parser import looks_like_exam

    raw = list(getattr(_db, "pending", None) or []) + list(_master_tasks) + _master_derived_tasks()
    return [t for t in raw if not looks_like_exam(getattr(t, "experiment", "") or "")]


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

    # 待排任务（表里没排时间的行 + 上传的大纲清单）与已排记录一起进总览
    extra = _schedulable_extra_tasks()
    ps = ParallelScheduler(_db, student=_student, extra_tasks=extra)
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
    _ensure_loaded()
    from app.engine.parallel_scheduler import ParallelScheduler

    extra = _schedulable_extra_tasks()
    ps = ParallelScheduler(_db, student=_student, extra_tasks=extra)
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


# ---------------------------------------------------------------------------
# 标准课表导出（班级课表 / 场地课表）
#
# 为什么要有这两个接口：教务在用系统（理论课排课工具）被认可的两项能力之一，
# 就是"排完能导出班级课程表、场地课程表"。本系统此前只能导出**新排出来的并行方案**，
# 导不出基于现有安排表的标准课表 —— 教秘拿不到可以直接发到班级群/贴在实验室门口的成品，
# 于是"排完了"和"能交付"之间还差一步人工整理。
# ---------------------------------------------------------------------------

# 节次渲染顺序（与 time_model.ORDER 一致；unknown 排最后，绝不静默丢弃）
_SLOT_ORDER = {"MORNING": 0, "567": 1, "678": 2, "91011": 3, "78": 4}


def _safe_sheet_name(raw: str, used: set) -> str:
    """Excel 表名：最长 31 字符，且不允许 : \\ / ? * [ ]。重名加 ~N 后缀。"""
    name = re.sub(r"[:\\/?*\[\]]", "_", str(raw or "").strip())[:31] or "未标注"
    base, i = name, 1
    while name in used:
        suffix = f"~{i}"
        name = base[: 31 - len(suffix)] + suffix
        i += 1
    used.add(name)
    return name


def _slot_text(e) -> str:
    """给教秘看的上课时间：原表写的是时间区间就照抄（含 14:00-17:00 这类跨节次写法），
    否则用节次码换成时钟（567 → 13:30-15:45）。"""
    raw = (getattr(e, "time_raw", "") or "").strip()
    if ":" in raw or "：" in raw:
        return raw
    from app.utils.time_model import clock_text

    return clock_text(e.time_slot) or raw or e.time_slot


def _entry_sort_key(e):
    from app.engine.parallel_scheduler import ALL_WEEKDAYS

    wd = ALL_WEEKDAYS.index(e.weekday) if e.weekday in ALL_WEEKDAYS else 99
    return (e.week, wd, _SLOT_ORDER.get(e.time_slot, 99), str(e.group))


def _xlsx_response(wb, filename: str):
    """工作簿 → 带中文文件名的下载响应（与并行排课导出同一套口径）。"""
    from io import BytesIO
    from urllib.parse import quote

    from fastapi.responses import StreamingResponse

    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"},
    )


def _write_sheet(ws, headers, widths):
    from openpyxl.styles import Alignment, Font

    ws.append(headers)
    for c in ws[1]:
        c.font = Font(bold=True)
        c.alignment = Alignment(horizontal="center")
    for col, w in zip("ABCDEFGH", widths):
        ws.column_dimensions[col].width = w


@router.get("/export/class-schedule", summary="导出班级课表（教务标准课表之一）",
            dependencies=[Depends(rate_limit("standard-export"))])
async def export_class_schedule(class_name: Optional[str] = None):
    """按班级导出课表：每个班一个 sheet，行=该班全部实验安排（按周次/星期/节次排序）。

    `class_name` 留空 = 导出全部班级（每班一页）。
    """
    _ensure_loaded()
    import openpyxl

    entries = [e for e in _db.entries if (not class_name or e.class_name == class_name)]
    if class_name and not entries:
        raise HTTPException(404, f"安排表里没有班级「{class_name}」的记录")

    grouped: dict = {}
    for e in sorted(entries, key=_entry_sort_key):
        grouped.setdefault(e.class_name or "未标注班级", []).append(e)

    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    used: set = set()
    for cls in sorted(grouped, key=str):
        ws = wb.create_sheet(_safe_sheet_name(cls, used))
        _write_sheet(ws, ["周次", "星期", "节次", "上课时间", "实验项目", "学生组", "教师", "场地"],
                     [8, 7, 8, 14, 30, 12, 10, 12])
        for e in grouped[cls]:
            ws.append([f"第{e.week}周", e.weekday, e.time_slot, _slot_text(e),
                       e.experiment, e.group, e.teacher, e.room or "（未填场地）"])
    if not grouped:
        ws = wb.create_sheet("无数据")
        ws.append(["当前安排表里没有可导出的记录"])
    return _xlsx_response(wb, f"班级课表_{class_name or '全部班级'}.xlsx")


@router.get("/export/room-schedule", summary="导出场地课表（教务标准课表之二）",
            dependencies=[Depends(rate_limit("standard-export"))])
async def export_room_schedule(room: Optional[str] = None):
    """按场地导出课表：每间实验室一个 sheet，行=该场地的全部占用。

    `room` 留空 = 导出全部场地。⚠️ 没填场地的记录（绪论课等）**不能静默丢弃** ——
    它们单独进「（未填场地）」页，否则教秘会以为全表都被扫到了（R-35 口径）。
    """
    _ensure_loaded()
    import openpyxl

    entries = [e for e in _db.entries if (not room or (e.room or "").strip() == room)]
    if room and not entries:
        raise HTTPException(404, f"安排表里没有场地「{room}」的记录")

    grouped: dict = {}
    for e in sorted(entries, key=_entry_sort_key):
        grouped.setdefault((e.room or "").strip() or "（未填场地）", []).append(e)

    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    used: set = set()
    for rm in sorted(grouped, key=str):
        ws = wb.create_sheet(_safe_sheet_name(rm, used))
        _write_sheet(ws, ["周次", "星期", "节次", "上课时间", "班级", "实验项目", "学生组", "教师"],
                     [8, 7, 8, 14, 26, 30, 12, 10])
        for e in grouped[rm]:
            ws.append([f"第{e.week}周", e.weekday, e.time_slot, _slot_text(e),
                       e.class_name, e.experiment, e.group, e.teacher])
    if not grouped:
        ws = wb.create_sheet("无数据")
        ws.append(["当前安排表里没有可导出的记录"])
    return _xlsx_response(wb, f"场地课表_{room or '全部场地'}.xlsx")


@router.get("/export/master-data", summary="导出基础数据台账（数据库重构的起始材料）",
            dependencies=[Depends(rate_limit("standard-export"))])
async def export_master_data():
    """把现有安排表**反推**成四张实体台账：实验项目 / 教师 / 场地 / 班级。

    为什么要有它：教务侧的第一号行动项是"基于教学大纲梳理所有实验课程属性、
    重构数据库"。而本系统手里只有扁平的安排表行，没有独立的课程/项目/场地实体 ——
    教秘想补全实验项目、核对哪些项目只在个别班开、哪些场地利用率异常，无处下手。
    这份台账就是可以直接打开核对、补列、再导回的起始材料。

    ⚠️ 口径：课次一律按**真实占用格数**计（`14:00-17:00` 算 2 格），
    与利用率面板保持一致；缺字段的记录归入"未标注/未指派"而不是丢弃。
    """
    _ensure_loaded()
    import openpyxl
    from collections import defaultdict

    WD = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

    def _cells(e):
        return len(getattr(e, "blocked", None) or [e.time_slot])

    def _week_span(weeks):
        if not weeks:
            return "—"
        ws = sorted(weeks)
        return f"第{ws[0]}-{ws[-1]}周（{len(ws)}周）"

    def _weekday_mix(counter):
        """按周一→周日顺序输出，周末不会被挤到末尾（R-41a）。"""
        parts = [f"{d} {counter[d]}" for d in WD if counter.get(d)]
        parts += [f"{d} {n}" for d, n in counter.items() if d not in WD]
        return "、".join(parts) or "—"

    proj = defaultdict(lambda: {"classes": set(), "groups": set(), "cells": 0,
                                "teachers": set(), "rooms": set(), "weeks": set()})
    teach = defaultdict(lambda: {"cells": 0, "projects": set(), "classes": set(),
                                 "weeks": set(), "wd": defaultdict(int)})
    room = defaultdict(lambda: {"cells": 0, "projects": set(), "classes": set(), "weeks": set()})
    cls = defaultdict(lambda: {"cells": 0, "projects": set(), "groups": set(),
                               "weeks": set(), "teachers": set(), "wd": defaultdict(int)})

    for e in _db.entries:
        c = _cells(e)
        p = proj[e.experiment or "未标注实验项目"]
        p["classes"].add(e.class_name or "未标注班级")
        p["groups"].add(e.group or "")
        p["cells"] += c
        p["teachers"].add(e.teacher or "未指派教师")
        p["rooms"].add((e.room or "").strip() or "（未填场地）")
        p["weeks"].add(e.week)

        t = teach[e.teacher or "未指派教师"]
        t["cells"] += c
        t["projects"].add(e.experiment or "未标注实验项目")
        t["classes"].add(e.class_name or "未标注班级")
        t["weeks"].add(e.week)
        t["wd"][e.weekday or "未标注"] += 1

        if (e.room or "").strip():
            r = room[e.room.strip()]
            r["cells"] += c
            r["projects"].add(e.experiment or "未标注实验项目")
            r["classes"].add(e.class_name or "未标注班级")
            r["weeks"].add(e.week)

        k = cls[e.class_name or "未标注班级"]
        k["cells"] += c
        k["projects"].add(e.experiment or "未标注实验项目")
        k["groups"].add(e.group or "")
        k["weeks"].add(e.week)
        k["teachers"].add(e.teacher or "未指派教师")
        k["wd"][e.weekday or "未标注"] += 1

    # 容量口径与 /stats 一致：分母由本表实际开放的星期×时段推导
    weeks_all = {e.week for e in _db.entries}
    n_weeks = (max(weeks_all) - min(weeks_all) + 1) if weeks_all else 0
    n_days = len({e.weekday for e in _db.entries if e.weekday}) or 5
    n_slots = len({s for e in _db.entries for s in (getattr(e, "blocked", None) or [e.time_slot]) if s}) or 3
    capacity = n_weeks * n_days * n_slots

    wb = openpyxl.Workbook()
    wb.remove(wb.active)

    ws = wb.create_sheet("实验项目")
    _write_sheet(ws, ["实验项目", "班级数", "学生组数", "课次(格)", "授课教师", "使用场地", "覆盖周次"],
                 [34, 8, 10, 10, 26, 24, 18])
    for name in sorted(proj, key=str):
        v = proj[name]
        ws.append([name, len(v["classes"]), len([g for g in v["groups"] if g]),
                   v["cells"], "、".join(sorted(v["teachers"])), "、".join(sorted(v["rooms"])),
                   _week_span(v["weeks"])])

    ws = wb.create_sheet("教师")
    _write_sheet(ws, ["教师", "课次(格)", "实验项目数", "班级数", "活跃周数", "上课星期分布"],
                 [12, 10, 12, 8, 10, 30])
    for name in sorted(teach, key=str):
        v = teach[name]
        ws.append([name, v["cells"], len(v["projects"]), len(v["classes"]),
                   len(v["weeks"]), _weekday_mix(v["wd"])])

    ws = wb.create_sheet("场地")
    _write_sheet(ws, ["场地", "占用格数", "利用率%", "班级数", "实验项目数", "覆盖周次"],
                 [12, 10, 10, 8, 12, 18])
    for name in sorted(room, key=str):
        v = room[name]
        ws.append([name, v["cells"], round(v["cells"] / capacity * 100, 1) if capacity else 0,
                   len(v["classes"]), len(v["projects"]), _week_span(v["weeks"])])

    ws = wb.create_sheet("班级")
    _write_sheet(ws, ["班级", "课次(格)", "实验项目数", "学生组数", "覆盖周次", "上课星期分布", "授课教师"],
                 [30, 10, 12, 10, 18, 30, 26])
    for name in sorted(cls, key=str):
        v = cls[name]
        ws.append([name, v["cells"], len(v["projects"]), len([g for g in v["groups"] if g]),
                   _week_span(v["weeks"]), _weekday_mix(v["wd"]), "、".join(sorted(v["teachers"]))])

    return _xlsx_response(wb, "实验排课基础数据台账.xlsx")


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
            # 按 blocked 落图：一条 `14:00-17:00` 的记录同时占 567 和 678，
            # 只按主标签落图会漏掉 678 那一半（R-40）。
            for slot in getattr(e, "blocked", None) or [e.time_slot]:
                room_map[(e.week, e.weekday, slot, e.room)].append(e)
        else:
            # ⚠️ 曾经无条件放进 room_map：于是一堆"空场地"被归到同一个 key
            # (week, weekday, slot, '')，凡两条以上就报"教室冲突（同室同段多班）"，
            # 而 place 是空串 —— 界面上显示成"第1周 周五 18:30-20:45 ·"，场地空着。
            # 没填场地不可能撞场地，这是纯假阳性；真正该说的是"这些条没场地"。
            missing_room.append(e)
        if e.teacher:
            for slot in getattr(e, "blocked", None) or [e.time_slot]:
                teacher_map[(e.week, e.weekday, slot, e.teacher)].append(e)

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

    # 时段看不懂、已被剔除的行：它们没进冲突图（不知道具体节次），
    # 但只要它们占着教室，那个教室当天其实不空 —— 沙盘按"整日占用"处理了，
    # 体检这里必须把原文摊出来，否则教秘会以为冲突扫描覆盖了全表。
    unparsed = list(getattr(_db, "skipped", None) or [])
    unparsed_samples = [
        f"第{s['week']}周 {s['weekday']} {s.get('class_name') or ''} @ {s.get('room') or '未填场地'}"
        f"（时段原文「{s.get('raw_ts')}」）"
        for s in unparsed[:5]
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
            "unparsed_slot_count": len(unparsed),
            "unparsed_slot_samples": unparsed_samples,
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

    # ⚠️ 分母曾经写死 `n_weeks * 5 * 3`（5 天 × 3 时段），而真实表**周末照常排课**
    # （433 条里周六 36、周日 35，占 16%；"24电信 42/46 节在周末"是常态）且存在上午时段
    # （MORNING 4 条）。把这两类排除在分母外 ⇒ 利用率系统性虚高：
    # 实测 17.6% vs 真实 9.4%（分母 240 vs 448/间），教秘会据此误判"场地还很空"。
    # 与 R-41 同族：拿"枚举里有的"（5 天 3 时段）当"现实里有的"（7 天 4 时段）。
    # 现在按**本表实际开放**的星期/时段推导，并把口径一并返回，界面上写清楚。
    schedulable_days = len({e.weekday for e in _db.entries if e.weekday}) or 5
    schedulable_slots = len(
        {s for e in _db.entries for s in (getattr(e, "blocked", None) or [e.time_slot]) if s}
    ) or 3
    total_slots = n_weeks * schedulable_days * schedulable_slots
    capacity_basis = f"{n_weeks} 周 × {schedulable_days} 天 × {schedulable_slots} 时段 = {total_slots} 格/间"

    room_sessions = Counter()
    room_weeks = defaultdict(set)
    teacher_sessions = Counter()
    teacher_experiments = defaultdict(set)
    teacher_weeks = defaultdict(set)

    for e in _db.entries:
        if e.room:
            # 按真实占用格数计：一条 `14:00-17:00` 同时占 567+678，算 2 格而不是 1 节。
            room_sessions[e.room] += len(getattr(e, "blocked", None) or [e.time_slot])
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
            # 口径必须和利用率一起给出：只说"9.4%"而不说分母怎么来的，
            # 教秘无法判断这个数字对自己有没有意义（R-38 同族：算出来的数要说清没算什么）
            "capacity_basis": capacity_basis,
            "schedulable_days": schedulable_days,
            "schedulable_slots": schedulable_slots,
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
    """用约束求解器基于现有数据自动生成排课初稿（带周次，避开现有安排表）"""
    _ensure_loaded()

    from app.engine.auto_scheduler import AutoScheduler
    scheduler = AutoScheduler(
        _db, student=_student,
        extra_tasks=_schedulable_extra_tasks(),
    )
    result = await run_in_threadpool(scheduler.solve, max_sections=30)

    # 定位说明：自动排课是"辅助初稿生成"，非替代学校已排定的完整安排表。
    # 原文案写"本次简化演示 12 个教学任务"，而实际 `max_sections=30` —— 文案与事实不符，
    # 同一份方案下方就列着 30 条。这里改成用真实数字拼，避免再漂。
    real_entries = len(_db.entries)
    note = (
        f"📌 定位说明：自动排课为辅助初稿生成。全表共 {result.get('total_sections_all', 0)} 个实验教学任务"
        f"（另有 {result.get('intro_excluded', 0)} 个绪论课任务教室已另行安排，不参与自动排课），\n"
        f"    本次取前 {result.get('total_sections', 0)} 个求解。方案已逐周避开现有安排表"
        f"（共 {real_entries} 条记录）占用的教室与教师。\n"
        f"    生成结果仅供教秘做初稿参考，落地前请按「我的课程 / 数据体检」复核。"
    )
    result["summary_text"] = note + "\n\n" + scheduler.summary(result)

    return {
        "success": result.get("success", False),
        "message": result.get("summary_text", result.get("error", "排课失败")),
        "data": {
            "total_sections": result.get("total_sections", 0),
            "total_sections_all": result.get("total_sections_all", 0),
            "intro_excluded": result.get("intro_excluded", 0),
            "solved": result.get("solved", 0),
            "weeks": result.get("weeks", []),
            "conflicts": result.get("conflicts", []),
            "schedule": result.get("schedule", []),
            "student_checked": result.get("student_checked", False),
            "capacity_checked": result.get("capacity_checked", False),
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
    try:
        # 幂等键：同一张云端工单可能被重复应用（网络重试、手滑点两次、审批台刷新）。
        # `_apply_reschedule` 的源周是"目标周之前最近的那个匹配周"，重复执行时目标周
        # 那条已被移走，它会去移**更早一周**的那条 —— 一张工单能连着撬动好几周。
        conn.execute("ALTER TABLE applied_moves ADD COLUMN idem_key TEXT DEFAULT ''")
    except Exception:
        pass  # 列已存在
    conn.commit()
    conn.close()


_init_db()


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
            return {
                "applied": 0, "skipped": 0,
                "details": [f"该申请已于 {hit['applied_at']} 应用过，本次未重复移动"],
            }

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
        if parsed["time_slot"]:
            # 按**真实占住的节次**比，不是主标签相等：`14:00-17:00` 主标签 567，
            # 但它同时占着 678 的前 1 小时 15 分，卡片上回显的就是 "14:00-17:00"。
            if parsed["time_slot"] not in set(getattr(e, "blocked", None) or [e.time_slot]):
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

    applied, skipped, details = 0, 0, []
    for e in src_entries:
        # 目标周该格是否已有人：按**真实占住的节次**比，而不是主标签相等。
        # 源记录是 `14:00-17:00`（主标签 567）时，目标周的 678 若已有课，
        # 主标签比较会判"不冲突"，于是把两条课叠在同一间教室里（R-40）。
        e_blocks = set(getattr(e, "blocked", None) or [e.time_slot])
        conflict = any(
            o is not e and o.week == target_week and o.weekday == e.weekday
            and not e_blocks.isdisjoint(set(getattr(o, "blocked", None) or [o.time_slot]))
            and (o.room == e.room or (o.teacher and o.teacher == e.teacher))
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
            "INSERT INTO applied_moves (request_data, applied_at, idem_key) VALUES (?, ?, ?)",
            (json.dumps(record, ensure_ascii=False), datetime.now().isoformat(timespec="seconds"),
             idem_key or ""),
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
        _ensure_loaded()

    record = {
        "teacher": req.teacher,
        "course_name": req.course_name,
        "class_name": req.class_name,
        "original_time": req.original_time,
        "target_week": req.target_week,
        "source_week": req.source_week,
    }
    result = await run_in_threadpool(_apply_reschedule, record, True, req.key or "")

    applied = result.get("applied", 0)
    skipped = result.get("skipped", 0)
    if applied:
        message = f"✅ 已移动 {applied} 条排课记录到第{req.target_week}周"
        if skipped:
            message += f"，{skipped} 条因冲突跳过"
    elif skipped:
        message = (f"⚠️ 调课已批准，但目标周该时段已有安排，{skipped} 条记录未自动移动"
                   f"（请人工协调）")
    else:
        message = "⚠️ 调课已批准，但未找到可自动移动的排课记录（请在教务系统中人工修改）"

    details = result.get("details") or []
    if details:
        message += "\n" + "\n".join(details[:6])

    return {"success": True, "data": {"applied": applied, "skipped": skipped,
                                      "details": details}, "message": message}
