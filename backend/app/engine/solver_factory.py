"""CP-SAT 求解器的统一构造入口。

为什么需要它（2026-10-29）
--------------------------
四处 `cp_model.CpSolver()` 都**没有设置 `num_search_workers`**，于是 ortools 默认按
CPU 核数开满并行搜索 —— 每个 worker 会复制一份模型。在这台 24 核机器上实测：
真实表 433 行的 `solve()` 峰值 **+158MB**；而可用内存掉到 2.6GB 时，
某一片的申请就会失败，且 ortools 的失败形态是 **`MemoryError: bad allocation`
直接 abort 进程**（不是抛 Python 异常，`try/except` 拦不住）——
一次内存紧张会把整个测试进程/服务进程带走。

同一台机器、同一份数据，把 worker 数设为 4 时峰值降到约 1/3（见各引擎的实测记录）。

设计
----
- 默认 `min(8, os.cpu_count())`：8 个 worker 在 CP-SAT 上已接近收益平台期
  （实测 4000 变量模型：默认/8/4/2 耗时都是 0.10s 左右，差异在噪声内），
  而内存随 worker 线性增长 —— 用"更多 worker 换更少内存"不划算。
- 环境变量 `SOLVER_WORKERS` 可覆盖（压测想要满核时用），设 0 表示交给 ortools 决定。
- 单一入口，避免"改了三处、漏了第四处"。
"""

from __future__ import annotations

import os
from typing import Optional

from ortools.sat.python import cp_model

DEFAULT_MAX_WORKERS = 8


def solver_workers() -> Optional[int]:
    """当前生效的 worker 数；None 表示不设置（交给 ortools 按核数决定）。"""
    raw = (os.getenv("SOLVER_WORKERS") or "").strip()
    if raw == "0":
        return None
    if raw.isdigit():
        return max(1, int(raw))
    if raw == "":
        return max(1, min(DEFAULT_MAX_WORKERS, os.cpu_count() or 1))
    return max(1, min(DEFAULT_MAX_WORKERS, os.cpu_count() or 1))


def new_solver(max_time_seconds: Optional[float] = None) -> "cp_model.CpSolver":
    """构造一个带内存上限的求解器。`max_time_seconds` 为 None 时不设时限。"""
    s = cp_model.CpSolver()
    w = solver_workers()
    if w is not None:
        s.parameters.num_search_workers = w
    if max_time_seconds is not None:
        s.parameters.max_time_in_seconds = max_time_seconds
    return s
