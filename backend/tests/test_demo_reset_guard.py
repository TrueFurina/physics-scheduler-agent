"""`/demo/reset` 的 DEMO_MODE 安全闸门测试（R-105）。

为什么必须有：这个端点会**真的清空申请单 + 撤销所有已应用调课 + 重载课表**，
是演示环境专用的"一键复位"。它的唯一保护就是 543-544 的
`if os.getenv("DEMO_MODE","") != "1": raise 403`。这一行此前**零覆盖** ——
等于"生产环境里这个危险按钮的保险丝没人测过"。必须双向锁死：
  ▸ DEMO_MODE 不是 "1" → 403（闸门生效）；
  ▸ DEMO_MODE == "1" → 正常复位成功。
R-40 注入验证：把 `raise 403` 改成 `pass`，禁止分支的测试必须转红。
"""

import os

from app import security as _security
from app.api import reschedule as R


def _admin_token(client):
    login = client.post("/api/reschedule/admin-login",
                        json={"username": _security.ADMIN_USER, "password": _security.ADMIN_PASS})
    assert login.status_code == 200, login.text
    return login.json()["token"]


def _gate_detail():
    return "演示复位仅在 DEMO_MODE=1 时可用"


# ---------------------------------------------------------------------------
# 1. 闸门：非演示模式必须拒绝（543-544）
# ---------------------------------------------------------------------------

def test_demo_reset_forbidden_when_demo_mode_off(monkeypatch):
    """DEMO_MODE 不是 "1" 时，哪怕拿着合法管理员令牌也必须 403，且文案点名闸门。"""
    from fastapi.testclient import TestClient
    from app.main import app

    # ⚠️ R-123：必须先拿令牌、**再**伪装 DEMO_MODE，两步顺序不能反。
    #
    # 原写法（先伪装再 `_admin_token`）注释里说"auth 依赖走自己的 os，不受影响"——
    # 那句是错的：`security.is_demo_mode()` 就是 `os.getenv("DEMO_MODE")`，
    # 而 `R.os` 与 `security.os` 是**同一个模块对象**，`setattr(R.os, "getenv", ...)`
    # 是全局替换。于是登录时 `is_demo_mode()` 读到 "0" ⇒ 默认口令被拒 ⇒ 403 ⇒
    # `_admin_token` 的 `assert 200` 先炸，根本走不到要测的闸门。
    #
    # 它以前能过，是因为本机 `.env` 里 `ADMIN_PASS=Psa@2026`（**非默认值**），
    # 此时 `admin_password_is_default()` 为 False，`DEMO_MODE` 是多少都不影响登录。
    # 一旦测试进程统一用默认口令（conftest 的基线），这个隐含依赖就暴露了。
    real_getenv = os.getenv

    with TestClient(app) as client:
        token = _admin_token(client)  # 真实 DEMO_MODE=1 下拿到的合法令牌

        def fake_getenv(key, default=None):
            if key == "DEMO_MODE":
                return "0"
            return real_getenv(key, default)

        monkeypatch.setattr(R.os, "getenv", fake_getenv)
        try:
            resp = client.post("/api/reschedule/demo/reset",
                               headers={"Authorization": f"Bearer {token}"})
        finally:
            monkeypatch.undo()

    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"] == _gate_detail(), resp.text


# ---------------------------------------------------------------------------
# 2. 演示模式下允许复位（545-557）
# ---------------------------------------------------------------------------

def test_demo_reset_succeeds_in_demo_mode(tmp_path, monkeypatch):
    """DEMO_MODE=1（测试进程默认）时，带令牌调用必须真的复位成功。

    隔离：请求库落到临时 sqlite；`_load_default_schedule` 桩掉（避免重载真实大表
    带来的外部依赖与耗时），本测试只验证「闸门放行 + 清空 + 回成功」这条契约。
    """
    from fastapi.testclient import TestClient
    from app.main import app

    monkeypatch.setattr(R, "_DB_PATH", str(tmp_path / "requests.db"))
    R._init_db()
    monkeypatch.setattr(R, "_load_default_schedule", lambda: None)  # 桩：避免重载大表

    with TestClient(app) as client:
        token = _admin_token(client)
        resp = client.post("/api/reschedule/demo/reset",
                           headers={"Authorization": f"Bearer {token}"})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["success"] is True, body
    assert "演示已复位" in body["message"], body["message"]
