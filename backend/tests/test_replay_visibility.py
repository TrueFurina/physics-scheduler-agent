"""重启重放的**失败可见性**契约（R-120）。

为什么单开一个文件
------------------
`_load_default_schedule()` 是"重启后状态能不能回来"的唯一路径：
先整表重载 Excel，再按 `applied_moves` + `landed_plans` 的时间戳顺序重放
写事件。它守着三件事（R-18 内存态 / R-36 审批闭环 / 落地采纳）。

原先这段代码有四层 `except Exception: pass`（两条重放分支各一处、外层两处）。
后果不是崩溃，而是**静默**：

    ✅ 成功加载 433 条排课记录

教秘看到这句话，会认为"一切正常"。而他上周采纳的落地方案、批过的调课
如果有一条没能重放，界面**没有任何线索** —— 他要等到某天发现"这张课表
怎么不对"才会回头查。

这与 R-35（时段含奇怪字符导致整行蒸发）、R-58（时段无法识别被丢）
是同一族。本项目已经反复吃过"静默丢弃"的亏，这是最后一处。

本文件锁三件事：
  1. 坏记录必须被**计数**并**说清原因**（含第一例的具体异常）。
  2. 好的记录不能因为旁边有坏的就被跳过（单条失败不该拖垮整表）。
  3. 没有失败时**不得**出现任何重放噪音（否则"警告"会变成背景音）。
"""

import json
import sqlite3
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from app.api import reschedule as R  # noqa: E402


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    """临时 sqlite + 让 `_landings()` 跟着走（否则会写真实 requests.db）。"""
    monkeypatch.setattr(R, "_DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setattr(R, "_landing_store", None)
    monkeypatch.setattr(R, "_REPLAY_FAILED", [], raising=False)
    R._init_db()
    return R


def _add_move(request_data: str, applied_at: str, key: str = "") -> None:
    conn = R._get_db()
    try:
        conn.execute(
            "INSERT INTO applied_moves (request_data, applied_at, idem_key) VALUES (?,?,?)",
            (request_data, applied_at, key),
        )
        conn.commit()
    finally:
        conn.close()


VALID_MOVE = {
    "teacher": "吴琳", "course_name": "声速测量", "class_name": "2024级应用化学1、2班",
    "original_time": "周二 13:30-15:45", "target_week": 9, "source_week": 0,
}


def test_bad_record_is_counted_and_explained(fresh_db):
    """← 核心：坏记录必须报出条数 + 第一例的具体原因。

    只报"有 1 条失败"没有用 —— 教秘/管理员无从判断是数据坏了还是代码坏了。
    给出异常类型与消息（这里 JSONDecodeError 还带列号）通常就够定位。
    """
    _add_move("{ 这不是 JSON", "2026-01-01T00:00:00", "bad-1")

    msg, _ = R._load_default_schedule()

    assert "未能重放" in msg, f"坏记录被静默吞掉了：\n{msg}"
    assert "1 条" in msg, f"没说清失败条数：\n{msg}"
    assert "JSONDecodeError" in msg, f"没说清原因：\n{msg}"
    # 结构化记录也要留痕，供 /status 回查
    assert len(R._REPLAY_FAILED) == 1
    assert R._REPLAY_FAILED[0]["why"].startswith("JSONDecodeError")
    assert R._REPLAY_FAILED[0]["kind"] == "调课"


def test_good_record_not_blocked_by_bad_neighbour(fresh_db):
    """单条坏数据不能拖垮整表：好的那条仍应被重放。"""
    _add_move("{ 坏", "2026-01-01T00:00:00", "bad-1")
    _add_move(json.dumps(VALID_MOVE, ensure_ascii=False), "2026-01-02T00:00:00", "ok-1")

    msg, n = R._load_default_schedule()

    assert n == 433, f"表本身应当照常加载：{n}"
    assert "1 条未能重放" in msg, msg
    # 成功的那条要么动了记录（applied>0），要么如实说"未找到匹配"（applied==0
    # 但 skipped 也为 0 且 details 有说明）—— 两者都算"被处理过"。
    # 这里只要求：它没有被算进失败。
    assert len(R._REPLAY_FAILED) == 1, f"好的那条被误判为失败：{R._REPLAY_FAILED}"


def test_no_noise_when_all_replayed(fresh_db):
    """没有任何重放记录时，消息里不得出现"未能重放"。"""
    msg, _ = R._load_default_schedule()
    assert "未能重放" not in msg, f"干净库出现了重放警告：\n{msg}"
    assert R._REPLAY_FAILED == []


def test_status_exposes_replay_failures(fresh_db):
    """`/status` 必须能回查到上次重放失败 —— 消息会被后续操作覆盖。"""
    from fastapi.testclient import TestClient
    from app.main import app

    _add_move("{ 坏", "2026-01-01T00:00:00", "bad-1")
    R._load_default_schedule()

    body = TestClient(app).get("/api/reschedule/status").json()
    assert "replay_failed" in body, f"/status 缺少 replay_failed：{list(body)}"
    assert len(body["replay_failed"]) == 1
    assert body["replay_failed"][0]["kind"] == "调课"


def test_landing_replay_failure_is_reported(fresh_db):
    """落地方案那条分支也要报 —— 两条分支各有自己的 try，容易只修一处。"""
    store = R._landings()
    store.init_schema()
    # 直接往 landed_plans 塞一条 plan 非 list 的记录（replay_landing 会炸）
    conn = sqlite3.connect(R._DB_PATH)
    try:
        conn.execute(
            "INSERT INTO landed_plans (landed_at, plan_json, before_json, after_entries, note)"
            " VALUES (?,?,?,?,?)",
            ("2026-01-03T00:00:00", "{}", "{}", 0, "坏记录"),
        )
        conn.commit()
    finally:
        conn.close()

    msg, _ = R._load_default_schedule()
    assert "未能重放" in msg, f"落地方案的失败被吞了：\n{msg}"
    assert any(x["kind"] == "落地方案" for x in R._REPLAY_FAILED), R._REPLAY_FAILED
