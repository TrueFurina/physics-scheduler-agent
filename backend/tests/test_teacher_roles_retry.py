"""`get_teacher_roles()` 的容错语义与注释相反（R51 · 缺口②）。

缺陷
----
`:994-995` 的注释写得很明确：

> 只有"真的开成功了一次工作簿"才认这个结果；开失败（文件不在/损坏）**不记**，
> 留给下一次重试 —— 否则一次瞬时 IO 错误会把备注永久钉成空。

但代码判的是 **`if path:`**（判*路径非空*，不是判*打开成功*）：

```
989-993  roles = {}; try: wb = openpyxl.load_workbook(...)  except Exception: pass
996      if path:                        ← 判的是路径，不是"打开成功"
997-998      self.roles = dict(roles); self._roles_loaded = True
```

⇒ 打开抛异常时照样把 `roles` 钉成 `{}` 并置 `_roles_loaded = True`
⇒ `:977` 的早退分支（`if self._roles_loaded: return dict(self.roles)`）**永久生效**
⇒ 注释承诺的"下一次重试"永远不会发生

成因可考：`:968-975` 记载的 2026-10-29 性能优化（`/teachers` 402ms、
事件循环被拖 407ms）把容错语义改没了，**注释留在原地没跟着改**。

### 这个修复的代价（必须写下来，别只当成"修好了"）

修好之后，若文件**持续**损坏（不是瞬时），每次 `/teachers` 都会重开工作簿，
即退回到 402ms 的老问题。

这是**有意的取舍**：一次瞬时 IO 错误就把教师备注永久钉成空，
是不可恢复的数据错误；而持续损坏时的重复开销，是每次 400ms 的可见性能问题。
前者无声、后者有声 —— 项目一贯的选择是"宁可慢，不能悄悄错"。

⇒ 若将来要两头都占，正确做法是"连续失败 N 次后定格并记 error 日志"，
而不是把语义改回 `if path:`。本测试只锁住当前这一档语义。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import app.utils.excel_parser as ep
from app.utils.excel_parser import ScheduleDatabase

REAL = os.path.join(os.path.dirname(__file__), "..", "..", "schedule.demo.xlsx")


@pytest.fixture()
def db_no_cache(tmp_path, monkeypatch):
    """一个"路径存在、但缓存未命中"的 db —— 强制走第 3 档真打开。"""
    if not os.path.isfile(REAL):
        pytest.skip("缺少 schedule.demo.xlsx")
    db = ScheduleDatabase()
    db._filepath = REAL
    db._roles_loaded = False
    # 关掉第 2 档（解析缓存），否则缓存命中就绕过 load_workbook，测不到这段
    monkeypatch.setattr(ep.parse_cache, "load", lambda p: None)
    monkeypatch.setattr(ep.parse_cache, "merge", lambda p, d: None)
    return db


def test_open_failure_does_not_mark_loaded(db_no_cache, monkeypatch):
    """打开失败 → 不能置 `_roles_loaded`，否则"下一次重试"永不发生。"""
    import openpyxl

    def boom(*a, **k):
        raise RuntimeError("文件损坏 (模拟)")

    monkeypatch.setattr(openpyxl, "load_workbook", boom)

    got = db_no_cache.get_teacher_roles()

    assert got == {}, "打开失败时本就拿不到备注"
    assert db_no_cache._roles_loaded is False, (
        "打开失败却把 `_roles_loaded` 置成 True —— 教师备注被永久钉成空 dict，"
        "注释承诺的「留给下一次重试」永远不会发生。"
    )


def test_open_failure_really_retries_next_time(db_no_cache, monkeypatch):
    """第二次调用必须真的重开工作簿 —— 这是上面那条的**行为层**证据。

    只断言标志位不够：标志位对了但没人再调 load_workbook，等于没修。
    """
    import openpyxl

    calls = {"n": 0}

    def boom(*a, **k):
        calls["n"] += 1
        raise RuntimeError("文件损坏 (模拟)")

    monkeypatch.setattr(openpyxl, "load_workbook", boom)

    db_no_cache.get_teacher_roles()
    db_no_cache.get_teacher_roles()

    assert calls["n"] == 2, (
        f"两次调用只尝试打开 {calls['n']} 次 —— 失败的那次被当成了已加载，"
        "后续不再重试。"
    )


def test_open_success_is_cached(db_no_cache, monkeypatch):
    """打开成功 → 必须定格，第二次不再开工作簿（守住 2026-10-29 的性能优化）。"""
    import openpyxl

    real_load = openpyxl.load_workbook
    calls = {"n": 0}

    def counting(*a, **k):
        calls["n"] += 1
        return real_load(*a, **k)

    monkeypatch.setattr(openpyxl, "load_workbook", counting)

    first = db_no_cache.get_teacher_roles()
    second = db_no_cache.get_teacher_roles()

    assert db_no_cache._roles_loaded is True, "开成功了却没定格 —— 每次都要重开"
    assert first == second, "两次结果不一致"
    assert calls["n"] == 1, (
        f"打开成功却开了 {calls['n']} 次工作簿 —— 把 2026-10-29 修掉的 402ms 又退回来了"
    )
