"""CP-SAT 预热（`app/engine/warmup.py`）回归测试。

预热是"只为省时间"的旁路：它失败**不能**影响任何功能，
所以这里的重点是"失败也不炸"和"状态可见"。
"""

import os
import sys

import pytest

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


# ---------------------------------------------------------------- 求解器 worker 上限（R-92）
#
# 2026-10-29：四处 `cp_model.CpSolver()` 都没设 `num_search_workers`，ortools 默认
# 按 CPU 核数开满 —— 每个 worker 复制一份模型。实测（真实表 433 行、solve()）：
#   worker=8  峰值 73MB   耗时 2.3s
#   满核(24)  峰值 182MB  耗时 2.8s
# 内存随核数翻 2.5 倍，速度反而更慢（小模型上 24 worker 是过量并行）。
#
# 更要紧的是失败形态：内存不足时 ortools 抛的是 **`MemoryError: bad allocation`
# 直接 abort 进程**（`try/except` 拦不住）。当系统可用内存掉到 2.6GB 时，
# 全量测试曾因此整体崩掉（exit=3），不是断言失败、是进程没了。

def test_solver_has_bounded_workers():
    """默认 worker 数必须被限制住（否则内存随核数放大，且 abort 无法捕获）。"""
    from app.engine.solver_factory import DEFAULT_MAX_WORKERS, new_solver, solver_workers

    w = solver_workers()
    assert w is not None, "默认不该把 worker 数交给 ortools（核数多时内存会爆）"
    assert 1 <= w <= max(DEFAULT_MAX_WORKERS, 1), f"worker 数越界：{w}"

    s = new_solver()
    assert s.parameters.num_search_workers == w


def test_solver_workers_env_override(monkeypatch):
    """`SOLVER_WORKERS` 可覆盖；`0` 表示交回 ortools（压测想要满核时用）。"""
    from app.engine.solver_factory import new_solver, solver_workers

    monkeypatch.setenv("SOLVER_WORKERS", "3")
    assert solver_workers() == 3
    assert new_solver().parameters.num_search_workers == 3

    monkeypatch.setenv("SOLVER_WORKERS", "0")
    assert solver_workers() is None, "0 应表示「不设置」，交给 ortools"


def test_all_engine_solvers_go_through_factory():
    """结构锁：引擎里不许再裸建 `cp_model.CpSolver()` —— 漏一处就漏一处内存。"""
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "app" / "engine"
    offenders = []
    for path in root.glob("*.py"):
        if path.name == "solver_factory.py":
            continue                     # 工厂自己当然要用
        code = "\n".join(line.split("#")[0]
                         for line in path.read_text(encoding="utf-8").split("\n"))
        # 不限定模块别名：`_cm.CpSolver()` 与 `cp_model.CpSolver()` 同样会按核数吃内存
        if re.search(r"CpSolver\s*\(", code):
            offenders.append(path.name)
    assert not offenders, (
        "这些引擎文件还在裸建 CpSolver（会按核数吃内存）：" + ", ".join(offenders)
    )


def test_worker_limit_does_not_reduce_solver_completeness():
    """限 worker 不能换来"排不满" —— 内存收益必须不以求解质量为代价。

    R-92 把 CP-SAT 的 worker 从"按核数开满"降到默认 8（内存 −60%、耗时还略快）。
    但这类改动必须回答一句：**少并行会不会降低求解质量**？
    项目把"满载排满"列为硬指标（R-75/R-76 就是满载下少排 52 个组课时被抓出来的），
    所以这里用真实表跑同一条路径，断言组课时仍然全覆盖。

    实测记录（2026-10-29，真实表 433 行）：
      worker=8   空闲 25.0s / 满载(28 忙等) 29.7s，两次都是 171 段 = 410 组课时
      worker=满核 单批 2.8s→2.3s；单班求解 0.82–1.15s，两者无差异

    ⚠️ **这条测试的检出能力有上限，要如实说明**：实测 `SOLVER_WORKERS=1` 也只有
    27s、410/410、`fallback=None` —— 这个规模下 CP-SAT 单线程本就解得动
    （且还有贪心兜底），所以"并行度降到 1"它**照样绿**。
    它的价值是"守住别退化到排不满"，而不是"证明 8 就是最优 worker 数"。
    真要验并行度，得用更大规模的合成表，成本与收益不成比例（见 R-93 备忘）。
    """
    import os

    from app.engine.auto_scheduler import AutoScheduler
    from app.utils.excel_parser import ScheduleDatabase

    real = os.path.join(os.path.dirname(__file__), "..", "..", "schedule.xlsx")
    if not os.path.exists(real):
        pytest.skip("真实排课表不在仓库里")

    db = ScheduleDatabase()
    db.load_from_excel(os.path.abspath(real))
    r = AutoScheduler(db).solve_all(chunk_size=60)

    assert r["success"] is True, r.get("message")
    assert r["solved"] == r["total_sections"], f"段没排满：{r['solved']}/{r['total_sections']}"
    # 组课时是"教秘真正看到的覆盖率"（段 vs 组两个口径，见 R-72 备忘）
    assert r["solved_groups"] == r["total_groups"], (
        f"组课时没排满：{r['solved_groups']}/{r['total_groups']} —— "
        "限 worker 不该牺牲覆盖；若这里红了，说明并行度不足导致求解退化"
    )
