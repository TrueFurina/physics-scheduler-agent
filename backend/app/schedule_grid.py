"""周课表网格：把安排表重排成「周次 × 星期 × 时段」的全景视图。

为什么需要这个模块
------------------
教秘此前的"看课表"路径只有导出 Excel：排完课 → 导出 → 打开文件 → 找那一格。
落地（R-61）之后更明显——方案写回安排表了，但**应用里没有一眼确认的面**，
看板只有统计数字。本模块把 `db.entries` 重排成网格，让"课表长什么样"
在浏览器里直接可见（README 路线图头号未做项）。

口径（每条都对应一个踩过的坑）
------------------------------
1. **7 天全列，不写死 5 天**：真实表周末照常排课（周六 36 + 周日 35 ＝ 71/433）。
   前端 `constants/labels.ts` 的 `WEEKDAYS` 只有 5 天、`GRID_TIME_SLOTS` 只有
   3 个时段——那是沙盘网格的旧口径（R-41：拿"枚举里有的"当"现实里有的"）。
   本模块的 `WEEKDAYS` / `SLOTS` 由 `time_model.ORDER` 推导，与解析器同源。
2. **一条记录占多节次就在每个节次各出现一次**：`blocked`（如 `["567","678"]`，
   原表写法 `14:00-17:00`）是真实占用（R-40）。若只在主标签格显示，另一格
   看起来是空的——沙盘会把教秘推荐到一间正在上课的教室。单元格带
   `blocked` / `time_raw`，前端可渲染"跨时段延续"。
3. **没填场地的记录照常入格**，打 `missing_room` 标记（真实表 16 条绪论课）。
   静默丢掉它们等于替教秘宣布"这些课不存在"（R-35 同族）。
4. **冲突标记不自己算**：吃 `conflict_model.scan_table` 的结论做索引关联，
   不重写一遍冲突判定——校验器与被验对象共犯同一错＝没校验（R-40 教训）。
   只标三类**核心**冲突（场地/教师/同班同组）；"同班不同组同格"是正常
   分批教学（真实表 47 处），不标。
5. **放不进网格的行必须报数**：时段无法识别的行（`db.skipped`）进不了任何格，
   按周计数后随响应返回——沉默地少显示 N 行，比显示一个空格更危险。
6. **周次不夹取**：超出安排表覆盖范围的周次直接报错并说清范围（R-32）。
"""

from __future__ import annotations

from typing import Dict, List, Optional

from app.utils import time_model

# 渲染顺序：与 time_model.ORDER 同源（MORNING/567/678/91011）。
# 刻意**不**复用前端 labels.ts 的 WEEKDAYS/GRID_TIME_SLOTS——那两组是
# 沙盘旧口径（5 天 × 3 时段），会把周末 71 条和上午 4 条排掉。
WEEKDAYS: tuple = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
SLOTS: tuple = tuple(time_model.ORDER)

_SLOT_NAMES = {"MORNING": "上午", "567": "下午一", "678": "下午二", "91011": "晚上"}


def slot_meta() -> List[dict]:
    """时段元信息（顺序即渲染顺序）：机器值 + 人话 + 真实时钟区间。"""
    out = []
    for s in SLOTS:
        clock = time_model.CLOCK.get(s)
        out.append({
            "slot": s,
            "label": _SLOT_NAMES.get(s, s),
            "clock": time_model.clock_text(s) if clock else s,
        })
    return out


def _entry_slots(e) -> List[str]:
    """这条记录真实占住的节次（顺序按 SLOTS，跨节次记录占多格）。"""
    blocked = [s for s in (getattr(e, "blocked", None) or []) if s in SLOTS]
    if not blocked and getattr(e, "time_slot", None) in SLOTS:
        blocked = [e.time_slot]
    return [s for s in SLOTS if s in blocked]


def _cell_key(weekday: str, slot: str) -> str:
    return f"{weekday}|{slot}"


def _match_room(e, place: str) -> bool:
    return (e.room or "").strip() == (place or "").strip()


def _match_teacher(e, place: str) -> bool:
    # scan_table 的分桶键是 strip 后的教师名，这里同口径
    return (e.teacher or "").strip() == (place or "").strip()


def _match_group(e, place: str) -> bool:
    """group 冲突的 place 是 `班级/组`（conflict_model 的约定）。"""
    if "/" not in (place or ""):
        return False
    cls, grp = place.split("/", 1)
    return (e.class_name or "").strip() == cls.strip() and (e.group or "").strip() == grp.strip()


def conflict_marks(entries, conflicts: Optional[Dict[str, list]], week: int) -> Dict[int, set]:
    """把 scan_table 的冲突结论映射到「记录 → 冲突类型集合」。

    不做判定、只做关联：冲突由 `conflict_model.scan_table`（全表唯一权威）
    算好，这里按 (周, 星期, 时段) + 资源标识找到对应记录打标。
    返回 `id(entry) -> {"room", "teacher", "group"}`。
    """
    marks: Dict[int, set] = {}
    if not conflicts:
        return marks
    matchers = {"room": _match_room, "teacher": _match_teacher, "group": _match_group}
    for key, items in conflicts.items():
        # scan_table 的键是 `room_conflicts` 这种带后缀的形式；`parallel_groups`
        # 不是冲突，必须跳过（同班不同组同格＝正常分批，R-56 口径）
        if not key.endswith("_conflicts"):
            continue
        kind = key[: -len("_conflicts")]
        if kind not in matchers:
            continue
        match = matchers[kind]
        for c in items or []:
            if c.get("week") != week:
                continue
            slot = str(c.get("time_slot") or "")
            place = c.get("place") or ""
            for e in entries:
                if e.week != week or (e.weekday or "") != (c.get("weekday") or ""):
                    continue
                if slot not in _entry_slots(e):
                    continue
                if match(e, place):
                    marks.setdefault(id(e), set()).add(kind)
    return marks


def build_grid(entries, *, week: int, class_name: str = "", room: str = "",
               conflicts: Optional[Dict[str, list]] = None,
               skipped: Optional[list] = None,
               week_min: Optional[int] = None, week_max: Optional[int] = None) -> dict:
    """把某一周的安排表重排成网格。

    参数
    ----
    entries   : 全部安排记录（`db.entries`）
    week      : 要看的周次（不在覆盖范围内抛 ValueError，调用方转 400）
    class_name/room : 可选的单元格过滤（空串＝不过滤）；冲突计数**不受过滤影响**，
                      否则按某个班筛选时会显示"0 冲突"，误导
    conflicts : `conflict_model.scan_table` 的输出（可选；给了才标冲突）
    skipped   : `db.skipped`（时段无法识别、未进 entries 的行），按周报数
    week_min/week_max : 覆盖范围；不传则从 entries 推导
    """
    weeks = [e.week for e in entries] or [0]
    w_min = week_min if week_min is not None else min(weeks)
    w_max = week_max if week_max is not None else max(weeks)
    if entries and not (w_min <= week <= w_max):
        raise ValueError(f"第 {week} 周不在安排表覆盖范围（第 {w_min}~{w_max} 周）内")

    cls_filter = (class_name or "").strip()
    room_filter = (room or "").strip()

    # 冲突标记先算（全周、全表口径，不受筛选影响）
    marks = conflict_marks(entries, conflicts, week)

    cells: Dict[str, List[dict]] = {}
    shown = 0
    missing_room = 0
    conflicted = 0
    for e in entries:
        if e.week != week:
            continue
        if cls_filter and (e.class_name or "").strip() != cls_filter:
            continue
        if room_filter and (e.room or "").strip() != room_filter:
            continue
        slots = _entry_slots(e)
        if not slots:
            continue  # 没有可用节次：理论上进不了 entries，防御性跳过
        shown += 1
        no_room = not (e.room or "").strip()
        if no_room:
            missing_room += 1
        kinds = marks.get(id(e)) or set()
        if kinds:
            conflicted += 1
        item = {
            "class_name": e.class_name or "",
            "group": e.group or "",
            "experiment": e.experiment or "",
            "teacher": e.teacher or "",
            "room": e.room or "",
            "time_slot": e.time_slot or "",
            "time_raw": e.time_raw or "",
            "blocked": slots,
            "missing_room": no_room,
            "conflicts": sorted(kinds),
        }
        for s in slots:
            cells.setdefault(_cell_key(e.weekday, s), []).append(item)

    # 冲突计数：全周全表口径（与筛选无关），说清楚范围免得被误读
    conflict_counts = {"room": 0, "teacher": 0, "group": 0}
    for kind in conflict_counts:
        for c in (conflicts or {}).get(f"{kind}_conflicts") or []:
            if c.get("week") == week:
                conflict_counts[kind] += 1

    # 放不进网格的行（时段无法识别）：按周计数，不静默
    unplaced = [s for s in (skipped or []) if s.get("week") == week]

    # 单元格内排序：班级 → 组 → 项目，同一格多条时顺序稳定
    for key in cells:
        cells[key].sort(key=lambda x: (x["class_name"], x["group"], x["experiment"]))

    # 筛选项从**全表**取（不受当前周/当前筛选影响）：否则按班级筛过一次之后，
    # 下拉里就只剩那一个班，想切回去得先清空——典型的"筛选项被筛选吃掉"。
    class_options = sorted({(e.class_name or "").strip() for e in entries
                            if (e.class_name or "").strip()})
    room_options = sorted({(e.room or "").strip() for e in entries
                           if (e.room or "").strip()})

    notes: List[str] = []
    if unplaced:
        notes.append(
            f"第 {week} 周有 {len(unplaced)} 条记录时段无法识别，未在网格中显示"
            f"（数据体检里同样单列；这些记录按整日占用处理）"
        )
    if missing_room:
        notes.append(
            f"本周 {missing_room} 条记录没填场地（灰色标注），不参与教室冲突判定——"
            f"这是数据缺口，不是冲突"
        )
    if any(len(v) > 1 for v in cells.values()):
        notes.append("同一格多条记录：可能是同班不同组的分批教学（正常），也可能是冲突——有冲突的记录带 ⚠ 标记")

    return {
        "week": week,
        "week_min": w_min,
        "week_max": w_max,
        "weekdays": list(WEEKDAYS),
        "slots": slot_meta(),
        "cells": cells,
        "filters": {"class_name": cls_filter, "room": room_filter},
        "class_options": class_options,
        "room_options": room_options,
        "totals": {
            "entries": shown,
            "nonempty_cells": len(cells),
            "missing_room": missing_room,
            "conflict_entries": conflicted,
            "unplaced": len(unplaced),
        },
        # 冲突计数是**全周全表**口径，不受班级/场地筛选影响——按班筛选时
        # 显示"0 冲突"会让教秘以为别的班也没冲突
        "conflict_counts": conflict_counts,
        "conflict_scope": "全周全表（不受班级/场地筛选影响）",
        "notes": notes,
    }
