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
- `APP_ENV`          运行环境；设为 `production` / `prod` 时启用生产策略（默认 development）
- `ADMIN_PASS`       管理员口令。生产环境必须设置且长度 >= 16，否则启动失败；
                     开发环境未设置则随机生成临时口令（重启失效，不打印明文）。
- `ADMIN_TOKEN_TTL`  令牌有效期秒数，默认 8 小时
- `SCHEDULER_USER`   排课人员账号，默认 `scheduler`
- `SCHEDULER_PASS`   排课人员口令。**未设置即不存在该角色**（默认关闭 = 系统等同纯 admin）；
                     设置后 `/scheduler-login` 才可用，签发 `role=scheduler` 的会话令牌。

角色（role）
------------
令牌载荷里的 `r` 字段表示角色：`admin`（全部能力）⊇ `scheduler`（排课写入能力）。
**旧令牌没有 `r` 字段**：一律按 `admin` 处理——这是向后兼容的地基，
改了签发逻辑也不会让既有会话突然 403。
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
# 策略分环境（借鉴 MARS-408 的 _init_admin 设计）：
#   生产（APP_ENV=production/prod）：未设置即 fail-fast；且强制长度 >= 16。
#   开发/本地：未设置则**随机生成**本次进程的临时口令（重启失效）。
#             这样本地开发不用配环境变量，同时任何环境都不会出现弱口令。
APP_ENV = (os.getenv("APP_ENV", "development") or "development").strip().lower()
_IS_PROD = APP_ENV in ("production", "prod")

# 公开的演示口令：出现即告警。仅用于本地演示，不可用于任何公网部署。
_DEMO_PASSWORDS = {"admin123", "admin", "password", "123456"}

_ADMIN_PASS_RAW = (os.getenv("ADMIN_PASS") or "").strip()

if _ADMIN_PASS_RAW:
    ADMIN_PASS = _ADMIN_PASS_RAW
    if _IS_PROD and len(ADMIN_PASS) < 16:
        raise RuntimeError(
            f"生产环境 ADMIN_PASS 长度必须 >= 16（当前 {len(ADMIN_PASS)}）。"
            "请注入足够强的管理员口令。"
        )
    if ADMIN_PASS in _DEMO_PASSWORDS:
        import warnings

        warnings.warn(
            "ADMIN_PASS 使用了公开的演示口令。仅限本地演示；公网部署必须更换。",
            RuntimeWarning,
            stacklevel=2,
        )
    ADMIN_PASS_IS_EPHEMERAL = False
elif _IS_PROD:
    raise RuntimeError(
        "生产环境必须设置环境变量 ADMIN_PASS（建议由密钥管理器注入强随机口令，长度 >= 16），"
        "拒绝以默认/随机口令启动。"
    )
else:
    # 开发环境：生成临时口令，绝不落盘、不打印明文。
    ADMIN_PASS = secrets.token_urlsafe(16)
    ADMIN_PASS_IS_EPHEMERAL = True
    import warnings

    warnings.warn(
        "ADMIN_PASS 未设置，本次启动使用随机生成的管理员口令（进程重启即失效，且不会打印）。"
        "需要固定口令时请设置 ADMIN_PASS。",
        RuntimeWarning,
        stacklevel=2,
    )

# 默认口令只是**演示**口令：DEMO_MODE=1 时可用，生产（未设 DEMO_MODE）一律拒绝。
# 起因（2026-10-26 实测）：线上 `POST /admin-login {"admin","admin123"}` 直接换到
# 管理员 token —— 任何拿到链接的人都能审批/落地/清清单。关掉默认口令比"换个更长的
# 口令"更硬：没有显式配置，就等于没有口令入口。
_BUILTIN_DEMO_PASS = "admin123"


def is_demo_mode() -> bool:
    return os.getenv("DEMO_MODE", "") == "1"


def admin_password_is_default() -> bool:
    return hmac_equal(ADMIN_PASS, _BUILTIN_DEMO_PASS)


def hmac_equal(a: str, b: str) -> bool:
    import hmac as _hmac
    return _hmac.compare_digest(str(a or "").encode(), str(b or "").encode())

try:
    TOKEN_TTL_SECONDS = int(os.getenv("ADMIN_TOKEN_TTL", str(8 * 3600)))
except ValueError:
    TOKEN_TTL_SECONDS = 8 * 3600


def describe_admin_password_state() -> str:
    """返回口令状态的一行摘要，供启动日志使用（绝不包含口令本身）。

    为什么需要它：ADMIN_PASS_IS_EPHEMERAL 只是一个内存布尔值，
    而"本次进程用的是固定口令还是随机口令"恰恰是运维最需要知道的——
    随机口令意味着重启后旧会话全部失效、管理员要重新登录。
    不写进日志，运维只能靠猜。
    """
    if ADMIN_PASS_IS_EPHEMERAL:
        return (
            f"ADMIN_PASS=ephemeral(随机生成, {len(ADMIN_PASS)} 字符, 重启即失效) "
            f"| APP_ENV={APP_ENV}"
        )
    weak = "weak-demo" if ADMIN_PASS in _DEMO_PASSWORDS else "fixed"
    return f"ADMIN_PASS={weak}({len(ADMIN_PASS)} 字符) | APP_ENV={APP_ENV}"


def log_admin_password_state(logger=None) -> str:
    """把口令状态写进日志并返回摘要。启动时调用一次即可。"""
    summary = describe_admin_password_state()
    if logger is not None:
        if ADMIN_PASS_IS_EPHEMERAL:
            logger.warning("[security] %s", summary)
        elif ADMIN_PASS in _DEMO_PASSWORDS:
            logger.warning("[security] %s —— 演示口令，不可用于公网", summary)
        else:
            logger.info("[security] %s", summary)
    return summary


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
# 角色（座谈要求第 3 步：指定排课人员、分配权限）
# ---------------------------------------------------------------------------
# 为什么要角色：审批台的"管理员口令"此前是**唯一**身份，教秘只想让人上传课表、
# 导入台账、采纳方案，也不得不把管理员口令交出去——而那个口令同时能清空工单表、
# 审批调课、撤销落地。职责混在一起的结果是"要么全给，要么谁都不给"。
#
# 权限包含关系刻意做成单向超集：`admin ⊇ scheduler`。
# 新增角色只**放宽**"谁能做排课类写操作"，一个既有管理员令牌的通行范围都不变，
# 所以这一步不改动任何现有调用方的行为（向后兼容的判据就在这里）。
ROLE_ADMIN = "admin"
ROLE_SCHEDULER = "scheduler"

# 排课人员能进的门口角色集合：管理员照旧在内（超集语义）。
SCHEDULER_ROLES = (ROLE_ADMIN, ROLE_SCHEDULER)

# 排课人员账号/口令。
#
# 与 ADMIN_PASS 的关键差别：**默认关闭**。
# SCHEDULER_PASS 未设置时这个角色根本不存在（`scheduler_enabled()` 为 False，
# `/scheduler-login` 直接 403），系统等同于纯 admin 形态——加了这套代码，
# 现网行为一个字节都没变，直到部署方显式注入口令。
# 反过来给个默认口令，就等于在公网上多开一个人人都知道账号名的入口。
SCHEDULER_USER_DEFAULT = "scheduler"


def scheduler_user() -> str:
    """排课人员账号名（`SCHEDULER_USER`，默认 `scheduler`）。"""
    return (os.getenv("SCHEDULER_USER") or "").strip() or SCHEDULER_USER_DEFAULT


def scheduler_password() -> str:
    """排课人员口令（`SCHEDULER_PASS`；未配置则为空串 = 角色不存在）。

    刻意在**调用时**读环境变量，而不是模块载入时冻结成常量：
    1. 与 `PUBLIC_READONLY` 开关同一取向（见 api/reschedule.py 的 `export_gate`）
       ——请求时读，测试才能用 monkeypatch 构造"已启用/未启用"两种形态；
    2. 载入时冻结会让 `import app.security` 早于任何环境准备的调用方拿到空值，
       那种"看起来配了却不生效"的坑排查成本极高。
    """
    return (os.getenv("SCHEDULER_PASS") or "").strip()


def scheduler_enabled() -> bool:
    """排课人员角色是否可用：只有显式配置了 SCHEDULER_PASS 才为真。"""
    return bool(scheduler_password())


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


def issue_admin_token(username: str, ttl: Optional[int] = None,
                      role: str = ROLE_ADMIN) -> dict:
    """签发会话令牌（默认角色 `admin`）。

    返回 `{"token": ..., "expires_in": ..., "expires_at": ...}`，
    其中 `expires_at` 为 Unix 秒时间戳，便于前端做倒计时。

    `role` 是后加的第三个参数，**默认值 "admin" 保证既有调用点
    （`issue_admin_token(u)` / `issue_admin_token(u, ttl=...)`）逐字不变**：
    不传角色就是管理员令牌，行为与加角色之前完全一致。
    """
    lifetime = TOKEN_TTL_SECONDS if ttl is None else int(ttl)
    expires_at = int(time.time()) + lifetime
    payload = {"u": username, "exp": expires_at, "n": secrets.token_hex(8), "r": role}
    payload_b64 = _b64e(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    return {
        "token": f"{payload_b64}.{_sign(payload_b64)}",
        "expires_in": lifetime,
        "expires_at": expires_at,
    }


def issue_scheduler_token(username: str, ttl: Optional[int] = None) -> dict:
    """签发排课人员令牌（`role="scheduler"`）。

    只是 `issue_admin_token(..., role="scheduler")` 的语义别名：
    签发/签名/有效期三条逻辑共用一份，避免"两份实现漂移"这类安全审计常见病。
    """
    return issue_admin_token(username, ttl=ttl, role=ROLE_SCHEDULER)


def _verify_identity(token: str) -> Optional[dict]:
    """校验令牌，有效则返回 `{"u": 账号, "r": 角色}`，否则 None。

    这是**唯一的验签入口**：`verify_admin_token` / `require_admin` /
    `require_scheduler` 都走这里，签名与过期校验只有一份实现。

    向后兼容：载荷里没有 `r` 字段（角色机制之前签发的旧令牌）时按 `admin` 处理。
    不做这个默认，加角色的那一刻所有在线会话都会掉进 403。

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
    if not isinstance(username, str) or not username:
        return None
    role = payload.get("r")
    if not isinstance(role, str) or not role:
        # 旧令牌（无 r）= 管理员。见上面 docstring 的向后兼容说明。
        role = ROLE_ADMIN
    return {"u": username, "r": role}


def verify_admin_token(token: str) -> Optional[str]:
    """校验令牌，有效则返回其中的账号，否则返回 None。

    任何异常（格式错、签名不符、过期、载荷非法）一律视为无效——
    失效关闭，不给"部分通过"的中间态。

    注意：**本函数只验"是不是本服务签发的有效会话"，不判角色**（角色门禁在
    `require_admin` / `require_scheduler` 里）。保留原签名与返回类型，
    它仍是既有测试与调用点依赖的公开契约。
    """
    ident = _verify_identity(token)
    return ident["u"] if ident else None


def check_admin_credentials(username: str, password: str) -> bool:
    """校验管理员账号口令（定时比较，避免计时侧信道）。

    ⚠️ 默认演示口令只在 DEMO_MODE=1 下有效；生产必须显式配置 ADMIN_PASS。
    """
    if admin_password_is_default() and not is_demo_mode():
        return False
    user_ok = hmac.compare_digest(str(username or ""), ADMIN_USER)
    pass_ok = hmac.compare_digest(str(password or ""), ADMIN_PASS)
    # 两个比较都要执行完，不能短路——短路会让"账号对不对"从耗时上泄漏出去。
    return user_ok and pass_ok


def check_scheduler_credentials(username: str, password: str) -> bool:
    """校验排课人员账号口令（定时比较，避免计时侧信道）。

    **默认关闭**：`SCHEDULER_PASS` 未配置时恒为 False —— 角色不存在，
    不引入任何新的可登录面（这与"给个默认口令"是两种安全结论）。

    不短路：账号比较与口令比较都执行完再合并，否则"账号是否存在"会从耗时上
    泄漏给爆破者（与 `check_admin_credentials` 同一理由）。
    """
    secret = scheduler_password()
    if not secret:
        return False
    # 先 encode 再比：`compare_digest` 对含非 ASCII 的 str 会抛 TypeError，
    # 那会让"输错账号名"变成 500 而不是 401（管理员那条路径沿用原样，不动它）。
    user_ok = hmac.compare_digest(str(username or "").encode("utf-8"),
                                  scheduler_user().encode("utf-8"))
    pass_ok = hmac.compare_digest(str(password or "").encode("utf-8"),
                                  secret.encode("utf-8"))
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


async def _require_roles(request: Request, allowed: tuple, *, missing_detail: str,
                         invalid_detail: str, forbidden_detail: str) -> str:
    """角色门禁的唯一实现：在候选令牌里找一个"签名有效 + 角色在 allowed 内"的身份。

    为什么要收成一个内部函数，而不是给每个角色复制一遍循环：
    "候选令牌逐个试"这件事本身是个安全实现（边缘代理会注入垃圾 Authorization，
    见 `_extract_tokens`），两份副本迟早漂移成两种行为。
    角色差异只体现在 `allowed` 集合与三段文案上。

    三种失败态分开报错（原样保留加固时的设计）：
      - 一个候选都没有 → 401 `missing_detail`（"还没登录"）
      - 有候选但全部验不过 → 401 `invalid_detail`（"令牌过期/伪造"）
      - 有令牌验过了、但角色不在 allowed 内 → 403 `forbidden_detail`（"越权"）
        401 与 403 的区分有意义：前者提示重新登录，后者重新登录一万次也没用，
        该找管理员开权限。
    """
    tokens = _extract_tokens(request)
    role_mismatch = False
    for token in tokens:
        ident = _verify_identity(token)
        if not ident:
            continue
        if ident["r"] in allowed:
            return ident["u"]
        role_mismatch = True

    if not tokens:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=missing_detail,
            headers={"WWW-Authenticate": "Bearer"},
        )
    if role_mismatch:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=forbidden_detail,
            headers={"WWW-Authenticate": "Bearer"},
        )
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=invalid_detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


async def require_admin(request: Request) -> str:
    """接口依赖：要求携带**角色为 admin** 的有效会话令牌，否则 401 / 越权 403。

    返回值是管理员账号，便于在需要留痕的写接口里记录操作者。

    向后兼容：角色机制之前签发的旧令牌没有 `r` 字段，`_verify_identity` 按
    `admin` 处理，所以既有管理员会话一个都不会被这一步挡掉。
    唯一新增的拒绝是"令牌有效但角色是排课人员"——那是本次要实现的隔离本身。
    """
    return await _require_roles(
        request,
        (ROLE_ADMIN,),
        missing_detail="该操作需要管理员会话，请先在审批台完成管理员登录。",
        invalid_detail="管理员会话已失效，请重新登录。",
        forbidden_detail="权限不足：该操作需要管理员权限（需要管理员会话），排课人员账号无权执行，请联系管理员。",
    )


async def require_scheduler(request: Request) -> str:
    """接口依赖：要求携带管理员**或排课人员**的有效会话令牌。

    用于"排课类写操作"（上传课表、导入台账、初始化主数据、采纳落地方案）：
    这些是排课人员日常工作要做的，但不必给到管理员级破坏力
    （清空工单、审批调课、撤销落地、删实体仍只认 admin）。

    `admin ⊇ scheduler`：管理员令牌在这里照旧放行，所以把某端点从
    `require_admin` 降挂到 `require_scheduler` **不会改变任何管理员的行为**，
    只是多授予了排课人员。
    """
    return await _require_roles(
        request,
        SCHEDULER_ROLES,
        missing_detail="该操作需要管理员或排课人员会话，请先登录。",
        invalid_detail="管理员/排课人员会话已失效，请重新登录。",
        forbidden_detail="权限不足：该操作需要管理员或排课人员会话，当前令牌的角色不被授予。",
    )
