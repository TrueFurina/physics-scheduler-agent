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

from app.utils.excel_parser import ScheduleDatabase, looks_like_exam
from app.agent.student_schedule import StudentSchedule

WEEKDAYS = ["周一", "周二", "周三", "周四", "周五"]
# 真实表 433 条里 71 条（16%）在周六/周日 —— 一所学校"金工 5、6 周排周六"是常态。
# 建模用 WEEKDAYS，但"这班平时哪天上课"必须把周末算进去，否则 42/46 节课都在
# 周末的班会被判成"周一出勤"（见 get_course_overview）。
ALL_WEEKDAYS = WEEKDAYS + ["周六", "周日"]
TIME_SLOTS = ["567", "678", "91011"]
# 安排表里没有任何已排记录时（只上传大纲清单），按一学期的默认跨度排
DEFAULT_SEMESTER_WEEKS = 16


def _is_intro(project: str) -> bool:
    """绪论课已单独安排（教室已定），不参与并行排课"""
    return "绪论" in (project or "")


class ParallelScheduler:
    """并行排课：一门课的全部实验项目 × 学生组别，一键统一排课"""

    def __init__(self, db: ScheduleDatabase, student: Optional[StudentSchedule] = None,
                 extra_tasks: Optional[List] = None):
        self.db = db
        self.student = student
        # 待排任务（PendingTask）：表里没排时间的行 + 上传的教学大纲/主数据清单。
        # 有了它，"排一个去年不存在的新实验项目"不再需要先伪造一行假课表。
        self.extra_tasks = list(extra_tasks or [])

    def _tasks_of(self, class_name: str) -> List:
        return [t for t in self.extra_tasks
                if t.class_name == class_name and t.experiment
                and not _is_intro(t.experiment)
                # 考试/考核类不进实验排课（真实表里有 3 行"大物实验B/C 考试"没排时间）。
                # 它们仍被记在待排清单里并报出来，只是不参与求解。
                and not looks_like_exam(t.experiment)]

    def get_course_overview(self) -> List[Dict]:
        """按班级聚合：每门课有哪些实验项目、每个项目有哪些学生组别、常用星期"""
        courses: Dict[str, Dict[str, set]] = defaultdict(lambda: defaultdict(set))
        weekday_votes: Dict[str, Counter] = defaultdict(Counter)
        pending_units: Dict[str, set] = defaultdict(set)
        for e in self.db.entries:
            if e.class_name and e.experiment and e.group and not _is_intro(e.experiment):
                courses[e.class_name][e.experiment].add(e.group)
                if e.weekday in ALL_WEEKDAYS:
                    weekday_votes[e.class_name][e.weekday] += 1
        # 待排任务并入总览：教秘要能在界面上看见"大纲里这几个项目还没排"
        for t in self.extra_tasks:
            if not t.class_name or not t.experiment or _is_intro(t.experiment):
                continue
            if looks_like_exam(t.experiment):
                continue
            courses[t.class_name][t.experiment].add(t.group or "1组")
            pending_units[t.class_name].add((t.experiment, t.group or "1组"))
            if t.weekday in ALL_WEEKDAYS:
                weekday_votes[t.class_name][t.weekday] += 1
        return [
            {
                "class_name": cls,
                "weekday": weekday_votes[cls].most_common(1)[0][0] if weekday_votes[cls] else "周四",
                # 星期分布（含周末）：「常上周一」原先只统计周一~周五，于是 42/46 节课
                # 都在周六/周日的班（24级电子信息工程1、2班）被显示成"常上周一"。
                "weekday_mix": dict(weekday_votes[cls].most_common()),
                "weekend_sessions": sum(v for k, v in weekday_votes[cls].items()
                                        if k in ("周六", "周日")),
                "projects": [
                    {"project": p, "groups": sorted(gs)}
                    for p, gs in sorted(projs.items())
                ],
                "total_units": sum(len(gs) for gs in projs.values()),
                # 其中有多少是"还没排过时间"的（大纲新增 / 清单里带来的）
                "pending_units": len(pending_units.get(cls, set())),
            }
            for cls, projs in sorted(courses.items())
        ]

    def solve(self, class_name: str, weekday: Optional[str] = None,
              start_week: int = 2, weeks: int = 10,
              time_slots: Optional[List[str]] = None,
              week_max_override: Optional[int] = None,
              max_time_seconds: float = 10.0) -> Dict:
        """并行排课求解：该课程所有 (项目, 组别) 单元 → (周次, 时段, 场地)

        `time_slots` / `week_max_override` 是教务给定的「课程总时间范围」：求解器只在
        这个范围里排，不再自己推断节次 / 把周次上限当成整张表。缺省时回退到
        「固定三段 567/678/91011 + 表内最后一周」的旧行为（向后兼容）。
        """
        # 教务给定的可用节次（缺省＝三段全集）
        slots = time_slots or TIME_SLOTS
        # 1. 收集单元（项目×组别，排除绪论）+ 历史教师 + 历史上课星期
        unit_teacher: Dict[tuple, str] = {}
        weekday_votes: Counter = Counter()
        for e in self.db.entries:
            if e.class_name != class_name:
                continue
            if e.weekday in ALL_WEEKDAYS:
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
        # 待排任务：与已排过的单元按 (项目, 组) 去重后并入 —— 这就是"从零排课"的入口。
        # 灰箱场景（教秘只给部分信息）下替用户做的每个默认决定都记进 graybox_notes，
        # 随结果返回 —— 排出来的课"为什么长这样"必须可解释，不能静默。
        graybox_notes: List[str] = []
        pending_count = 0
        for t in self._tasks_of(class_name):
            key = (t.experiment, t.group or "1组")
            if key in seen:
                continue
            seen.add(key)
            units.append({"project": t.experiment, "group": t.group or "1组"})
            pending_count += 1
            if t.teacher:
                unit_teacher.setdefault(key, t.teacher)
            elif not t.group:
                graybox_notes.append(f"「{t.experiment}」未指定学生组，按 1 组处理"
                                     f"（保守策略：与 1 组互斥排课，不会让学生双抓）")
        if not units:
            return {"success": False, "error": f"未找到 {class_name} 的实验项目/分组数据"}

        # 2. 上课星期：参数优先，否则取历史最常用（含周六/周日）
        wd = weekday or (weekday_votes.most_common(1)[0][0] if weekday_votes else "周四")
        if wd not in ALL_WEEKDAYS:
            wd = "周四"
        # 灰箱假设：星期是怎么定的，说清楚（教务没给、请求也没给时＝我们在推断）
        if not weekday:
            if weekday_votes:
                graybox_notes.append(
                    f"上课星期未显式给定，按该班历史最常用的 {wd}"
                    f"（{weekday_votes.most_common(1)[0][1]}/{sum(weekday_votes.values())} 次）")
            else:
                graybox_notes.append("上课星期未给定且无历史记录可推断，默认周四")

        # 场地：安排表里没有（例如只上传了大纲清单）时，退回主数据里写的候选场地 ——
        # 否则"没有可用场地"会直接把从零排课这条路堵死。
        rooms = self.db.get_all_rooms()
        if not rooms:
            rooms = sorted({t.room for t in self._tasks_of(class_name) if t.room})
        if not rooms:
            return {"success": False, "error": "没有可用场地（安排表里没有场地，主数据里也没写候选场地）"}

        # 灰箱假设：哪些单元连教师都没有（补全也救不回来的），如实报告而不是留空了事
        for u in units:
            if not unit_teacher.get((u["project"], u["group"])):
                graybox_notes.append(
                    f"「{u['project']}」{u['group']} 没有教师信息（清单与主数据都没有），"
                    f"结果中教师列为空")

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
            for ts in slots:
                if s.get("room"):
                    room_occupied.add((s["week"], ts, s["room"]))
                if s.get("teacher"):
                    teacher_occupied.add((s["week"], ts, s["teacher"]))

        # 4. 可排周次窗口 —— 按"这门课至少需要几周"推导，并且不越过本表覆盖的最后一个周次。
        #
        # 原先硬编码成 `(weeks, weeks + 4)`，默认 start_week=2 / weeks=10 ⇒ 最多 14 周
        # （第2–15周）。真实表里 17 个班有 2 个排不出来，而它们需要的是 15 周：
        #   23物本                 30 个单元 / 2 组                → 15 周
        #   24级电子信息工程1、2班   45 个单元 / 5 组 / 1 位教师(3 时段) → 15 周
        # 差的就是这 1 周，可错误文案写的是"场地/教师资源在该星期被占用过多"⇒
        # 教秘把 7 个星期按钮挨个点一遍，得到 7 个一模一样的失败。
        # 同时，`weeks+4` 也不看表里到底有几个周次：weeks=14 时重试 18 周，
        # 会排出第 17–19 周 —— 那是学校里根本不存在的周次。
        all_weeks = sorted({e.week for e in self.db.entries})
        # 只上传了大纲清单（表里一条已排记录都没有）时，不能拿"表里的最后一周"当上限 ——
        # 那样可用周数恒为 0，从零排课直接被自己堵死。退回一学期的默认跨度。
        if not all_weeks:
            all_weeks = list(range(1, DEFAULT_SEMESTER_WEEKS + 1))
        week_max = week_max_override if week_max_override else (all_weeks[-1] if all_weeks else start_week)
        max_avail = max(0, week_max - start_week + 1)
        # 灰箱假设：节次 / 周次上限如果没给定（教务没给、请求也没给），如实报告
        if time_slots is None:
            graybox_notes.append("节次范围未给定，默认 567/678/91011 三段全部可用"
                                 "（教务给了「课程总时间范围」后不会再出现本条）")
        if week_max_override is None:
            graybox_notes.append(f"周次上限未给定，按{'安排表最后一周' if all_weeks else '默认学期跨度'}"
                                 f"（第{week_max}周）为界")
        if max_avail <= 0:
            return {"success": False,
                    "error": (f"起始周第{start_week}周已超出本表覆盖范围"
                              f"（第{all_weeks[0]}–{week_max}周），请把起始周调早")}

        groups_n = len({u["group"] for u in units})
        n_u = len(units)
        teachers_n = len(set(unit_teacher.values())) or 1
        per_week = max(1, min(groups_n, len(slots) * teachers_n))
        need_weeks = -(-n_u // per_week)

        free_cells = sum(
            1
            for w in range(start_week, start_week + max_avail)
            for ts in slots
            for rm in rooms
            if (w, ts, rm) not in room_occupied
        )

        # 由小到大尝试：先按最小可行窗口，再放宽到整张表。窗口只影响"最多能排到第几周"。
        windows = sorted({
            min(max(weeks, 1), max_avail),
            min(max(need_weeks, weeks), max_avail),
            max_avail,
        })

        for attempt_weeks in windows:
            week_list = list(range(start_week, start_week + attempt_weeks))
            n_w, n_t, n_r = len(week_list), len(slots), len(rooms)
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
                    ts = slots[t]
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
                                        "time_slot": slots[t],
                                        "room": rooms[r],
                                        "teacher": unit_teacher.get((u["project"], u["group"]), ""),
                                    })
                schedule.sort(key=lambda s: (s["week"], slots.index(s["time_slot"]),
                                             s["group"], s["project"]))
                by_week = Counter(s["week"] for s in schedule)
                return {
                    "success": True,
                    "status": solver.StatusName(status),
                    "weekday": wd,
                    "total_units": len(units),
                    "scheduled": len(schedule),
                    # `weeks_used` 是"实际用到了几周"，`weeks_window` 才是"最多允许排到第几周"。
                    # 原先 `weeks_used` 填的是窗口宽度：界面写"18/18 个单元已排定，用 10 周"，
                    # 而沙盘上只有第 2–7 周有格子（实测 2024级制药工程）。
                    "weeks_used": len(by_week),
                    "weeks_window": attempt_weeks,
                    # 其中有多少单元是"大纲里新增、此前从没排过时间"的（从零排课的证据）
                    "pending_units": pending_count,
                    "schedule": schedule,
                    "by_week": {str(k): v for k, v in sorted(by_week.items())},
                    "note": f"并行排课（CP-SAT）：{len(units)} 个项目×组别单元一键排定，"
                            f"固定{wd}，组/场地/教师三重互斥"
                            + (f"；其中 {pending_count} 个是待排任务（大纲新增，此前没排过时间）"
                               if pending_count else ""),
                    # 灰箱补全：教秘只给了部分信息时，系统替他做的每个默认决定都在这里
                    "assumptions": graybox_notes,
                }
            if status != cp_model.INFEASIBLE and status != cp_model.UNKNOWN:
                break  # 别的状态（如 MODEL_INVALID）加周也没用，别白试

        # 所有窗口都不可行 —— 把原因说准。
        span = f"第{start_week}–{start_week + max_avail - 1}周（共 {max_avail} 周）"
        hist = "、".join(f"{k} {v} 节" for k, v in weekday_votes.most_common(3))
        tail = f"（该班历史课次：{hist}）" if hist else ""
        if need_weeks > max_avail:
            return {"success": False,
                    "error": (f"排不下：这门课有 {n_u} 个教学单元、{groups_n} 个学生组、"
                              f"{teachers_n} 位指导教师，每周最多排 {per_week} 个 ⇒ 至少需要 "
                              f"{need_weeks} 周；而本表只覆盖到第{week_max}周，从第{start_week}周起"
                              f"仅剩 {max_avail} 周。请把起始周调早，或减少同批排课的项目/组别。{tail}")}
        return {"success": False,
                "error": (f"排不下：需要 {n_u} 个可用的「周次×时段×场地」格子，"
                          f"而{wd} {span}内只剩 {free_cells} 个空闲教室格子"
                          f"（其余被其他班占用）。可换一个上课星期再试。{tail}")}
