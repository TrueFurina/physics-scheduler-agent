"""`master_workbook` 对「占位值 / 脏数值 / 空名行」的处理（R-109）。

为什么单独一个文件：`test_master_workbook.py` 锁的是往返无损、编辑生效、统计列不成实体、
认不出就报（R-34）—— 都是"正常数据"的契约。但教秘手里的台账常有**脏格子**：

  ▸ 多值列里填「—」「-」「无」表示"没有"（137）：不能把它们切成实体名「—」；
  ▸ 容量/周次填了「六十」「abc」这类非数字（149-150）：`_to_int` 要返回 None 而不是崩；
  ▸ 整行**没有名字但有别的格子**（180 / 374-376）：既不能当实体导入，也不能静默丢弃 ——
    要计入 `blank_rows` 让教秘看见"这行被跳过了"。

这些正是 R-34/R-33 同族：**预览给教秘看的行 = 导入真正写库的行**，任何一处静默吞掉
或伪造实体，都会让"预览说 N 行、导入进 M 行"无人能发现。
"""

import os
import sys

import openpyxl
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.master_workbook import _row_to_rec, _split_multi, _to_int, parse_workbook


# ---------------------------------------------------------------------------
# 1. _split_multi：占位值「—」「-」「无」→ 空列表，绝不当实体名（137）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("placeholder", ["—", "-", "无", " — ", ""])
def test_split_multi_treats_placeholders_as_empty(placeholder):
    assert _split_multi(placeholder) == []


def test_split_multi_still_splits_real_values():
    """占位值处理不能误伤正常多值（回归保护）。"""
    assert _split_multi("吴琳、林晨") == ["吴琳", "林晨"]


# ---------------------------------------------------------------------------
# 2. _to_int：非数字/空 → None，不抛异常（149-150）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value", ["abc", "六十", "", None])
def test_to_int_non_numeric_is_none(value):
    assert _to_int(value) is None


def test_to_int_parses_float_strings_and_ints():
    assert _to_int("60") == 60
    assert _to_int("60.9") == 60      # Excel 数值常是 60.0
    assert _to_int(45) == 45


# ---------------------------------------------------------------------------
# 3. _row_to_rec：名称为空 → (None, 0)，不伪造空名实体（180）
# ---------------------------------------------------------------------------

_SPEC = {"fields": {"name": ("名称",), "capacity": ("容量",)}, "multi": set()}
_MAPPING = {"name": 0, "capacity": 1}


def test_row_to_rec_empty_name_returns_none():
    rec, bad = _row_to_rec(("", "60"), _MAPPING, _SPEC)
    assert rec is None
    assert bad == 0


# ---------------------------------------------------------------------------
# 4. _row_to_rec：容量填了非数字 → 计入 bad，rec 仍产出（192）
# ---------------------------------------------------------------------------

def test_row_to_rec_counts_bad_numeric_cell():
    rec, bad = _row_to_rec(("A410", "六十"), _MAPPING, _SPEC)
    assert rec is not None and rec["name"] == "A410"
    assert rec["capacity"] is None      # 非数字不硬塞
    assert bad == 1                     # 但要报出来（预览能看见"这格填错了"）


# ---------------------------------------------------------------------------
# 5. parse_workbook：空名但有数据的行 → 计入 blank_rows，不静默丢（374-376）
# ---------------------------------------------------------------------------

def test_parse_workbook_counts_blank_name_rows_with_data(tmp_path):
    p = tmp_path / "dirty.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "实验项目"
    ws.append(["实验项目", "课程", "教师", "场地", "班级"])   # 表头
    ws.append(["扭摆法", "大学物理实验", "吴琳", "A410", "甲班"])  # 正常行
    ws.append(["", "大学物理实验", "林晨", "A412", "乙班"])       # 空名但有数据 → 计入 blank
    wb.save(p)

    tables, report = parse_workbook(str(p))
    exp = report["tables"]["experiments"]
    assert exp["rows"] == 1              # 只有正常那行进表
    assert exp["blank_rows"] == 1        # 空名有数据行被"看见"并计数，不是静默丢弃
    assert [r["name"] for r in tables["experiments"]] == ["扭摆法"]
