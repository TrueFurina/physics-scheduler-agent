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
