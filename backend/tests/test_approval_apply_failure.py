# -*- coding: utf-8 -*-
"""R85：审批自动应用调课**失败**时，不许谎报「未找到可自动移动的记录」。

探针实测（2026-11-07 00:35，`_r85_probe.py`，不改源码）
--------------------------------------------------------
令 `_apply_reschedule` 抛异常（模拟账本损坏 / SQLite 写入失败），
`approve_request` 的 `except` 分支把 apply_result 置成 applied=0/skipped=0，
于是落到末尾 else 说「未找到可自动移动的记录，请在教务系统中人工修改」，
details 里却又写着「自动应用失败：…」：

    ✅ 已同意调课申请（未找到可自动移动的记录，请在教务系统中人工修改）
    自动应用失败：模拟：SQLite 写入失败 / 账本损坏（可人工在教务系统调整）

而「真的查无记录」时返回的第一句**一模一样**。系统一条都没查过，
却让教秘以为查过了、去教务系统再改一遍；工单此时已是 approved。

修复：`except` 分支给结构化信号 `apply_failed`，message 分支单列失败文案，
与「真的查无记录」分开 —— 不许靠中文文案反解状态（同 R82 的 already_applied）。

本文件只断言用户可见结果与状态，不改库层、不动既有分支语义。
"""

import pytest
from fastapi.testclient import TestClient

from app import security as _security
from app.api import reschedule as R
from app.main import app
from app.utils.excel_parser import ScheduleDatabase, ScheduleEntry

TEACHER = "吴琳"
COURSE = "扭摆法测物体转动惯量"
CLASS = "2024级应用化学1、2班"
ORIGINAL = "周二 13:30-15:45"
BOOM_TEXT = "模拟：SQLite 写入失败 / 账本损坏"


def _table():
    d = ScheduleDatabase()
    d.entries = [
        ScheduleEntry(3, "周二", "567", CLASS, "24应化2_1", COURSE, "A504", TEACHER),
        ScheduleEntry(5, "周二", "567", CLASS, "24应化2_1", COURSE, "A504", TEACHER),
    ]
    d.loaded = True
    return d


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    """TestClient 的 lifespan 会先跑，所以 `_db` 必须在它启动之后再注入。"""
    with TestClient(app) as client:
        d = _table()
        monkeypatch.setattr(R, "_db", d)
        monkeypatch.setattr(R, "_DB_PATH", str(tmp_path / "t.db"))
        R._init_db()
        login = client.post(
            "/api/reschedule/admin-login",
            json={"username": _security.ADMIN_USER, "password": _security.ADMIN_PASS},
        )
        assert login.status_code == 200, login.text
        yield client, {"Authorization": "Bearer " + login.json()["token"]}, d


def _payload(**kw):
    body = {"teacher": TEACHER, "course_name": COURSE, "class_name": CLASS,
            "original_time": ORIGINAL, "target_week": 7}
    body.update(kw)
    return body


def _create(ctx, **kw):
    client, h, _ = ctx
    r = client.post("/api/reschedule/requests", headers=h, json=_payload(**kw))
    assert r.status_code == 200, r.text
    return r.json()["request"]["id"]


def _approve(ctx, rid, comment="同意", approve=True):
    client, h, _ = ctx
    return client.post(f"/api/reschedule/requests/{rid}/approve",
                       headers=h, json={"approve": approve, "comment": comment})


def _with_boom(monkeypatch):
    """让自动应用调课在线程池里崩掉，模拟账本损坏 / 写库失败。"""
    def boom(*a, **kw):
        raise RuntimeError(BOOM_TEXT)

    monkeypatch.setattr(R, "_apply_reschedule", boom)


def _first_line(message):
    return message.splitlines()[0]


# ============ 1. 会先红的用例：失败不许伪装成「查无记录」 ============

def test_apply_failure_first_line_does_not_claim_no_matching_record(ctx, monkeypatch):
    """调用崩了 ≠ 查过没找到。第一句不许说「未找到可自动移动的记录」。"""
    _with_boom(monkeypatch)
    body = _approve(ctx, _create(ctx)).json()
    assert "未找到可自动移动的记录" not in _first_line(body["message"]), body["message"]


def test_apply_failure_first_line_says_failed_not_success(ctx, monkeypatch):
    """失败要说失败，也不许打「✅」—— 别让 details 里的真话替主句圆谎。"""
    _with_boom(monkeypatch)
    message = _approve(ctx, _create(ctx)).json()["message"]
    first = _first_line(message)
    assert "失败" in first, first
    assert "✅" not in first, first


# ============ 2. 防放宽：修复不许把别的话也说没了 ============

def test_apply_failure_keeps_exception_reason(ctx, monkeypatch):
    """具体异常原因必须留在返回体里，方便教秘/管理员定位。"""
    _with_boom(monkeypatch)
    message = _approve(ctx, _create(ctx)).json()["message"]
    assert BOOM_TEXT in message, message
    assert "自动应用失败" in message, message


def test_apply_failure_still_approves_ticket(ctx, monkeypatch):
    """行为不变：审批仍成功，工单仍 approved，失败只体现在文案上。"""
    _with_boom(monkeypatch)
    body = _approve(ctx, _create(ctx)).json()
    assert body["success"] is True
    assert body["request"]["status"] == "approved"


def test_apply_failure_does_not_claim_records_moved(ctx, monkeypatch):
    """一条都没移动，不许说「已同意并自动应用调课 N 条」。"""
    _with_boom(monkeypatch)
    message = _approve(ctx, _create(ctx)).json()["message"]
    assert "已同意并自动应用调课" not in message, message
    assert "已移动" not in message, message


def test_normal_approval_message_unchanged(ctx):
    """正常应用成功时文案一字不改（防把失败分支接在最前面误伤）。"""
    message = _approve(ctx, _create(ctx)).json()["message"]
    assert "已同意并自动应用调课" in message, message
    assert "失败" not in _first_line(message), message


def test_real_no_match_still_says_no_record(ctx):
    """真的查无记录时仍要说「未找到可自动移动的记录」—— 那句是真话。"""
    message = _approve(ctx, _create(ctx, teacher="查无此人")).json()["message"]
    assert "未找到可自动移动的记录" in _first_line(message), message
    assert "自动应用调课失败" not in message, message


def test_unloaded_db_still_says_data_not_loaded(ctx, monkeypatch):
    """R81 的未加载降级实话不许被失败分支吞掉。"""
    monkeypatch.setattr(R._db, "loaded", False)
    message = _approve(ctx, _create(ctx)).json()["message"]
    assert "排课数据尚未加载" in message, message


def test_already_applied_still_says_no_action(ctx, monkeypatch):
    """R83 的幂等命中实话不许被失败分支吞掉。"""
    monkeypatch.setattr(
        R, "_apply_reschedule",
        lambda rec, persist=True, idem_key="": {
            "applied": 0, "skipped": 0, "already_applied": True,
            "details": ["该申请已于 2026-11-06 应用过，本次未重复移动"],
        },
    )
    message = _approve(ctx, _create(ctx)).json()["message"]
    assert "无需人工处理" in message or "此前已应用过" in message, message
    assert "失败" not in _first_line(message), message


def test_reject_message_unchanged(ctx):
    """驳回不调自动应用，文案不许被改动。"""
    message = _approve(ctx, _create(ctx), approve=False, comment="时间冲突").json()["message"]
    assert "已驳回" in message and "时间冲突" in message, message
