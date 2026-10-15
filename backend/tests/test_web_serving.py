"""同源静态托管（发布形态）回归测试。

锁定 backend/app/main.py 新增的行为：
  - /                → 前端 SPA 的 index.html（而非 JSON）
  - 未知前端路由      → 回落 index.html（SPA fallback）
  - /assets/**       → 真实静态资源
  - /health、/api/**、/docs、/openapi.json → 不被兜底路由吞掉
  - 路径穿越         → 一律回落 index.html，绝不吐出仓库内文件
"""

import os
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.main import FRONTEND_DIST, app

client = TestClient(app)

# 前端产物缺失时这些用例无意义（例如未执行 npm run build）
pytestmark = pytest.mark.skipif(
    not (FRONTEND_DIST / "index.html").is_file(),
    reason="前端未构建（先执行 cd frontend && npm run build）",
)


def test_root_serves_spa():
    r = client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert 'id="root"' in r.text


def test_unknown_route_falls_back_to_index():
    r = client.get("/some/deep/frontend/route")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert 'id="root"' in r.text


def test_health_not_swallowed():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_api_not_swallowed():
    r = client.get("/api/reschedule/status")
    assert r.status_code == 200
    assert "application/json" in r.headers["content-type"]
    assert "loaded" in r.json()


def test_docs_and_openapi_reachable():
    assert client.get("/docs").status_code == 200
    assert client.get("/openapi.json").status_code == 200


def test_assets_are_served():
    assets_dir = FRONTEND_DIST / "assets"
    js_files = list(assets_dir.glob("*.js"))
    if not js_files:
        pytest.skip("未找到 JS 产物")
    r = client.get(f"/assets/{js_files[0].name}")
    assert r.status_code == 200
    assert "javascript" in r.headers["content-type"]


@pytest.mark.parametrize(
    "path",
    [
        "/../backend/requirements.txt",
        "/%2e%2e%2fbackend%2frequirements.txt",
        "/..%2f..%2fbackend%2frequirements.txt",
        "/../../schedule.xlsx",
    ],
)
def test_path_traversal_is_blocked(path):
    """穿越尝试必须回落到 index.html，绝不能返回仓库内文件内容。"""
    r = client.get(path)
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    # 反向验证：没有泄露后端依赖清单或 Excel 的二进制内容
    assert "fastapi==" not in r.text
    assert "PK" != r.text[:2]
