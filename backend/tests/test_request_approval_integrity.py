"""R81：审批台的三处静默错误。

探针（2026-11-05，本机实测，不改生产代码）先把现象钉死：

1. 两个管理员**同时**点「同意」同一张工单：两个请求都读到 `status='pending'`，
   都返回 200，`_apply_reschedule` 被调用 **2 次**。而 `_apply_reschedule` 的源周
   是「目标周之前最近的匹配周」，重复执行会去移更早一周 —— 一张工单连撬几周。
   顺序重复审批有 M15 守着（第二次读到非 pending ⇒ 400），并发没有。
2. `GET /requests/export?status=../../pwned` 返回 200，
   `Content-Disposition` 里带 `调课申请记录_../../pwned.xlsx`。
   `urllib.parse.quote` 默认 `safe='/'`，斜杠原样保留 ⇒ 下载文件名可被用户拼出路径。
3. `_db.loaded=False`（沙箱回收后）点「同意」：工单照样变 `approved`，
   文案却是「未找到可自动移动的记录」—— 实际连查都没查，是谎报。

这三条都不是「看起来会出问题」，是探针跑出来的返回体。
"""
import os
import sys
import threading
from urllib.parse import unquote

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.api import reschedule as api
from app.main import app
from app.security import issue_admin_token

ADMIN = {"Authorization": f"Bearer {issue_admin_token('admin')['token']}"}
REQ = {"teacher": "吴老师", "course_name": "大学物理实验", "class_name": "甲班",
       "original_time": "周二 13:30-15:45 · A412", "target_week": 5, "reason": "出差"}


@pytest.fixture()
def isolated(monkeypatch, tmp_path):
    """把 SQLite 指到临时库，避免污染真实库。"""
    monkeypatch.setattr(api, "_DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setattr(api, "_DATA_DIR", str(tmp_path / "up"))
    monkeypatch.setattr(api, "_landing_store", None)
    api._init_db()
    client = TestClient(app)
    client.delete("/api/reschedule/requests", headers=ADMIN)
    return client


def _new_request(client):
    return client.post("/api/reschedule/requests", json=REQ, headers=ADMIN).json()["request"]["id"]


def _race(client, rid, monkeypatch, bodies, gate_select=False):
    """同时发起 N 个审批，返回 (响应列表, _apply_reschedule 的调用次数)。

    `fake_apply` 里放一道 Barrier：让已经通过 pending 检查的请求都停在这儿，
    把「两个请求之间的时间窗」人为撑开，红因才稳定可复现。
    修复后只有 1 个请求能走到这里，Barrier 超时是**预期行为**。

    `gate_select=True` 时再在 `SELECT ... WHERE id = ?` 前拦一道，强制两个请求
    都读到 `pending` 快照 —— 这才是 CAS 的 `rowcount` 分支真正被执行的场景。
    否则 TestClient 常常把第二个请求排到第一个 commit 之后，M15 的 400 就先
    把它挡了，测试根本没碰到 CAS。
    """
    calls = []
    gate = threading.Barrier(len(bodies))
    select_gate = threading.Barrier(len(bodies)) if gate_select else None
    real_get_db = api._get_db

    def fake_apply(rec, persist=True):
        try:
            gate.wait(timeout=1.0)
        except threading.BrokenBarrierError:
            pass  # 修复后只有一个请求能进，等不到同伴
        calls.append(rec)
        return {"applied": 1, "skipped": 0, "details": []}

    monkeypatch.setattr(api, "_db", type("_D", (), {"loaded": True})())
    monkeypatch.setattr(api, "_apply_reschedule", fake_apply)

    if gate_select:
        class _GatedConn:
            """只拦第一次查工单的 SELECT，其余原样转发给真实 sqlite 连接。"""

            def __init__(self, real):
                self._real = real
                self._armed = True

            def execute(self, sql, *args):
                if self._armed and "SELECT * FROM requests WHERE id = ?" in sql:
                    self._armed = False
                    select_gate.wait(timeout=2.0)
                return self._real.execute(sql, *args)

            def __getattr__(self, name):
                return getattr(self._real, name)

        monkeypatch.setattr(api, "_get_db", lambda: _GatedConn(real_get_db()))

    start = threading.Barrier(len(bodies))
    out = []

    def fire(body):
        start.wait()
        r = client.post(f"/api/reschedule/requests/{rid}/approve", json=body, headers=ADMIN)
        out.append(r)

    threads = [threading.Thread(target=fire, args=(b,)) for b in bodies]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return out, calls


# ==================== 一、并发审批只准应用一次 ====================

def test_concurrent_approvals_apply_move_exactly_once(isolated, monkeypatch):
    """两个「同意」同时到达：只有一个能抢到工单，另一个必须被明确拒绝。"""
    rid = _new_request(isolated)
    responses, calls = _race(
        isolated, rid, monkeypatch,
        [{"approve": True}, {"approve": True}],
    )
    codes = sorted(r.status_code for r in responses)
    assert len(calls) == 1, (
        f"同一张工单被自动应用了 {len(calls)} 次 —— _apply_reschedule 重复执行会"
        f"连着撬动更早的周次（响应码 {codes}）")
    # 400（M15 读到已审批）与 409（CAS 抢输）都算正确挡下，取决于调度时序。
    assert codes[0] == 200 and codes[1] in (400, 409), (
        f"应有一个请求被并发冲突挡下，实际 {codes}")


def test_cas_rejects_loser_when_both_read_pending(isolated, monkeypatch):
    """强制两个请求都读到 pending 快照：M15 帮不上忙，只能靠 CAS 的 rowcount。

    这条是 M57（删 `AND status='pending'`）/ M58（不检查 rowcount）的真正靶子。
    没有它，上一条用例可能全程被 400 挡下，CAS 形同没测。
    """
    rid = _new_request(isolated)
    responses, calls = _race(
        isolated, rid, monkeypatch,
        [{"approve": True, "comment": "赢家"}, {"approve": True, "comment": "输家"}],
        gate_select=True,
    )
    codes = sorted(r.status_code for r in responses)
    assert codes == [200, 409], f"两个都读到 pending 时应由 CAS 判出唯一赢家，实际 {codes}"
    assert len(calls) == 1, f"CAS 失守：调课被应用了 {len(calls)} 次"
    loser = next(r for r in responses if r.status_code == 409)
    assert "刷新" in loser.json()["detail"]
    # 两个请求都被 gate 放行后，谁先抢到 SQLite 写锁是随机的：
    # 不能假设第一个 body 一定赢。覆盖率插桩下调度一变就会翻过来（实测过一次）。
    winner = next(r for r in responses if r.status_code == 200)
    final = isolated.get("/api/reschedule/requests", headers=ADMIN).json()["requests"][0]
    assert final["admin_comment"] == winner.json()["request"]["admin_comment"]


def test_concurrent_approve_and_reject_has_single_winner(isolated, monkeypatch):
    """一个同意、一个驳回同时到达：状态只能由赢家决定，不能被后写者覆盖。"""
    rid = _new_request(isolated)
    responses, calls = _race(
        isolated, rid, monkeypatch,
        [{"approve": True, "comment": "准了"}, {"approve": False, "comment": "不行"}],
    )
    codes = sorted(r.status_code for r in responses)
    assert codes[0] == 200 and codes[1] in (400, 409), (
        f"应有一个请求被并发冲突挡下，实际 {codes}")
    winner = next(r for r in responses if r.status_code == 200)
    final = isolated.get("/api/reschedule/requests", headers=ADMIN).json()["requests"][0]
    assert final["status"] == winner.json()["request"]["status"], (
        "落库状态与成功响应不一致：输家的 UPDATE 把赢家的结果覆盖了")
    assert final["admin_comment"] == winner.json()["request"]["admin_comment"]
    assert len(calls) == (1 if final["status"] == "approved" else 0)


def test_final_status_is_not_overwritten_by_loser(isolated, monkeypatch):
    """CAS 的写必须是条件写：末尾不许再留一次无条件 UPDATE 把状态盖回去。"""
    rid = _new_request(isolated)
    responses, _ = _race(
        isolated, rid, monkeypatch,
        [{"approve": True, "comment": "赢家"}, {"approve": True, "comment": "输家"}],
    )
    winner = next(r for r in responses if r.status_code == 200)
    final = isolated.get("/api/reschedule/requests", headers=ADMIN).json()["requests"][0]
    assert final["admin_comment"] == winner.json()["request"]["admin_comment"]


# ==================== 二、导出文件名不许回显用户输入 ====================

def test_export_rejects_status_outside_whitelist(isolated):
    """未知 status 直接 400，不让它进 SQL，更不让它进 Content-Disposition。"""
    for bad in ("../../pwned", "pending/../../x", "a;b", "' OR 1=1 --", "PENDING"):
        r = isolated.get("/api/reschedule/requests/export",
                         params={"status": bad}, headers=ADMIN)
        assert r.status_code == 400, f"status={bad!r} 应被拒绝，实际 {r.status_code}"


def test_export_filename_uses_only_whitelisted_labels(isolated):
    """合法 status 也必须走中文标签，文件名里不许出现原始参数或路径分隔符。"""
    _new_request(isolated)
    for status, label in (("", ""), ("pending", "待审批"),
                          ("approved", "已通过"), ("rejected", "已驳回")):
        r = isolated.get("/api/reschedule/requests/export",
                         params={"status": status}, headers=ADMIN)
        assert r.status_code == 200, (status, r.status_code)
        cd = r.headers["content-disposition"]
        decoded = unquote(cd)
        assert "/" not in decoded.split("filename*=")[-1], (status, cd)
        assert label in decoded, (status, decoded)
        assert r.headers["content-type"].startswith(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


# ==================== 三、未加载时不要谎报 ====================

def test_unloaded_db_approval_says_data_not_loaded(isolated, monkeypatch):
    """数据没加载就别把工单悄悄标成「已应用」，更别说是「未找到匹配记录」。"""
    monkeypatch.setattr(api._db, "loaded", False)
    rid = _new_request(isolated)
    r = isolated.post(f"/api/reschedule/requests/{rid}/approve",
                      json={"approve": True}, headers=ADMIN)
    assert r.status_code == 200
    body = r.json()
    assert body["request"]["status"] == "approved"   # 行为不变：审批本身仍成功
    assert "数据尚未加载" in body["message"], body["message"]
    assert "未找到可自动移动的记录" not in body["message"], body["message"]
    assert "人工" in body["message"], "要留下人工兜底指引"


# ==================== 四、回归：原有语义一条都不许丢 ====================

def test_sequential_double_approve_still_400(isolated):
    rid = _new_request(isolated)
    first = isolated.post(f"/api/reschedule/requests/{rid}/approve",
                          json={"approve": False, "comment": "x"}, headers=ADMIN)
    assert first.status_code == 200
    second = isolated.post(f"/api/reschedule/requests/{rid}/approve",
                           json={"approve": True}, headers=ADMIN)
    assert second.status_code == 400
    assert "已审批" in second.json()["detail"]


def test_unknown_request_still_404(isolated):
    r = isolated.post("/api/reschedule/requests/999999/approve",
                      json={"approve": True}, headers=ADMIN)
    assert r.status_code == 404


def test_reject_path_unchanged(isolated):
    rid = _new_request(isolated)
    r = isolated.post(f"/api/reschedule/requests/{rid}/approve",
                      json={"approve": False}, headers=ADMIN)
    assert r.status_code == 200
    assert r.json()["request"]["status"] == "rejected"
    assert r.json()["request"]["admin_comment"] == "未说明原因"


def test_approve_without_comment_uses_default_comment(isolated, monkeypatch):
    rid = _new_request(isolated)
    monkeypatch.setattr(api._db, "loaded", False)
    r = isolated.post(f"/api/reschedule/requests/{rid}/approve",
                      json={"approve": True}, headers=ADMIN)
    assert r.json()["request"]["admin_comment"] == "同意调课"


def test_export_without_status_is_still_allowed(isolated):
    """前端 AdminView 只传空串，别把正常留档路径挡在外面。"""
    _new_request(isolated)
    r = isolated.get("/api/reschedule/requests/export", headers=ADMIN)
    assert r.status_code == 200
    assert r.content[:2] == b"PK", "返回的不是 xlsx"
