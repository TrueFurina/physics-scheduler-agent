"""物理实验调课辅助系统 - 后端入口

部署形态说明：
- 开发态：前端跑在 Vite(:5173)，通过 vite.config.ts 的 proxy 把 /api 转发到本服务(:8000)。
- 发布态：本服务**同时托管前端构建产物**（frontend/webui），前后端同源，
  前端 `API_BASE = '/api'` 相对路径无需任何改动即可工作，也不再依赖 CORS。
"""

import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api import reschedule

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
    docs_url=None if _DOCS_DISABLED else "/docs",
    redoc_url=None if _DOCS_DISABLED else "/redoc",
    openapi_url=None if _DOCS_DISABLED else "/openapi.json",
)

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
    return {"status": "ok"}


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
        app.mount("/assets", StaticFiles(directory=str(_assets_dir)), name="assets")

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
                return FileResponse(_DIST_ROOT / "index.html")
            if candidate.is_file():
                return FileResponse(candidate)
        return FileResponse(_DIST_ROOT / "index.html")

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
