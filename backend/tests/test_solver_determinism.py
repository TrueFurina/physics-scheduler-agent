"""一键排课必须可复现（R62）。

缺陷本身：`solver_factory` 此前默认 8 个 worker 且不设 `random_seed`。
CP-SAT 的多 worker 是并行搜索 + 工作窃取，模型又有多个同等最优解
（"把 30 个任务排满 10 周"有海量等价解）⇒ **每次跑挑到不同的那个**。

实测（demo 表 433 行、`solve_all(chunk_size=60)`、3 片共 410 行）：

    worker=8：连跑两次 **288 / 410 行换了位置**，两次 status 都是 OPTIMAL
    worker=1：连跑两次完全一致，耗时 11.45s vs 11.07s（1.03x）

所以这不是"没算完就返回"，是**两个都正确的解里随机挑一个**。后果：
同一份数据、同一个按钮，两个人点出来的课表对不上；教秘拿着打印稿问
"这和我上次排的不一样"，谁也说不清哪个是"上次那个"。

判据：同一份数据连排两次，**归一化后逐条一致**。
- 不比较耗时、不比较 chunk 内部顺序 —— 只比"哪个任务落在哪一格"。
- 连跑两次而不是三次：单次 11 秒，两次已能暴露（实测两次就不同），
  三次会把这条用例推到 35 秒，收益不抵成本。

⚠️ 跳过的条件只有一条：**显式**设了 `SOLVER_WORKERS`（压测场景）。
刻意不用"当前生效的 worker 数 > 1"当跳过条件 —— 那样把默认值改回 8 时，
这条会安静地 skip，等于自己把守门人劝退了（R62 第一版就是这么写错的，
V1 变异跑出 `1 skipped` 才发现）。默认值必须是 1，由
`test_solver_factory_contract.py` 的另一条守；两条各管一件事。
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.utils.excel_parser import ScheduleDatabase

REAL = os.path.join(os.path.dirname(__file__), "..", "..", "schedule.demo.xlsx")


def _norm(rows):
    def key(s):
        return (
            s.get("week"), s.get("weekday"), s.get("time_slot"),
            s.get("room") or "", s.get("teacher") or "",
            s.get("class_name") or "", s.get("experiment") or "",
        )

    return sorted(key(s) for s in rows)


@pytest.mark.skipif(not os.path.isfile(REAL), reason="缺少 schedule.demo.xlsx")
def test_solve_all_is_reproducible(monkeypatch):
    """同一份数据连排两次，落点必须逐条相同。"""
    if os.getenv("SOLVER_WORKERS"):
        pytest.skip("显式指定了 worker 数（压测场景），结果不可复现是预期行为")
    monkeypatch.delenv("SOLVER_WORKERS", raising=False)
    monkeypatch.delenv("SOLVER_SEED", raising=False)

    from app.engine.auto_scheduler import AutoScheduler

    db = ScheduleDatabase()
    db.load_from_excel(REAL)

    first = AutoScheduler(db).solve_all(chunk_size=60)
    second = AutoScheduler(db).solve_all(chunk_size=60)

    a = _norm(first.get("schedule") or [])
    b = _norm(second.get("schedule") or [])

    # 防构造失效：真数据排不出东西时，这条用例会"空过"
    assert len(a) > 100, f"只排出 {len(a)} 行，演示表都没跑起来，这条用例没意义"
    assert first.get("solved") == second.get("solved"), (
        f"两次排入数量就不同：{first.get('solved')} vs {second.get('solved')}"
    )

    only_a, only_b = set(a) - set(b), set(b) - set(a)
    assert not only_a and not only_b, (
        f"同一份数据连排两次有 {len(only_a) + len(only_b)} 行落点不同 —— "
        f"一键排课不可复现。\n"
        f"  只在第一次：{sorted(only_a)[:2]}\n"
        f"  只在第二次：{sorted(only_b)[:2]}\n"
        f"（根因通常是 solver_factory 的 worker 数 > 1）"
    )
