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

锁五条契约（R60 追加两条、R61 追加两条，见文末）：
1. `POST /parallel/land` 的响应带 `landed_by`，且库里 `actor` 真是那个管理员；
2. `GET /parallel/land/history` 存在、要管理员、返回 `{total,active,revoked,history}`；
3. 撤销后该条在历史里标成 `revoked=true` 并带上 `revoked_by`（不删行）；
4. `GET /parallel/land/status` 带 `landed_by`；
5. 无令牌访问 history → 401（不能因为"只是看看历史"就放开）。

R60 追加两条（R50 断点②）：
6. `POST /parallel/land` 的响应里有**人话** `message`，四个差异账齐全；
7. 同一方案再落一次（无差异）时，`message` 明说"没有任何变化"而不是复读"已落地"。
   —— 这两条守的是"回执"本身：字段没了，前端会把它渲染成空白，而不是报错。

R61 追加两条（撤销失败分型）：
8. 没有可撤销的记录 → **404**（资源不存在，语义正确）；
9. 课表**已经**回滚、只是撤销标记写不进库 → **500**，且 `rolled_back` /
   `restored_entries` / `detail` 必须都在。此前这一型也报 404，界面会说
   「没有可撤销的记录」—— 而安排表其实已经退回落地前了。
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


# ------------------------------------------------------------------ R60 追加

def test_land_commit_returns_human_message(env):
    """落地回执里必须有一句人话，且四个差异账都在。

    R50 断点②：这一步是整条链路里唯一拿不到 message 的写操作，前端只好自己拼
    文案 —— 于是"落地成功"在界面上可能只剩一个数字。字段被回退时，前端会渲染
    出空白而不是报错，**这类缺失只有接线测试守得住**。
    """
    client, headers = env
    r = client.post("/api/reschedule/parallel/land", json=PLAN, headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    msg = body.get("message")
    assert isinstance(msg, str) and msg, f"落地回执没有 message：{body.keys()}"
    assert msg.startswith("✅ 已落地 1 条方案行（1 个单元）"), msg
    for frag in ("新增 0 个单元", "调整 1 个", "未覆盖保留 1 个（1 行）", "原样保留 0 个"):
        assert frag in msg, f"回执缺账目「{frag}」：{msg}"
    assert "安排表仍 2 条" in msg, msg
    assert "由 admin 落地" in msg, msg


def test_land_commit_message_admits_no_change(env):
    """同一方案再落一次：回执必须说"没有任何变化"，不能复读"已落地 N 条"。

    第二次落地时接口仍返回 200、库里仍多一条记录，但安排表一个字节都没动。
    若回执照抄第一次的措辞，教秘会以为又改了一轮 —— 这正是"写路径不实"。
    """
    client, headers = env
    first = client.post("/api/reschedule/parallel/land", json=PLAN, headers=headers).json()
    assert "没有任何变化" not in first["message"]

    second = client.post("/api/reschedule/parallel/land", json=PLAN, headers=headers)
    assert second.status_code == 200, second.text
    msg = second.json()["message"]
    assert "没有任何变化" in msg, msg
    assert "1 个单元与现有安排完全一致" in msg, msg
    assert "已落地" not in msg, f"无差异时不能说「已落地」：{msg}"


# ------------------------------------------------------------------ R61 追加

def test_undo_without_record_is_404(env):
    """没有可撤销的记录 → 404。这一型是对的，不能被下一条改动带走。"""
    client, headers = env
    r = client.post("/api/reschedule/parallel/land/undo", headers=headers)

    assert r.status_code == 404, f"空库撤销应报 404，实得 {r.status_code}：{r.text}"
    assert "没有可撤销" in r.json().get("detail", ""), r.json()


def test_undo_revoke_failure_is_500_and_says_rolled_back(env, monkeypatch):
    """课表已回滚、撤销标记写失败 → 必须是 500，且"已回滚"这个事实不许丢。

    此前这里一律 404：界面写"没有可撤销的记录"，而安排表其实已经退回落地前。
    更贵的是 `rolled_back` / `restored_entries` 随异常一起被丢掉 —— 前端无从判断
    "该不该再点一次撤销"（该），只会弹一句通用报错，教秘转头去重新落地，
    把刚退掉的方案又落回去。

    所以这条同时锁三件事：状态码不是 404、结构体还在、人话在 `detail` 里。
    """
    client, headers = env
    client.post("/api/reschedule/parallel/land", json=PLAN, headers=headers)

    def boom(self, *args, **kwargs):
        raise RuntimeError("disk I/O error")

    # 重试两次都失败才走这一支，所以 patch 整个方法即可（两次调用都会炸）
    monkeypatch.setattr(api.plan_landing.LandingStore, "revoke", boom)

    r = client.post("/api/reschedule/parallel/land/undo", headers=headers)
    assert r.status_code != 404, (
        f"课表已回滚却报 404 —— 界面会说「没有可撤销的记录」：{r.text}")
    assert r.status_code == 500, f"服务端自己没写完，应是 500，实得 {r.status_code}"

    body = r.json()
    assert body.get("rolled_back") is True, f"「已回滚」这条事实被吞了：{body}"
    assert body.get("revoked") is False, body
    assert body.get("restored_entries") == 2, body
    # 通用错误翻译读的是 detail 字符串；只留 error 字段的话前端拿不到人话
    detail = body.get("detail", "")
    assert "已回滚到落地前" in detail, detail
    assert "disk I/O error" in detail, f"两次失败原因没报出来：{detail}"

    # 记录仍未被标撤销 —— 库与内存此刻不一致，这正是"必须重试"的理由
    h = client.get("/api/reschedule/parallel/land/history", headers=headers).json()
    assert h["history"][0]["revoked"] is False
