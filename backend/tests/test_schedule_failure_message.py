"""排课失败时不能输出一份「成功的定位说明」（R65）。

缺陷
----
`auto_schedule` 路由对 `scheduler.solve()` 的结果**不区分成功与失败**，
无条件拼出整段定位说明：

```python
result = await run_in_threadpool(scheduler.solve, ...)
...
note = (f"📌 定位说明：自动排课为辅助初稿生成。全表共 {total_all} 个教学段"
        ... f"共排定 {solved} 段 / {solved_groups} 个组课时。" ...)
result["summary_text"] = note + "\n\n" + scheduler.summary(result)
```

实测（空表 → `solve()` 返回 `{'success': False, 'error': '没有可排课的教学任务'}`）：

```
success: False
message: 📌 定位说明：自动排课为辅助初稿生成。全表共 0 个教学段……
         本次求解 0 个教学段；共排定 0 段 / 0 个组课时。
         ……未排入的 0 个任务/组已逐条列出原因……
```

**真正的原因「没有可排课的教学任务」一个字都没出现。**

### 为什么这比"报错"更糟

教秘看到的是一份**结构完整、术语专业、语气肯定**的说明文档，
唯一的异常是数字都是 0。他会怀疑是数据没加载、或者自己点错了，
然后反复重试 —— 而系统其实已经明确知道原因了。

同一个形状在三处存在（`solve` / `solve_all` / `parallel_solve`）：
**错误分支只返回 `{success, error}`，而调用方按成功路径拼文案。**

### 修法

- 失败时**直接返回 `error`**，不拼定位说明
- 只在成功路径上生成 `summary_text`
- `data` 里的字段仍给全（避免下游 `.get` 拿不到）

这与 R64 的「降级产物要标来源」同族：**说清楚发生了什么，
而不是给一份看起来正常的产物。**
"""
import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import app.api.reschedule as api
from app.utils.excel_parser import ScheduleDatabase


@pytest.fixture()
def empty_env(monkeypatch):
    db = ScheduleDatabase()
    db.entries = []
    db.loaded = True
    monkeypatch.setattr(api, "_db", db)
    api._master_data.__init__()
    api._master_tasks.clear()
    return api


def test_empty_table_reports_the_actual_reason(empty_env):
    """空表 → message 必须说「没有可排课的教学任务」，不能是定位说明。"""
    r = asyncio.run(empty_env.auto_schedule(None))

    assert r.get("success") is False
    msg = r.get("message") or ""
    assert "没有可排课的教学任务" in msg, (
        "真实原因被定位说明盖住了 —— 教秘会反复重试，而系统已经知道原因。"
        f"实际 message：{msg[:160]!r}"
    )
    assert "定位说明" not in msg, (
        "失败时不该输出「成功的定位说明」：那种结构完整、语气肯定的文档会让人以为只是数字为 0"
    )
    assert "共排定" not in msg, "失败时不该说「共排定 N 段」"


def test_failure_keeps_data_shape(empty_env):
    """失败时 data 里的字段仍要给全，避免下游 .get 拿到 None。"""
    r = asyncio.run(empty_env.auto_schedule(None))
    data = r.get("data") or {}
    for k in ("total_sections", "total_sections_all", "solved",
              "solved_groups", "conflicts"):
        assert k in data, f"失败响应缺字段 {k} —— 下游会拿到 None"


def test_no_rooms_reports_that_reason(empty_env, monkeypatch):
    """有任务但没教室 → 报「没有可用教室」。"""
    from app.utils.excel_parser import ScheduleEntry

    empty_env._db.entries = [
        ScheduleEntry(1, "周一", "567", "甲班", "1组", "项目A", "A101", "吴琳"),
    ]
    monkeypatch.setattr(empty_env._db, "get_all_rooms", lambda: [])

    r = asyncio.run(empty_env.auto_schedule(None))
    assert r.get("success") is False
    assert "教室" in (r.get("message") or ""), (
        f"没教室的原因没说清楚：{(r.get('message') or '')[:120]!r}"
    )
