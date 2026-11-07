"""按客户端 IP 的滑动窗口限流。

为什么要这个模块
----------------
本服务发布在公网单端口上，链接是**零门槛公开**的（无登录、无白名单），
而其中若干接口带**真实资源代价**：

    /chat、/experiment-tutor   后端直连 LLM（消耗额度/费用）
    /auto-schedule                             CP-SAT 求解，单次最多 10 秒 CPU
    /parallel/solve                            CP-SAT 求解，单次最多 10 秒 CPU
    /impact-analysis                           影响分析（规则 + LLM）
    /admin-login                               口令校验（可被爆破）

这些接口**不能加鉴权**——它们是教秘演示的主路径，加了会直接废掉演示
（前端实名调用，且演示者不持有任何令牌）。所以治理手段是**限流**而非门禁：
正常演示碰不到，脚本化高频调用被挡住。

设计取向
--------
1. **阈值刻意放宽**：一个人正常演示（点几下、看结果、偶尔重试）远够不到阈值，
   闸门只拦"机器级高频"。数值见下方 `RULES`。
2. **三维度计数**（任一超限即 429）：
   - `fp`  客户端指纹（User-Agent + Accept-Language）→ 抗"源 IP 被轮换/伪造"
   - `ip`  源 IP                                      → 抗"单 IP 洪峰"
   - `all` 全站总量                                   → 兜底
   为什么必须有指纹这一维（**线上实测结论，不是设计洁癖**）：
   2026-10-15 线上压测 34 次 `/admin-login` 全部 401、一次 429 都没触发
   （同一份代码本地第 31 次必触发）。逐项探测确认：边缘网关会**覆盖**
   客户端自带的 `X-Forwarded-For` / `X-Real-IP`，而它注入的源 IP 逐次变化
   ——于是"单 IP 桶"永远攒不满。指纹维在同一压测里稳定命中，
   把一条**无法验证的防护**变成了可验证的。
3. **进程内内存**：与本项目其它状态一致（沙箱实测为单进程有状态服务），
   不引入 Redis 等外部依赖。
4. **零副作用**：只记录时间戳，不触任何业务状态；进程重启自动清零。

为什么不加鉴权而用限流
----------------------
这些接口都是**教秘演示的主路径**，前端实名调用且演示者不持有任何令牌，
加门禁会直接废掉演示。所以治理手段是限流而非门禁。

已知局限（不要误以为它是万能的）
--------------------------------
指纹与源 IP 都是**客户端可伪造**的：同时轮换 UA 与出口 IP 的攻击者能绕开
前两维，只剩全站桶兜底。它的定位是"抬高滥用成本、挡住无脑脚本"，
不是身份级访问控制。真正要防住单点滥用，仍需在网关层做（本沙箱不提供）。

环境变量
--------
    RATELIMIT_DISABLED=1   关闭限流（本地压测/调试用，发布态不要开）
"""

from __future__ import annotations

import hashlib
import os
import threading
import time
from collections import deque
from typing import Deque, Dict, List, Optional, Tuple

from fastapi import HTTPException, Request, status

# ---------------------------------------------------------------------------
# 规则表：bucket 名 → (单客户端上限, 全站上限, 窗口秒数)
#
# "单客户端"= 源 IP 或浏览器指纹，二者**各自独立计数，任一超限即拦**，
# 所以这里的数字对同一个人/同一台机器都成立（不会因为多算一维而收紧一半）。
#
# 数值按"宁松勿紧"取：目标是拦住**机器级高频**（脚本刷量、并发压测、薅 LLM 额度），
# 而不是限制真人的演示节奏。以 60 秒窗为例，单客户端 120~300 次意味着
# 「平均每 0.2~0.5 秒一次且允许突发」——真人点按绝对够不到，脚本一刷就触顶。
# 求解类接口单次开销大（CP-SAT 最多 10 秒 CPU），所以上限压得低一档。
#
# 若演示时真被误伤（同一台机器多人共用一个浏览器指纹），只需调高对应数字，
# 或把窗口拉长为 300 秒；应急可设 `RATELIMIT_DISABLED=1` 整体关闭。
# ---------------------------------------------------------------------------
RULES: Dict[str, Tuple[int, int, int]] = {
    # LLM 类：慢（60s 超时）但单价低 → 给足余量，正常演示日均也碰不到
    "chat": (120, 1200, 60),
    "experiment-tutor": (120, 1200, 60),
    # 影响分析只走规则计算（不调 LLM），成本很低
    "impact-analysis": (180, 1800, 60),
    # 求解类：单次最多 10 秒 CPU，60/分钟已远超"人手点按"所需
    "auto-schedule": (60, 600, 60),
    "parallel-solve": (60, 600, 60),
    # 口令校验：防爆破为主，真人输错几次绝不该被锁
    "admin-login": (30, 300, 60),
    # 下面两个是**免鉴权 + 单次真开销**的组合（D-7）：
    #   load-default  重解析 433 行 Excel（~1s CPU，本地实测 0.98s；冷沙箱 3.8s）
    #   parallel-export 每次生成并落盘一个 xlsx
    # 前端只在首屏调用一次 load-default，阈值放到 20/分钟 仍远超正常节奏。
    "load-default": (20, 200, 60),
    "parallel-export": (20, 200, 60),
    # 标准课表导出（班级/场地）：每次遍历全表并生成一个多 sheet 的 xlsx，
    # 代价与 parallel-export 同档
    "standard-export": (20, 200, 60),
    # 方案落地：预览是只读 diff；采纳/撤销是 sqlite 单行写 + 内存替换，
    # 单次开销小，但属"教秘想清楚才点"的操作，阈值与导出同档。
    # land/status 刻意**不在这个桶**里：它是零开销只读查询（界面挂载/切 tab
    # 就调），和写操作混桶会让演示态的无谓查询消耗落地额度——2026-10-24
    # 实测：UI 探针一轮跑下来 60s 内 status 查询超 20 次撞 429，而落地本身
    # 只点了 2 次。只读查询给到求解类同档（60/分钟）仍远超真人节奏。
    "parallel-land": (20, 200, 60),
    "parallel-land-status": (60, 600, 60),
}

# 单 IP 桶的 key 数量上限：防止伪造大量源 IP 撑爆内存。
# 触顶时整体重置（限流状态丢失可接受，好过 OOM）。
_MAX_IP_KEYS = 20_000


def _disabled() -> bool:
    return os.getenv("RATELIMIT_DISABLED", "").strip().lower() in {"1", "true", "yes", "on"}


class SlidingWindowLimiter:
    """按 key 的滑动窗口计数器（线程安全）。

    每个 key 维护一个时间戳队列；命中时先丢弃窗口外的旧记录，
    再判断队列长度是否已达上限。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._hits: Dict[str, Deque[float]] = {}

    def hit(
        self,
        key: str,
        limit: int,
        window: int,
        now: Optional[float] = None,
    ) -> int:
        """记一次访问并判断是否超限。

        返回 0 表示放行；返回正数表示超限，值为建议等待的秒数（供 `Retry-After`）。
        `now` 仅用于测试注入，运行时留空即可。
        """
        ts = time.monotonic() if now is None else now
        cutoff = ts - window

        with self._lock:
            if len(self._hits) > _MAX_IP_KEYS:
                # 极端情况（大量伪造源 IP）下重置，避免内存无限增长。
                self._hits.clear()

            dq = self._hits.get(key)
            if dq is None:
                dq = deque()
                self._hits[key] = dq

            while dq and dq[0] <= cutoff:
                dq.popleft()

            if len(dq) >= limit:
                # 最早那条滑出窗口时即可再次放行
                return max(1, int(dq[0] + window - ts) + 1)

            dq.append(ts)
            return 0

    def peek(self, key: str, window: int, now: Optional[float] = None) -> int:
        """只读：返回该 key 在窗口内的当前计数（测试与诊断用）。"""
        ts = time.monotonic() if now is None else now
        cutoff = ts - window
        with self._lock:
            dq = self._hits.get(key)
            if not dq:
                return 0
            return sum(1 for t in dq if t > cutoff)

    def reset(self) -> None:
        """清空全部计数（测试用）。"""
        with self._lock:
            self._hits.clear()


LIMITER = SlidingWindowLimiter()


def client_ip(request: Request) -> str:
    """提取客户端 IP。

    沙箱前面有网关，`request.client.host` 往往是网关地址，所以优先看转发头。
    注意：转发头可被伪造，且**线上实测边缘会自己覆盖它**（客户端自带的值
    到不了本函数），因此这一维只作参考，真正的兜底是指纹维与全站桶。
    """
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        first = forwarded.split(",")[0].strip()
        if first:
            return first

    real_ip = (request.headers.get("x-real-ip") or "").strip()
    if real_ip:
        return real_ip

    return (request.client.host if request.client else "") or "unknown"


def client_fingerprint(request: Request) -> str:
    """客户端指纹：User-Agent + Accept-Language 的哈希前 16 位。

    为什么需要它（线上实测驱动）：边缘网关注入的源 IP **逐次变化**，
    导致"单 IP 桶"在线上永远攒不满（34 次压测 0 次 429）。
    指纹在同样压测下稳定命中，且对"同一台机器换出口 IP"的脚本依然有效。

    取短哈希即可：这里只做计数分桶，不需要抗碰撞强度，也不落任何可还原信息。
    """
    ua = (request.headers.get("user-agent") or "")[:200]
    lang = (request.headers.get("accept-language") or "")[:60]
    return hashlib.sha1(f"{ua}|{lang}".encode("utf-8", "ignore")).hexdigest()[:16]


def client_keys(request: Request) -> List[str]:
    """该请求参与计数的所有"单客户端"维度（不含全站桶）。"""
    return [f"ip|{client_ip(request)}", f"fp|{client_fingerprint(request)}"]


def rate_limit(bucket: str):
    """生成一个 FastAPI 依赖，给 `bucket` 对应的端点装上闸门。

    用法：`@router.post("/chat", dependencies=[Depends(rate_limit("chat"))])`
    """
    if bucket not in RULES:
        raise KeyError(f"未定义的限流 bucket：{bucket}")
    per_client, global_cap, window = RULES[bucket]

    async def _guard(request: Request) -> None:
        if _disabled():
            return

        # 三个维度**各自计数**（都记，再统一判定），任一超限即 429。
        # 先记后判是有意的：被拦下的请求也应计入，避免"被拦就等于没发生"
        # 让滥用者在限额边缘反复试探。
        waits: List[int] = []
        for suffix in client_keys(request):
            waits.append(LIMITER.hit(f"{bucket}|{suffix}", per_client, window))
        waits.append(LIMITER.hit(f"{bucket}|all", global_cap, window))

        wait = max(waits)
        if wait:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="请求过于频繁，请稍候再试。",
                headers={"Retry-After": str(wait)},
            )

    return _guard


# ---------------------------------------------------------------------------
# 并发闸（R-114）：限制**同时进行**的重活请求数
#
# 为什么"次数闸"不够
# ------------------
# 上面 RULES 的数字全是"每窗口允许几次"，没有"每次多少 CPU 秒"的维度。
# 求解类单次 10–280 秒，而只读查询 1–2ms，两者被同一套次数口径约束，于是：
#   600 次/分钟 × 280 秒 = 168000 CPU·秒 —— 而机器只有 24 核。
# 更关键的是单客户端维度挡不住：指纹 = sha1(UA+Accept-Language) 两行代码就能每请求变一次，
# 源 IP 在网关下本就逐次变化（见 client_ip 注释）。实测轮换 IP+UA 时前 600 次全部放行。
#
# 次数闸管的是"总量"，管不了"同时"。而"同时"才是把机器打瘫的那个变量：
# 求解走 run_in_threadpool，anyio 池上限 min(32, cpu+4)=28，600 个请求会全部
# 挤进去排队，28 个吃满 24 核，其余 572 个挂到网关超时 —— 教秘看到的是"网站卡死"。
#
# 口径：全站同时进行的重活 ≤ CPU//4（24 核 → 6）。超出的**立刻** 429 而不是排队，
# 因为排队只会把失败推迟到超时，并且继续占着连接。立刻拒绝让客户端知道"现在不行"。
# ---------------------------------------------------------------------------

# 进程内信号量：单进程部署够用（uvicorn 多 worker 时每个 worker 各自限，
# 总量按 worker 数放大，仍是可接受的上界；要全局精确需上 Redis，本项目不引这个依赖）。
_HEAVY_SEM_LIMIT = max(1, (os.cpu_count() or 4) // 4)
_HEAVY_SEM = threading.BoundedSemaphore(_HEAVY_SEM_LIMIT)
_HEAVY_ACTIVE = {"n": 0}
_HEAVY_LOCK = threading.Lock()


def heavy_slot(name: str):
    """给单次"重活"占一个并发位；拿不到就 429（不排队）。

    用法：
        @router.post("/parallel/solve")
        async def parallel_solve(...):
            async with heavy_slot("parallel-solve"):
                ...

    ⚠️ 释放必须放在 `finally`（`async with` 已保证）：异常路径漏放会让信号量
    永久少一个坑，跑满 `_HEAVY_SEM_LIMIT` 次失败后整个端点永久 429 —— 比不加闸更糟。
    """
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _ctx():
        got = _HEAVY_SEM.acquire(blocking=False)
        if not got:
            with _HEAVY_LOCK:
                active = _HEAVY_ACTIVE["n"]
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=(
                    f"服务器正在处理 {active} 个排课计算（上限 {_HEAVY_SEM_LIMIT}），"
                    "请稍后重试。"
                ),
                headers={"Retry-After": "15"},
            )
        with _HEAVY_LOCK:
            _HEAVY_ACTIVE["n"] += 1
        try:
            yield
        finally:
            with _HEAVY_LOCK:
                _HEAVY_ACTIVE["n"] -= 1
            _HEAVY_SEM.release()

    return _ctx()


def heavy_stats() -> Dict[str, int]:
    """当前并发占用（诊断用，供 /health 或测试断言）。"""
    with _HEAVY_LOCK:
        return {"active": _HEAVY_ACTIVE["n"], "limit": _HEAVY_SEM_LIMIT}
