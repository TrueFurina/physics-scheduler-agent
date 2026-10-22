"""全校一键（R-60）：逐班·按天、组级排课

为什么需要
----------
`/parallel/solve` 是**单班**一键：17 个班要点 17 次，不是"一键全面排课"。
但直接把 17 个班塞进一次 solve 不行 —— 单班求解只避开 `db.entries` 里其他班的
占用，看不见本次刚给别的班排出来的格子，两个班就会把同一间教室排到同一格。

另一个真实约束：班级时间范围常跨 2–3 天（24级电子信息工程 周日 24 节 + 周六
18 节）。若压成"单一星期"，45 个单元在 11 周里怎么都排不下（实测 7 个班失败）。
这里按**历史星期**把单元分到各天，再逐天求解（编排层切分，不动 CP-SAT 模型）。
"""

import asyncio
import os
import sys
from collections import Counter

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.engine.parallel_scheduler import ParallelScheduler
from app.master_data import MasterData
from app.utils.excel_parser import ScheduleDatabase, ScheduleEntry

import app.api.reschedule as api

REAL_XLSX = os.path.join(os.path.dirname(__file__), "..", "..", "schedule.xlsx")


def _entry(week, weekday, slot, cls, group, room, teacher, experiment):
    return ScheduleEntry(week, weekday, slot, cls, group, experiment, room, teacher)


def _db(entries):
    d = ScheduleDatabase()
    d.entries = list(entries)
    d.skipped = []
    d.loaded = True
    return d


# ---------------------------------------------------------------- 按天切分

def test_multi_day_class_units_go_to_their_historical_day():
    """一个班跨两天：项目A 历史在周一、项目B 在周二 → 各回各的天，且全部排定。"""
    entries = []
    for w in range(1, 5):
        entries += [
            _entry(w, "周一", "567", "跨天班", "1组", "A410", "吴老师", "项目A"),
            _entry(w, "周二", "567", "跨天班", "1组", "A411", "吴老师", "项目B"),
        ]
    ps = ParallelScheduler(_db(entries))
    r = ps.solve_school([{
        "class_name": "跨天班", "weekdays": ["周一", "周二"],
        "start_week": 1, "weeks": 4, "time_slots": ["567", "678"], "week_max": 4,
    }])

    assert r["success"] is True
    assert r["classes_ok"] == 1
    assert r["scheduled"] == 2  # 两个单元
    by_project = {s["project"]: s["weekday"] for s in r["schedule"]}
    assert by_project["项目A"] == "周一"
    assert by_project["项目B"] == "周二"


def test_single_day_class_unchanged():
    """单天班行为不变（向后兼容）。"""
    entries = [_entry(w, "周四", "567", "单天班", "1组", "A410", "吴老师", "项目A")
               for w in range(1, 5)]
    ps = ParallelScheduler(_db(entries))
    r = ps.solve_school([{
        "class_name": "单天班", "weekday": "周四", "weekdays": ["周四"],
        "start_week": 1, "weeks": 4, "time_slots": ["567"], "week_max": 4,
    }])
    assert r["classes_ok"] == 1
    assert {s["weekday"] for s in r["schedule"]} == {"周四"}


# ---------------------------------------------------------------- 跨班互斥

def test_classes_do_not_double_book_room_or_teacher():
    """两个班共用一位教师/一间教室：全校求解后同格不得重复（这是单班相加必踩的坑）。"""
    entries = []
    for w in range(1, 5):
        entries += [
            _entry(w, "周一", "567", "甲班", "1组", "A410", "吴老师", "项目A"),
            _entry(w, "周一", "567", "乙班", "1组", "A410", "吴老师", "项目A"),
        ]
    ps = ParallelScheduler(_db(entries))
    r = ps.solve_school([
        {"class_name": "甲班", "weekdays": ["周一"], "start_week": 1, "weeks": 4,
         "time_slots": ["567", "678"], "week_max": 4},
        {"class_name": "乙班", "weekdays": ["周一"], "start_week": 1, "weeks": 4,
         "time_slots": ["567", "678"], "week_max": 4},
    ])
    assert r["classes_ok"] == 2
    rooms = Counter((s["week"], s["weekday"], s["time_slot"], s["room"]) for s in r["schedule"])
    teachers = Counter((s["week"], s["weekday"], s["time_slot"], s["teacher"]) for s in r["schedule"])
    assert max(rooms.values()) == 1, "两个班被排进同一间教室同一格"
    assert max(teachers.values()) == 1, "同一位教师被排到同一格的两个班"


# ---------------------------------------------------------------- API

@pytest.fixture
def api_env(monkeypatch):
    entries = []
    for w in range(1, 5):
        entries += [
            _entry(w, "周一", "567", "甲班", "1组", "A410", "吴老师", "项目A"),
            _entry(w, "周一", "678", "乙班", "1组", "A411", "林老师", "项目B"),
        ]
    db = _db(entries)
    md = MasterData()
    md.build_from_entries(entries)
    monkeypatch.setattr(api, "_db", db)
    monkeypatch.setattr(api, "_master_data", md)
    monkeypatch.setattr(api, "_master_tasks", [])
    monkeypatch.setattr(api, "_student", api.StudentSchedule())
    return db, md


def test_api_solve_all_returns_group_level_plan(api_env):
    out = asyncio.run(api.parallel_solve_all())
    assert out["success"] is True
    assert out["classes_total"] == 2 and out["classes_ok"] == 2
    assert out["scheduled"] >= 2
    row = out["schedule"][0]
    assert {"project", "group", "week", "weekday", "time_slot", "room", "teacher",
            "class_name"} <= set(row)


def test_api_solve_all_reports_failed_classes_honestly(monkeypatch):
    """排不下的班必须出现在 classes_failed 里（含原因），不能整体显示"成功"就完事。"""
    # 一周只有 1 个格子、却要排 6 个单元
    entries = [_entry(1, "周一", "567", "挤不下班", f"{i}组", "A410", "吴老师", f"项目{i}")
               for i in range(6)]
    db = _db(entries)
    md = MasterData()
    md.build_from_entries(entries)
    monkeypatch.setattr(api, "_db", db)
    monkeypatch.setattr(api, "_master_data", md)
    monkeypatch.setattr(api, "_master_tasks", [])
    monkeypatch.setattr(api, "_student", api.StudentSchedule())

    out = asyncio.run(api.parallel_solve_all())
    # 要么排下（多周窗口）要么如实报失败，但绝不能静默少排
    if out["classes_ok"] == 0:
        assert out["classes_failed"] and out["classes_failed"][0]["error"]
    else:
        assert out["scheduled"] == out["units"]


@pytest.mark.skipif(not os.path.exists(REAL_XLSX), reason="真实排课表不在仓库里")
def test_real_table_school_plan_covers_all_classes():
    """真实表：17 个班全覆盖、跨班零撞车（约 15 秒）。"""
    db = ScheduleDatabase()
    db.load_from_excel(os.path.abspath(REAL_XLSX))
    md = MasterData().build_from_entries(db.entries)
    specs = [{
        "class_name": name,
        "weekdays": sorted(c.get("weekdays") or []) or None,
        "weekday": c.get("weekday") or None,
        "start_week": c.get("week_min") or 2,
        "weeks": 10,
        "time_slots": sorted(c.get("time_slots") or []) or None,
        "week_max": c.get("week_max"),
    } for name, c in sorted(md.classes.items())]

    r = ParallelScheduler(db).solve_school(specs)
    assert r["classes_ok"] == r["classes_total"] == len(specs)
    assert r["scheduled"] > 300
    rooms = Counter((s["week"], s["weekday"], s["time_slot"], s["room"]) for s in r["schedule"])
    teachers = Counter((s["week"], s["weekday"], s["time_slot"], s["teacher"]) for s in r["schedule"])
    assert max(rooms.values()) == 1 and max(teachers.values()) == 1
