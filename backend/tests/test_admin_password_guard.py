"""默认管理员口令的边界（2026-10-26 实测事故）

线上 `POST /admin-login {"admin","admin123"}` 直接换到了管理员 token —— 任何拿到
链接的人都能审批、落地方案、清清单。根因不是"口令太弱"，而是**默认值被当成了
可用口令**：代码里 `ADMIN_PASS` 的缺省值是 `admin123`，部署又不会配环境变量。

修法：默认口令只在 `DEMO_MODE=1`（本地演示）下有效；生产必须显式配置 ADMIN_PASS。
本文件锁三件事：
  1. 生产 + 默认口令 → 拒绝；
  2. 演示模式 + 默认口令 → 允许（本地演示脚本不变）；
  3. 生产 + 自定义口令 → 允许。
"""

import asyncio
import os
import sys

import pytest
from fastapi import HTTPException

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import app.api.reschedule as api
import app.security as security


def test_default_password_rejected_outside_demo(monkeypatch):
    monkeypatch.setattr(security, "ADMIN_PASS", "admin123")
    monkeypatch.delenv("DEMO_MODE", raising=False)
    assert security.check_admin_credentials("admin", "admin123") is False


def test_default_password_allowed_in_demo(monkeypatch):
    monkeypatch.setattr(security, "ADMIN_PASS", "admin123")
    monkeypatch.setenv("DEMO_MODE", "1")
    assert security.check_admin_credentials("admin", "admin123") is True


def test_custom_password_works_without_demo(monkeypatch):
    monkeypatch.setattr(security, "ADMIN_PASS", "S3cure-Pass!")
    monkeypatch.delenv("DEMO_MODE", raising=False)
    assert security.check_admin_credentials("admin", "S3cure-Pass!") is True
    assert security.check_admin_credentials("admin", "admin123") is False


def test_login_endpoint_says_why_it_refuses(monkeypatch):
    """生产未配口令要回 403 + 可执行的原因，而不是含糊的"密码错误"。"""
    monkeypatch.setattr(security, "ADMIN_PASS", "admin123")
    monkeypatch.delenv("DEMO_MODE", raising=False)
    with pytest.raises(HTTPException) as ei:
        asyncio.run(api.admin_login(api.AdminLoginRequest(username="admin", password="admin123")))
    assert ei.value.status_code == 403
    assert "ADMIN_PASS" in ei.value.detail
