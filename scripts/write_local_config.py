"""生成仓库根的运行期配置文件（`.env` + `env.local`）。

用途
----
把「本机用户级环境变量里的密钥/配置」固化成两份等价文件，供以下场景使用：

  - 本地各种入口（`run.py` / `uvicorn app.main:app` / `pytest`）行为一致；
  - 发布到 sites 沙箱后，沙箱进程也能读到同一份配置
    （沙箱无法注入环境变量，只能靠随包上传的文件，见 `app/__init__.py` 的说明）。

安全约定
--------
1. 两份文件都在 `.gitignore` 内（`.env` / `.env.local`），**永不进 git**。
2. 脚本只从**环境变量**读密钥，密钥值不打印、不写日志。
3. `APP_SECRET_KEY` 一旦生成就复用现有值，避免每次重生成导致
   管理员会话在沙箱重启后失效。

用法
----
    python scripts/write_local_config.py            # 生成/更新
    python scripts/write_local_config.py --dry-run  # 只看会写哪些键，不落盘
"""

from __future__ import annotations

import argparse
import os
import secrets
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TARGETS = (REPO_ROOT / ".env", REPO_ROOT / "env.local")

HEADER = """\
# 物理实验调课辅助系统 — 运行期配置（由 scripts/write_local_config.py 生成）
#
# ⚠️ 本文件含明文密钥，已在 .gitignore 内（.env / .env.local），**不要提交**。
# ⚠️ 本文件同时是发布到沙箱时的配置载体（沙箱不能注入环境变量）。
#    修改配置请改脚本里的读取来源后重新生成，不要只改其中一份。
#
# 优先级：真实环境变量 > .env > env.local（见 backend/app/__init__.py）
"""


def _read_env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def _existing_secret_key() -> str:
    """优先复用已有的 APP_SECRET_KEY，保证管理员会话跨重启稳定。"""
    for path in TARGETS:
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("APP_SECRET_KEY="):
                value = line.split("=", 1)[1].strip()
                if value:
                    return value
    return ""


def build_config() -> tuple[str, list[str]]:
    """返回 (文件全文, 已填值的键名列表)。密钥值只在函数内流转，不外泄。"""
    provider = _read_env("LLM_PROVIDER") or "zhipu"
    zhipu_key = _read_env("ZHIPU_API_KEY")
    modelscope_key = _read_env("MODELSCOPE_API_KEY")
    deepseek_key = _read_env("DEEPSEEK_API_KEY")

    secret_key = _existing_secret_key() or secrets.token_hex(32)

    filled: list[str] = []

    def note(name: str, value: str) -> str:
        if value:
            filled.append(name)
            return value
        return ""

    lines = [
        HEADER.rstrip("\n"),
        "",
        "# --- LLM 渠道（后端 /chat 的意图解析与候选方案生成）---",
        "# 见 E:\\Program\\免费Token总账.md：智谱 glm-4-flash 永久免费不限量，作为主防线。",
        f"LLM_PROVIDER={provider}",
        "# 选填：显式覆盖渠道（留空则按 LLM_PROVIDER → 自动探测可用 key）",
        "LLM_API_BASE=",
        "LLM_API_KEY=",
        "LLM_MODEL=",
        "",
        f"ZHIPU_API_KEY={note('ZHIPU_API_KEY', zhipu_key)}",
        f"MODELSCOPE_API_KEY={note('MODELSCOPE_API_KEY', modelscope_key)}",
        f"DEEPSEEK_API_KEY={note('DEEPSEEK_API_KEY', deepseek_key)}",
        "",
        "# --- 管理员会话 ---",
        "# 不设 ADMIN_USER/ADMIN_PASS 时用代码默认值 admin / admin123（演示口令）。",
        "# 改口令：把下面两行注释去掉并填值即可（前端 UI 已不再公示口令）。",
        "# ADMIN_USER=admin",
        "# ADMIN_PASS=admin123",
        "#",
        "# 签名密钥：设了它，沙箱重启后管理员会话仍然有效；不设则每次启动随机生成。",
        f"APP_SECRET_KEY={secret_key}",
        "# 令牌有效期（秒），默认 8 小时",
        "ADMIN_TOKEN_TTL=28800",
        "",
    ]
    return "\n".join(lines), filled


def main() -> int:
    parser = argparse.ArgumentParser(description="生成仓库根运行期配置文件")
    parser.add_argument("--dry-run", action="store_true", help="只报告将写入的键，不落盘")
    args = parser.parse_args()

    content, filled = build_config()

    if args.dry_run:
        print("将写入以下键（值已隐藏）：")
        for name in ["LLM_PROVIDER", *filled, "APP_SECRET_KEY", "ADMIN_TOKEN_TTL"]:
            print(f"  - {name}")
        print(f"目标文件：{[str(p) for p in TARGETS]}")
        return 0

    for path in TARGETS:
        path.write_text(content, encoding="utf-8")
        print(f"写入 {path}（{len(content)} 字节）")

    missing = [k for k in ("ZHIPU_API_KEY", "MODELSCOPE_API_KEY", "DEEPSEEK_API_KEY") if k not in filled]
    if "ZHIPU_API_KEY" in missing:
        print("⚠️ 未读到 ZHIPU_API_KEY —— 后端 LLM 将不可用（检查用户级环境变量）", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
