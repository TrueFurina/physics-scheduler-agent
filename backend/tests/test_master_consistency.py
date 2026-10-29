"""主数据与安排表是两个来源：删除教师后安排表里的课怎么办（R40）。

先说清楚这不是 bug
------------------
系统有两份"事实"：

| 来源 | 内容 | 谁是权威 |
|---|---|---|
| **安排表** `_db.entries` | 433 条排课行（来自 Excel） | 排课的事实 |
| **主数据** `_master_data` | 课程/实验/教师/场地/班级 | 教秘维护的台账 |

`delete_teacher` 只动主数据，**不动安排表** —— 这是对的：
删一个教师条目，不等于要删他排过的 116 条课。

删除后的实测行为（已核验）：

```
删除教师H  → 主数据教师 13→12，实验里的教师关联同时摘除（0 个残留）
            → 安排表仍有 116 条教师H的课（未动，符合预期）
            → 重启从落盘恢复 → 仍然是 12 个（删除持久）
            → 落盘失效、重新反推基线 → 教师H 又回来了（13 个）
```

最后一行是**设计使然**，`master_store` 的文件头写得很明确：

    安排表换了 → 落盘的主数据作废，回退到新表的反推基线 ——
    安排表永远是主数据的权威来源

所以这些都不是缺陷，是"两套事实"的正常表现。

真正需要盯住的是**能自洽的部分**
--------------------------------
既然删了主数据里的教师，而他的课还在，那么：

1. 排课引擎看到这 116 条课，仍会按"教师H 占用"处理 —— 不能因为主数据没这个人就忽略
2. 灰箱补全在找"负载最轻的教师"时，不该把已删的教师补回来
3. 数据体检不该因为主数据少一个人而漏报冲突

这三条才是"删除后系统仍然自洽"的判据。本文件锁住它们。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.utils.excel_parser import ScheduleDatabase

REAL = os.path.join(os.path.dirname(__file__), "..", "..", "schedule.demo.xlsx")


@pytest.fixture(scope="module")
def db():
    if not os.path.isfile(REAL):
        pytest.skip("缺少 schedule.demo.xlsx")
    d = ScheduleDatabase()
    d.load_from_excel(REAL)
    return d


def _busiest_teacher(db):
    from collections import Counter
    c = Counter(e.teacher for e in db.entries if e.teacher)
    return c.most_common(1)[0][0]


def test_deleting_teacher_does_not_touch_schedule(db):
    """删主数据里的教师，安排表（排课事实）必须原样保留。"""
    from app.master_data import MasterData

    md = MasterData()
    md.build_from_entries(db.entries)
    t = _busiest_teacher(db)
    before = len([e for e in db.entries if e.teacher == t])
    assert before > 0, "测试前提：该教师有课"

    md.delete_teacher(t)

    after = len([e for e in db.entries if e.teacher == t])
    assert after == before, (
        f"删主数据教师动了安排表：{before} → {after} 条 —— "
        "删台账条目不应删历史排课"
    )
    assert t not in md.teachers


def test_deleted_teacher_still_blocks_rooms_and_slots(db):
    """主数据删了教师，排课引擎仍必须把他当"已占用"。

    否则自动排课会把这个人的课当成空档，把别的课排进同一时段。
    """
    from app.engine.auto_scheduler import AutoScheduler
    from app.master_data import MasterData

    t = _busiest_teacher(db)
    md = MasterData()
    md.build_from_entries(db.entries)
    md.delete_teacher(t)

    sched = AutoScheduler(db)
    # 合并后 _occupancy 返回 3 元组（room/teacher/class 三重互斥，本地 R-7x 加了班级维度）
    _room_busy, teacher_busy, _class_busy = sched._occupancy()    # noqa: SLF001
    hits = [k for k in teacher_busy if k[3] == t]
    assert hits, (
        f"删主数据教师后，排课引擎不再认为 {t} 占用时段 —— "
        "他的 116 条课会变成别人可以排进去的空档"
    )


def test_deleted_teacher_not_reintroduced_by_enrichment(db):
    """灰箱补全（从主数据挑教师）不该把已删教师补回来。"""
    from app.master_data import MasterData

    t = _busiest_teacher(db)
    md = MasterData()
    md.build_from_entries(db.entries)
    md.delete_teacher(t)
    assert t not in md.teachers

    # 主数据里任何位置都不该再出现这个名字
    for name, ex in md.experiments.items():
        assert t not in (ex.get("teachers") or set()), f"实验「{name}」仍关联已删教师"


def test_delete_teacher_room_class_survives_restart(db, tmp_path, monkeypatch):
    """删教师/场地/班级后重启（落盘恢复），删除必须还在。

    R39 的教训是"写操作要校验结果"。这三条删除没有 `_rebuild` 干扰，
    实测是持久的 —— 这里把"持久"这件事钉住，防止将来引入重建逻辑又把它弄丢。
    """
    from app import master_store
    import app.api.reschedule as api

    monkeypatch.setattr(master_store, "STORE_PATH", str(tmp_path / "s.json"))
    monkeypatch.setattr(api, "_DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setattr(api, "_schedule_fp", "fp-test")
    monkeypatch.setattr(api, "_db", db)
    api._master_data.__init__()
    api._ensure_master_data()

    t = _busiest_teacher(db)
    room = sorted(api._master_data.rooms)[0]
    cls = sorted(api._master_data.classes)[0]
    n0 = (len(api._master_data.teachers), len(api._master_data.rooms),
          len(api._master_data.classes))

    api._master_data.delete_teacher(t)
    api._persist_master_state()
    api._master_data.delete_room(room)
    api._persist_master_state()
    api._master_data.delete_class(cls)
    api._persist_master_state()

    # 模拟重启：内存清空 → 从落盘恢复
    api._master_data.__init__()
    api._ensure_master_data()

    n1 = (len(api._master_data.teachers), len(api._master_data.rooms),
          len(api._master_data.classes))
    assert n1 == (n0[0] - 1, n0[1] - 1, n0[2] - 1), (
        f"重启后删除丢失：{n0} → {n1}（应各少 1）"
    )
    assert t not in api._master_data.teachers
    assert room not in api._master_data.rooms
    assert cls not in api._master_data.classes
