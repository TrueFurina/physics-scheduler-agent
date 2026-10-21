"""CP-SAT 预热（`app/engine/warmup.py`）回归测试。

预热是"只为省时间"的旁路：它失败**不能**影响任何功能，
所以这里的重点是"失败也不炸"和"状态可见"。
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.engine import warmup


def test_warmup_runs_and_reports_ok(monkeypatch):
    monkeypatch.delenv("SCHEDULE_WARMUP", raising=False)
    warmup._run()  # 直接跑，不开线程（测试要确定性）

    state = warmup.state()
    assert state["status"] == "ok", state
    assert state["elapsed_ms"] is not None and state["elapsed_ms"] >= 0
    assert state["error"] is None


def test_warmup_disabled_by_env(monkeypatch):
    monkeypatch.setenv("SCHEDULE_WARMUP", "0")
    # 允许重复调用：禁用时必须直接返回 False 且不启动线程
    assert warmup.start_background() is False


def test_warmup_failure_is_contained(monkeypatch):
    """把导入打坏，预热只能记 failed，不能抛到调用方。"""
    import builtins

    real_import = builtins.__import__

    def broken(name, *args, **kwargs):
        if name.startswith("ortools"):
            raise ImportError("simulated missing ortools")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", broken)
    for mod in list(sys.modules):
        if mod.startswith("ortools"):
            del sys.modules[mod]

    warmup._run()  # 不抛

    state = warmup.state()
    assert state["status"] == "failed"
    assert "ortools" in (state["error"] or "")
