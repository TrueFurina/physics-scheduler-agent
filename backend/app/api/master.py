"""主数据维护端点族（R77 从 `app/api/reschedule.py` 整块搬出）。

搬了什么
--------
五张实体表（课程 / 实验项目 / 教师 / 场地 / 班级）的维护入口：

* 只读总览与一键反推：`/master/overview`、`/master/init`
* 十个增删（每表一个 upsert + 一个 delete）
* 台账导入三步：`/master/import-preview`（只读预览）、`/master/tasks-preview`
  （只读预览）、`/master/import`（真正写入）
* 五个请求体模型 `_CourseIn` / `_ExperimentIn` / `_TeacherIn` / `_RoomIn` / `_ClassIn`

**没搬** `/export/master-workbook` 与它的两个同步建表函数（`_master_workbook_bytes`
/ `_master_workbook_build`）。它们属于导出族（缓存 `_cached_export`、`_master_digest`、
`_xlsx_bytes_response`、`export_gate`、限流器），与"维护主数据"是两件事；导出族整体
下沉线程池是 R-70/R-71 的成果，留给下一刀整块搬。

搬迁原则：只搬家，不改行为
--------------------------
1. 路由路径、方法、summary、dependencies 一字未动；`main.py` 用同一个 prefix + tags
   挂载，`/openapi.json` 里这 17 条与搬迁前一致。
2. 函数体、文档字符串、注释原文照搬（除第 3 条的 `_api.` 前缀）。
3. 状态读写一律写成 `_api.xxx` 模块属性访问，见下一节。
4. `reschedule.py` 末尾保留旧名惰性转发（模块级 `__getattr__`），`reschedule.master_init`
   这类既有引用照旧可用，测试零改动。

为什么状态必须走模块属性（搬迁时最容易写错的一条）
--------------------------------------------------
`_master_data` / `_master_tasks` / `_db` 都是 `reschedule` 的**可变模块状态**；
`_ensure_master_data()` / `_persist_master_state_async()` / `_ensure_loaded_async()` /
`_read_upload_limited()` / `_upload_target()` / `_EXCEL_SUFFIXES` / `_ADMIN_ONLY` 也是
它的模块属性。测试靠 `monkeypatch.setattr(reschedule, "_master_data", MasterData())`
把整条链路指向临时状态（`tests/test_export_nonblocking.py` 的 `small_db`、
`tests/test_master_delete.py`、`tests/test_master_consistency.py` 都这么干）。

若这里写 `from app.api.reschedule import _master_data`，拿到的是**导入那一刻**的对象：
之后 `monkeypatch.setattr(reschedule, "_master_data", ...)` 全部失明 ——
表现为"测试是绿的，跑的是真主数据"，或者"patch 了但导入仍写进旧对象"。
所以一律 `_api._master_data`，每次调用现取。这条有 AST 守卫
（`tests/test_master_module_split.py`）。

⚠️ `_master_write` 用的是 `reschedule._master_lock`（RLock）。写入口必须与导出族
（R-70 起跑在工作线程上，读主数据时在锁内取快照）**共用同一把锁对象** ——
这里是在装饰期 `@_api._master_write` 取到那个函数，函数体里 `with _master_lock:`
引用的是 reschedule 的全局锁，故仍是同一把。复制一份锁 = 两把锁 = 锁等于没锁。

依赖方向（单向，导入期不成环）
------------------------------
本模块 → `reschedule`（取状态、`_master_write`、`_ADMIN_ONLY`）。
反向只有 `reschedule.__getattr__` 里的**惰性**转发，导入期不触发。
`app/main.py` 里按 reschedule → landing → master 顺序导入。
"""

import io
from typing import List, Optional

from fastapi import APIRouter, File, HTTPException, UploadFile
from pydantic import BaseModel

from app.api import reschedule as _api

router = APIRouter()

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
    weekday: Optional[str] = None
    weekdays: Optional[List[str]] = None
    time_slots: Optional[List[str]] = None
    week_min: Optional[int] = None
    week_max: Optional[int] = None


@router.get("/master/overview", summary="主数据五张实体表总览")
async def master_overview():
    """五张实体表（课程/实验项目/教师/场地/班级）的完整状态。免鉴权：纯实体清单。"""
    await _api._ensure_loaded_async()
    _api._ensure_master_data()
    return {"success": True, "data": _api._master_data.overview()}


@router.post("/master/init", summary="从安排表反推主数据（一键初始化）", dependencies=_api._ADMIN_ONLY)
@_api._master_write
async def master_init():
    """幂等：清空主数据后从当前安排表重新反推。"""
    await _api._ensure_loaded_async()
    _api._master_data.build_from_entries(_api._db.entries)
    await _api._persist_master_state_async()
    return {"success": True, "data": _api._master_data.overview()}


@router.post("/master/courses", summary="新建课程", dependencies=_api._ADMIN_ONLY)
@_api._master_write
async def master_upsert_course(req: _CourseIn):
    await _api._ensure_loaded_async()
    _api._ensure_master_data()
    try:
        _api._master_data.upsert_course(req.name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await _api._persist_master_state_async()
    return {"success": True, "data": _api._master_data.overview()}


@router.delete("/master/courses/{name}", summary="删除课程", dependencies=_api._ADMIN_ONLY)
@_api._master_write
async def master_delete_course(name: str):
    # R39：删默认课程会抛 ValueError（见 master_data.delete_course）。
    # 不接住就是 500 —— 那会显示"服务端出错"，教秘以为是系统坏了，
    # 而这其实是一条可以照着做的操作提示。
    await _api._ensure_loaded_async()
    _api._ensure_master_data()
    try:
        _api._master_data.delete_course(name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await _api._persist_master_state_async()
    return {"success": True, "data": _api._master_data.overview()}


@router.post("/master/experiments", summary="新建/更新实验项目", dependencies=_api._ADMIN_ONLY)
@_api._master_write
async def master_upsert_experiment(req: _ExperimentIn):
    await _api._ensure_loaded_async()
    _api._ensure_master_data()
    try:
        _api._master_data.upsert_experiment(
            req.name, course=req.course or "", teachers=req.teachers,
            rooms=req.rooms, classes=req.classes)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await _api._persist_master_state_async()
    return {"success": True, "data": _api._master_data.overview()}


@router.delete("/master/experiments/{name}", summary="删除实验项目", dependencies=_api._ADMIN_ONLY)
@_api._master_write
async def master_delete_experiment(name: str):
    _api._ensure_master_data()
    _api._master_data.delete_experiment(name)
    await _api._persist_master_state_async()
    return {"success": True, "data": _api._master_data.overview()}


@router.post("/master/teachers", summary="新建/更新教师", dependencies=_api._ADMIN_ONLY)
@_api._master_write
async def master_upsert_teacher(req: _TeacherIn):
    await _api._ensure_loaded_async()
    _api._ensure_master_data()
    try:
        _api._master_data.upsert_teacher(req.name, projects=req.projects)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await _api._persist_master_state_async()
    return {"success": True, "data": _api._master_data.overview()}


@router.delete("/master/teachers/{name}", summary="删除教师", dependencies=_api._ADMIN_ONLY)
@_api._master_write
async def master_delete_teacher(name: str):
    _api._ensure_master_data()
    _api._master_data.delete_teacher(name)
    await _api._persist_master_state_async()
    return {"success": True, "data": _api._master_data.overview()}


@router.post("/master/rooms", summary="新建/更新场地", dependencies=_api._ADMIN_ONLY)
@_api._master_write
async def master_upsert_room(req: _RoomIn):
    await _api._ensure_loaded_async()
    _api._ensure_master_data()
    try:
        _api._master_data.upsert_room(req.name, capacity=req.capacity)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await _api._persist_master_state_async()
    return {"success": True, "data": _api._master_data.overview()}


@router.delete("/master/rooms/{name}", summary="删除场地", dependencies=_api._ADMIN_ONLY)
@_api._master_write
async def master_delete_room(name: str):
    _api._ensure_master_data()
    _api._master_data.delete_room(name)
    await _api._persist_master_state_async()
    return {"success": True, "data": _api._master_data.overview()}


@router.post("/master/classes", summary="新建/更新班级", dependencies=_api._ADMIN_ONLY)
@_api._master_write
async def master_upsert_class(req: _ClassIn):
    await _api._ensure_loaded_async()
    _api._ensure_master_data()
    try:
        _api._master_data.upsert_class(req.name, groups=req.groups, weekday=req.weekday or "",
                                  time_slots=req.time_slots, week_min=req.week_min,
                                  week_max=req.week_max, weekdays=req.weekdays)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await _api._persist_master_state_async()
    return {"success": True, "data": _api._master_data.overview()}


@router.delete("/master/classes/{name}", summary="删除班级", dependencies=_api._ADMIN_ONLY)
@_api._master_write
async def master_delete_class(name: str):
    _api._ensure_master_data()
    _api._master_data.delete_class(name)
    await _api._persist_master_state_async()
    return {"success": True, "data": _api._master_data.overview()}


@router.post("/master/import-preview", summary="预览主数据台账导入（只读，不写入）",
             dependencies=_api._ADMIN_ONLY)
async def master_import_preview(file: UploadFile = File(...)):
    """导入向导第 2 步：把文件解析成「页 → 列 → 字段」映射给教秘看，**不写任何状态**。

    与 `/master/import` 共用 `parse_workbook`（同一份选页/匹配/统计列/行转换
    代码）：预览报的"将导入 N 行、没认出哪些列"就是点确认后真正发生的导入，
    不会出现"预览说 12 行、导入进 9 行"的错版（R-34 同族：校验器与被验对象
    必须同源）。

    刻意不落盘：预览是纯读路径，文件走内存 BytesIO 解析，
    `_DATA_DIR` 里不该留下一个谁都不会再看的临时文件。
    """
    content = await _api._read_upload_limited(file)
    try:
        from app.master_workbook import preview_workbook

        report = preview_workbook(io.BytesIO(content))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"台账解析失败：{e!s}")
    return {"success": True, "kind": "workbook", "data": report}


@router.post("/master/tasks-preview", summary="预览教学任务清单导入（只读，不写入）",
             dependencies=_api._ADMIN_ONLY)
async def master_tasks_preview(file: UploadFile = File(...)):
    """导入向导第 2 步（清单版）：只解析不写入。

    `_master_tasks` / `db.pending` 都不碰 —— 确认走 `/upload-master`，
    它自己会再完整解析一次（同一份代码，结果逐字一致）。
    认不出「班级/实验项目」两列时这里就 400 说清缺什么，不用等导入那一刻
    才发现整份文件白传。
    """
    content = await _api._read_upload_limited(file)
    try:
        from app.utils.excel_parser import preview_master_tasks

        report = preview_master_tasks(io.BytesIO(content))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"清单解析失败：{e!s}")
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"清单解析失败：{e!s}")
    # R49：确认（`/upload-master`）是**整批替换**语义（先 clear 再 extend），
    # 而预览只说"将读入 N 条"—— 教秘读到的是"再加 N 条"。
    # 行为不改（替换本来就是这套清单的设计，见 `clear_master_tasks` 的说明），
    # 补的是**说明**：把"现有几条、会被整体换掉"一并给出，让预览与提交对得上。
    report["existing_tasks"] = len(_api._master_tasks)
    return {"success": True, "kind": "tasks", "data": report}


@router.post("/master/import", summary="导入主数据台账（Excel 批量维护五张表）",
             dependencies=_api._ADMIN_ONLY)
@_api._master_write
async def master_import(file: UploadFile = File(...)):
    """把 `/export/master-workbook` 导出的台账改完传回来，批量写入主数据。

    这是 P2 的最后一块闭环：教秘在 Excel 里一次性补几十个项目/教师/班级，
    比在界面上一个个点快得多。语义上**只做新增与补充，不做删除** ——
    在表里删掉一行不会删掉实体（删请用上面的 delete 接口），理由写在
    `master_workbook` 的填写说明里：Excel 里的"空"可能是"没填"，
    也可能就是"没变"，导入端不猜这个意图。

    返回体一定带 `report`：认出哪些页、哪些列没认出、跳过了哪些行、
    哪些列被判为统计列而忽略 —— 静默导入一半比报错危险得多（R-34）。
    """
    save_path = _api._upload_target(file.filename, _api._EXCEL_SUFFIXES, prefix="master_")
    content = await _api._read_upload_limited(file)
    with open(save_path, "wb") as f:
        f.write(content)

    try:
        from app.master_workbook import apply_tables, parse_workbook

        tables, report = parse_workbook(save_path)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"台账解析失败：{e!s}")

    await _api._ensure_loaded_async()
    _api._ensure_master_data()
    # R54：先记下"导入前主数据有哪些名字"，用于判断"预览之后变了什么"。
    #
    # 导入是两阶段（预览 → 确认），中间可能隔几分钟。这期间别人可能已建出
    # 同名实体 —— 于是预览说"新增1个"、提交说"新增0个"。**两个数字都对**，
    # 但它们描述的不是同一件事；教秘看到 added=0 会以为导入失败。
    names_before = {
        k: set(getattr(_api._master_data, k)) for k in
        ("courses", "experiments", "teachers", "rooms", "classes")
    }
    try:
        stats = apply_tables(_api._master_data, tables)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"台账写入失败：{e!s}")
    await _api._persist_master_state_async()

    total = sum(s.get("rows", 0) for s in stats.values())
    added = sum(s.get("added", 0) for s in stats.values())
    if total == 0:
        msg = ("⚠️ 台账里没读到任何可导入的行 —— "
               + ("；".join(report["warnings"]) or "请确认文件里有「实验项目/教师/场地/班级/课程」页且有表头"))
    else:
        msg = f"✅ 已导入 {total} 行（新建实体 {added} 个）；导入只增补，不删除已有实体"

    # R42：新增实体必须**点名**。
    #
    # 教秘把「教师C」改成「教师C改」再导入，得到的正是"新建实体 1 个" ——
    # 而他想的是"改好了"。不点名的话，他无处判断这 1 个是不是自己那行。
    # 等下次排课发现"教师C改"没有课、"教师C"还有 116 条课，已经过去很久。
    added_names = {k: v.get("added_names") or [] for k, v in stats.items()}
    flat = [(k, n) for k, names in added_names.items() for n in names]
    if flat:
        label = {"courses": "课程", "experiments": "实验项目",
                 "teachers": "教师", "rooms": "场地", "classes": "班级"}
        shown = "、".join(f"{label.get(k, k)}「{n}」" for k, n in flat[:8])
        msg += f"\n🆕 本次新建：{shown}"
        if len(flat) > 8:
            msg += f" 等 {len(flat)} 个"
        msg += "\n（导入按**名称**识别实体：改名会被当成「新增一个」，原名称仍然保留。要改名请在界面上操作）"

    # R54：文件里有、但**导入前主数据里已经存在**的实体 —— 它们不会被算作"新增"，
    # 因为 `apply_tables` 只补不改。若其中有"预览时还不存在"的，说明这期间
    # 数据被别人改过：必须说出来，否则教秘会把"新增 0 个"读成"导入没生效"。
    label_all = {"courses": "课程", "experiments": "实验项目",
                 "teachers": "教师", "rooms": "场地", "classes": "班级"}
    preexisting = {}
    for key, rows in tables.items():
        if key not in names_before:
            continue
        want = {str(r.get("name") or "").strip() for r in (rows or [])}
        want.discard("")
        hit = sorted(want & names_before[key])
        if hit:
            preexisting[key] = hit
    # R54：什么时候该提示"这些实体导入前就已存在"？
    #
    # 判据不是"有没有已存在项"（那太常见：重复导入同一份文件会命中 66 个），
    # 而是**教秘会不会因此误解**：
    #
    #   added == 0 且文件里确实有内容 → 很可能是"预览说会新增、实际没有"，
    #                                   或"重复导入"。必须说明，否则读成"导入失败"
    #   added > 0 且也有已存在项      → 预览可能只看到一部分，也要说明
    #   added > 0 且全是新增          → 无歧义，不提示
    #
    # 实测反例（不加这个分支时）：预览说新增 1 个，中间被别人建出来，
    # 提交得到 added=0、message 只有"新建实体 0 个" —— 教秘以为导入没生效。
    flat_pre = [(k, n) for k, names in preexisting.items() for n in names]
    if flat_pre and (added == 0 or flat):
        shown_pre = "、".join(f"{label_all.get(k, k)}「{n}」" for k, n in flat_pre[:6])
        if len(flat_pre) > 6:
            shown_pre += f" 等 {len(flat_pre)} 个"
        msg += (f"\nℹ️ 其中 {len(flat_pre)} 个在导入前就已存在（只补充、不重复创建）：{shown_pre}")
        msg += ("\n  ⚠️ 若与预览时看到的不一致，说明这期间台账已被别处修改 ——"
                " 以上面的**实际结果**为准。")

    report["added_names"] = added_names

    return {"success": total > 0, "message": msg,
            "data": _api._master_data.overview(), "stats": stats, "report": report}
