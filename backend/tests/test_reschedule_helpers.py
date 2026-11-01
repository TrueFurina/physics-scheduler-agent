"""云端工单文案的反解契约（R-103）。

为什么值得单独锁
----------------
`_parse_original_time` 是**兜底**：云端工单表只存了给人读的一句文案
（"周六 上午 · A412-2"），没有结构化字段，审批要落地就只能反解。它身上有
**三个真实事故**（源码注释里写着），但覆盖率实测只有"空字符串"那条路被执行过 ——
也就是说三个坑都修好了，却**没有任何测试保证它们不被改回去**。

三个坑（每条都对应下面一个用例）：
  ① 星期正则漏掉周末 → "周六 上午" 的星期解析为空 → 星期不参与匹配 →
     第 6 周周六/周日各有一条同名课，一张工单把两天的课一起搬走（实测 applied=2）。
  ② 兜底数字正则从**教室号**里抠节次 → `A412-2` 解析出 "412" → 一个字都匹配不到 →
     审批"通过"了、课表一个字节没变。
  ③ 跨节次写法 `14:00-17:00` 只认主标签 567（判匹配时另有 blocked 逻辑配合）。
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.api.reschedule import _label_contains, _parse_original_time


# ---------------------------------------------------------------- 正常反解

def test_parses_weekday_slot_and_room():
    got = _parse_original_time("周四 13:30-15:45 · A504")
    assert got == {"weekday": "周四", "time_slot": "567", "room": "A504"}


def test_parses_explicit_slot_code():
    got = _parse_original_time("周三 678 · A411")
    assert got["time_slot"] == "678"
    assert got["room"] == "A411"


def test_tilde_separator_is_normalized():
    got = _parse_original_time("周二 15:45~18:00 · A410")
    assert got["time_slot"] == "678", "~ 与 - 必须等价"


def test_room_optional():
    got = _parse_original_time("周四 13:30-15:45")
    assert got["weekday"] == "周四" and got["time_slot"] == "567" and got["room"] == ""


# ---------------------------------------------------------------- 三个历史坑

def test_weekend_is_parsed_not_dropped():
    """坑①：周末必须能解析出来，否则"周六那节课"会匹配到周日的同名课。"""
    got = _parse_original_time("周六 上午 · A412-2")
    assert got["weekday"] == "周六", "星期解析为空 ⇒ 星期不参与匹配 ⇒ 会误搬别的天的课"
    assert got["time_slot"] == "MORNING"


def test_sunday_alias_tian_maps_to_ri():
    """文案里写「周天」的也要认成「周日」（真实表两种写法都有）。"""
    got = _parse_original_time("周天 18:30-20:45 · A410")
    assert got["weekday"] == "周日"
    assert got["time_slot"] == "91011"


def test_room_number_is_never_read_as_slot():
    """坑②：`A412-2` / `A567` 里的数字不许被当成节次。

    这是"审批通过但课表没动"的直接成因：时段解析成 "412" 后一个格都匹配不到。
    两种写法都要覆盖：带 `·` 分隔（教室被先切走）与**不带分隔**（教室还在 head 里，
    这时只有正则的 `(?<![\\dA-Za-z])` 边界断言能挡住它）。
    """
    for text in ("周六 上午 · A412-2", "周一 上午 · A567", "周一 上午 A567", "周六 上午 A412-2"):
        got = _parse_original_time(text)
        assert got["time_slot"] == "MORNING", f"{text} 的教室号被当成节次了：{got}"


def test_cross_slot_range_maps_to_primary_label():
    """坑③：14:00-17:00 跨了 567/678 两格，主标签取 567（占用判定另有 blocked 逻辑）。"""
    got = _parse_original_time("周五 14:00-17:00 · A410")
    assert got["time_slot"] == "567"


# ---------------------------------------------------------------- 保守原则

def test_unrecognizable_text_yields_empty_not_a_guess():
    """认不出就留空 —— 调用方据此拒绝，绝不猜一个"看起来像"的时段。"""
    for text in ("", "待定", "时间见群通知", "第5周"):
        got = _parse_original_time(text)
        assert got["time_slot"] == "", f"{text!r} 不该反解出时段"
        assert got["weekday"] == "" or text == "第5周"


# ---------------------------------------------------------------- 退让档匹配

def test_label_contains_requires_both_sides():
    assert _label_contains("", "扭摆法") is False
    assert _label_contains("扭摆法", "") is False


def test_label_contains_accepts_abbreviation_either_direction():
    """教秘常只写简称（「扭摆法」），也偶尔写全称去配简称 —— 两个方向都算包含。"""
    assert _label_contains("扭摆法", "扭摆法测物体转动惯量") is True
    assert _label_contains("扭摆法测物体转动惯量", "扭摆法") is True
    assert _label_contains("声速测量", "光的衍射") is False
