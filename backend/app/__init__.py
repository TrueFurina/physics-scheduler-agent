"""app 包初始化：在**任何子模块导入之前**把仓库根的配置文件读进 `os.environ`。

为什么放在这里
--------------
下列配置都是在**模块导入时**求值的：

    app/security.py    ADMIN_USER / ADMIN_PASS / APP_SECRET_KEY / ADMIN_TOKEN_TTL
    app/agent/llm_intent_parser.py  LLM_PROVIDER / *_API_KEY

而 `import app.security` 一定会先执行 `app/__init__.py`，所以这里是唯一
「早于一切、且不依赖调用顺序」的注入点。放在别处（比如 run.py）会漏掉
`pytest`、`uvicorn app.main:app` 这类不走 run.py 的入口。

为什么不用 pydantic-settings 自带的 env_file
--------------------------------------------
它的 `env_file = ".env"` 是**相对当前工作目录**解析的：从仓库根跑是 `/.env`，
从 `backend/` 跑就变成 `backend/.env`，沙箱里启动目录又可能是第三种。
按「仓库根」绝对定位才能让三个入口行为一致。

配置优先级（高 → 低）
---------------------
1. 进程环境变量（沙箱/CI 注入、`setx` 设的用户变量）
2. `<仓库根>/.env`
3. `<仓库根>/env.local`   ← 同一份内容的镜像，见下方说明

为什么会有两个文件
------------------
发布上传器**不读 .gitignore**（已被 `backend/app/data/requests.db` 随包上传
实测证实），所以 gitignore 只能防 git、防不住上传。但上传器的**排除规则未知**，
`.env` 这种名字有可能被当作"密钥文件"跳过。因此把同一份配置同时写成
`.env`（主）与 `env.local`（镜像），二者内容由
`scripts/write_local_config.py` 统一生成，任一到位即可生效。
**两者都必须留在 .gitignore 里，绝不进 git。**
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import List

# backend/app/__init__.py → parents[0]=backend/app, [1]=backend, [2]=仓库根
REPO_ROOT = Path(__file__).resolve().parents[2]

CONFIG_FILES = (".env", "env.local")


def _load_config_files() -> List[str]:
    """把仓库根的配置文件灌进 os.environ（已存在的变量不覆盖）。

    `override=False` 是有意的：真实环境变量代表"更高优先级的注入"，
    不能让一个随包上传的文件把它们顶掉——否则沙箱里想临时改配置就得改文件。
    """
    try:
        from dotenv import load_dotenv
    except ImportError:  # 极简环境下退化为"只认环境变量"
        return []

    loaded: List[str] = []
    for name in CONFIG_FILES:
        path = REPO_ROOT / name
        if path.is_file():
            load_dotenv(path, override=False, encoding="utf-8")
            loaded.append(name)
    return loaded


LOADED_CONFIG_FILES: List[str] = _load_config_files()

# 诊断用：只暴露"读到了哪个文件"，不暴露任何值。
if os.getenv("APP_CONFIG_DEBUG", "").strip().lower() in {"1", "true", "yes", "on"}:
    print(f"[app] 已加载配置文件: {LOADED_CONFIG_FILES or '（无，仅用环境变量）'}")
