"""R99：/impact-analysis 的冲突明细不许把 date_degraded 丢在复制路上。

动手前只读探针（真实 demo 表）：R98 给 find_slots_with_reasons 的
available / excluded 每条都加了 date_degraded，但 /impact-analysis 在
insights.py 里**重新构造** conflict_slots 字典时只复制了 6 个字段，把标记
漏掉了。实测起始日读失败时冲突明细的 date 从「03月03日」变成「03月10日」
（整整差 7 天），而 RescheduleView 又把 c.date 渲染进「（{c.date}）」——
教秘看冲突明细时同样会照着错日期去调课。

本轮要求：顶层 data + 每条 conflict_slot 都带结构化信号。判据沿用 R98：
只有**真的读失败**才置位；表里没写起始日（返回 None）是设计回退，不许狼来了。
"""
import asyncio
import inspect
import os
import sys
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.agent.reschedule_agent import SEMESTER_START, RescheduleAgent, week_to_date
from app.api import insights
from app.api import reschedule as api
from app.utils.excel_parser import ScheduleDatabase

REAL = os.path.join(os.path.dirname(__file__), "..", "..", "schedule.demo.xlsx")
BROKEN = RuntimeError("workbook 损坏")
MESSAGE = "我是吴琳，把大学物理实验调到第3周"


@pytest.fixture(scope="module")
def db():
    if not os.path.isfile(REAL):
        pytest.skip("缺少 schedule.demo.xlsx")
    d = ScheduleDatabase()
    d.load_from_excel(REAL)
    return d


def _fake_db(db, *, raises=None, semester=None):
    """复刻真实 db 的只读面，只把「读学期起始日」换成指定行为。"""
    fake = MagicMock()
    fake.loaded = True
    fake.entries = db.entries
    fake.get_all_rooms = db.get_all_rooms
    fake.get_all_teachers = db.get_all_teachers
    fake.get_week_range = db.get_week_range
    if raises is not None:
        fake.get_semester_start_date = MagicMock(side_effect=raises)
    else:
        fake.get_semester_start_date = MagicMock(return_value=semester)
    return fake


def _impact(agent, monkeypatch):
    async def _noop():
        return None

    monkeypatch.setattr(api, "_ensure_loaded_async", _noop, raising=False)
    monkeypatch.setattr(api, "_agent", agent, raising=False)
    out = asyncio.run(insights.impact_analysis(api.ChatRequest(message=MESSAGE, fast=True)))
    assert out["success"] is True
    return out["data"]


def test_broken_semester_start_marks_top_level(db, monkeypatch):
    """顶层 data 必须带结构化信号，不能只塞在明细里（明细可能被截断）。"""
    data = _impact(RescheduleAgent(_fake_db(db, raises=BROKEN)), monkeypatch)
    assert data["date_degraded"] is True
    detail = data["date_degraded_detail"]
    assert isinstance(detail, str) and "workbook 损坏" in detail


def test_broken_semester_start_marks_every_conflict_slot(db, monkeypatch):
    """标记与 date 并列放进每条冲突明细，前端渲染 date 时才看得见警告。"""
    data = _impact(RescheduleAgent(_fake_db(db, raises=BROKEN)), monkeypatch)
    slots = data["conflict_slots"]
    assert slots
    assert all(s.get("date_degraded") is True for s in slots)


def test_broken_conflict_dates_really_come_from_fallback_constant(db, monkeypatch):
    """偏移不是断言写错：明细里的 date 必须等于用兜底常量算出来的值。"""
    normal = _impact(
        RescheduleAgent(_fake_db(db, semester=db.get_semester_start_date())), monkeypatch)
    broken = _impact(RescheduleAgent(_fake_db(db, raises=BROKEN)), monkeypatch)
    assert normal["conflict_slots"]
    for a, b in zip(normal["conflict_slots"], broken["conflict_slots"]):
        assert a["date"] != b["date"], "起始日读失败后日期竟然没变，探针可能没命中"
        assert b["date"] == week_to_date(3, b["weekday"], SEMESTER_START)


def test_normal_semester_start_is_quiet(db, monkeypatch):
    """正常读取不许误报（R86–R98 同款口径）。"""
    data = _impact(
        RescheduleAgent(_fake_db(db, semester=db.get_semester_start_date())), monkeypatch)
    assert data["date_degraded"] is False
    assert data["date_degraded_detail"] is None
    assert all(s.get("date_degraded") is False for s in data["conflict_slots"])


def test_missing_semester_start_is_not_a_failure(db, monkeypatch):
    """表里没写起始日 → 返回 None 是设计回退，不是故障。"""
    data = _impact(RescheduleAgent(_fake_db(db, semester=None)), monkeypatch)
    assert data["date_degraded"] is False
    assert data["date_degraded_detail"] is None
    assert all(s.get("date_degraded") is False for s in data["conflict_slots"])


def test_legacy_excluded_without_the_flag_stays_quiet(db, monkeypatch):
    """防放宽：老形状 result（顶层无字段）+ truthy 字符串都不算故障，也不许炸。"""
    agent = RescheduleAgent(_fake_db(db, semester=db.get_semester_start_date()))
    real = agent.find_slots_with_reasons

    def legacy(week, teacher_name=None):
        r = real(week, teacher_name=teacher_name)
        return {
            "available": r["available"],
            "excluded": [dict(e, date_degraded="RuntimeError") for e in r["excluded"]],
        }

    monkeypatch.setattr(agent, "find_slots_with_reasons", legacy)
    data = _impact(agent, monkeypatch)
    assert data["date_degraded"] is False
    assert data["date_degraded_detail"] is None
    assert all(s.get("date_degraded") is False for s in data["conflict_slots"])


def test_impact_analysis_http_exposes_date_degraded(db, monkeypatch):
    """端到端（规则 24）：POST /impact-analysis 的 data 里真带信号，没被响应模型吃掉。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    async def _noop():
        return None

    monkeypatch.setattr(api, "_ensure_loaded_async", _noop, raising=False)
    monkeypatch.setattr(
        api, "_agent", RescheduleAgent(_fake_db(db, raises=BROKEN)), raising=False)
    app = FastAPI()
    app.include_router(insights.router)
    client = TestClient(app)
    resp = client.post("/impact-analysis", json={"message": MESSAGE, "fast": True})
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    data = body["data"]
    assert data["date_degraded"] is True
    assert data["conflict_slots"]
    assert all(s.get("date_degraded") is True for s in data["conflict_slots"])


def test_insights_source_copies_the_flag():
    """删型变异守卫：复制路径必须显式写出 date_degraded（规则 27）。"""
    src = inspect.getsource(insights.impact_analysis)
    assert "conflict_slots.append" in src
    assert '"date_degraded"' in src
    assert '"date_degraded_detail"' in src
