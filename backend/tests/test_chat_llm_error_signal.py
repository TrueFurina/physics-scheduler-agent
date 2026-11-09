"""R94：聊天族 LLM 候选生成失败的**结构化信号**测试。

背景
----
`process_request` 在 `with_ai=True` 且 LLM 已配置时调用 `suggest_plans` 生成
AI 候选方案。此前两种失败都被静默吞掉（R94 修复前）：

  - `suggest_plans` 返回 ``{"error": ...}``  → 被 ``.get("plans", [])`` 直接丢弃；
  - `suggest_plans` 抛出异常               → 被 ``except Exception: ai_plans = []`` 吞掉。

降级到规则推荐时 `ai_plans_source` 仍报 ``"rule"``，调用方无从得知 LLM 失败过。

R94 要求：LLM **已配置且真正失败**时，结果必须带 ``llm_error=True`` 与
``llm_error_detail``。但以下两种"非失败"必须保持 ``False``/``None``，不能污染信号：

  - ``with_ai=False``（fast 模式）跳过 LLM；
  - LLM 未配置（离线常态）—— 那不是故障，是正常路径。
"""
import os
import sys
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.agent.reschedule_agent import RescheduleAgent
from app.utils.excel_parser import ScheduleDatabase

REAL = os.path.join(os.path.dirname(__file__), "..", "..", "schedule.demo.xlsx")


@pytest.fixture(scope="module")
def db():
    if not os.path.isfile(REAL):
        pytest.skip("缺少 schedule.demo.xlsx")
    d = ScheduleDatabase()
    d.load_from_excel(REAL)
    return d


def _configured_parser(suggest_return=None, raises=None):
    """构造一个 configured=True 的 llm_parser 替身。

    suggest_return / raises 二选一：前者让 suggest_plans 正常返回该 dict，
    后者让 suggest_plans 抛出指定异常。parse 也兜底返回空 dict，
    避免规则解析已拿到周次时仍被 MagicMock 的 .get 误用。
    """
    parser = MagicMock()
    parser.configured = True
    parser.parse.return_value = {}
    if raises is not None:
        parser.suggest_plans.side_effect = raises
    else:
        parser.suggest_plans.return_value = suggest_return
    return parser


def test_llm_error_dict_is_surfaced(db):
    """suggest_plans 返回 {"error": ...} 时，结果必须带 llm_error=True 与 detail。"""
    parser = _configured_parser(suggest_return={"error": "LLM 调用失败：HTTP 503"})
    agent = RescheduleAgent(db, llm_parser=parser)
    result = agent.process_request("我是吴琳，把声速测量调到第3周", True)
    assert result["llm_error"] is True
    assert result["llm_error_detail"] == "LLM 调用失败：HTTP 503"
    # 降级到规则推荐仍会发生，但必须带着故障信号 —— 不能冒充 AI 方案
    assert result["ai_plans_source"] in ("rule", "none")
    assert result["success"] is True


def test_llm_exception_is_surfaced(db):
    """suggest_plans 抛出异常时，结果必须带 llm_error=True 与异常详情。

    注意：LLM 失败仍会优雅降级到规则推荐（ai_plans 非空、source="rule"），
    这是预期行为；关键断言是**绝不能**把降级产物标成 "llm"，且故障信号必须置位。
    """
    parser = _configured_parser(raises=RuntimeError("upstream timeout"))
    agent = RescheduleAgent(db, llm_parser=parser)
    result = agent.process_request("我是吴琳，把声速测量调到第3周", True)
    assert result["llm_error"] is True
    assert "upstream timeout" in result["llm_error_detail"]
    assert result["ai_plans_source"] == "rule"
    assert not any(p.get("source") == "llm" for p in result["ai_plans"])


def test_fast_mode_does_not_report_llm_error(db):
    """fast 模式（with_ai=False）跳过 LLM，不算失败，且不应调用 suggest_plans。"""
    parser = _configured_parser(suggest_return={"error": "boom"})  # 即便会失败也不该被调用
    agent = RescheduleAgent(db, llm_parser=parser)
    result = agent.process_request("我是吴琳，把声速测量调到第3周", False)
    assert result["llm_error"] is False
    assert result["llm_error_detail"] is None
    parser.suggest_plans.assert_not_called()


def test_unconfigured_llm_does_not_report_llm_error(db):
    """LLM 未配置（离线常态）不算失败，且不应调用 suggest_plans。"""
    parser = MagicMock()
    parser.configured = False  # 离线
    parser.parse.return_value = {}
    agent = RescheduleAgent(db, llm_parser=parser)
    result = agent.process_request("我是吴琳，把声速测量调到第3周", True)
    assert result["llm_error"] is False
    assert result["llm_error_detail"] is None
    parser.suggest_plans.assert_not_called()


def test_successful_llm_no_error(db):
    """LLM 成功时 llm_error 必须保持 False（不能污染信号），来源标 llm。"""
    parser = _configured_parser(suggest_return={
        "plans": [{"weekday": "周一", "time_slot": "567", "room": "A410",
                   "reason": "与原课时段接近"}]
    })
    agent = RescheduleAgent(db, llm_parser=parser)
    result = agent.process_request("我是吴琳，把声速测量调到第3周", True)
    assert result["llm_error"] is False
    assert result["llm_error_detail"] is None
    assert result["ai_plans_source"] == "llm"
