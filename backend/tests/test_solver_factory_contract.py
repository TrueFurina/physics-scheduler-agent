"""求解器构造入口的契约（R62）。

为什么单独锁这个文件：`solver_factory.new_solver()` 是全项目几个 CP-SAT 调用点的
唯一入口，它的参数直接决定**同一份数据两次排课会不会给出两张不同课表**。
实测（demo 表 433 行、`solve_all(chunk_size=60)`、410 行结果）：

    worker=8（旧默认）连跑两次 —— 288 / 410 行换了位置，两次 status 都是 OPTIMAL
    worker=1（新默认）连跑两次 —— 完全一致，耗时 1.03x

这组用例只碰参数、**不跑求解**，所以是毫秒级的。真正的行为证据在
`test_solver_determinism.py`（那条要跑二十多秒）。分开的理由很实际：
参数配错时立刻能定位，不必等二十秒才知道红了。

守五条：
1. 默认 worker=1 —— 多 worker 下 CP-SAT 的并行搜索 + 工作窃取不可复现；
2. 种子显式设入（值本身不重要，重要的是不依赖 ortools 的内部默认）；
3. `SOLVER_WORKERS` 能覆盖（压测要并行），`0` 表示交回 ortools（此时不可复现）；
4. `SOLVER_SEED` 能覆盖（复现某个特定解时用）；
5. 时限按传入值设，`None` 时不设 —— 调用方各自决定。
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.engine import solver_factory


def test_default_workers_is_one():
    """默认必须是单 worker。多 worker 下"同一份数据两次排课得到不同课表"是实测过的。"""
    assert solver_factory.solver_workers() == 1, (
        "默认 worker 被改回多线程了 —— 这会让一键排课的结果不可复现"
    )


def test_new_solver_applies_single_worker():
    s = solver_factory.new_solver()

    assert s.parameters.num_search_workers == 1, (
        f"构造出的求解器 worker={s.parameters.num_search_workers}，不是 1 —— "
        "契约在函数上成立了、在对象上没成立"
    )


def test_worker_override_by_env(monkeypatch):
    """压测时可以放开并行；这是唯一允许不定的入口，必须显式设。"""
    monkeypatch.setenv("SOLVER_WORKERS", "4")

    assert solver_factory.solver_workers() == 4
    assert solver_factory.new_solver().parameters.num_search_workers == 4


def test_worker_zero_means_let_ortools_decide(monkeypatch):
    """`0` 是"交回 ortools"，返回 None（不写这个字段）。"""
    monkeypatch.setenv("SOLVER_WORKERS", "0")

    assert solver_factory.solver_workers() is None
    # 不设置时该字段保持 ortools 默认 0
    assert solver_factory.new_solver().parameters.num_search_workers == 0


def test_worker_env_garbage_falls_back_to_default(monkeypatch):
    """环境变量写歪了不能变成"随便开满" —— 必须退回默认 1。"""
    monkeypatch.setenv("SOLVER_WORKERS", "abc")

    assert solver_factory.solver_workers() == 1


def test_seed_default_and_override(monkeypatch):
    """种子要能被显式指定；`SOLVER_SEED` 不生效说明代码根本没在读它。"""
    assert solver_factory.solver_seed() == solver_factory.DEFAULT_SEED

    monkeypatch.setenv("SOLVER_SEED", "7")

    assert solver_factory.solver_seed() == 7
    assert solver_factory.new_solver().parameters.random_seed == 7, (
        "种子没有写进求解器参数 —— 复现某个特定解时会拿到别的那个"
    )


def test_seed_env_garbage_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("SOLVER_SEED", "v2")

    assert solver_factory.solver_seed() == solver_factory.DEFAULT_SEED


def test_time_limit_passthrough():
    s = solver_factory.new_solver(max_time_seconds=2.5)

    assert abs(s.parameters.max_time_in_seconds - 2.5) < 1e-9


def test_no_time_limit_leaves_it_unset():
    """不传时限就不能偷偷给一个 —— 那会让"没算完"变成默认行为。"""
    s = solver_factory.new_solver()

    assert s.parameters.max_time_in_seconds == float("inf"), (
        f"未传时限却设成了 {s.parameters.max_time_in_seconds}"
    )
