"""`paths.resolve_data_dir()` 的分支契约（R-113）。

为什么这个只有 16 行的模块值得单开一个测试文件
----------------------------------------------
它是**唯一**决定"线上数据放哪"的地方。发布器每次重发都会整目录上传，
如果数据目录落在 `backend/app/data`（包内），线上改过的工单、采纳的方案、
主数据修改**一次重发就回到解放前**——这是 2026-10-26 用户明确要求修掉的问题
（"线上数据必须持久"）。

覆盖率实测（R-113，CI 同口径）显示 `backend/app/paths.py` 只有 **75%**
（11/16 行），是全仓唯一低于 80% 的文件。未覆盖的正是那几条**分支**：

    if env:                     # PSA_DATA_DIR 配了没
        if _usable(path):       # 父目录在不在
            try: os.makedirs   # 建得出来吗
            except OSError: pass   # 建不出来怎么办

而"分支写反"在这类代码里的后果不是报错，是**静默写错地方**：
线上把数据写进包内目录，重发一次全丢，而且当天看不出来——直到下次发布。
这种失败模式没法靠"跑一遍看看"发现，只能靠把每条分支钉住。

判据不是"函数返回了什么"，而是"给定环境时，它必须/不得落到包内 data"。
"""

import importlib
import os
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent


def _fresh_paths():
    """重新导入 `app.paths`，让模块级的 `DATA_DIR = resolve_data_dir()` 重算。

    必须这么做：`DATA_DIR` 在 import 时就定死了，直接调函数测不到
    "import 那一刻用了哪个分支"——而线上出错恰恰是那一刻。
    """
    sys.path.insert(0, str(BACKEND))
    if "app.paths" in sys.modules:
        del sys.modules["app.paths"]
    return importlib.import_module("app.paths")


def test_defaults_inside_package_when_env_absent(tmp_path, monkeypatch):
    """没配 PSA_DATA_DIR 时，必须回落到包内 data（本地开发零改动）。"""
    monkeypatch.delenv("PSA_DATA_DIR", raising=False)
    m = _fresh_paths()
    assert os.path.join(str(BACKEND / "app"), "data") == m.DATA_DIR


def test_env_var_used_when_parent_exists(tmp_path, monkeypatch):
    """配了 PSA_DATA_DIR 且父目录存在时，必须用它，且**不得**落到包内。"""
    target = tmp_path / "psa-data"
    # 父目录存在（即 tmp_path），但 target 自身还不存在 —— 这是沙箱里的真实形态
    monkeypatch.setenv("PSA_DATA_DIR", str(target))
    m = _fresh_paths()

    assert str(target) == m.DATA_DIR, f"没使用 PSA_DATA_DIR：{m.DATA_DIR}"
    assert os.path.join(str(BACKEND / "app"), "data") != m.DATA_DIR, (
        "配了 PSA_DATA_DIR 却仍落到包内 data —— 线上重发会覆盖数据"
    )
    # 目录应当被建出来，否则后续写入会失败
    assert target.is_dir(), "resolve_data_dir 应当 makedirs 出目标目录"


def test_env_var_ignored_when_parent_missing(tmp_path, monkeypatch):
    """配了 PSA_DATA_DIR 但父目录不存在时，必须回退到包内 data。

    这条对应 Windows 本机的情形：文档里举的例子是 `/home/user/psa-data`，
    而 `/home/user` 在 Windows 上不存在。若不校验父目录就直接 makedirs，
    会在盘根造出一个诡异的 `/home` 目录树，或者直接抛错让服务起不来。
    """
    missing = tmp_path / "no-such-parent" / "sub" / "psa-data"
    monkeypatch.setenv("PSA_DATA_DIR", str(missing))
    m = _fresh_paths()

    assert os.path.join(str(BACKEND / "app"), "data") == m.DATA_DIR, (
        f"父目录不存在时应回退包内 data，实际：{m.DATA_DIR}"
    )


def test_env_var_expanduser(monkeypatch):
    """`~` 必须展开成用户主目录，而不是被当成普通字符。

    为什么这条不能用 `assert "~" not in DATA_DIR` 判断（第一版就是这么写的，
    缺陷注入时发现它**抓不到问题**）：去掉 `os.path.expanduser` 后，
    `_usable("~/x")` 的父目录是字面量 `"~"`，它不存在 ⇒ 函数**回退到包内 data**
    ⇒ 返回值里当然没有 `~` ⇒ 断言恒真。测试红了是别的用例连带，它自己其实是假绿。

    正确判据是**行为**：配了 `~/xxx` 时，结果必须落在用户主目录下。
    这里不真的让函数建目录（会在主目录留垃圾），只比较路径前缀。
    """
    monkeypatch.setenv("PSA_DATA_DIR", "~/psa-data-probe-113")
    m = _fresh_paths()
    home = os.path.expanduser("~")
    assert os.path.normcase(m.DATA_DIR).startswith(os.path.normcase(home)), (
        f"`~` 没有被展开到用户主目录：DATA_DIR={m.DATA_DIR}，home={home}"
    )
    assert "~" not in m.DATA_DIR


def test_makedirs_failure_falls_back(tmp_path, monkeypatch):
    """`makedirs` 抛 OSError 时必须回退，不能让 import 直接崩。

    场景：目标路径被一个**同名文件**占住（沙箱重挂载、权限错配都可能这样）。
    此时 `os.makedirs(..., exist_ok=True)` 抛 FileExistsError（OSError 子类），
    若不接住，`import app.paths` 就会失败 —— 整个服务起不来，
    而报错信息只会说"路径已存在"，完全看不出是数据目录的问题。
    """
    blocker = tmp_path / "blocked"
    blocker.write_text("i am a file, not a dir", encoding="utf-8")
    monkeypatch.setenv("PSA_DATA_DIR", str(blocker))
    m = _fresh_paths()

    assert os.path.join(str(BACKEND / "app"), "data") == m.DATA_DIR, (
        f"makedirs 失败时应回退包内 data，实际：{m.DATA_DIR}"
    )


def test_usable_accepts_existing_path_itself(tmp_path):
    """`_usable` 对"路径本身已是目录"也要返回真（不只是父目录存在）。"""
    m = _fresh_paths()
    existing = tmp_path / "already-there"
    existing.mkdir()
    assert m._usable(str(existing)) is True


def test_usable_rejects_both_missing(tmp_path):
    """父目录和自身都不存在时必须为假。"""
    m = _fresh_paths()
    assert m._usable(str(tmp_path / "a" / "b" / "c")) is False


def test_data_dir_is_absolute():
    """`DATA_DIR` 必须是绝对路径。

    相对路径会随**进程工作目录**漂移：从仓库根启动和从 `backend/` 启动
    会指向两个不同的 data 目录，表现为"刚写的工单不见了"。
    """
    m = _fresh_paths()
    assert os.path.isabs(m.DATA_DIR), f"DATA_DIR 是相对路径：{m.DATA_DIR}"


def test_posix_absolute_path_not_usable_on_windows(monkeypatch):
    r"""POSIX 绝对路径（`/home/user/...`）在 Windows 上必须判为不可用。← 真实缺陷

    这是写本文件时**顺手发现的现存 bug**（不是假想）：

        PSA_DATA_DIR=/home/user/psa-data    # .env:30 的沙箱配置，随包分发

    Windows 上：
        os.path.isdir("/home")       -> True    # 被当成"当前盘符下的 \home"
        os.path.isabs("/home/user")  -> False   # 根是 E:\，不是 /
        os.path.abspath(...)         -> E:\home\user\psa-data

    于是 `_usable()` 判真、`makedirs` 成功、`DATA_DIR` 指向 **E:\home\user\psa-data**。
    实测已经在盘根建出这三个空目录。而 `.env` 第 29 行的注释明确写着
    "本机 Windows 不存在 /home/user，会自动回落到 backend/app/data" ——
    意图是对的，实现漏了平台判断。

    后果不是报错，是**静默写到盘根**：数据看似正常，换机/重发就"数据消失"。
    """
    if os.name != "nt":
        pytest.skip("这条锁的是 Windows 的路径语义；Linux 上 /home 是合法绝对路径")

    monkeypatch.setenv("PSA_DATA_DIR", "/home/user/psa-data")
    m = _fresh_paths()

    assert os.path.join(str(BACKEND / "app"), "data") == m.DATA_DIR, (
        f"Windows 上应回退包内 data，实际落到了：{m.DATA_DIR}\n"
        "这会把数据写到盘根（如 E:/home/user/...），重发即丢失"
    )


def test_env_without_abs_path_falls_back(monkeypatch):
    """相对路径的 PSA_DATA_DIR 一律不可用（不依赖当前工作目录）。"""
    monkeypatch.setenv("PSA_DATA_DIR", "relative/psa-data")
    m = _fresh_paths()
    assert os.path.join(str(BACKEND / "app"), "data") == m.DATA_DIR, (
        f"相对路径应被拒绝，实际：{m.DATA_DIR}"
    )
    assert os.path.isabs(m.DATA_DIR)
