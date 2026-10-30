"""空表时「这周」被当成「没说要哪周」（R51）。

缺陷链
------
    1. `db.get_week_range()` 空表时返回 `(0, 0)`
    2. 元组 `(0, 0)` 是**真值** ⇒ `week_range[0] if week_range else 1` 取到 0
    3. `current_week = 0`
    4. 「这周」分支：`target_week = base`（base=0）
    5. `process_request` 里 `if not parsed["target_week"]` —— **0 是假值**
       ⇒ 认为"用户没说周次"，触发追问

实测：

    输入「这周先调一下」（数据未加载）
    返回 need_info=True
    追问「请问要把课调到第几周？（例如：第5周、下周、下下周）」

**用户明明说了"这周"，系统却问他"要调到第几周"。**

这个缺陷很隐蔽，因为：
- 最终行为"追问"看起来是对的（数据没加载，确实不该出结果）
- 但**追问的原因错了** —— 它以为用户没说周次

更麻烦的是，「下周」在这条路径上算出来是 **1**（`0 + 1`），
也是假值之外的一个"看起来合理"的数 —— 教秘会得到"第1周"的沙盘，
而那是一个**凭空猜出来的周次**（数据都没有，哪来的"第1周"）。

修法：`current_week` 必须有明确语义 —— 拿不到就返回 None，
让"这周/下周"这类**相对表达**在无基准时不成立，直接要求明确周次。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.agent.reschedule_agent import RescheduleAgent
from app.utils.excel_parser import ScheduleDatabase, ScheduleEntry


@pytest.fixture()
def empty_agent():
    db = ScheduleDatabase()
    db.entries = []
    db.loaded = False
    return RescheduleAgent(db)


@pytest.fixture()
def loaded_agent():
    db = ScheduleDatabase()
    db.entries = [
        ScheduleEntry(1, "周一", "567", "甲班", "1组", "项目A", "A101", "吴琳"),
        ScheduleEntry(8, "周二", "567", "甲班", "2组", "项目A", "A101", "吴琳"),
    ]
    db.loaded = True
    return RescheduleAgent(db)


def test_empty_table_does_not_yield_week_zero(empty_agent):
    """空表时不能把「这周」算成第 0 周（0 会被当成"没说"）。"""
    p = empty_agent.parse_request("这周先调一下")
    assert p.get("target_week") != 0, (
        "空表时「这周」解析出 0 周 —— 0 是假值，下游会当成"
        "「用户没说周次」而追问，可用户明明说了"
    )


def test_empty_table_does_not_guess_week_one(empty_agent):
    """空表时「下周」也不该凭空算成第 1 周（那是一个猜出来的周次）。"""
    p = empty_agent.parse_request("下周调一下")
    assert p.get("target_week") in (None,), (
        f"空表时「下周」算出第 {p.get('target_week')} 周 —— "
        "数据都没有，这个周次是凭空猜的"
    )


def test_loaded_table_still_relative(loaded_agent):
    """有数据时「这周/下周」照常按当前周推导（1-8 周，当前周=1）。"""
    assert loaded_agent.parse_request("这周调一下").get("target_week") == 1
    assert loaded_agent.parse_request("下周调一下").get("target_week") == 2


def test_explicit_week_works_without_data(empty_agent):
    """明确说了「第5周」，即使数据未加载也该解析出来（规则不依赖数据集）。"""
    assert empty_agent.parse_request("调到第5周").get("target_week") == 5


def test_empty_table_asks_for_explicit_week_not_silent(empty_agent):
    """空表 + 「这周」→ 追问时必须说"请给出具体周次"，不能假装用户没说。"""
    r = empty_agent.process_request("吴琳老师这周先调一下", False)
    msg = r.get("message") or ""
    assert r.get("need_info") is True
    # 追问列表里应出现"具体周次"一类措辞（而不是含糊的"要调到第几周"）
    assert any(k in msg for k in ("具体", "第几周", "周次")), (
        f"追问没有提到周次：{msg[:120]}"
    )
