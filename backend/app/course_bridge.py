"""课程维度桥接 —— 把主数据的「课程」接进扁平的安排表行。

为什么需要它
------------
`ScheduleEntry` **没有课程字段**（安排表一行 = 班级/组/项目/场地/教师，课
程不在其中）。课程只存在于主数据的 `experiment.course` 上，靠项目名反推。
所以「这条记录属于哪门课」必须查主数据才知道 —— 这就是本模块唯一的职责。

导师点名的后续难点（2026-10-21）：「若同一班同时上多门含实验的课程，需跨
课程合并并标冲突」。

先说清楚现状（不粉饰）
----------------------
`/export/class-schedule` 按 `class_name` 过滤**本来就合并了全部记录**，不分
课程 —— 所以"合并"这件事它一直在做。真正缺的是两样：

1. **没有课程列**：当前真实表所有项目都归默认课程「大学物理实验」（课程数 1），
   一页班级课表看不出问题；但教秘一旦用 R-55 的台账导回把某项目改归另一门课，
   17 页班级课表立刻变成一堆无法归因的行 —— 看不出哪行属于哪门课。
2. **跨课程撞车不标**：同一个班同一组，在两门不同课程下被排进同一格，学生
   分身乏术。这比同学科内的整组冲突更难被发现 —— 两门课的任课教师互不知情，
   谁都不会觉得自己排错了。

判据（⚠️ 与 R-56 整组冲突同族，别踩同一个坑）
---------------------------------------------
跨课程撞车 = 同 (班级, **组**, 周, 星期, 真实重叠格) 且两条记录的**课程不同**。

- 必须带**组**：真实表「同班不同组、同一格」有 **47 处**，那是正常的分批并行
  教学（1 组 A504 扭摆法 / 2 组 A516 声速测量）。按班级判 = 立刻 47 处假阳性，
  与 R-26「空场地全归 '' 报假冲突」是同族错误。
- 必须**课程不同**：课程相同的同班同组撞格由 `conflict_model` 的 `group` 类
  覆盖（那是同一门课内部的排课错误），这里只管跨课程那一层，不重复报。

覆盖度（`course_coverage`）
--------------------------
课程是反推出来的，就有"反推不出来"的情况：项目不在主数据里、或主数据还没
初始化。这些**必须说出来**，否则教秘看到"跨课程冲突 0 处"会以为真的查过了，
实际是根本没查（R-35「时段看不懂就整行静默丢弃」同族错误）。
"""

from collections import defaultdict
from typing import Dict, List, Optional

from app.master_data import DEFAULT_COURSE

CROSS_COURSE_KIND = "cross_course"
CROSS_COURSE_LABEL = "同一班同一组在两门课程的安排撞车"
CROSS_COURSE_FIX = "两门课程的任课教师互不知情，需两位老师协同改时间"

UNKNOWN_PROJECT = "（项目不在主数据中）"


def _norm(s) -> str:
    return str(s or "").strip()


def project_courses(master) -> Dict[str, str]:
    """项目名 → 课程名。主数据没初始化（None）时返回空表，由调用方回落默认。"""
    if master is None:
        return {}
    experiments = getattr(master, "experiments", None) or {}
    out: Dict[str, str] = {}
    for name, ex in experiments.items():
        out[_norm(name)] = _norm(ex.get("course")) or DEFAULT_COURSE
    return out


def resolve_course(entry, courses: Optional[Dict[str, str]] = None) -> str:
    """一条记录属于哪门课。

    优先级（R-66 显式外键）：
      ① 条目**自带** course（源安排表有课程列时解析而来）—— 权威、无歧义，直接用；
      ② 否则按项目名查主数据 `experiment.course` 反推；
      ③ 都查不到（主数据未初始化 / 项目不在主数据里）回落 `DEFAULT_COURSE` ——
         宁可把它归到默认课，也不让一行记录变成"没有课程的幽灵行"被静默丢掉。

    为什么 ① 优先：主数据 `experiments` 以**项目名**为 key、每个项目只有单一 course，
    同名项目跨两门课时会在主数据里塌缩成一门。条目自带的 course 是逐行的事实，
    不受这层塌缩影响 —— 这正是"显式外键"要解决的歧义。
    """
    explicit = _norm(getattr(entry, "course", ""))
    if explicit:
        return explicit
    if not courses:
        return DEFAULT_COURSE
    return courses.get(_norm(getattr(entry, "experiment", "")), DEFAULT_COURSE)


def course_coverage(entries, master) -> Dict[str, int]:
    """课程归属的可信度：**没查出来的要报出来**，不能让"0 处"被读成"查过了"。"""
    courses = project_courses(master)
    total = len(entries)
    named = sum(1 for e in entries if _norm(getattr(e, "experiment", "")))
    known = sum(1 for e in entries
                if _norm(getattr(e, "experiment", "")) in courses) if courses else 0
    # 源表自带课程列、逐行显式归属的条数（R-66）：这部分不依赖项目名反推，最可信。
    explicit_in_entry = sum(1 for e in entries if _norm(getattr(e, "course", "")))
    explicit = 0
    for e in entries:
        c = resolve_course(e, courses)
        if c != DEFAULT_COURSE:
            explicit += 1
    return {
        "total_entries": total,
        "named_project": named,
        "project_in_master": known,
        "explicit_in_entry": explicit_in_entry,  # 源表课程列直接给出的条数（最可信）
        "explicit_course": explicit,          # 归到非默认课程的条数（含显式 + 反推到的非默认）
        "fallback_default": total - explicit,  # 回落默认课程的条数
        "master_ready": bool(courses),        # False ⇒ 主数据没初始化，跨课程根本没查
        "distinct_courses": len(set(resolve_course(e, courses) for e in entries)),
    }


def slots_of(entry) -> set:
    """真实占住的节次（跨节次记录占多格，见 R-40）。"""
    blocked = getattr(entry, "blocked", None)
    if blocked:
        return set(blocked)
    ts = getattr(entry, "time_slot", None)
    return {ts} if ts else set()


def scan_cross_course(entries, master) -> Dict[str, list]:
    """跨课程撞车：同一班同一组同一格，却分属**两门不同的课程**。

    返回 `conflicts`（每条带 severity/label/fix_hint）与 `coverage`（口径）。
    `master_ready=False` 时课程全部回落默认 ⇒ 恒 0 处，此时 `coverage` 会把
    这件事说出来，绝不让 0 处被读成"查过了，没问题"。
    """
    courses = project_courses(master)
    buckets = defaultdict(list)
    for e in entries:
        cls, grp = _norm(getattr(e, "class_name", "")), _norm(getattr(e, "group", ""))
        # 组缺失不参与判定：宁可漏报，也不造假阳性（真实表组全非空，此分支不触发）
        if not cls or not grp:
            continue
        for slot in slots_of(e):
            buckets[(e.week, _norm(e.weekday), slot, cls, grp)].append(e)

    found: List[dict] = []
    for key, group_entries in buckets.items():
        if len(group_entries) < 2:
            continue
        by_course = defaultdict(list)
        for e in group_entries:
            by_course[resolve_course(e, courses)].append(e)
        if len(by_course) < 2:
            continue  # 只有一门课 → 同学科内的整组冲突，归 conflict_model 管
        week, weekday, slot, cls, grp = key
        involved = sorted(by_course)
        found.append({
            "kind": CROSS_COURSE_KIND,
            "severity": "core",
            "label": CROSS_COURSE_LABEL,
            "fix_hint": CROSS_COURSE_FIX,
            "can_force": False,
            "week": week, "weekday": weekday, "time_slot": slot,
            "place": f"{cls}/{grp}",
            "involved": involved,
            "detail": [f"【{c}】" + "；".join(
                f"{x.experiment}（{x.teacher or '未指派教师'}）@{x.room or '未填场地'}"
                for x in lst) for c, lst in sorted(by_course.items())],
        })
    found.sort(key=lambda c: (c["week"], c["weekday"], str(c["time_slot"]), c["place"]))
    return {"conflicts": found, "coverage": course_coverage(entries, master)}


def group_by_course(entries, master):
    """按课程给一个班的记录分块：`[(课程名, [记录...]), ...]`，课程名升序。

    导出行列表时用：同一班的记录先按课程分块，块内按周次/星期/节次排序。
    只有一门课时也走这条路径 —— 只是恰好只有一个块，不多不少一个标题行。
    """
    courses = project_courses(master)
    buckets = defaultdict(list)
    for e in entries:
        buckets[resolve_course(e, courses)].append(e)
    return sorted(buckets.items(), key=lambda kv: str(kv[0]))
