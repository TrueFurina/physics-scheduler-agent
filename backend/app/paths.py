"""运行期数据目录（sqlite / 主数据落盘 / 解析缓存）

为什么单独抽出来
----------------
发布器每次都会把仓库整目录上传，`backend/app/data/` 里的 `requests.db`、
`master_store.json` 会被**本地那份覆盖**——线上改过的工单、采纳的方案、主数据修改，
一次重发就回到解放前（2026-10-26 用户要求"线上数据必须持久"）。

做法：数据目录可用 `PSA_DATA_DIR` 指到**上传包之外**（沙箱里是 `/home/user/psa-data`）。
判据不是"有没有配置"，而是"父目录是否存在"：
  - 沙箱：`/home/user` 存在 → 用它，重发不覆盖；
  - 本机 Windows：`/home/user` 不存在 → 自动回落到 `backend/app/data`，本地开发零改动。
"""

import os

_APP_DIR = os.path.dirname(os.path.abspath(__file__))


def _usable(path: str) -> bool:
    parent = os.path.dirname(path.rstrip("/\\")) or os.sep
    return os.path.isdir(parent) or os.path.isdir(path)


def resolve_data_dir() -> str:
    env = (os.getenv("PSA_DATA_DIR") or "").strip()
    if env:
        path = os.path.expanduser(env)
        if _usable(path):
            try:
                os.makedirs(path, exist_ok=True)
                return path
            except OSError:
                pass
    return os.path.join(_APP_DIR, "data")


DATA_DIR = resolve_data_dir()
