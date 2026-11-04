"""导出台账 / 课表的全部端点（R78 从 `app/api/reschedule.py` 搬出）。

为什么单独一个模块
------------------
`reschedule.py` 一度 3493 行、六族职责混在一起（主数据 CRUD、落地方案、导出、
工单、聊天、统计）。R76 搬走落地方案一族、R77 搬走主数据维护一族，这一轮搬第三族：
**导出**。搬完 `reschedule.py` 只剩 2500 行上下，剩下的都是"排课本体"。

这一族有一个共同的、只看单条路由看不出来的性质：**它们是同一份数据的不同投影**，
共享一套字节缓存与一套预热。所以它们必须待在同一个模块里 —— 分散在三处时，
"缓存键要不要带主数据指纹"这种判断会各写一遍（历史上有过：`/export/room-schedule`
的键曾漏掉主数据指纹，改动课程归属后场地课表不重建）。

搬了什么
--------
| 类 | 名字 |
|---|---|
| 5 个导出端点 | `export_class_schedule` / `export_room_schedule` / `export_master_data` / `export_master_workbook` / `auto_schedule_export` / `parallel_export` |
| 请求体 | `ParallelExportRequest` / `AutoScheduleExportRequest` |
| 字节缓存 | `_EXPORT_CACHE` / `_EXPORT_CACHE_MAX` / `_EXPORT_FORMAT_VERSION` / `_entries_digest` / `_master_digest` / `_cached_export` |
| 预热 | `_warm_export_cache` / `start_export_warmup` / `_EXPORT_WARM_THREAD` |
| 成对拆分的同步主体 | `_*_bytes`（取缓存）/ `_*_build`（真建表，跑在线程池上） |

**没搬**：`/requests/export`（工单留档）。它是 SQLite 工单的投影，不读排课表、
不走这份缓存，跟着"工单"这一族走才对；把它塞进这里只会让"导出一族"变糊。
R78 时它还在 `reschedule.py`，**R80 随工单族搬到了 `app/api/requests_board.py`**
—— 归属判据见 `tests/test_export_module_split.py`：文件名变了，边界没变。

不变量（搬完必须仍然成立）
--------------------------
1. **状态一律 `_api.xxx` 现取**，不许值绑定 —— 否则 monkeypatch 静默失明。
   见 `backend/tests/test_export_module_split.py` 的 AST 守卫。
2. **5 个重活端点必须 `run_in_threadpool`**（R-70）。它们各自实测 65–830ms，
   跑在事件循环上就是整站停摆。
3. **缓存键必须覆盖全部输入**：安排表全字段 + 主数据 + 请求参数 + 格式版本。
   少一项就是脏缓存（R-66 同族）。
4. **`/export/*` 的门禁是 `export_gate`**（`PUBLIC_READONLY=0` 时要求管理员会话）。
   它留在 `reschedule`（`landing` 也在用），这里按模块属性取。
"""

import hashlib
import os
import threading
from collections import OrderedDict
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from app.api import reschedule as _api
from app.api.xlsx_helpers import (
    _entry_sort_key,
    _safe_sheet_name,
    _slot_text,
    _workbook_bytes,
    _write_sheet,
    _xlsx_bytes_response,
    _xlsx_response,
)
from app.ratelimit import rate_limit

router = APIRouter()


@router.get("/export/master-workbook", summary="导出可编辑主数据台账（改完可导回）",
            dependencies=[Depends(_api.export_gate), Depends(rate_limit("standard-export"))])
async def export_master_workbook():
    """五张实体表（课程/实验项目/教师/场地/班级）的可编辑台账，改完可导回。

    与 `/export/master-data` 的区别：那份是**统计台账**（课次/覆盖率，给教务核对），
    这份是**实体台账**（行=实体，列=可编辑字段，给教秘批量维护）。
    两份各司其职 —— 统计表的「班级数」这类数值列如果拿去导入，会被判为统计列
    并显式忽略（见 `master_workbook._is_stat_column`），不会变成名叫「5」的班级。
    """
    data = await run_in_threadpool(_master_workbook_bytes)
    return _xlsx_bytes_response(data, "主数据台账（可编辑，可导回）.xlsx")


def _master_workbook_bytes() -> bytes:
    """（同步、纯 CPU）五张实体表台账 → 字节（带字节缓存，R-71）。"""
    _api._ensure_loaded()
    _api._ensure_master_data()
    key = ("master-workbook", _EXPORT_FORMAT_VERSION, _master_digest(_api._master_data))
    return _cached_export(key, _master_workbook_build)


def _master_workbook_build() -> bytes:
    """真正的建表（同步、纯 CPU）。实测 85ms，必须走线程池（R-70）。"""
    from app.master_workbook import build_workbook

    # 遍历五张表前先取快照：本函数在工作线程上，CRUD 可能同时改写主数据
    return _workbook_bytes(build_workbook(_api._master_snapshot()))

class ParallelExportRequest(BaseModel):
    """并行排课方案导出请求"""
    class_name: str
    schedule: list  # [{project, group, week, weekday, time_slot, room, teacher}]

@router.post("/parallel/export", summary="并行排课方案导出 Excel",
             dependencies=[Depends(rate_limit("parallel-export"))])
async def parallel_export(req: ParallelExportRequest):
    """把并行排课方案导出为 Excel（贴合排课员真实工作流）

    ⚠️ 这份导出**刻意留在请求线程上**：实测 24 行 12ms / 433 行 52ms，
    走线程池的切换与排队成本与之同量级，不值得（R-70 口径：只改实测 ≥50ms
    且每次点击都会走的路径，不做"看着不优雅"的改造）。
    """
    import openpyxl
    from openpyxl.styles import Alignment, Font

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

    return _xlsx_response(wb, f"并行排课方案_{req.class_name}.xlsx")

# ---------------------------------------------------------------------------
# 标准课表导出（班级课表 / 场地课表）
#
# 为什么要有这两个接口：教务在用系统（理论课排课工具）被认可的两项能力之一，
# 就是"排完能导出班级课程表、场地课程表"。本系统此前只能导出**新排出来的并行方案**，
# 导不出基于现有安排表的标准课表 —— 教秘拿不到可以直接发到班级群/贴在实验室门口的成品，
# 于是"排完了"和"能交付"之间还差一步人工整理。
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# 导出字节缓存（R-71）
#
# 为什么要缓存：导出实测 77–350ms（进程首次 830ms），而教秘在一次演示里会把
# 班级课表 / 场地课表 / 两份台账各点一遍，还会为了"再确认一下"重复点同一个。
# 重算一遍没有任何新信息 —— 这正是"一次算、多次用"。
#
# 为什么不怕缓存脏（本项目最怕这一类，R-66 就是缓存静默失效）：
# 键**不是**手工维护的版本号，而是从数据自身算出来的指纹 —— 安排表每一条的
# **全部字段**（`vars(e)`，不是手写字段列表，漏一个就是脏缓存）+ 主数据五张表 +
# 请求参数 + 导出格式版本。数据一变键就变，旧字节自然不再被命中。
# 代价：每次请求 2ms 指纹（实测 entries 1.6ms + master 0.3ms），
# 相比 77–350ms 的建表可以忽略。
# ---------------------------------------------------------------------------
_EXPORT_CACHE_MAX = 12          # 每份 10–50KB，封顶约 600KB
_EXPORT_CACHE: "OrderedDict[tuple, bytes]" = OrderedDict()
_EXPORT_FORMAT_VERSION = 1      # 导出**格式**变了才 +1（数据变化靠指纹，不用动它）


def _entries_digest(entries) -> bytes:
    """安排表全字段指纹。用 `vars(e)` 而不是手写字段名：漏一个字段就是脏缓存。"""
    h = hashlib.blake2b(digest_size=16)
    for e in entries:
        h.update(repr(vars(e)).encode("utf-8", "ignore"))
    return h.digest()


def _master_digest(master) -> bytes:
    """主数据五张表指纹（课程映射、教师/场地候选都在里面）。"""
    return hashlib.blake2b(repr(master.to_dict()).encode("utf-8", "ignore"),
                           digest_size=16).digest()


def _cached_export(key: tuple, build) -> bytes:
    """命中就返回缓存字节，未命中才建表并存入。线程安全：最坏情况是重复建一次。"""
    hit = _EXPORT_CACHE.get(key)
    if hit is not None:
        _EXPORT_CACHE.move_to_end(key)
        return hit
    data = build()
    _EXPORT_CACHE[key] = data
    while len(_EXPORT_CACHE) > _EXPORT_CACHE_MAX:
        _EXPORT_CACHE.popitem(last=False)
    return data

# ---------------------------------------------------------------------------
# 导出预热（R-71）
#
# 缓存解决的是"重复点击"，但演示里教秘点的恰恰是**第一次**（首调 417–830ms，
# 大头是 openpyxl 的冷启动）。所以在数据加载完之后，后台把四份导出各建一次，
# 让第一次点击也是热的。
#
# 取向与 solver 预热一致（engine/warmup.py）：daemon 线程、不阻塞启动、
# 不占请求路径、失败只退回"首调慢一点"，绝不因为预热失败而影响任何功能。
# ---------------------------------------------------------------------------
_EXPORT_WARM_THREAD: Optional[threading.Thread] = None


def _warm_export_cache() -> None:
    try:
        if not _api._db.loaded:
            return
        _api._ensure_master_data()
        _class_schedule_bytes()
        _room_schedule_bytes()
        _master_data_bytes()
        _master_workbook_bytes()
    except Exception as exc:  # 预热失败绝不能影响服务可用性
        print(f"⚠️ 导出预热失败（不影响功能，只是首调会慢一点）：{type(exc).__name__}: {exc}")


def start_export_warmup() -> bool:
    """后台预热导出缓存；已有预热在跑 / 被禁用则返回 False（幂等）。"""
    global _EXPORT_WARM_THREAD
    if os.getenv("SCHEDULE_WARMUP", "1").strip().lower() in {"0", "false", "no", "off"}:
        return False
    if _EXPORT_WARM_THREAD is not None and _EXPORT_WARM_THREAD.is_alive():
        return False
    _EXPORT_WARM_THREAD = threading.Thread(
        target=_warm_export_cache, name="export-warmup", daemon=True)
    _EXPORT_WARM_THREAD.start()
    return True

@router.get("/export/class-schedule", summary="导出班级课表（教务标准课表之一）",
            dependencies=[Depends(_api.export_gate), Depends(rate_limit("standard-export"))])
async def export_class_schedule(class_name: Optional[str] = None):
    """按班级导出课表：每个班一个 sheet，行=该班全部实验安排（按周次/星期/节次排序）。

    `class_name` 留空 = 导出全部班级（每班一页）。

    课程维度（R-57，导师点名的后续难点）
    -----------------------------------
    课程归属以 R-66 显式外键（源表课程列 / 「课程类别」分类码列翻译，产品决策 B
    2026-10-24）为权威，主数据 `experiment.course` 反推兜底。

    - 一个班只有一门课时：直接平铺，不多一个标题行（当前真实表每班单一类别，
      即使全校三门课也是这样）。
    - 一个班跨多门课程时：**按课程分块**，块首加「【课程名】」标题行 ——
      否则 17 页班级课表会变成一堆无法归因的行，教秘分不清哪行属于哪门课。
    - 该班若出现**跨课程撞车**（同一组在两门课被排进同一格），在表尾单独列出：
      两门课的任课教师互不知情，谁都不会觉得自己排错了，这是最难自发发现的一类。
    """
    data = await run_in_threadpool(_class_schedule_bytes, class_name)
    return _xlsx_bytes_response(data, f"班级课表_{class_name or '全部班级'}.xlsx")


def _class_schedule_bytes(class_name: Optional[str] = None) -> bytes:
    """（同步、纯 CPU）班级课表工作簿 → 字节（带字节缓存，R-71）。

    拆出来只为了一件事：把这段 244–830ms 的建表工作交给线程池，
    别占着事件循环（R-70；R-19 同一族，只是当时只修了 5 处、没覆盖后来新增的导出）。
    """
    _api._ensure_loaded()
    _api._ensure_master_data()
    key = ("class", class_name, _EXPORT_FORMAT_VERSION,
           _entries_digest(_api._db.entries), _master_digest(_api._master_data))
    return _cached_export(key, lambda: _class_schedule_build(class_name))


def _class_schedule_build(class_name: Optional[str] = None) -> bytes:
    """真正的建表（同步、纯 CPU）。"""
    import openpyxl
    from app.course_bridge import group_by_course, scan_cross_course

    # 课程映射在锁内取快照：本函数跑在工作线程上，教秘的 CRUD 可能同时改写主数据
    courses = _api._master_courses_snapshot()

    entries = [e for e in _api._db.entries if (not class_name or e.class_name == class_name)]
    if class_name and not entries:
        raise HTTPException(404, f"安排表里没有班级「{class_name}」的记录")

    grouped: dict = {}
    for e in sorted(entries, key=_entry_sort_key):
        grouped.setdefault(e.class_name or "未标注班级", []).append(e)

    # 跨课程撞车按班归堆（只扫导出范围：单班导出时不必惊动全表）
    cross_by_class: dict = {}
    for c in scan_cross_course(entries, _api._master_data, courses)["conflicts"]:
        cross_by_class.setdefault((c["place"].split("/")[0]), []).append(c)

    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    used: set = set()
    for cls in sorted(grouped, key=str):
        ws = wb.create_sheet(_safe_sheet_name(cls, used))
        _write_sheet(ws, ["周次", "星期", "节次", "上课时间", "课程", "实验项目",
                          "学生组", "教师", "场地"],
                     [8, 7, 8, 14, 16, 30, 12, 10, 12])
        blocks = group_by_course(grouped[cls], _api._master_data, courses)
        multi_course = len(blocks) > 1
        for course, items in blocks:
            if multi_course:
                ws.append([f"【{course}】"])
                ws.cell(row=ws.max_row, column=1).font = openpyxl.styles.Font(bold=True)
            for e in items:
                ws.append([f"第{e.week}周", e.weekday, e.time_slot, _slot_text(e),
                           course, e.experiment, e.group, e.teacher, e.room or "（未填场地）"])
        # 该班的跨课程撞车：放在表尾，空一行隔开，别混进行列表里
        for c in cross_by_class.get(cls, []):
            ws.append([])
            ws.append([f"⚠️ 跨课程撞车：第{c['week']}周 {c['weekday']} {c['time_slot']}"
                       f" {c['place']}"])
            ws.cell(row=ws.max_row, column=1).font = openpyxl.styles.Font(bold=True, color="C00000")
            for d in c["detail"]:
                ws.append(["", d])
    if not grouped:
        ws = wb.create_sheet("无数据")
        ws.append(["当前安排表里没有可导出的记录"])
    return _workbook_bytes(wb)

@router.get("/export/room-schedule", summary="导出场地课表（教务标准课表之二）",
            dependencies=[Depends(_api.export_gate), Depends(rate_limit("standard-export"))])
async def export_room_schedule(room: Optional[str] = None):
    """按场地导出课表：每间实验室一个 sheet，行=该场地的全部占用。

    `room` 留空 = 导出全部场地。⚠️ 没填场地的记录（绪论课等）**不能静默丢弃** ——
    它们单独进「（未填场地）」页，否则教秘会以为全表都被扫到了（R-35 口径）。
    """
    data = await run_in_threadpool(_room_schedule_bytes, room)
    return _xlsx_bytes_response(data, f"场地课表_{room or '全部场地'}.xlsx")


def _room_schedule_bytes(room: Optional[str] = None) -> bytes:
    """（同步、纯 CPU）场地课表工作簿 → 字节（带字节缓存，R-71）。"""
    _api._ensure_loaded()
    key = ("room", room, _EXPORT_FORMAT_VERSION, _entries_digest(_api._db.entries))
    return _cached_export(key, lambda: _room_schedule_build(room))


def _room_schedule_build(room: Optional[str] = None) -> bytes:
    """真正的建表（同步、纯 CPU）。实测 265–342ms，必须走线程池（R-70）。"""
    import openpyxl

    entries = [e for e in _api._db.entries if (not room or (e.room or "").strip() == room)]
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
    return _workbook_bytes(wb)

@router.get("/export/master-data", summary="导出基础数据台账（数据库重构的起始材料）",
            dependencies=[Depends(_api.export_gate), Depends(rate_limit("standard-export"))])
async def export_master_data():
    """把现有安排表**反推**成四张实体台账：实验项目 / 教师 / 场地 / 班级。

    为什么要有它：教务侧的第一号行动项是"基于教学大纲梳理所有实验课程属性、
    重构数据库"。而本系统手里只有扁平的安排表行，没有独立的课程/项目/场地实体 ——
    教秘想补全实验项目、核对哪些项目只在个别班开、哪些场地利用率异常，无处下手。
    这份台账就是可以直接打开核对、补列、再导回的起始材料。

    ⚠️ 口径：课次一律按**真实占用格数**计（`14:00-17:00` 算 2 格），
    与利用率面板保持一致；缺字段的记录归入"未标注/未指派"而不是丢弃。
    """
    data = await run_in_threadpool(_master_data_bytes)
    return _xlsx_bytes_response(data, "实验排课基础数据台账.xlsx")


def _master_data_bytes() -> bytes:
    """（同步、纯 CPU）四张统计台账 → 字节（带字节缓存，R-71）。"""
    _api._ensure_loaded()
    key = ("master-data", _EXPORT_FORMAT_VERSION, _entries_digest(_api._db.entries))
    return _cached_export(key, _master_data_build)


def _master_data_build() -> bytes:
    """真正的建表（同步、纯 CPU）。实测 77ms，必须走线程池（R-70）。"""
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

    for e in _api._db.entries:
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
    weeks_all = {e.week for e in _api._db.entries}
    n_weeks = (max(weeks_all) - min(weeks_all) + 1) if weeks_all else 0
    n_days = len({e.weekday for e in _api._db.entries if e.weekday}) or 5
    n_slots = len({s for e in _api._db.entries for s in (getattr(e, "blocked", None) or [e.time_slot]) if s}) or 3
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

    return _workbook_bytes(wb)

class AutoScheduleExportRequest(BaseModel):
    """自动排课初稿导出：把 /auto-schedule 返回的 schedule 原样发回来。"""
    schedule: list  # [{week, weekday, time_slot, room, teacher, course, class_name}]


@router.post("/auto-schedule/export", summary="自动排课初稿导出 Excel（总表 + 每班一页）",
             dependencies=[Depends(_api.export_gate), Depends(rate_limit("standard-export"))])
async def auto_schedule_export(req: AutoScheduleExportRequest):
    """把一键自动排课的初稿导出成可发班级群的 Excel。

    为什么需要：`/auto-schedule` 只返回文本摘要 + JSON 明细，教秘拿到 171 行
    文本无法落地 —— 要么手抄，要么重新在 Excel 里排一遍。这份导出就是"排完了"
    到"能交付"之间缺的那一步：总表给排课人员核对，每班一页直接发给班级。
    """
    rows = list(req.schedule or [])
    if not rows:
        raise HTTPException(
            status_code=400,
            detail="没有可导出的排课方案（schedule 为空）。请先选择或生成一份方案，再执行导出。",
        )
    # 文件名里的两个数字在请求线程上算：只是去重计数，不值得进线程池再传回来
    n_class = len({(r.get("class_name") or "（未填班级）") for r in rows})
    data = await run_in_threadpool(_auto_schedule_bytes, rows)
    return _xlsx_bytes_response(data, f"自动排课初稿_{len(rows)}条_{n_class}班.xlsx")


def _auto_schedule_bytes(rows: list) -> bytes:
    """（同步、纯 CPU）自动排课初稿（总表 + 每班一页）→ 字节（带字节缓存，R-71）。

    键就是**请求载荷本身**的指纹：方案由前端带上来，服务端不存会话态。
    """
    payload = hashlib.blake2b(repr(rows).encode("utf-8", "ignore"), digest_size=16).digest()
    key = ("auto-schedule", _EXPORT_FORMAT_VERSION, payload)
    return _cached_export(key, lambda: _auto_schedule_build(rows))


def _auto_schedule_build(rows: list) -> bytes:
    """真正的建表（同步、纯 CPU）。

    实测 433 行 / 18 页 ≈ 184ms —— 171 个任务的真实规模只会更大，必须走线程池（R-70）。
    """
    import openpyxl
    from openpyxl.styles import Alignment, Font

    order = {"MORNING": 0, "567": 1, "678": 2, "91011": 3, "78": 4}
    rows = [dict(r, course=(r.get("course") or r.get("project") or "")) for r in rows]
    rows.sort(key=lambda s: (s.get("week") or 0, s.get("weekday") or "",
                             order.get(s.get("time_slot") or "", 9),
                             s.get("class_name") or "", s.get("teacher") or ""))

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "总表"
    headers = ["周次", "星期", "时段", "场地", "教师", "实验项目", "班级"]
    ws.append(headers)
    for c in ws[1]:
        c.font = Font(bold=True)
        c.alignment = Alignment(horizontal="center")
    for s in rows:
        ws.append([s.get("week"), s.get("weekday"), s.get("time_slot"), s.get("room"),
                   s.get("teacher"), s.get("course") or s.get("project"), s.get("class_name")])
    for col, w in zip("ABCDEFG", [8, 8, 14, 10, 20, 30, 26]):
        ws.column_dimensions[col].width = w
    ws.freeze_panes = "A2"

    used = {"总表"}
    by_class: dict = {}
    for s in rows:
        by_class.setdefault(s.get("class_name") or "（未填班级）", []).append(s)
    for cls, items in by_class.items():
        sheet = _safe_sheet_name(cls, used)
        used.add(sheet)
        sh = wb.create_sheet(sheet)
        sh.append(["周次", "星期", "时段", "场地", "教师", "实验项目"])
        for c in sh[1]:
            c.font = Font(bold=True)
            c.alignment = Alignment(horizontal="center")
        for s in items:
            sh.append([s.get("week"), s.get("weekday"), s.get("time_slot"), s.get("room"),
                       s.get("teacher"), s.get("course") or s.get("project")])
        for col, w in zip("ABCDEF", [8, 8, 14, 10, 20, 30]):
            sh.column_dimensions[col].width = w
        sh.freeze_panes = "A2"

    return _workbook_bytes(wb)
