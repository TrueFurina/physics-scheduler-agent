"""CP-SAT 求解器的统一构造入口。

两条硬约束打在这个文件上：**内存**和**可复现**。

一、内存（2026-10-29）
----------------------
四处 `cp_model.CpSolver()` 都**没有设置 `num_search_workers`**，于是 ortools 默认按
CPU 核数开满并行搜索 —— 每个 worker 会复制一份模型。在这台 24 核机器上实测：
真实表 433 行的 `solve()` 峰值 **+158MB**；而可用内存掉到 2.6GB 时，
某一片的申请就会失败，且 ortools 的失败形态是 **`MemoryError: bad allocation`
直接 abort 进程**（不是抛 Python 异常，`try/except` 拦不住）——
一次内存紧张会把整个测试进程/服务进程带走。

二、可复现（2026-11-02，R62）
-----------------------------
`new_solver()` 此前只设了 `num_search_workers` 和 `max_time_in_seconds`，
**没设 `random_seed`**，且 worker 数默认为 8。CP-SAT 的多 worker 是并行搜索 +
工作窃取：哪个 worker 先摸到某个解、谁抢到了哪棵子树，取决于线程调度。
模型有多个同等最优解时（排课几乎总是如此 —— "把 30 个任务排满 10 周"有海量等价解），
**每次跑会挑到不同的那个**。

实测（2026-11-02，demo 表 433 行，`solve_all(chunk_size=60)`，3 片，410 行结果）：

| worker | 连跑两次结果 | 逐条差异 | 耗时 |
|---|---|---|---|
| 8（旧默认） | **完全不同** | **288 / 410 行换了位置** | 11.07s / 11.44s |
| 1（新默认） | **完全一致** | 0 | 11.45s / 11.54s |

要点两条：
- 两种配置下 `status` 都是 `OPTIMAL` —— 不是"没算完就返回"，是**真的有两个解**，
  差别只在挑哪一个。所以这不是性能问题，是**产品问题**：同一份数据、同一个按钮，
  两个人点出来的课表对不上，也没法复查"上次排的那份"。
- 单 worker 的耗时代价是 **1.03x**（+0.38s / 11s）。60 个任务的片上，
  8 worker 是 4.2s、1 worker 是 4.4s —— CP-SAT 在这个规模上并行收益本来就接近零，
  而内存随 worker 线性增长。**确定性这一侧几乎不要钱。**

边界（必须写清，别过度承诺）：
- 只保证"**同数据 + 同配置 + 未触发时限**"时逐条一致。
- 一旦 `max_time_in_seconds` 到点被截断（`status=FEASIBLE`），结果取决于当时算到哪，
  仍然是随机的。真实 60 任务片耗时 4.4s、时限 7s，余量够；但机器极慢时可能顶到。
  ⇒ 想彻底确定得连时限一起去掉，那会带来"卡住不返回"的风险，权衡后保留时限。

设计
----
- `SOLVER_WORKERS`：默认 **1**（确定性 + 最省内存）。压测想并行时设 8 或更大；
  设 0 表示交给 ortools 自己决定（此时不可复现，仅用于性能对照）。
- `SOLVER_SEED`：默认 1。只在 worker=1 时有意义 —— worker>1 时种子管不住线程调度。
- 单一入口，避免"改了三处、漏了第四处"。
"""

from __future__ import annotations

import os
from typing import Optional

from ortools.sat.python import cp_model

# 默认 1：可复现 + 内存最省。并行搜索在排课模型的规模上收益接近零（见文件头实测）。
DEFAULT_MAX_WORKERS = 1
# 固定种子。只在 worker=1 时才能保证可复现。
DEFAULT_SEED = 1


def solver_workers() -> Optional[int]:
    """当前生效的 worker 数；None 表示不设置（交给 ortools 按核数决定，不可复现）。"""
    raw = (os.getenv("SOLVER_WORKERS") or "").strip()
    if raw == "0":
        return None
    if raw.isdigit():
        return max(1, int(raw))
    return DEFAULT_MAX_WORKERS


def solver_seed() -> int:
    """当前生效的随机种子。`SOLVER_SEED` 可覆盖（复现某个特定解时用）。"""
    raw = (os.getenv("SOLVER_SEED") or "").strip()
    if raw.isdigit():
        return int(raw)
    return DEFAULT_SEED


def new_solver(max_time_seconds: Optional[float] = None) -> "cp_model.CpSolver":
    """构造一个确定性、带内存与时限的求解器。`max_time_seconds` 为 None 时不设时限。"""
    s = cp_model.CpSolver()
    w = solver_workers()
    if w is not None:
        s.parameters.num_search_workers = w
    s.parameters.random_seed = solver_seed()
    if max_time_seconds is not None:
        s.parameters.max_time_in_seconds = max_time_seconds
    return s
