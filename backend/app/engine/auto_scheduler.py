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
from typing import AbstractSet, Dict, List, Optional, Set, Tuple

from ortools.sat.python import cp_model

from app.utils.excel_parser import ScheduleDatabase, looks_like_exam
from app.engine.solver_factory import new_solver

# 可排课时段（工作日 3 个下午时段）
WEEKDAYS = ["周一", "周二", "周三", "周四", "周五"]
TIME_SLOTS = ["567", "678", "91011"]

# 所有 note 共用的范围声明（R37）。
#
# 周末已有课**仍记为占用**（`_busy` 按 `e.weekday` 收集，实测与周末课撞教室 0 次），
# 但周末从不作为候选时段。这两件事的区别必须写出来：
#   · 不写 → 读者以为"这周的空档都考虑过了"（实际少考虑了两天）
#   · 写了 → 读者知道"周末得自己去排，或改这一句的范围"
WEEKDAY_ONLY_NOTE = "；**只在工作日（周一至周五）排课，周末不作候选**（周末已有课仍计入占用）"

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
        """提取需要排课的教学任务（教师 + 课程 + 班级），并**带上该段的全部学生组**。

        返回 (任务列表, 被排除的绪论任务数)。

        ⚠️ 任务源除了"已排过的课表行"，还包括**待排任务**（表里没排时间的行 + 上传的
        大纲清单）：否则一键自动排课永远只能重排去年，排不了大纲里新增的实验项目。

        为什么 section 里要带 `groups`（2026-10-28 修）
        ----------------------------------------------
        去重键仍是 (教师, 项目, 班级)，所以任务数还是 171 段 —— 但**组别不能再丢**。
        真实表 433/433 条组别全有，而一个教学段的 k 个组在原表里是**同一周、同一间
        教室、连续时段**各占一格（实测 171/171 段同教室；5 组的例子铺到周六周日）。
        此前这里把 `group` 丢在门外，导致两个后果：
          ① 产出没有 group ⇒ 初稿根本落不进安排表；
          ② 171 段把 **410 个组课时**压成 171 个，`171/171 全覆盖` 覆盖的是压小的口径。
        现在 section 记下 groups，排定后由 `_expand_groups` 展开成 k 行（段级求解、
        组级产出），求解规模与耗时不变。
        """
        sections: List[Dict] = []
        seen: Dict[Tuple, Dict] = {}
        intro_seen: Set[Tuple] = set()
        excluded_intro = 0

        def _add(teacher: str, experiment: str, class_name: str, group: str) -> None:
            key = (teacher, experiment, class_name)
            sec = seen.get(key)
            if sec is None:
                sec = {"teacher": teacher, "course": experiment or "未知实验",
                       "class_name": class_name, "groups": []}
                seen[key] = sec
                sections.append(sec)
            g = (group or "").strip()
            if g and g not in sec["groups"]:
                sec["groups"].append(g)

        for e in self.db.entries:
            if _is_intro(e.experiment or ""):
                key = (e.teacher, e.experiment, e.class_name)
                if key not in intro_seen:
                    intro_seen.add(key)
                    excluded_intro += 1
                continue
            _add(e.teacher or "", e.experiment or "", e.class_name or "", e.group or "")
        for t in (self.extra_tasks or []):
            if _is_intro(t.experiment or "") or looks_like_exam(t.experiment or ""):
                continue
            _add(t.teacher or "", t.experiment or "", t.class_name or "", t.group or "")
        return sections, excluded_intro

    def _get_weeks(self) -> List[int]:
        """表中出现的周次区间（不用 `get_week_range()`：它对未加载/空表返回 (0,0)，
        而元组是真值，很容被当成"有效范围"继续用 —— R-32 踩过这个坑）。"""
        ws = sorted({int(e.week) for e in self.db.entries
                     if getattr(e, "week", None) and int(e.week) > 0})
        if not ws:
            return [1]
        return list(range(ws[0], ws[-1] + 1))

    def _occupancy(self) -> Tuple[Set[Tuple], Set[Tuple], Set[Tuple]]:
        """现有安排表已经占住的格子。

        返回 (room_busy, teacher_busy, class_busy)：
            room_busy    = {(week, weekday, time_slot, room), ...}
            teacher_busy = {(week, weekday, time_slot, teacher), ...}
            class_busy   = {(week, weekday, time_slot, class), ...} —— **只收不参与重排的行**

        为什么 class_busy 只收"不参与重排"的行（2026-10-28 修）
        ------------------------------------------------------
        自动排课的产出是**替换**那 410 个非绪论单元，所以它当然可以占用旧单元原来的格子；
        但绪论课（教室已另行安排、不参与重排）**必须原样保留**，新方案不能排到
        "这个班那节课正在上绪论"的格子上。
        此前这里只返回教室与教师两个维度，班级维度只统计了同批任务之间，
        于是新方案会撞上绪论课 —— 实测真实表 3 处。这不是"多算"，是漏算。
        """
        room_busy: Set[Tuple] = set()
        teacher_busy: Set[Tuple] = set()
        class_busy: Set[Tuple] = set()
        for e in self.db.entries:
            keeps = _is_intro(e.experiment or "") or looks_like_exam(e.experiment or "")
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
                if keeps and e.class_name:
                    class_busy.add((int(e.week), e.weekday, ts, e.class_name))

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
        return room_busy, teacher_busy, class_busy

    def _kept_busy(self) -> Tuple[Set[Tuple], Set[Tuple], Set[Tuple]]:
        """只收"不参与重排"的既有课（绪论/考试）在**三个维度**上的占用格。

        为什么组展开要用这个、而不是 `_occupancy()`
        --------------------------------------------
        `_occupancy()` 的 room/teacher 收**全部**既有行，供求解器建候选域用
        （求解器只在原本空着的格子上落段）。但**组展开**不一样：它的产出是替换
        那 410 个非绪论单元，所以非绪论旧格的教室/教师/班级**都可以复用**
        （旧课被替换走了）；唯独绪论/考试（`_is_intro` / `looks_like_exam`）
        必须原样保留。
        2026-10-28 实测：拿 `_occupancy()` 的 room/teacher（全量）去挡展开，
        会把可复用的非绪论旧格也挡掉 —— 覆盖从 410/410 掉到 365/410、45 处缺口。
        只挡 kept 后既消除 5 处绪论碰撞、又保住覆盖。
        """
        kr: Set[Tuple] = set()
        kt: Set[Tuple] = set()
        kc: Set[Tuple] = set()
        for e in self.db.entries:
            if not (_is_intro(e.experiment or "") or looks_like_exam(e.experiment or "")):
                continue
            blocks = list(getattr(e, "blocked", None) or [])
            for ts in TIME_SLOTS:
                if ts not in blocks and not _spans_overlap(e.time_slot, ts):
                    continue
                if e.room:
                    kr.add((int(e.week), e.weekday, ts, e.room))
                for tn in _teacher_names(e.teacher):
                    kt.add((int(e.week), e.weekday, ts, tn))
                if e.class_name:
                    kc.add((int(e.week), e.weekday, ts, e.class_name))
        return kr, kt, kc

    # ------------------------------------------------------------------ 求解
    def solve(self, max_sections: int = 30, sections_override: Optional[List[Dict]] = None,
              extra_room_busy: Optional[Set[Tuple]] = None,
              extra_teacher_busy: Optional[Set[Tuple]] = None,
              extra_class_busy: Optional[Set[Tuple]] = None,
              flatten_seconds: float = 4.0, expand: bool = True) -> Dict:
        sections, intro_excluded = self._get_course_sections()
        if not sections:
            return {"success": False, "error": "没有可排课的教学任务"}

        rooms = self._get_rooms()
        if not rooms:
            return {"success": False, "error": "没有可用教室"}

        total_all = len(sections)
        weeks = self._get_weeks()
        room_busy, teacher_busy, kept_class_busy = self._occupancy()
        # 分片求解（solve_all）时把前面片已排定的格子回填进来 —— 否则第 2 片会把
        # 教室/教师/班级排到第 1 片刚占的位置上（片与片之间互相不可见）。
        room_busy |= set(extra_room_busy or ())
        teacher_busy |= set(extra_teacher_busy or ())
        # 班级维度 = 不参与重排的既有课（绪论/考试，必须原样保留）+ 前面片已排定的格子
        class_busy = kept_class_busy | set(extra_class_busy or ())

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

        # worker 数受 SOLVER_WORKERS 控制：内存随 worker 线性增长，而 ortools
        # 内存不足时是 `bad allocation` 直接 abort（拦不住），见 solver_factory
        solver = new_solver(max_time_seconds=max(1.0, float(flatten_seconds) + 2.0))
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

                solver2 = new_solver(max_time_seconds=max(0.5, float(flatten_seconds)))
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

        # ---- 确定性兜底（R-75）：顶层单批调用拿不到解时，不许直接判失败 ----
        # `solve_all` 早就定了取向"覆盖优先、质量其次"（见 `_greedy_fill` 的理由），
        # 但**单批**这条路一直没有兜底：机器一忙，CP-SAT 在时限内 UNKNOWN ⇒
        # `success=False`，「取前 N 段」就整个失败（实测：CPU 饱和时
        # `test_real_table_plan_has_zero_collisions_with_existing_entries` 必红）。
        # 约束集与上面的候选域**完全一致**（教室/教师 = 全量既有占用，班级 = 仅 kept），
        # 所以兜底不会比 CP-SAT 多占任何一格 —— 不是放松约束，只是把"求解"换成
        # "按同样约束确定性找格子"。
        fallback_used = False
        if expand and not schedule and sections:
            kr, kt, kc = self._kept_busy()
            greedy = self._greedy_fill(sections, set(room_busy), set(teacher_busy),
                                       set(class_busy), kr, kt, kc)
            if greedy:
                schedule = greedy
                schedule.sort(key=lambda s: (s["week"], WEEKDAYS.index(s["weekday"]),
                                             TIME_SLOTS.index(s["time_slot"]),
                                             s["class_name"], s["teacher"]))
                fallback_used = True

        # ---- 段 → 组课时（R-72）----
        # 求解按段建模（171 个变量、速度可控），但产出必须是「班级 × 项目 × 组」，
        # 否则既落不了地、又把 410 个组课时压成 171 个。
        total_groups = sum(len(s.get("groups") or [""]) for s in sections)
        gaps: List[Dict] = []
        if expand and schedule:
            groups_of = {(s["teacher"], s["course"], s["class_name"]):
                         (s.get("groups") or [""]) for s in sections}
            occ_rooms, occ_teachers, occ_kept_class = self._occupancy()
            # kept_* = 既有占用（含 extra 回填），**只读**：展开时"释放自己那一格"
            # 只允许释放本批自己的占用，绝不能把既有占用一起抹掉（R-74）。
            kept_r = occ_rooms | set(extra_room_busy or ())
            kept_t = occ_teachers | set(extra_teacher_busy or ())
            # 班级维度同样要并入"不参与重排的既有课"（绪论/考试），与 solve() 口径一致
            kept_c = occ_kept_class | set(extra_class_busy or ())
            ex_rooms, ex_teachers, ex_class = set(kept_r), set(kept_t), set(kept_c)
            for r in schedule:                      # 预置本批自己的占用（展开时逐段释放）
                cell = (r["week"], r["weekday"], r["time_slot"])
                ex_rooms.add(cell + (r["room"],))
                if r.get("teacher"):
                    ex_teachers.add(cell + (r["teacher"],))
                if r.get("class_name"):
                    ex_class.add(cell + (r["class_name"],))
            schedule, gaps, total_groups = self._expand_groups(
                schedule, groups_of, ex_rooms, ex_teachers, ex_class,
                kept_r, kept_t, kept_c)

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
            # 兜底出来的方案同样是可行解（约束集与候选域一致），不能因为"CP-SAT 超时"
            # 就对外报失败 —— 那会让机器一忙，教秘点一次就得到一句"排课失败"（R-75）。
            "success": bool(schedule) and (ok or fallback_used),
            "status": solver.StatusName(status),
            "fallback": "greedy" if fallback_used else None,
            "weeks": [weeks[0], weeks[-1]],
            "total_sections": len(sections),          # 本次参与求解的**段**数
            "total_sections_all": total_all,          # 全表段数（未截断）
            "intro_excluded": intro_excluded,         # 绪论课（不参与自动排课）
            # solved = 排定的**段**数（口径与旧版一致）；组课时另算
            "solved": len({(s["teacher"], s["course"], s["class_name"]) for s in schedule}),
            "solved_groups": len(schedule),           # 排定的**组课时**数（= 产出行数）
            "total_groups": total_groups,             # 应排的组课时总数
            "group_gaps": gaps,                       # 同周连续格不够而没排进的组（如实报）
            "conflicts": conflicts,
            "schedule": schedule,
            "capacity_checked": False,
            "note": (
                "OR-Tools CP-SAT 约束求解：教室/教师/班级三重互斥 + 与现有安排表逐周对齐"
                # R37：必须说清搜索范围。周末已有课**仍算占用**（不会被撞上），
                # 但周末从不作为候选 —— 它不是"排不下"，是"没参与"。
                # 不写这句，读者会以为这周的空档都考虑过了。
                + WEEKDAY_ONLY_NOTE
                + "；容量未校验（安排表无容量字段）"
            ),
        }

    # ------------------------------------------------------------ 确定性兜底
    def _greedy_fill(self, sections, room_busy, teacher_busy, class_busy,
                     kept_room: AbstractSet[Tuple] = frozenset(),
                     kept_teacher: AbstractSet[Tuple] = frozenset(),
                     kept_class: AbstractSet[Tuple] = frozenset()) -> List[Dict]:
        """把 CP-SAT 没排上的单元按确定性贪心补齐（只排"确实有位置"的）。

        为什么需要：CP-SAT 在固定时限下**不稳定** —— 同一片数据这次 60/60、
        下次 UNKNOWN 0（实测：171 个任务只排定 51 个，前两片全 0，而每片单独看
        都"成功"）。演示/交付不能接受这种抖动：覆盖优先，质量其次。
        贪心只做一件事：按"当周负载最轻"选格子，遵守场地容量/教师/班级/同组每周
        一个实验四条硬约束；排不下就如实留空（不编造）。

        `kept_*` = **不参与重排**的既有课（绪论/考试）占用，只读、不写入。
        必须单独传、不能并进 `room_busy` 再传进来：那三个集合是**会被本函数 add**
        的全局累积器，混进 kept 之后调用方就再也分不清"谁占的"了。
        为什么贪心也必须看 kept（R-74）：`solve_all` 的累积器从空集起步（要允许复用
        非绪论旧格），本身不含绪论/考试 —— 贪心看不见它们，就会把段塞进绪论课的格子。
        """
        rooms_all = self._get_rooms()
        weeks = self._get_weeks()
        rows: List[Dict] = []
        week_load = Counter()
        # 同一班级同一周只补一个段（保守口径：这一格只是段的起点，组展开会占用同周
        # 更多的格子；若这里再放第二个段，展开时必然抢格）。
        class_week = set()
        for sec in sections:
            best = None
            for w in weeks:
                for d in WEEKDAYS:
                    for t in TIME_SLOTS:
                        if sec.get("teacher") and (w, d, t, sec["teacher"]) in teacher_busy:
                            continue
                        if sec.get("teacher") and (w, d, t, sec["teacher"]) in kept_teacher:
                            continue
                        if sec["class_name"] and (w, d, t, sec["class_name"]) in class_busy:
                            continue
                        if sec["class_name"] and (w, d, t, sec["class_name"]) in kept_class:
                            continue
                        if sec["class_name"] and (sec["class_name"], w) in class_week:
                            continue
                        free = [r for r in rooms_all
                                if (w, d, t, r) not in room_busy
                                and (w, d, t, r) not in kept_room]
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
            if sec["class_name"]:
                class_week.add((sec["class_name"], w))
            room_busy.add((w, d, t, room))
            if sec.get("teacher"):
                teacher_busy.add((w, d, t, sec["teacher"]))
            if sec.get("class_name"):
                class_busy.add((w, d, t, sec["class_name"]))
        return rows

    # ------------------------------------------------------------ 组展开
    def _expand_groups(self, rows: List[Dict], groups_of: Dict[Tuple, List[str]],
                       room_busy: Set[Tuple], teacher_busy: Set[Tuple],
                       class_busy: Set[Tuple],
                       kept_room: AbstractSet[Tuple] = frozenset(),
                       kept_teacher: AbstractSet[Tuple] = frozenset(),
                       kept_class: AbstractSet[Tuple] = frozenset()
                       ) -> Tuple[List[Dict], List[Dict], int]:
        """把"教学段"展开成"组课时"：一个段的 k 个组各占一格。

        口径照真实表（实测 171/171 段）：一个段的所有组 = **同一周、同一间教室、
        按时间顺序连续占格**。5 组的例子：周六 567/678 + 周日 567/678/91011。

        为什么必须展开
        --------------
        求解器按"段"建模（171 个变量，速度可控），但安排表与落地的单位是
        「班级 × 实验项目 × 学生组」。不展开就是两个后果：产出没有 `group` 落不了地；
        171 段把 410 个组课时压成 171，`171/171` 覆盖的是被压小的口径。

        参数里的三个 busy 集合**调用方要预先塞入本批 rows 自己的占用**：展开时先把自己
        那一格释放出来再找位置，这样既不会吃掉别的段的格子，也能用回自己排定的起点。

        `kept_*` = **不参与重排**的既有课（绪论/考试）占用，**只读**。
        为什么必须单独传、不能并进 busy 再传（R-74 修的真 bug）：下面的"释放自己那一格"
        是 `discard`，并进同一集合后它会**把绪论/考试在那一格的记录一起抹掉** ——
        若上游（贪心兜底）恰好把段放在绪论格上，抹掉之后就再也拦不住，最终产出直接
        撞绪论课。分成只读集合后，释放只可能释放本批自己的占用。

        找位置的顺序（五步，前一步不成立才退后一步）：
          1. 原周、原教室，从求解器给的那一格起**向后**找连续 k 格；
          2. 原周、原教室，**任意位置**找连续 k 格（允许落在起点之前）——
             求解器定的是"哪一周"，具体星期/时段可以在这周内挪，这不算改口径；
          3. 原周、**换一间教室**找连续 k 格（教室本就是按空闲清单随手分的）；
          4. **换一周**：本周凑不出 k 格就到其它周找（同教室优先、再任意教室）——
             周次只是摊平负载用的，整段挪周不违反任何互斥；
          5. 退化：原周原教室任意 k 个空格（不连续），并在缺口里标明。
        五步都不成立才记缺口。**绝不硬塞到别人占着的格子里。**

        返回 (展开行, 缺口, 组课时总数)。
        """
        timeline = [(d, t) for d in WEEKDAYS for t in TIME_SLOTS]
        pos_of = {cell: i for i, cell in enumerate(timeline)}
        n_slot = len(timeline)
        ordered = sorted(rows, key=lambda r: (r["week"], pos_of[(r["weekday"], r["time_slot"])]))

        out: List[Dict] = []
        gaps: List[Dict] = []
        total = 0
        for row in ordered:
            w, room = row["week"], row["room"]
            teacher, cls = row.get("teacher") or "", row.get("class_name") or ""
            key = (row.get("teacher"), row.get("course"), row.get("class_name"))
            groups = groups_of.get(key) or [""]
            total += len(groups)
            k = len(groups)

            # 释放自己那一格：本批 rows 的占用是预置的，不释放就永远"被自己占着"
            room_busy.discard((w, row["weekday"], row["time_slot"], room))
            if teacher:
                teacher_busy.discard((w, row["weekday"], row["time_slot"], teacher))
            if cls:
                class_busy.discard((w, row["weekday"], row["time_slot"], cls))

            # 默认参数把循环变量绑死：闭包晚绑定会让"同一格是否空闲"在下一轮变味（B023）
            def _cell_free(pos: int, ww: int, rm: str, tch: str = teacher,
                           cl: str = cls) -> bool:
                d, t = timeline[pos]
                if (ww, d, t, rm) in room_busy or (ww, d, t, rm) in kept_room:
                    return False
                if tch and ((ww, d, t, tch) in teacher_busy
                            or (ww, d, t, tch) in kept_teacher):
                    return False
                return not (cl and ((ww, d, t, cl) in class_busy
                                    or (ww, d, t, cl) in kept_class))

            def _run_in(ww: int, rm: str, lo: int, kk: int = k) -> List[int]:
                """在第 ww 周的教室 rm 里从 lo 起找连续 k 格（不碰教师/班级占用）。"""
                for s in range(lo, n_slot - kk + 1):
                    if all(_cell_free(p, ww, rm) for p in range(s, s + kk)):
                        return list(range(s, s + kk))
                return []

            start = pos_of[(row["weekday"], row["time_slot"])]
            chosen: List[int] = []
            use_room, use_week = room, w
            # 1) 原周原教室：从起点向后连续 k 格
            chosen = _run_in(w, room, start)
            # 2) 原周原教室：全周连续 k 格
            if not chosen:
                chosen = _run_in(w, room, 0)
            # 3) 原周**换一间教室**：教室本来就是求解器按空闲清单顺序随手分的，
            #    整段换房不影响任何硬约束，却能救回一大批"本教室这一周不够 k 格"的段。
            if not chosen:
                for alt in self._get_rooms():
                    if alt == room:
                        continue
                    run = _run_in(w, alt, start) or _run_in(w, alt, 0)
                    if run:
                        chosen, use_room = run, alt
                        break
            # 4) **换周**：本周实在凑不出 k 个连续格 → 到其它周找（同教室优先，再任意
            #    教室）。求解器定的"周"只是为摊平负载，不是硬约束；整段挪到另一周
            #    不违反任何互斥，还能把"5 组挤不进本周"这类缺口补上（实测真实表
            #    第 2 周 5 组段曾只剩 1 组排不进，换周后 410/410）。
            if not chosen:
                alt_rooms = [room] + [r for r in self._get_rooms() if r != room]
                for alt_w in self._get_weeks():
                    if alt_w == w:
                        continue
                    for alt in alt_rooms:
                        run = _run_in(alt_w, alt, start) or _run_in(alt_w, alt, 0)
                        if run:
                            chosen, use_room, use_week = run, alt, alt_w
                            break
                    if chosen:
                        break
            # 5) 退化：原周原教室任意 k 个空格（不连续）
            if not chosen:
                chosen = [p for p in range(n_slot) if _cell_free(p, w, room)][:k]

            if len(chosen) < k:
                for g in groups[len(chosen):]:
                    gaps.append({
                        "week": use_week, "class_name": cls, "course": row.get("course"),
                        "group": g,
                        "reason": (f"第{use_week}周（及其它周）都没有 {k} 个可用格子"
                                   f"（该段 {k} 组，仅排入 {len(chosen)} 组）"),
                    })
            for g, pos in zip(groups, chosen):
                d, t = timeline[pos]
                out.append({**row, "week": use_week, "weekday": d, "time_slot": t,
                            "room": use_room, "group": g})
                room_busy.add((use_week, d, t, use_room))
                if teacher:
                    teacher_busy.add((use_week, d, t, teacher))
                if cls:
                    class_busy.add((use_week, d, t, cls))
        return out, gaps, total

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
            """求一个子片 → 贪心兜底 → **组展开** → 并入全局占用。

            返回 (组课时行, 冲突+缺口, 状态, 是否用了贪心兜底)。
            顺序很重要：贪心补的是"段"，必须在**展开之前**补完，否则展开出来的组
            会和后面补的段抢同一批格子。
            """
            r = self.solve(
                sections_override=sub,
                extra_room_busy=room_busy,
                extra_teacher_busy=teacher_busy,
                extra_class_busy=class_busy,
                flatten_seconds=seconds,
                expand=False,          # 展开留到贪心之后统一做
            )
            rows = r.get("schedule") or []
            status = str(r.get("status") or ("FAILED" if not r.get("success") else "FEASIBLE"))

            placed_keys = {(x["teacher"], x["course"], x["class_name"]) for x in rows}
            missing = [x for x in sub
                       if (x["teacher"], x["course"], x["class_name"]) not in placed_keys]
            # 绪论/考试占用：贪心与展开**都必须**看见它（R-74）。
            # 注意不能并进下面三个累积集再传 —— 它们是会被 add 的全局状态。
            kept_room, kept_teacher, kept_class = self._kept_busy()
            filled = 0
            if missing:
                extra_rows = self._greedy_fill(missing, room_busy, teacher_busy, class_busy,
                                               kept_room, kept_teacher, kept_class)
                filled = len(extra_rows)
                rows = list(rows) + extra_rows
                if filled:
                    status = "FEASIBLE" if status in ("OPTIMAL", "FEASIBLE") else status

            # ---- 段 → 组课时 ----
            groups_of = {(s["teacher"], s["course"], s["class_name"]):
                         (s.get("groups") or [""]) for s in sub}
            for row in rows:                  # 预置本批占用（展开时逐段释放自己的格子）
                cell = (row["week"], row["weekday"], row["time_slot"])
                room_busy.add(cell + (row["room"],))
                if row.get("teacher"):
                    teacher_busy.add(cell + (row["teacher"],))
                if row.get("class_name"):
                    class_busy.add(cell + (row["class_name"],))
            # 展开时**必须**并入"不参与重排的既有课"（绪论/考试）占用 —— 全局累积集
            # 从空集起步（要允许复用非绪论旧格），本身不含它们。这里只用 **_kept_busy**
            # （绪论/考试）而不是 _occupancy()（全量）：非绪论旧格的教室/教师/班级会被
            # 替换腾出，可以复用；全量挡会把覆盖从 410/410 压到 365/410（实测）。
            # ⚠️ 必须作为**独立只读参数**传，不能 `busy | kept` 合并：合并后展开里的
            # "释放自己那一格"会把 kept 一起 discard 掉（R-74 的撞绪论教师就是这么来的）。
            expanded, gaps, _total = self._expand_groups(
                rows, groups_of,
                room_busy, teacher_busy, class_busy,
                kept_room, kept_teacher, kept_class)
            # 展开产出的组课时要回填进全局占用，否则下一片求解器复用不到、
            # 会与之互撞（片间互相不可见）。
            for er in expanded:
                cell = (er["week"], er["weekday"], er["time_slot"])
                room_busy.add(cell + (er["room"],))
                if er.get("teacher"):
                    teacher_busy.add(cell + (er["teacher"],))
                if er.get("class_name"):
                    class_busy.add(cell + (er["class_name"],))
            return expanded, (r.get("conflicts") or []) + gaps, status, filled > 0

        for idx, chunk in enumerate(chunks, 1):
            t0 = time.time()
            # CP-SAT 只负责"质量"（周次摊平、早周优先）；覆盖由确定性贪心兜底，
            # 组展开在 `_place` 内部完成 —— 这里拿到的已经是**组课时**行。
            rows, cfs, status, retried = _place(chunk, flatten_seconds)

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
        total_groups = sum(len(s.get("groups") or [""]) for s in sections_all)
        return {
            "success": bool(schedule),
            "status": overall,
            "weeks": self._get_weeks()[:1] + self._get_weeks()[-1:],
            "total_sections": len(sections_all),
            "total_sections_all": len(sections_all),
            "intro_excluded": intro_excluded,
            # solved = 排定的**段**数（口径与旧版一致）；组课时另算
            "solved": len({(s["teacher"], s["course"], s["class_name"]) for s in schedule}),
            "solved_groups": len(schedule),
            "total_groups": total_groups,
            "conflicts": conflicts,
            "schedule": schedule,
            "chunks": chunk_reports,
            "chunked": len(chunks) > 1,
            "elapsed_s": round(time.time() - t_start, 1),
            "capacity_checked": False,
            "note": (
                f"全校一键排课（分片 CP-SAT）：{len(sections_all)} 个教学任务分 {len(chunks)} 片求解，"
                f"片间回填教室/教师/班级占用；段级求解、组级产出（同周同教室连续格）；"
                f"教室/教师/班级三重互斥 + 与现有安排表逐周对齐"
                + WEEKDAY_ONLY_NOTE
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

        # R37：排课范围也必须说出口。
        #
        # 前端只读 `message`（`setAutoScheduleMsg(r.data.message)`），
        # 不读 `data.note` —— 只改 note 教秘看不到。
        # 而"只在工作日排"这件事直接影响他怎么读这份结果：
        # 看到"第3周：8 个教学任务"，他会以为这周就这些课；
        # 实际周末那两天根本没参与搜索。
        lines.append(
            "📅 排课范围：**周一至周五**（周末不作候选；周末已有课仍计入占用，不会被撞掉）")

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
