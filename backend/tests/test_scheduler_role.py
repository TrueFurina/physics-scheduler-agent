"""排课人员（scheduler）角色门禁回归测试（座谈第 3 步：指定排课人员、分配权限）。

锁定的边界
----------
令牌载荷里新增 `r`（角色）。两条**方向相反**的性质必须同时成立：

  1. **降挂不破管理员**：`admin ⊇ scheduler`。管理员令牌在排课类写接口
     （/upload、/upload-master、/master/init、/master/import、/parallel/land、
     收紧后的整表导出）照旧通过 —— 一条都不少。
  2. **越权必拒**：`role="scheduler"` 的令牌打管理员专属口（清工单、审批、
     撤销落地、五表删除）必须 **403**，而不是被当成"另一种管理员"放过去。

还锁定向后兼容的地基：**角色机制之前签发的旧令牌（载荷无 `r`）按 admin 处理**。
哪天有人把这个默认改掉，在线会话会集体掉进 403 —— 见
`test_legacy_token_without_role_is_still_admin`。

以及"默认关闭"：未配置 `SCHEDULER_PASS` 时该角色**不存在**，
`/scheduler-login` 回 403"未启用"、`check_scheduler_credentials` 恒 False，
所以合入这一步后现网行为与之前逐字一致。

--------------------------------------------------------------------------
反假绿说明（R-25）
--------------------------------------------------------------------------
本文件里"越权必拒"那几条断言的是 **403**，而不是"非 2xx"。这一点很重要：
若把 `require_admin` 的角色校验去掉（退回成"任何能验签的令牌都算管理员"），
`test_scheduler_token_rejected_on_approve` / `..._on_delete_requests` /
`..._on_land_undo` 会拿到 200/404 而**必然变红**。
实测过程见交付报告：临时删掉 role 判定 → 这三条红；改回 → 全绿。
反过来，"排课人员被放行"那几条也做了同构防呆：断言的是
`not in (401, 403)` 而不是 `== 200`，业务校验失败（400）算通过门禁，
而门禁被误挂成 admin-only 时它们会红。
"""

import json
import os
import sys
import time

import pytest

# 口令不设默认值（fail-closed），必须在导入 app.* 之前注入（与 test_api_auth.py 同法）。
os.environ.setdefault("ADMIN_PASS", "admin123")
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import security as sec
from app.api import reschedule as rs
from app.main import app
from app.security import (
    check_scheduler_credentials,
    issue_admin_token,
    issue_scheduler_token,
    verify_admin_token,
)

client = TestClient(app)

PREFIX = "/api/reschedule"
SCHED = "scheduler"


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _admin_tok(user: str = "admin") -> dict:
    """管理员票：走默认 role（不写 role 参数，锁住"旧调用点行为不变"）。"""
    return _bearer(issue_admin_token(user)["token"])


def _sched_tok(user: str = SCHED) -> dict:
    return _bearer(issue_scheduler_token(user)["token"])


@pytest.fixture()
def isolated_data_dir(tmp_path, monkeypatch):
    """把上传落盘与 sqlite 重定向到临时目录。

    门禁测试只关心"有没有 401/403"，请求会在**通过门禁之后**才写盘；
    不重定向就会把垃圾文件落进真实 `backend/app/data/`（该目录随发布包上传）。

    刻意不重定向 `_DB_PATH`：sqlite 的 `requests` 表在 `include_router` 阶段
    并不建表，指向临时路径会让"管理员放行"那几条读到 no such table。
    """
    monkeypatch.setattr(rs, "_DATA_DIR", str(tmp_path / "up"))
    monkeypatch.setattr(rs, "_landing_store", None)
    return tmp_path


# ---------------------------------------------------------------------------
# 1. 令牌载荷与角色默认
# ---------------------------------------------------------------------------


def _payload_of(token: str) -> dict:
    payload_b64, _, _sig = token.rpartition(".")
    return json.loads(sec._b64d(payload_b64).decode("utf-8"))


def test_default_role_is_admin_in_payload():
    """`issue_admin_token` 不传 role 时载荷里必须是 admin —— 既有调用点零改动。"""
    assert _payload_of(issue_admin_token("admin")["token"])["r"] == "admin"


def test_issue_scheduler_token_carries_scheduler_role():
    ticket = issue_scheduler_token(SCHED)
    assert _payload_of(ticket["token"])["r"] == "scheduler"
    # 账号照旧取得到：verify_admin_token 的公开契约（返回 username）没变
    assert verify_admin_token(ticket["token"]) == SCHED
    assert sec._verify_identity(ticket["token"]) == {"u": SCHED, "r": "scheduler"}


def test_legacy_token_without_role_is_still_admin(isolated_data_dir):
    """向后兼容的地基：角色机制**之前**签的令牌（载荷没有 r）仍算管理员。

    若有人把 `_verify_identity` 的默认改成"缺 r 即拒绝"，所有在线的管理员会话
    会在这一步合入后集体 403 —— 这条断言就是那次的保险栓。
    """
    legacy_payload = {"u": "admin", "exp": int(time.time()) + 3600, "n": "deadbeef"}
    b64 = sec._b64e(json.dumps(legacy_payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    legacy = f"{b64}.{sec._sign(b64)}"

    assert "r" not in _payload_of(legacy), "样本构造错了：这必须是旧格式令牌"
    assert verify_admin_token(legacy) == "admin"
    assert sec._verify_identity(legacy) == {"u": "admin", "r": "admin"}
    # 管理员专属口（require_admin）与排课口（require_scheduler）都必须放行
    assert client.get(f"{PREFIX}/requests", headers=_bearer(legacy)).status_code == 200
    assert client.post(f"{PREFIX}/upload", headers=_bearer(legacy),
                       files={"file": ("a.xlsx", b"x", "application/vnd.ms-excel")}
                       ).status_code not in (401, 403)


def test_forged_role_scheduler_signature_mismatch_rejected():
    """改载荷保签名（把 r 伪造成别的角色）必须无效 —— 角色不能自己填。"""
    token = issue_admin_token("admin")["token"]
    payload = _payload_of(token)
    payload["r"] = "auditor"
    forged_b64 = sec._b64e(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    sig = token.rpartition(".")[2]
    assert forged_b64 != token.rpartition(".")[0], "样本构造错了：载荷必须真的变了"
    r = client.get(f"{PREFIX}/requests", headers=_bearer(f"{forged_b64}.{sig}"))
    assert r.status_code == 401, f"签名不符的伪造角色令牌竟然放行：{r.status_code}"


# ---------------------------------------------------------------------------
# 2. 排课类写接口：scheduler 放行、匿名仍挡、admin 不减
# ---------------------------------------------------------------------------


def _xlsx_bytes() -> bytes:
    """一个合法后缀、非法内容的 xlsx：足以证明"门禁过了、卡在解析"。"""
    return b"PK\x03\x04" + b"A" * 512


def test_scheduler_token_allowed_on_upload(isolated_data_dir):
    """role=scheduler 打 /upload 必须**不是 401/403**（能进门禁；400 解析失败是业务层）。"""
    r = client.post(f"{PREFIX}/upload", headers=_sched_tok(),
                    files={"file": ("sched.xlsx", _xlsx_bytes(), "application/vnd.ms-excel")})
    assert r.status_code not in (401, 403), f"排课人员被门禁挡住：{r.status_code} {r.text[:160]}"


def test_admin_token_still_works_on_scheduler_endpoints(isolated_data_dir):
    """admin ⊇ scheduler：管理员令牌在降挂后的排课口照旧通过。"""
    r = client.post(f"{PREFIX}/upload", headers=_admin_tok(),
                    files={"file": ("adm.xlsx", _xlsx_bytes(), "application/vnd.ms-excel")})
    assert r.status_code not in (401, 403), f"管理员被排课门禁误伤：{r.status_code} {r.text[:160]}"


@pytest.mark.parametrize("name", ["upload-master", "master/import"])
def test_scheduler_token_allowed_on_other_downgraded_uploads(name, isolated_data_dir):
    """/upload-master、/master/import 同样降挂：排课人员应通过门禁（内容仍会 400）。"""
    r = client.post(f"{PREFIX}/{name}", headers=_sched_tok(),
                    files={"file": ("m.xlsx", _xlsx_bytes(), "application/vnd.ms-excel")})
    assert r.status_code not in (401, 403), f"{name} 未降挂 scheduler：{r.status_code} {r.text[:160]}"


def test_scheduler_token_allowed_on_master_init(isolated_data_dir, monkeypatch):
    """/master/init 降挂后可用；主数据对象换成临时实例，不污染其它用例。"""
    monkeypatch.setattr(rs, "_master_data", rs.MasterData())
    r = client.post(f"{PREFIX}/master/init", headers=_sched_tok())
    assert r.status_code not in (401, 403), f"/master/init 未降挂：{r.status_code} {r.text[:160]}"


def test_scheduler_token_allowed_on_parallel_land():
    """采纳落地方案是排课人员的日常动作：门禁必须放行（空方案回 400 属业务校验）。"""
    r = client.post(f"{PREFIX}/parallel/land", headers=_sched_tok(), json={"schedule": []})
    assert r.status_code not in (401, 403), f"/parallel/land 未降挂：{r.status_code} {r.text[:160]}"


def test_no_token_upload_still_401():
    """降挂 scheduler 不等于把门开没了：匿名 /upload 仍 401，且文案给出会话指引。"""
    r = client.post(f"{PREFIX}/upload",
                    files={"file": ("a.xlsx", b"x", "application/vnd.ms-excel")})
    assert r.status_code == 401, f"匿名竟然能上传：{r.status_code}"
    assert "管理员" in r.json()["detail"], "401 文案丢了'管理员'字样（test_api_auth 依赖它）"


@pytest.mark.parametrize("method,path,kwargs", [
    ("POST", "/upload", {"files": {"file": ("a.xlsx", b"x", "application/vnd.ms-excel")}}),
    ("POST", "/upload-master", {"files": {"file": ("a.xlsx", b"x", "application/vnd.ms-excel")}}),
    ("POST", "/master/init", {}),
    ("POST", "/master/import", {"files": {"file": ("a.xlsx", b"x", "application/vnd.ms-excel")}}),
    ("POST", "/parallel/land", {"json": {"schedule": []}}),
])
def test_anonymous_still_blocked_on_downgraded_endpoints(method, path, kwargs, isolated_data_dir):
    """五个降挂端点逐个确认：匿名一律 401（门禁存在，只是受众变成"管理员或排课人员"）。"""
    r = client.request(method, f"{PREFIX}{path}", **kwargs)
    assert r.status_code == 401, f"{method} {path} 匿名可达：{r.status_code}"
    assert "管理员" in r.json()["detail"], f"{path} 的 401 文案不含'管理员'（与既有规格冲突）"


@pytest.mark.parametrize("method,path,kwargs", [
    ("POST", "/upload", {"files": {"file": ("a.xlsx", b"x", "application/vnd.ms-excel")}}),
    ("POST", "/parallel/land", {"json": {"schedule": []}}),
])
def test_expired_scheduler_token_rejected_on_downgraded_endpoints(method, path, kwargs, isolated_data_dir):
    """过期/伪造的排课票同样要挡：降挂不是放宽验签，只放宽角色集合。"""
    expired = issue_scheduler_token(SCHED, ttl=-1)["token"]
    r = client.request(method, f"{PREFIX}{path}", headers=_bearer(expired), **kwargs)
    assert r.status_code == 401
    assert "管理员" in r.json()["detail"]


# ---------------------------------------------------------------------------
# 3. 管理员专属口：scheduler 一律 403（越权必拒）
# ---------------------------------------------------------------------------
# 反假绿锚点（R-25）：下面三条断言的是 **403**。
# 如果把 `require_admin` 的角色校验去掉（退回"任意可验签令牌即管理员"），
# 这三条必红；`test_api_auth.py` 里那批"有效管理员令牌 → 放行"的用例不会红
# —— 正因为如此，这三条才是"越权被拒"的唯一哨兵，不能省。


def test_scheduler_token_rejected_on_approve():
    """审批调课是管理员权：排课人员令牌必须 403（不是 401，也不是放行）。"""
    r = client.post(f"{PREFIX}/requests/1/approve", headers=_sched_tok(),
                    json={"approve": True, "comment": "ok"})
    assert r.status_code == 403, f"排课人员竟然能审批：{r.status_code} {r.text[:160]}"
    detail = r.json()["detail"]
    assert "权限不足" in detail and "管理员" in detail, f"403 文案不可操作：{detail}"


def test_scheduler_token_rejected_on_delete_requests():
    """清空工单表是破坏性操作：排课人员令牌 403。"""
    r = client.delete(f"{PREFIX}/requests", headers=_sched_tok())
    assert r.status_code == 403, f"排课人员竟然能清工单：{r.status_code}"


def test_scheduler_token_rejected_on_land_undo():
    """撤销落地一次回滚整批课表改动，仍只认管理员：排课人员令牌 403。"""
    r = client.post(f"{PREFIX}/parallel/land/undo", headers=_sched_tok())
    assert r.status_code == 403, f"排课人员竟然能撤销落地：{r.status_code}"


@pytest.mark.parametrize("method,path,kwargs", [
    ("POST", "/requests", {"json": {
        "teacher": "t", "course_name": "c", "class_name": "k",
        "original_time": "周一 567", "target_week": 3, "reason": "r",
    }}),
    ("GET", "/requests", {}),
    ("GET", "/requests/export", {}),
    ("POST", "/apply-move", {"json": {
        "teacher": "t", "course_name": "c", "class_name": "k",
        "original_time": "周一 567", "target_week": 3,
    }}),
    ("DELETE", "/master/courses/__no_such_course__", {}),
])
def test_scheduler_token_rejected_on_more_admin_only_paths(method, path, kwargs):
    """其它保持 require_admin 的口也要真的挡住（清单钉死，防止"顺手降挂"）。"""
    r = client.request(method, f"{PREFIX}{path}", headers=_sched_tok(), **kwargs)
    assert r.status_code == 403, f"{method} {path} 竟然放行了排课人员：{r.status_code} {r.text[:160]}"


def test_admin_token_still_passes_admin_only_gate():
    """管理员专属口没有因为加角色而收紧：admin 票照旧能审批（404 = 业务层"工单不存在"，不是门禁）。"""
    r = client.post(f"{PREFIX}/requests/999999/approve", headers=_admin_tok(),
                    json={"approve": True, "comment": "ok"})
    assert r.status_code not in (401, 403), f"管理员被自己的门禁挡住：{r.status_code} {r.text[:160]}"


# ---------------------------------------------------------------------------
# 4. /scheduler-login 的默认关闭与启用形态
# ---------------------------------------------------------------------------


def test_scheduler_login_disabled_without_env(monkeypatch):
    """未配置 SCHEDULER_PASS：403"未启用"，且凭据校验恒 False（角色不存在）。"""
    monkeypatch.delenv("SCHEDULER_PASS", raising=False)
    assert sec.scheduler_enabled() is False
    assert check_scheduler_credentials(SCHED, "whatever") is False

    r = client.post(f"{PREFIX}/scheduler-login", json={"username": SCHED, "password": "whatever"})
    assert r.status_code == 403, f"未启用时竟然签发令牌：{r.status_code} {r.text[:160]}"
    assert "未启用" in r.json()["detail"]


def test_scheduler_login_blank_password_counts_as_disabled(monkeypatch):
    """`SCHEDULER_PASS=`（空串/纯空格）也算未启用：不能把空口令当"已配置"。"""
    monkeypatch.setenv("SCHEDULER_PASS", "   ")
    assert sec.scheduler_enabled() is False
    assert check_scheduler_credentials(SCHED, "") is False
    r = client.post(f"{PREFIX}/scheduler-login", json={"username": SCHED, "password": ""})
    assert r.status_code == 403


@pytest.mark.parametrize("username,password", [
    (SCHED, "wrong-pass"),
    ("wrong-user", "a-pass"),
])
def test_scheduler_login_wrong_credentials_is_401_not_403(monkeypatch, username, password):
    """启用后凭据错 → 401（与 admin-login 同款文案，不区分账号/口令，防枚举）。"""
    monkeypatch.setenv("SCHEDULER_PASS", "a-scheduler-pass")
    r = client.post(f"{PREFIX}/scheduler-login", json={"username": username, "password": password})
    assert r.status_code == 401, f"凭据错竟然不是 401：{r.status_code} {r.text[:160]}"
    assert "用户名或密码错误" in r.json()["detail"]


def test_scheduler_login_enabled_issues_token_with_real_permissions(monkeypatch, isolated_data_dir):
    """启用 → 200 + role=scheduler 的票，并且这张票的**实际能力**符合设计：
    排课口放行、审批口 403。（登录成功但票不好用 = 假绿，所以一路验到底。）
    """
    monkeypatch.setenv("SCHEDULER_PASS", "a-scheduler-pass")
    r = client.post(f"{PREFIX}/scheduler-login", json={"username": SCHED, "password": "a-scheduler-pass"})
    assert r.status_code == 200, r.text[:200]
    body = r.json()
    assert body["role"] == "scheduler"
    assert body["token"] and body["expires_at"] > 0

    headers = _bearer(body["token"])
    assert client.post(f"{PREFIX}/upload", headers=headers,
                       files={"file": ("s.xlsx", _xlsx_bytes(), "application/vnd.ms-excel")}
                       ).status_code not in (401, 403)
    assert client.post(f"{PREFIX}/requests/1/approve", headers=headers,
                       json={"approve": True, "comment": "ok"}).status_code == 403
    # 登录口的票同样受"管理员专属"保护：不能反过来让 scheduler 冒 admin
    assert client.delete(f"{PREFIX}/requests", headers=headers).status_code == 403


def test_admin_login_response_unchanged():
    """管理员登录的响应体与行为零变更（role=admin、令牌可用）。"""
    r = client.post(f"{PREFIX}/admin-login",
                    json={"username": "admin", "password": os.environ["ADMIN_PASS"]})
    assert r.status_code == 200, r.text[:200]
    body = r.json()
    assert body["role"] == "admin" and body["token"]
    assert client.get(f"{PREFIX}/requests", headers=_bearer(body["token"])).status_code == 200


# ---------------------------------------------------------------------------
# 5. 收紧后的整表导出：管理员或排课人员都放行，匿名仍 401
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("method,path,body", [
    ("GET", "/export/class-schedule", None),
    ("GET", "/export/room-schedule", None),
    ("GET", "/export/master-workbook", None),
    ("GET", "/export/master-data", None),
    ("POST", "/parallel/land/preview", {"schedule": []}),
    ("POST", "/auto-schedule/export", {"schedule": []}),
])
def test_tightened_export_allows_scheduler_and_admin(method, path, body, monkeypatch,
                                                     isolated_data_dir):
    """PUBLIC_READONLY=0：排课人员是导出的主要使用者，必须放行；管理员照旧（admin ⊇ scheduler）。"""
    monkeypatch.setenv("PUBLIC_READONLY", "0")

    anon = client.request(method, f"{PREFIX}{path}", json=body)
    assert anon.status_code == 401, f"{path} 收紧后匿名竟可访问"
    assert "管理员" in anon.json()["detail"]

    for label, headers in (("scheduler", _sched_tok()), ("admin", _admin_tok())):
        r = client.request(method, f"{PREFIX}{path}", json=body, headers=headers)
        assert r.status_code not in (401, 403), f"{label} 被收紧后的导出门禁挡住：{r.status_code}"


def test_export_open_by_default_untouched(monkeypatch):
    """默认形态（PUBLIC_READONLY 缺省）完全不变：匿名仍能导出，没被这次改动误伤。"""
    monkeypatch.delenv("PUBLIC_READONLY", raising=False)
    r = client.get(f"{PREFIX}/export/class-schedule")
    assert r.status_code != 401, f"默认形态被门禁误伤：{r.status_code}"


# ---------------------------------------------------------------------------
# 6. 门禁实现层的静态守卫
# ---------------------------------------------------------------------------


def test_downgraded_endpoints_wired_correctly():
    """守卫：清单里的端点挂 require_scheduler，其余破坏性口仍挂 require_admin。

    为什么不只靠 HTTP 行为测：路由定义与依赖的对应关系一旦被人"顺手改回去"，
    行为测试只会报"某个用例 403 了"，指不到具体是哪条装饰器动过。

    实现上读 `rs.router.routes`（不是 `app.routes`）：`include_router` 在本机的
    FastAPI 版本里把子路由收进一个 `_IncludedRouter` 包装，`app.routes` 里
    根本摊平不出每条路径。
    """
    def _gate_names(path: str, method: str) -> set:
        for route in rs.router.routes:
            if route.path == path and method in (route.methods or set()):
                return {getattr(d.dependency, "__name__", "?")
                        for d in (route.dependencies or [])}
        raise AssertionError(f"找不到路由 {method} {path}（路径或方法变了？同步更新本测试）")

    for path, method in (("/upload", "POST"), ("/upload-master", "POST"),
                         ("/master/init", "POST"), ("/master/import", "POST"),
                         ("/parallel/land", "POST")):
        names = _gate_names(path, method)
        assert "require_scheduler" in names, f"{method} {path} 未降挂排课人员门禁：{names}"
        assert "require_admin" not in names, f"{method} {path} 仍挂着管理员门禁：{names}"

    for path, method in (("/requests", "DELETE"), ("/requests", "POST"), ("/requests", "GET"),
                         ("/requests/export", "GET"), ("/requests/{request_id}/approve", "POST"),
                         ("/parallel/land/undo", "POST"), ("/master/courses/{name}", "DELETE"),
                         ("/apply-move", "POST"), ("/demo/reset", "POST"),
                         ("/master/tasks", "DELETE")):
        assert "require_admin" in _gate_names(path, method), \
            f"{method} {path} 被误降挂：破坏性/审批必须只认管理员"

    # 登录口自身必须免鉴权（否则没人能拿到第一张票）
    for path in ("/admin-login", "/scheduler-login"):
        assert "require_admin" not in _gate_names(path, "POST")
        assert "require_scheduler" not in _gate_names(path, "POST")


def test_export_gate_uses_scheduler_role():
    """收紧态的导出门必须走 `require_scheduler`（否则排课人员一天要点无数次导出的活儿干不了）。"""
    import inspect

    assert "require_scheduler" in inspect.getsource(rs.export_gate)


def test_unknown_role_token_is_forbidden_not_401(monkeypatch):
    """角色既不是 admin 也不是 scheduler 的有效令牌：两个门都回 403（授予集之外一律拒绝）。

    目前系统只有两种角色，这条走的是"逻辑写全"的分支 —— 用未来可能新增的
    角色（如 auditor）把边界钉住，免得那时才发现默认放行。
    """
    weird = issue_admin_token("someone", role="auditor")["token"]
    assert client.post(f"{PREFIX}/upload", headers=_bearer(weird),
                       files={"file": ("a.xlsx", b"x", "application/vnd.ms-excel")}
                       ).status_code == 403
    assert client.get(f"{PREFIX}/requests", headers=_bearer(weird)).status_code == 403


def test_x_admin_token_header_works_for_scheduler():
    """排课票同样吃 `X-Admin-Token` 兜底头（边缘代理会覆盖 Authorization，
    这是 security.py 里实测踩过的坑，不能只给管理员修）。"""
    r = client.post(f"{PREFIX}/parallel/land", headers={"X-Admin-Token": issue_scheduler_token(SCHED)["token"]},
                    json={"schedule": []})
    assert r.status_code not in (401, 403)


def test_scheduler_token_survives_injected_authorization_header():
    """代理注入的垃圾 Authorization + 有效排课票放在 X-Admin-Token → 仍应放行。"""
    r = client.post(f"{PREFIX}/parallel/land",
                    headers={"Authorization": "Bearer injected-by-proxy",
                             "X-Admin-Token": issue_scheduler_token(SCHED)["token"]},
                    json={"schedule": []})
    assert r.status_code not in (401, 403), f"注入头把排课票屏蔽了：{r.status_code}"


def test_require_roles_helper_is_single_source_of_truth():
    """两个依赖共用一份候选令牌循环（防"两份实现漂移"）：require_admin 里不该再自己解析头。"""
    import inspect

    for fn in (sec.require_admin, sec.require_scheduler):
        body = inspect.getsource(fn)
        assert "_extract_tokens" not in body, f"{fn.__name__} 自己抄了一遍令牌循环"
        assert "_require_roles" in body, f"{fn.__name__} 没走统一实现"
