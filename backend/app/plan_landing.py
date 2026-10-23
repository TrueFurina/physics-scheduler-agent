"""排课方案落地：把求解结果写回安排表（预览 → 采纳 → 撤销）

为什么需要
-----------
全校一键（solve_school）算出的方案此前只能看、只能导出 —— 安排表
（`db.entries`）还是原来上传的那份，看板 / 班级课表 / 场地课表 / 数据体检
用的都是旧数据，教秘必须把 Excel 人工录回教务系统。"排完了"和"能交付"
之间差最后一公里。

设计（与既有架构同构，不引入第二套持久化引擎）
----------------------------------------------
- `db.entries` 是纯内存态：重启时 `_load_default_schedule` 从 schedule.xlsx
  重载，并重放 `applied_moves` 覆盖层（审批通过的调课）。落地沿用**同一模式**：
  `landed_plans` 表（与 requests.db 同库、同目录、同 gitignore）存方案 +
  时间戳 + 落地前快照；重启后按**时间戳顺序**与 applied_moves 一起重放 ——
  后发生的事件赢，审批调课不会被落地静默吞掉，落地也不会被后来的调课抹掉。
- 只替换**可排单元**（班级×项目×组）的行；绪论课 / 考试考核类
  （`is_schedulable_project` 为 False）原样保留 —— 方案里从来就没有它们，
  一并替换掉就是数据丢失。
- 方案没覆盖的可排单元：**保留原行**并在预览里列出来。求解器没能排满时
  （部分成功），删掉这些行就是静默丢课。
- 采纳时 diff **重新计算**，不信预览那一份：预览与采纳之间若有审批调课
  生效，按旧 diff 写入就是偷换（R-36：写路径的任何不实都必须可见）。
- 撤销＝弹掉最后一条落地 + 恢复其 before 快照；落地之后又生效的调课**如实
  计数**并告知需重新审批，不静默吞掉。
"""

import json
import os
import sqlite3
from typing import Dict, List, Optional, Tuple

from app.master_data import is_schedulable_project
from app.utils.excel_parser import PendingTask, ScheduleEntry

# 与 parallel_scheduler._tasks_of 的兜底一致：没有组名的任务按 1 组处理
UNIT_EMPTY_GROUP = "1组"


class PlanValidationError(Exception):
    """方案行缺必填字段。bad 是 [{index, row, missing}] —— 报出来，绝不静默丢行。"""

    def __init__(self, bad: List[dict]):
        super().__init__(f"方案有 {len(bad)} 行缺少必填字段")
        self.bad = bad


# ---------------------------------------------------------------------------
# 单元标识与行指纹
# ---------------------------------------------------------------------------

def _norm_group(g) -> str:
    return (str(g) if g is not None else "").strip() or UNIT_EMPTY_GROUP


def unit_key(class_name, experiment, group) -> tuple:
    """可排单元的业务标识：班级×项目×组（与求解器的单元粒度一致）。"""
    return ((str(class_name or "")).strip(),
            (str(experiment or "")).strip(),
            _norm_group(group))


def entry_unit(e) -> tuple:
    return unit_key(e.class_name, e.experiment, e.group)


def unit_label(unit: tuple) -> str:
    cls, exp, grp = unit
    return f"{cls} / {exp} / {grp}"


def _entry_sig(e) -> tuple:
    """已排行的指纹：按 **blocked** 比，不按主标签。

    `14:00-17:00` 主标签是 567，但真实占住 567+678 两节（R-40）。只比主标签
    会把"方案只排了 567"误判成"与旧行一致"，差异预览就漏掉了半节课。
    """
    blocked = tuple(sorted(set(getattr(e, "blocked", None) or [e.time_slot])))
    return (e.week, e.weekday, blocked, e.room or "", e.teacher or "")


def _row_sig(r: dict) -> tuple:
    return (int(r["week"]), r["weekday"], (r["time_slot"],),
            r.get("room") or "", r.get("teacher") or "")


def _entry_row(e) -> dict:
    return {"week": e.week, "weekday": e.weekday, "time_slot": e.time_slot,
            "class_name": e.class_name, "group": e.group, "project": e.experiment,
            "room": e.room, "teacher": e.teacher}


# ---------------------------------------------------------------------------
# 方案行校验 / 归一化
# ---------------------------------------------------------------------------

def normalize_plan(rows, default_class: Optional[str] = None) -> List[dict]:
    """校验并归一化方案行。认不出的行**报错列出**，绝不静默丢弃（R-35 同族）。"""
    bad: List[dict] = []
    out: List[dict] = []
    for i, r in enumerate(rows or []):
        if not isinstance(r, dict):
            bad.append({"index": i, "row": r, "missing": ["整行不是对象"]})
            continue
        cls = (r.get("class_name") or default_class or "").strip()
        project = (r.get("project") or "").strip()
        group = (r.get("group") or "").strip()
        weekday = (r.get("weekday") or "").strip()
        slot = (r.get("time_slot") or "").strip()
        try:
            week = int(r.get("week"))
        except (TypeError, ValueError):
            week = None
        missing = [name for name, v in (
            ("班级", cls), ("实验项目", project), ("学生组", group),
            ("周次", week), ("星期", weekday), ("时段", slot)) if not v]
        if missing:
            bad.append({"index": i, "row": r, "missing": missing})
            continue
        out.append({"class_name": cls, "project": project, "group": group,
                    "week": week, "weekday": weekday, "time_slot": slot,
                    "room": (r.get("room") or "").strip(),
                    "teacher": (r.get("teacher") or "").strip()})
    if bad:
        raise PlanValidationError(bad)
    return out


# ---------------------------------------------------------------------------
# 差异预览（不写入）
# ---------------------------------------------------------------------------

def diff_plan(plan_rows: List[dict], entries: List) -> dict:
    """方案 vs 当前安排表的差异。pure：只读，不改任何状态。"""
    plan: Dict[tuple, List[dict]] = {}
    for r in plan_rows:
        plan.setdefault(unit_key(r["class_name"], r["project"], r["group"]), []).append(r)

    cur: Dict[tuple, List] = {}
    nonsched = 0
    for e in entries:
        if is_schedulable_project(e.experiment):
            cur.setdefault(entry_unit(e), []).append(e)
        else:
            nonsched += 1

    added, changed = [], []
    unchanged = 0
    for unit, rows in plan.items():
        old = cur.get(unit)
        if not old:
            added.append({"unit": unit_label(unit), "rows": rows})
            continue
        if sorted(_entry_sig(e) for e in old) == sorted(_row_sig(r) for r in rows):
            unchanged += 1
        else:
            changed.append({"unit": unit_label(unit),
                            "old": [_entry_row(e) for e in old], "new": rows})

    # 方案没覆盖的可排单元：保留原行（求解未覆盖就删＝静默丢课），单列出来
    uncovered = [{"unit": unit_label(u), "rows": [_entry_row(e) for e in es]}
                 for u, es in sorted(cur.items()) if u not in plan]
    uncovered_rows = sum(len(u["rows"]) for u in uncovered)

    # 跨时段长实验：原表用多行表示一次长课（同周/同天地/同教师占 567+678，
    # 如 13:30-18:00 的 4.5 小时实验）。方案每单元只排一个时段 ⇒ 落地会把这些
    # 长实验**截短**。真实表里有 6 个这样的单元（24海资_1 全部实验）——
    # 不单列出来的话，教秘要在 400 条差异里自己看出"2 行变 1 行"。
    long_sessions = []
    for unit, rows in plan.items():
        old = cur.get(unit)
        if not old:
            continue
        groups: Dict[tuple, set] = {}
        for e in old:
            # 长实验有两种表示，都必须认出来：
            # ① 同周同天地多行，每行一个时段（567 行 + 678 行）；
            # ② 单行多节次（blocked=["567","678"]，R-40 的 14:00-17:00）。
            # 只认 ① 会把 ② 漏掉——而那正是真实表里 14:00-17:00 的存法。
            slots = set(getattr(e, "blocked", None) or [e.time_slot])
            groups.setdefault((e.week, e.weekday, e.room or "", e.teacher or ""),
                              set()).update(slots)
        long_slots = {s for slots in groups.values() if len(slots) > 1 for s in slots}
        lost = long_slots - {r["time_slot"] for r in rows}
        if lost:
            long_sessions.append({"unit": unit_label(unit),
                                  "old_slots": sorted(long_slots),
                                  "plan_slots": sorted({r["time_slot"] for r in rows})})
    for c in changed:
        c["long_session"] = any(ls["unit"] == c["unit"] for ls in long_sessions)

    warnings: List[str] = []
    if uncovered:
        warnings.append(
            f"{len(uncovered)} 个可排单元不在方案里（求解未覆盖），共 {uncovered_rows} 行，"
            f"已保留原安排——请确认是求解失败还是这些单元本就不该排")
    if long_sessions:
        warnings.append(
            f"⚠️ {len(long_sessions)} 个单元在原表是**跨时段的长实验**（同周同天地占多个时段，"
            f"如 13:30-18:00），方案每单元只排一个时段，落地后这些长实验将被截短——"
            f"如确需保留第二时段，请在教务系统人工补回")
    no_room = [r for r in plan_rows if not r["room"]]
    if no_room:
        warnings.append(f"{len(no_room)} 条方案行没有场地（灰箱场景无场地信息，落地后这些行也没有）")
    no_teacher = [r for r in plan_rows if not r["teacher"]]
    if no_teacher:
        warnings.append(f"{len(no_teacher)} 条方案行没有教师（清单与主数据都没有教师信息）")

    return {
        "plan_units": len(plan),
        "plan_rows": len(plan_rows),
        "before_entries": len(entries),
        "after_entries": nonsched + uncovered_rows + len(plan_rows),
        "added": added,
        "changed": changed,
        "unchanged_units": unchanged,
        "uncovered": uncovered,
        "long_sessions": long_sessions,
        "untouched_nonsched": nonsched,
        "warnings": warnings,
        "has_changes": bool(added or changed),
    }


# ---------------------------------------------------------------------------
# 快照（撤销用）
# ---------------------------------------------------------------------------

def snapshot(db) -> dict:
    """落地前的完整内存态（entries + pending）。撤销＝恢复它。"""
    return {
        "entries": [{"week": e.week, "weekday": e.weekday, "time_slot": e.time_slot,
                     "class_name": e.class_name, "group": e.group,
                     "experiment": e.experiment, "room": e.room, "teacher": e.teacher,
                     "time_raw": getattr(e, "time_raw", "") or "",
                     "blocked": list(getattr(e, "blocked", None) or [e.time_slot])}
                    for e in db.entries],
        "pending": [{"class_name": t.class_name, "experiment": t.experiment,
                     "group": t.group, "teacher": t.teacher, "room": t.room,
                     "course": getattr(t, "course", "") or "",
                     "week": getattr(t, "week", None),
                     "weekday": getattr(t, "weekday", "") or "",
                     "source": getattr(t, "source", "schedule") or "schedule"}
                    for t in getattr(db, "pending", None) or []],
    }


def restore(db, snap: dict) -> None:
    """恢复快照并重建索引（索引必须现场重建，否则占用查询读到旧状态）。"""
    db.entries = [ScheduleEntry(
        week=int(r["week"]), weekday=r["weekday"], time_slot=r["time_slot"],
        class_name=r.get("class_name") or "", group=r.get("group") or "",
        experiment=r.get("experiment") or "", room=r.get("room") or "",
        teacher=r.get("teacher") or "", time_raw=r.get("time_raw") or "",
        blocked=list(r.get("blocked") or [r["time_slot"]]),
    ) for r in snap.get("entries") or []]
    db.pending = [PendingTask(
        class_name=p.get("class_name") or "", experiment=p.get("experiment") or "",
        group=p.get("group") or "", teacher=p.get("teacher") or "",
        room=p.get("room") or "", course=p.get("course") or "",
        week=p.get("week"), weekday=p.get("weekday") or "",
        source=p.get("source") or "schedule",
    ) for p in snap.get("pending") or []]
    db._build_index()


# ---------------------------------------------------------------------------
# 应用（采纳 / 重启重放共用）
# ---------------------------------------------------------------------------

def apply_plan(db, plan_rows: List[dict]) -> dict:
    """把方案写进 db.entries：替换可排单元的行，其余原样保留。

    幂等：同一份方案重复应用结果相同（重启重放依赖这一点）。
    """
    plan: Dict[tuple, List[dict]] = {}
    for r in plan_rows:
        plan.setdefault(unit_key(r["class_name"], r["project"], r["group"]), []).append(r)

    kept = [e for e in db.entries
            if not is_schedulable_project(e.experiment) or entry_unit(e) not in plan]
    new_entries = [ScheduleEntry(
        week=r["week"], weekday=r["weekday"], time_slot=r["time_slot"],
        class_name=r["class_name"], group=r["group"], experiment=r["project"],
        room=r["room"], teacher=r["teacher"], time_raw="", blocked=[r["time_slot"]],
    ) for r in plan_rows]

    db.entries = kept + new_entries
    db._build_index()
    # 已排掉的待排任务不再算待排：否则总览永远显示"含 N 个待排单元"，
    # 而求解器又因 seen 去重不会重排它们 —— 界面与行为会对不上。
    db.pending = [t for t in getattr(db, "pending", None) or []
                  if unit_key(t.class_name, t.experiment, t.group) not in plan]
    return {"kept": len(kept), "replaced_with": len(new_entries),
            "total": len(db.entries)}


# ---------------------------------------------------------------------------
# 持久化（sqlite，与 requests.db 同库 —— 不引入第二套存储引擎）
# ---------------------------------------------------------------------------

class LandingStore:
    """landed_plans 表：落地方案 + 时间戳 + 落地前快照。

    测试可传临时库路径（与 `_DB_PATH` 被 monkeypatch 的行为一致）。
    """

    def __init__(self, db_path: str):
        self.path = db_path

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def init_schema(self) -> None:
        conn = self._conn()
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS landed_plans (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    landed_at TEXT NOT NULL,
                    plan_json TEXT NOT NULL,
                    before_json TEXT NOT NULL,
                    after_entries INTEGER DEFAULT 0,
                    note TEXT DEFAULT ''
                )
            """)
            conn.commit()
        finally:
            conn.close()

    def save(self, plan_rows: List[dict], before: dict, note: str = "") -> dict:
        from datetime import datetime
        # 微秒精度（与 applied_moves.applied_at 一致）：重启重放按时间戳混排，
        # 秒级精度会让同一秒内的落地与调课无法区分先后。
        landed_at = datetime.now().isoformat(timespec="microseconds")
        conn = self._conn()
        try:
            cur = conn.execute(
                "INSERT INTO landed_plans (landed_at, plan_json, before_json, after_entries, note)"
                " VALUES (?, ?, ?, ?, ?)",
                (landed_at, json.dumps(plan_rows, ensure_ascii=False),
                 json.dumps(before, ensure_ascii=False), len(before.get("entries") or []),
                 note or ""))
            conn.commit()
            return {"id": cur.lastrowid, "landed_at": landed_at,
                    "after_entries": len(before.get("entries") or [])}
        finally:
            conn.close()

    def all(self) -> List[dict]:
        """全部落地记录（重启重放用，按 id 顺序）。"""
        self.init_schema()
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT id, landed_at, plan_json FROM landed_plans ORDER BY id").fetchall()
            return [{"id": r["id"], "landed_at": r["landed_at"],
                     "plan": json.loads(r["plan_json"])} for r in rows]
        finally:
            conn.close()

    def latest(self) -> Optional[dict]:
        self.init_schema()
        conn = self._conn()
        try:
            r = conn.execute(
                "SELECT * FROM landed_plans ORDER BY id DESC LIMIT 1").fetchone()
            return dict(r) if r else None
        finally:
            conn.close()

    def pop_latest(self) -> Optional[dict]:
        """弹出最后一条（撤销）。返回被弹出的记录。"""
        rec = self.latest()
        if rec:
            conn = self._conn()
            try:
                conn.execute("DELETE FROM landed_plans WHERE id = ?", (rec["id"],))
                conn.commit()
            finally:
                conn.close()
        return rec

    def moves_after(self, ts: str) -> int:
        """落地时间戳之后生效的审批调课条数（撤销时要如实告知会一起回滚）。"""
        if not ts:
            return 0
        try:
            conn = self._conn()
            try:
                r = conn.execute(
                    "SELECT COUNT(*) AS n FROM applied_moves"
                    " WHERE applied_at != '' AND applied_at > ?", (ts,)).fetchone()
                return int(r["n"]) if r else 0
            finally:
                conn.close()
        except sqlite3.Error:
            return 0  # 库里还没有 applied_moves 表（全新临时库）

    def clear(self) -> int:
        """清空（换了安排表时调用：旧表上的方案对新表没有意义）。返回丢弃条数。"""
        self.init_schema()
        conn = self._conn()
        try:
            n = conn.execute("SELECT COUNT(*) AS n FROM landed_plans").fetchone()["n"]
            conn.execute("DELETE FROM landed_plans")
            conn.commit()
            return int(n)
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# 撤销
# ---------------------------------------------------------------------------

def undo(db, store: LandingStore) -> dict:
    """撤销最后一次落地：恢复 before 快照 + 弹出记录。

    落地之后又生效的调课会随快照一起回滚 —— 不静默吞，计数报出来。
    """
    rec = store.latest()
    if not rec:
        return {"success": False, "error": "当前没有可撤销的落地方案"}
    before = json.loads(rec["before_json"])
    restore(db, before)
    store.pop_latest()
    return {
        "success": True,
        "landed_at": rec["landed_at"],
        "restored_entries": len(db.entries),
        "rolled_back_moves": store.moves_after(rec["landed_at"]),
    }


def replay_landing(db, plan_rows: List[dict]) -> dict:
    """重启重放：按单元差异重新应用（幂等，不写 store）。"""
    return apply_plan(db, plan_rows)
