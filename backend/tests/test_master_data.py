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
    assert set(md.experiments) == {"扭摆法", "声速测量"}  # 绪论课被排除
    assert set(md.teachers) == {"吴老师", "林老师"}
    assert set(md.rooms) == {"A410", "A411"}
    assert set(md.classes) == {"一班", "二班"}
    assert {"扭摆法", "声速测量"} == md.courses[DEFAULT_COURSE]["experiments"]


def test_build_from_entries_excludes_exam_and_empty():
    md = MasterData()
    md.build_from_entries([
        _entry(1, "周四", "567", "一班", "1组", "A410", experiment="大物实验B考试"),
        _entry(1, "周四", "567", "一班", "1组", "A410", experiment=""),
    ])
    assert md.experiments == {}
    assert md.teachers == {}


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
