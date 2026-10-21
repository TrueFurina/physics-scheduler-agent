"""冲突分级 —— 导师座谈（2026-10-21）明确的业务规则。

导师原话：「多数小冲突可直接强制通过，无需额外处理」。

分级口径
--------
  **core（核心）**：场地被占 / 教师本人该时段有课 / 同一班级同一组该时段已有课
                 → 必须调整，**不可**强制通过
  **minor（轻微）**：仅个别学生的课表重叠 → 可强制通过

⛔ 本模块**只产出 core，不判 minor**。识别"个别学生"必须先有学生课表，而
`/upload-student` 是 `require_admin` 且前端零调用；学生 PII 不可顺手放开 ——
用户已明确"学生课表冲突暂时不管"（2026-10-22 产品边界，R-51）。
所以这里不引入 `force` 参数：只做核心冲突时它是永远为假的死参数，
等轻微冲突（需学生课表）一起做才有意义。

⚠️ 整班冲突必须按 **(班级, 组)** 判，不能按班级判
---------------------------------------------
真实表 433 行里「同班不同组、同一格」有 **47 处**，那是正常的分批并行教学
（1 组在 A504 做扭摆法、2 组在 A516 做声速测量）。按班级判会立刻造出 47 处
假阳性 —— 与 R-26「空场地全归 '' 报假冲突」是同族错误。
同理：组缺失的行不参与整组判定（宁可漏报，也不造假阳性；真实表 434 行组全非空）。
"""

from typing import Dict, List, Optional, Set

from collections import defaultdict

CORE = "core"

# kind → 界面文案。core 三类之外不再有其他取值（minor 未实现，见模块头）
KIND_LABELS = {
    "room": "场地被占",
    "teacher": "教师本人该时段有课",
    "group": "同一班级同一组该时段已有课",
}

KIND_FIX = {
    "room": "换一间教室即可解决",
    "teacher": "必须改时间或换指导教师",
    "group": "整组学生分身乏术，必须改时间",
}


def slots_of(entry) -> Set[str]:
    """一条记录真实占住的时段（跨节次记录占多格，见 R-40）。"""
    blocked = getattr(entry, "blocked", None)
    if blocked:
        return set(blocked)
    return {entry.time_slot} if getattr(entry, "time_slot", None) else set()


def _same_teacher(a: str, b: str) -> bool:
    a, b = (a or "").strip(), (b or "").strip()
    if not a or not b:
        return False
    return a == b or a in b or b in a


def _holder(o) -> dict:
    return {
        "class_name": o.class_name or "", "group": o.group or "",
        "experiment": o.experiment or "", "teacher": o.teacher or "",
        "room": o.room or "",
    }


def _hit(kind: str, o, week, weekday, overlap: List[str], resource: str) -> dict:
    return {
        "kind": kind,
        "severity": CORE,
        "label": KIND_LABELS[kind],
        "fix_hint": KIND_FIX[kind],
        "can_force": False,
        "week": week, "weekday": weekday,
        "time_slots": sorted(overlap),
        "resource": resource,
        "holder": _holder(o),
        "detail": (
            f"第{week}周 {weekday} {'/'.join(sorted(overlap))} "
            f"「{resource or '（无场地）'}」已被 {o.class_name or ''}/{o.group or ''} "
            f"{o.experiment or ''}（{o.teacher or '未指派教师'}）占用"
        ),
    }


def classify_move(entries, *, week: int, weekday: str, slots,
                  room: Optional[str] = None, teacher: Optional[str] = None,
                  class_name: Optional[str] = None, group: Optional[str] = None,
                  exclude=None) -> List[dict]:
    """把一条课**要落进某一格**会撞上什么，分门别类说出来。

    与旧逻辑的差别：旧 `_apply_reschedule` 只回一个布尔 + 一句「目标周该时段已有
    安排」，教秘分不清是教室被占还是老师本人有课 —— 而这两种的处理完全不同
    （换教室 vs 必须改时间）。这里按场地/教师/整组三类分别给出占用者。
    """
    want = set(slots) if not isinstance(slots, str) else {slots}
    room = (room or "").strip()
    teacher = (teacher or "").strip()
    class_name = (class_name or "").strip()
    group = (group or "").strip()

    hits: List[dict] = []
    for o in entries:
        if exclude is not None and o is exclude:
            continue
        if o.week != week or (o.weekday or "") != weekday:
            continue
        overlap = sorted(slots_of(o) & want)
        if not overlap:
            continue

        if room and (o.room or "").strip() == room:
            hits.append(_hit("room", o, week, weekday, overlap, room))
        if teacher and _same_teacher(teacher, o.teacher):
            hits.append(_hit("teacher", o, week, weekday, overlap, teacher))
        # 整组：两边组都非空且相等才判（组缺失不判，防假阳性，见模块头）
        if class_name and group and (o.class_name or "").strip() == class_name \
                and (o.group or "").strip() == group:
            hits.append(_hit("group", o, week, weekday, overlap,
                             f"{class_name}/{group}"))
    # 同格多类冲突时按"最难解决的"排前面：整组 > 教师 > 场地
    order = {"group": 0, "teacher": 1, "room": 2}
    hits.sort(key=lambda h: (order.get(h["kind"], 9), h["resource"]))
    return hits


# --------------------------------------------------------------------------- 整表扫描

def scan_table(entries) -> Dict[str, list]:
    """全表扫描三类核心冲突，每条带 `severity` / `kind`。

    额外返回 `parallel_groups`：同班**不同组**同一格的条数。它**不是冲突**
    （真实表 47 处，属正常分批教学），但必须在体检报告里说出来 ——
    否则教秘自己按班级一数，会发现"系统说 0 冲突，可我明明看到 47 处重叠"，
    于是再也不信这个体检。口径说出口，比数字好看重要。
    """
    buckets = {
        "room": defaultdict(list),
        "teacher": defaultdict(list),
        "group": defaultdict(list),
    }
    parallel = 0
    by_cell = defaultdict(list)

    for e in entries:
        for slot in slots_of(e):
            by_cell[(e.week, e.weekday, slot)].append(e)
            if (e.room or "").strip():
                buckets["room"][(e.week, e.weekday, slot, e.room.strip())].append(e)
            if (e.teacher or "").strip():
                buckets["teacher"][(e.week, e.weekday, slot, e.teacher.strip())].append(e)
            if (e.class_name or "").strip() and (e.group or "").strip():
                buckets["group"][(e.week, e.weekday, slot,
                                  (e.class_name.strip(), e.group.strip()))].append(e)

    out: Dict[str, list] = {}
    for kind, mapping in buckets.items():
        found = []
        for key, group_entries in mapping.items():
            if len(group_entries) < 2:
                continue
            parties = {f"{x.class_name}/{x.group}" for x in group_entries if x.class_name}
            # 场地/教师：沿用体检的老判据 —— 同一资源被**不同**班级/组占用才算冲突
            # （同一格同一教师只有一条记录的情况不构成冲突）。
            # 整组：不能用这条判据 —— 同班同组的 parties 恒为 1，会被永远过滤掉。
            if kind in ("room", "teacher") and len(parties) < 2:
                continue
            found.append({
                "kind": kind, "severity": CORE,
                "label": KIND_LABELS[kind], "fix_hint": KIND_FIX[kind],
                "can_force": False,
                "week": key[0], "weekday": key[1], "time_slot": key[2],
                "place": key[3] if kind != "group" else f"{key[3][0]}/{key[3][1]}",
                "involved": sorted({f"{x.class_name}/{x.group}" for x in group_entries}),
                "detail": [f"{x.class_name}/{x.group} {x.experiment}"
                           f"（{x.teacher or '未指派教师'}）" for x in group_entries],
            })
        found.sort(key=lambda c: (c["week"], c["weekday"], str(c["time_slot"])))
        out[f"{kind}_conflicts"] = found

    # 同班不同组并行（正常教学，不计入冲突，但要说出来）
    for _cell, group_entries in by_cell.items():
        classes = defaultdict(set)
        for x in group_entries:
            if (x.class_name or "").strip() and (x.group or "").strip():
                classes[x.class_name.strip()].add(x.group.strip())
        parallel += sum(1 for _c, gs in classes.items() if len(gs) > 1)

    out["parallel_groups"] = parallel
    out["counts"] = {f"{k}_conflict_count": len(out[f"{k}_conflicts"])
                     for k in ("room", "teacher", "group")}
    return out
