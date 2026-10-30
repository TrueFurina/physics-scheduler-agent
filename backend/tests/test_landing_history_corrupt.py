"""落地历史里坏记录不能显示成"0 行方案"（R63）。

缺陷
----
`LandingStore.history()`：

```python
try:
    n_rows = len(json.loads(r["plan_json"]))
except Exception:
    n_rows = 0              # ← 解析失败也算 0 行
```

实测（塞一条 `plan_json` 是坏 JSON 的记录）：

```
[记录数] 1
[plan_rows] 0
[note] 坏方案
```

**和"这方案真的落了 0 行"完全无法区分。**

### 为什么危险

`/parallel/land/history` 是 R58 刚补回的端点，用途是排查
「上周谁落过方案、影响多少条」。教秘看到某条记录 `plan_rows: 0`，
会理解为"那次落地什么也没改" —— 而真相是**记录本身坏了**。

这两件事的处置完全不同：

| 真实情况 | 该怎么办 |
|---|---|
| 方案确实 0 行 | 正常，不用管 |
| 记录损坏 | **要查为什么坏了**（磁盘？并发写？） |

这是 R50/R51/R60 之后**第四次**撞上同一形状：
把"未知"塌缩成一个看起来正常的值。

### 修法

- 解析失败时 `plan_rows` 给 `None`（JSON 里是 `null`），而不是 `0`
- 同时给一个 `plan_json_corrupt: true` 标记，让界面能直接判断
- 前端 `null` 显示成「?」或「无法解析」，而不是「0」
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import app.api.reschedule as api
import app.plan_landing as pl


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "_DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setattr(api, "_landing_store", None)
    api._init_db()
    return api._landings()


def _insert(store, plan_json, note="x"):
    conn = store._conn()
    conn.execute(
        "INSERT INTO landed_plans"
        " (plan_json, before_json, note, after_entries, landed_at, actor)"
        " VALUES (?,?,?,?,?,?)",
        (plan_json, "[]", note, 0, "2026-10-30T00:00:00", "admin"))
    conn.commit()
    conn.close()


def test_corrupt_plan_json_is_null_not_zero(store):
    """坏 JSON → plan_rows 必须是 None，不能是 0。"""
    _insert(store, "{这不是 JSON", note="坏方案")
    rows = store.history()
    assert len(rows) == 1

    r = rows[0]
    assert r["plan_rows"] is None, (
        f"坏记录显示 plan_rows={r['plan_rows']!r} —— "
        "和「方案确实 0 行」无法区分，教秘会以为那次落地什么也没改"
    )
    assert r.get("plan_json_corrupt") is True, (
        "没有标记「这条记录坏了」，界面只能看到一个 null 却不知道为什么"
    )


def test_valid_empty_plan_stays_zero(store):
    """合法但空的方案 → 仍然是 0（不能把所有 0 都当成损坏）。"""
    _insert(store, "[]", note="空方案")
    rows = store.history()
    assert rows[0]["plan_rows"] == 0
    assert not rows[0].get("plan_json_corrupt")


def test_normal_plan_counts_rows(store):
    """正常方案 → 行数正确，且没有损坏标记。"""
    _insert(store, json.dumps([{"a": 1}, {"a": 2}, {"a": 3}]), note="三行")
    rows = store.history()
    assert rows[0]["plan_rows"] == 3
    assert not rows[0].get("plan_json_corrupt")
