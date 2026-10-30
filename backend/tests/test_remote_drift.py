"""推送前核对远端：本地副本落后时必须拦下来（R44）。

背景
----
R43 事故：本地 codeload 解包副本整体落后于远端，用旧版 `reschedule.py`
（2739 行）覆盖了远端 3280 行的当前版 —— 净损失 541 行、41 个函数/路由。
发现时已经推了 20 轮，而且每轮都"推送成功"。

根因不是某次手滑，是**流程缺一步核对**：

    读本地文件 → 改 → 整文件 PUT

在"远端没有别人的提交"时没问题；本地一落后，它就是**用旧版覆盖新版**，
且 GitHub 上看不出异常。

`scripts/check_remote_drift.py` 把那一步固化下来。本文件测它的判断逻辑。

为什么不用 mock 测网络层
------------------------
真正要锁住的是**判断规则**：
  · 本地行数比远端少          → 落后（高危）
  · 本地缺远端有的顶层函数    → 落后（一定是丢了东西）
  · 本地比远端多              → 正常（可能是新增的改动）

这三条是纯字符串运算，直接测 `_symbols` 与 `check_one` 的判定分支即可，
不必起 HTTP。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

SCRIPT = os.path.join(os.path.dirname(__file__), "..", "..",
                      "scripts", "check_remote_drift.py")


def _load():
    """把 script 当模块载入（它不在 package 里，用 spec 加载）。"""
    if not os.path.isfile(SCRIPT):
        pytest.skip("缺少 scripts/check_remote_drift.py")
    import importlib.util
    spec = importlib.util.spec_from_file_location("crd", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_symbols_extracts_toplevel_defs():
    crd = _load()
    src = '''
def alpha():
    pass

async def beta():
    pass

@router.get("/x")
async def gamma():
    pass

class NotAFunction:
    pass
'''
    syms = crd._symbols(src)
    assert "alpha" in syms
    assert "beta" in syms
    assert "gamma" in syms
    # 嵌套/类内方法不算顶层
    assert "NotAFunction" not in syms


def test_check_reports_drift_when_local_is_shorter(tmp_path, monkeypatch):
    """本地行数少于远端 → 必须报落后。"""
    crd = _load()
    monkeypatch.setattr(crd, "ROOT", str(tmp_path))

    remote_src = "\n".join(["def a():", "    pass", "", "def b():", "    pass", ""])
    monkeypatch.setattr(crd, "_get", lambda *a, **kw: remote_src)

    p = tmp_path / "m.py"
    p.write_text("def a():\n    pass\n", encoding="utf-8")

    r = crd.check_one("m.py", token="x")
    assert r["bad"] is True
    assert r["status"] == "drift"


def test_check_reports_drift_when_local_lost_a_function(tmp_path, monkeypatch):
    """行数差不多但少了函数 → 也要报落后（行数不是唯一判据）。"""
    crd = _load()
    monkeypatch.setattr(crd, "ROOT", str(tmp_path))

    remote_src = "def a():\n    pass\n\ndef b():\n    pass\n"
    monkeypatch.setattr(crd, "_get", lambda *a, **kw: remote_src)

    # 本地把 b 换成了别的填充，行数一样
    p = tmp_path / "m.py"
    p.write_text("def a():\n    pass\n\nx = 1\ny = 2\n", encoding="utf-8")

    r = crd.check_one("m.py", token="x")
    assert r["bad"] is True, "行数相同但丢了函数，必须仍然报落后"
    assert "b" in r["detail"]


def test_check_ok_when_local_has_extra(tmp_path, monkeypatch):
    """本地比远端多 → 正常（新增的改动），不该拦。"""
    crd = _load()
    monkeypatch.setattr(crd, "ROOT", str(tmp_path))

    remote_src = "def a():\n    pass\n"
    monkeypatch.setattr(crd, "_get", lambda *a, **kw: remote_src)

    p = tmp_path / "m.py"
    p.write_text("def a():\n    pass\n\ndef new_one():\n    pass\n", encoding="utf-8")

    r = crd.check_one("m.py", token="x")
    assert r["bad"] is False


def test_check_flags_local_missing_file(tmp_path, monkeypatch):
    """远端有、本地没有 → 必须拦（否则推送时会误以为"新建"）。"""
    crd = _load()
    monkeypatch.setattr(crd, "ROOT", str(tmp_path))
    monkeypatch.setattr(crd, "_get", lambda *a, **kw: "def a():\n    pass\n")

    r = crd.check_one("nope.py", token="x")
    assert r["bad"] is True
    assert r["status"] == "local_missing"
