"""灰箱排课：信息不全时一键排课能否成功 + 系统替用户做的决定是否可见

背景（用户 2026-10-22 的验收场景）
--------------------------------
「之前平台是导入了信息，之后将真实模拟，将数据进行灰箱提供，在缺少信息下，
能否成功一键全面排课。」

灰箱 = 教秘只给部分信息：清单里不写指导教师 / 不写场地 / 不给星期 / 不给
节次 / 不给周次。系统此前会**静默**用默认值（教师留空、默认周四、三段全集、
排到表内最后一周）—— 排出来了，但教秘不知道系统替他做了什么决定，导出的
课表教师列是空的也没人提醒。

这批用例锁两件事：
  1. 补全：缺的教师/场地从主数据自动补上（负载最轻者优先），缺不补的如实报告；
  2. 可见性：所有默认决定都进 `assumptions`，绝不静默。
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.master_data import MasterData
from app.utils.excel_parser import PendingTask, ScheduleDatabase, ScheduleEntry

import app.api.reschedule as api


def _entry(week, weekday, slot, cls, group, room, teacher="吴老师", experiment="扭摆法"):
    return ScheduleEntry(week, weekday, slot, cls, group, experiment, room, teacher)


def _md_with_pool():
    """主数据：扭摆法可授 吴/林 两位老师，场地 A410/A411。"""
    md = MasterData()
    md.build_from_entries([
        _entry(1, "周四", "567", "一班", "1组", "A410", teacher="吴老师", experiment="扭摆法"),
        _entry(2, "周四", "567", "二班", "1组", "A411", teacher="林老师", experiment="扭摆法"),
    ])
    return md


# ---------------------------------------------------------------- 补全

def test_enrich_fills_missing_teacher_from_master(monkeypatch):
    """清单没写指导教师 → 从主数据该项目可授教师里补，并记录假设。"""
    db = ScheduleDatabase()
    db.entries = []
    db.pending = []
    db.loaded = True
    monkeypatch.setattr(api, "_db", db)
    monkeypatch.setattr(api, "_master_tasks", [
        PendingTask("灰箱班", "扭摆法", "1组"),
        PendingTask("灰箱班", "扭摆法", "2组"),
    ])
    monkeypatch.setattr(api, "_master_data", _md_with_pool())
    monkeypatch.setattr(api, "_master_derived_tasks", lambda: [])

    tasks, assumptions = api._enrich_extra_tasks()
    assert all(t.teacher in {"吴老师", "林老师"} for t in tasks)
    # 负载均衡：两个空位分给两位不同老师，而不是都砸给第一个
    assert {t.teacher for t in tasks} == {"吴老师", "林老师"}
    assert any("自动分配" in a for a in assumptions)
    assert len([a for a in assumptions if "自动分配" in a]) == 2


def test_enrich_fills_missing_room_from_master(monkeypatch):
    db = ScheduleDatabase()
    db.entries = []
    db.pending = []
    db.loaded = True
    monkeypatch.setattr(api, "_db", db)
    monkeypatch.setattr(api, "_master_tasks", [PendingTask("灰箱班", "扭摆法", "1组")])
    monkeypatch.setattr(api, "_master_data", _md_with_pool())
    monkeypatch.setattr(api, "_master_derived_tasks", lambda: [])

    tasks, assumptions = api._enrich_extra_tasks()
    assert tasks[0].room == "A410"
    assert any("候选场地" in a for a in assumptions)


def test_enrich_never_overrides_explicit_values(monkeypatch):
    """灰箱补全只填空字段：显式给出的教师/场地一个字节都不动。"""
    db = ScheduleDatabase()
    db.entries = []
    db.pending = []
    db.loaded = True
    monkeypatch.setattr(api, "_db", db)
    monkeypatch.setattr(api, "_master_tasks", [
        PendingTask("灰箱班", "扭摆法", "1组", teacher="指定老师", room="B999"),
    ])
    monkeypatch.setattr(api, "_master_data", _md_with_pool())
    monkeypatch.setattr(api, "_master_derived_tasks", lambda: [])

    tasks, assumptions = api._enrich_extra_tasks()
    assert tasks[0].teacher == "指定老师"
    assert tasks[0].room == "B999"
    assert assumptions == []


def test_enrich_twice_still_reports_assumptions(monkeypatch):
    """回归：总览接口先补全过一次，solve 再调时假设不能静默消失。

    任务对象在会话内共享，第一次补全把教师填进去了；如果第二次调用只看
    "字段是否为空"，就会把系统自己做的决定当成用户给的 —— 假设报告丢失
    （灰箱实测踩过：solve 的 assumptions 里只剩求解层默认值）。
    """
    db = ScheduleDatabase()
    db.entries = []
    db.pending = []
    db.loaded = True
    monkeypatch.setattr(api, "_db", db)
    monkeypatch.setattr(api, "_master_tasks", [
        PendingTask("灰箱班", "扭摆法", "1组"),
        PendingTask("灰箱班", "扭摆法", "2组"),
    ])
    monkeypatch.setattr(api, "_master_data", _md_with_pool())
    monkeypatch.setattr(api, "_master_derived_tasks", lambda: [])

    _, first = api._enrich_extra_tasks()
    assert any("自动分配" in a for a in first)
    # 第二次调用（模拟另一个接口先跑过）：字段已满，但假设必须仍在
    tasks, second = api._enrich_extra_tasks()
    assert all(t.teacher for t in tasks)
    assert len([a for a in second if "自动分配" in a]) == 2


def test_enrich_reports_when_no_pool(monkeypatch):
    """主数据里也没有教师 → 不编造，任务教师留空（求解器会在假设里报告）。"""
    db = ScheduleDatabase()
    db.entries = []
    db.pending = []
    db.loaded = True
    monkeypatch.setattr(api, "_db", db)
    monkeypatch.setattr(api, "_master_tasks", [PendingTask("灰箱班", "全新无主项目", "1组")])
    md = MasterData()
    md.build_from_entries([])
    md.upsert_experiment("全新无主项目")  # 有项目但没登记任何教师
    monkeypatch.setattr(api, "_master_data", md)

    tasks, assumptions = api._enrich_extra_tasks()
    assert tasks[0].teacher == ""


# ---------------------------------------------------------------- 求解层假设

def _graybox_db():
    db = ScheduleDatabase()
    db.entries = [
        _entry(1, "周四", "567", "一班", "1组", "A410", experiment="扭摆法"),
        _entry(2, "周四", "567", "一班", "1组", "A410", experiment="声速测量"),
    ]
    db.loaded = True
    return db


def test_solve_reports_graybox_defaults(monkeypatch):
    """不给星期/节次/周次 → 排得出，且每个默认决定都出现在 assumptions 里。"""
    from app.engine.parallel_scheduler import ParallelScheduler

    db = _graybox_db()
    ps = ParallelScheduler(db, extra_tasks=[
        PendingTask("一班", "牛顿环", "2组", teacher="吴老师", room="A411"),
    ])
    r = ps.solve("一班", start_week=1, weeks=8)  # 全部不传：weekday/time_slots/week_max_override
    assert r["success"] is True
    a = "\n".join(r["assumptions"])
    assert "周四" in a                    # 星期默认（历史里一班全是周四 → 推断口径）
    assert "567/678/91011" in a           # 节次默认三段
    assert "周次上限" in a                # 周次默认表内最后一周
    # 排出来的单元教师齐备
    assert all(s["teacher"] for s in r["schedule"])


def test_solve_reports_missing_group_and_teacher(monkeypatch):
    """连组别和教师都没给 → 按 1 组保守互斥排课；教师空缺如实报告，不留静默空白。"""
    from app.engine.parallel_scheduler import ParallelScheduler

    db = ScheduleDatabase()
    db.entries = [
        _entry(1, "周四", "567", "一班", "1组", "A410", experiment="扭摆法"),
        _entry(4, "周四", "567", "一班", "1组", "A410", experiment="声速测量"),
        _entry(7, "周四", "567", "一班", "1组", "A410", experiment="牛顿环"),
    ]
    db.loaded = True
    ps = ParallelScheduler(db, extra_tasks=[
        PendingTask("一班", "密立根油滴", ""),  # 无组别、无教师
    ])
    r = ps.solve("一班", weekday="周四", start_week=1, weeks=8,
                 time_slots=["567"], week_max_override=8)
    assert r["success"] is True
    a = "\n".join(r["assumptions"])
    assert "按 1 组处理" in a
    assert "没有教师信息" in a
    # 该单元确实被排出来了（教师列为空但课排上了 —— 灰箱下先有课，教师后补）
    unit = [s for s in r["schedule"] if s["project"] == "密立根油滴"]
    assert len(unit) == 1 and unit[0]["teacher"] == ""


def test_enriched_tasks_flow_into_solve(monkeypatch):
    """端到端：API 层补全教师后，求解结果里不再是空教师（灰箱班的课表可导出）。"""
    db = ScheduleDatabase()
    db.entries = []
    db.pending = []
    db.loaded = True
    monkeypatch.setattr(api, "_db", db)
    monkeypatch.setattr(api, "_master_tasks", [
        PendingTask("灰箱班", "扭摆法", "1组"),
        PendingTask("灰箱班", "扭摆法", "2组"),
    ])
    monkeypatch.setattr(api, "_master_data", _md_with_pool())

    tasks, _ = api._enrich_extra_tasks()
    from app.engine.parallel_scheduler import ParallelScheduler
    ps = ParallelScheduler(db, extra_tasks=tasks)
    r = ps.solve("灰箱班", start_week=1, weeks=8)
    assert r["success"] is True
    assert r["scheduled"] == 2
    assert all(s["teacher"] in {"吴老师", "林老师"} for s in r["schedule"])
