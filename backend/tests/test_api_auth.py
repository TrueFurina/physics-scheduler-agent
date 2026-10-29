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

# 口令不设默认值（fail-closed），测试必须在导入 app.* 之前注入。
# 这里用显式演示值，与 start.bat 保持一致。
os.environ.setdefault("ADMIN_PASS", "admin123")
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.api.reschedule import (
    _DATA_DIR,
    _EXCEL_SUFFIXES,
    _safe_upload_name,
    _upload_target,
)
from app.main import app
from app.security import issue_admin_token, verify_admin_token

client = TestClient(app)

ADMIN = {"username": "admin", "password": os.environ["ADMIN_PASS"]}


def _admin_headers() -> dict:
    return {"Authorization": f"Bearer {issue_admin_token('admin')['token']}"}


# ---------------------------------------------------------------------------
# 1. 受保护接口：无令牌必须 401
# ---------------------------------------------------------------------------

# (method, path, kwargs) —— 带合法请求体，确保被拒原因是"缺鉴权"而不是"参数不合法"
_PROTECTED = [
    ("POST", "/api/reschedule/upload", {"files": {"file": ("a.xlsx", b"x", "application/vnd.ms-excel")}}),
    ("POST", "/api/reschedule/requests", {"json": {
        "teacher": "t", "course_name": "c", "class_name": "k",
        "original_time": "周一 567", "target_week": 3, "reason": "r",
    }}),
    ("GET", "/api/reschedule/requests", {}),
    ("DELETE", "/api/reschedule/requests", {}),
    ("POST", "/api/reschedule/requests/1/approve", {"json": {"approve": True, "comment": "ok"}}),
    # 方案落地：写真实排课数据，与 /upload、/apply-move 同门禁
    ("POST", "/api/reschedule/parallel/land", {"json": {"schedule": []}}),
    ("POST", "/api/reschedule/parallel/land/undo", {}),
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
    """探活必须免鉴权（且不因数据未加载而失败），深度字段只是附加信息。"""
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert "data" in body


# ---------------------------------------------------------------------------
# 2c. 整表导出端点的 PUBLIC_READONLY 一键收紧（2026-10-24 交付评审 P1-2）
#
# 这 6 个端点默认免鉴权（教秘演示主路径，已文档化为客户接受的风险），
# 但匿名即可整表拉走 433 条真实排课。开关把"接受风险"变成显式动作：
# PUBLIC_READONLY=0 时它们要求管理员会话，其余演示端点不受影响。
# ---------------------------------------------------------------------------

# (method, path, body)：四个导出 + 落地预览 + 自动排课初稿导出
_FULL_TABLE_EXPORTS = [
    ("GET", "/api/reschedule/export/class-schedule", None),
    ("GET", "/api/reschedule/export/room-schedule", None),
    ("GET", "/api/reschedule/export/master-workbook", None),
    ("GET", "/api/reschedule/export/master-data", None),
    ("POST", "/api/reschedule/parallel/land/preview", {"schedule": []}),
    ("POST", "/api/reschedule/auto-schedule/export", {"schedule": []}),
]


@pytest.mark.parametrize("method,path,body", _FULL_TABLE_EXPORTS)
def test_full_table_exports_stay_open_by_default(method, path, body):
    """默认形态（PUBLIC_READONLY 缺省）：不被 401 挡住——演示主路径不变。"""
    r = client.request(method, path, json=body)
    assert r.status_code != 401, f"{path} 默认形态就被门禁挡住（开关Regression）"


@pytest.mark.parametrize("method,path,body", _FULL_TABLE_EXPORTS)
def test_full_table_exports_require_admin_when_tightened(method, path, body, monkeypatch):
    """PUBLIC_READONLY=0：匿名 401（门禁先于请求体校验）；带管理员令牌放行。"""
    monkeypatch.setenv("PUBLIC_READONLY", "0")

    anon = client.request(method, path, json=body)
    assert anon.status_code == 401, f"{path} 收紧后匿名竟可访问（{anon.status_code}）"
    assert "管理员" in anon.json().get("detail", "")

    authed = client.request(method, path, json=body, headers=_admin_headers())
    assert authed.status_code != 401, f"{path} 收紧后管理员也被挡住"


def test_tightening_does_not_touch_other_demo_paths(monkeypatch):
    """收紧只影响那 6 个整表端点；/status、/teachers、/stats 等主路径照旧。"""
    monkeypatch.setenv("PUBLIC_READONLY", "0")
    for path in ("/api/reschedule/status", "/api/reschedule/stats",
                 "/api/reschedule/data-audit"):
        r = client.get(path)
        assert r.status_code != 401, f"{path} 被导出开关误伤"


# ---------------------------------------------------------------------------
# 3. 管理员登录
# ---------------------------------------------------------------------------


def test_admin_login_rejects_wrong_password():
    r = client.post("/api/reschedule/admin-login", json={"username": "admin", "password": "wrong"})
    assert r.status_code == 401


def test_admin_login_rejects_wrong_username():
    r = client.post("/api/reschedule/admin-login", json={"username": "root", "password": os.environ["ADMIN_PASS"]})
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


@pytest.mark.parametrize("bad", ["evil.exe", "x.xlsx.exe", "noext", ""])
def test_upload_suffix_whitelist(bad):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as e:
        _safe_upload_name(bad, _EXCEL_SUFFIXES)
    assert e.value.status_code == 400


def test_upload_rejects_pdf_for_schedule_table():
    """/upload 只收 Excel；.pdf 不在白名单内，共用净化函数也不会放开。"""
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


# ---------------------------------------------------------------------------
# 9. 口令策略（分环境）
# ---------------------------------------------------------------------------
# 为什么不靠"测试能跑通"来证明：本文件顶部用 os.environ.setdefault 注入演示值，
# 这会让"未配置"这一场景在测试中永远不出现。
# 所以这里用子进程显式构造各种环境，直接验证 security 模块的加载行为。
#
# 策略（与 app/security.py 保持一致）：
#   生产 + 未设置      -> RuntimeError（fail-closed）
#   生产 + 口令 < 16   -> RuntimeError（强度不足）
#   开发 + 未设置      -> 载入成功，但口令为随机生成的临时值（非弱口令、非空）
#   开发 + 演示口令    -> 载入成功 + 告警
#
# 这些是本次加固的核心安全属性，必须被锁定：
# 哪天有人把弱默认值加回来，"开发 + 未设置"的用例会因为拿到 admin123 而失败。


def _run_security_in_subprocess(extra_env: dict) -> str:
    """在干净子进程里加载 app.security，返回 stdout。"""
    import subprocess
    import textwrap

    backend_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    code = textwrap.dedent(
        """
        import os, sys, warnings
        sys.path.insert(0, ".")
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            try:
                import app.security as s
            except RuntimeError as exc:
                print("REFUSED|" + str(exc).replace(chr(10), " ")[:120])
                raise SystemExit(0)
        warned = any("演示口令" in str(w.message) for w in caught)
        print("LOADED|ephemeral=%s|len=%d|demo_warned=%s|pass_eq_weak=%s" % (
            s.ADMIN_PASS_IS_EPHEMERAL, len(s.ADMIN_PASS), warned,
            s.ADMIN_PASS in {"admin123", "admin", "password", "123456"}))
        """
    )
    env = {k: v for k, v in os.environ.items()
           if k not in ("ADMIN_PASS", "APP_ENV")}
    env.update(extra_env)
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=backend_root, env=env, capture_output=True, text=True,
    )
    return proc.stdout.strip()


def test_production_without_admin_pass_refuses_to_start():
    """生产环境未设置口令必须拒绝启动。"""
    out = _run_security_in_subprocess({"APP_ENV": "production"})
    assert out.startswith("REFUSED"), (
        f"生产环境未设置 ADMIN_PASS 竟然启动成功——fail-closed 失效。out={out!r}"
    )
    assert "ADMIN_PASS" in out


def test_production_rejects_short_admin_pass():
    """生产环境口令长度不足 16 必须拒绝启动。"""
    out = _run_security_in_subprocess(
        {"APP_ENV": "production", "ADMIN_PASS": "short123"}
    )
    assert out.startswith("REFUSED"), (
        f"生产环境短口令竟然通过——强度校验失效。out={out!r}"
    )
    assert "16" in out


def test_development_without_admin_pass_uses_random_not_weak():
    """开发环境未设置口令：必须载入成功，但口令是随机临时值，绝不能是弱口令。"""
    out = _run_security_in_subprocess({})
    assert out.startswith("LOADED"), (
        f"开发环境未设置 ADMIN_PASS 竟然启动失败——本地开发应能零配置运行。out={out!r}"
    )
    assert "ephemeral=True" in out, f"未标记为临时口令。out={out!r}"
    assert "pass_eq_weak=False" in out, (
        f"开发环境回落到了弱口令——这正是本次要消除的行为。out={out!r}"
    )
    # 随机口令长度应显著大于弱口令
    assert "len=8" not in out, f"口令长度异常。out={out!r}"


def test_demo_password_emits_warning():
    """使用公开演示口令时必须告警（不拦截，但要让线上日志看得见）。"""
    import subprocess
    import textwrap

    backend_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    code = textwrap.dedent(
        """
        import os, sys, warnings
        os.environ["ADMIN_PASS"] = "admin123"
        sys.path.insert(0, ".")
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            import app.security  # noqa: F401
        hit = [w for w in caught if "演示口令" in str(w.message)]
        print("WARNED" if hit else "SILENT")
        """
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=backend_root,
        env=dict(os.environ),
        capture_output=True,
        text=True,
    )
    assert "WARNED" in proc.stdout, (
        f"演示口令未触发告警。stdout={proc.stdout!r} stderr={proc.stderr[-400:]!r}"
    )


# ---------------------------------------------------------------------------
# 10. 启动日志必须暴露口令状态（但不泄露口令本身）
# ---------------------------------------------------------------------------
# 为什么：ADMIN_PASS_IS_EPHEMERAL 只是内存里的布尔值，不写日志的话，
# 运维无法从启动输出判断"本次用的是固定口令还是随机口令"。
# 随机口令意味着重启后旧会话全部失效——这是排障的第一信息。
#
# 同时必须锁定：日志里绝不能出现口令明文。


def _startup_log(extra_env: dict) -> str:
    """在子进程里跑一次 lifespan 启动，返回 [security] 相关的日志行。"""
    import subprocess
    import textwrap

    backend_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    code = textwrap.dedent(
        """
        import os, sys, logging, asyncio, warnings
        sys.path.insert(0, ".")
        warnings.simplefilter("ignore")
        logging.basicConfig(level=logging.INFO, format="%(levelname)s|%(message)s")
        from app.main import app, lifespan
        async def boot():
            async with lifespan(app):
                pass
        asyncio.run(boot())
        """
    )
    env = {k: v for k, v in os.environ.items()
           if k not in ("ADMIN_PASS", "APP_ENV")}
    env.update(extra_env)
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=backend_root, env=env, capture_output=True, text=True,
    )
    lines = [l for l in (proc.stdout + proc.stderr).splitlines() if "[security]" in l]
    return "\n".join(lines)


def test_startup_logs_ephemeral_state_in_dev():
    log = _startup_log({})
    assert "ephemeral" in log, f"开发环境未标记为随机口令。log={log!r}"
    assert "ADMIN_PASS=ephemeral" in log


def test_startup_logs_fixed_state_in_production():
    secret = "Xk9#mQ2!vL7pR4sT"
    log = _startup_log({"APP_ENV": "production", "ADMIN_PASS": secret})
    assert "ADMIN_PASS=fixed" in log, f"生产环境未标记为固定口令。log={log!r}"
    # 关键：日志里绝不能出现口令明文
    assert secret not in log, f"启动日志泄露了口令明文！log={log!r}"


def test_startup_logs_weak_demo_password():
    log = _startup_log({"ADMIN_PASS": "admin123"})
    assert "weak-demo" in log, f"演示口令未被标记。log={log!r}"
    assert "admin123" not in log, f"启动日志泄露了口令明文！log={log!r}"


# ---------------------------------------------------------------------------
# 11. 上传体积上限（防内存耗尽 DoS）
# ---------------------------------------------------------------------------
# 背景：5 个上传端点原本都是 `await file.read()` 一次性读全量。
# 超大文件会先把内存顶爆，然后才在解析阶段报"不是 zip"——
# 那道格式校验根本起不到保护作用。
#
# 更关键的是：必须在**读取过程中**累加判断，不能读完再判大小。


def test_upload_rejects_oversized_file():
    """超过上限的上传必须 413，而不是先读完再报格式错。"""
    import io as _io

    from app.api import reschedule as rs

    headers = _admin_headers()
    oversize = b"PK\x03\x04" + b"A" * (1024 * 1024)  # 1MB 实体
    # 把上限临时压到 64KB，避免测试里真的构造 32MB
    old = rs.MAX_UPLOAD_BYTES
    try:
        import os as _os
        rs.MAX_UPLOAD_BYTES = 64 * 1024
        r = client.post(
            "/api/reschedule/upload",
            files={"file": ("big.xlsx", _io.BytesIO(oversize), "application/octet-stream")},
            headers=headers,
        )
    finally:
        rs.MAX_UPLOAD_BYTES = old

    assert r.status_code == 413, f"超大文件未被拦截：{r.status_code} {r.text[:120]}"
    assert "上限" in r.json()["detail"]
    assert r.headers.get("X-Max-Upload-Bytes") == str(64 * 1024)


def test_upload_under_limit_reaches_parser():
    """上限内的文件应继续走到解析阶段（拿 400 而不是 413），证明没误伤正常上传。"""
    import io as _io

    headers = _admin_headers()
    payload = b"PK\x03\x04" + b"A" * 1024  # 1KB，远小于上限
    r = client.post(
        "/api/reschedule/upload",
        files={"file": ("ok.xlsx", _io.BytesIO(payload), "application/octet-stream")},
        headers=headers,
    )
    # 内容不是合法 zip → 解析失败 400；关键是**不是** 413
    assert r.status_code == 400, f"正常尺寸文件被误拦：{r.status_code}"
    assert "上限" not in r.text


def test_all_upload_endpoints_use_limited_reader():
    """守卫：5 个上传端点都不得再用裸 await file.read()。"""
    import inspect

    from app.api import reschedule as rs

    src = inspect.getsource(rs)
    assert "content = await file.read()" not in src, (
        "仍有上传端点用裸 file.read() 全量读入，超大文件会顶爆内存"
    )
    assert src.count("await _read_upload_limited(file)") >= 5, (
        "上传端点数与受限读取调用数不匹配"
    )


# ---------------------------------------------------------------------------
# 12. Excel 解析规模守卫（防"小而密"的压缩炸弹）
# ---------------------------------------------------------------------------
# 背景（2026-10-29 实测）：
#   271KB 的 xlsx 解压后 5.7MB XML / 50,000 行；
#   openpyxl 全量加载峰值 25.4MB（放大 96 倍），耗时 2.0 秒。
#   上传接口的 32MB 上限**完全拦不住**这种文件——它太小了。
#
# 所以必须在解析层加行数上限，且遍历时也要有兜底（防 dimension 被伪造）。


def test_sheet_size_guard_rejects_oversized():
    from app.utils import excel_parser as ep

    class FakeSheet:
        max_row = ep.MAX_SHEET_ROWS + 1

    with pytest.raises(ep.SheetTooLargeError) as ei:
        ep.assert_sheet_size(FakeSheet(), "测试表")
    assert "超过上限" in str(ei.value)


def test_sheet_size_guard_accepts_normal():
    from app.utils import excel_parser as ep

    class FakeSheet:
        max_row = 500

    ep.assert_sheet_size(FakeSheet(), "测试表")  # 不应抛异常


def test_bounded_iter_rows_stops_at_cap():
    """即使 dimension 被伪造（max_row 报小值），遍历时也必须停下。"""
    from app.utils import excel_parser as ep

    class FakeSheet:
        max_row = 10  # 谎报

        def iter_rows(self, *a, **kw):
            for i in range(ep.MAX_SHEET_ROWS + 5):
                yield (i,)

    n = 0
    with pytest.raises(ep.SheetTooLargeError):
        for _ in ep.bounded_iter_rows(FakeSheet()):
            n += 1
    # 应该在刚好越界时中断，不会跑完全部
    assert n == ep.MAX_SHEET_ROWS, f"遍历了 {n} 行才中断，边界不对"


def test_parser_wires_the_guard():
    """守卫不接线就只是摆设：确认解析入口真的调用了它。"""
    import inspect

    from app.utils import excel_parser as ep

    src = inspect.getsource(ep)
    assert "assert_sheet_size(sheet" in src, "解析入口未调用 assert_sheet_size"
    assert "bounded_iter_rows(sheet" in src, "行循环未用 bounded_iter_rows"


# ---------------------------------------------------------------------------
# 13. 行数超限的用户可见文案（不是运维话术）
# ---------------------------------------------------------------------------
# 背景：`SheetTooLargeError` 原本被 `except Exception` 吞掉，变成一个
# 「文件解析失败：…请设置环境变量 MAX_SHEET_ROWS 后重启服务。」的 400。
# 教秘看到的是：既让他去动环境变量，又说"解析失败"（其实根本没开始解析）。
#
# 这里锁定三件事：
#   1. 状态码 400（不是 500）
#   2. detail 有可操作建议（精简/拆分），无运维话术（环境变量/重启）
#   3. 不误用"文件解析失败"前缀（那是给真正的解析异常用的）
#   4. 限额头 X-Max-Sheet-Rows 暴露，供前端展示


def test_sheet_too_large_message_is_user_facing(monkeypatch):
    import io as _io

    import openpyxl

    from app.api import reschedule as rs
    from app.utils import excel_parser as ep

    # 压低上限，避免真的构造两万行
    monkeypatch.setattr(ep, "MAX_SHEET_ROWS", 5)

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "实验安排表"
    ws.append(["h1"])
    ws.append(["h2"])
    for i in range(20):
        ws.append([i + 1, "周一", "567", "甲班", "1组", "项目A", "A410", "吴老师"])
    buf = _io.BytesIO()
    wb.save(buf)

    r = client.post(
        "/api/reschedule/upload",
        files={"file": ("big.xlsx", buf.getvalue(), "application/vnd.ms-excel")},
        headers=_admin_headers(),
    )

    assert r.status_code == 400, f"行数超限应回 400，实际 {r.status_code}"
    detail = r.json()["detail"]

    # 必须可操作
    assert ("精简" in detail) or ("拆分" in detail), f"缺少可操作建议：{detail}"
    # 不得出现运维话术
    assert "环境变量" not in detail, f"泄漏了运维话术：{detail}"
    assert "重启" not in detail, f"泄漏了运维话术：{detail}"
    # 不得误用"解析失败"前缀
    assert "文件解析失败" not in detail, f"误用了解析失败前缀：{detail}"
    # 限额走头
    assert r.headers.get("X-Max-Sheet-Rows") == "5"


def test_sheet_too_large_error_carries_structured_fields():
    """异常对象要带 limit/actual，API 层不必从字符串里抠数字。"""
    from app.utils.excel_parser import SheetTooLargeError

    e = SheetTooLargeError("行数超限", limit=20000, actual=50000)
    assert e.limit == 20000
    assert e.actual == 50000
    assert isinstance(e, ValueError)


# ---------------------------------------------------------------------------
# 14. 令牌候选去重（R1 变异发现：原测试不覆盖）
# ---------------------------------------------------------------------------
# 发现过程：把 `_extract_tokens` 的去重逻辑删掉（直接 `return candidates`），
# `duplicate_authorization_values_are_all_considered` 等 3 个测试**仍然全绿**。
# 说明去重本身没有任何测试保护。
#
# 去重不是功能问题（重复 token 仍能验过），而是：
#   1. 同一 token 被重复验签 N 次 → N 次 HMAC 计算
#   2. 攻击者可以塞上千个重复头放大 CPU（限流按请求计，不按头数计）
# 所以它值得被锁住。


def test_extract_tokens_deduplicates_preserving_order():
    """同一令牌重复出现时只保留一份，且不改变首次出现的顺序。"""
    from app.security import _extract_tokens

    class _Req:
        def __init__(self, items):
            self._items = items

        class _H:
            def __init__(self, items):
                self._items = items

            def getlist(self, name):
                return self._items.get(name, [])

        @property
        def headers(self):
            return self._H(self._items)

    req = _Req({
        "authorization": ["Bearer tok-A", "Bearer tok-A", "Bearer tok-B"],
        "x-admin-token": ["tok-A"],
    })
    got = _extract_tokens(req)
    # tok-A 出现两次（Bearer + x-admin-token）→ 只留一个；tok-B 保留
    assert got == ["tok-A", "tok-B"], f"去重或保序失效：{got}"


def test_extract_tokens_dedup_under_many_duplicates():
    """大量重复头不应产生等量候选——这是放大攻击的入口。"""
    from app.security import _extract_tokens

    class _Req:
        class _H:
            def __init__(self, items):
                self._items = items

            def getlist(self, name):
                return self._items.get(name, [])

        def __init__(self, items):
            self._h = self._H(items)

        @property
        def headers(self):
            return self._h

    req = _Req({"authorization": ["Bearer same"] * 500})
    got = _extract_tokens(req)
    assert len(got) == 1, f"500 个重复头应压成 1 个候选，实际 {len(got)}"


# ---------------------------------------------------------------------------
# 15. 短行保护（R2 探针发现）
# ---------------------------------------------------------------------------
# 发现过程：用 openpyxl 构造「合并单元格表头」的文件（A1:D1 合并），
# 上传后返回 400「文件解析失败：tuple index out of range」。
#
# 根因：`for row in ...: row[0], row[1], ..., row[7]` 硬解包，
# 而合并单元格会让后续数据行的 tuple 变短（实测只剩 4 列）。
# IndexError 被上层 `except Exception` 包成"解析失败"，把 Python 内部错误
# 泄漏给了教秘。
#
# 这类畸形表在生产中很常见（教秘习惯合并表头单元格）。


def _xlsx_with_merged_header() -> bytes:
    import io as _io

    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "实验安排表"
    ws.append(["实验安排表", None, None, None])
    ws.merge_cells("A1:D1")
    ws.append(["周次", "星期", "节次", "班级"])
    ws.append([1, "周一", "567", "甲班"])
    buf = _io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_merged_header_does_not_crash():
    """合并单元格表头不得导致 IndexError，也不得把内部错误泄漏到 detail。"""
    r = client.post(
        "/api/reschedule/upload",
        files={"file": ("merged.xlsx", _xlsx_with_merged_header(), "application/vnd.ms-excel")},
        headers=_admin_headers(),
    )
    assert r.status_code == 200, f"合并表头应能解析，实际 {r.status_code}: {r.text[:200]}"
    body = r.json()
    # 那条 4 列的数据行没有实验项目/场地/教师 → 判为待排而非崩溃
    assert body["total_entries"] >= 0
    # 关键：不得出现 Python 内部错误字样
    assert "index out of range" not in r.text
    assert "tuple index" not in r.text


def test_short_rows_are_reported():
    """短行应被计数并出现在 load_report 里，而不是静默补齐。"""
    from app.utils.excel_parser import ScheduleDatabase
    import tempfile, os as _os

    fd, path = tempfile.mkstemp(suffix=".xlsx")
    _os.close(fd)
    try:
        with open(path, "wb") as f:
            f.write(_xlsx_with_merged_header())
        db = ScheduleDatabase()
        db.load_from_excel(path)
        assert db.load_report.get("short_rows", None) == 1, (
            f"短行未计数：{db.load_report.get('short_rows')}"
        )
    finally:
        try:
            _os.unlink(path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# 16. 4xx 文案可操作性（R3 审计发现）
# ---------------------------------------------------------------------------
# 审计方法：静态扫描 31 处 HTTPException + 运行时真实触发 10 个 4xx。
# 发现并修复：
#   1. 413 detail 含"设置环境变量 MAX_UPLOAD_MB 后重启服务"——运维话术漏给教秘
#   2. 登录失败只说"用户名或密码错误"，没说怎么办
#   3. 新建课程/教师/场地/班级 空名只说"不能为空"，没说下一步
#
# 这里锁定这几条，防止回退。


def test_413_detail_has_no_ops_jargon():
    """413 的 detail 不得出现环境变量/重启等运维话术。"""
    import io as _io

    from app.api import reschedule as rs

    old = rs.MAX_UPLOAD_BYTES
    try:
        rs.MAX_UPLOAD_BYTES = 64 * 1024
        big = b"PK\x03\x04" + b"A" * (200 * 1024)
        r = client.post(
            "/api/reschedule/upload",
            files={"file": ("big.xlsx", _io.BytesIO(big), "application/octet-stream")},
            headers=_admin_headers(),
        )
    finally:
        rs.MAX_UPLOAD_BYTES = old

    assert r.status_code == 413
    detail = r.json()["detail"]
    for bad in ("环境变量", "重启", "MAX_UPLOAD_MB", ".env", "配置文件"):
        assert bad not in detail, f"413 detail 泄漏运维话术「{bad}」：{detail}"
    assert ("压缩" in detail) or ("拆分" in detail), f"缺可操作建议：{detail}"


def test_login_failure_says_what_to_do():
    """登录失败要给出下一步，但仍不区分账号错/口令错（防账号枚举）。"""
    r = client.post(
        "/api/reschedule/admin-login",
        json={"username": "admin", "password": "definitely-wrong-xxxx"},
    )
    assert r.status_code == 401
    detail = r.json()["detail"]
    assert "用户名或密码错误" in detail
    assert any(k in detail for k in ("请", "联系", "稍后")), f"无可操作指引：{detail}"
    # 不得泄漏"账号是否存在"
    assert "不存在" not in detail and "无此用户" not in detail


@pytest.mark.parametrize(
    "path,body",
    [
        ("/api/reschedule/master/courses", {"name": ""}),
        ("/api/reschedule/master/teachers", {"name": ""}),
        ("/api/reschedule/master/rooms", {"name": ""}),
        ("/api/reschedule/master/classes", {"name": ""}),
    ],
)
def test_master_empty_name_says_what_to_do(path, body):
    r = client.post(path, json=body, headers=_admin_headers())
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert "不能为空" in detail
    assert any(k in detail for k in ("请", "填写", "再")), f"无可操作指引：{detail}"


# ---------------------------------------------------------------------------
# 17. 上传重载的原子性（R4 竞态实测）
# ---------------------------------------------------------------------------
# 发现过程：`load_from_excel` 是原地改写（先 loaded=False、entries=[]，
# 再逐行填充）。在清空后注入 300ms 延迟，并发读取方 **100% 读到 0 条**
# （21,572,523 次读数全部异常）。真实全量解析约 157ms，窗口虽窄但确实存在。
#
# 修复：upload_schedule 改为「先构建新 ScheduleDatabase，成功后原子替换
# 全局引用」，读取方要么看到旧表、要么看到新表。
#
# 修复后同一探针：异常读数 0 次。


def test_upload_does_not_blank_db_during_reload(monkeypatch):
    """上传期间，并发读取不得看到空表或半成品。"""
    import threading
    import time as _time

    from app.api import reschedule as rs
    from app.utils.excel_parser import ScheduleDatabase

    # 准备一份可解析的 xlsx
    import io as _io

    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "实验安排表"
    ws.append(["h1"])
    ws.append(["h2"])
    for i in range(10):
        ws.append([i + 1, "周一", "567", "甲班", f"{i}组", "项目A", "A410", "吴老师"])
    buf = _io.BytesIO()
    wb.save(buf)
    payload = buf.getvalue()

    # 基线：先让 _db 处于已加载状态
    base_db = ScheduleDatabase()
    monkeypatch.setattr(rs, "_db", base_db)
    monkeypatch.setattr(rs, "_master_data", rs.MasterData())
    monkeypatch.setattr(rs, "_master_tasks", [])
    import pathlib as _pathlib
    import tempfile as _tempfile

    _tmp = _pathlib.Path(_tempfile.mkdtemp())
    monkeypatch.setattr(rs, "_DB_PATH", str(_tmp / "t.db"))
    monkeypatch.setattr(rs, "_DATA_DIR", str(_tmp / "up"))
    monkeypatch.setattr(rs, "_landing_store", None)

    orig_load = ScheduleDatabase.load_from_excel

    # 第一步：正常速度先传一次，建立 "10 条" 基线。
    # 不先建基线的话，采样线程会在上传**之前**读到 0 条，误报为异常。
    r0 = client.post(
        "/api/reschedule/upload",
        files={"file": ("base.xlsx", payload, "application/vnd.ms-excel")},
        headers=_admin_headers(),
    )
    assert r0.status_code == 200, f"基线建立失败：{r0.status_code} {r0.text[:150]}"
    assert len(rs._db.entries) == 10, f"基线条数异常：{len(rs._db.entries)}"

    # 第二步：让解析变慢，放大重载窗口
    def slow(self, *a, **kw):
        r = orig_load(self, *a, **kw)
        _time.sleep(0.6)
        return r

    monkeypatch.setattr(ScheduleDatabase, "load_from_excel", slow)

    stop = threading.Event()
    bad = []
    reads = [0]
    saw_nonempty = [0]

    def reader():
        while not stop.is_set():
            cur = rs._db
            n = len(cur.entries)
            reads[0] += 1
            # 记录"窗口内确实读到过数据"的证据：否则可能是读取线程根本没跑在窗口内，
            # 测试会假绿（变异体存活正是这个原因）。
            if n > 0:
                saw_nonempty[0] += 1
            # 只看条数，不看 loaded。
            # 诊断（R4）：变异状态下读取方看到的是 (entries=0, loaded=False)，
            # 因为 load_from_excel 先置 loaded=False 再清空。若把 loaded 当条件，
            # 这类样本会被整个漏掉，测试假绿。
            if n != 10:
                bad.append((n, cur.loaded))

    t = threading.Thread(target=reader, daemon=True)
    t.start()

    r = client.post(
        "/api/reschedule/upload",
        files={"file": ("s.xlsx", payload, "application/vnd.ms-excel")},
        headers=_admin_headers(),
    )
    stop.set()
    t.join(timeout=3)

    # 恢复
    monkeypatch.setattr(ScheduleDatabase, "load_from_excel", orig_load)

    assert r.status_code == 200, f"上传失败：{r.status_code} {r.text[:150]}"
    assert reads[0] > 0, "并发读取未执行"
    # 反假绿：读取线程必须在窗口内真的读到过非空数据，
    # 否则"没发现异常"只是因为没读到，而不是因为原子性生效。
    assert saw_nonempty[0] > 0, (
        f"读取线程在 {reads[0]} 次采样中从未读到非空数据——"
        "说明它没覆盖到上传窗口，此测试无效（需调大 sleep 或增加采样）"
    )
    assert not bad, (
        f"上传期间读到不一致数据 {len(bad)} 次（含空表），"
        f"示例 (entries, loaded)：{bad[:5]}"
    )


def test_upload_keeps_old_data_when_parse_fails(monkeypatch):
    """解析失败时不应把现有 _db 搞空——旧数据要保持可用。"""
    import io as _io

    from app.api import reschedule as rs
    from app.utils.excel_parser import ScheduleDatabase

    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "实验安排表"
    ws.append(["h1"])
    ws.append(["h2"])
    for i in range(3):
        ws.append([i + 1, "周一", "567", "甲班", "1组", "项目A", "A410", "吴老师"])
    ok = _io.BytesIO()
    wb.save(ok)

    monkeypatch.setattr(rs, "_DATA_DIR", str(__import__("tempfile").mkdtemp() + "/up"))
    monkeypatch.setattr(rs, "_landing_store", None)
    monkeypatch.setattr(rs, "_master_tasks", [])
    monkeypatch.setattr(rs, "_master_data", rs.MasterData())

    # 先加载成功
    r1 = client.post(
        "/api/reschedule/upload",
        files={"file": ("a.xlsx", ok.getvalue(), "application/vnd.ms-excel")},
        headers=_admin_headers(),
    )
    assert r1.status_code == 200, r1.text[:150]
    before = len(rs._db.entries)
    assert before == 3

    # 再传一个坏文件
    r2 = client.post(
        "/api/reschedule/upload",
        files={"file": ("bad.xlsx", b"not a zip", "application/vnd.ms-excel")},
        headers=_admin_headers(),
    )
    assert r2.status_code == 400

    after = len(rs._db.entries)
    assert after == before, (
        f"解析失败后旧数据被破坏：{before} -> {after}（应保持原样）"
    )


# ---------------------------------------------------------------------------
# 18. /status 的加载提示（R18 走查 README bootstrap 时发现）
# ---------------------------------------------------------------------------
# 问题：冷启动后 `/status` 返回 loaded=false（这是正确的，该端点只查状态不触发
# 懒加载）。但新部署者第一眼看到 false 会以为服务坏了 —— README 也没说"首次
# 访问数据端点才加载"。
#
# 修复：/status 增加 loaded_hint，明确说明这是懒加载。


def test_status_hint_explains_lazy_load(monkeypatch):
    """未加载时 hint 必须解释这是懒加载，并指出如何触发。"""
    from app.api import reschedule as rs

    monkeypatch.setattr(rs._db, "loaded", False)
    r = client.get("/api/reschedule/status")
    assert r.status_code == 200
    hint = r.json().get("loaded_hint", "")
    assert hint, "/status 缺少 loaded_hint"
    assert "懒加载" in hint or "未加载" in hint, f"未解释状态：{hint}"
    # 要给出可操作的下一步
    assert "teachers" in hint or "数据端点" in hint, f"未指出如何触发加载：{hint}"


def test_status_hint_says_loaded_when_loaded(monkeypatch):
    from app.api import reschedule as rs

    monkeypatch.setattr(rs._db, "loaded", True)
    r = client.get("/api/reschedule/status")
    assert r.json().get("loaded_hint") == "已加载"


# ---------------------------------------------------------------------------
# R31：agent 层返回的字段，API 层必须真的透传出去
#
# `reschedule_chat` 的 `data` 是**白名单拼出来的 dict**：
#
#     data={ "target_week": ..., "available_slots": ..., ... }
#
# 漏写一个键 = 该字段被静默吞掉。R21 在 agent 层加了
# `course_name` / `course_unrecognized`，并注释写着"下游不要从 message 里
# 正则反解"—— 但 API 层从未透传，下游根本拿不到。
#
# 这个 bug 在有 487 个测试的情况下活了十轮，因为：
#   · agent 层的测试直接调 `process_request`，看得到字段
#   · API 层的测试只断言 status_code / success
# **两层各自都对，接缝处没人看。**
# ---------------------------------------------------------------------------


def test_chat_passes_through_course_fields():
    """`/chat` 的 data 里必须带上 course_name 与 course_unrecognized。"""
    r = client.post("/api/reschedule/chat", json={
        "message": "我是吴琳，把声速测量调到第3周", "fast": True,
    })
    assert r.status_code == 200
    data = r.json()["data"]
    assert "course_name" in data, (
        "API 层没透传 course_name —— agent 层返回了但被 data 白名单吞掉"
    )
    assert "course_unrecognized" in data, (
        "API 层没透传 course_unrecognized —— R21 的软提示在下游拿不到"
    )


def test_chat_marks_unrecognized_course_via_structured_field():
    """简称匹配不到全名时，结构化字段要标出来（而不是只在 message 里说）。"""
    # 先预热：不加载数据会走"尚未加载"分支，验不到正常路径
    client.get("/api/reschedule/teachers")
    r = client.post("/api/reschedule/chat", json={
        "message": "我是教师H，把PN结调到第3周", "fast": True,
    })
    assert r.status_code == 200
    data = r.json()["data"]
    if data.get("need_info"):
        pytest.skip("该数据集下先追问了，未走到出结果的分支")
    # 断言的是**两个字段之间的一致性**，不是某个固定值 ——
    # 演示表里没有「PN结…」这门课，所以 course_name 一定是 None；
    # 但真实表里可能有，不能把"这个数据集的结果"写成断言。
    recognized = bool(data.get("course_name"))
    flag = data.get("course_unrecognized")
    assert flag is (not recognized), (
        f"course_unrecognized={flag} 与 course_name={data.get('course_name')!r} 矛盾 —— "
        "下游要么拿到错的标志，要么只能去正则解析 message"
    )
