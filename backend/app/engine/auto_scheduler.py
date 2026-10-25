"""排课自动生成 —— OR-Tools CP-SAT 求解器

本文件在第十三批里被整体重写。原实现有三个"自述与事实不符"的问题：

1. **没有周次维度**（R-37）。决策变量只有 (星期, 时段, 教室)，返回的方案里
   没有 `week` 字段 —— 教秘拿到一份"不知道属于哪一周"的课表。用真实表实测：
   同一份方案落到第 2 周有 7/15 条撞课，第 8–10 周有 10/15 条。
2. **不与现有安排表做任何冲突检查**（R-37）。约束只在"本次新生成的任务之间"
   生效，完全不知道学校已经排定的 433 条记录。note 却写着"教室/教师/学生三重不冲突"。
3. **容量是编的**（R-38）。房间容量按"名字里有没有'大'字"近似（120 / 否则 60），
   班级人数按"名字里有没有'、'或','"近似（85 / 否则 60）。真实表 10 间教室
   （A410…A516）没有一个含"大"字 ⇒ 全部按 60 人 ⇒ 所有合班任务永久不可排，
   实测 30 个任务里 15 个排不进，而原因统一写成"资源不足或约束过紧"。
   安排表里**根本没有容量字段**，这两个数字都是凭空造的。

现在：
  决策：任务 i → (第 w 周, 星期 d, 时段 t)；教室在求解后按可用清单分配
  硬约束（全部逐周生效）
    1. 每个任务最多排一次
    2. 同一周、同一星期、同一时段的**教室并发数**不超过该格可用教室数
       （等价于"同一教室同时段最多一个任务"，但不逐教室建变量）
    3. 同一教师、同一周、同一星期、同一时段最多一个任务
    4. 同一班级、同一周、同一星期、同一时段最多一个任务
    5. 与**现有安排表**对齐：该周该天该时段教室/教师已被占 → 该格不进候选域
  目标：最大化排入数量

为什么教室只做"并发数"约束：10 间教室对任务是可互换的，逐教室建变量会让
CP-SAT 面对 10! 量级的对称解，实测 30 个任务 5.2 万个布尔变量跑满 10 秒
连可行解都拿不到（status=UNKNOWN、solved=0）。改成"每格能塞几个班"的聚合
约束后剩 5.9 千个变量，1.3 秒拿到 OPTIMAL 30/30。教室在求解后按可用清单
顺序分配 —— 对教学任务而言，选哪间同类实验室没有业务差别。

口径说明
--------
- 时段按**真实时钟区间**判重叠，不按标签字符串相等：标签体系里历史出现过
  `78` / `15:50-18:05` 这类写法（R-34），别处已统一映射，这里再独立判一次，
  与 `scripts/audit_conflicts.py`（独立 oracle）同一口径。
- 时段无法识别的行（`db.skipped`）按**整日占用**处理，与
  `reschedule_agent.find_slots_with_reasons` 一致（R-35）。
- 绪论课不参与自动排课：这类记录的实验名本身就写着"教室已安排，请查系统"
  或"教室自己申请"，教室不在安排表里，硬排进实验室是错的。
  这条与 `parallel_scheduler._is_intro` 同一口径。
"""

import time
from collections import Counter, defaultdict
from typing import Dict, List, Optional, Set, Tuple

from ortools.sat.python import cp_model

from app.utils.excel_parser import ScheduleDatabase, looks_like_exam

# 可排课时段（工作日 3 个下午时段）
WEEKDAYS = ["周一", "周二", "周三", "周四", "周五"]
TIME_SLOTS = ["567", "678", "91011"]

# 时段标签 → 真实时钟区间（分钟）。`567` 与 `678` 在 15:45 首尾相接但**不重叠**。
_SLOT_SPAN = {
    "567": (13 * 60 + 30, 15 * 60 + 45),
    "678": (15 * 60 + 45, 18 * 60),
    "78": (15 * 60 + 50, 18 * 60 + 5),
    "91011": (18 * 60 + 30, 20 * 60 + 45),
    "MORNING": (9 * 60, 11 * 60 + 15),
}

# 教师字段可能是"吴琳、张三"这类多名共用
_TEACHER_SEPS = "、，,/ "


def _spans_overlap(a: str, b: str) -> bool:
    """两个时段标签的真实时钟区间是否相交。未知标签一律视为"整天占用"。"""
    sa, sb = _SLOT_SPAN.get(a), _SLOT_SPAN.get(b)
    if sa is None or sb is None:
        return True
    return max(sa[0], sb[0]) < min(sa[1], sb[1])


def _teacher_names(raw: Optional[str]) -> List[str]:
    """把"吴琳、张三"切成姓名列表。不按子串匹配（"李明"不该命中"李明华"）。"""
    if not raw:
        return []
    out, buf = set(), raw
    for sep in _TEACHER_SEPS:
        buf = buf.replace(sep, "\n")
    for part in buf.split("\n"):
        p = part.strip()
        if p:
            out.add(p)
    return sorted(out)


def _is_intro(project: str) -> bool:
    """绪论课已单独安排（教室已定/自申请），不参与自动排课"""
    return "绪论" in (project or "")


class AutoScheduler:
    """排课自动生成器（CP-SAT 求解，带周次维度）"""

    def __init__(self, db: ScheduleDatabase,
                 extra_tasks: Optional[List] = None):
        self.db = db
        # 待排任务（PendingTask）：没排过时间的教学任务，与已排记录并列的一类输入
        self.extra_tasks = list(extra_tasks or [])

    # ------------------------------------------------------------------ 取数
    def _get_rooms(self) -> List[str]:
        return self.db.get_all_rooms()

    def _get_course_sections(self) -> Tuple[List[Dict], int]:
        """提取需要排课的教学任务（教师 + 课程 + 班级）。

        返回 (任务列表, 被排除的绪论任务数)。

        ⚠️ 任务源除了"已排过的课表行"，还包括**待排任务**（表里没排时间的行 + 上传的
        大纲清单）：否则一键自动排课永远只能重排去年，排不了大纲里新增的实验项目。
        """
        sections, seen = [], set()
        excluded_intro = 0
        for e in self.db.entries:
            if _is_intro(e.experiment or ""):
                key = (e.teacher, e.experiment, e.class_name)
                if key not in seen:
                    seen.add(key)
                    excluded_intro += 1
                continue
            key = (e.teacher, e.experiment, e.class_name)
            if key in seen:
                continue
            seen.add(key)
            sections.append({
                "teacher": e.teacher or "",
                "course": e.experiment or "未知实验",
                "class_name": e.class_name or "",
            })
        for t in (self.extra_tasks or []):
            if _is_intro(t.experiment or "") or looks_like_exam(t.experiment or ""):
                continue
            key = (t.teacher, t.experiment, t.class_name)
            if key in seen:
                continue
            seen.add(key)
            sections.append({
                "teacher": t.teacher or "",
                "course": t.experiment or "未知实验",
                "class_name": t.class_name or "",
            })
        return sections, excluded_intro

    def _get_weeks(self) -> List[int]:
        """表中出现的周次区间（不用 `get_week_range()`：它对未加载/空表返回 (0,0)，
        而元组是真值，很容被当成"有效范围"继续用 —— R-32 踩过这个坑）。"""
        ws = sorted({int(e.week) for e in self.db.entries
                     if getattr(e, "week", None) and int(e.week) > 0})
        if not ws:
            return [1]
        return list(range(ws[0], ws[-1] + 1))

    def _occupancy(self) -> Tuple[Set[Tuple], Set[Tuple]]:
        """现有安排表已经占住的格子。

        返回 (room_busy, teacher_busy)：
            room_busy    = {(week, weekday, time_slot, room), ...}
            teacher_busy = {(week, weekday, time_slot, teacher), ...}
        """
        room_busy: Set[Tuple] = set()
        teacher_busy: Set[Tuple] = set()
        for e in self.db.entries:
            blocks = list(getattr(e, "blocked", None) or [])
            for ts in TIME_SLOTS:
                # blocked 是解析阶段算出的"真实占住的节次"（跨节次写法会有两个）；
                # _spans_overlap 兜住历史缓存里只有主标签的条目。
                if ts not in blocks and not _spans_overlap(e.time_slot, ts):
                    continue
                if e.room:
                    room_busy.add((int(e.week), e.weekday, ts, e.room))
                for tn in _teacher_names(e.teacher):
                    teacher_busy.add((int(e.week), e.weekday, ts, tn))

        # 时段看不懂、已被剔除的行：只知道"这天这间教室/这个人在上课"，
        # 不知道具体节次 ⇒ 三个时段全按占用算（宁可少推荐，不可推荐到占用中）。
        for s in (getattr(self.db, "skipped", None) or []):
            try:
                wk, wd = int(s.get("week")), s.get("weekday")
            except (TypeError, ValueError):
                continue
            if not wd:
                continue
            for ts in TIME_SLOTS:
                if s.get("room"):
                    room_busy.add((wk, wd, ts, s["room"]))
                for tn in _teacher_names(s.get("teacher")):
                    teacher_busy.add((wk, wd, ts, tn))
        return room_busy, teacher_busy

    # ------------------------------------------------------------------ 求解
    def solve(self, max_sections: int = 30, sections_override: Optional[List[Dict]] = None,
              extra_room_busy: Optional[Set[Tuple]] = None,
              extra_teacher_busy: Optional[Set[Tuple]] = None,
              extra_class_busy: Optional[Set[Tuple]] = None,
              flatten_seconds: float = 4.0) -> Dict:
        sections, intro_excluded = self._get_course_sections()
        if not sections:
            return {"success": False, "error": "没有可排课的教学任务"}

        rooms = self._get_rooms()
        if not rooms:
            return {"success": False, "error": "没有可用教室"}

        total_all = len(sections)
        weeks = self._get_weeks()
        room_busy, teacher_busy = self._occupancy()
        # 分片求解（solve_all）时把前面片已排定的格子回填进来 —— 否则第 2 片会把
        # 教室/教师/班级排到第 1 片刚占的位置上（片与片之间互相不可见）。
        room_busy |= set(extra_room_busy or ())
        teacher_busy |= set(extra_teacher_busy or ())
        class_busy = set(extra_class_busy or ())

        # 限制规模保证求解速度（全表 171 个任务 × 16 周 × 15 时段过重，单次会超时）
        sections = sections_override if sections_override is not None \
            else sections[:max(1, int(max_sections))]

        # ---- 候选域：先按"现有表占用"剪掉不可用的格子，模型只建可行格 ----
        # `free_rooms[(w,d,t)]` = 该格当前还空着的教室（顺序稳定，用于求解后分配）
        free_rooms: Dict[Tuple, List[str]] = {}
        for w in weeks:
            for d in WEEKDAYS:
                for t in TIME_SLOTS:
                    idle = []
                    for r in rooms:
                        if (w, d, t, r) in room_busy:
                            continue
                        idle.append(r)
                    free_rooms[(w, d, t)] = idle

        domains: Dict[int, List[Tuple]] = {}
        empty_reason: Dict[int, str] = {}
        for i, sec in enumerate(sections):
            cells = []
            for w in weeks:
                for d in WEEKDAYS:
                    for t in TIME_SLOTS:
                        if not free_rooms[(w, d, t)]:
                            continue
                        if sec.get("teacher") and (w, d, t, sec["teacher"]) in teacher_busy:
                            continue
                        # 分片求解时，同一班级在前面的片里已排定的格子必须避开
                        if sec["class_name"] and (w, d, t, sec["class_name"]) in class_busy:
                            continue
                        cells.append((w, d, t))
            domains[i] = cells
            if not cells:
                empty_reason[i] = (
                    "整学期（第%d–%d周）该教师的所有时段都已被现有安排表占满" % (weeks[0], weeks[-1])
                    if sec["teacher"] else
                    "整学期（第%d–%d周）所有教室在可排时段都已被占满" % (weeks[0], weeks[-1])
                )

        model = cp_model.CpModel()
        x: Dict[Tuple, cp_model.IntVar] = {}
        cell_groups = defaultdict(list)      # (w,d,t) → 全部落在该格的任务变量
        teacher_groups = defaultdict(list)
        class_groups = defaultdict(list)
        for i, sec in enumerate(sections):
            for (w, d, t) in domains[i]:
                v = model.NewBoolVar(f"x_{i}_{w}_{d}_{t}")
                x[(i, w, d, t)] = v
                cell_groups[(w, d, t)].append(v)
                if sec["teacher"]:
                    teacher_groups[(w, d, t, sec["teacher"])].append(v)
                if sec["class_name"]:
                    class_groups[(w, d, t, sec["class_name"])].append(v)

        # 每个任务最多排一次
        for i in range(len(sections)):
            vs = [x[(i,) + cell] for cell in domains[i]]
            if vs:
                model.Add(sum(vs) <= 1)

        # 教室并发上限：同一格最多塞 len(free_rooms) 个任务（见文件头"为什么"）
        for cell, vs in cell_groups.items():
            cap = len(free_rooms[cell])
            if len(vs) > cap:
                model.Add(sum(vs) <= cap)

        # 教师 / 班级 互斥（同一周、同一星期、同一时段）
        for groups in (teacher_groups, class_groups):
            for vs in groups.values():
                if len(vs) > 1:
                    model.Add(sum(vs) <= 1)

        model.Maximize(sum(x.values()))

        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = max(1.0, float(flatten_seconds) + 2.0)
        status = solver.Solve(model)
        ok = status in (cp_model.OPTIMAL, cp_model.FEASIBLE)

        # 第二步：数量已经拉满后，把周次摊平。
        #
        # 只做"最大化排入数量"的话，CP-SAT 会把 30 个任务全塞进前 3–4 周
        # （第 1 周 12 个、后面几周空着）—— 数量没错，但这不是一份能交给教秘的
        # 学期初稿：真实表每周 27–69 条，是摊开的。所以补一个 min-max：
        # 固定住已求得的最大数量 K，再最小化"单周任务数"的上界 z。
        chosen: Dict[int, Tuple] = {}
        if ok and x:
            total_placed = round(solver.ObjectiveValue())
            if total_placed:
                model.Add(sum(x.values()) == total_placed)
                z = model.NewIntVar(0, total_placed, "max_week_load")
                for w in weeks:
                    in_week = [v for (i, ww, d, t), v in x.items() if ww == w]
                    if in_week:
                        model.Add(z >= sum(in_week))
                model.Minimize(z)

                solver2 = cp_model.CpSolver()
                solver2.parameters.max_time_in_seconds = max(0.5, float(flatten_seconds))
                status2 = solver2.Solve(model)
                if status2 in (cp_model.OPTIMAL, cp_model.FEASIBLE):
                    solver, status = solver2, status2
                # 第二步超时/失败就退回第一步的解（`solver` 仍指第一步的结果）
                for i in range(len(sections)):
                    for cell in domains[i]:
                        if solver.Value(x[(i,) + cell]) == 1:
                            chosen[i] = cell
                            break
            else:
                ok = False

        schedule: List[Dict] = []
        if chosen:
            # 按格分配具体教室：同格任务数已被约束 ≤ 该格空教室数，按清单顺序取即可。
            picked: Dict[Tuple, List[int]] = defaultdict(list)
            for i, cell in chosen.items():
                picked[cell].append(i)
            for cell in sorted(picked):
                idxs = sorted(picked[cell],
                              key=lambda i: (sections[i]["class_name"], sections[i]["teacher"]))
                for k, i in enumerate(idxs):
                    w, d, t = cell
                    sec = sections[i]
                    schedule.append({
                        "week": w,
                        "weekday": d,
                        "time_slot": t,
                        "room": free_rooms[cell][k],
                        "teacher": sec["teacher"],
                        "course": sec["course"],
                        "class_name": sec["class_name"],
                    })
        schedule.sort(key=lambda s: (s["week"], WEEKDAYS.index(s["weekday"]),
                                     TIME_SLOTS.index(s["time_slot"]),
                                     s["class_name"], s["teacher"]))

        placed = {(s["teacher"], s["course"], s["class_name"]) for s in schedule}
        conflicts = []
        for i, sec in enumerate(sections):
            if (sec["teacher"], sec["course"], sec["class_name"]) in placed:
                continue
            conflicts.append({
                "teacher": sec["teacher"],
                "course": sec["course"],
                "class_name": sec["class_name"],
                "reason": empty_reason.get(i) or "CP-SAT 在时限内未排入（资源不足或约束过紧）",
            })

        return {
            "success": ok and bool(schedule),
            "status": solver.StatusName(status),
            "weeks": [weeks[0], weeks[-1]],
            "total_sections": len(sections),          # 本次参与求解的任务数
            "total_sections_all": total_all,          # 全表任务数（未截断）
            "intro_excluded": intro_excluded,         # 绪论课（不参与自动排课）
            "solved": len(schedule),
            "conflicts": conflicts,
            "schedule": schedule,
            "capacity_checked": False,
            "note": (
                "OR-Tools CP-SAT 约束求解：教室/教师/班级三重互斥 + 与现有安排表逐周对齐"
                + "；容量未校验（安排表无容量字段）"
            ),
        }

    # ------------------------------------------------------------ 确定性兜底
    def _greedy_fill(self, sections, room_busy, teacher_busy, class_busy) -> List[Dict]:
        """把 CP-SAT 没排上的单元按确定性贪心补齐（只排"确实有位置"的）。

        为什么需要：CP-SAT 在固定时限下**不稳定** —— 同一片数据这次 60/60、
        下次 UNKNOWN 0（实测：171 个任务只排定 51 个，前两片全 0，而每片单独看
        都"成功"）。演示/交付不能接受这种抖动：覆盖优先，质量其次。
        贪心只做一件事：按"当周负载最轻"选格子，遵守场地容量/教师/班级/同组每周
        一个实验四条硬约束；排不下就如实留空（不编造）。
        """
        rooms_all = self._get_rooms()
        weeks = self._get_weeks()
        rows: List[Dict] = []
        week_load = Counter()
        group_week = set()
        for sec in sections:
            best = None
            for w in weeks:
                for d in WEEKDAYS:
                    for t in TIME_SLOTS:
                        if sec.get("teacher") and (w, d, t, sec["teacher"]) in teacher_busy:
                            continue
                        if sec["class_name"] and (w, d, t, sec["class_name"]) in class_busy:
                            continue
                        if (sec.get("class_name"), sec.get("group"), w) in group_week:
                            continue
                        free = [r for r in rooms_all if (w, d, t, r) not in room_busy]
                        if not free:
                            continue
                        score = (week_load[w], w, WEEKDAYS.index(d), TIME_SLOTS.index(t))
                        if best is None or score < best[0]:
                            best = (score, w, d, t, free[0])
            if not best:
                continue
            _, w, d, t, room = best
            rows.append({
                "week": w, "weekday": d, "time_slot": t, "room": room,
                "teacher": sec["teacher"], "course": sec["course"],
                "class_name": sec["class_name"],
            })
            week_load[w] += 1
            group_week.add((sec.get("class_name"), sec.get("group"), w))
            room_busy.add((w, d, t, room))
            if sec.get("teacher"):
                teacher_busy.add((w, d, t, sec["teacher"]))
            if sec.get("class_name"):
                class_busy.add((w, d, t, sec["class_name"]))
        return rows

    # ------------------------------------------------------------ 全校一键（分片）
    def solve_all(self, chunk_size: int = 60, max_chunks: int = 40,
                  flatten_seconds: float = 5.0) -> Dict:
        """全表一键排课：**全部**教学任务而不是前 N 个。

        为什么必须分片：171 个任务一次性建模（16 周 × 15 时段 + 摊平目标）实测
        6 秒时限内连可行解都拿不到（status=UNKNOWN）；60 个一片 7 秒可解。
        分片之间不是独立的 —— 前一片排定的 (周,星期,时段) 上的教室/教师/班级
        必须回填成后一片的占用，否则两片会互相"看不见"而重排同一格。

        ⚠️ `flatten_seconds=5.0`（一阶段时限 7 秒）不是拍脑袋，是照着上面那句
        实测证据设的：曾用 3.0（一阶段 5 秒），比"7 秒可解"的实测点还紧 2 秒，
        真实表第 2 片（带着第 1 片回填的占用，域更小更难）在 5 秒边界上时灵时不灵
        —— 实测单独跑 5 次里 4 次 UNKNOWN，教秘点一次"全校初稿"有 4/5 概率拿到
        111/171 的 PARTIAL。这不是求解器退化，是时限给错了。
        """
        sections_all, intro_excluded = self._get_course_sections()
        if not sections_all:
            return {"success": False, "error": "没有可排课的教学任务"}
        if not self._get_rooms():
            return {"success": False, "error": "没有可用教室"}

        chunk_size = max(1, int(chunk_size))
        chunks = [sections_all[i:i + chunk_size]
                  for i in range(0, len(sections_all), chunk_size)][:max_chunks]

        room_busy: Set[Tuple] = set()
        teacher_busy: Set[Tuple] = set()
        class_busy: Set[Tuple] = set()
        schedule: List[Dict] = []
        conflicts: List[Dict] = []
        chunk_reports: List[Dict] = []
        statuses: List[str] = []
        t_start = time.time()

        def _place(sub, seconds):
            """求一个子片并把结果并入全局占用；返回 (rows, conflicts, status)。"""
            r = self.solve(
                sections_override=sub,
                extra_room_busy=room_busy,
                extra_teacher_busy=teacher_busy,
                extra_class_busy=class_busy,
                flatten_seconds=seconds,
            )
            rows = r.get("schedule") or []
            for row in rows:
                key = (row["week"], row["weekday"], row["time_slot"])
                room_busy.add(key + (row["room"],))
                if row.get("teacher"):
                    teacher_busy.add(key + (row["teacher"],))
                if row.get("class_name"):
                    class_busy.add(key + (row["class_name"],))
            return rows, (r.get("conflicts") or []), \
                str(r.get("status") or ("FAILED" if not r.get("success") else "FEASIBLE"))

        for idx, chunk in enumerate(chunks, 1):
            t0 = time.time()
            rows, cfs, status = _place(chunk, flatten_seconds)
            # CP-SAT 只负责"质量"（周次摊平、早周优先）；覆盖由确定性贪心兜底。
            # 求解失败/漏排时按当周负载最轻补齐 —— 演示不能出现"171 个只排 51 个"。
            placed_keys = {(r["teacher"], r["course"], r["class_name"]) for r in rows}
            missing = [x for x in chunk
                       if (x["teacher"], x["course"], x["class_name"]) not in placed_keys]
            filled = 0
            if missing:
                extra_rows = self._greedy_fill(missing, room_busy, teacher_busy, class_busy)
                filled = len(extra_rows)
                rows = list(rows) + extra_rows
                if filled:
                    status = "FEASIBLE" if status in ("OPTIMAL", "FEASIBLE") else status
            retried = filled > 0

            schedule.extend(rows)
            conflicts.extend(cfs)
            statuses.append(status)
            chunk_reports.append({
                "index": idx, "size": len(chunk), "solved": len(rows),
                "status": status, "seconds": round(time.time() - t0, 1),
                "retried": retried,
            })

        schedule.sort(key=lambda s: (s["week"], WEEKDAYS.index(s["weekday"]),
                                     TIME_SLOTS.index(s["time_slot"]),
                                     s["class_name"], s["teacher"]))
        overall = ("OPTIMAL" if statuses and all(s == "OPTIMAL" for s in statuses)
                   else "FEASIBLE" if all(s in ("OPTIMAL", "FEASIBLE") for s in statuses)
                   else "PARTIAL")
        if not schedule:
            return {"success": False, "status": overall, "error": "分片求解未产出任何排课结果",
                    "chunks": chunk_reports, "conflicts": conflicts}
        return {
            "success": bool(schedule),
            "status": overall,
            "weeks": self._get_weeks()[:1] + self._get_weeks()[-1:],
            "total_sections": len(sections_all),
            "total_sections_all": len(sections_all),
            "intro_excluded": intro_excluded,
            "solved": len(schedule),
            "conflicts": conflicts,
            "schedule": schedule,
            "chunks": chunk_reports,
            "chunked": len(chunks) > 1,
            "elapsed_s": round(time.time() - t_start, 1),
            "capacity_checked": False,
            "note": (
                f"全校一键排课（分片 CP-SAT）：{len(sections_all)} 个教学任务分 {len(chunks)} 片求解，"
                f"片间回填教室/教师/班级占用；教室/教师/班级三重互斥 + 与现有安排表逐周对齐"
                + "；容量未校验（安排表无容量字段）"
            ),
        }

    # ------------------------------------------------------------------ 文案
    def summary(self, result: Dict) -> str:
        """生成可读的排课方案摘要"""
        if not result.get("success"):
            return result.get("error", "排课失败")

        lo, hi = result["weeks"]
        lines = [
            f"📋 自动排课结果（CP-SAT）：成功 {result['solved']}/{result['total_sections']} 个教学任务"
            f"（第{lo}–{hi}周）",
        ]
        unchecked = []
        if not result.get("capacity_checked"):
            unchecked.append("教室容量（安排表无容量字段）")
        if unchecked:
            lines.append("⚠️ 未校验：" + "、".join(unchecked))

        if result["conflicts"]:
            lines.append(f"⚠️ 未排上 {len(result['conflicts'])} 个：")
            for c in result["conflicts"][:8]:
                lines.append(f"  - {c['teacher']} {c['course']}（{c['class_name']}）：{c['reason']}")
            if len(result["conflicts"]) > 8:
                lines.append(f"  …… 另有 {len(result['conflicts']) - 8} 个")

        by_week: Dict[int, List[Dict]] = defaultdict(list)
        for s in result["schedule"]:
            by_week[s["week"]].append(s)

        # 全文列出 171 条会把教秘淹没（界面里是一个 96px 高的滚动框）：
        # 大方案只给每周条数 + 每周前几行示例，完整明细走「导出初稿 Excel」。
        big = len(result["schedule"]) > 40
        for week in sorted(by_week):
            rows = by_week[week]
            if big:
                lines.append(f"\n第{week}周：{len(rows)} 个教学任务")
                for s in rows[:3]:
                    lines.append(
                        f"  {s['weekday']} {s['time_slot']} {s['room']} | "
                        f"{s['teacher']} {s['course']}（{s['class_name']}）")
                if len(rows) > 3:
                    lines.append(f"  …… 另有 {len(rows) - 3} 个（完整明细请点「导出初稿 Excel」）")
                continue
            lines.append(f"\n第{week}周：")
            for s in rows:
                lines.append(
                    f"  {s['weekday']} {s['time_slot']} {s['room']} | "
                    f"{s['teacher']} {s['course']}（{s['class_name']}）"
                )
        return "\n".join(lines)
