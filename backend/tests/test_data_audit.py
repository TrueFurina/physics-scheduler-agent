"""数据体检（/data-audit）语义回归 —— 没填场地 ≠ 同室冲突

历史缺陷
--------
`data_audit()` 无条件把每条记录塞进 `room_map[(week, weekday, slot, e.room)]`。
真实安排表里有 16 条「绪论课（教室已安排，请查系统）」，`room` 是空串 ——
它们被归到同一个 key `(…, '')`，只要两条以上就报成：

    教室冲突（同室同段多班）  第1周 周五 18:30-20:45 ·        ← 场地是空的

后果有两个，方向相反但都致命：
1. **假阳性**：没填场地不可能撞场地。真实数据里唯一的「教室冲突」100% 是这一条，
   教秘看到的第一条体检结论就是错的；
2. **真信号被吃掉**：这 16 条确实没被教室冲突检查覆盖，本该作为「数据缺口」提示，
   结果被伪装成一个冲突 —— 修掉假阳性后如果不补这个计数，就变成"静默少扫 16 条"。

所以这里同时锁两件事：无场地不进冲突；无场地要单独计数。
"""

import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import app.api.reschedule as api
from app.utils.excel_parser import ScheduleDatabase, ScheduleEntry

WEEK = 1
NO_ROOM_EXPERIMENT = "绪论课（教室已安排，请查系统）"


def _entry(weekday, slot, cls, group, room, teacher="林老师", experiment="伏安法"):
    return ScheduleEntry(WEEK, weekday, slot, cls, group, experiment, room, teacher)


@pytest.fixture()
def make_db(monkeypatch):
    """按需替换模块级单例：每个用例自己决定这张表长什么样。"""

    def _make(entries):
        db = ScheduleDatabase()
        db.entries = entries
        db.loaded = True
        monkeypatch.setattr(api, "_db", db)
        return db

    return _make


def _audit() -> dict:
    out = asyncio.run(api.data_audit())
    assert out["success"] is True
    return out["data"]


def test_roomless_entries_never_become_room_conflicts(make_db):
    """两条都没填场地、又落在同一时段 —— 这不是同室冲突。"""
    make_db([
        _entry("周五", "91011", "24级机器人工程", "1和2", "", experiment=NO_ROOM_EXPERIMENT),
        _entry("周五", "91011", "24级机械电子工程", "1和2", "", experiment=NO_ROOM_EXPERIMENT),
    ])
    data = _audit()
    assert data["room_conflict_count"] == 0, \
        "没填场地的两条被报成了教室冲突 —— 空串被当成了一个真实的场地名"
    assert data["missing_room_count"] == 2
    assert data["missing_room_samples"], "缺口要给出样例，否则教秘不知道该查哪几条"


def test_real_room_conflict_still_detected_and_has_place(make_db):
    """有场地的真冲突不能被漏掉；而且 place 必须非空（界面上是"· {place}"）。"""
    make_db([
        _entry("周一", "567", "24电子1班", "1组", "A101"),
        _entry("周一", "567", "24电子2班", "2组", "A101"),
        _entry("周一", "567", "24软件1班", "1组", ""),   # 同段但无场地：不得参与
    ])
    data = _audit()
    assert data["room_conflict_count"] == 1
    conflict = data["room_conflicts"][0]
    assert conflict["place"] == "A101"
    assert sorted(conflict["involved"]) == ["24电子1班/1组", "24电子2班/2组"]
    # 无场地那条不能混进这个冲突的当事人里
    assert all("24软件1班" not in p for p in conflict["involved"])
    assert data["missing_room_count"] == 1


def test_conflict_free_but_incomplete_is_not_called_healthy_complete(make_db):
    """没有冲突 ≠ 数据齐全：healthy 为真时仍要能看出缺口（前端据此报"🟡"而不是"✅"）。"""
    make_db([
        _entry("周二", "12", "24电子1班", "1组", ""),          # 无场地，且该时段只有它
        _entry("周三", "34", "24电子2班", "2组", "A102"),      # 正常
    ])
    data = _audit()
    assert data["room_conflict_count"] == 0
    assert data["teacher_conflict_count"] == 0
    assert data["healthy"] is True, "没有冲突时 healthy 仍应为真（向后兼容）"
    assert data["missing_room_count"] == 1, "healthy 为真不代表数据齐全"


def test_fully_clean_schedule_is_healthy_with_no_gap(make_db):
    make_db([
        _entry("周一", "567", "24电子1班", "1组", "A101", teacher="陈老师"),
        _entry("周二", "567", "24电子2班", "2组", "A102", teacher="林老师"),
    ])
    data = _audit()
    assert data["healthy"] is True
    assert data["room_conflict_count"] == 0
    assert data["missing_room_count"] == 0
    assert data["room_count"] == 2
    assert data["teacher_count"] == 2
    assert data["week_min"] == WEEK and data["week_max"] == WEEK
