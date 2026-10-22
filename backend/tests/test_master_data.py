"""主数据五张实体表 + CRUD + 求解器派生（导师一号行动项「重构数据库」）

为什么这批用例存在
------------------
导师座谈（2026-10-21）的一号行动项是「基于教学大纲梳理所有实验课程属性、重构
数据库」。此前系统只有扁平的安排表行（R-47 台账只能**读**、R-48 清单只能**导**），
没有「课程 → 实验项目 → 教师/场地」这套**可增删改**的主数据。

这批用例锁三件事：
  1. 从安排表反推五张实体表，绪论课 / 考试类被正确排除；
  2. CRUD 增删改**保持引用一致**（删教师 → 项目里同步摘除，不产生孤儿引用）；
  3. 求解器派生：只有「某班没排过的新增项目」才补成待排任务，反推基线派生为 0
     （按 (班级, 项目) 粒度判，组名写法不一致不会造成假漏排）。
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.master_data import DEFAULT_COURSE, MasterData
from app.utils.excel_parser import ScheduleDatabase, ScheduleEntry

import app.api.reschedule as api


def _entry(week, weekday, slot, cls, group, room, teacher="吴老师", experiment="扭摆法"):
    return ScheduleEntry(week, weekday, slot, cls, group, experiment, room, teacher)


# ---------------------------------------------------------------- 反推

def test_build_from_entries_derives_five_tables():
    md = MasterData()
    md.build_from_entries([
        _entry(1, "周四", "567", "一班", "1组", "A410", teacher="吴老师", experiment="扭摆法"),
        _entry(1, "周四", "567", "一班", "2组", "A411", teacher="林老师", experiment="扭摆法"),
        _entry(2, "周四", "678", "二班", "1组", "A410", teacher="吴老师", experiment="声速测量"),
        _entry(3, "周五", "91011", "一班", "1组", "", teacher="吴老师", experiment="绪论课（教室已安排）"),
    ])
    assert md.initialized
    # R-58：绪论课**登记为实体**（台账可见、可指定课程），不再整行跳过 ——
    # 早先为"不排绪论课"把它的事实信息也扔了，17 条记录的课程只能回落默认课。
    assert set(md.experiments) == {"扭摆法", "声速测量", "绪论课（教室已安排）"}
    intro = md.experiments["绪论课（教室已安排）"]
    assert intro["classes"] == {"一班"} and intro["teachers"] == {"吴老师"}  # 事实链接保留
    # 但班级/教师/场地**实体**累积仍排除绪论（ week_min 基线不漂移，见下一条）
    assert set(md.teachers) == {"吴老师", "林老师"}
    assert set(md.rooms) == {"A410", "A411"}
    assert set(md.classes) == {"一班", "二班"}
    # 课程聚合含绪论（归默认课，教秘可在台账里改）
    assert {"扭摆法", "声速测量", "绪论课（教室已安排）"} == md.courses[DEFAULT_COURSE]["experiments"]


def test_intro_does_not_shift_class_time_range():
    """绪论课在该班第一次实验**之前**：若计入班级累积，week_min 会提前 —— 求解器
    周次窗口（R-51 产品边界："时间由教务给定"）随之放宽。实测真实表 17 个班全部
    会提前 1 周，故班级时间范围**只由可排课项目累积**。
    """
    md = MasterData()
    md.build_from_entries([
        _entry(1, "周五", "91011", "一班", "1组", "", teacher="吴老师", experiment="绪论课"),
        _entry(3, "周四", "567", "一班", "1组", "A410", teacher="吴老师", experiment="扭摆法"),
    ])
    c = md.classes["一班"]
    assert c["week_min"] == 3 and c["week_max"] == 3, c  # 不是 1
    assert "绪论课" in md.experiments  # 但项目实体在
    ov_exp = next(x for x in md.overview()["experiments"] if x["name"] == "绪论课")
    assert ov_exp["schedulable"] is False  # 台账可见「不排课」


def test_build_from_entries_excludes_exam_and_empty():
    """考试类：项目**登记**为实体（R-58），但教师/场地/班级实体不累积；空名跳过。"""
    md = MasterData()
    md.build_from_entries([
        _entry(1, "周四", "567", "一班", "1组", "A410", experiment="大物实验B考试"),
        _entry(1, "周四", "567", "一班", "1组", "A410", experiment=""),
    ])
    assert set(md.experiments) == {"大物实验B考试"}  # 登记但不累积
    assert md.experiments["大物实验B考试"]["classes"] == {"一班"}
    assert md.teachers == {}
    assert md.rooms == {}


# ---------------------------------------------------------------- 查询

def test_classes_of_and_groups_of():
    md = MasterData()
    md.build_from_entries([
        _entry(1, "周四", "567", "一班", "1组", "A410", experiment="扭摆法"),
        _entry(1, "周四", "567", "一班", "2组", "A410", experiment="扭摆法"),
        _entry(1, "周四", "567", "一班", "1组", "A411", experiment="声速测量"),
    ])
    assert md.classes_of("一班") == {"扭摆法", "声速测量"}
    assert md.groups_of("一班") == {"1组", "2组"}


# ---------------------------------------------------------------- CRUD

def test_upsert_delete_experiment_keeps_references_consistent():
    md = MasterData()
    md.upsert_experiment("光纤传感", course=DEFAULT_COURSE,
                         teachers=["吴老师"], rooms=["A502"], classes=["一班"])
    assert md.teachers["吴老师"]["projects"] == {"光纤传感"}
    assert "一班" in md.experiments["光纤传感"]["classes"]
    assert "光纤传感" in md.courses[DEFAULT_COURSE]["experiments"]

    md.delete_experiment("光纤传感")
    assert "光纤传感" not in md.experiments
    assert "光纤传感" not in md.teachers["吴老师"]["projects"]  # 引用一致性
    # 删掉课程下唯一项目后，该课程自动从 courses 里消失（空课程不保留）
    assert "光纤传感" not in md.courses.get(DEFAULT_COURSE, {}).get("experiments", set())


def test_delete_teacher_and_room_remove_from_experiments():
    md = MasterData()
    md.upsert_experiment("扭摆法", teachers=["吴老师"], rooms=["A410"], classes=["一班"])
    md.delete_teacher("吴老师")
    assert "吴老师" not in md.experiments["扭摆法"]["teachers"]
    md.delete_room("A410")
    assert "A410" not in md.experiments["扭摆法"]["rooms"]


def test_delete_class_removes_from_experiments():
    md = MasterData()
    md.upsert_experiment("扭摆法", classes=["一班", "二班"])
    md.delete_class("一班")
    assert md.experiments["扭摆法"]["classes"] == {"二班"}


def test_delete_course_reassigns_projects_to_default():
    md = MasterData()
    md.upsert_course("光学实验")
    md.upsert_experiment("分光计", course="光学实验", classes=["一班"])
    assert md.courses["光学实验"]["experiments"] == {"分光计"}
    md.delete_course("光学实验")
    assert "光学实验" not in md.courses
    assert md.experiments["分光计"]["course"] == DEFAULT_COURSE
    assert "分光计" in md.courses[DEFAULT_COURSE]["experiments"]


def test_upsert_empty_name_rejected():
    md = MasterData()
    with pytest.raises(ValueError):
        md.upsert_experiment("   ")
    with pytest.raises(ValueError):
        md.upsert_teacher("")


def test_overview_is_json_serializable():
    md = MasterData()
    md.build_from_entries([_entry(1, "周四", "567", "一班", "1组", "A410", experiment="扭摆法")])
    import json
    ov = md.overview()
    json.dumps(ov)  # 序列化不抛异常
    assert ov["initialized"] is True
    assert len(ov["experiments"]) == 1
    assert ov["experiments"][0]["name"] == "扭摆法"


# ---------------------------------------------------------------- 求解器派生

def test_master_derived_tasks_only_new_projects(monkeypatch):
    """反推基线派生 0；新增「某班没排过」的项目才派生待排任务。"""
    db = ScheduleDatabase()
    db.entries = [_entry(1, "周四", "567", "一班", "1组", "A410", experiment="扭摆法")]
    db.pending = []
    db.loaded = True
    monkeypatch.setattr(api, "_db", db)
    monkeypatch.setattr(api, "_master_tasks", [])
    md = MasterData()
    md.build_from_entries(db.entries)
    monkeypatch.setattr(api, "_master_data", md)

    assert api._master_derived_tasks() == []  # 基线：项目已排，不派生

    md.upsert_experiment("光纤传感", classes=["一班"])
    derived = api._master_derived_tasks()
    assert len(derived) == 1
    assert derived[0].experiment == "光纤传感"
    assert derived[0].class_name == "一班"


def test_master_derived_tasks_skip_intro_and_exam(monkeypatch):
    """R-58 防线：绪论/考试类项目登记为实体后，**绝不派生**待排任务。

    不过滤的话：教秘经 CRUD 或台账导入给「绪论课」加一个适用班级，求解器就会
    把一场绪论课排进某个教室 —— R-48 的口径是绪论课已单独安排、不进求解器。
    """
    db = ScheduleDatabase()
    db.entries = [_entry(1, "周四", "567", "一班", "1组", "A410", experiment="扭摆法")]
    db.pending = []
    db.loaded = True
    monkeypatch.setattr(api, "_db", db)
    monkeypatch.setattr(api, "_master_tasks", [])
    md = MasterData()
    md.build_from_entries(db.entries)
    monkeypatch.setattr(api, "_master_data", md)

    md.upsert_experiment("绪论课", classes=["一班"])       # 模拟台账导入/CRUD 补班级
    md.upsert_experiment("大物实验C考试", classes=["一班"])
    derived = api._master_derived_tasks()
    names = {t.experiment for t in derived}
    assert "绪论课" not in names and "大物实验C考试" not in names, names
    assert names == set(), names  # 扭摆法已排，其余全被 schedulable 过滤


def test_raw_schedulable_tasks_filter_intro_and_exam(monkeypatch):
    """灰箱可排清单统一走 is_schedulable_project：绪论与考试类都不进求解器。

    此前只过滤 looks_like_exam —— R-58 让绪论登记进主数据后，上传清单
    （_master_tasks）或 pending 里若出现绪论行，灰箱排课就会把一场绪论课
    排进某个教室（R-48：绪论课已单独安排）。
    """
    from app.utils.excel_parser import PendingTask

    db = ScheduleDatabase()
    db.entries = []
    db.pending = [PendingTask(class_name="一班", group="1组", experiment="绪论课")]
    db.loaded = True
    monkeypatch.setattr(api, "_db", db)
    monkeypatch.setattr(api, "_master_tasks", [
        PendingTask(class_name="一班", group="1组", experiment="大物实验B考试"),
        PendingTask(class_name="一班", group="1组", experiment="扭摆法"),
    ])
    monkeypatch.setattr(api, "_master_derived_tasks", lambda: [])

    names = [t.experiment for t in api._raw_schedulable_tasks()]
    assert names == ["扭摆法"], names  # 绪论与考试都被滤掉，只剩可排项


def test_master_derived_tasks_group_granularity_no_false_positive(monkeypatch):
    """组名写法不一致（班上有组 A 上过别的项目、却没上「扭摆法」）不应被误判为漏排。"""
    db = ScheduleDatabase()
    db.entries = [
        _entry(1, "周四", "567", "一班", "1组", "A410", experiment="扭摆法"),
        _entry(2, "周四", "567", "一班", "2组", "A410", experiment="声速测量"),  # 2组 没上扭摆法
    ]
    db.pending = []
    db.loaded = True
    monkeypatch.setattr(api, "_db", db)
    monkeypatch.setattr(api, "_master_tasks", [])
    md = MasterData()
    md.build_from_entries(db.entries)
    monkeypatch.setattr(api, "_master_data", md)

    assert api._master_derived_tasks() == []  # 按 (班级, 项目) 判，不因组名差异而误派生


# ------------------------------------------------------- 教务给定的时间范围

def test_build_from_entries_derives_class_time_range():
    """班级的上课时间范围（星期/节次/周次）从安排表反推，少数派星期不计入。"""
    md = MasterData()
    md.build_from_entries([
        _entry(1, "周四", "567", "一班", "1组", "A410", experiment="扭摆法"),
        _entry(2, "周四", "567", "一班", "1组", "A410", experiment="声速测量"),
        _entry(3, "周四", "678", "一班", "1组", "A410", experiment="扭摆法"),
        _entry(4, "周五", "567", "一班", "1组", "A410", experiment="扭摆法"),  # 少数派
    ])
    c = md.classes["一班"]
    assert c["weekday"] == "周四"          # 多数派胜出
    assert c["time_slots"] == {"567", "678"}
    assert c["week_min"] == 1 and c["week_max"] == 4


def test_upsert_class_time_range():
    """教务给定的时间范围可显式录入（覆盖反推基线）。"""
    md = MasterData()
    md.upsert_class("一班", weekday="周五", time_slots=["567"], week_min=2, week_max=8)
    c = md.classes["一班"]
    assert c["weekday"] == "周五"
    assert c["time_slots"] == {"567"}
    assert c["week_min"] == 2 and c["week_max"] == 8


def test_solve_respects_time_slots_parameter():
    """求解器只在教务给定的节次里排，不会用到范围外的节次。"""
    from app.engine.parallel_scheduler import ParallelScheduler

    db = ScheduleDatabase()
    db.entries = [
        _entry(1, "周四", "567", "一班", "1组", "A410", experiment="扭摆法"),
        _entry(1, "周四", "567", "一班", "1组", "A410", experiment="声速测量"),
    ]
    db.loaded = True
    ps = ParallelScheduler(db, extra_tasks=[])
    # 只允许 567 一个节次：两个同组单元只能分到不同周，但绝不能出现 678/91011。
    # week_max_override 隔离周次推导的干扰（测试数据只在第 1 周）。
    r = ps.solve("一班", weekday="周四", start_week=1, weeks=4, time_slots=["567"], week_max_override=4)
    assert r["success"] is True
    used_slots = {s["time_slot"] for s in r["schedule"]}
    assert used_slots == {"567"}

