# -*- coding: utf-8 -*-
"""R84：升级前账本行（`idem_key=''` / NULL）必须按内容指纹被认回来。

探针实测（2026-11-06，本机 `_r84_probe.py`，不改源码）
------------------------------------------------------
A. 老版本写账 `idem_key=''`（第 5 周 → 第 7 周），升级后新代码带
   `auto:a41327f9…` 键再调同一调课：查重 SQL 只认 `idem_key != ''`，
   于是第二次**又去匹配第 4 周**，`already_applied=None`；只是恰好撞目标周
   核心冲突没动，返回体却谎报「必须调整后再申请」。
C. 端点 `/apply-move` 在同样场景返回 `already_applied: False` +
   「⚠️ 目标周撞上核心冲突…核心冲突必须调整，不能强制通过」——
   明明是重复提交，却叫教秘去改一张根本不存在的冲突。
B. 老行 `target_week=8`、新请求 `target_week=7`：正常应用两行账本。
   这是**不能误挡**的正向对照，修法必须保住。

为什么按内容、不按 `idem_key` 参数比
------------------------------------
老行没有键，能比的只有 `request_data` 里的调课内容。当前请求带的键可能是
`cloud-<工单id>`（外部系统给的），也可能是 `auto:<指纹>`；两者都得先还原成
「纯内容指纹」再与老行比，否则云端闭环永远认不回老账。

边界（本文件显式锁住）
----------------------
· 库层「空串互不冲突」的唯一索引约定一字未动；本轮只加**查重时的回认**。
· 重放 `_apply_reschedule(rec, persist=False, idem_key="")` 刻意不查重，
  重放本来就要重执行账本 —— 不得因为兼容老行而把重放吞掉。
· 老行 JSON 坏了 / 不是 dict / 字段对不上：宁可漏认（多应用一次），
  也不误挡一张真正的新工单。
"""

import json

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
LEGACY_AT = "2026-10-01T09:00:00.000000"


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


def _insert_legacy(payload, applied_at=LEGACY_AT, idem_key=""):
    """模拟升级前写下的账本行：有完整 request_data，但没有幂等键。"""
    raw = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    conn = R._get_db()
    try:
        cur = conn.execute(
            "INSERT INTO applied_moves (request_data, applied_at, idem_key) "
            "VALUES (?, ?, ?)", (raw, applied_at, idem_key))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def _ledger():
    conn = R._get_db()
    try:
        return [(r["id"], r["idem_key"]) for r in conn.execute(
            "SELECT id, idem_key FROM applied_moves ORDER BY id").fetchall()]
    finally:
        conn.close()


def _weeks():
    return sorted(e.week for e in R._db.entries if e.teacher == TEACHER)


def _replayed_table():
    """把第 5 周改成第 7 周，模拟重启重放老账之后的课表。"""
    for e in R._db.entries:
        if e.teacher == TEACHER and e.week == 5:
            e.week = 7


def _apply_move(ctx, **kw):
    client, h, _ = ctx
    return client.post("/api/reschedule/apply-move", headers=h, json=_payload(**kw))


def _create(ctx, **kw):
    client, h, _ = ctx
    r = client.post("/api/reschedule/requests", headers=h, json=_payload(**kw))
    assert r.status_code == 200, r.text
    return r.json()["request"]["id"]


def _approve(ctx, rid):
    client, h, _ = ctx
    return client.post(f"/api/reschedule/requests/{rid}/approve",
                       headers=h, json={"approve": True, "comment": "同意"})


# ---------------------------------------------------------------- 新契约：共用回认 helper

def test_helper_exists_and_returns_none_without_legacy_rows(ctx):
    assert R._find_legacy_applied_move(R._resolve_idem_key(_payload())) is None


def test_helper_recognizes_legacy_row_by_content(ctx):
    _insert_legacy(_payload())
    assert R._find_legacy_applied_move(R._resolve_idem_key(_payload())) == LEGACY_AT


def test_helper_ignores_corrupt_json(ctx):
    _insert_legacy("{not-json")
    assert R._find_legacy_applied_move(R._resolve_idem_key(_payload())) is None


def test_helper_ignores_non_dict_payload(ctx):
    _insert_legacy("[1, 2, 3]")
    assert R._find_legacy_applied_move(R._resolve_idem_key(_payload())) is None


def test_helper_recognizes_null_idem_key_row(ctx):
    _insert_legacy(_payload(), idem_key=None)
    assert R._find_legacy_applied_move(R._resolve_idem_key(_payload())) == LEGACY_AT


def test_helper_is_read_only(ctx):
    _insert_legacy(_payload())
    before = _ledger()
    R._find_legacy_applied_move(R._resolve_idem_key(_payload()))
    assert _ledger() == before


def test_helper_does_not_match_rows_with_a_real_key(ctx):
    """带键的新行不归它管（精确查重负责），避免两套判据互相盖。"""
    _insert_legacy(_payload(), idem_key="auto:some-other-key")
    assert R._find_legacy_applied_move(R._resolve_idem_key(_payload())) is None


# ---------------------------------------------------------------- 真缺陷：老账本行挡不住重复提交

def test_legacy_row_blocks_repeat_apply_with_auto_key(ctx):
    _insert_legacy(_payload())
    res = R._apply_reschedule(_payload(), persist=True,
                              idem_key=R._resolve_idem_key(_payload()))
    assert res.get("already_applied") is True, res
    assert res["applied"] == 0, res
    assert _ledger() == [(1, "")], _ledger()


def test_legacy_row_blocks_repeat_apply_with_explicit_key(ctx):
    """云端闭环带 `cloud-<id>`：也必须按**内容**认回老账，而不是按参数字面比。"""
    _insert_legacy(_payload())
    res = R._apply_reschedule(_payload(), persist=True, idem_key="cloud-42")
    assert res.get("already_applied") is True, res
    assert res["applied"] == 0, res
    assert _ledger() == [(1, "")], _ledger()


def test_legacy_hit_returns_structured_signal_and_honest_detail(ctx):
    _insert_legacy(_payload())
    res = R._apply_reschedule(_payload(), persist=True,
                              idem_key=R._resolve_idem_key(_payload()))
    assert res.get("already_applied") is True
    assert any("历史账本" in d for d in res["details"]), res["details"]
    assert not any("必须调整后再申请" in d for d in res["details"]), res["details"]


def test_legacy_hit_keeps_weeks_unchanged(ctx):
    _insert_legacy(_payload())
    before = _weeks()
    R._apply_reschedule(_payload(), persist=True,
                        idem_key=R._resolve_idem_key(_payload()))
    assert _weeks() == before


def test_endpoint_reports_already_applied_for_legacy(ctx):
    _insert_legacy(_payload())
    r = _apply_move(ctx)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["data"]["already_applied"] is True, body
    assert "无需人工处理" in body["message"], body["message"]
    assert "核心冲突" not in body["message"], body["message"]
    assert "人工修改" not in body["message"], body["message"]


def test_approval_reports_already_applied_for_legacy(ctx):
    _insert_legacy(_payload())
    rid = _create(ctx)
    r = _approve(ctx, rid)
    assert r.status_code == 200, r.text
    msg = r.json()["message"]
    assert "此前已应用过" in msg, msg
    assert "无需人工处理" in msg, msg
    assert "人工修改" not in msg, msg


def test_replayed_then_resubmit_is_not_called_a_conflict(ctx):
    """升级后重启重放了老账，教秘再点一次：不能谎报成「目标周撞核心冲突」。"""
    _insert_legacy(_payload())
    _replayed_table()
    r = _apply_move(ctx)
    body = r.json()
    assert body["data"]["already_applied"] is True, body
    assert "核心冲突" not in body["message"], body["message"]
    assert _weeks() == [3, 4, 7]


# ---------------------------------------------------------------- 防放宽：这些本来就该绿

def test_different_target_week_is_not_swallowed(ctx):
    _insert_legacy(_payload(target_week=8))
    res = R._apply_reschedule(_payload(), persist=True,
                              idem_key=R._resolve_idem_key(_payload()))
    assert res.get("already_applied") is not True, res
    assert res["applied"] == 1, res
    assert _weeks() == [3, 4, 7], _weeks()


def test_different_source_week_is_not_swallowed(ctx):
    _insert_legacy(_payload(source_week=3))
    res = R._apply_reschedule(_payload(source_week=5), persist=True,
                              idem_key=R._resolve_idem_key(_payload(source_week=5)))
    assert res.get("already_applied") is not True, res
    assert res["applied"] == 1, res


def test_replay_without_key_is_not_deduplicated(ctx):
    """重放刻意不查重：即使账本里有老行，重放仍要真的把课表移过去。"""
    _insert_legacy(_payload())
    res = R._apply_reschedule(_payload(), persist=False, idem_key="")
    assert res["applied"] == 1, res
    assert _weeks() == [3, 4, 7]
    assert _ledger() == [(1, "")]


def test_new_rows_still_have_nonempty_idem_key(ctx):
    r = _apply_move(ctx)
    assert r.status_code == 200, r.text
    rows = _ledger()
    assert len(rows) == 1, rows
    assert rows[0][1].startswith("auto:"), rows


def test_exact_key_hit_still_short_circuits(ctx):
    """既有精确查重优先：带键行命中时不该绕去扫老行、也不该新增账。"""
    R._apply_reschedule(_payload(), persist=True, idem_key="cloud-77")
    _insert_legacy(_payload())
    before = _ledger()
    res = R._apply_reschedule(_payload(), persist=True, idem_key="cloud-77")
    assert res.get("already_applied") is True, res
    assert _ledger() == before
