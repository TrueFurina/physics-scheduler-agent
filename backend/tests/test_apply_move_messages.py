"""`/apply-move` 端点「审批台文案」契约测试（R-105）。

为什么单独一个文件：`test_apply_move.py` 锁的是「记录有没有真的搬动」，
但端点最后那段**给用户看的消息拼装**（3325-3382）一直是零覆盖 —— 而这正是
R-78/R-79 反复踩的坑：审批台按文案断言（"全校一键排课 · X/Y 个班排满"那类），
文案写错一个字，前端探针就傻等超时。这里专门锁三种结局文案：

  ▸ 目标周撞核心冲突（3368-3371）：必须明说「核心冲突」+ 冲突类别，不能假装成功；
  ▸ 找不到可移动记录（3372-3373）：必须明说「未找到可自动移动的排课记录」；
  ▸ 部分成功部分跳过（3366-3367）：移动了几条 + 几条因冲突没动，两条数都要交代。

全部走真实 HTTP 层（TestClient）+ 演示管理员令牌，断言的是**返回消息本身**，
不是只跑通。
"""

import pytest

from app import security as _security
from app.utils.excel_parser import ScheduleDatabase, ScheduleEntry

from app.api import reschedule as R


TEACHER = "吴琳"
COURSE = "扭摆法测物体转动惯量"
CLASS = "2024级应用化学1、2班"
ORIGINAL = "周二 13:30-15:45"


@pytest.fixture
def db(tmp_path, monkeypatch):
    """一张小表：吴琳同门课连排 3/4/5 周（A504），第 9 周同教室被别人占。"""
    d = ScheduleDatabase()
    d.entries = [
        ScheduleEntry(3, "周二", "567", CLASS, "24应化2_1", COURSE, "A504", TEACHER),
        ScheduleEntry(4, "周二", "567", CLASS, "24应化2_1", COURSE, "A504", TEACHER),
        ScheduleEntry(5, "周二", "567", CLASS, "24应化2_1", COURSE, "A504", TEACHER),
        # 第 9 周 A504 被别人占了 —— 吴琳的课要挪到第 9 周必撞核心冲突
        ScheduleEntry(9, "周二", "567", "别的班", "别班_1", "声速测量", "A504", "陈老师"),
    ]
    d.loaded = True
    monkeypatch.setattr(R, "_db", d)
    monkeypatch.setattr(R, "_DB_PATH", str(tmp_path / "t.db"))
    R._init_db()
    return d


def _admin_token(client):
    login = client.post("/api/reschedule/admin-login",
                        json={"username": _security.ADMIN_USER, "password": _security.ADMIN_PASS})
    assert login.status_code == 200, login.text
    return login.json()["token"]


def _apply(client, token, **kw):
    payload = {
        "teacher": TEACHER, "course_name": COURSE, "class_name": CLASS,
        "original_time": ORIGINAL, "key": "cloud-msg",
    }
    payload.update(kw)
    return client.post("/api/reschedule/apply-move",
                       headers={"Authorization": f"Bearer {token}"}, json=payload)


# ---------------------------------------------------------------------------
# 1. 目标周撞核心冲突：文案必须点名「核心冲突」+ 冲突类别，绝不假装成功
# ---------------------------------------------------------------------------

def test_endpoint_message_names_core_conflict(db):
    """吴琳挪到第 9 周（A504 被别人占）→ 全跳过 + 文案含「核心冲突」。"""
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app) as client:
        token = _admin_token(client)
        resp = _apply(client, token, target_week=9)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    # 一条记录都没动（撞核心冲突必须如实跳过，不能硬塞出双占）
    assert body["data"]["applied"] == 0, body
    assert body["data"]["skipped"] >= 1, body
    # 文案（3368-3371）：必须明说核心冲突且点出「必须调整、不能强制通过」。
    # 收紧到这句专属短语（details 里写的是「撞上核心冲突」，不含「必须调整」），
    # 这样端点主文案被改动能真正让测试转红，而不是被跳过详情里的同词蒙混过去。
    assert "核心冲突必须调整" in body["message"], body["message"]


# ---------------------------------------------------------------------------
# 2. 找不到可移动记录：必须明说「未找到可自动移动的排课记录」
# ---------------------------------------------------------------------------

def test_endpoint_message_when_no_matching_record(db):
    """教师/课程在表里根本没有对应记录 → 文案必须点明「未找到」，不能返回假成功。"""
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app) as client:
        token = _admin_token(client)
        resp = _apply(client, token, teacher="查无此人", target_week=7)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["data"]["applied"] == 0 and body["data"]["skipped"] == 0, body
    # 文案（3372-3373）：教秘必须一眼看出「审批了但什么也没发生」
    assert "未找到可自动移动的排课记录" in body["message"], body["message"]


# ---------------------------------------------------------------------------
# 3. 部分成功部分跳过：移动了几条 + 几条因冲突没动，两条数都要交代（3366-3367）
# ---------------------------------------------------------------------------

def test_endpoint_message_partial_skip(tmp_path, monkeypatch):
    """吴琳同门课在源周有两节（A504 + A505），挪到第 7 周：

      · 第 3 周 A504 那节 → 目标周 A504 被陈老师占（核心冲突），但 A505 空 →
        自动改签到 A505（3364-3365「其中 N 条自动改签到空闲时段」）；
      · 第 3 周 A505 那节 → 目标周 A505 此时已被上一节改签占满（同教师同组），
        且本表只有 A504/A505 两间、再无空位 → 跳过（3366-3367「M 条未移动」）。

    一条测试同时锁住 3363-3367 的全部子分支：applied=1, rescheduled=1, skipped=1。
    """
    from fastapi.testclient import TestClient
    from app.main import app

    d = ScheduleDatabase()
    d.entries = [
        # 源周第 3 周：吴琳同门课两个实验室（A504 一节、A505 一节）
        ScheduleEntry(3, "周二", "567", CLASS, "24应化2_1", COURSE, "A504", TEACHER),
        ScheduleEntry(3, "周二", "567", CLASS, "24应化2_1", COURSE, "A505", TEACHER),
        # 目标周第 7 周：A504 被陈老师占（逼 A504 那节改签到 A505）
        ScheduleEntry(7, "周二", "567", "别的班", "别班_1", "声速测量", "A504", "陈老师"),
    ]
    d.loaded = True
    monkeypatch.setattr(R, "_db", d)
    monkeypatch.setattr(R, "_DB_PATH", str(tmp_path / "t2.db"))
    R._init_db()

    with TestClient(app) as client:
        token = _admin_token(client)
        resp = _apply(client, token, target_week=7)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["data"]["applied"] == 1, body
    assert body["data"]["skipped"] == 1, body
    msg = body["message"]
    # 3363 主句：移动了几条
    assert "已移动 1 条" in msg, msg
    # 3364-3365 改签子句：有几条自动改签到空闲时段
    assert "其中 1 条自动改签到空闲时段" in msg, msg
    # 3366-3367 跳过子句：几条因目标周无空闲时段没动
    assert "1 条因目标周无空闲时段未移动" in msg, msg
