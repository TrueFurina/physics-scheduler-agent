"""API 命名空间不许被前端兜底吞掉（R52）

缺陷（本轮实测，修复前）
------------------------
`main.py` 的 SPA 兜底路由是 `@app.get("/{full_path:path}")`，会接住**任何** GET。
于是：

```
GET /api/reschedule/parallel/ladn        -> 200  text/html   '<!DOCTYPE html>...'
GET /api/reschedule/not-a-real-endpoint  -> 200  text/html   '<!DOCTYPE html>...'
GET /api/nothing-here                    -> 200  text/html   '<!DOCTYPE html>...'
GET /api/reschedule/upload               -> 200  text/html   （只支持 POST 的写接口）
GET /api/reschedule/master/init          -> 200  text/html   （管理员写接口！）
```

三层后果，一层比一层难查：

1. **排查方向被带偏。** 前端 `r.json()` 抛 `Unexpected token '<'`。看到这个报错，
   第一反应是"响应被 HTML 污染了/代理配错了"，而真相是"这个路径根本不存在"。
2. **监控看不见。** 日志里 `"status":200`，`_COUNTERS["errors"]` 不增，
   `app.http` 也不打日志（它只记 4xx/5xx 与慢请求）。这批请求对运维完全隐形 ——
   与 R35「时段看不懂就整行静默丢弃」、R39「删默认课程返回成功但什么都没删」同族。
3. **写接口被误读成成功。** `GET /master/init` 返回 200，任何人（包括抓包看到
   路径的人）都会以为初始化执行了。实际什么都没发生。

判据
----
- `/api` 与 `/api/**` 下的**一切**响应必须是 JSON，且不存在的路径必须是 404。
  没有"回落到前端页面"这个例外 —— 那不是前端路由，是 API 命名空间。
- 非 `/api` 的深链接仍必须回落 index.html（SPA 的正常行为，别修坏了）。
- 这批 404 必须计入 `errors` 计数器（可观测性：能被看见才算留痕）。

前两条一个是修、一个是**反向保护**：这类"兜底路由"改坏的典型方式是顺手把
SPA 兜底一起干掉，表现为刷新页面白屏 —— 线上比原缺陷更严重。
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi.testclient import TestClient

from app.main import FRONTEND_DIST, app, metrics

# 兜底路由只有在「前端产物存在」时才注册（见 main.py 末尾的 if）。
# 产物不存在时它退回一个只讲 API 说明的 `/`，本文件全部用例都不适用。
pytestmark = pytest.mark.skipif(
    not (FRONTEND_DIST / "index.html").is_file(),
    reason="前端产物未构建（frontend/webui 不存在），SPA 兜底路由未注册",
)

client = TestClient(app)

# 本轮实测出来的四类"被吞"的路径
SWALLOWED_BEFORE = [
    "/api/reschedule/parallel/ladn",          # 拼错（land → ladn）
    "/api/reschedule/not-a-real-endpoint",    # 完全不存在
    "/api/reschedule/upload",                 # 只支持 POST 的写接口
    "/api/reschedule/master/init",            # 管理员写接口（回 200 最危险）
    "/api",                                   # 命名空间本身
]


@pytest.mark.parametrize("path", SWALLOWED_BEFORE)
def test_api_namespace_never_falls_back_to_html(path):
    """不存在的 API 路径必须是 404 JSON —— 不许回落到 index.html。"""
    r = client.get(path)

    assert r.status_code == 404, f"{path} 仍返回 {r.status_code}（修复前这里是 200 + HTML）"
    assert "application/json" in r.headers.get("content-type", ""), \
        f"{path} 返回了非 JSON：{r.headers.get('content-type')}"
    assert "接口不存在" in r.json().get("detail", ""), r.text[:200]
    assert "<!DOCTYPE html>" not in r.text


def test_registered_get_api_still_works():
    """对照组：真的存在且是 GET 的接口，必须照常 200 JSON。

    防的是"把所有 /api 请求都挡成 404"这种矫枉过正 —— 那会让整个系统不可用。
    """
    r = client.get("/api/reschedule/status")

    assert r.status_code == 200
    assert "application/json" in r.headers.get("content-type", "")
    assert "loaded" in r.json()


def test_frontend_deep_link_still_falls_back_to_index():
    """反向保护：非 /api 的前端深链接必须继续回落 index.html。

    SPA 兜底是**功能**，不是 bug。改这个缺陷最危险的走法是把兜底一起收掉，
    表现为"刷新页面白屏" —— 比原缺陷严重得多。
    """
    r = client.get("/some/deep/frontend/route")

    assert r.status_code == 200
    assert "text/html" in r.headers.get("content-type", "")
    assert 'id="root"' in r.text


def test_root_still_serves_spa():
    """反向保护：`/` 仍是前端入口。"""
    r = client.get("/")

    assert r.status_code == 200
    assert "text/html" in r.headers.get("content-type", "")


def test_preexisting_assets_still_served():
    """反向保护：兜底路由里"命中真实文件就返回文件"的分支不能被短路掉。"""
    index = FRONTEND_DIST / "index.html"
    r = client.get("/index.html")

    assert r.status_code == 200
    assert r.content == index.read_bytes()


def test_swallowed_api_paths_are_counted_as_errors():
    """可观测性：这批请求必须计入 errors 计数器（修复前 status=200，一个都不计）。

    这条不是"锦上添花的指标"，而是本缺陷最难发现的那一层：
    日志和计数器都说"一切正常"，出问题的请求在监控上是隐形的。
    """
    before = metrics()["errors"]
    client.get("/api/reschedule/parallel/ladn")
    after = metrics()["errors"]

    assert after == before + 1, f"errors 计数器没涨：{before} → {after}"
