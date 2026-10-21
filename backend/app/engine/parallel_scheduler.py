"""并行排课引擎（源自 2026-10-08 教师深度访谈的核心需求）

痛点（受访者原话）："他要每一个实验项目进去以后……先把实验项目调出来，才可以出现学生的名单，
才可以排课……重复性太大了。" 理想形态："所有实验项目列出来，分组分完以后，把时间地点、
安排的老师都进去，就像 AI 一键全部完成。"

本引擎实现：
1. 按课程（班级视角）列出全部实验项目 × 学生组别（绪论课已单独安排，排除）
2. CP-SAT 一次性统一分配：项目×组别 → (周次, 时段, 场地)
   - 上课星期沿用历史安排（真实场景：每个班固定星期进实验室，如 23物本=周四）
3. 硬约束：
   - 每个单元（项目×组别）恰好排一次
   - 同一场地同时段最多一个单元
   - 同一学生组同周最多一个实验、同时段最多一个单元
   - 同一教师同时段最多带一个组（教师按历史沿用）
   - 不与其他课程已排定的场地/教师冲突
4. 软目标：尽量排前面的周（早完成）
"""

from typing import Dict, List, Optional
from collections import defaultdict, Counter

from ortools.sat.python import cp_model

from app.utils.excel_parser import ScheduleDatabase
from app.agent.student_schedule import StudentSchedule

WEEKDAYS = ["周一", "周二", "周三", "周四", "周五"]
TIME_SLOTS = ["567", "678", "91011"]


def _is_intro(project: str) -> bool:
    """绪论课已单独安排（教室已定），不参与并行排课"""
    return "绪论" in (project or "")


class ParallelScheduler:
    """并行排课：一门课的全部实验项目 × 学生组别，一键统一排课"""

    def __init__(self, db: ScheduleDatabase, student: Optional[StudentSchedule] = None):
        self.db = db
        self.student = student

    def get_course_overview(self) -> List[Dict]:
        """按班级聚合：每门课有哪些实验项目、每个项目有哪些学生组别、常用星期"""
        courses: Dict[str, Dict[str, set]] = defaultdict(lambda: defaultdict(set))
        weekday_votes: Dict[str, Counter] = defaultdict(Counter)
        for e in self.db.entries:
            if e.class_name and e.experiment and e.group and not _is_intro(e.experiment):
                courses[e.class_name][e.experiment].add(e.group)
                if e.weekday in WEEKDAYS:
                    weekday_votes[e.class_name][e.weekday] += 1
        return [
            {
                "class_name": cls,
                "weekday": weekday_votes[cls].most_common(1)[0][0] if weekday_votes[cls] else "周四",
                "projects": [
                    {"project": p, "groups": sorted(gs)}
                    for p, gs in sorted(projs.items())
                ],
                "total_units": sum(len(gs) for gs in projs.values()),
            }
            for cls, projs in sorted(courses.items())
        ]

    def solve(self, class_name: str, weekday: Optional[str] = None,
              start_week: int = 2, weeks: int = 10,
              max_time_seconds: float = 10.0) -> Dict:
        """并行排课求解：该课程所有 (项目, 组别) 单元 → (周次, 时段, 场地)"""
        # 1. 收集单元（项目×组别，排除绪论）+ 历史教师 + 历史上课星期
        unit_teacher: Dict[tuple, str] = {}
        weekday_votes: Counter = Counter()
        for e in self.db.entries:
            if e.class_name != class_name:
                continue
            if e.weekday in WEEKDAYS:
                weekday_votes[e.weekday] += 1
            if not e.experiment or not e.group or _is_intro(e.experiment):
                continue
            key = (e.experiment, e.group)
            if key not in unit_teacher and e.teacher:
                unit_teacher[key] = e.teacher

        units, seen = [], set()
        for e in self.db.entries:
            if e.class_name != class_name or not e.experiment or not e.group:
                continue
            if _is_intro(e.experiment):
                continue
            key = (e.experiment, e.group)
            if key not in seen:
                seen.add(key)
                units.append({"project": e.experiment, "group": e.group})
        if not units:
            return {"success": False, "error": f"未找到 {class_name} 的实验项目/分组数据"}

        # 2. 上课星期：参数优先，否则取历史最常用
        wd = weekday or (weekday_votes.most_common(1)[0][0] if weekday_votes else "周四")
        if wd not in WEEKDAYS:
            wd = "周四"

        rooms = self.db.get_all_rooms()
        if not rooms:
            return {"success": False, "error": "没有可用场地"}

        # 3. 其他课程已占用（同星期）：场地 / 教师
        room_occupied, teacher_occupied = set(), set()
        for e in self.db.entries:
            if e.class_name == class_name or e.weekday != wd:
                continue
            # 按 blocked 记占用：`14:00-17:00` 这类跨节次记录同时占 567/678，
            # 只记主标签会让另一半节次被判空闲（R-40）。
            for ts in getattr(e, "blocked", None) or [e.time_slot]:
                room_occupied.add((e.week, ts, e.room))
                if e.teacher:
                    teacher_occupied.add((e.week, ts, e.teacher))

        # 时段无法识别的行：不知道具体节次 ⇒ 当天三个时段全按占用算。
        # 少了这一步，求解器会把这些教室当成全天空闲（实测真实表里有 1 条）。
        for s in (getattr(self.db, "skipped", None) or []):
            if s.get("weekday") != wd:
                continue
            for ts in TIME_SLOTS:
                if s.get("room"):
                    room_occupied.add((s["week"], ts, s["room"]))
                if s.get("teacher"):
                    teacher_occupied.add((s["week"], ts, s["teacher"]))

        # 4. CP-SAT 建模（资源不足自动加周重试一次）
        for attempt_weeks in (weeks, weeks + 4):
            week_list = list(range(start_week, start_week + attempt_weeks))
            n_w, n_t, n_r = len(week_list), len(TIME_SLOTS), len(rooms)
            n_u = len(units)

            model = cp_model.CpModel()
            x = {}
            for i in range(n_u):
                for w in range(n_w):
                    for t in range(n_t):
                        for r in range(n_r):
                            x[(i, w, t, r)] = model.NewBoolVar(f"x_{i}_{w}_{t}_{r}")

            # 每单元恰好一次
            for i in range(n_u):
                model.Add(sum(x[(i, w, t, r)] for w in range(n_w)
                              for t in range(n_t) for r in range(n_r)) == 1)

            group_names = sorted({u["group"] for u in units})
            group_units = {g: [i for i, u in enumerate(units) if u["group"] == g]
                           for g in group_names}
            teachers = sorted({unit_teacher.get((u["project"], u["group"]))
                               for u in units} - {None})
            teacher_units = {te: [i for i, u in enumerate(units)
                                  if unit_teacher.get((u["project"], u["group"])) == te]
                             for te in teachers}

            # 同组同周最多一个实验（真实节奏：每组每周做一个实验）
            for g, idxs in group_units.items():
                if len(idxs) > 1:
                    for w in range(n_w):
                        model.Add(sum(x[(i, w, t, r)] for i in idxs
                                      for t in range(n_t) for r in range(n_r)) <= 1)

            for w in range(n_w):
                wk = week_list[w]
                for t in range(n_t):
                    ts = TIME_SLOTS[t]
                    # 场地互斥
                    for r in range(n_r):
                        model.Add(sum(x[(i, w, t, r)] for i in range(n_u)) <= 1)
                    # 同组同时段互斥
                    for g, idxs in group_units.items():
                        if len(idxs) > 1:
                            model.Add(sum(x[(i, w, t, r)] for i in idxs
                                          for r in range(n_r)) <= 1)
                    # 同教师同时段互斥
                    for te, idxs in teacher_units.items():
                        model.Add(sum(x[(i, w, t, r)] for i in idxs
                                      for r in range(n_r)) <= 1)
                    # 其他课程占用 → 禁用
                    for i in range(n_u):
                        u = units[i]
                        te = unit_teacher.get((u["project"], u["group"]))
                        teacher_busy_here = bool(te) and (wk, ts, te) in teacher_occupied
                        for r in range(n_r):
                            if (wk, ts, rooms[r]) in room_occupied:
                                model.Add(x[(i, w, t, r)] == 0)
                            # 教师占用必须**逐个教室**禁用。原先这一句写在 `for r` 循环之外，
                            # 于是 `r` 停在 n_r-1，只有最后一个教室被禁 —— 教师互斥实际只覆盖
                            # 1/10 的教室（实测 17 个班里 7 个班共 48 处教师双排）。
                            if teacher_busy_here:
                                model.Add(x[(i, w, t, r)] == 0)

            # 目标：尽早完成
            model.Maximize(sum((n_w - w) * x[(i, w, t, r)]
                               for i in range(n_u) for w in range(n_w)
                               for t in range(n_t) for r in range(n_r)))

            solver = cp_model.CpSolver()
            solver.parameters.max_time_in_seconds = max_time_seconds
            status = solver.Solve(model)

            if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
                # 5. 提取结果
                schedule = []
                for i, u in enumerate(units):
                    for w in range(n_w):
                        for t in range(n_t):
                            for r in range(n_r):
                                if solver.Value(x[(i, w, t, r)]) == 1:
                                    schedule.append({
                                        "project": u["project"],
                                        "group": u["group"],
                                        "week": week_list[w],
                                        "weekday": wd,
                                        "time_slot": TIME_SLOTS[t],
                                        "room": rooms[r],
                                        "teacher": unit_teacher.get((u["project"], u["group"]), ""),
                                    })
                schedule.sort(key=lambda s: (s["week"], TIME_SLOTS.index(s["time_slot"]),
                                             s["group"], s["project"]))
                by_week = Counter(s["week"] for s in schedule)
                return {
                    "success": True,
                    "status": solver.StatusName(status),
                    "weekday": wd,
                    "total_units": len(units),
                    "scheduled": len(schedule),
                    "weeks_used": attempt_weeks,
                    "schedule": schedule,
                    "by_week": {str(k): v for k, v in sorted(by_week.items())},
                    "note": f"并行排课（CP-SAT）：{len(units)} 个项目×组别单元一键排定，"
                            f"固定{wd}，组/场地/教师三重互斥",
                }
            if status == cp_model.INFEASIBLE and attempt_weeks == weeks:
                continue  # 周数不够，加周重试
            break

        return {"success": False,
                "error": "无可行排课方案（场地/教师资源在该星期被占用过多，请尝试其他星期）"}
