"""`_load_default_schedule()` 的外层守卫仍在静默（R51 · 缺口①）。

缺陷
----
内层两条重放已在 R62 修好：坏记录 → `replay_failed.append(...)` → 消息里报 ⚠️。
**但外层 try 的处理器还是裸 `except Exception: pass`，而且连 `as e` 都没有**：

```
1049  try:
1050      conn = _get_db()              ← 任一失败
1054      lands = _landings().all()     ←
1069      events.sort(...)              ←
1081  except Exception:
1082      pass
1084  msg = f"✅ 成功加载 {len(_db.entries)} 条排课记录"
```

异常一旦发生在这三处，整个重放被跳过；而 `replay_failed` 仍是**空列表**
（所有 `append` 都在这个 `try` 的内层），于是消息照样是「✅ 成功加载 N 条」。

⇒ R62 注释里那句「不阻断启动，但**绝不静默**」，
在最需要它的那一支上并不成立。

### 为什么这条比 R62 那三条更该有测试

R62 的 `test_replay_honesty.py` 塞的是**坏记录**，走的是内层 `append`。
但"读不到 applied_moves 表、读不到落地记录、排序抛错"这类故障**不经过内层** ——
它们把整个重放段跳过，一条留痕都不产生。

而这正是最危险的时刻：数据库打不开时，"没有已审批的调课要重放"
与"调课全都没重放"在消息里长得**一模一样**。

### 取舍

不改成"失败就抛"：数据损坏不该让服务起不来（既定取舍，见 R62 注释）。
只要求**说出声** —— 把异常带进 `replay_failed`，复用 `:1088` 已有的 ⚠️ 汇总分支。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import app.api.reschedule as api
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
    monkeypatch.setattr(api, "_schedule_fp", "fp-test")
    return api


def _assert_replay_trouble_reported(msg, where):
    """消息里必须出现"重放出了问题"的痕迹，而不是只有「✅ 成功加载」。"""
    said_ok = "成功加载" in msg
    said_trouble = ("重放" in msg) and (("失败" in msg) or ("中断" in msg))
    assert said_trouble, (
        f"{where} 抛了异常，整段重放被跳过，消息却只说「成功加载」——"
        f"数据库打不开与「没有调课要重放」在消息里长得一模一样。实际：{msg!r}"
    )
    assert said_ok, (
        f"{where} 失败不该把整表加载一起废掉（数据损坏不该让服务起不来）。实际：{msg!r}"
    )


def test_get_db_failure_is_reported(env, monkeypatch):
    """`_get_db()` 抛异常 → 必须报出来，且不能阻断整表加载。"""
    def boom():
        raise RuntimeError("database is locked (模拟)")

    monkeypatch.setattr(env, "_get_db", boom)

    msg, total = env._load_default_schedule()

    assert total > 0, "重放段故障不该连整表都不加载"
    _assert_replay_trouble_reported(msg, "_get_db()")


def test_landings_failure_is_reported(env, monkeypatch):
    """`_landings().all()` 抛异常 → 同样必须报出来。

    这条单独写，是因为它证明"不是只给 `_get_db()` 一处打了补丁" ——
    外层 `try` 里任何一个调用点坏了，表现都是整段静默跳过。
    """
    class _BrokenLandings:
        def all(self):
            raise RuntimeError("landing store 损坏 (模拟)")

    monkeypatch.setattr(env, "_landings", lambda: _BrokenLandings())

    msg, total = env._load_default_schedule()

    assert total > 0, "重放段故障不该连整表都不加载"
    _assert_replay_trouble_reported(msg, "_landings().all()")


def test_healthy_path_stays_quiet(env):
    """一切正常时不许冒出重放故障的噪音 —— 防止把守卫改成狼来了。"""
    msg, total = env._load_default_schedule()
    assert total > 0
    assert "重放失败" not in msg, f"没出问题却报了失败：{msg!r}"
    assert "重放中断" not in msg, f"没出问题却报了中断：{msg!r}"
