"""发布态统一启动入口。

为什么需要这个文件：
- sites 沙箱只给一个公网端口，并以 **PORT 环境变量**注入，要求服务绑定 0.0.0.0。
- 但沙箱执行启动命令时**不会替我们展开 `$PORT`**（实测 `--port $PORT` 会得到空值，
  uvicorn 报 `Option '--port' requires an argument`），所以端口必须由进程自己读。
- 仓库根不是 Python 包根（应用包位于 `backend/app`），直接 `python -m uvicorn app.main:app`
  在根目录会 import 失败，因此这里显式把 `backend/` 加入 `sys.path`。

本地用法：
    python run.py                # 默认 8000
    PORT=9000 python run.py      # 指定端口
    SCHEDULE_PATH=schedule.xlsx python run.py   # 显式指定数据表（默认已优先真实表）

数据表解析顺序（见 backend/app/api/reschedule.py::_resolve_default_schedule_path）：
    SCHEDULE_PATH 环境变量 → 仓库根 schedule.xlsx → schedule.demo.xlsx
真实表缺失时自动回退脱敏表。

环境变量：
    DISABLE_DOCS=1        关闭 /docs、/redoc、/openapi.json（本文件默认置位）
    APP_SECRET_KEY=...    管理员会话签名密钥。不设则每次启动随机生成，
                          沙箱重启后管理员需重新登录；需要跨重启保持会话时注入。
    ADMIN_USER / ADMIN_PASS   管理员凭据，默认 admin / admin123
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "backend"))

# 发布态默认关闭交互式文档（/docs、/redoc、/openapi.json）。
# 必须在 import app.main 之前置位：FastAPI() 在 import 时就固化了 docs_url/openapi_url。
# 本地开发直接 `uvicorn app.main:app` 不经过本文件，文档保持可用。
os.environ.setdefault("DISABLE_DOCS", "1")

import uvicorn  # noqa: E402

from app.main import app  # noqa: E402


def main() -> None:
    uvicorn.run(
        app,
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "8000")),
    )


if __name__ == "__main__":
    main()
