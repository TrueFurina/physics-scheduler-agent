"""R97：聊天族 LLM **意图解析**（parse）失败的结构化信号测试。

背景
----
R94 只修了 `suggest_plans`（AI 候选生成）的失败吞没。但 `process_request`
还有更早的一个 LLM 调用点：规则解析不出周次时会调 `llm_parser.parse(text)`。
两处彼此独立，而且 **parse 失败恰恰让 `target_week` 为空 ⇒ 根本走不到
`suggest_plans`**，R94 的信号在这条路径上永远没机会置位。

动手前只读探针实测（输入「我想调一下课」，规则解析不出周次）：

  - parse 返回 ``{"error": ...}`` → ``llm_error=False`` / detail=None /
    ``need_info=True`` / ``suggest_plans.called=False``（静默降级）
  - parse 抛异常                 → RuntimeError 直接冒泡，``/chat`` 会 500

R97 要求：LLM **已配置且真正失败**时结果必须带 ``llm_error=True`` 与
``llm_error_detail``；未配置（离线常态）、parse 成功、fast 模式都必须保持
``False``/``None``，不能污染信号（R94 同款防放宽）。
"""
import os
import sys
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.agent.reschedule_agent import RescheduleAgent
from app.utils.excel_parser import ScheduleDatabase

REAL = os.path.join(os.path.dirname(__file__), "..", "..", "schedule.demo.xlsx")
# 规则解析不出周次 ⇒ 必进 llm_parser.parse 分支（实测过，勿换成含「第N周/下周」的句子）
TEXT = "我想调一下课"


@pytest.fixture(scope="module")
def db():
    if not os.path.isfile(REAL):
        pytest.skip("缺少 schedule.demo.xlsx")
    d = ScheduleDatabase()
    d.load_from_excel(REAL)
    return d


def _parser(configured=True, parse_return=None, parse_raises=None):
    parser = MagicMock()
    parser.configured = configured
    if parse_raises is not None:
        parser.parse.side_effect = parse_raises
    else:
        parser.parse.return_value = parse_return if parse_return is not None else {}
    parser.suggest_plans.return_value = {"plans": []}
    return parser


def test_parse_error_dict_is_surfaced(db):
    """parse 返回 error dict ⇒ 必须带 llm_error=True 与 detail。"""
    parser = _parser(parse_return={"error": "LLM 调用失败：HTTP 503",
                                   "needs_followup": False})
    result = RescheduleAgent(db, llm_parser=parser).process_request(TEXT, True)
    assert parser.parse.called, "本用例必须真的走进 parse 分支，否则是空测"
    assert result["llm_error"] is True
    assert "HTTP 503" in result["llm_error_detail"]
    assert result["need_info"] is True
    # 这条路径根本走不到候选生成，R94 的信号不可能替它救场
    assert not parser.suggest_plans.called


def test_parse_exception_is_surfaced_not_raised(db):
    """parse 抛异常 ⇒ 不再冒泡 500，带信号优雅降级到追问。"""
    parser = _parser(parse_raises=RuntimeError("upstream timeout"))
    result = RescheduleAgent(db, llm_parser=parser).process_request(TEXT, True)
    assert result["llm_error"] is True
    assert "upstream timeout" in result["llm_error_detail"]
    assert result["need_info"] is True


def test_unconfigured_parse_error_does_not_report(db):
    """防放宽：未配置（离线常态）返回的 error 是预期，不能污染信号。"""
    parser = _parser(configured=False,
                     parse_return={"error": "LLM 未配置（未找到可用的 API 密钥）",
                                   "needs_followup": False})
    result = RescheduleAgent(db, llm_parser=parser).process_request(TEXT, True)
    assert parser.parse.called
    assert result["llm_error"] is False
    assert result["llm_error_detail"] is None


def test_successful_parse_no_error(db):
    """parse 成功 ⇒ 不置位，且后续照常进入候选生成（逐字节路径不变）。"""
    parser = _parser(parse_return={"target_week": 5, "teacher": "吴琳",
                                   "needs_followup": False})
    result = RescheduleAgent(db, llm_parser=parser).process_request(TEXT, True)
    assert result["llm_error"] is False
    assert result["llm_error_detail"] is None
    assert parser.suggest_plans.called


def test_followup_branch_preserves_signal(db):
    """追问分支（_need_info）也必须把信号透传出去，不能置位后又被丢掉。

    教师名已给出、但周次仍解析不出时，失败信号会一路走到 `_need_info`；
    如果调用点不传参，信号就在这里蒸发（规则 24：message 里说了 ≠ 用户看到了）。
    """
    parser = _parser(parse_return={"error": "LLM 输出无法解析为 JSON",
                                   "needs_followup": False})
    result = RescheduleAgent(db, llm_parser=parser).process_request(
        "我是吴琳，想调一下课", True)
    assert parser.parse.called
    assert result["need_info"] is True
    assert result["llm_error"] is True
    assert "无法解析为 JSON" in result["llm_error_detail"]
