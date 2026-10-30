"""落地链路的**接线**测试：端点层（R58）

为什么单独写这一层
------------------
R55 给 `plan_landing` 加了 `actor` / `revoke` / `history`，并新增了
`GET /parallel/land/history`。R56 的前端历史面板就靠这个端点。

但 R55 的 7 条用例**全部只测函数**（`LandingStore.save/revoke/history`、
`plan_landing.undo`），**没有一条打 HTTP**。于是当另一次推送把
`reschedule.py` 里那段端点整段回退掉时：

- 654 条后端用例全绿；
- `plan_landing.history()` 依然好使；
- 前端历史面板**静默消失**（面板对 404 做了降级，不报错）。

功能的"接线"没人守，函数再对也没用。**这层测试存在的唯一理由，就是这种事。**

锁五条契约：
1. `POST /parallel/land` 的响应带 `landed_by`，且库里 `actor` 真是那个管理员；
2. `GET /parallel/land/history` 存在、要管理员、返回 `{total,active,revoked,history}`；
3. 撤销后该条在历史里标成 `revoked=true` 并带上 `revoked_by`（不删行）；
4. `GET /parallel/land/status` 带 `landed_by`；
5. 无令牌访问 history → 401（不能因为"只是看看历史"就放开）。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import app.api.reschedule as api


@pytest.fixture()
def env(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.master_data import MasterData
    from app.security import issue_admin_token
    from app.utils.excel_parser import ScheduleDatabase, ScheduleEntry

    db = ScheduleDatabase()
    db.entries = [
        ScheduleEntry(1, "周一", "567", "甲班", "1组", "项目A", "A101", "吴老师"),
        ScheduleEntry(2, "周一", "567", "乙班", "1组", "项目B", "A102", "张老师"),
    ]
    db.pending = []
    db.loaded = True

    md = MasterData()
    md.build_from_entries(db.entries)
    monkeypatch.setattr(api, "_db", db)
    monkeypatch.setattr(api, "_master_data", md)
    monkeypatch.setattr(api, "_master_tasks", [])
    monkeypatch.setattr(api, "_DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setattr(api, "_landing_store", None)
    api._init_db()

    client = TestClient(app)
    headers = {"Authorization": "Bearer " + issue_admin_token("admin")["token"]}
    return client, headers


PLAN = {"schedule": [
    {"class_name": "甲班", "project": "项目A", "group": "1组",
     "week": 3, "weekday": "周三", "time_slot": "91011",
     "room": "A201", "teacher": "吴老师"},
]}


def test_land_commit_records_the_admin_actor(env):
    """落地响应与库里都要能看出"谁落的"。"""
    client, headers = env
    r = client.post("/api/reschedule/parallel/land",
                    json={**PLAN, "note": "一键排全校"}, headers=headers)
    assert r.status_code == 200, r.text
    assert r.json().get("landed_by") == "admin", r.json()

    h = client.get("/api/reschedule/parallel/land/history", headers=headers)
    assert h.status_code == 200, h.text
    row = h.json()["history"][0]
    assert row["actor"] == "admin"
    assert row["note"] == "一键排全校"
    assert row["revoked"] is False


def test_history_endpoint_exists_and_has_shape(env):
    """端点必须在，且返回约定的四个字段。

    这条就是"接线检查"：R55 的用例全绿时，这个端点曾被整段删掉而无人发现。
    """
    client, headers = env
    h = client.get("/api/reschedule/parallel/land/history", headers=headers)

    assert h.status_code == 200, f"history 端点不见了（{h.status_code}）"
    body = h.json()
    for k in ("success", "total", "active", "revoked", "history"):
        assert k in body, f"少了字段 {k}：{body}"


def test_undo_marks_revoked_and_keeps_the_row(env):
    """撤销后：历史里这条仍在，标成已撤销，并记下撤销人。"""
    client, headers = env
    client.post("/api/reschedule/parallel/land", json=PLAN, headers=headers)

    u = client.post("/api/reschedule/parallel/land/undo", headers=headers)
    assert u.status_code == 200, u.text
    assert u.json().get("revoked_by") == "admin"

    body = client.get("/api/reschedule/parallel/land/history", headers=headers).json()
    assert body["total"] == 1, "撤销把记录删掉了"
    assert body["revoked"] == 1
    row = body["history"][0]
    assert row["revoked"] is True
    assert row["revoked_by"] == "admin"
    assert row["revoked_at"]


def test_status_reports_landed_by(env):
    """状态接口带"由谁落地"，界面才显示得出来。"""
    client, headers = env
    client.post("/api/reschedule/parallel/land", json=PLAN, headers=headers)

    s = client.get("/api/reschedule/parallel/land/status", headers=headers)
    assert s.status_code == 200, s.text
    assert s.json().get("landed") is True
    assert s.json().get("landed_by") == "admin"


def test_history_requires_admin(env):
    """历史里有操作者与时间，属管理信息 —— 无令牌必须 401。"""
    client, _headers = env
    r = client.get("/api/reschedule/parallel/land/history")

    assert r.status_code == 401, f"历史接口被裸奔访问了：{r.status_code}"
