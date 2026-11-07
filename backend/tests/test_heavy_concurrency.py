"""并发闸（R-114）：限制"同时进行"的重活数，而不只是"每窗口几次"。

为什么需要这个机制
------------------
`ratelimit.RULES` 的数字全是"每窗口允许几次"，**没有"每次多少 CPU 秒"的维度**。
求解类单次 10–280 秒（`solve_school` 一阶段 10s，空片重试 `max(30, 10*3)`，按 7 天
算最坏 280 CPU·秒），只读查询 1–2ms，两者被同一套次数口径约束：

    `parallel-solve` 全站桶 600 次/分钟 × 280 秒 = 168000 CPU·秒 —— 机器只有 24 核

而且单客户端维度挡不住：指纹是 `sha1(UA + Accept-Language)`（两行代码就能每请求
变一次），源 IP 在网关下本就"逐次变化"（`client_ip` 自己的注释这么写）。
实测轮换 IP+UA 时**前 600 次全部放行，第 601 次才拦**。

次数闸管总量，管不了"同时"。而"同时"才是把机器打瘫的变量：求解走
`run_in_threadpool`，anyio 池上限 `min(32, cpu+4)=28`，600 个请求全挤进去排队，
28 个吃满 24 核，其余 572 个挂到网关超时。

本文件锁三件事：
  1. 并发位用尽时，**立刻** 429（不排队 —— 排队只是把失败推迟到超时，还占着连接）。
  2. 正常路径释放（否则端点会永久少一个坑）。
  3. **异常路径也释放**（`async with` 的 finally）。第 3 条最重要：
     漏放会让信号量永久少坑，跑满上限次失败后，整个端点**永久 429** —— 比不加闸更糟，
     而且只在"出过错之后"才显形，是最难查的一类自伤。
"""

import asyncio
import os
import sys
from pathlib import Path

import pytest
from fastapi import HTTPException

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from app.ratelimit import _HEAVY_SEM_LIMIT, heavy_slot, heavy_stats  # noqa: E402


def _drain_all():
    """把可能泄漏的坑全部放掉，保证用例间互不影响。"""
    for _ in range(_HEAVY_SEM_LIMIT + 2):
        try:
            heavy_slot("drain").__enter__  # noqa: B018 - 仅探测存在性
            break
        except Exception:
            break
    # 直接归零：本模块自己的会计变量
    with __import__("app.ratelimit", fromlist=["x"])._HEAVY_LOCK:
        __import__("app.ratelimit", fromlist=["x"])._HEAVY_ACTIVE["n"] = 0


def test_limit_is_quarter_of_cpu():
    """上限应为 CPU//4 —— 求解是纯 CPU 活，同时开更多只会互相拖慢。"""
    expected = max(1, (os.cpu_count() or 4) // 4)
    assert expected == _HEAVY_SEM_LIMIT, (
        f"并发上限 {_HEAVY_SEM_LIMIT} 与 CPU//4={expected} 不一致"
    )


def test_slot_acquires_and_releases():
    """最基础的一条：拿一次、放一次，计数回到 0。"""

    async def go():
        before = heavy_stats()["active"]
        async with heavy_slot("t"):
            assert heavy_stats()["active"] == before + 1
        assert heavy_stats()["active"] == before

    asyncio.run(go())


def test_exhaustion_returns_429_immediately():
    """占满后必须**立刻** 429，且带 Retry-After（不是挂起）。"""

    async def go():
        held = []
        try:
            for _ in range(_HEAVY_SEM_LIMIT):
                c = heavy_slot("t")
                await c.__aenter__()
                held.append(c)

            with pytest.raises(HTTPException) as ei:
                await heavy_slot("t").__aenter__()
            assert ei.value.status_code == 429
            assert ei.value.headers.get("Retry-After"), "429 必须带 Retry-After"
            assert "上限" in ei.value.detail, f"提示信息应说明上限：{ei.value.detail}"
        finally:
            for c in held:
                await c.__aexit__(None, None, None)

    asyncio.run(go())
    assert heavy_stats()["active"] == 0


def test_release_on_exception():
    """← 最重要：异常路径必须释放。

    若不释放，每次求解抛错就永久丢一个并发位。跑满 `_HEAVY_SEM_LIMIT` 次失败后，
    整个求解端点会**永久 429** —— 用户侧表现是"排课功能彻底坏了"，
    而服务进程看起来完全正常（没有崩溃、日志里只有 429）。
    """

    async def go():
        for _ in range(_HEAVY_SEM_LIMIT + 3):   # 故意超过上限次数
            with pytest.raises(RuntimeError):
                async with heavy_slot("t"):
                    raise RuntimeError("模拟求解内部抛错")
        # 全部释放后，仍应能正常拿到位
        assert heavy_stats()["active"] == 0, (
            f"异常路径泄漏了并发位：{heavy_stats()}"
        )
        async with heavy_slot("t"):
            pass

    asyncio.run(go())


def test_cancellation_releases():
    """任务被取消（客户端断开/网关超时）时也必须释放。

    这条在真实场景里比异常更常见：用户在求解途中关掉页面，或网关在 60s 处
    掐断连接 —— FastAPI 会取消协程，若不释放就是同样的"永久少坑"。
    """

    async def worker():
        async with heavy_slot("t"):
            await asyncio.sleep(10)     # 长任务，等着被取消

    async def go():
        task = asyncio.create_task(worker())
        await asyncio.sleep(0.05)       # 让它拿到位
        assert heavy_stats()["active"] == 1
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert heavy_stats()["active"] == 0, (
            f"取消后泄漏了并发位：{heavy_stats()}"
        )

    asyncio.run(go())


def test_solve_endpoints_are_guarded():
    """三个求解入口都必须真的用上 heavy_slot。

    光有机制没用 —— 没有接到端点上的闸等于没有。这条按**源码**检查，
    因为用一个假请求去打真实求解太慢（单次 10–280 秒），不适合放进单测。
    """
    src = (BACKEND / "app" / "api" / "reschedule.py").read_text(encoding="utf-8")
    assert "heavy_slot(" in src, "reschedule.py 里根本没有引用 heavy_slot"

    # 每个求解端点函数体内必须出现一次 heavy_slot
    import ast
    tree = ast.parse(src)
    targets = {"parallel_solve", "parallel_solve_all", "auto_schedule"}
    found = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in targets:
            body = ast.get_source_segment(src, node) or ""
            found[node.name] = "heavy_slot(" in body

    missing = [n for n in targets if not found.get(n)]
    assert not missing, (
        f"这些求解端点没有接并发闸：{missing}（只加机制不接线，等于没加）"
    )


def test_heavy_stats_shape():
    """诊断接口的形状（供 /health 或运维读取）。"""
    st = heavy_stats()
    assert set(st) == {"active", "limit"}
    assert isinstance(st["active"], int) and isinstance(st["limit"], int)
    assert 0 <= st["active"] <= st["limit"]


def test_health_exposes_heavy_state():
    """`/health` 必须暴露求解并发占用 —— 运维要能一眼看出"是不是被压满了"。

    为什么这条值得单列：R-18 的教训是"一个永远返回 `{\"status\":\"ok\"}` 的探活，
    会把真实状态完全掩盖"。同理，如果并发闸已经 429 而 `/health` 只说 ok，
    运维会去翻日志猜；把 `active/limit` 摆出来，"排课慢"就有了解释。
    """
    from app.main import health_check

    body = health_check()
    assert "heavy" in body, "health 缺少 heavy 字段"
    assert set(body["heavy"]) == {"active", "limit"}
    assert body["heavy"]["limit"] >= 1
