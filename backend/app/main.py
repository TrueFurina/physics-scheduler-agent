"""物理实验调课辅助系统 - 后端入口

部署形态说明：
- 开发态：前端跑在 Vite(:5173)，通过 vite.config.ts 的 proxy 把 /api 转发到本服务(:8000)。
- 发布态：本服务**同时托管前端构建产物**（frontend/webui），前后端同源，
  前端 `API_BASE = '/api'` 相对路径无需任何改动即可工作，也不再依赖 CORS。
"""

import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api import reschedule
from app.engine import warmup

_STARTED_AT = time.time()

# ---------------------------------------------------------------------------
# 轻量可观测性：计数 + 慢请求/错误日志
#
# 为什么不用 `@app.middleware("http")`：那是 BaseHTTPMiddleware，会用额外任务
# 包住整个响应流，对 StaticFiles 的流式文件响应有性能与语义副作用（缓冲、
# 背景任务时序）。这里写成纯 ASGI 中间件，只旁听 `http.response.start`，
# 不改动任何响应体流转。
# ---------------------------------------------------------------------------
_COUNTERS = {"requests": 0, "errors": 0, "slow": 0, "throttled": 0}
_SLOW_MS = int(os.getenv("SLOW_REQUEST_MS", "1000") or 1000)
_http_log = logging.getLogger("app.http")


class _ObserveMiddleware:
    """记录耗时与状态码；只对**慢请求和 4xx/5xx** 打日志，避免正常访问刷屏。"""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        started = time.perf_counter()
        info = {"status": 500}

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                info["status"] = message["status"]
                elapsed_ms = int((time.perf_counter() - started) * 1000)
                message.setdefault("headers", [])
                message["headers"].append((b"x-response-time-ms", str(elapsed_ms).encode()))
            await send(message)

        await self.app(scope, receive, send_wrapper)

        elapsed_ms = int((time.perf_counter() - started) * 1000)
        status = info["status"]
        _COUNTERS["requests"] += 1
        if status >= 400:
            _COUNTERS["errors"] += 1
        if status == 429:
            _COUNTERS["throttled"] += 1
        if elapsed_ms >= _SLOW_MS:
            _COUNTERS["slow"] += 1

        if status >= 400 or elapsed_ms >= _SLOW_MS:
            _http_log.warning(
                '{"method":"%s","path":"%s","status":%d,"ms":%d}',
                scope.get("method", ""), scope.get("path", ""), status, elapsed_ms,
            )


def metrics() -> dict:
    return dict(_COUNTERS)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """进程启动钩子：后台预热 CP-SAT。

    放在 lifespan 而不是模块导入期，是为了让"导入 main"（测试、静态检查）
    不产生任何后台线程；只有真正起服务时才预热。
    """
    warmup.start_background()
    yield

# backend/app/main.py → parents[0]=backend/app, [1]=backend, [2]=仓库根
REPO_ROOT = Path(__file__).resolve().parents[2]
# 前端产物目录名刻意不叫 dist：发布上传会排除「构建产物」（node_modules/.git/dist…），
# 而本项目由 FastAPI 同源托管，产物必须随源码进沙箱 → 构建到 frontend/webui。
FRONTEND_DIST = Path(os.getenv("FRONTEND_DIST") or (REPO_ROOT / "frontend" / "webui"))

# 发布态关闭交互式文档。
#
# 为什么：`/openapi.json` 会把全部接口（含写接口）连同参数结构公网列出，
# 等于附赠一份利用说明书；而本项目发布后并不需要在线 Swagger。
# 本地开发仍要文档，所以由环境变量控制——发布入口 `run.py` 会默认置位，
# 本地 `uvicorn app.main:app` 直起时保持开启。
_DOCS_DISABLED = os.getenv("DISABLE_DOCS", "").strip().lower() in {"1", "true", "yes", "on"}

app = FastAPI(
    title="物理实验调课辅助系统",
    description="上传实验安排表 → AI筛选空闲时段 → 教师选择 → 管理员审批",
    version="0.1.0",
    lifespan=lifespan,
    docs_url=None if _DOCS_DISABLED else "/docs",
    redoc_url=None if _DOCS_DISABLED else "/redoc",
    openapi_url=None if _DOCS_DISABLED else "/openapi.json",
)

# 传输压缩：注册顺序决定洋葱结构——后注册的在外层。
# 故 GZip 先注册（内层），CORS 后注册（外层）：保证压缩失败/异常时响应头仍带 CORS。
#
# 为什么需要：前端主包 ~430KB，gzip 后 ~132KB（实测 31%）。
# 首屏在弱网/跨区链路上主要成本就是这一个文件，压缩是零风险的净收益。
app.add_middleware(GZipMiddleware, minimum_size=1024)

# 最外层：观测耗时/状态码（纯 ASGI，见 _ObserveMiddleware 注释）
app.add_middleware(_ObserveMiddleware)

# CORS 仅服务于开发态（Vite dev server）。发布态同源，不经过 CORS。
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:3000",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 只注册调课相关的路由
app.include_router(reschedule.router, prefix="/api/reschedule", tags=["调课管理"])


@app.get("/health")
def health_check():
    """探活。

    只报状态、**不触发数据加载**（探活必须廉价，且不能让外部轮询把沙箱算力烧掉）。

    `data.loaded` 用来区分两种"看起来都正常"：
    - `loaded=true`  → 数据在内存，可正常服务
    - `loaded=false` → 进程活着但内存被回收（正是 R-18 的成因）。
      此时首个数据接口会走 `_ensure_loaded()` 自愈，但**外部监控应能看到**这个状态，
      而不是被一个永远 `{"status":"ok"}` 的探活掩盖过去。
    """
    return {
        "status": "ok",
        "uptime_s": int(time.time() - _STARTED_AT),
        "data": reschedule.data_state(),
        # pending/running/ok/failed —— 判断"首次排课慢"是否因为预热没跑成
        "solver": warmup.state(),
        # 进程内累计（重启清零）：total / errors(>=400) / slow(>=SLOW_REQUEST_MS) / throttled(429)
        "http": metrics(),
    }


class _HashedAssetFiles(StaticFiles):
    """Vite 产物文件名自带内容哈希（如 `index-DRvktcTZ.js`）⇒ 内容与名一一对应。

    因此可以给一年 `immutable` 强缓存：二次访问直接命中本地缓存、零网络往返；
    一旦重新构建，哈希变化就是新 URL，不存在"旧文件被误用"的窗口。
    Starlette 默认只发 ETag/Last-Modified，浏览器每次仍要发一次条件请求（304）。
    """

    async def get_response(self, path, scope):  # noqa: D102 - 仅追加响应头
        response = await super().get_response(path, scope)
        if response.status_code in (200, 304):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response


def _index_response() -> FileResponse:
    """返回 SPA 入口，并显式要求**每次使用前重新校验**。

    若 index.html 被浏览器启发式缓存，重新发布后可能出现
    "旧 index 引用已删除的旧哈希资源" → 白屏。`no-cache` + ETag
    让绝大多数请求以 304 结束（几十字节），代价可忽略。
    """
    return FileResponse(_DIST_ROOT / "index.html", headers={"Cache-Control": "no-cache"})


# ---------------------------------------------------------------------------
# 前端静态资源（同源托管）
#
# 注册顺序很重要：本段必须放在最后，/api/**、/health、/docs、/openapi.json
# 都已在此之前注册，会优先匹配；这里的兜底路由只接住前端路由。
# ---------------------------------------------------------------------------
if FRONTEND_DIST.is_dir() and (FRONTEND_DIST / "index.html").is_file():
    _DIST_ROOT = FRONTEND_DIST.resolve()
    _assets_dir = FRONTEND_DIST / "assets"
    if _assets_dir.is_dir():
        app.mount("/assets", _HashedAssetFiles(directory=str(_assets_dir)), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa_fallback(full_path: str):
        """命中真实文件就返回文件，否则回落到 index.html 交给前端路由。

        路径穿越防护：解析后的路径必须仍在 dist 目录内，否则一律回落。
        """
        if full_path:
            candidate = (_DIST_ROOT / full_path).resolve()
            try:
                candidate.relative_to(_DIST_ROOT)
            except ValueError:
                return _index_response()
            if candidate.is_file():
                return FileResponse(candidate)
        return _index_response()

else:

    @app.get("/")
    def root():
        """尚未构建前端时的退化行为：只暴露 API 说明。"""
        return {
            "message": "物理实验调课辅助系统 API",
            "version": "0.1.0",
            "usage": "POST /api/reschedule/upload 上传实验安排表 → POST /api/reschedule/chat 调课咨询",
            "hint": "前端未构建：请先执行 `cd frontend && npm run build`，或访问 /docs 查看接口。",
        }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "8000")),
    )
