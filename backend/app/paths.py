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
    r"""这个路径能不能当数据目录用。

    R-113：原实现只有"父目录或自身存在"一条判据：
        parent = os.path.dirname(path.rstrip("/\\")) or os.sep
        return os.path.isdir(parent) or os.path.isdir(path)

    它在 Linux 上没问题，在 **Windows 上会把 POSIX 绝对路径误判为可用**，后果是
    数据被写到一个谁也不知道的目录：

        PSA_DATA_DIR=/home/user/psa-data        # .env 里的沙箱配置，随包分发
        os.path.isdir("/home")        -> True   # Windows 把它当"当前盘符下的 \home"
        os.path.isabs("/home/user")   -> False  # 因为根是 E:\
        os.path.abspath(...)          -> E:\home\user\psa-data

    实测（2026-11-07）：本地跑测试时，它在盘根建出了 `E:\home\user\psa-data`。
    `.env` 第 29 行的注释写着"本机 Windows 不存在 /home/user，会自动回落到
    backend/app/data"——**设计意图正确，实现没做到**。而这条路径一旦生效，
    线上/本地数据就落到盘根，重发或换机启动都会"数据消失"，且当天看不出来。

    修法：额外要求"该值本身是当前平台认可的绝对路径"。
    这样 `/home/user` 在 Windows 上被判不可用 ⇒ 按设计回退到包内 data；
    在 Linux 上仍照常可用，沙箱行为不变。
    """
    # 平台一致性前置：Windows 不认 POSIX 绝对路径（`/foo` 不是绝对路径）
    if not os.path.isabs(path):
        return False
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
