"""服务端会话门禁：管理员凭据校验 + HMAC 自签会话令牌。

为什么要这个模块
----------------
本服务发布在公网单端口上，而 `/api/reschedule/**` 原先**没有任何鉴权**：
`DELETE /requests`、`POST /requests/{id}/approve`、`POST /upload` 等写接口
任何人都能直接调用——绕开前端"云登录 + 管理员口令"两步即可改数据。

原先 `/admin-login` 看似在服务端校验，但它 `return {"success": True}` 之后
**不发任何凭据**，也就不存在"会话"这回事：调用方拿到 success 就完了，
下一个请求依然是匿名的。前端口令比对又写在 bundle 里（`App.tsx` 中可 grep 到明文），
所以那个"校验"只是 UI 层遮蔽。

设计取向
--------
1. **零外部依赖**：不验云 JWT、不连外部服务，用进程内 HMAC 自签会话，
   纯标准库实现，发布沙箱里不会因缺依赖或网络问题失效。
2. **密钥不落盘**：默认每次进程启动随机生成。宁可沙箱重启后管理员重新登录，
   也不把密钥写进会随包上传的仓库目录（`backend/app/data/` 正是会被上传的目录，
   写在那里等于把密钥一起发布出去）。
   需要跨重启保持会话时，用环境变量 `APP_SECRET_KEY` 注入。
3. **定时比较**：凭据与签名一律 `hmac.compare_digest`，避免计时侧信道。
4. **失效关闭（fail-closed）**：令牌解析/签名任一环节异常即视为无效，
   不返回"部分通过"。

环境变量
--------
- `APP_SECRET_KEY`   会话签名密钥（不设则进程内随机生成）
- `ADMIN_USER`       管理员账号，默认 `admin`
- `ADMIN_PASS`       管理员口令，**无默认值**；未设置则启动失败（fail-closed）。
                     设为公开演示口令（如 admin123）只告警不拦截，仅限本地演示。
- `ADMIN_TOKEN_TTL`  令牌有效期秒数，默认 8 小时
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from typing import Optional

from fastapi import HTTPException, Request, status

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

ADMIN_USER = os.getenv("ADMIN_USER", "admin")

# 口令**不给默认值**：缺失就是配置错误，启动即失败。
#
# 为什么不沿用 `os.getenv("ADMIN_PASS", "admin123")`：
# 那个写法是 fail-open —— 忘配环境变量不报错，服务照常起来，
# 公网端口上就挂着一个全仓库都读得到的默认口令。
# "忘记配置"恰恰是最常见的线上事故来源，应该让它在**启动时**炸，
# 而不是在**被人登录时**才暴露。
#
# 演示/本地开发：启动前显式设置 ADMIN_PASS（start.bat / start.ps1 已内置演示值）。
# 生产：注入强口令，不要复用演示值。
_ADMIN_PASS_RAW = os.getenv("ADMIN_PASS")
if not _ADMIN_PASS_RAW:
    raise RuntimeError(
        "ADMIN_PASS 未设置，拒绝启动。\n"
        "  本地演示：set ADMIN_PASS=<demo-value>（Windows）或 export ADMIN_PASS=...（bash）\n"
        "  生产环境：注入强口令，且不要与仓库中的演示值相同。"
    )
ADMIN_PASS = _ADMIN_PASS_RAW

# 演示默认口令的已知值：出现即视为配置缺陷，启动时大声警告。
# 只做告警不做拦截——本地演示仍要能跑；但线上日志里一眼能看到这条。
_DEMO_PASSWORDS = {"admin123", "admin", "password", "123456"}
if ADMIN_PASS in _DEMO_PASSWORDS:
    import warnings

    warnings.warn(
        "ADMIN_PASS 使用了公开的演示口令。仅限本地演示；公网部署必须更换。",
        RuntimeWarning,
        stacklevel=2,
    )

try:
    TOKEN_TTL_SECONDS = int(os.getenv("ADMIN_TOKEN_TTL", str(8 * 3600)))
except ValueError:
    TOKEN_TTL_SECONDS = 8 * 3600


def _load_secret() -> bytes:
    """取得签名密钥。

    优先用环境变量（便于跨重启保持会话）；未配置时生成进程内随机密钥。
    刻意不落盘：本仓库的 `backend/app/data/` 会随发布包上传到沙箱，
    把密钥写进去等同于公开发布。
    """
    env_secret = os.getenv("APP_SECRET_KEY", "").strip()
    if env_secret:
        return env_secret.encode("utf-8")
    return secrets.token_bytes(32)


_SECRET: bytes = _load_secret()

# 重启后令牌失效属于预期行为：随机密钥不跨进程复用。
_SECRET_IS_EPHEMERAL: bool = not os.getenv("APP_SECRET_KEY", "").strip()


# ---------------------------------------------------------------------------
# 令牌签发 / 校验
# ---------------------------------------------------------------------------


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64d(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def _sign(payload_b64: str) -> str:
    mac = hmac.new(_SECRET, payload_b64.encode("ascii"), hashlib.sha256).digest()
    return _b64e(mac)


def issue_admin_token(username: str, ttl: Optional[int] = None) -> dict:
    """签发管理员会话令牌。

    返回 `{"token": ..., "expires_in": ..., "expires_at": ...}`，
    其中 `expires_at` 为 Unix 秒时间戳，便于前端做倒计时。
    """
    lifetime = TOKEN_TTL_SECONDS if ttl is None else int(ttl)
    expires_at = int(time.time()) + lifetime
    payload = {"u": username, "exp": expires_at, "n": secrets.token_hex(8)}
    payload_b64 = _b64e(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    return {
        "token": f"{payload_b64}.{_sign(payload_b64)}",
        "expires_in": lifetime,
        "expires_at": expires_at,
    }


def verify_admin_token(token: str) -> Optional[str]:
    """校验令牌，有效则返回其中的管理员账号，否则返回 None。

    任何异常（格式错、签名不符、过期、载荷非法）一律视为无效——
    失效关闭，不给"部分通过"的中间态。
    """
    if not token or not isinstance(token, str) or "." not in token:
        return None
    payload_b64, _, signature = token.rpartition(".")
    if not payload_b64 or not signature:
        return None

    try:
        expected = _sign(payload_b64)
        # 先比签名再看内容：签名不符时不必解析载荷。
        if not hmac.compare_digest(expected, signature):
            return None
        payload = json.loads(_b64d(payload_b64).decode("utf-8"))
    except Exception:
        return None

    if not isinstance(payload, dict):
        return None
    exp = payload.get("exp")
    if not isinstance(exp, int) or exp < int(time.time()):
        return None
    username = payload.get("u")
    return username if isinstance(username, str) and username else None


def check_admin_credentials(username: str, password: str) -> bool:
    """校验管理员账号口令（定时比较，避免计时侧信道）。"""
    user_ok = hmac.compare_digest(str(username or ""), ADMIN_USER)
    pass_ok = hmac.compare_digest(str(password or ""), ADMIN_PASS)
    # 两个比较都要执行完，不能短路——短路会让"账号对不对"从耗时上泄漏出去。
    return user_ok and pass_ok


# ---------------------------------------------------------------------------
# FastAPI 依赖
# ---------------------------------------------------------------------------

_TOKEN_HEADER = "authorization"
_FALLBACK_HEADER = "x-admin-token"


def _extract_tokens(request: Request) -> list[str]:
    """收集请求里所有**候选**令牌，交由调用方逐个验签。

    为什么不"取一个就返回"（这是实测踩到的坑）：
    沙箱边缘代理会给上游请求**注入**一个 `Authorization` 头（本机 `curl -v` 确认
    自己没发这个头，服务端却收到了）。若实现成"有 Authorization 就只看它"，
    那个注入的无效令牌会把调用方放在 `X-Admin-Token` 里的有效令牌整个屏蔽掉，
    表现为"刚登录成功、下一个请求就 401"。

    所以这里把所有来源都收上来，谁能验过就用谁：
      - `Authorization: Bearer <token>`（标准写法，可能被代理覆盖）
      - `X-Admin-Token: <token>`（自定义头，代理不会碰）
    """
    candidates: list[str] = []

    # 同名字段可能重复出现（代理再追加一个），逐个解析。
    for raw in request.headers.getlist(_TOKEN_HEADER):
        scheme, _, value = raw.partition(" ")
        if scheme.lower() == "bearer" and value.strip():
            candidates.append(value.strip())
        elif raw.strip():
            # 没带 Bearer 前缀的裸令牌也收下，宽容一点。
            candidates.append(raw.strip())

    for raw in request.headers.getlist(_FALLBACK_HEADER):
        if raw.strip():
            candidates.append(raw.strip())

    # 去重但保序
    seen: set[str] = set()
    return [t for t in candidates if not (t in seen or seen.add(t))]


async def require_admin(request: Request) -> str:
    """接口依赖：要求携带有效的管理员会话令牌，否则 401。

    返回值是管理员账号，便于在需要留痕的写接口里记录操作者。
    """
    tokens = _extract_tokens(request)
    for token in tokens:
        username = verify_admin_token(token)
        if username:
            return username

    # 一个都没有 ≠ 有但无效：分开报错，否则排查时会往"令牌过期"上找错方向。
    if not tokens:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="该操作需要管理员会话，请先在审批台完成管理员登录。",
            headers={"WWW-Authenticate": "Bearer"},
        )
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="管理员会话已失效，请重新登录。",
        headers={"WWW-Authenticate": "Bearer"},
    )
