# -*- coding: utf-8 -*-
"""R83：演示模式审批路径必须与 `/apply-move` 共用同一套幂等语义。

探针实测（2026-11-06，本机 `_r83_probe.py`，不改源码）
------------------------------------------------------
A. 两张内容完全相同的独立工单先后批准：第二次**真的去匹配了第 4 周**
   （比第一次的源周更早一周），只是恰好撞核心冲突没动；message 却说
   「请在教务系统中人工协调 / 必须调整后再申请」。账本只有一行 `idem_key=''`。
B. 同一调课内容先走 `/apply-move`（R82 已写 `auto:a41327f9…`），再走本地
   工单审批：审批不带键，**绕过已有账本查重**，同样去撬第 4 周、同样谎报。

根因：R82 把规范化内容指纹只做在 `/apply-move` 端点里，而
`requests_board.approve_request` 仍调 `_apply_reschedule(record_dict)`
（不带键），也完全不认 R82 新增的 `already_applied` 结构化信号。

本文件只断言用户可见结果 + 结构化契约：message、账本、课表周次、以及传给
`_apply_reschedule` 的幂等键。库层「空串互不冲突」的显式约定一字未动。
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
SIX_FIELDS = ("teacher", "course_name", "class_name",
              "original_time", "target_week", "source_week")


def _table():
    d = ScheduleDatabase()
    d.entries = [
        ScheduleEntry(3, "周二", "567", CLASS, "24应化2_1", COURSE, "A504", TEACHER),
        ScheduleEntry(4, "周二", "567", CLASS, "24应化2_1", COURSE, "A504", TEACHER),
        ScheduleEntry(5, "周二", "567", CLASS, "24应化2_1", COURSE, "A504", TEACHER),
        ScheduleEntry(9, "周二", "567", "别的班", "别班_1", "声速测量", "A504", "陈老师"),
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


def _approve(ctx, rid, comment="同意"):
    client, h, _ = ctx
    return client.post(f"/api/reschedule/requests/{rid}/approve",
                       headers=h, json={"approve": True, "comment": comment})


def _apply_move(ctx, **kw):
    client, h, _ = ctx
    return client.post("/api/reschedule/apply-move", headers=h, json=_payload(**kw))


def _weeks():
    return sorted(e.week for e in R._db.entries if e.teacher == TEACHER)


def _ledger():
    conn = R._get_db()
    try:
        return [dict(r) for r in conn.execute(
            "SELECT id, idem_key FROM applied_moves ORDER BY id").fetchall()]
    finally:
        conn.close()


def _helper():
    fn = getattr(R, "_resolve_idem_key", None)
    assert fn is not None, (
        "缺少共用的规范化幂等键 helper：审批路径与 /apply-move 各算各的，"
        "同一调课内容会拿到不同的键")
    return fn


# ==================== 1. helper 契约 ====================

def test_helper_returns_stable_nonempty_auto_key():
    fn = _helper()
    a = fn(_payload())
    b = fn(dict(_payload()))
    assert a == b, "同一内容的幂等键必须稳定"
    assert a.startswith("auto:") and len(a) > 10, a


def test_helper_explicit_key_wins_and_is_stripped():
    fn = _helper()
    assert fn(_payload(), "  cloud-7  ") == "cloud-7"
    assert fn(_payload(), "cloud-7") == "cloud-7"


def test_helper_ignores_ticket_specific_fields():
    """工单 id / 状态 / 批注 / 提交时间都不是调课内容，不参与指纹。"""
    fn = _helper()
    base = fn(_payload())
    ticket = dict(_payload(), id=42, status="approved", admin_comment="同意",
                  created_at="2026-11-06T23:00:00", reason="出差",
                  source_week=0)
    assert fn(ticket) == base, "requests 表整行与 /apply-move 的六字段必须同键"


@pytest.mark.parametrize("field,value", [
    ("teacher", "别的老师"), ("course_name", "声速测量"),
    ("class_name", "别的班"), ("original_time", "周三 08:00-10:00"),
    ("target_week", 8), ("source_week", 3),
])
def test_helper_changes_when_any_content_field_changes(field, value):
    """六个字段任一改变都必须是另一张工单 —— 防「一刀切全挡」式假修。"""
    fn = _helper()
    assert fn(_payload(**{field: value})) != fn(_payload()), field


def test_helper_key_is_shared_between_both_closed_loops():
    """`/apply-move` 的六字段 record 与审批的整行 record 必须生成同一个键。"""
    fn = _helper()
    move_record = {k: _payload().get(k) for k in SIX_FIELDS}
    approve_record = dict(_payload(), id=7, status="approved", admin_comment="x")
    assert fn(move_record) == fn(approve_record)


# ==================== 2. 审批路径必须真的带键 ====================

def test_approval_passes_nonempty_idem_key(ctx, monkeypatch):
    """审批调 `_apply_reschedule` 时必须传键；空串等于没防。"""
    real = R._apply_reschedule
    seen = []

    def spy(rec, persist=True, idem_key=""):
        seen.append(idem_key)
        return real(rec, persist, idem_key)

    monkeypatch.setattr(R, "_apply_reschedule", spy)
    _approve(ctx, _create(ctx))
    assert len(seen) == 1, seen
    assert seen[0], "审批仍在用空串幂等键，唯一索引刻意放过空串"
    assert seen[0].startswith("auto:"), seen


def test_two_identical_tickets_apply_only_once(ctx, monkeypatch):
    """探针场景 A：两张内容相同的工单，第二次不许再去撬更早一周、不许谎报。"""
    real = R._apply_reschedule
    seen = []

    def spy(rec, persist=True, idem_key=""):
        seen.append(idem_key)
        return real(rec, persist, idem_key)

    monkeypatch.setattr(R, "_apply_reschedule", spy)
    rid1, rid2 = _create(ctx), _create(ctx)
    first = _approve(ctx, rid1)
    assert first.status_code == 200, first.text
    assert "已同意并自动应用" in first.json()["message"], first.json()
    assert _weeks() == [3, 4, 7], _weeks()

    second = _approve(ctx, rid2)
    assert second.status_code == 200, second.text
    message = second.json()["message"]
    assert "此前已应用过" in message or "无需人工处理" in message, message
    for bad in ("人工协调", "人工修改", "必须调整后再申请", "核心冲突", "未找到可自动移动"):
        assert bad not in message, f"重复调课被谎报成「{bad}」：{message}"
    assert len(seen) == 2 and seen[0] == seen[1] and seen[0], seen
    assert len(_ledger()) == 1, _ledger()
    assert _weeks() == [3, 4, 7], _weeks()
    assert second.json()["request"]["status"] == "approved", "状态仍须落 approved"


def test_approval_after_apply_move_does_not_reapply(ctx, monkeypatch):
    """探针场景 B：两条闭环对同一调课内容必须互相认账。"""
    real = R._apply_reschedule
    seen = []

    def spy(rec, persist=True, idem_key=""):
        seen.append(idem_key)
        return real(rec, persist, idem_key)

    monkeypatch.setattr(R, "_apply_reschedule", spy)
    moved = _apply_move(ctx)
    assert moved.json()["data"]["applied"] == 1, moved.json()
    assert _weeks() == [3, 4, 7]

    approved = _approve(ctx, _create(ctx))
    body = approved.json()
    assert "此前已应用过" in body["message"] or "无需人工处理" in body["message"], body["message"]
    assert "核心冲突" not in body["message"], body["message"]
    assert len(seen) == 2 and seen[0] == seen[1] and seen[0], seen
    assert len(_ledger()) == 1, _ledger()
    assert _weeks() == [3, 4, 7], _weeks()


def test_approval_ledger_row_has_nonempty_fingerprint(ctx):
    """审批写账的 idem_key 不能是空串，否则重启 / 别的调用点仍会绕过查重。"""
    _approve(ctx, _create(ctx))
    rows = _ledger()
    assert len(rows) == 1, rows
    assert rows[0]["idem_key"].startswith("auto:"), rows


# ==================== 3. 审批必须认 already_applied 信号 ====================

def test_approval_reports_already_applied_instead_of_manual_fix(ctx, monkeypatch):
    """R82 给 helper 加的结构化信号，审批路径也得认。"""
    monkeypatch.setattr(
        R, "_apply_reschedule",
        lambda rec, persist=True, idem_key="": {
            "applied": 0, "skipped": 0, "already_applied": True,
            "details": ["该申请已于 2026-11-06 应用过，本次未重复移动"],
        },
    )
    body = _approve(ctx, _create(ctx)).json()
    message = body["message"]
    assert "此前已应用过" in message or "无需人工处理" in message, message
    assert "人工修改" not in message, message
    assert "未找到可自动移动" not in message, message
    assert body["request"]["status"] == "approved"


def test_approval_does_not_repeat_the_detail_when_already_applied(ctx, monkeypatch):
    """诚实文案里不许再叠一句「已应用过」的 details，避免自相矛盾的复刻。"""
    monkeypatch.setattr(
        R, "_apply_reschedule",
        lambda rec, persist=True, idem_key="": {
            "applied": 0, "skipped": 0, "already_applied": True,
            "details": ["该申请已于 2026-11-06 应用过，本次未重复移动"],
        },
    )
    message = _approve(ctx, _create(ctx)).json()["message"]
    assert "该申请已于" not in message, message


# ==================== 4. 防放宽：原有语义不许被改坏 ====================

def test_different_content_still_really_applies(ctx):
    """内容变了就是另一张工单，不许拿幂等当万能借口全挡掉。"""
    first = _approve(ctx, _create(ctx))
    assert "已同意并自动应用" in first.json()["message"], first.json()
    second = _approve(ctx, _create(ctx, target_week=8))
    assert "已同意并自动应用" in second.json()["message"], second.json()
    assert "此前已应用过" not in second.json()["message"], second.json()
    assert len(_ledger()) == 2, _ledger()


def test_cas_still_rejects_double_approve_of_same_ticket(ctx):
    """同一张工单重复审批仍须 400（R81 的 M15 守卫不许被动摇）。"""
    rid = _create(ctx)
    assert _approve(ctx, rid).status_code == 200
    assert _approve(ctx, rid).status_code == 400


def test_unloaded_db_still_says_data_not_loaded(ctx, monkeypatch):
    """未加载时的降级实话（R81）不许被 already_applied 分支吞掉。"""
    monkeypatch.setattr(R._db, "loaded", False)
    message = _approve(ctx, _create(ctx)).json()["message"]
    assert "排课数据尚未加载" in message, message
