"""LLM 渠道配置回归测试（2026-10-15 新增）。

背景：原实现把渠道写死在 `JIYUAN_LVDONG_BASE_URL/JIYUAN_LVDONG_API_KEY`
（tokenrhythm.studio 第三方中转），而 `E:\\Program\\免费Token总账.md` 2026-10-15
实测该 key **余额不足 402** —— 也就是说后端 LLM 当时根本调不通，只是被
"规则解析优先"的降级路径掩盖了（模板表单还专门用 `fast=true` 绕过它）。

改成"渠道表 + 环境变量自动探测"后，这里锁死三件事：

  1. **默认渠道必须是总账里"永久免费不限量"的那个**（智谱 glm-4-flash）。
     哪天有人把它改成付费渠道或失效渠道，这些用例会红——这正是本次修复的目的。
  2. **优先级正确**：显式 `LLM_API_KEY` > `LLM_PROVIDER` 指定 > 自动探测顺序。
  3. **`describe()` 不泄漏密钥**：它会被 `/status` 输出到公网（核验渠道用），
     只能含 provider / model / endpoint / key_source。
"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.agent.llm_intent_parser import (
    _CHANNELS,
    LLMIntentParser,
    resolve_llm_config,
)

# 渠道表里出现过的所有密钥变量名
_ALL_KEY_ENVS = sorted({name for spec in _CHANNELS.values() for name in spec["env"]})
# 决定"选哪个渠道"的开关；`LLM_DISABLED` 必须在列——conftest 默认置位它，
# 不清掉的话下面每个用例都会拿到"未配置"。
_SWITCHES = ("LLM_PROVIDER", "LLM_API_KEY", "LLM_API_BASE", "LLM_MODEL", "LLM_DISABLED")


def _clear_env(monkeypatch) -> None:
    """清空所有渠道密钥与切换开关，得到一个"什么都没配"的干净起点。

    必须显式清空：`app/__init__.py` 会把仓库根 `.env` 灌进 `os.environ`，
    所以测试进程里这些变量通常是**有值**的。
    """
    for name in (*_ALL_KEY_ENVS, *_SWITCHES):
        monkeypatch.delenv(name, raising=False)


# ---------------------------------------------------------------------------
# 一、默认渠道 = 总账里的"永久免费"渠道
# ---------------------------------------------------------------------------


def test_default_channel_is_free_zhipu(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("ZHIPU_API_KEY", "test-key")

    cfg = resolve_llm_config()
    assert cfg["provider"] == "zhipu"
    assert cfg["model"] == "glm-4-flash", "默认模型必须是永久免费的 glm-4-flash"
    assert "open.bigmodel.cn" in cfg["base_url"]
    assert cfg["configured"] is True


def test_auto_order_prefers_free_over_paid(monkeypatch):
    """同时存在 ModelScope（免费有限额）与 DeepSeek（付费余额制）时选前者。"""
    _clear_env(monkeypatch)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setenv("MODELSCOPE_API_KEY", "test-key")

    assert resolve_llm_config()["provider"] == "modelscope"


def test_dead_channel_is_last_resort_and_labelled(monkeypatch):
    """已欠费的 jiyuan 只能当最后兜底，且备注里必须写明 402。"""
    _clear_env(monkeypatch)
    monkeypatch.setenv("JIYUAN_LVDONG_API_KEY", "test-key")

    cfg = resolve_llm_config()
    assert cfg["provider"] == "jiyuan"
    assert "402" in cfg["note"], "已欠费事实必须留在备注里，避免又被当默认渠道"


# ---------------------------------------------------------------------------
# 二、优先级
# ---------------------------------------------------------------------------


def test_explicit_llm_api_key_wins(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("ZHIPU_API_KEY", "zhipu-key")
    monkeypatch.setenv("LLM_API_KEY", "explicit-key")
    monkeypatch.setenv("LLM_API_BASE", "https://example.invalid/v1")
    monkeypatch.setenv("LLM_MODEL", "my-model")

    cfg = resolve_llm_config()
    assert cfg["provider"] == "custom"
    assert cfg["api_key"] == "explicit-key"
    assert cfg["base_url"] == "https://example.invalid/v1"
    assert cfg["model"] == "my-model"


def test_llm_provider_forces_channel(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("ZHIPU_API_KEY", "zhipu-key")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "deepseek-key")
    monkeypatch.setenv("LLM_PROVIDER", "deepseek")

    cfg = resolve_llm_config()
    assert cfg["provider"] == "deepseek"
    assert cfg["api_key"] == "deepseek-key"
    assert "deepseek.com" in cfg["base_url"]


def test_unknown_provider_falls_back_to_auto(monkeypatch):
    """写错渠道名不能直接把 LLM 弄死——退回自动探测。"""
    _clear_env(monkeypatch)
    monkeypatch.setenv("LLM_PROVIDER", "not-a-real-channel")
    monkeypatch.setenv("ZHIPU_API_KEY", "zhipu-key")

    assert resolve_llm_config()["provider"] == "zhipu"


def test_unconfigured_is_reported_explicitly(monkeypatch):
    _clear_env(monkeypatch)

    cfg = resolve_llm_config()
    assert cfg["configured"] is False
    assert cfg["api_key"] == ""
    assert "总账" in cfg["note"], "未配置时要指向总账，方便排查"


def test_llm_disabled_forces_offline(monkeypatch):
    """LLM_DISABLED=1 时即便有可用密钥也判定为未配置（离线开发 / CI 用）。"""
    _clear_env(monkeypatch)
    monkeypatch.setenv("ZHIPU_API_KEY", "test-key")
    monkeypatch.setenv("LLM_DISABLED", "1")

    cfg = resolve_llm_config()
    assert cfg["configured"] is False
    assert "LLM_DISABLED" in cfg["note"]

    parser = LLMIntentParser()
    assert parser.configured is False
    assert parser.describe()["configured"] is False


# ---------------------------------------------------------------------------
# 三、安全不变量：自述信息不得泄漏密钥
# ---------------------------------------------------------------------------


def test_describe_never_leaks_api_key(monkeypatch):
    _clear_env(monkeypatch)
    sentinel = "SENTINEL_SECRET_DO_NOT_LEAK_9f3a"
    monkeypatch.setenv("ZHIPU_API_KEY", sentinel)

    parser = LLMIntentParser()
    blob = json.dumps(parser.describe(), ensure_ascii=False)

    assert sentinel not in blob, "describe() 会被 /status 暴露到公网，绝不能带密钥"
    assert parser.describe()["key_source"] == "ZHIPU_API_KEY"
    assert parser.describe()["endpoint"] == "open.bigmodel.cn"
    assert parser.configured is True


def test_status_endpoint_exposes_channel_without_key(monkeypatch):
    """端到端确认 /status 的 ai 字段可用作线上核验，且同样不含密钥。"""
    from fastapi.testclient import TestClient
    from app.main import app

    _clear_env(monkeypatch)
    sentinel = "SENTINEL_SECRET_DO_NOT_LEAK_9f3a"
    monkeypatch.setenv("ZHIPU_API_KEY", sentinel)

    r = TestClient(app).get("/api/reschedule/status")
    assert r.status_code == 200
    assert sentinel not in r.text
    assert "ai" in r.json()


# ---------------------------------------------------------------------------
# 四、运行期可重载（不重启进程切换渠道）
# ---------------------------------------------------------------------------


def test_reload_config_picks_up_new_env(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("ZHIPU_API_KEY", "zhipu-key")
    parser = LLMIntentParser()
    assert parser.provider == "zhipu"

    monkeypatch.setenv("DEEPSEEK_API_KEY", "deepseek-key")
    monkeypatch.setenv("LLM_PROVIDER", "deepseek")
    parser.reload_config()

    assert parser.provider == "deepseek"
    assert parser.MODEL == "deepseek-chat"
    assert parser.API_KEY == "deepseek-key"
