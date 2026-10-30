"""落地/撤销的审计留痕：撤销不能把这件事从历史里抹掉（R55）

缺陷（本轮实测，修复前）
------------------------
`landed_plans` 表存着"方案 + 落地前快照 + 时间"，本来是好设计。但撤销走的是：

```python
def pop_latest(self):
    rec = self.latest()
    conn.execute("DELETE FROM landed_plans WHERE id = ?", (rec["id"],))
```

**删行。** 于是教秘点一次"撤销"，这次落地在库里一根毛都不剩：
谁落的（本来就没记）、什么时候落的、改了哪些单元、影响多少条 —— 全没了。

而教秘点撤销的本意是"把课表恢复"，不是"把这件事从历史里抹掉"。
审计表和能 pop 的栈，在正常使用下看不出区别；出事时是"有据可查"与"什么都没有"。

同时补上 `actor`：`require_admin` 一直有返回值，但从来没被写进记录 ——
"谁点的采纳"只能事后问人。

四条硬约束（下面每条都有用例）
------------------------------
1. 撤销之后记录仍在，`revoked_at` 非空，`revoked_by` 记到人；
2. `/land/status` 必须说"没落地"（`latest()` 只认未撤销的）——
   否则界面会一边显示"已落地"、一边课表已经恢复，比没有状态更糟；
3. **重启重放不得复活已撤销的方案**（`all()` 过滤）。这条最危险：
   教秘撤销过的方案在重启后自己回来，而且没人点过任何按钮；
4. 老库（六列的老表）必须能自动补列，不许在"落地"那一刻报 no such column。
"""

import json
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import plan_landing
from app.plan_landing import LandingStore
from app.utils.excel_parser import ScheduleDatabase, ScheduleEntry


def _e(week, weekday, slot, cls, group, exp, room="A504", teacher="吴琳"):
    return ScheduleEntry(week, weekday, slot, cls, group, exp, room, teacher)


def _db():
    d = ScheduleDatabase()
    d.entries = [_e(3, "周一", "345", "24物理1班", "1组", "伏安法"),
                 _e(3, "周一", "678", "24物理1班", "1组", "声速测量")]
    d.loaded = True
    return d


PLAN = [
    {"week": 5, "weekday": "周二", "time_slot": "345", "class_name": "24物理1班",
     "group": "1组", "project": "伏安法", "room": "B201", "teacher": "吴琳"},
    {"week": 5, "weekday": "周二", "time_slot": "678", "class_name": "24物理1班",
     "group": "1组", "project": "声速测量", "room": "B202", "teacher": "吴琳"},
]


@pytest.fixture
def store(tmp_path):
    s = LandingStore(str(tmp_path / "land.db"))
    s.init_schema()
    return s


# ==================== 一、撤销不抹历史 ====================

def test_undo_keeps_the_record_and_marks_it_revoked(store):
    """撤销后：记录还在、标了时间、记了人。"""
    db = _db()
    before = plan_landing.snapshot(db)
    plan_landing.apply_plan(db, PLAN)
    store.save(PLAN, before, note="一键排全校", actor="admin")

    out = plan_landing.undo(db, store, actor="admin")

    assert out["success"] is True
    assert out["revoked_id"] == 1
    assert out["revoked_by"] == "admin"
    assert out["landed_actor"] == "admin"

    rows = store.history()
    assert len(rows) == 1, "撤销把记录删掉了 —— 历史没了"
    assert rows[0]["revoked"] is True
    assert rows[0]["revoked_at"]
    assert rows[0]["actor"] == "admin"
    assert rows[0]["note"] == "一键排全校"


def test_history_keeps_both_landed_and_revoked(store):
    """落两次、撤一次：历史里两条都在，一条失效一条有效。"""
    db = _db()
    for _ in range(2):
        b = plan_landing.snapshot(db)
        plan_landing.apply_plan(db, PLAN)
        store.save(PLAN, b, actor="admin")
    plan_landing.undo(db, store, actor="admin")

    rows = store.history()
    assert len(rows) == 2
    assert [r["revoked"] for r in rows] == [True, False], rows  # 新的在前


# ==================== 二、状态与重放必须跟着"失效"走 ====================

def test_status_says_not_landed_after_undo(store):
    """撤销后 latest() 必须为空 —— 否则界面说"已落地"而课表已经恢复。"""
    db = _db()
    b = plan_landing.snapshot(db)
    plan_landing.apply_plan(db, PLAN)
    store.save(PLAN, b, actor="admin")
    assert store.latest() is not None

    plan_landing.undo(db, store, actor="admin")

    assert store.latest() is None, "撤销后仍认为已落地"


def test_replay_never_resurrects_a_revoked_plan(store):
    """**最危险的一条**：重启重放不得把已撤销的方案捡回来。

    `all()` 是重放的输入。撤销改成打标记之后，若它不过滤 revoked，
    教秘撤销过的方案会在下一次重启后自己回到课表上 —— 没人点过任何按钮。
    """
    db = _db()
    b = plan_landing.snapshot(db)
    plan_landing.apply_plan(db, PLAN)
    store.save(PLAN, b, actor="admin")
    plan_landing.undo(db, store, actor="admin")

    assert store.all() == [], f"重放会把已撤销的方案再应用一遍：{store.all()}"


def test_all_still_returns_active_landings(store):
    """正向控制：没撤销的那条必须仍在 all() 里，否则重放会丢状态。"""
    db = _db()
    b = plan_landing.snapshot(db)
    plan_landing.apply_plan(db, PLAN)
    store.save(PLAN, b, actor="admin")

    rows = store.all()
    assert len(rows) == 1
    assert len(rows[0]["plan"]) == len(PLAN)


# ==================== 三、老库迁移 ====================

def test_old_six_column_table_is_migrated_in_place(tmp_path):
    """老库（只有 note，没有 actor/revoked_at）必须能自动补列并保留数据。

    `CREATE TABLE IF NOT EXISTS` 不会给已存在的表加列。若不迁移，报错会发生在
    **教秘刚点完"采纳"**的那一刻（INSERT 带新列 → no such column）。
    """
    path = str(tmp_path / "old.db")
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE landed_plans (
        id INTEGER PRIMARY KEY AUTOINCREMENT, landed_at TEXT NOT NULL,
        plan_json TEXT NOT NULL, before_json TEXT NOT NULL,
        after_entries INTEGER DEFAULT 0, note TEXT DEFAULT '')""")
    conn.execute("INSERT INTO landed_plans (landed_at, plan_json, before_json, note)"
                 " VALUES ('2026-10-01T10:00:00.000000', ?, ?, '旧记录')",
                 (json.dumps(PLAN, ensure_ascii=False),
                  json.dumps({"entries": [], "pending": []}, ensure_ascii=False)))
    conn.commit()
    conn.close()

    store = LandingStore(path)
    store.init_schema()          # 不抛 = 迁移成功

    rows = store.history()
    assert len(rows) == 1, "迁移把旧记录弄丢了"
    assert rows[0]["note"] == "旧记录"
    assert rows[0]["actor"] == "", "老记录没有操作者，应是空串而不是报错"
    assert rows[0]["revoked"] is False

    # 新列可用：补一条也能写进去
    store.save(PLAN, {"entries": [], "pending": []}, note="新", actor="root")
    assert len(store.history()) == 2


# ==================== 四、撤销到底撤哪一条 ====================

def test_undo_goes_back_through_the_stack(store):
    """连点两次撤销 = 依次弹掉最近两条（打标记，不删）。"""
    db = _db()
    for _ in range(2):
        b = plan_landing.snapshot(db)
        plan_landing.apply_plan(db, PLAN)
        store.save(PLAN, b, actor="admin")

    plan_landing.undo(db, store, actor="admin")
    plan_landing.undo(db, store, actor="admin")

    rows = store.history()
    assert len(rows) == 2
    assert all(r["revoked"] for r in rows), rows

    out = plan_landing.undo(db, store, actor="admin")
    assert out["success"] is False
    assert "没有可撤销" in out["error"]
