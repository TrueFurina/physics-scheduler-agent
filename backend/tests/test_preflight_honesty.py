"""预检不能把「查不了」当成「没问题」（R60）。

缺陷
----
`preflight_release.py` 的 git 检查：

```python
st = subprocess.run(["git", "status", "--short"], cwd=ROOT, ...).stdout.strip()
head = subprocess.run(["git", "log", "--oneline", "-1"], cwd=ROOT, ...).stdout.strip()
check("git 工作区", not st, f"{head}" + (...))
```

`git` 失败时 **stdout 是空字符串** —— 而 `not ""` 为真 ⇒ 判定「干净」。

实测（本工作目录不是 git 仓库）：

```
$ git status --short
fatal: not a git repository (or any of the parent directories): .git
退出码 128

$ python scripts/preflight_release.py
 ✅ git 工作区：｜干净          ← 明明一条都查不了
```

`head` 也是空的，所以输出是 `｜干净` —— 前面那个位置本该是提交号。

### 为什么这很危险

发布前检查里最不该说谎的就是这条。「未提交改动会被打进发布包」
是一个真实风险；而检查器在"根本查不了"的时候给了一个**绿色勾**。

这与 R50（`load()` 用 `None` 混淆"不存在/损坏/版本旧"）同族：
**把"未知"塌缩成"正常"**。

### 修法

1. 检查 `returncode`，非 0 时明确报"不是 git 仓库/查不了"
2. `head` 为空也要说出来，而不是留一段空白
3. 这类"查不了"应当是**阻塞项**吗？

    不是。本地 codeload 解包的工作副本本来就不是 git 仓库，
    发布走的是那个目录的内容。但**必须说清楚**，让人知道这条检查没生效。
"""
import os
import re
import subprocess
import sys

import pytest

SCRIPT = os.path.join(os.path.dirname(__file__), "..", "..",
                      "scripts", "preflight_release.py")


def _src():
    if not os.path.isfile(SCRIPT):
        pytest.skip("缺少 scripts/preflight_release.py")
    with open(SCRIPT, encoding="utf-8") as f:
        return f.read()


def test_git_check_uses_returncode():
    """源码契约：git 检查必须看 returncode，不能只看 stdout 是否为空。"""
    src = _src()
    # 定位 git 检查那一段
    idx = src.find('"git 工作区"')
    assert idx > 0, "找不到 git 检查"
    window = src[max(0, idx - 1200):idx]
    assert "returncode" in window, (
        "git 检查没有判断 returncode —— 命令失败时 stdout 为空，"
        "`not st` 会把「查不了」判定成「干净」"
    )


def test_git_failure_is_reported_not_green(tmp_path):
    """端到端：在非 git 目录跑预检，git 那条不能是 ✅。"""
    root = os.path.dirname(os.path.dirname(SCRIPT))
    proc = subprocess.run(
        [sys.executable, SCRIPT, "--offline"],
        cwd=root, capture_output=True, text=True, timeout=300)
    out = proc.stdout + proc.stderr
    line = [l for l in out.splitlines() if "git 工作区" in l]
    assert line, f"预检没有输出 git 那一行：\n{out[:400]}"
    the_line = line[0]

    is_repo = subprocess.run(["git", "status", "--short"], cwd=root,
                             capture_output=True, text=True).returncode == 0
    if not is_repo:
        # 关键不是"能不能打勾"（不阻塞，可以打勾），而是**必须说明没查成**。
        #
        # 原来的断言写成"不许有 ✅"是错的：codeload 解包的工作副本本来
        # 就不是 git 仓库，这不阻塞发布，打 ✅ 合理。真正的问题是
        # 旧版本输出 `｜干净` —— 三个字，看不出"这条没生效"。
        assert ("不是 git" in the_line) or ("未检查" in the_line) or ("无法" in the_line), (
            f"没有说清楚这条检查没生效：{the_line}\n"
            "「查不了」被当成「干净」，等于用一个绿勾掩盖了检查缺失"
        )
        assert "｜干净" not in the_line, (
            f"仍然输出「干净」：{the_line} —— 这条检查根本没跑成"
        )
        assert ("不是 git" in the_line) or ("查不了" in the_line) or ("无法" in the_line), (
            f"没有说清楚为什么查不了：{the_line}"
        )


def test_freshness_check_failure_is_blocking_not_skipped():
    """产物新鲜度检查自己跑挂了 → 必须拦，不能"跳过"。

    它是 R59 立的阻塞项。若"检查脚本跑挂"就放行，等于留了一个
    比修产物容易得多的绕过口子（把脚本删掉即可）。

    与 git 那条的区别：git 查不了是环境正常形态（不是仓库），
    产物检查跑挂是**异常**。
    """
    src = _src()
    idx = src.find('"前端产物新鲜度"')
    assert idx > 0
    # 找它所在 try 的 except 分支
    tail = src[idx:]
    ex = tail.find("except Exception")
    assert ex > 0, "找不到 except 分支"
    block = tail[ex:ex + 600]
    assert "check(\"前端产物新鲜度\", False" in block, (
        "产物新鲜度检查异常时没有判 False —— 异常被降级成通过了：" + block[:300]
    )
