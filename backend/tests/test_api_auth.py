"""接口门禁回归测试（2026-10-15 加固）。

锁定加固后的鉴权边界，防止后续改动把口子重新开开：

  - 写接口 / 涉 PII 接口：无令牌 → 401，伪造令牌 → 401，有效令牌 → 放行
  - 演示主路径的只读接口：保持免鉴权（这是教秘演示的核心，不能被顺手锁掉）
  - /admin-login：口令错误 → 401；正确 → 签发可用的服务端会话令牌
  - 上传文件名净化：`../` 与 `..\\` 都不能逃出 data 目录
  - DISABLE_DOCS=1 时不泄漏 OpenAPI schema
"""

import importlib
import os
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.api.reschedule import (
    _DATA_DIR,
    _EXCEL_SUFFIXES,
    _STUDENT_SUFFIXES,
    _safe_upload_name,
    _upload_target,
)
from app.main import app
from app.security import issue_admin_token, verify_admin_token

client = TestClient(app)

ADMIN = {"username": "admin", "password": "admin123"}


def _admin_headers() -> dict:
    return {"Authorization": f"Bearer {issue_admin_token('admin')['token']}"}


# ---------------------------------------------------------------------------
# 1. 受保护接口：无令牌必须 401
# ---------------------------------------------------------------------------

# (method, path, kwargs) —— 带合法请求体，确保被拒原因是"缺鉴权"而不是"参数不合法"
_PROTECTED = [
    ("POST", "/api/reschedule/upload", {"files": {"file": ("a.xlsx", b"x", "application/vnd.ms-excel")}}),
    ("POST", "/api/reschedule/upload-student", {"files": {"file": ("a.xlsx", b"x", "application/vnd.ms-excel")}}),
    ("GET", "/api/reschedule/student-status", {}),
    ("POST", "/api/reschedule/requests", {"json": {
        "teacher": "t", "course_name": "c", "class_name": "k",
        "original_time": "周一 567", "target_week": 3, "reason": "r",
    }}),
    ("GET", "/api/reschedule/requests", {}),
    ("DELETE", "/api/reschedule/requests", {}),
    ("POST", "/api/reschedule/requests/1/approve", {"json": {"approve": True, "comment": "ok"}}),
]


@pytest.mark.parametrize("method,path,kwargs", _PROTECTED)
def test_protected_endpoint_requires_token(method, path, kwargs):
    r = client.request(method, path, **kwargs)
    assert r.status_code == 401, f"{method} {path} 竟然无需令牌（{r.status_code}）"
    assert "管理员" in r.json().get("detail", "")


@pytest.mark.parametrize("method,path,kwargs", _PROTECTED)
def test_protected_endpoint_rejects_forged_token(method, path, kwargs):
    """签名不符的令牌必须被拒——否则门禁只是摆设。"""
    bad = {"Authorization": "Bearer " + issue_admin_token("admin")["token"][:-4] + "AAAA"}
    r = client.request(method, path, headers=bad, **kwargs)
    assert r.status_code == 401


def test_tampered_payload_rejected():
    """改载荷保签名：签名校验必须先于内容解析，否则可伪造任意账号。"""
    token = issue_admin_token("admin")["token"]
    payload_b64, _, sig = token.rpartition(".")
    forged = f"{payload_b64}.{sig}"[:-1] + ("A" if sig[-1] != "A" else "B")
    assert client.get("/api/reschedule/requests", headers={"Authorization": f"Bearer {forged}"}).status_code == 401


def test_expired_token_rejected():
    expired = issue_admin_token("admin", ttl=-1)["token"]
    assert verify_admin_token(expired) is None
    assert client.get("/api/reschedule/requests", headers={"Authorization": f"Bearer {expired}"}).status_code == 401


# ---------------------------------------------------------------------------
# 2. 演示主路径必须保持免鉴权
# ---------------------------------------------------------------------------

PUBLIC_READS = [
    "/api/reschedule/status",
    "/api/reschedule/teachers",
]


@pytest.mark.parametrize("path", PUBLIC_READS)
def test_demo_read_paths_stay_open(path):
    """这些是教秘演示的主路径，加固不能把它们一起锁掉。

    不断言 200：`/teachers` 之类需要先 `load-default`，未加载时会回 400
    （"请先加载实验安排表"）——那是业务提示，不是鉴权拦截。
    这里要锁的是"**没有**被 401 挡住"。
    """
    r = client.get(path)
    assert r.status_code != 401, f"{path} 被门禁误伤"
    assert "application/json" in r.headers["content-type"]


def test_status_is_200_without_token():
    r = client.get("/api/reschedule/status")
    assert r.status_code == 200
    assert "loaded" in r.json()


def test_health_stays_open():
    assert client.get("/health").json() == {"status": "ok"}


# ---------------------------------------------------------------------------
# 3. 管理员登录
# ---------------------------------------------------------------------------


def test_admin_login_rejects_wrong_password():
    r = client.post("/api/reschedule/admin-login", json={"username": "admin", "password": "wrong"})
    assert r.status_code == 401


def test_admin_login_rejects_wrong_username():
    r = client.post("/api/reschedule/admin-login", json={"username": "root", "password": "admin123"})
    assert r.status_code == 401


def test_admin_login_issues_working_token():
    r = client.post("/api/reschedule/admin-login", json=ADMIN)
    assert r.status_code == 200
    body = r.json()
    # 只回一句 success 是不够的：必须有真令牌，否则等于没有会话
    assert body["token"]
    assert body["role"] == "admin"
    assert body["expires_at"] > 0

    headers = {"Authorization": f"Bearer {body['token']}"}
    assert client.get("/api/reschedule/requests", headers=headers).status_code == 200
    assert client.get("/api/reschedule/student-status", headers=headers).status_code == 200


def test_token_accepts_x_admin_token_header():
    token = issue_admin_token("admin")["token"]
    assert client.get("/api/reschedule/requests", headers={"X-Admin-Token": token}).status_code == 200


def test_valid_fallback_header_survives_injected_authorization():
    """回归：边缘代理注入的无效 Authorization 不得屏蔽有效令牌。

    实测现场——本机 curl -v 确认自己没发 Authorization，服务端却收到了一个；
    当时实现是"有 Authorization 就只看它"，导致登录成功后每个请求都 401。
    现在要求：任一头验过即放行。
    """
    token = issue_admin_token("admin")["token"]
    r = client.get(
        "/api/reschedule/requests",
        headers={"Authorization": "Bearer injected-by-proxy", "X-Admin-Token": token},
    )
    assert r.status_code == 200


def test_valid_authorization_survives_garbage_fallback_header():
    """反向也要成立：先验 Authorization，不要让垃圾 X-Admin-Token 抢先短路。"""
    token = issue_admin_token("admin")["token"]
    r = client.get(
        "/api/reschedule/requests",
        headers={"Authorization": f"Bearer {token}", "X-Admin-Token": "garbage"},
    )
    assert r.status_code == 200


def test_duplicate_authorization_values_are_all_considered():
    """代理追加同名头时应逐个尝试，而不是只看第一个。"""
    token = issue_admin_token("admin")["token"]
    r = client.get(
        "/api/reschedule/requests",
        headers=[("Authorization", "Bearer injected-by-proxy"), ("Authorization", f"Bearer {token}")],
    )
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# 4. 上传文件名净化
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "evil",
    [
        "../../../schedule.demo.xlsx",
        "..\\..\\..\\schedule.xlsx",
        "/etc/passwd.xlsx",
        "....//....//x.xlsx",
        "..%2f..%2fx.xlsx",
        "a/../../b.xlsx",
    ],
)
def test_upload_name_neutralised(evil):
    """穿越尝试必须被压成 data 目录内的单个文件名。"""
    name = _safe_upload_name(evil, _EXCEL_SUFFIXES)
    assert "/" not in name and "\\" not in name
    assert ".." not in name
    assert name.endswith(".xlsx") or name.endswith(".xls")

    target = _upload_target(evil, _EXCEL_SUFFIXES)
    # 落盘路径必须真的在 data 目录里，而不是只靠字符串看起来安全
    assert os.path.dirname(os.path.abspath(target)) == os.path.abspath(_DATA_DIR)


def test_upload_name_keeps_chinese_stem():
    """学生姓名来自文件名主干，不能被洗成乱码或空串。"""
    name = _safe_upload_name("张敏杰课表.pdf", _STUDENT_SUFFIXES, prefix="student_")
    assert name == "student_张敏杰课表.pdf"


@pytest.mark.parametrize("bad", ["evil.exe", "x.xlsx.exe", "noext", ""])
def test_upload_suffix_whitelist(bad):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as e:
        _safe_upload_name(bad, _EXCEL_SUFFIXES)
    assert e.value.status_code == 400


def test_upload_rejects_pdf_for_schedule_table():
    """/upload 只收 Excel；.pdf 是学生课表专用的，不能因为共用净化函数就放开。"""
    from fastapi import HTTPException

    with pytest.raises(HTTPException):
        _safe_upload_name("x.pdf", _EXCEL_SUFFIXES)


# ---------------------------------------------------------------------------
# 5. 发布态关闭 OpenAPI
# ---------------------------------------------------------------------------


def test_disable_docs_hides_openapi_schema(monkeypatch):
    """DISABLE_DOCS=1 时不得吐出接口清单。

    注意断言的是"没有泄漏 schema"，而不是"404"：关闭文档后
    `/openapi.json` 会落到 SPA 兜底路由，返回 index.html（200）。
    """
    monkeypatch.setenv("DISABLE_DOCS", "1")
    import app.main as main_mod

    try:
        reloaded = importlib.reload(main_mod)
        c = TestClient(reloaded.app)
        for path in ("/openapi.json", "/docs", "/redoc"):
            r = c.get(path)
            assert '"openapi"' not in r.text, f"{path} 仍泄漏接口清单"
            assert "swagger" not in r.text.lower(), f"{path} 仍是 Swagger 页面"
    finally:
        monkeypatch.delenv("DISABLE_DOCS", raising=False)
        importlib.reload(main_mod)


def test_openapi_present_when_docs_enabled():
    """本地开发默认保留文档，避免为了发布把开发体验一起砍掉。"""
    if os.getenv("DISABLE_DOCS"):
        pytest.skip("当前进程已置位 DISABLE_DOCS")
    r = client.get("/openapi.json")
    assert r.status_code == 200
    assert r.json()["openapi"].startswith("3.")
