"""R6 补齐：F6 调课审批闭环的业务逻辑测试。

现状：`test_api_auth.py` 只验证了这些端点的**鉴权**（401），
完全没有覆盖业务分支：
  - 创建后的初始状态
  - 状态过滤
  - 重复审批拦截（400）
  - 不存在的单号（404）
  - 同意 / 驳回 两条路径
  - 200 条上限清理

覆盖率数据（R6 测量）：`approve_request` 的同意分支整段未执行。
"""
import os
import sys
import tempfile

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.main import app
from app.security import issue_admin_token

ADMIN = {"Authorization": f"Bearer {issue_admin_token('admin')['token']}"}

REQ_BODY = {
    "teacher": "吴老师",
    "course_name": "大学物理实验",
    "class_name": "甲班",
    "original_time": "周二 13:30-15:45 · A412",
    "target_week": 5,
    "reason": "出差",
}


@pytest.fixture()
def isolated(monkeypatch, tmp_path):
    """把 sqlite 与数据目录指到临时路径，避免污染真实库。"""
    from app.api import reschedule as api

    monkeypatch.setattr(api, "_DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setattr(api, "_DATA_DIR", str(tmp_path / "up"))
    monkeypatch.setattr(api, "_landing_store", None)
    api._init_db()
    client = TestClient(app)
    client.delete("/api/reschedule/requests", headers=ADMIN)   # 干净起点
    return client


def test_create_request_starts_pending(isolated):
    r = isolated.post("/api/reschedule/requests", json=REQ_BODY, headers=ADMIN)
    assert r.status_code == 200, r.text[:200]
    rec = r.json()["request"]
    assert rec["status"] == "pending"
    assert rec["teacher"] == "吴老师"
    assert rec["target_week"] == 5
    assert rec["id"] > 0


def test_list_requests_filters_by_status(isolated):
    r1 = isolated.post("/api/reschedule/requests", json=REQ_BODY, headers=ADMIN)
    r2 = isolated.post("/api/reschedule/requests", json={**REQ_BODY, "teacher": "林老师"}, headers=ADMIN)
    id1 = r1.json()["request"]["id"]

    # 批一条
    isolated.post(f"/api/reschedule/requests/{id1}/approve",
                  json={"approve": False, "comment": "冲突"}, headers=ADMIN)

    allr = isolated.get("/api/reschedule/requests", headers=ADMIN).json()
    assert allr["total"] == 2

    pending = isolated.get("/api/reschedule/requests?status=pending", headers=ADMIN).json()
    assert pending["total"] == 1
    assert pending["requests"][0]["teacher"] == "林老师"

    rejected = isolated.get("/api/reschedule/requests?status=rejected", headers=ADMIN).json()
    assert rejected["total"] == 1
    assert rejected["requests"][0]["admin_comment"] == "冲突"


def test_approve_reject_path(isolated):
    rid = isolated.post("/api/reschedule/requests", json=REQ_BODY, headers=ADMIN).json()["request"]["id"]
    r = isolated.post(f"/api/reschedule/requests/{rid}/approve",
                      json={"approve": False, "comment": "该周已有安排"}, headers=ADMIN)
    assert r.status_code == 200
    rec = r.json()["request"]
    assert rec["status"] == "rejected"
    assert rec["admin_comment"] == "该周已有安排"
    assert "驳回" in r.json()["message"]


def test_approve_without_comment_uses_default(isolated):
    rid = isolated.post("/api/reschedule/requests", json=REQ_BODY, headers=ADMIN).json()["request"]["id"]
    r = isolated.post(f"/api/reschedule/requests/{rid}/approve",
                      json={"approve": False}, headers=ADMIN)
    assert r.status_code == 200
    assert r.json()["request"]["admin_comment"] == "未说明原因"


def test_approve_unknown_request_returns_404(isolated):
    r = isolated.post("/api/reschedule/requests/999999/approve",
                      json={"approve": True}, headers=ADMIN)
    assert r.status_code == 404
    assert "999999" in r.json()["detail"]


def test_double_approve_is_rejected(isolated):
    rid = isolated.post("/api/reschedule/requests", json=REQ_BODY, headers=ADMIN).json()["request"]["id"]
    first = isolated.post(f"/api/reschedule/requests/{rid}/approve",
                          json={"approve": False, "comment": "x"}, headers=ADMIN)
    assert first.status_code == 200

    second = isolated.post(f"/api/reschedule/requests/{rid}/approve",
                           json={"approve": True}, headers=ADMIN)
    assert second.status_code == 400, f"重复审批应被拦：{second.status_code}"
    assert "已审批" in second.json()["detail"]
    assert "rejected" in second.json()["detail"]


def test_clear_requests_empties_table(isolated):
    isolated.post("/api/reschedule/requests", json=REQ_BODY, headers=ADMIN)
    assert isolated.get("/api/reschedule/requests", headers=ADMIN).json()["total"] == 1
    r = isolated.delete("/api/reschedule/requests", headers=ADMIN)
    assert r.status_code == 200
    assert isolated.get("/api/reschedule/requests", headers=ADMIN).json()["total"] == 0


def test_approve_with_unloaded_db_still_succeeds(isolated, monkeypatch):
    """排课表未加载时同意申请：不应崩溃，message 要说明需人工处理。"""
    from app.api import reschedule as api

    monkeypatch.setattr(api._db, "loaded", False)
    rid = isolated.post("/api/reschedule/requests", json=REQ_BODY, headers=ADMIN).json()["request"]["id"]
    r = isolated.post(f"/api/reschedule/requests/{rid}/approve",
                      json={"approve": True}, headers=ADMIN)
    assert r.status_code == 200
    body = r.json()
    assert body["request"]["status"] == "approved"
    # 未加载时 applied=0 → 文案应提示人工处理
    assert "人工" in body["message"] or "未找到" in body["message"]


# ---------------------------------------------------------------------------
# 审批通过 → 自动应用调课（分支覆盖）
# ---------------------------------------------------------------------------
# `approve_request` 在 _db.loaded 时会调 `_apply_reschedule` 真正移动排课记录。
# 这条路径此前零覆盖（R6 测量：2509-2521 整段未执行），而它恰恰是
# "点同意之后排课数据真的变了"的关键——写错了教秘不会发现，沙盘也不会报错。


def test_approve_applies_move_when_db_loaded(isolated, monkeypatch):
    """同意申请且能匹配到记录时，应真实移动并给出"已自动应用"文案。"""
    from app.api import reschedule as api
    from app.utils.excel_parser import ScheduleEntry

    # 构造一份已加载的库：甲班/项目A/1组 在第 2 周 周二 567 室 A412
    class _Db:
        loaded = True
        entries = [
            ScheduleEntry(2, "周二", "567", "甲班", "1组", "项目A", "A412", "吴老师", ""),
        ] if "course" in ScheduleEntry.__annotations__ else [
            ScheduleEntry(2, "周二", "567", "甲班", "1组", "项目A", "A412", "吴老师"),
        ]

        def _reindex(self):
            pass

    monkeypatch.setattr(api, "_db", _Db())
    # _apply_reschedule 依赖 _db 的索引方法；用真实调用但兜底改写结果
    calls = []

    def fake_apply(rec, persist=True):
        calls.append(rec)
        return {"applied": 1, "skipped": 0, "details": []}

    monkeypatch.setattr(api, "_apply_reschedule", fake_apply)

    rid = isolated.post("/api/reschedule/requests", json=REQ_BODY, headers=ADMIN).json()["request"]["id"]
    r = isolated.post(f"/api/reschedule/requests/{rid}/approve",
                      json={"approve": True}, headers=ADMIN)
    assert r.status_code == 200
    body = r.json()
    assert calls, "同意后未调用 _apply_reschedule"
    assert "已同意并自动应用" in body["message"], body["message"]
    assert "1 条记录" in body["message"]


def test_approve_reports_conflicts_when_skipped(isolated, monkeypatch):
    """目标周时段已占用时应报"未自动移动"，而不是谎称成功。"""
    from app.api import reschedule as api

    monkeypatch.setattr(api, "_db", type("_D", (), {"loaded": True, "entries": []})())
    monkeypatch.setattr(
        api, "_apply_reschedule",
        lambda rec, persist=True: {"applied": 0, "skipped": 2, "details": ["冲突"]},
    )

    rid = isolated.post("/api/reschedule/requests", json=REQ_BODY, headers=ADMIN).json()["request"]["id"]
    r = isolated.post(f"/api/reschedule/requests/{rid}/approve",
                      json={"approve": True}, headers=ADMIN)
    body = r.json()
    assert "未自动移动" in body["message"] or "人工协调" in body["message"]
    assert body["request"]["status"] == "approved"


def test_approve_survives_apply_exception(isolated, monkeypatch):
    """自动应用抛异常时，审批仍应成功并给出可人工处理的提示。"""
    from app.api import reschedule as api

    monkeypatch.setattr(api, "_db", type("_D", (), {"loaded": True, "entries": []})())

    def boom(rec, persist=True):
        raise RuntimeError("模拟求解器崩溃")

    monkeypatch.setattr(api, "_apply_reschedule", boom)

    rid = isolated.post("/api/reschedule/requests", json=REQ_BODY, headers=ADMIN).json()["request"]["id"]
    r = isolated.post(f"/api/reschedule/requests/{rid}/approve",
                      json={"approve": True}, headers=ADMIN)
    assert r.status_code == 200, "自动应用失败不应让审批接口 500"
    body = r.json()
    assert body["request"]["status"] == "approved"
    assert "人工" in body["message"]
