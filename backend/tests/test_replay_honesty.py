"""重放失败必须报出来（R62）。

缺陷
----
`_load_default_schedule` 的重放循环：

```python
for r in moves:
    try:
        rec = json.loads(r["request_data"])
        res = _apply_reschedule(rec, persist=False)
        replay_applied += res["applied"]
    except Exception:
        pass          # ← 静默丢弃
```

实测（塞两条坏记录：一条 JSON 坏、一条字段为空）：

```
✅ 成功加载 433 条排课记录（⚠️ 1 条时段无法识别，未纳入冲突扫描）
```

**两条重放失败一个字都没提。**

### 为什么危险

重放的存在意义是"重启后不丢已审批的调课"。如果某条重放失败，
那条调课的**效果就此消失** —— 而教秘看到的是"✅ 成功加载"。

他会以为课表是完整的。实际有一节课停在旧周次，或者干脆没动。

这与项目反复踩的「静默不实」完全同族（R-35 静默丢弃行、R-36 审批没生效、
R50 坏落盘永久豁免）。而且这次后果更重：**丢的是已审批的变更**。

### 修法

- 计数：`replay_failed`
- 消息里明说「⚠️ N 条已审批记录重放失败」
- 给出可操作提示（哪条、为什么）

不阻断启动 —— 数据损坏不该让服务起不来。但**绝不能不说**。
"""
import json
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


def _insert_move(payload, ts):
    conn = api._get_db()
    conn.execute(
        "INSERT INTO applied_moves (request_data, applied_at, idem_key) VALUES (?,?,?)",
        (payload, ts, "k-" + ts))
    conn.commit()
    conn.close()


def test_replay_failure_is_counted_and_reported(env):
    """坏记录重放失败 → 必须计数并写进 message。"""
    _insert_move("{这不是 JSON", "2026-10-30T00:00:00")
    _insert_move("{}", "2026-10-30T00:00:01")

    msg, total = env._load_default_schedule()

    assert total > 0, "整表本身应该加载成功"
    assert ("重放失败" in msg) or ("失败" in msg and "重放" in msg), (
        f"重放失败了却只说「成功加载」—— 已审批的变更静默消失。实际：{msg!r}"
    )
    assert ("2" in msg) or ("条" in msg), "没有报出失败的条数"


def test_replay_failure_does_not_block_startup(env):
    """坏记录不能阻断启动 —— 数据损坏不该让服务起不来。"""
    _insert_move("{{{{", "2026-10-30T00:00:02")
    msg, total = env._load_default_schedule()
    assert total > 0, "有坏记录就不加载整表了 —— 那会把可用数据一起废掉"


def test_good_replay_still_silent(env):
    """没有坏记录时，不该冒出「重放失败」的噪音。"""
    msg, total = env._load_default_schedule()
    assert "重放失败" not in msg, f"没有坏记录却报了失败：{msg!r}"
