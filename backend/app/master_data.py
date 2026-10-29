"""主数据（课程 / 实验项目 / 教师 / 场地 / 班级）内存实体库。

导师座谈（2026-10-21）的一号行动项是「基于教学大纲梳理所有实验课程属性、
重构数据库」。本模块把这件事落地：

    「课程 → 实验项目 →（可授课教师 / 可用场地 / 适用班级）」 与 「班级 → 学生组」
    这套主数据变成**一等公民**，支持：
      1. 从安排表行**反推**填充（一键初始化）；
      2. 增删改查（教秘补全项目 / 调整教师与场地 / 删除过时项目）；
      3. 作为求解器的**候选资源字典**（新增项目不必再伪造旧课表行）。

存储：纯内存 dict（与 `ScheduleDatabase` 同构），进程重启后由 `_ensure_loaded`
从安排表反推自动恢复 —— 不引入第二套持久化，避免「主数据 / 安排表」两处状态
不一致（本项目反复踩的坑）。
"""

from typing import Dict, List, Optional, Set

from collections import Counter

from app.utils.excel_parser import looks_like_exam

# 真实表里没有「课程」字段，25 个项目都属于大学物理实验这门大课。
# 教秘可后续通过 CRUD 把项目归到不同课程（如「光学实验」「电磁学实验」）。
DEFAULT_COURSE = "大学物理实验"

_INTRO_KW = ("绪论",)


def is_schedulable_project(name: str) -> bool:
    """该项目是否**参与排课**：绪论课 / 考试考核类不参与（R-48 的业务口径）。

    ⚠️ 这是"可排"属性，不是"存在"属性 —— 绪论与考试类**照样登记为实体**
    （见 `build_from_entries`）。早先的做法是整行 `continue` 跳过，于是这 3 个项目
    连实体都不建：教秘在台账里看不到它们、无法给它们指定课程，17 条记录的课程
    只能回落默认课（R-57 实测发现）。代价是"为了不排绪论课，把它的事实信息也扔了"。

    它是**项目名的函数，不可被用户覆盖** —— 否则教秘把"考试"改成可排，求解器就会
    去排一场考试（R-48 明确考试/考核类的时间与场地逻辑不同，不该进求解器）。
    """
    return not (_is_intro_like(name) or looks_like_exam(name))


def _new_class() -> dict:
    """班级实体的统一骨架：组 + 教务给定的上课时间范围。"""
    return {"groups": set(), "weekday": "", "weekdays": set(), "time_slots": set(),
            "week_min": None, "week_max": None}


def _is_intro_like(name: str) -> bool:
    """绪论课已单独安排（教室已定），不进主数据、不参与并行排课。"""
    return any(k in (name or "") for k in _INTRO_KW)


class MasterData:
    """五张实体表，key=名称，value=实体 dict。

    表结构
    ------
      courses     : name -> {"experiments": Set[str]}
      experiments : name -> {"course": str, "teachers": Set, "rooms": Set, "classes": Set}
      teachers    : name -> {"projects": Set[str]}
      rooms       : name -> {"capacity": Optional[int]}
      classes     : name -> {"groups": Set[str], "weekday": str, "time_slots": Set[str],
                             "week_min": Optional[int], "week_max": Optional[int]}
    """

    def __init__(self):
        self.courses: Dict[str, dict] = {}
        self.experiments: Dict[str, dict] = {}
        self.teachers: Dict[str, dict] = {}
        self.rooms: Dict[str, dict] = {}
        self.classes: Dict[str, dict] = {}
        self.initialized = False

    # ------------------------------------------------------------------ 反推
    def build_from_entries(self, entries) -> "MasterData":
        """从安排表行反推五张实体表（绪论课 / 考试类跳过，其余如实收录）。"""
        self.courses.clear()
        self.experiments.clear()
        self.teachers.clear()
        self.rooms.clear()
        self.classes.clear()

        for e in entries:
            exp = (e.experiment or "").strip()
            if not exp:
                continue
            cls = (e.class_name or "").strip()
            teacher = (e.teacher or "").strip()
            room = (e.room or "").strip()
            group = (e.group or "").strip()

            # 项目实体**一律登记**（含绪论/考试类）：教秘要能在台账里看到它们、
            # 给它们指定课程。早先整行跳过 ⇒ 17 条记录的课程只能回落默认课（R-57）。
            ex = self.experiments.setdefault(
                exp, {"course": DEFAULT_COURSE, "teachers": set(), "rooms": set(), "classes": set()})
            if cls:
                ex["classes"].add(cls)
            if teacher:
                ex["teachers"].add(teacher)
            if room:
                ex["rooms"].add(room)

            # ⚠️ 班级 / 教师 / 场地**实体**的累积仍然跳过绪论与考试类。
            # 实测：若把它们也算进去，17 个班的 week_min 会统一提前 1 周（绪论课在该班
            # 第一次实验之前），求解器的周次窗口随之放宽 —— 那是 R-51 锁定的产品边界
            # （"时间由教务给定"），不该顺手改。教师 13 / 场地 10 / 班级 17 也不受影响。
            if not is_schedulable_project(exp):
                continue

            if teacher:
                self.teachers.setdefault(teacher, {"projects": set()})["projects"].add(exp)
            if room:
                self.rooms.setdefault(room, {"capacity": None})
            if cls:
                c = self.classes.setdefault(cls, _new_class())
                c.setdefault("_wd_count", Counter())
                if group:
                    c["groups"].add(group)
                # 时间范围累积：教务已给「课程总时间范围」，这里从安排表反推为默认基线，
                # 教秘可在主数据里改。节次按 blocked 展开（跨节次记录占多格）。
                if e.weekday:
                    c["_wd_count"][e.weekday] += 1
                    c["weekdays"].add(e.weekday)
                for s in (getattr(e, "blocked", None) or [e.time_slot]):
                    if s:
                        c["time_slots"].add(s)
                if isinstance(e.week, int):
                    c["week_min"] = e.week if c["week_min"] is None else min(c["week_min"], e.week)
                    c["week_max"] = e.week if c["week_max"] is None else max(c["week_max"], e.week)

        # 主要上课星期：取出现最多的（少数派星期不计入时间范围，避免推断错误）
        for c in self.classes.values():
            wd_count = c.pop("_wd_count", None)
            c["weekday"] = wd_count.most_common(1)[0][0] if wd_count else ""

        self._rebuild_courses()
        self.initialized = True
        return self

    def _rebuild_courses(self):
        """课程表由实验项目的 `course` 字段反向聚合，永远一致。"""
        self.courses.clear()
        for name, ex in self.experiments.items():
            course = ex.get("course") or DEFAULT_COURSE
            self.courses.setdefault(course, {"experiments": set()})["experiments"].add(name)

    # ------------------------------------------------------------------ 查询
    def project_classes(self, experiment: str) -> Set[str]:
        """某实验项目适用哪些班级（反推自历史开课记录）。"""
        return set(self.experiments.get(experiment, {}).get("classes", set()))

    def classes_of(self, class_name: str) -> Set[str]:
        """某班级要上哪些实验项目（反向索引，反推自历史开课记录）。"""
        return {n for n, ex in self.experiments.items() if class_name in ex.get("classes", set())}

    def groups_of(self, class_name: str) -> Set[str]:
        """某班级的学生组（反推自安排表，空组补一个默认）。"""
        g = self.classes.get(class_name, {}).get("groups", set())
        return g or {""}

    # ------------------------------------------------------------------ CRUD
    def upsert_course(self, name: str) -> str:
        name = (name or "").strip()
        if not name:
            raise ValueError("课程名不能为空，请填写后再提交")
        self.courses.setdefault(name, {"experiments": set()})
        return name

    def delete_course(self, name: str):
        """删课程：其下实验项目归回默认课程（不删项目，只改归属）。"""
        for ex in self.experiments.values():
            if ex.get("course") == name:
                ex["course"] = DEFAULT_COURSE
        self.courses.pop(name, None)
        self._rebuild_courses()

    def upsert_experiment(self, name: str, course: str = "", teachers: Optional[List[str]] = None,
                          rooms: Optional[List[str]] = None, classes: Optional[List[str]] = None) -> dict:
        name = (name or "").strip()
        if not name:
            raise ValueError("实验项目名不能为空，请填写后再提交")
        ex = self.experiments.setdefault(
            name, {"course": DEFAULT_COURSE, "teachers": set(), "rooms": set(), "classes": set()})
        if course:
            ex["course"] = course.strip()
        if teachers is not None:
            for t in teachers:
                t = (t or "").strip()
                if t:
                    ex["teachers"].add(t)
                    # 实体回链只给**可排课**项目：与 build_from_entries 的反推口径一致
                    # （绪论/考试类登记实体但不做教师/场地/班级累积）。否则台账导回
                    # 绪论项目会把它的教师/场地凭空建成实体，往返不再相等（实测抓到）。
                    if is_schedulable_project(name):
                        self.teachers.setdefault(t, {"projects": set()})["projects"].add(name)
        if rooms is not None:
            for r in rooms:
                r = (r or "").strip()
                if r:
                    ex["rooms"].add(r)
                    if is_schedulable_project(name):
                        self.rooms.setdefault(r, {"capacity": None})
        if classes is not None:
            for c in classes:
                c = (c or "").strip()
                if c:
                    ex["classes"].add(c)
                    if is_schedulable_project(name):
                        self.classes.setdefault(c, _new_class())
        self._rebuild_courses()
        return self.experiments[name]

    def delete_experiment(self, name: str):
        """删实验项目：同时从教师的可授项目里摘除（保持引用一致）。"""
        for t in self.teachers.values():
            t.get("projects", set()).discard(name)
        self.experiments.pop(name, None)
        self._rebuild_courses()

    def upsert_teacher(self, name: str, projects: Optional[List[str]] = None) -> dict:
        name = (name or "").strip()
        if not name:
            raise ValueError("教师名不能为空，请填写后再提交")
        t = self.teachers.setdefault(name, {"projects": set()})
        if projects is not None:
            for p in projects:
                p = (p or "").strip()
                if p:
                    t["projects"].add(p)
                    self.experiments.setdefault(
                        p, {"course": DEFAULT_COURSE, "teachers": set(), "rooms": set(), "classes": set()}
                    )["teachers"].add(name)
        self._rebuild_courses()
        return t

    def delete_teacher(self, name: str):
        """删教师：同时从实验项目的授课教师里摘除。"""
        for ex in self.experiments.values():
            ex.get("teachers", set()).discard(name)
        self.teachers.pop(name, None)

    def upsert_room(self, name: str, capacity: Optional[int] = None) -> dict:
        name = (name or "").strip()
        if not name:
            raise ValueError("场地名不能为空，请填写后再提交")
        r = self.rooms.setdefault(name, {"capacity": None})
        if capacity is not None:
            r["capacity"] = capacity
        return r

    def delete_room(self, name: str):
        """删场地：同时从实验项目的可用场地里摘除。"""
        for ex in self.experiments.values():
            ex.get("rooms", set()).discard(name)
        self.rooms.pop(name, None)

    def upsert_class(self, name: str, groups: Optional[List[str]] = None,
                     weekday: str = "", time_slots: Optional[List[str]] = None,
                     week_min: Optional[int] = None, week_max: Optional[int] = None,
                     weekdays: Optional[List[str]] = None) -> dict:
        name = (name or "").strip()
        if not name:
            raise ValueError("班级名不能为空，请填写后再提交")
        c = self.classes.setdefault(name, _new_class())
        if groups is not None:
            for g in groups:
                g = (g or "").strip()
                if g:
                    c["groups"].add(g)
        # 教务给定的「课程总时间范围」：星期 / 节次 / 周次。空字符串/None 表示不覆盖。
        if weekday:
            c["weekday"] = weekday.strip()
            c["weekdays"].add(weekday.strip())
        if weekdays is not None:
            for d in weekdays:
                d = (d or "").strip()
                if d:
                    c["weekdays"].add(d)
        if time_slots is not None:
            for s in time_slots:
                s = (s or "").strip()
                if s:
                    c["time_slots"].add(s)
        if week_min is not None:
            c["week_min"] = week_min
        if week_max is not None:
            c["week_max"] = week_max
        return c

    def delete_class(self, name: str):
        """删班级：同时从实验项目的适用班级里摘除。"""
        for ex in self.experiments.values():
            ex.get("classes", set()).discard(name)
        self.classes.pop(name, None)

    # -------------------------------------------------------------- 序列化
    def to_dict(self) -> dict:
        """完整状态 → JSON 友好 dict（集合转有序列表），用于落盘持久化。"""
        return {
            "courses": {n: {"experiments": sorted(v["experiments"], key=str)}
                        for n, v in self.courses.items()},
            "experiments": {n: {"course": v.get("course") or DEFAULT_COURSE,
                                "teachers": sorted(v["teachers"], key=str),
                                "rooms": sorted(v["rooms"], key=str),
                                "classes": sorted(v["classes"], key=str)}
                            for n, v in self.experiments.items()},
            "teachers": {n: {"projects": sorted(v["projects"], key=str)}
                         for n, v in self.teachers.items()},
            "rooms": {n: {"capacity": v.get("capacity")} for n, v in self.rooms.items()},
            "classes": {n: {"groups": sorted(v["groups"], key=str),
                            "weekday": v.get("weekday", ""),
                            "weekdays": sorted(v.get("weekdays") or [], key=str),
                            "time_slots": sorted(v["time_slots"], key=str),
                            "week_min": v.get("week_min"), "week_max": v.get("week_max")}
                        for n, v in self.classes.items()},
        }

    def from_dict(self, d: dict) -> "MasterData":
        """to_dict 的逆操作：恢复五张表并重建课程聚合。坏数据fail-closed（回退反推基线）。"""
        try:
            self.courses = {n: {"experiments": set(v["experiments"])}
                            for n, v in (d.get("courses") or {}).items()}
            self.experiments = {n: {"course": v.get("course") or DEFAULT_COURSE,
                                    "teachers": set(v.get("teachers") or []),
                                    "rooms": set(v.get("rooms") or []),
                                    "classes": set(v.get("classes") or [])}
                                for n, v in (d.get("experiments") or {}).items()}
            self.teachers = {n: {"projects": set(v.get("projects") or [])}
                             for n, v in (d.get("teachers") or {}).items()}
            self.rooms = {n: {"capacity": v.get("capacity")}
                          for n, v in (d.get("rooms") or {}).items()}
            self.classes = {n: {"groups": set(v.get("groups") or []),
                                "weekday": v.get("weekday") or "",
                                "weekdays": set(v.get("weekdays") or []),
                                "time_slots": set(v.get("time_slots") or []),
                                "week_min": v.get("week_min"), "week_max": v.get("week_max")}
                            for n, v in (d.get("classes") or {}).items()}
            self._rebuild_courses()
            self.initialized = True
            return self
        except (TypeError, AttributeError, KeyError) as e:
            print(f"⚠️ 主数据落盘文件损坏（{e}），回退到安排表反推基线")
            return self

    def overview(self) -> dict:
        """五张表的完整状态（JSON 友好，供前端展示 + 免鉴权只读接口）。"""
        def _sorted(x): return sorted(x, key=str)
        return {
            "initialized": self.initialized,
            "courses": [
                {"name": n, "experiments": _sorted(v["experiments"])}
                for n, v in sorted(self.courses.items(), key=lambda kv: str(kv[0]))
            ],
            "experiments": [
                {"name": n, "course": v.get("course") or DEFAULT_COURSE,
                 "teachers": _sorted(v["teachers"]), "rooms": _sorted(v["rooms"]),
                 "classes": _sorted(v["classes"]),
                 "schedulable": is_schedulable_project(n)}
                for n, v in sorted(self.experiments.items(), key=lambda kv: str(kv[0]))
            ],
            "teachers": [
                {"name": n, "projects": _sorted(v["projects"])}
                for n, v in sorted(self.teachers.items(), key=lambda kv: str(kv[0]))
            ],
            "rooms": [
                {"name": n, "capacity": v.get("capacity")}
                for n, v in sorted(self.rooms.items(), key=lambda kv: str(kv[0]))
            ],
            "classes": [
                {"name": n, "groups": _sorted(v["groups"]), "weekday": v.get("weekday", ""),
                 "weekdays": _sorted(v.get("weekdays") or []),
                 "time_slots": _sorted(v["time_slots"]), "week_min": v.get("week_min"),
                 "week_max": v.get("week_max")}
                for n, v in sorted(self.classes.items(), key=lambda kv: str(kv[0]))
            ],
        }
