"""LLM 调用路径的**离线**回归（用 httpx.MockTransport，不联网、不花钱）。

为什么需要这一组
----------------
`conftest.py` 全局置位 `LLM_DISABLED=1`，所以此前所有测试都只走"规则解析 + 降级"，
**真实调用 LLM 的三条路径一行都没被执行过**。用 `scripts/coverage_probe.py`
跑全量实测：`llm_intent_parser.py` 只有 **46.4%**，未覆盖的正好是
`parse()` / `consult()` / `suggest_plans()` 里 `httpx.Client(...)` 之后那些行
（即线上真实会走、测试从没走过的部分）。

而这里恰恰最容易静默出错：
  * 上游返回 200 但 `choices` 为空 → 老代码 `.get("choices", [{}])[0]` 会抛 IndexError；
  * 返回 `content` 是空串 → `_extract_json` 落到底部兜底，必须给出可读原因；
  * 返回 429/402 → 必须说清"是额度问题"，而不是一句"解析失败"；
  * `httpx` 抛超时 → 必须被吞成 `{"error": ...}`，不能让异常冒到路由层变 500。

**这里不测"模型答得对不对"**（那需要真模型），只测"我们怎么处理它的返回" ——
后者才是代码，前者是服务。所有响应都由 `MockTransport` 构造。
"""

import os
import sys

import httpx
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.agent import llm_intent_parser as L
from app.agent.llm_intent_parser import LLMIntentParser


ALL_KEY_ENVS = sorted({name for spec in L._CHANNELS.values() for name in spec["env"]})
SWITCHES = ("LLM_PROVIDER", "LLM_API_KEY", "LLM_API_BASE", "LLM_MODEL", "LLM_DISABLED")


@pytest.fixture
def parser(monkeypatch):
    """一个"已配置"的解析器（密钥是假的，请求全被拦截）。"""
    for name in (*ALL_KEY_ENVS, *SWITCHES):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setenv("LLM_API_BASE", "https://llm.test/v1")
    return LLMIntentParser()


def _mock(monkeypatch, handler):
    """把 httpx.Client 换成走 MockTransport 的版本（真实序列化，不联网）。"""
    real_client = httpx.Client

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(L.httpx, "Client", factory)


def _ok(content: str, status: int = 200):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={
            "choices": [{"message": {"content": content}}],
        })
    return handler


# ---------------------------------------------------------------- parse()

def test_parse_happy_path_returns_structured_slots(parser, monkeypatch):
    _mock(monkeypatch, _ok('{"teacher":"吴琳","target_week":5,"needs_followup":false}'))

    out = parser.parse("吴琳说下周三有事，往后挪一周")

    assert out["teacher"] == "吴琳"
    assert out["target_week"] == 5
    assert "error" not in out


def test_parse_survives_fenced_json(parser, monkeypatch):
    """模型经常把 JSON 包在 ```json 代码块里 —— 必须能剥出来。"""
    _mock(monkeypatch, _ok('好的，解析如下：\n```json\n{"target_week": 7}\n```\n以上。'))

    assert parser.parse("调到第7周")["target_week"] == 7


def test_parse_survives_leading_prose(parser, monkeypatch):
    """没有任何代码块、只是前后多说了话 —— 抓第一个 {...}。"""
    _mock(monkeypatch, _ok('我认为 {"target_week": 9, "teacher": "柯达"} 就是这样'))

    out = parser.parse("随便")
    assert out["target_week"] == 9 and out["teacher"] == "柯达"


def test_parse_reports_unparseable_output_instead_of_raising(parser, monkeypatch):
    """模型回了一段人话、没有 JSON → 必须返回可读的 error，不能抛。"""
    _mock(monkeypatch, _ok("抱歉，我不太明白你的意思。"))

    out = parser.parse("嗯")
    assert "error" in out
    assert "无法解析" in out["error"]
    assert out["raw"], "要在 error 里留下原始片段，否则线上无法定位"


def test_parse_does_not_crash_on_empty_choices(parser, monkeypatch):
    """HTTP 200 但 `choices` 为空数组 —— 必须给"空内容"的结论，而不是 Python 内部错误。

    实测（修前）：`{"choices": []}` 让 `[0]` 抛 `IndexError`，被外层 `except Exception`
    吞成 `LLM 调用异常：list index out of range`。不会崩服务，但把 Python 内部错误
    摆到 `/chat` 返回里 —— 教秘看到"系统坏了"，而真相是"上游没给内容"。
    这条断言**不许**再出现 `index out of range`。
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": []})
    _mock(monkeypatch, handler)

    out = parser.parse("调到第3周")
    assert "error" in out
    assert "空内容" in out["error"], out
    assert "index out of range" not in out["error"], out


def test_parse_reports_empty_string_content(parser, monkeypatch):
    """`content: ""`（限流/审核后的典型返回）同样要有明确结论。"""
    _mock(monkeypatch, _ok(""))

    out = parser.parse("调到第3周")
    assert "空内容" in out["error"], out


def test_parse_handles_non_dict_message(parser, monkeypatch):
    """`message` 不是 dict（某些网关会回字符串）不许抛。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": "oops"}]})
    _mock(monkeypatch, handler)

    assert "空内容" in parser.parse("x")["error"]


def test_parse_reports_http_status_with_its_code(parser, monkeypatch):
    """429/402 这类额度错误必须把状态码带出来 —— 否则分不清"限流"和"代码坏了"。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": "rate limited"})
    _mock(monkeypatch, handler)

    out = parser.parse("调到第3周")
    assert "429" in out["error"], out


def test_parse_swallows_transport_errors(parser, monkeypatch):
    """网络层异常（超时/连接失败）必须被吞成 error 字段，不能冒到路由层。"""
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out")
    _mock(monkeypatch, handler)

    out = parser.parse("调到第3周")
    assert "error" in out and "异常" in out["error"]


def test_parse_when_not_configured_returns_error_without_calling_out(monkeypatch):
    """未配置时不许发请求（离线/CI 的关键保证）。"""
    for name in (*ALL_KEY_ENVS, *SWITCHES):
        monkeypatch.delenv(name, raising=False)
    p = LLMIntentParser()
    assert p.configured is False

    def handler(request: httpx.Request) -> httpx.Response:   # pragma: no cover
        raise AssertionError("未配置时不该发出任何请求")
    _mock(monkeypatch, handler)

    out = p.parse("调到第3周")
    assert "未配置" in out["error"]


# ---------------------------------------------------------------- consult()

def test_consult_happy_path(parser, monkeypatch):
    _mock(monkeypatch, _ok("实验报告应包含误差分析。"))

    out = parser.consult("实验报告怎么写？")
    assert out["reply"] == "实验报告应包含误差分析。"


def test_consult_prepends_context(parser, monkeypatch):
    """上下文必须真的进请求体 —— 否则"实验规范查询"就失去了知识源。"""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json
        seen.update(_json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})
    _mock(monkeypatch, handler)

    parser.consult("学时多少？", context="【知识源】声速测量 3 学时")
    joined = "\n".join(m["content"] for m in seen["messages"])
    assert "声速测量 3 学时" in joined
    assert "学时多少？" in joined


def test_consult_role_prompt_is_selected(parser, monkeypatch):
    """不同角色应拿到不同 system prompt（规范查询 vs 通用）。"""
    prompts = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json
        body = _json.loads(request.content)
        prompts.append(body["messages"][0]["content"])
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})
    _mock(monkeypatch, handler)

    parser.consult("q1", role="experiment_tutor")
    parser.consult("q2", role="general")
    assert "实验规范" in prompts[0]
    assert prompts[0] != prompts[1]


def test_consult_reports_http_error(parser, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={})
    _mock(monkeypatch, handler)

    assert "500" in parser.consult("q")["error"]


def test_consult_swallows_transport_error(parser, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow")
    _mock(monkeypatch, handler)

    assert "异常" in parser.consult("q")["error"]


# ---------------------------------------------------------------- suggest_plans()

def test_suggest_plans_returns_plans(parser, monkeypatch):
    _mock(monkeypatch, _ok(
        '{"plans":[{"weekday":"周一","time_slot":"567","room":"A410","reason":"接近原时段"}]}'))

    out = parser.suggest_plans("吴琳", "声速测量", 3, [
        {"weekday": "周一", "time_slot": "567", "room": "A410"},
        {"weekday": "周二", "time_slot": "678", "room": "A411"},
    ])
    assert out["plans"][0]["room"] == "A410"


def test_suggest_plans_without_plans_key_is_an_error(parser, monkeypatch):
    """模型返回了合法 JSON 但没有 plans → 要说清，不能返回一个空成功。"""
    _mock(monkeypatch, _ok('{"slots":[]}'))

    out = parser.suggest_plans("吴琳", "声速测量", 3, [])
    assert "error" in out and out["raw"]


def test_suggest_plans_compresses_slots_to_two_per_day(parser, monkeypatch):
    """可用时段可能上百条：必须按天压缩，否则 token 爆炸（线上 429 的老原因）。"""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json
        seen.update(_json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"plans":[]}'}}]})
    _mock(monkeypatch, handler)

    slots = [{"weekday": "周一", "time_slot": f"slot{i}", "room": f"A{i}"} for i in range(40)]
    parser.suggest_plans("吴琳", "声速测量", 3, slots)

    prompt = seen["messages"][0]["content"]
    assert "slot0" in prompt
    assert "slot2" not in prompt, "每天只该保留前 2 个时段"


def test_suggest_plans_reports_http_error(parser, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(402, json={})
    _mock(monkeypatch, handler)

    assert "402" in parser.suggest_plans("吴琳", "声速测量", 3, [])["error"]


def test_suggest_plans_when_not_configured(monkeypatch):
    for name in (*ALL_KEY_ENVS, *SWITCHES):
        monkeypatch.delenv(name, raising=False)
    p = LLMIntentParser()
    assert p.configured is False
    assert "未配置" in p.suggest_plans("吴琳", "声速测量", 3, [])["error"]


# ---------------------------------------------------------------- 密钥不泄漏
# 这一条与 test_llm_config 里的同名承诺呼应，但覆盖面不同：那里测 describe()，
# 这里测**错误信息**——httpx 的异常字符串里可能带 URL，绝不能带 Authorization。

def test_error_messages_never_contain_the_api_key(parser, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("cannot connect to https://llm.test/v1/chat/completions")
    _mock(monkeypatch, handler)

    for payload in (parser.parse("x"), parser.consult("x"),
                    parser.suggest_plans("吴琳", "声速测量", 3, [])):
        blob = str(payload)
        assert "test-key" not in blob, f"错误信息里出现了密钥：{blob[:200]}"
