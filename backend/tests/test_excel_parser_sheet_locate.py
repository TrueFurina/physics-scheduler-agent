"""`find_schedule_sheet` 的工作表容错定位（R-107）。

为什么单独一个文件：`find_schedule_sheet` 的 docstring 记着一件真实事故 ——
旧实现硬编码 `wb["大学物理实验安排表 (周) "]`（**还带一个尾随空格**），
换一所学校、或同一个文件另存时少个空格，就抛 KeyError → 400
「文件解析失败：'大学物理实验安排表 (周) '」。而产品文档承诺"上传 Excel 可替换
（支持其他学校）"——那句话在当时是伪承诺。修复方式就是这条回退链：

    精确候选（SHEET_CANDIDATES） → 名称含"安排表" → 名称含"周" → 第一张表 → (None, None)

`test_master_input.py` 只测了"精确候选"和"取第一张表"两端，**中间两级含关键字
的回退（真正让别的学校能用的那两级）从没被跑过** —— 一旦有人把它"简化"回
精确匹配，旧 bug 会静默复活。这里把四级回退的优先级和空表都钉死。

每个回退用例都放一张**诱饵首表**：单表工作簿里"回退失效"会落到"取第一张表"
返回同一张表、假绿；有诱饵才能让缺失回退真的返回错表、转红。
"""

import os
import sys

import openpyxl

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.utils.excel_parser import find_schedule_sheet


# ---------------------------------------------------------------------------
# 1. 四级回退链：精确候选(231) → "安排表"含(234) → "周"含(237) → 首表(239) → None(240)
# ---------------------------------------------------------------------------

def test_falls_back_to_contains_pai_ban_biao():
    """表名含「安排表」但不是精确候选（如某校「2025春实验安排表」）→ 必须命中它，
    而不是 KeyError/None，也不是"退而取第一张表"。"""
    wb = openpyxl.Workbook()
    wb.active.title = "说明"                    # 诱饵：第一张表
    wb.create_sheet("2025春实验安排表")          # 目标：含「安排表」但不在首位
    ws, name = find_schedule_sheet(wb)
    assert ws is not None
    assert name == "2025春实验安排表"


def test_falls_back_to_contains_zhou():
    """表名不含「安排表」但含「周」（如「第3周课表」）→ 仍要命中它，不取诱饵首表。"""
    wb = openpyxl.Workbook()
    wb.active.title = "说明"                    # 诱饵：第一张表
    wb.create_sheet("第3周课表")                # 目标：含「周」但不在首位
    ws, name = find_schedule_sheet(wb)
    assert ws is not None
    assert name == "第3周课表"


def test_empty_workbook_returns_none():
    """连一张表都没有 → 返回 (None, None)，由上层给出"文件里没有任何工作表"的明确报错，
    而不是在这里抛 IndexError。"""
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    assert wb.sheetnames == []
    ws, name = find_schedule_sheet(wb)
    assert ws is None and name is None


def test_candidate_wins_over_contains():
    """优先级：精确候选必须压过"含关键字"的回退（否则会挑错表）。"""
    wb = openpyxl.Workbook()
    wb.active.title = "随便的表"
    wb.create_sheet("大学物理实验安排表 (周) ")   # 精确候选
    wb.create_sheet("某校实验安排表")             # 含「安排表」，优先级更低
    _, name = find_schedule_sheet(wb)
    assert name == "大学物理实验安排表 (周) "
