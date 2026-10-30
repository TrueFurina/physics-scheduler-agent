"""教师端「我的课程」：课程归属与周次区间必须说实话（R50）

为什么单独一轮查这张卡片
------------------------
这张卡片是**唯一**让教师看到"我教了什么"的入口，也是调课申请的起点：
卡片 → `POST /requests` → 审批 → `POST /apply-move`。它一错，后面全是错的。

现状（本轮实测，修复前）
------------------------
1. **课程维度整条链路都没进这张卡片。** R-66 起 `ScheduleEntry` 已经有显式
   `course` 外键，产品决策 B 把真实表 433 条逐行归到三门课（大学物理实验B/C、
   普通物理实验3），`course_bridge.resolve_course` 也早就会算 —— 但
   `get_teacher_courses` 一个字都没用。分组键只有
   `(实验项目, 班级)`，所以同一个项目跨两门课时**塌缩成一张卡片**，
   教师永远看不到自己上的是哪门课。`course_bridge` 的模块注释已经点明这个
   塌缩，只是没接到这条路径上。

2. **`week_range` 会凭空造出一段连续的周次。** 实测 `[3, 5, 9, 11]` →
   `"3-11周"`。物理实验单双周轮换是常态，教师看到的"3-11周"要么被读成
   "每周都有课"（多 5 次），要么被读成"从第 3 周到第 11 周共 9 周"（多 5 次）。
   这是最典型的"输出错了但无声"—— 没有报错，没有空值，只是一个看起来
   很正常的数字。

判据
----
- 归属要**说全**：一门课给准确名字，多门课给列表并显式标记 `course_ambiguous`，
  绝不用第一门课冒充"这门课的课程"。
- 周次要**说准**：连续才写 `a-b周`，不连续就逐周列出，单周就写 `n周`。
  机器要用的 `weeks` 字段始终是真实周次列表。

反向保护（别把正常路径改坏）
----------------------------
- 卡片的 `course_name` 仍是**实验项目名**，不是课程名 —— `apply-move` 的
  匹配口径是 `_label_matches(course_name, e.experiment)`，改成课程名会让
  所有历史申请单匹配不上。用例 8 锁死这一点。
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.api import reschedule as R
from app.master_data import DEFAULT_COURSE
from app.utils.excel_parser import ScheduleDatabase, ScheduleEntry

COURSE_B = "大学物理实验B"
COURSE_3 = "普通物理实验3"


def _e(week, weekday, slot, cls, group, exp, room="A504", teacher="吴琳", course=""):
    return ScheduleEntry(week, weekday, slot, cls, group, exp, room, teacher, course=course)


def _db(entries):
    d = ScheduleDatabase()
    d.entries = entries
    d.loaded = True
    return d


def _one(d, teacher="吴琳"):
    cards = d.get_teacher_courses(teacher)
    assert len(cards) == 1, cards
    return cards[0]


# ==================== 一、课程归属不许塌缩 ====================

def test_same_project_in_two_courses_is_not_silently_collapsed():
    """同一老师、同一班、同一实验名，分属两门课 → 卡片必须说出"两门都有"。

    旧行为：塌缩成一张卡片，course 字段不存在 ⇒ 教师以为自己只教了一门课。
    """
    d = _db([
        _e(3, "周一", "345", "24物理1班", "1组", "扭摆法测物体转动惯量", "A504", course=COURSE_B),
        _e(5, "周二", "567", "24物理1班", "1组", "扭摆法测物体转动惯量", "A516", course=COURSE_3),
    ])
    card = _one(d)

    assert card["courses"] == [COURSE_B, COURSE_3], card
    assert card["course_ambiguous"] is True, "跨两门课却不标记歧义 = 让教师自己猜"
    assert COURSE_B in card["course"] and COURSE_3 in card["course"], card["course"]


def test_single_course_card_reports_exactly_one_course():
    """正向控制：只属一门课时给准确名字，不许含糊成列表或默认值。"""
    d = _db([
        _e(3, "周一", "345", "24物理1班", "1组", "声速测量", "A504", course=COURSE_3),
        _e(4, "周一", "345", "24物理1班", "1组", "声速测量", "A504", course=COURSE_3),
    ])
    card = _one(d)

    assert card["course"] == COURSE_3, card
    assert card["courses"] == [COURSE_3]
    assert card["course_ambiguous"] is False


def test_course_falls_back_to_master_mapping_when_entry_has_none():
    """源表没有课程列（entry.course 为空）→ 用主数据「项目→课程」映射。"""
    d = _db([_e(3, "周一", "345", "24物理1班", "1组", "扭摆法测物体转动惯量")])
    card = _one_with_courses(d, {"扭摆法测物体转动惯量": COURSE_B})

    assert card["course"] == COURSE_B, card


def test_entry_course_beats_master_mapping():
    """显式外键优先于反推：主数据说 B，条目自己写 3 ⇒ 用条目（逐行事实更权威）。"""
    d = _db([_e(3, "周一", "345", "24物理1班", "1组", "扭摆法测物体转动惯量", course=COURSE_3)])
    card = _one_with_courses(d, {"扭摆法测物体转动惯量": COURSE_B})

    assert card["course"] == COURSE_3, card


def test_nothing_known_falls_back_to_default_course_but_says_so():
    """主数据未初始化、条目也没有课程列 → 回落默认课程，但必须仍带 courses 字段。

    回落不是"猜对了"，而是"只知道默认课"；`course_coverage.master_ready`
    负责把"没查"说出来，这里只需保证卡片结构一致、不抛 KeyError。
    """
    d = _db([_e(3, "周一", "345", "24物理1班", "1组", "某个还没进主数据的项目")])
    card = _one(d)

    assert card["course"] == DEFAULT_COURSE
    assert card["courses"] == [DEFAULT_COURSE]
    assert card["course_ambiguous"] is False


def _one_with_courses(d, courses, teacher="吴琳"):
    cards = d.get_teacher_courses(teacher, courses)
    assert len(cards) == 1, cards
    return cards[0]


# ==================== 二、周次区间不许编 ====================

def test_non_contiguous_weeks_are_listed_not_faked_as_a_range():
    """[3, 5, 9, 11] 必须逐周列出 —— 旧行为写 "3-11周"，多报 5 次课。"""
    d = _db([_e(w, "周三", "345", "24物理2班", "2组", "声速测量") for w in (3, 5, 9, 11)])
    card = _one(d)

    assert card["weeks"] == [3, 5, 9, 11]
    assert card["week_range"] == "3、5、9、11周", card["week_range"]
    assert "-" not in card["week_range"], "不连续的周次不许写成区间"


def test_contiguous_weeks_keep_the_range_form():
    """正向控制：真连续时仍写 "3-5周"，不要为了修上一条把区间形式全废掉。"""
    d = _db([_e(w, "周三", "345", "24物理2班", "2组", "声速测量") for w in (3, 4, 5)])
    card = _one(d)

    assert card["week_range"] == "3-5周", card["week_range"]


def test_single_week_is_not_written_as_a_range():
    """只有一周时写 "7-7周" 是另一种难看的谎 —— 应写 "7周"。"""
    d = _db([_e(7, "周三", "345", "24物理2班", "2组", "声速测量")])
    card = _one(d)

    assert card["week_range"] == "7周", card["week_range"]


def test_two_non_adjacent_weeks_are_not_a_range():
    """[3, 11] 是"两次课"，不是"3 到 11 周"。边界：区间长度 2 也要判连续。"""
    d = _db([_e(w, "周三", "345", "24物理2班", "2组", "声速测量") for w in (3, 11)])
    card = _one(d)

    assert card["week_range"] == "3、11周", card["week_range"]


# ==================== 三、别把调课链路改坏 ====================

def test_card_course_name_is_still_the_experiment_name():
    """`course_name` 仍是**实验项目名**：apply-move 按它匹配 `entry.experiment`。

    新增的 `course` 是给人看的归属，不得挤掉 `course_name` 的既有语义 ——
    否则所有历史申请单（course_name 存的是实验名）全都匹配不上。
    """
    d = _db([_e(3, "周一", "345", "24物理1班", "1组", "扭摆法测物体转动惯量", course=COURSE_B)])
    card = _one(d)

    assert card["course_name"] == "扭摆法测物体转动惯量"
    assert card["course_name"] != card["course"]


def test_existing_card_fields_are_unchanged():
    """回归：原有 8 个字段一个不少、语义不变（前端与申请单都在用）。"""
    d = _db([_e(3, "周一", "345", "24物理1班", "1组", "声速测量", "A504")])
    card = _one(d)

    for key in ("course_name", "class_name", "original_time", "room",
                "week_range", "total_slots", "adjustable", "adjustable_reason"):
        assert key in card, f"卡片丢了字段 {key}"
    assert card["total_slots"] == 1
    assert card["original_time"] == "周一 345"


# ==================== 四、端点层：字段真的送出去了吗 ====================

def test_endpoint_returns_course_attribution(monkeypatch):
    """端到端：/teachers/{name}/courses 的响应里必须带课程归属。

    单测全绿 ≠ 接口能拿到 —— 端点若不把主数据快照传下去，课程会全部回落
    默认课，卡片看起来"正常"但归属是假的。
    """
    from fastapi.testclient import TestClient
    from app.main import app

    d = _db([
        _e(3, "周一", "345", "24物理1班", "1组", "扭摆法测物体转动惯量", course=COURSE_B),
    ])
    monkeypatch.setattr(R, "_db", d)

    # 主数据快照固定下来（端点内部会取锁内快照，这里给一份确定的映射）
    monkeypatch.setattr(R, "_master_courses_snapshot",
                        lambda: {"扭摆法测物体转动惯量": COURSE_B})

    client = TestClient(app)
    resp = client.get("/api/reschedule/teachers/吴琳/courses")
    assert resp.status_code == 200, resp.text

    body = resp.json()
    assert body["total"] == 1, body
    card = body["courses"][0]
    assert card["course"] == COURSE_B, card
    assert card["courses"] == [COURSE_B]
    assert card["weeks"] == [3]
    assert card["week_range"] == "3周"
