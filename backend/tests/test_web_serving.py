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
    body = r.json()
    assert body["status"] == "ok"
    # 深度探活：进程活着 ≠ 数据在内存（R-18 的成因），必须能被外部看到
    assert body["data"]["loaded"] is False or body["data"]["loaded"] is True
    assert body["data"]["total_entries"] >= 0
    assert body["uptime_s"] >= 0
    # 预热状态可见：pending/running/ok/failed 之一（TestClient 不起 lifespan，故常为 pending）
    assert body["solver"]["status"] in {"pending", "running", "ok", "failed", "disabled", "unexpected"}


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
    assert r.text[:2] != "PK"


# ---------------------------------------------------------------------------
# 传输层：压缩 + 缓存
# ---------------------------------------------------------------------------

def test_hashed_assets_are_gzipped():
    """主包 ~430KB，压缩后 ~132KB。没压缩 = 每次冷加载多传 ~300KB。"""
    js_files = list((FRONTEND_DIST / "assets").glob("*.js"))
    if not js_files:
        pytest.skip("未找到 JS 产物")
    r = client.get(f"/assets/{js_files[0].name}", headers={"accept-encoding": "gzip"})
    assert r.status_code == 200
    assert r.headers.get("content-encoding") == "gzip"


def test_large_json_api_is_gzipped():
    """概览接口 13.9KB → 1.6KB（-88%），对弱网首屏影响最大。"""
    client.post("/api/reschedule/load-default")
    r = client.get(
        "/api/reschedule/parallel/overview", headers={"accept-encoding": "gzip"}
    )
    assert r.status_code == 200
    assert r.headers.get("content-encoding") == "gzip"


def test_hashed_assets_are_immutable_cached():
    js_files = list((FRONTEND_DIST / "assets").glob("*.js"))
    if not js_files:
        pytest.skip("未找到 JS 产物")
    r = client.get(f"/assets/{js_files[0].name}")
    assert r.headers.get("cache-control") == "public, max-age=31536000, immutable"


def test_index_html_must_revalidate():
    """index.html 不能被强缓存：否则重发布后可能引用已删除的旧哈希资源 → 白屏。"""
    for path in ("/", "/some/deep/frontend/route"):
        r = client.get(path)
        assert r.status_code == 200
        assert "no-cache" in r.headers.get("cache-control", "")


# ---------------------------------------------------------------------------
# 可观测性：耗时头 + 进程内计数
# ---------------------------------------------------------------------------

def test_every_response_carries_timing_header():
    r = client.get("/health")
    assert r.headers.get("x-response-time-ms", "").isdigit()


def test_counters_track_requests_and_client_errors():
    from app.main import metrics

    before = metrics()
    client.get("/health")               # 200 → requests+1，errors 不变
    client.post("/health")              # 405 → errors+1（方法不允许）
    after = metrics()

    assert after["requests"] >= before["requests"] + 2
    assert after["errors"] >= before["errors"] + 1
