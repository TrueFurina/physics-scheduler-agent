"""safe_push 在拿不到 token 时必须立刻停（R61）。

缺陷
----
```python
HEADERS = {"Authorization": "Bearer " + _token(), ...}
```

`_token()` 拿不到时返回空字符串 ⇒ 请求头变成 `"Bearer "` ⇒ 全部 401。

而 `safe_push` 对 401 的处理是"这个文件 PUT 失败"，于是输出：

```
  backend/app/x.py  ->  401 ...
  backend/app/y.py  ->  401 ...
```

**整批文件都"失败"，但原因不是文件有问题，是根本没登录。**
读的人会先怀疑 token 权限、再怀疑网络，而真相是最简单的那一条。

这与 R60（`not st` 把"查不了"当"干净"）同族：
**把"前置条件不满足"和"操作失败"混成同一个现象。**

修法
----
- token 为空 → 立刻退出并明说"拿不到 GITHUB_TOKEN"
- 单个文件 401/403 → 单独提示"鉴权失败"，不要把整批都刷成失败
"""
import os
import sys

import pytest

SCRIPT = os.path.join(os.path.dirname(__file__), "..", "..",
                      "scripts", "safe_push.py")


def _load():
    if not os.path.isfile(SCRIPT):
        pytest.skip("缺少 scripts/safe_push.py")
    import importlib.util
    spec = importlib.util.spec_from_file_location("sp2", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    # 不执行文件级副作用（它没有），直接 exec
    spec.loader.exec_module(mod)
    return mod


def test_no_token_exits_early(monkeypatch, capsys):
    """拿不到 token → 立刻退出，不发任何请求。"""
    sp = _load()
    monkeypatch.setattr(sp, "_token", lambda: "")

    sent = []

    def fake_api(method, path, body=None, tries=4):
        sent.append((method, path))
        return 401, {"message": "Bad credentials"}

    monkeypatch.setattr(sp, "api", fake_api)
    monkeypatch.setattr(sp, "drift_report", lambda paths: {"bad": 0, "results": []})

    rc = sp.main(["--message", "m", "--file", "a.py", "--force"])
    out = capsys.readouterr().out

    assert rc != 0, "没有 token 却返回成功"
    assert not sent, f"没有 token 还是发了请求：{sent}"
    assert "GITHUB_TOKEN" in out, f"没有说清缺什么：{out!r}"


def test_auth_failure_is_named(monkeypatch, capsys):
    """有 token 但被拒（401）→ 提示鉴权失败，而不是笼统的"推送失败"。"""
    sp = _load()
    monkeypatch.setattr(sp, "_token", lambda: "expired")
    monkeypatch.setattr(sp, "drift_report", lambda paths: {"bad": 0, "results": []})
    monkeypatch.setattr(sp, "api",
                        lambda m, p, b=None, tries=4: (401, {"message": "Bad credentials"}))
    monkeypatch.setattr(sp.os.path, "isfile", lambda p: True)
    monkeypatch.setattr("builtins.open", lambda *a, **kw: __import__("io").BytesIO(b"d"))

    rc = sp.main(["--message", "m", "--file", "a.py", "--force"])
    out = capsys.readouterr().out

    assert rc != 0
    assert ("鉴权" in out) or ("token" in out.lower()) or ("凭据" in out), (
        f"401 被当成普通失败，读的人会去查文件而不是查凭据：{out!r}"
    )
