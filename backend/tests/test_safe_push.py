"""推送前必须经过漂移检查（R58 的教训固化）。

为什么要脚本而不是"记得跑"
--------------------------
R43 和 R58 是同一个错误犯了两次：

| 轮次 | 行为 | 后果 |
|---|---|---|
| R43 | 用 2739 行旧版覆盖远端 3280 行 | 净损失 541 行 / 41 个函数 |
| R58 | 用 R54 本地副本覆盖远端刚补回的端点 | `land/history` 再次消失 |

R44 建了 `check_remote_drift.py`，但它**要人去跑** ——
R54 推送时没想到跑，于是又覆盖了一次。

`scripts/safe_push.py` 把检查放进推送本身：任一文件落后 → 拒绝推送。

本文件测的是**拦截逻辑**，不是网络层：
- 落后文件 → 返回 1 且不动远端
- 全部干净 → 返回 0
- `--force` 时放行但明确告警
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

SCRIPT = os.path.join(os.path.dirname(__file__), "..", "..",
                      "scripts", "safe_push.py")


def _load():
    if not os.path.isfile(SCRIPT):
        pytest.skip("缺少 scripts/safe_push.py")
    import importlib.util
    spec = importlib.util.spec_from_file_location("sp", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_refuses_when_any_file_drifts(monkeypatch, capsys):
    """有文件落后时必须拒绝，且**一个文件都不推**。"""
    sp = _load()

    monkeypatch.setattr(sp, "_token", lambda: "x")
    monkeypatch.setattr(sp, "drift_report", lambda paths: {
        "bad": 1,
        "results": [
            {"path": paths[0], "status": "drift", "bad": True,
             "detail": "本地 100 行 / 远端 200 行"},
        ],
    })

    pushed = []

    def fake_api(method, path, body=None, tries=4):
        if method == "PUT":
            pushed.append(path)
        return 404, {"err": "should not reach"}

    monkeypatch.setattr(sp, "api", fake_api)

    rc = sp.main(["--message", "m", "--file", "a.py"])
    out = capsys.readouterr().out

    assert rc == 1, "落后时必须以非 0 退出"
    assert pushed == [], f"落后时仍推送了：{pushed}"
    assert "拒绝推送" in out


def test_force_pushes_but_warns(monkeypatch, capsys):
    """--force 时放行，但必须把"强制推送了落后文件"写进输出。"""
    sp = _load()
    monkeypatch.setattr(sp, "_token", lambda: "x")
    monkeypatch.setattr(sp, "drift_report", lambda paths: {
        "bad": 1,
        "results": [{"path": paths[0], "status": "drift", "bad": True, "detail": "x"}],
    })
    monkeypatch.setattr(sp, "api",
                        lambda m, p, b=None, tries=4: (200, {"commit": {"sha": "0" * 10}})
                        if m == "PUT" else (404, {}))
    monkeypatch.setattr(sp.os.path, "isfile", lambda p: True)
    monkeypatch.setattr("builtins.open", lambda *a, **kw: __import__("io").BytesIO(b"data"))

    rc = sp.main(["--message", "m", "--file", "a.py", "--force"])
    out = capsys.readouterr().out
    assert "--force" in out and "强制推送" in out, "强制推送没有告警"
    assert rc == 0


def test_pushes_when_clean(monkeypatch):
    """不落后时正常推送。"""
    sp = _load()
    monkeypatch.setattr(sp, "_token", lambda: "x")
    monkeypatch.setattr(sp, "drift_report", lambda paths: {"bad": 0, "results": []})
    monkeypatch.setattr(sp, "api",
                        lambda m, p, b=None, tries=4: (200, {"commit": {"sha": "a" * 10}})
                        if m == "PUT" else (200, {"sha": "old"}))
    monkeypatch.setattr(sp.os.path, "isfile", lambda p: True)
    monkeypatch.setattr("builtins.open", lambda *a, **kw: __import__("io").BytesIO(b"data"))

    assert sp.main(["--message", "m", "--file", "a.py"]) == 0


def test_drift_tool_absence_is_reported_not_silent(monkeypatch, capsys):
    """漂移检查不可用时必须说出来，不能当成"检查通过"。"""
    sp = _load()
    monkeypatch.setattr(sp, "_token", lambda: "x")
    monkeypatch.setattr(sp, "drift_report", lambda paths: None)
    monkeypatch.setattr(sp, "api",
                        lambda m, p, b=None, tries=4: (200, {"commit": {"sha": "a" * 10}})
                        if m == "PUT" else (404, {}))
    monkeypatch.setattr(sp.os.path, "isfile", lambda p: True)
    monkeypatch.setattr("builtins.open", lambda *a, **kw: __import__("io").BytesIO(b"data"))

    sp.main(["--message", "m", "--file", "a.py"])
    out = capsys.readouterr().out
    assert "漂移检查不可用" in out, "检查没跑却没告警 —— 等于默认放行"
