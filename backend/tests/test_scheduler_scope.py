"""自动排课的 `note` 必须说明它只在工作日排（R37）。

背景
----
两个排课引擎（`auto_scheduler` / `parallel_scheduler`）都只建模
周一到周五，这是**刻意设计**：

    # parallel_scheduler.py
    WEEKDAYS = ["周一", ..., "周五"]
    # 真实表 433 条里 71 条（16%）在周六/周日 —— 一所学校"金工 5、6 周排周六"
    # 是常态。建模用 WEEKDAYS，但"这班平时哪天上课"必须把周末算进去

周末的已排课**确实会被记为占用**（`_busy` 按 `e.weekday` 记），
所以不会撞车 —— 实测：排定 30 条全部落在工作日，与已有周末课撞教室 0 次。

问题在**说明**：`note` 写的是

    教室/教师/班级三重互斥 + 与现有安排表逐周对齐；容量未校验

读这句话的人会合理地理解为"这周的空档都考虑过了"。实际上周末从未作为
候选进入模型 —— 它不是"排不下"，是"没参与"。

这与 R36 同族：**显示的是真实结果的一份裁剪，但不说明裁掉了什么。**
区别是 R36 裁掉了界面上的两天，这里裁掉的是搜索空间的两天。
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


@pytest.fixture(scope="module")
def solved(db):
    from app.engine.auto_scheduler import AutoScheduler
    return AutoScheduler(db).solve()


def test_auto_schedule_note_declares_weekday_only(solved):
    """结论里必须说清"只排工作日"，否则读者会以为周末也参与过。"""
    note = solved.get("note") or ""
    assert ("工作日" in note) or ("周末" in note), (
        "自动排课的 note 没说明它只在工作日排 —— "
        "读的人会以为这周的空档都参与过求解。实际 note: " + note
    )


def test_auto_schedule_does_not_emit_weekend(solved):
    """设计上不排周末：这条**钉住现行为**，改动时必须同步改 note。"""
    days = {s["weekday"] for s in solved.get("schedule") or []}
    assert not (days & {"周六", "周日"}), (
        f"排出了周末课（{days}）—— 若这是有意改动，请同时更新 note 与本文档"
    )


def test_weekend_existing_entries_still_block_rooms(solved, db):
    """周末已有课必须仍然占住教室（不排 ≠ 忽略）。"""
    weekend = {(e.week, e.weekday, e.time_slot, e.room)
               for e in db.entries if e.weekday in ("周六", "周日")}
    if not weekend:
        pytest.skip("真实表无周末课")
    clash = [s for s in solved.get("schedule") or []
             if (s["week"], s["weekday"], s["time_slot"], s["room"]) in weekend]
    assert not clash, f"排出的课与已有周末课撞教室：{clash[:3]}"

def test_summary_message_states_scope(solved, db):
    """前端只读 `message`（不读 data.note），所以范围说明必须进 summary。"""
    from app.engine.auto_scheduler import AutoScheduler

    msg = AutoScheduler(db).summary(solved)
    assert ("周一至周五" in msg) or ("工作日" in msg) or ("周末" in msg), (
        "自动排课的摘要没提排课范围 —— 前端把 message 直接显示给教秘，"
        "他看到的是一份「第N周：X 个教学任务」的清单，会以为这周就这些课"
    )
