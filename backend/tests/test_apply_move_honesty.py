"""R82：`/apply-move` 重复提交必须说实话，且不带 key 也要幂等。

起因（探针实测，2026-11-06）
----------------------------
1. 带 `key` 重复点一次，message 是
   「⚠️ 未找到可自动移动的排课记录（**请在教务系统中人工修改**）」
   紧跟一句「该申请已于 … 应用过，本次未重复移动」—— 自相矛盾。
   教秘照前半句去教务系统再改一遍，就是一次重复调课。
2. `ApplyMoveRequest.key` 默认 `""`（可省略），而库层唯一索引刻意放过空串
   （历史行 + 本来就没给键的调用，见 `test_apply_match.py`）。
   探针实测第二次真的去匹配了**第 4 周**（比第一次的源周更早一周），
   只是恰好撞上目标周冲突被跳过；message 又谎报成「必须调整后再申请」。
   目标周若另有空闲格，`_find_free_slot` 会自动改签成功 ⇒ 真的重复撬动。

本文件只断言用户可见结果：message 说什么、`data` 报什么、周次有没有动、
账本落了几行。不锚死实现细节（指纹算法、字段名以外的私有变量）。
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


def _table():
    d = ScheduleDatabase()
    d.entries = [
        ScheduleEntry(3, "周二", "567", CLASS, "24应化2_1", COURSE, "A504", TEACHER),
        ScheduleEntry(4, "周二", "567", CLASS, "24应化2_1", COURSE, "A504", TEACHER),
        ScheduleEntry(5, "周二", "567", CLASS, "24应化2_1", COURSE, "A504", TEACHER),
        # 第 9 周同格被别的班占了 —— 用来验证「真冲突」不被误判成幂等
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
        yield client, login.json()["token"], d


def _payload(**kw):
    body = {
        "teacher": TEACHER,
        "course_name": COURSE,
        "class_name": CLASS,
        "original_time": ORIGINAL,
        "target_week": 7,
    }
    body.update(kw)
    return body


def _post(ctx, payload):
    client, token, _ = ctx
    return client.post(
        "/api/reschedule/apply-move",
        headers={"Authorization": f"Bearer {token}"},
        json=payload,
    )


def _weeks():
    return sorted(e.week for e in R._db.entries if e.teacher == TEACHER)


def _ledger():
    conn = R._get_db()
    try:
        return [dict(r) for r in conn.execute(
            "SELECT idem_key FROM applied_moves").fetchall()]
    finally:
        conn.close()


# ----------------------------------------------------------- 1. 带 key 重复提交

def test_repeat_with_key_says_already_applied_not_manual_fix(ctx):
    """重复提交不能再叫教秘去教务系统人工改一遍。"""
    first = _post(ctx, _payload(key="cloud-7"))
    assert first.status_code == 200, first.text
    assert first.json()["data"]["applied"] == 1, first.json()
    assert _weeks() == [3, 4, 7]

    second = _post(ctx, _payload(key="cloud-7"))
    assert second.status_code == 200, second.text
    body = second.json()
    message = body["message"]
    assert "人工修改" not in message, f"重复提交仍在叫人手工改课：{message}"
    assert "未找到可自动移动" not in message, f"重复提交被谎报成没找到记录：{message}"
    assert "已应用过" in message or "无需人工处理" in message, message
    assert body["data"]["already_applied"] is True, body["data"]
    assert body["data"]["applied"] == 0, body["data"]
    assert _weeks() == [3, 4, 7], "重复提交又动了课表"


def test_repeat_with_key_does_not_grow_the_ledger(ctx):
    """幂等命中不许再记一行账（账本是撤销依据与 /demo/reset 的计数来源）。"""
    _post(ctx, _payload(key="cloud-7"))
    _post(ctx, _payload(key="cloud-7"))
    assert len(_ledger()) == 1, _ledger()


# ----------------------------------------------------------- 2. 不带 key 也要幂等

def test_repeat_without_key_is_idempotent(ctx):
    """key 可省略，省略时端点层必须自己挡住重复提交。"""
    first = _post(ctx, _payload())
    assert first.json()["data"]["applied"] == 1, first.json()
    assert _weeks() == [3, 4, 7]

    second = _post(ctx, _payload())
    body = second.json()
    assert body["data"]["applied"] == 0, body
    assert body["data"]["already_applied"] is True, body["data"]
    # 修复前第二次会去撬第 4 周、撞冲突被跳过 → skipped=1 且谎报「必须调整后再申请」
    assert body["data"]["skipped"] == 0, body["data"]
    assert "核心冲突" not in body["message"], body["message"]
    assert "已应用过" in body["message"] or "无需人工处理" in body["message"], body["message"]
    assert _weeks() == [3, 4, 7], "不带 key 的重复提交又动了课表"


def test_without_key_ledger_has_nonempty_fingerprint(ctx):
    """隐式幂等键必须真的落库，否则进程一重启重复提交就又失效。"""
    _post(ctx, _payload())
    rows = _ledger()
    assert len(rows) == 1, rows
    key = rows[0]["idem_key"]
    assert key, "空串不参与唯一索引，等于没防"
    assert key.startswith("auto:"), f"应是端点层内容指纹，实际 {key!r}"


def test_explicit_key_wins_over_fingerprint(ctx):
    """显式传 key 时原样落库，不许被内容指纹覆盖。"""
    _post(ctx, _payload(key="cloud-42"))
    assert [r["idem_key"] for r in _ledger()] == ["cloud-42"], _ledger()


def test_whitespace_key_counts_as_missing(ctx):
    """key='   ' 不是有效键，不能让它绕过幂等。"""
    _post(ctx, _payload(key="   "))
    _post(ctx, _payload(key="   "))
    assert len(_ledger()) == 1, _ledger()
    assert _ledger()[0]["idem_key"].startswith("auto:"), _ledger()
    assert _weeks() == [3, 4, 7]


# ----------------------------------------------------------- 3. 不许「一刀切全挡」

def test_different_target_week_is_not_swallowed_as_repeat(ctx):
    """内容变了就是另一张工单，必须真的执行 —— 防「所有重复请求都挡掉」式假修。"""
    _post(ctx, _payload())
    second = _post(ctx, _payload(target_week=8))
    assert second.json()["data"]["applied"] == 1, second.json()
    assert second.json()["data"]["already_applied"] is False, second.json()["data"]
    assert _weeks() == [3, 4, 8], _weeks()


def test_no_match_is_still_reported_as_no_match(ctx):
    """真没匹配到记录时仍要如实说「未找到」，不许拿幂等当万能借口。"""
    resp = _post(ctx, _payload(teacher="查无此人", key="cloud-x"))
    body = resp.json()
    assert body["data"]["applied"] == 0, body
    assert body["data"]["already_applied"] is False, body["data"]
    assert "未找到" in body["message"], body["message"]


def test_real_conflict_is_still_reported_as_conflict(ctx):
    """真撞核心冲突时仍要如实报冲突，不许被误判成「已应用过」。"""
    resp = _post(ctx, _payload(target_week=9, key="cloud-9"))
    body = resp.json()
    assert body["data"]["skipped"] == 1, body
    assert body["data"]["already_applied"] is False, body["data"]
    assert "核心冲突" in body["message"], body["message"]


# ----------------------------------------------------------- 4. helper 层结构化信号

def test_helper_marks_already_applied_on_lookup_hit(ctx):
    """`_apply_reschedule` 查重命中要带结构化标记，端点不能靠中文文案反解。"""
    _, _, _ = ctx
    rec = _payload()
    first = R._apply_reschedule(dict(rec), persist=True, idem_key="cloud-direct")
    assert first["applied"] == 1, first
    second = R._apply_reschedule(dict(rec), persist=True, idem_key="cloud-direct")
    assert second["applied"] == 0, second
    assert second.get("already_applied") is True, second


# ----------------------------------------------------------- 5. 并发 / 写失败分支

def test_integrity_error_branch_marks_already_applied(ctx, monkeypatch):
    """并发撞唯一索引时也要带同一个结构化信号，不能只有查重命中那条带。

    `/apply-move` 整段在 `_DB_WRITE_LOCK` 里，单进程内走不到这个分支；
    但多实例 / 重放 / 别的写者共库时会走到，标记缺了就又变回谎报。
    """
    import sqlite3

    class _FakeConn:
        def execute(self, sql, *args):
            if sql.lstrip().upper().startswith("INSERT"):
                raise sqlite3.IntegrityError("UNIQUE constraint failed")

            class _Row:
                def fetchone(self):
                    return None

                def fetchall(self):
                    # R84：查重后还会按内容回认升级前的空键老行。
                    # 真实库里这张夹具表没有老行，fetchall() 就该是空列表；
                    # 别让假连接因为少一个方法而把 IntegrityError 分支测丢。
                    return []

            return _Row()

        def commit(self):
            pass

        def rollback(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(R, "_get_db", lambda: _FakeConn())
    result = R._apply_reschedule(dict(_payload()), persist=True, idem_key="cloud-race")
    assert result["applied"] == 0, result
    assert result.get("already_applied") is True, result


def test_concurrent_without_key_writes_one_ledger_row(ctx):
    """两个不带 key 的重复请求同时到达：账本只准一行，课表只准动一次。"""
    import threading

    client, token, _ = ctx
    bar = threading.Barrier(2)
    responses = []

    def work():
        bar.wait()
        responses.append(client.post(
            "/api/reschedule/apply-move",
            headers={"Authorization": f"Bearer {token}"},
            json=_payload(),
        ))

    threads = [threading.Thread(target=work) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(_ledger()) == 1, _ledger()
    assert _weeks() == [3, 4, 7], _weeks()
    assert sum(r.json()["data"]["applied"] for r in responses) == 1, [
        r.json() for r in responses
    ]
