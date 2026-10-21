"""审批闭环的单测：`_apply_reschedule` + `/apply-move`。

为什么单独立一个文件：
  这条写路径此前**零覆盖** —— 现有测试全在验"沙盘算得对不对"，
  没有任何一条验"教秘点完同意之后，课表到底有没有变"。
  而这正是最容易静默失败的地方：审批台显示"已同意"，课表一个字节没动，
  页面不会报错，用户不会怀疑。

本文件只用小的内存表构造场景（不读真实 xlsx），断言的是**数据本身**：
周次有没有变、变了哪几条、冲突时是不是如实跳过、重复调用是不是幂等。
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.utils.excel_parser import ScheduleDatabase, ScheduleEntry

from app.api import reschedule as R


TEACHER = "吴琳"
COURSE = "扭摆法测物体转动惯量"
CLASS = "2024级应用化学1、2班"
ORIGINAL = "周二 13:30-15:45"


@pytest.fixture
def db(tmp_path, monkeypatch):
    """一张 3 条记录的小表 + 一个临时 SQLite（绝不碰真实的 requests.db）。"""
    d = ScheduleDatabase()
    d.entries = [
        # 吴琳 同一门课连排三周（第 3/4/5 周）
        ScheduleEntry(3, "周二", "567", CLASS, "24应化2_1", COURSE, "A504", TEACHER),
        ScheduleEntry(4, "周二", "567", CLASS, "24应化2_1", COURSE, "A504", TEACHER),
        ScheduleEntry(5, "周二", "567", CLASS, "24应化2_1", COURSE, "A504", TEACHER),
        # 第 9 周同一间教室被别人占了 —— 移到第 9 周必须被跳过而不是硬塞
        ScheduleEntry(9, "周二", "567", "别的班", "别班_1", "声速测量", "A504", "陈老师"),
    ]
    d.loaded = True
    monkeypatch.setattr(R, "_db", d)
    monkeypatch.setattr(R, "_DB_PATH", str(tmp_path / "t.db"))
    R._init_db()
    return d


def _record(target_week: int, **kw) -> dict:
    rec = {
        "teacher": TEACHER,
        "course_name": COURSE,
        "class_name": CLASS,
        "original_time": ORIGINAL,
        "target_week": target_week,
        "source_week": 0,
    }
    rec.update(kw)
    return rec


def _weeks(db, teacher=TEACHER):
    return sorted(e.week for e in db.entries if e.teacher == teacher)


# ---------- 1. 应用真的会改数据 ----------

def test_apply_moves_the_record_to_target_week(db):
    """核心承诺：审批通过 → 排课记录真的换周。"""
    before = _weeks(db)
    r = R._apply_reschedule(_record(7), persist=False)

    assert r["applied"] == 1, r
    # 源周 = 目标周之前最近的一周 = 第 5 周，所以是它被搬到第 7 周
    assert _weeks(db) == [3, 4, 7], f"期望第5周被移到第7周，实际 {_weeks(db)}（原来是 {before}）"


def test_apply_picks_source_week_before_target(db):
    """源周 = 目标周之前**最近**的匹配周，而不是最早的那条。

    否则"调到第7周"会顺手把第3周也搬走 —— 教秘只批了一次，却改了两周的课。
    """
    R._apply_reschedule(_record(7), persist=False)

    assert _weeks(db) == [3, 4, 7], f"只该动第5周（最近的），实际 {_weeks(db)} 全动了"


def test_apply_honours_explicit_source_week(db):
    """带 source_week 时按它移动，不再自动挑最近周。"""
    r = R._apply_reschedule(_record(7, source_week=3), persist=False)

    assert r["applied"] == 1, r
    assert _weeks(db) == [4, 5, 7], f"被移动的应当是第3周，实际 {_weeks(db)}"


# ---------- 2. 冲突必须如实跳过 ----------

def test_conflict_is_skipped_not_forced(db):
    """目标周该教室已被占 → 跳过并说明，绝不硬塞出双占。"""
    r = R._apply_reschedule(_record(9), persist=False)

    assert r["applied"] == 0, r
    assert r["skipped"] == 1, r
    assert any("已有安排" in d for d in r["details"]), r
    # 第 5 周那条原地不动（源周是第5周，它没被移走）
    assert _weeks(db) == [3, 4, 5], f"冲突时不该动任何记录，实际 {_weeks(db)}"


def test_no_match_says_so_instead_of_pretending(db):
    """匹配不到记录时要明说，不能返回一个"成功但什么也没发生"。"""
    r = R._apply_reschedule(_record(7, teacher="查无此人"), persist=False)

    assert r["applied"] == 0 and r["skipped"] == 0, r
    assert any("未找到" in d for d in r["details"]), r


# ---------- 3. 幂等 ----------

def test_same_key_applies_only_once(db):
    """同一张工单重复调用：第二次必须明确说"已应用过"，且不再动数据。"""
    rec = _record(7)
    first = R._apply_reschedule(rec, persist=True, idem_key="cloud-42")
    assert first["applied"] == 1, first
    weeks_after_first = _weeks(db)

    second = R._apply_reschedule(rec, persist=True, idem_key="cloud-42")
    assert second["applied"] == 0, f"重复应用又移动了记录：{second}"
    assert any("应用过" in d for d in second["details"]), second
    assert _weeks(db) == weeks_after_first


def test_different_keys_are_independent(db):
    """幂等键是"每张工单一次"，不是"每门课一次"——两张不同工单各自生效。"""
    R._apply_reschedule(_record(7), persist=True, idem_key="cloud-1")
    R._apply_reschedule(_record(8), persist=True, idem_key="cloud-2")

    # 第一次移走第5周（→7），第二次移走第7周（→8），剩下 3、4
    assert _weeks(db) == [3, 4, 8], f"两张工单应各移一条，实际 {_weeks(db)}"


def test_idem_key_is_persisted(db):
    """键必须落库：只在内存里判重的话，进程一重启就失效了。"""
    R._apply_reschedule(_record(7), persist=True, idem_key="cloud-42")

    conn = R._get_db()
    try:
        rows = conn.execute("SELECT idem_key FROM applied_moves").fetchall()
    finally:
        conn.close()
    assert [r["idem_key"] for r in rows] == ["cloud-42"]


# ---------- 4. HTTP 层：写接口不能匿名可调 ----------

def test_apply_move_endpoint_requires_admin(db):
    """`/apply-move` 会改排课数据，必须挂管理员门禁。"""
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app) as client:
        resp = client.post("/api/reschedule/apply-move", json={
            "teacher": TEACHER, "course_name": COURSE,
            "original_time": ORIGINAL, "target_week": 7, "key": "cloud-x",
        })

    assert resp.status_code in (401, 403), f"匿名调用居然放行了：HTTP {resp.status_code}"


def test_apply_move_endpoint_moves_data_with_token(db):
    """带令牌的真实调用路径：能移动，且返回里说清楚移动了几条。"""
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app) as client:
        login = client.post("/api/reschedule/admin-login",
                            json={"username": "admin", "password": "admin123"})
        assert login.status_code == 200, login.text
        token = login.json()["token"]

        resp = client.post("/api/reschedule/apply-move", headers={"Authorization": f"Bearer {token}"},
                           json={
                               "teacher": TEACHER, "course_name": COURSE, "class_name": CLASS,
                               "original_time": ORIGINAL, "target_week": 7, "key": "cloud-7",
                           })

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["data"]["applied"] == 1, body
    assert "已移动 1 条" in body["message"], body["message"]
    assert _weeks(db) == [3, 4, 7]
