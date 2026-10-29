"""R7：time_model 边界行为锁定。

`time_model.py` 的注释里写了若干**承诺**，它们直接决定"推荐的时段会不会撞课"：

  1. 首尾相接不算重叠（567 结束 15:45 = 678 开始 15:45）
  2. 跨节次区间占住所有真实重叠的节次（14:00-17:00 → 567 + 678）
  3. 不认识的时间一律不猜（返回空，由调用方按数据缺口处理）
  4. 写反的区间不硬猜（17:00-14:00 → None）

这些此前零测试覆盖。R7 探针确认当前行为**全部正确**，这里把它锁住——
因为任何一条被改错，都表现为"推荐到了正在上课的教室"，而不会报错。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.utils.time_model import (
    CLOCK, clock_text, intervals_overlap, labels_overlap,
    parse_clock_range, primary_label, span_labels,
)


# ---------------------------------------------------------------- 1. 区间解析

@pytest.mark.parametrize("raw", ["14:00-17:00", "14：00-17：00", "14:00－17:00",
                                 "14:00~17:00", "14:00—17:00"])
def test_parse_accepts_separator_variants(raw):
    """Excel 里的分隔符不统一，全角/波浪号都要能认。"""
    assert parse_clock_range(raw) == (840, 1020)


@pytest.mark.parametrize("raw", ["17:00-14:00", "14:00-14:00", "", None,
                                 "567", "abc", "14:00", "14:00-"])
def test_parse_rejects_invalid(raw):
    """写反 / 起止相同 / 空 / 纯节次码 / 垃圾 —— 一律 None，不猜。"""
    assert parse_clock_range(raw) is None


def test_parse_single_digit_hour():
    assert parse_clock_range("9:00-10:30") == (540, 630)


# ---------------------------------------------------------------- 2. 重叠判定

def test_adjacent_slots_do_not_overlap():
    """核心承诺：567 结束于 15:45，678 开始于 15:45，首尾相接不算撞课。"""
    assert intervals_overlap(CLOCK["567"], CLOCK["678"]) is False
    assert labels_overlap("567", "678") is False
    assert labels_overlap("678", "91011") is False


def test_spanned_range_blocks_all_overlapping_slots():
    """14:00-17:00 同时压住 567 与 678 —— 两间教室在这两个节次都不空。"""
    assert span_labels("14:00-17:00") == ["567", "678"]


def test_unknown_label_is_treated_as_overlapping():
    """不认识的标签保守视为占用：宁可少推荐，不可推荐到正在上课的教室。"""
    assert labels_overlap("XYZ", "567") is True
    assert labels_overlap("", "567") is True
    assert labels_overlap("567", "567") is True


# ---------------------------------------------------------------- 3. 不猜

def test_out_of_schedule_range_returns_empty():
    """07:00-08:00 落在课表节次之外：不猜，交给调用方当数据缺口。"""
    assert span_labels("07:00-08:00") == []
    assert primary_label("07:00-08:00") == ""


def test_span_labels_falls_back_for_unknown_text():
    assert span_labels("abc", "567") == ["567"]
    assert span_labels("abc") == []
    assert span_labels(None) == []


# ---------------------------------------------------------------- 4. 主标签

def test_primary_label_uses_start_instant():
    """跨节次时主标签取**起始时刻**所在节次：14:00 在 567 内。"""
    assert primary_label("14:00-17:00") == "567"
    assert primary_label("15:50-18:05") == "678"


def test_primary_label_passthrough_fallback():
    assert primary_label("abc", "678") == "678"


# ---------------------------------------------------------------- 5. 展示

def test_clock_text_normalises_fullwidth_colon():
    assert clock_text("14：00-17：00") == "14:00-17:00"


def test_clock_text_expands_slot_code():
    assert clock_text("567") == "13:30-15:45"


# ---------------------------------------------------------------- 6. 解析链路

def test_parser_normalises_slot_codes_via_lookup_table():
    """节次码走 ② 查表分支（span_labels 返回空 → TIME_SLOT_NORMALIZE）。"""
    from app.utils.excel_parser import TIME_SLOT_NORMALIZE

    for code in ("567", "678", "91011"):
        assert span_labels(code) == [], f"{code} 应走查表分支"
        assert TIME_SLOT_NORMALIZE.get(code) == code


def test_parser_rejects_unrecognised_time_slots():
    """看不懂的时段必须返回空，而不是猜一个 —— 否则沙盘会误判教室空闲。"""
    from app.utils.excel_parser import TIME_SLOT_NORMALIZE

    for bad in ("abc", "07:00-08:00", ""):
        assert span_labels(bad) == []
        assert TIME_SLOT_NORMALIZE.get(bad, "") == ""
