"""限流回归测试（2026-10-15 新增）。

本服务发布在公网、链接零门槛公开，而若干免鉴权接口带真实资源代价
（LLM 额度、CP-SAT 的 10 秒 CPU、口令爆破面）。治理手段是限流而非门禁，
因此这里锁死两件**必须同时成立**的事：

  1. **机器级高频被挡住**：超限返回 429，且带 `Retry-After`；
  2. **真人演示不被误伤**：阈值放宽后，正常几十次调用必须全部放行。
     —— 只测 1 不测 2 的话，把阈值调到 1 也能"通过"，等于没锁住设计意图。

三维度计数各有不可替代的作用，逐个锁：
  - **指纹维**：轮换源 IP 也拦得住（线上实测：边缘注入的 IP 逐次变化，
    单靠 IP 维在线上 34 次压测 0 命中——这条用例就是那次事故的回归测试）；
  - **IP 维**：单 IP 洪峰拦得住；
  - **全站桶**：同时轮换 IP 与指纹时的兜底。

⚠️ 本文件的所有用例都必须显式打开限流（conftest 默认把它关了）。
"""

import asyncio
import os
import sys

import pytest
from fastapi import HTTPException
from starlette.requests import Request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.ratelimit import (  # noqa: E402
    LIMITER,
    RULES,
    SlidingWindowLimiter,
    client_fingerprint,
    client_ip,
    rate_limit,
)

# 真实浏览器/脚本都会带的头；TestClient 默认带 `user-agent: testclient`
_UA = {"user-agent": "Mozilla/5.0 (Windows NT 10.0) Chrome/131.0"}


def _enable(monkeypatch) -> None:
    """打开限流并清空计数（conftest 默认关闭）。"""
    monkeypatch.delenv("RATELIMIT_DISABLED", raising=False)
    LIMITER.reset()


def _req(ip: str, headers: dict | None = None) -> Request:
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/reschedule/chat",
        "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
        "client": (ip, 54321),
    }
    return Request(scope)


# ---------------------------------------------------------------------------
# 一、计数器的基本行为
# ---------------------------------------------------------------------------


def test_allows_until_limit_then_blocks():
    lim = SlidingWindowLimiter()
    for i in range(3):
        assert lim.hit("k", limit=3, window=60, now=1000.0) == 0, f"第 {i+1} 次应放行"
    wait = lim.hit("k", limit=3, window=60, now=1000.0)
    assert wait > 0, "超过上限必须返回等待秒数"
    assert 1 <= wait <= 61


def test_window_slides_back():
    """窗口滑出后应重新放行——否则限流会退化成"一次超限永久封禁"。"""
    lim = SlidingWindowLimiter()
    assert lim.hit("k", limit=2, window=10, now=100.0) == 0
    assert lim.hit("k", limit=2, window=10, now=100.0) == 0
    assert lim.hit("k", limit=2, window=10, now=105.0) > 0     # 窗口内仍满
    assert lim.hit("k", limit=2, window=10, now=111.0) == 0    # 最早那条已滑出


def test_peek_is_read_only_and_reset_clears():
    lim = SlidingWindowLimiter()
    lim.hit("k", limit=5, window=60, now=100.0)
    assert lim.peek("k", window=60, now=100.0) == 1
    assert lim.peek("k", window=60, now=100.0) == 1  # peek 不计数
    lim.reset()
    assert lim.peek("k", window=60, now=100.0) == 0


def test_disabled_env_bypasses(monkeypatch):
    """RATELIMIT_DISABLED=1 时整体放行（本地压测/调试用）。"""
    monkeypatch.setenv("RATELIMIT_DISABLED", "1")
    LIMITER.reset()
    guard = rate_limit("chat")
    for _ in range(5):
        asyncio.run(guard(_req("1.2.3.4")))


# ---------------------------------------------------------------------------
# 二、源 IP 提取
# ---------------------------------------------------------------------------


def test_client_ip_prefers_forwarded_then_real_ip():
    assert client_ip(_req("10.0.0.1", {"x-forwarded-for": "203.0.113.9, 10.0.0.1"})) == "203.0.113.9"
    assert client_ip(_req("10.0.0.1", {"x-real-ip": "198.51.100.7"})) == "198.51.100.7"
    assert client_ip(_req("10.0.0.1")) == "10.0.0.1"


def test_fingerprint_ignores_ip_and_follows_user_agent():
    """指纹必须与源 IP 无关、与 UA 相关——这正是它比 IP 可靠的原因。"""
    a = client_fingerprint(_req("1.1.1.1", {**_UA}))
    b = client_fingerprint(_req("2.2.2.2", {**_UA}))
    c = client_fingerprint(_req("1.1.1.1", {"user-agent": "curl/8.0"}))
    assert a == b, "同 UA 不同 IP 必须落到同一个桶"
    assert a != c, "不同 UA 必须分开计数"


# ---------------------------------------------------------------------------
# 三、三维度计数：任一维度超限都拦得住
# ---------------------------------------------------------------------------


def test_rotating_source_ip_does_not_bypass(monkeypatch):
    """**线上事故的回归测试**：每次换源 IP、但不换 UA，仍然要被拦住。

    背景：线上压测 34 次 `/admin-login` 一次 429 都没触发，原因是边缘网关
    注入的源 IP 逐次变化，单 IP 桶永远攒不满。若只按 IP 计数，这条会失败。
    """
    _enable(monkeypatch)
    monkeypatch.setitem(RULES, "admin-login", (3, 10_000, 60))
    guard = rate_limit("admin-login")

    for i in range(3):
        ip = f"203.0.113.{i}"
        asyncio.run(guard(_req(ip, {**_UA, "x-forwarded-for": ip})))

    with pytest.raises(HTTPException) as exc:
        asyncio.run(guard(_req("203.0.113.200", {**_UA, "x-forwarded-for": "203.0.113.200"})))
    assert exc.value.status_code == 429


def test_global_bucket_catches_ip_spoofing(monkeypatch):
    """每个客户端都在自己额度内，但全站总量超限 → 仍然 429。

    这一条覆盖"同时轮换 IP"的场景（此时指纹维也可能被绕过），全站桶是最后兜底。
    """
    _enable(monkeypatch)
    monkeypatch.setitem(RULES, "admin-login", (100, 3, 60))  # 单客户端很宽，全站只有3
    guard = rate_limit("admin-login")

    for i in range(3):
        ip = f"203.0.113.{i}"
        asyncio.run(guard(_req(ip, {**_UA, "x-forwarded-for": ip})))  # 三个不同伪造 IP，全放行

    with pytest.raises(HTTPException) as exc:
        asyncio.run(guard(_req("203.0.113.99", {**_UA, "x-forwarded-for": "203.0.113.99"})))
    assert exc.value.status_code == 429


def test_per_client_bucket_blocks_single_source(monkeypatch):
    _enable(monkeypatch)
    monkeypatch.setitem(RULES, "chat", (2, 1000, 60))
    guard = rate_limit("chat")
    asyncio.run(guard(_req("198.51.100.5", {**_UA})))
    asyncio.run(guard(_req("198.51.100.5", {**_UA})))
    with pytest.raises(HTTPException) as exc:
        asyncio.run(guard(_req("198.51.100.5", {**_UA})))
    assert exc.value.status_code == 429
    assert "Retry-After" in (exc.value.headers or {})


# ---------------------------------------------------------------------------
# 四、端到端：真实阈值下的 429，以及"演示节奏不被误伤"
# ---------------------------------------------------------------------------


def test_admin_login_gets_429_after_real_threshold(monkeypatch):
    """按真实阈值压 /admin-login：前 N 次是 401（口令错），第 N+1 次必须 429。"""
    from fastapi.testclient import TestClient
    from app.main import app

    _enable(monkeypatch)
    per_ip = RULES["admin-login"][0]
    client = TestClient(app)
    bad = {"username": "admin", "password": "definitely-wrong"}

    for i in range(per_ip):
        assert client.post("/api/reschedule/admin-login", json=bad).status_code == 401, (
            f"第 {i+1} 次（限额内）应正常返回 401"
        )

    blocked = client.post("/api/reschedule/admin-login", json=bad)
    assert blocked.status_code == 429
    assert "Retry-After" in blocked.headers


def test_demo_pace_is_not_throttled(monkeypatch):
    """演示节奏不变量：阈值放宽后，连续 50 次**受限接口**调用必须全部 200。

    注意必须打真正挂了限流的接口（`/chat`），打 `/status` 这种没挂闸门的
    接口是测不出东西的；同时用能被规则解析出周次的文本（"第六周"），
    保证用例本身廉价可控、不触发 LLM。

    防的是"为了过限流测试而把阈值调到 1"这种反向优化。
    """
    from fastapi.testclient import TestClient
    from app.main import app

    _enable(monkeypatch)
    LIMITER.reset()
    client = TestClient(app)

    per_client = RULES["chat"][0]
    assert per_client >= 60, "chat 阈值低于 60/分钟会开始影响正常演示"

    payload = {"message": "第六周", "fast": True}
    for i in range(50):
        r = client.post("/api/reschedule/chat", json=payload)
        assert r.status_code == 200, f"第 {i+1} 次受限接口调用被误伤：{r.status_code}"
        # 兜住另一个方向：这条请求不该被降级成"请补充信息"
        # （那意味着规则没解析出周次、白调了一次 LLM）
        assert "补充信息" not in r.text, "规则解析未生效，说明又被 LLM 兜底了"
