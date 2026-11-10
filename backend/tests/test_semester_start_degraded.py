"""R98：学期起始日读取失败，不许静默换一套日期。

动手前只读探针（真实 demo 表）：
    真实学期起始日 = 2025-02-17，兜底常量 SEMESTER_START = 2025-02-24。
    `get_semester_start_date()` 抛异常时 `_get_semester_start()` 回退常量，
    第 3 周周一的 `date` 从「03月03日」变成「03月10日」（整整差 7 天），
    而返回体里没有任何标记 —— 前端 RescheduleView 又真的把
    `availSlot.date` 渲染上屏，教秘会照着错的日期去调课。

与 R94/R97 同族：判据必须是**真的读失败**，不是"表里没写起始日"。
没写时回退常量是设计（无基准可用），不能狼来了。
"""
import os
import sys
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.agent.reschedule_agent import SEMESTER_START, RescheduleAgent, week_to_date
from app.utils.excel_parser import ScheduleDatabase

REAL = os.path.join(os.path.dirname(__file__), "..", "..", "schedule.demo.xlsx")
BROKEN = RuntimeError("workbook 损坏")


@pytest.fixture(scope="module")
def db():
    if not os.path.isfile(REAL):
        pytest.skip("缺少 schedule.demo.xlsx")
    d = ScheduleDatabase()
    d.load_from_excel(REAL)
    return d


def _db_with(db, *, semester=None, raises=None):
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


def test_broken_semester_start_is_structurally_reported(db):
    """读失败 → 顶层 + 每条 slot 都带信号，且 date 确实是兜底常量算出来的。"""
    normal = RescheduleAgent(db).find_slots_with_reasons(3)
    assert normal["date_degraded"] is False
    assert normal["date_degraded_detail"] is None
    assert normal["available"]
    assert all(s.get("date_degraded") is False for s in normal["available"])
    good_slot = normal["available"][0]

    degraded = RescheduleAgent(_db_with(db, raises=BROKEN)).find_slots_with_reasons(3)
    assert degraded["date_degraded"] is True
    detail = degraded["date_degraded_detail"]
    assert isinstance(detail, str) and "RuntimeError" in detail and "workbook" in detail
    assert degraded["available"]
    bad_slot = degraded["available"][0]
    # 占用计算没变，变的只有日期基准
    assert (bad_slot["weekday"], bad_slot["time_slot"], bad_slot["room"]) == (
        good_slot["weekday"], good_slot["time_slot"], good_slot["room"])
    assert bad_slot["date_degraded"] is True
    assert bad_slot["date"] != good_slot["date"]
    assert bad_slot["date"] == week_to_date(3, bad_slot["weekday"], SEMESTER_START)
    assert all(s.get("date_degraded") is True for s in degraded["available"])
    assert all(s.get("date_degraded") is True for s in degraded["excluded"])


def test_exception_never_bubbles_but_is_not_swallowed(db):
    """异常必须被兜住（旧行为是 500/冒到上层），但不能兜完就当没事。"""
    agent = RescheduleAgent(_db_with(db, raises=BROKEN))
    result = agent.find_slots_with_reasons(3)
    assert result["date_degraded"] is True
    assert agent._semester_start_cache == SEMESTER_START


def test_missing_semester_start_is_not_a_failure(db):
    """表里没写起始日 → 设计回退常量，不算故障（防放宽：不许狼来了）。"""
    agent = RescheduleAgent(_db_with(db, semester=None))
    result = agent.find_slots_with_reasons(3)
    assert result["date_degraded"] is False
    assert result["date_degraded_detail"] is None
    assert result["available"]
    s0 = result["available"][0]
    assert s0["date"] == week_to_date(3, s0["weekday"], SEMESTER_START)
    assert all(s.get("date_degraded") is False for s in result["available"])


def test_unloaded_db_keeps_quiet_and_keeps_the_keys(db):
    """未加载时早返回也要给同形的结构化字段，不能让下游拿到 None。"""
    empty = ScheduleDatabase()
    empty.loaded = False
    result = RescheduleAgent(empty).find_slots_with_reasons(3)
    assert result["available"] == []
    assert result["date_degraded"] is False
    assert result["date_degraded_detail"] is None


def test_empty_detail_must_not_be_reported_as_degraded(db):
    """防放宽（规则 22）：空串 detail 不是「有故障」，判据不许用 truthy/`is not None` 偷懒。"""
    agent = RescheduleAgent(db)
    agent._semester_start_cache = SEMESTER_START
    agent._semester_start_degraded = ""  # 伪装成空结果
    result = agent.find_slots_with_reasons(3)
    assert result["date_degraded"] is False
    assert result["date_degraded_detail"] is None


def test_signal_persists_while_fallback_is_cached(db):
    """缓存里就是兜底日期，后续每次都得继续报，不能第一次报完就抹掉（规则 29）。"""
    agent = RescheduleAgent(_db_with(db, raises=BROKEN))
    first = agent.find_slots_with_reasons(3)
    second = agent.find_slots_with_reasons(4)
    assert second["date_degraded"] is True
    assert second["date_degraded_detail"] == first["date_degraded_detail"]
    assert agent._semester_start_degraded


def test_process_request_slots_carry_the_signal(db):
    """信号必须穿过 process_request 的 available_slots，不能只停在 agent 内部。"""
    agent = RescheduleAgent(_db_with(db, raises=BROKEN),
                            llm_parser=MagicMock(configured=False))
    result = agent.process_request("我是吴琳，把声速测量调到第3周", False)
    assert result["success"] is True
    assert result["available_slots"]
    assert all(s.get("date_degraded") is True for s in result["available_slots"])


def test_chat_http_exposes_date_degraded(db, monkeypatch):
    """端到端：POST /chat 的 data.available_slots 里真的带标记（规则 24）。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api import reschedule as api

    async def _noop():
        return None

    monkeypatch.setattr(api, "_ensure_loaded_async", _noop, raising=False)
    monkeypatch.setattr(
        api, "_agent",
        RescheduleAgent(_db_with(db, raises=BROKEN),
                        llm_parser=MagicMock(configured=False)),
        raising=False,
    )
    app = FastAPI()
    app.include_router(api.router)
    client = TestClient(app)
    resp = client.post(
        "/chat", json={"message": "我是吴琳，把声速测量调到第3周", "fast": True})
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    slots = body["data"]["available_slots"]
    assert slots
    assert all(s.get("date_degraded") is True for s in slots)
    assert body["data"].get("llm_error") is False  # 不能顺手误报 LLM 故障
