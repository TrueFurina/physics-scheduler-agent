"""`schedule_grid` 的网格口径边缘分支（R-110）。

`test_schedule_grid.py` 锁的是主路径（7 天全列、跨节次多格、无场地入格、冲突标记、
周次越界报错）。本文件补的是几条"边角但会误导教秘"的分支：

  ▸ 记录没有 `blocked`（61-63）→ 回退用主标签 `time_slot`，而不是当成"没占任何格"；
  ▸ `_match_group` 的 place 不是「班级/组」形式（83）→ 判 False，不误标；
  ▸ `conflict_marks` 遇到未知冲突类（106）→ 跳过，不 KeyError；
  ▸ `conflict_marks` 遇到**别的周**的冲突（110）→ 不标到本周，免得张冠李戴；
  ▸ `build_grid` 遇到连主标签都不在 SLOTS 的记录（164）→ 防御性跳过，`totals.entries` 不计。
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.schedule_grid import SLOTS, _entry_slots, _match_group, build_grid, conflict_marks
from app.utils.excel_parser import ScheduleEntry


def _entry(week=3, weekday="周一", slot="567", cls="甲班", group="1组",
           experiment="项目A", room="A410", teacher="吴老师", blocked=None):
    return ScheduleEntry(week, weekday, slot, cls, group, experiment, room, teacher,
                         blocked=blocked)


# ---------------------------------------------------------------------------
# 1. _entry_slots：没有 blocked 时回退主标签（63）
# ---------------------------------------------------------------------------

def test_entry_slots_falls_back_to_primary_slot_when_no_blocked():
    e = _entry(slot=SLOTS[1])
    e.blocked = []                      # 模拟"解析时没拿到 blocked"的记录
    assert _entry_slots(e) == [SLOTS[1]]


# ---------------------------------------------------------------------------
# 2. _match_group：place 不含「/」→ False（83）
# ---------------------------------------------------------------------------

def test_match_group_without_slash_is_false():
    e = _entry(cls="甲班", group="1组")
    assert _match_group(e, "甲班") is False       # 缺组，不是「班级/组」形式
    assert _match_group(e, "甲班/1组") is True    # 正常形式仍匹配


# ---------------------------------------------------------------------------
# 3. conflict_marks：未知冲突类跳过（106）/ 别的周的冲突不标（110）
# ---------------------------------------------------------------------------

def test_conflict_marks_skips_unknown_kind():
    e = _entry(week=3)
    # 键不是 room/teacher/group 三类之一 → kind 不在 matchers，跳过，不崩
    marks = conflict_marks([e], {"foo_conflicts": [
        {"week": 3, "weekday": "周一", "time_slot": "567", "place": "A410"}]}, week=3)
    assert marks == {}


def test_conflict_marks_ignores_conflicts_from_other_week():
    e = _entry(week=3, room="A410")
    # 冲突发生在第 5 周；查询第 3 周时不能把它标到本周记录上
    marks = conflict_marks([e], {"room_conflicts": [
        {"week": 5, "weekday": "周一", "time_slot": "567", "place": "A410"}]}, week=3)
    assert marks == {}


# ---------------------------------------------------------------------------
# 4. build_grid：连主标签都不在 SLOTS 的记录防御性跳过（164）
# ---------------------------------------------------------------------------

def test_build_grid_skips_entry_without_any_valid_slot():
    e = _entry(week=3, slot="??")        # 主标签不在 SLOTS
    e.blocked = []                        # 也没有 blocked
    grid = build_grid([e], week=3)
    assert grid["totals"]["entries"] == 0
    assert grid["cells"] == {}
