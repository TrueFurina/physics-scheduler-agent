"""令牌验签的**失效关闭**契约（R-101）。

为什么单独一个文件
------------------
`_verify_identity` 是全项目唯一的验签入口（`verify_admin_token` / `require_admin` /
`require_scheduler` 都走它），它的 docstring 写着"任何异常一律视为无效——失效关闭，
不给部分通过的中间态"。

但这条契约此前**没有任何单元级锁**：`test_scheduler_role.py` 测的是端点上的
401/403（好，但走的是 `_require_roles` 那条链），覆盖率实测 `security.py` 有 22 行
从未被执行 —— 正是这些"坏令牌"分支（空载荷/坏 base64/载荷非 dict/exp 非法）。

风险不在今天的行为（实测 13 种坏输入全部返回 None），而在**下次重构**：
把 `return None` 写成 `return payload`、或把签名比较换成 `in`，端点测试未必抓得到。
所以这里按"契约"而不是"实现"来锁：输入 → 必须 None，一个都不许放行。
"""

import json
import time

import pytest
from starlette.requests import Request

from app import security as S


def _token(payload, *, sign=True) -> str:
    """按真实格式造一个令牌：base64(payload) + '.' + 签名。"""
    body = S._b64e(json.dumps(payload).encode("utf-8"))
    return f"{body}.{S._sign(body) if sign else 'deadbeef'}"


def _request(headers) -> Request:
    raw = [(k.lower().encode(), v.encode()) for k, v in headers]
    return Request({"type": "http", "headers": raw})


NOW = int(time.time())

# (用例名, 令牌)
BAD_TOKENS = [
    ("空串", ""),
    ("None 之外的假值", "   "),
    ("没有分隔点", "abcdef"),
    ("缺载荷", ".signature"),
    ("缺签名", "payload."),
    ("签名不符", _token({"u": "admin", "exp": NOW + 600}, sign=False)),
    ("载荷不是 JSON", None),          # 下面用 _b64e 现造，见 test 里替换
    ("载荷是 list", _token([1, 2, 3])),
    ("exp 是字符串", _token({"u": "admin", "exp": "9999999999"})),
    ("exp 已过期", _token({"u": "admin", "exp": NOW - 10})),
    ("没有用户名", _token({"exp": NOW + 600})),
    ("用户名空串", _token({"u": "", "exp": NOW + 600})),
    ("用户名不是字符串", _token({"u": 123, "exp": NOW + 600})),
]


@pytest.mark.parametrize("name,token", BAD_TOKENS, ids=[n for n, _ in BAD_TOKENS])
def test_bad_tokens_are_rejected(name, token):
    """任何坏输入都必须返回 None —— 不许出现"部分通过"。"""
    if token is None:  # 载荷不是 JSON 的那条
        body = S._b64e(b"not-json")
        token = f"{body}.{S._sign(body)}"
    assert S._verify_identity(token) is None, f"{name} 竟然通过了验签"


def test_non_string_token_rejected():
    """非字符串（比如直接传 bytes/None）也必须拒绝，不能靠 AttributeError 兜底。"""
    assert S._verify_identity(None) is None
    assert S._verify_identity(b"abc.def") is None


def test_valid_admin_token_roundtrip():
    tok = S.issue_admin_token("admin", ttl=600)["token"]
    ident = S._verify_identity(tok)
    assert ident == {"u": "admin", "r": S.ROLE_ADMIN}


def test_legacy_token_without_role_defaults_to_admin():
    """角色机制之前签发的旧令牌（载荷里没有 r）必须仍然按管理员放行。

    不做这个默认，加角色的那一刻所有在线会话都会掉进 403 —— 这是刻意的向后兼容，
    不是漏洞：签名仍然要过。
    """
    tok = _token({"u": "admin", "exp": NOW + 600})
    assert S._verify_identity(tok) == {"u": "admin", "r": S.ROLE_ADMIN}


def test_scheduler_token_carries_scheduler_role():
    tok = S.issue_scheduler_token("排课员", ttl=600)["token"]
    assert S._verify_identity(tok) == {"u": "排课员", "r": S.ROLE_SCHEDULER}


def test_token_with_valid_signature_but_unknown_role_passes_verification():
    """验签层**不做**角色白名单：签名有效就是有效身份，越权由 `_require_roles` 判 403。

    这一条是防止"把角色检查偷偷塞进验签"——那样 401 与 403 就分不开了，
    而两者的处置完全不同（重登 vs 找管理员开权限）。
    """
    tok = _token({"u": "admin", "exp": NOW + 600, "r": "some-new-role"})
    assert S._verify_identity(tok) == {"u": "admin", "r": "some-new-role"}


# ---------------------------------------------------------------- 头部解析

def test_bearer_prefix_is_stripped():
    tokens = S._extract_tokens(_request([("Authorization", "Bearer abc.def")]))
    assert tokens == ["abc.def"]


def test_bare_token_without_scheme_is_also_accepted():
    """边缘代理会把 Authorization 改写成裸令牌（线上实测），不带 Bearer 也要收下。"""
    tokens = S._extract_tokens(_request([("Authorization", "abc.def")]))
    assert tokens == ["abc.def"]


def test_duplicate_headers_are_all_collected_and_deduped():
    """同名头可能被代理追加一份；两个都收，重复的去掉（保序）。"""
    tokens = S._extract_tokens(_request([
        ("Authorization", "Bearer a.b"),
        ("Authorization", "Bearer c.d"),
        ("Authorization", "Bearer a.b"),
    ]))
    assert tokens == ["a.b", "c.d"]


def test_fallback_header_is_appended_after_authorization():
    """`X-Admin-Token` 是代理不会碰的自定义头 —— 线上靠它，必须排在标准头之后。"""
    tokens = S._extract_tokens(_request([
        ("X-Admin-Token", "fallback.tok"),
        ("Authorization", "Bearer primary.tok"),
    ]))
    assert tokens == ["primary.tok", "fallback.tok"]


def test_no_headers_yields_empty_candidate_list():
    """一个候选都没有时返回空列表 —— 上层据此报 401「还没登录」而不是「令牌无效」。"""
    assert S._extract_tokens(_request([])) == []
