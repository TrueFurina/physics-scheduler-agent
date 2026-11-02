"""`AutoScheduler` 的"优雅失败 + 空表兜底"契约（R-108）。

为什么单独一个文件：`test_auto_schedule.py` 锁的是**排得对不对**（周维度、避让、
容量、绪论排除、超时兜底），但"排不出来时怎么说"那几条分支一直没测 —— 而它们正是
本项目反复踩的"自述与事实不符"（R-37/R-76 同族）：空表/无教室时若不给明确原因，
界面就只会显示一个笼统的成功或崩溃。

  ▸ 没有任何可排任务（268）→ `{"success": False, "error": "没有可排课的教学任务"}`，
    而不是抛异常或返回空成功；
  ▸ 有任务但没有可用教室（272）→ `{"success": False, "error": "没有可用教室"}`；
  ▸ 表里没有任何有效周次（175）→ 回退到第 1 周，而不是空列表（R-32：`get_week_range()`
    对空表返回 `(0,0)`，元组是真值，会被当成"有效范围"继续用）；
  ▸ 待排任务里的绪论/考试（165）→ 跳过，不混进求解（它们已单独安排）。
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.engine.auto_scheduler import AutoScheduler
from app.utils.excel_parser import PendingTask, ScheduleDatabase, ScheduleEntry


def _db(entries=None, skipped=None):
    d = ScheduleDatabase()
    d.entries = list(entries or [])
    d.skipped = list(skipped or [])
    d.loaded = True
    return d


def _entry(week, weekday, slot, cls, room, teacher,
           experiment="扭摆法测物体转动惯量", group="1组"):
    return ScheduleEntry(week, weekday, slot, cls, group, experiment, room, teacher)


# ---------------------------------------------------------------------------
# 1. 没有任何可排任务 → 明确失败，不是空成功（268）
# ---------------------------------------------------------------------------

def test_solve_without_sections_fails_clearly():
    res = AutoScheduler(_db([])).solve(max_sections=5)
    assert res["success"] is False, res
    assert res["error"] == "没有可排课的教学任务", res


# ---------------------------------------------------------------------------
# 2. 有任务但没有可用教室 → 明确失败（272）
# ---------------------------------------------------------------------------

def test_solve_without_rooms_fails_clearly():
    # 没有已排记录 ⇒ 没有教室；但给一条待排任务 ⇒ 有 section、无 room
    task = PendingTask("甲班", "扭摆法测物体转动惯量", "1组", teacher="吴老师")
    res = AutoScheduler(_db([]), extra_tasks=[task]).solve(max_sections=5)
    assert res["success"] is False, res
    assert res["error"] == "没有可用教室", res


# ---------------------------------------------------------------------------
# 3. 表里没有任何有效周次 → 回退第 1 周（175，R-32）
# ---------------------------------------------------------------------------

def test_weeks_fall_back_to_one_when_no_valid_week():
    # week=0 不算有效周次（真实表里被丢弃的行可能带 0），不能让空范围毒化周次
    db = _db([_entry(0, "周一", "567", "甲班", "A410", "吴老师")])
    assert AutoScheduler(db)._get_weeks() == [1]


def test_weeks_empty_table_falls_back_to_one():
    assert AutoScheduler(_db([]))._get_weeks() == [1]


# ---------------------------------------------------------------------------
# 4. 待排任务里的绪论/考试 → 跳过（165）
# ---------------------------------------------------------------------------

def test_extra_intro_and_exam_tasks_are_skipped():
    db = _db([_entry(2, "周一", "567", "甲班", "A410", "吴老师", experiment="扭摆法")])
    tasks = [
        PendingTask("甲班", "绪论课", "1组", teacher="吴老师"),
        PendingTask("甲班", "项目A考试", "1组", teacher="吴老师"),
        PendingTask("甲班", "扭摆法", "2组", teacher="吴老师"),
    ]
    sections, _ = AutoScheduler(db, extra_tasks=tasks)._get_course_sections()
    courses = {s["course"] for s in sections}

    assert "绪论课" not in courses
    assert "项目A考试" not in courses
    # 可排任务仍要在（且 2 组并进同一段）
    assert "扭摆法" in courses
    sec = next(s for s in sections if s["course"] == "扭摆法")
    assert set(sec["groups"]) == {"1组", "2组"}
