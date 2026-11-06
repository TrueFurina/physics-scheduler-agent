"""R86：/load-default 的降级必须给结构化信号，不能只藏在中文 message 里。

缺陷（探针实证 2026-11-07）
--------------------------
`_load_default_schedule()` 早在 R62/R51 就把重放失败、重放中断写进了
`message`，但 `/load-default` 只回三个字段：

```json
{
  "success": true,
  "message": "✅ 成功加载 433 条排课记录；⚠️ 1 条已审批记录**重放失败**，它们的调整未生效（…）",
  "total_entries": 433
}
```

而前端首屏 `loadTeacherList` 是 `await rescheduleApi.loadDefault();` —— 返回值
整个丢弃；`client.ts` 的类型里也没有任何降级字段。更糟的是 `/status.loaded`
转 true 之后前端再也不会调这个端点：**这段警告只在冷启动那一次响应里出现，
丢了就永久消失**。教秘看到的课表少了一节已审批的调课，且没有任何提示。

修法
----
- `_load_default_schedule` 把 `replay_failed` / `replay_aborted` 写进
  `_db.load_report`（不改函数签名，既有 20+ 调用点零影响）；
- `/load-default` 从**结构化状态**算出 `degraded` / `warnings` 一并返回；
- 前端接住返回值并在不覆盖用户消息的前提下显示横幅（见前端测试）。

为什么不算 `skipped`
--------------------
demo 表本身就带 1 条「时段无法识别」的行。把它算进 `degraded` 会让每次
首屏都弹警告 —— 狼来了比不报更糟（R62 `test_good_replay_still_silent`
的同族原则）。skipped 继续留在 message 里，`degraded` 只表示
「已审批变更没能生效」这种需要人工介入的降级。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi.testclient import TestClient
import app.api.reschedule as api
from app.main import app
from app.utils.excel_parser import ScheduleDatabase

REAL = os.path.join(os.path.dirname(__file__), "..", "..", "schedule.demo.xlsx")


@pytest.fixture()
def env(tmp_path, monkeypatch):
    import app.master_store as ms

    if not os.path.isfile(REAL):
        pytest.skip("缺少 schedule.demo.xlsx")
    monkeypatch.setattr(api, "_DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setattr(ms, "STORE_PATH", str(tmp_path / "s.json"))
    monkeypatch.setattr(api, "_landing_store", None)
    api._init_db()

    db = ScheduleDatabase()
    db.load_from_excel(REAL)
    monkeypatch.setattr(api, "_db", db)
    monkeypatch.setattr(api, "_schedule_fp", "fp-r86")
    return api


def _insert_move(payload, ts):
    conn = api._get_db()
    conn.execute(
        "INSERT INTO applied_moves (request_data, applied_at, idem_key) VALUES (?,?,?)",
        (payload, ts, "k-" + ts))
    conn.commit()
    conn.close()


def _post():
    with TestClient(app) as client:
        r = client.post("/api/reschedule/load-default")
    assert r.status_code == 200, r.text
    return r.json()


def test_replay_failure_is_structurally_degraded(env):
    """坏记录重放失败 → 必须有 degraded=True 与可读的 warnings。"""
    _insert_move("{这不是 JSON", "2026-11-07T00:00:00")
    body = _post()
    assert "degraded" in body, (
        "响应里没有 degraded —— 降级只藏在 message 里，前端拿不到结构化信号")
    assert body["degraded"] is True, "重放失败了却说未降级"
    warnings = body.get("warnings")
    assert isinstance(warnings, list) and warnings, "降级了却没有 warnings 明细"
    assert all(isinstance(w, str) for w in warnings), "warnings 必须能直接上屏"
    assert any("重放失败" in w for w in warnings), warnings


def test_replay_failure_count_is_not_lost(env):
    """两条坏记录 → warnings 必须报「2」，不许笼统说「有失败」。"""
    _insert_move("{坏一", "2026-11-07T00:00:01")
    _insert_move("{坏二", "2026-11-07T00:00:02")
    body = _post()
    assert body["degraded"] is True
    assert any("2" in w for w in body["warnings"]), (
        f"两条失败被说成一条/没报数：{body['warnings']}")


def test_replay_abort_is_structurally_degraded(env, monkeypatch):
    """外层兜底（_get_db 抛异常）→ 整段没重放，必须报「中断」并带异常。"""
    def boom():
        raise RuntimeError("database is locked (模拟)")

    monkeypatch.setattr(env, "_get_db", boom)
    body = _post()
    assert body["degraded"] is True
    assert any("中断" in w for w in body["warnings"]), body["warnings"]
    assert any("RuntimeError" in w for w in body["warnings"]), (
        f"中断警告没带可操作的原因：{body['warnings']}")


def test_healthy_load_is_not_degraded(env):
    """防放宽：没有重放故障时绝不许报降级（否则每次首屏都弹狼来了）。"""
    body = _post()
    assert body["degraded"] is False, (
        f"demo 表本身带 1 条 skipped，但那不是重放故障：{body}")
    assert body.get("warnings") == [], f"健康路径冒出警告噪音：{body['warnings']}"


def test_skipped_rows_alone_do_not_degrade(env, monkeypatch):
    """防放宽：只有无法识别时段的行，不算需要人工介入的降级。"""
    real = env._load_default_schedule

    def fake_load():
        msg, total = real()
        env._db.load_report["skipped"] = 3
        return msg, total

    monkeypatch.setattr(env, "_load_default_schedule", fake_load)
    body = _post()
    assert body["degraded"] is False, (
        "把 skipped 算成 degraded 会让正常首屏常年挂警告")


def test_legacy_contract_fields_survive(env):
    """防放宽：不许靠把 success 改成 False / 删 message 来「解决」问题。"""
    body = _post()
    assert body["success"] is True, "success 恒 True 是既有契约，前端已依赖"
    assert "成功加载" in body["message"], "message 的原有文案不能丢"
    assert body["total_entries"] == len(env._db.entries), "记录数必须仍然真实"


def test_load_report_holds_structured_replay_state(env):
    """结构化落点：重放状态必须进 load_report，而不是只拼进中文字符串。"""
    _insert_move("{坏", "2026-11-07T00:00:03")
    env._load_default_schedule()
    rep = env._db.load_report
    assert "replay_failed" in rep, "load_report 里没有结构化失败列表"
    assert "replay_aborted" in rep, "load_report 里没有结构化中断原因"
    assert isinstance(rep["replay_failed"], list) and len(rep["replay_failed"]) == 1
    assert rep["replay_aborted"] is None


def test_degraded_reads_report_not_message_wording(env, monkeypatch):
    """防糊弄：把 message 伪装成「一切正常」也不能掩盖结构化降级。"""
    def fake_load():
        env._db.load_report = dict(env._db.load_report, replay_failed=["#9: 模拟"],
                                   replay_aborted=None)
        return "✅ 一切正常", 433

    monkeypatch.setattr(env, "_load_default_schedule", fake_load)
    body = _post()
    assert body["degraded"] is True, (
        "靠 `⚠️ in message` 嗅探会被这句正常文案骗过去 —— 必须读结构化状态")


def test_warnings_are_empty_list_when_no_replay_state(env, monkeypatch):
    """防炸：老版本 load_report 没有这两个键时也不能 500。"""
    def fake_load():
        rep = env._db.load_report
        rep.pop("replay_failed", None)
        rep.pop("replay_aborted", None)
        return "✅ 加载完成", 433

    monkeypatch.setattr(env, "_load_default_schedule", fake_load)
    body = _post()
    assert body["degraded"] is False
    assert body["warnings"] == []
