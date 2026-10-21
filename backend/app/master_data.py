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

from app.utils.excel_parser import looks_like_exam

# 真实表里没有「课程」字段，25 个项目都属于大学物理实验这门大课。
# 教秘可后续通过 CRUD 把项目归到不同课程（如「光学实验」「电磁学实验」）。
DEFAULT_COURSE = "大学物理实验"

_INTRO_KW = ("绪论",)


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
      classes     : name -> {"groups": Set[str]}
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
            if not exp or _is_intro_like(exp) or looks_like_exam(exp):
                continue
            cls = (e.class_name or "").strip()
            teacher = (e.teacher or "").strip()
            room = (e.room or "").strip()
            group = (e.group or "").strip()

            ex = self.experiments.setdefault(
                exp, {"course": DEFAULT_COURSE, "teachers": set(), "rooms": set(), "classes": set()})
            if cls:
                ex["classes"].add(cls)
            if teacher:
                ex["teachers"].add(teacher)
            if room:
                ex["rooms"].add(room)

            if teacher:
                self.teachers.setdefault(teacher, {"projects": set()})["projects"].add(exp)
            if room:
                self.rooms.setdefault(room, {"capacity": None})
            if cls:
                self.classes.setdefault(cls, {"groups": set()})
                if group:
                    self.classes[cls]["groups"].add(group)

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
            raise ValueError("课程名不能为空")
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
            raise ValueError("实验项目名不能为空")
        ex = self.experiments.setdefault(
            name, {"course": DEFAULT_COURSE, "teachers": set(), "rooms": set(), "classes": set()})
        if course:
            ex["course"] = course.strip()
        if teachers is not None:
            for t in teachers:
                t = (t or "").strip()
                if t:
                    ex["teachers"].add(t)
                    self.teachers.setdefault(t, {"projects": set()})["projects"].add(name)
        if rooms is not None:
            for r in rooms:
                r = (r or "").strip()
                if r:
                    ex["rooms"].add(r)
                    self.rooms.setdefault(r, {"capacity": None})
        if classes is not None:
            for c in classes:
                c = (c or "").strip()
                if c:
                    ex["classes"].add(c)
                    self.classes.setdefault(c, {"groups": set()})
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
            raise ValueError("教师名不能为空")
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
            raise ValueError("场地名不能为空")
        r = self.rooms.setdefault(name, {"capacity": None})
        if capacity is not None:
            r["capacity"] = capacity
        return r

    def delete_room(self, name: str):
        """删场地：同时从实验项目的可用场地里摘除。"""
        for ex in self.experiments.values():
            ex.get("rooms", set()).discard(name)
        self.rooms.pop(name, None)

    def upsert_class(self, name: str, groups: Optional[List[str]] = None) -> dict:
        name = (name or "").strip()
        if not name:
            raise ValueError("班级名不能为空")
        c = self.classes.setdefault(name, {"groups": set()})
        if groups is not None:
            for g in groups:
                g = (g or "").strip()
                if g:
                    c["groups"].add(g)
        return c

    def delete_class(self, name: str):
        """删班级：同时从实验项目的适用班级里摘除。"""
        for ex in self.experiments.values():
            ex.get("classes", set()).discard(name)
        self.classes.pop(name, None)

    # -------------------------------------------------------------- 序列化
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
                 "classes": _sorted(v["classes"])}
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
                {"name": n, "groups": _sorted(v["groups"])}
                for n, v in sorted(self.classes.items(), key=lambda kv: str(kv[0]))
            ],
        }
