"""教室利用率的分母口径：不能把周末和上午排除在外

历史缺陷
--------
`/stats` 里写死 `total_slots = n_weeks * 5 * 3`（5 天 × 3 时段），而真实安排表
**周末照常排课**（433 条里周六 36、周日 35，占 16%；24电信 42/46 节在周末）
并且存在上午时段（MORNING 4 条）。把这两类排除在分母外 ⇒ 利用率系统性虚高：

    旧分母 16×5×3 = 240 格/间  →  平均利用率 17.6%
    真实   16×7×4 = 448 格/间  →  平均利用率  9.4%

教秘看到"17.4%"会以为实验室还很空，而真实占用接近翻倍 —— 这直接影响
"场地够不够、要不要再开时段"的判断。与 R-41 同族：拿"枚举里有的"
（5 天 3 时段）当"现实里有的"（7 天 4 时段）。

同时分子也有偏差：一条 `14:00-17:00` 同时占 567+678，按"1 节课"计会少算一格。
"""

import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import app.api.reschedule as api
from app.utils.excel_parser import ScheduleDatabase, ScheduleEntry

REAL_XLSX = os.path.join(os.path.dirname(__file__), "..", "..", "schedule.xlsx")


def _entry(week, weekday, slot, cls, room, teacher="林老师", group="1组",
           experiment="伏安法", blocked=None):
    return ScheduleEntry(week, weekday, slot, cls, group, experiment, room, teacher,
                         blocked=blocked)


def _db(entries):
    d = ScheduleDatabase()
    d.entries = list(entries)
    d.loaded = True
    return d


@pytest.fixture()
def swap_db(monkeypatch):
    def _swap(entries):
        monkeypatch.setattr(api, "_db", _db(entries))
        return entries

    return _swap


def _stats() -> dict:
    out = asyncio.run(api.stats())
    assert out["success"] is True
    return out["data"]


# ---------------------------------------------------------------------------
# 分母：由本表实际开放的星期/时段推导
# ---------------------------------------------------------------------------

def test_capacity_basis_is_derived_from_the_table(swap_db):
    """用到周六/周日和上午 ⇒ 分母必须把它们算进去（旧口径只认 5 天 3 时段）。"""
    swap_db([
        _entry(1, "周一", "567", "一班", "A410"),
        _entry(1, "周六", "567", "一班", "A410"),
        _entry(1, "周日", "MORNING", "一班", "A410"),
    ])
    data = _stats()
    assert data["schedulable_days"] == 3, "周末没被算进可排日"
    assert data["schedulable_slots"] == 2, "上午时段没被算进可排时段"
    # 1 周 × 3 天 × 2 时段 = 6 格/间，占用 3 格 → 50%
    assert data["capacity_basis"] == "1 周 × 3 天 × 2 时段 = 6 格/间"
    assert data["avg_room_utilization"] == 50.0
    # 反向验证：写死 5×3 会得到 3/15=20%，与本值明显不同 —— 口径不是摆设
    assert abs(data["avg_room_utilization"] - 20.0) > 1


def test_weekday_and_slot_counting_ignores_empty_values(swap_db):
    swap_db([
        _entry(1, "周一", "567", "一班", "A410"),
        _entry(1, "", "567", "一班", "A410"),      # 星期为空：不得撑大分母
    ])
    data = _stats()
    assert data["schedulable_days"] == 1


def test_numerator_counts_cross_slot_entry_as_two_cells(swap_db):
    """`14:00-17:00` 占 567+678 两格，不能只算一节。"""
    swap_db([_entry(1, "周一", "567", "一班", "A410", blocked=["567", "678"])])
    data = _stats()
    assert data["top_rooms"][0]["sessions"] == 2, "跨节次记录只按 1 节计了"


# ---------------------------------------------------------------------------
# 真实表：口径必须是 7 天 × 4 时段
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not os.path.exists(REAL_XLSX), reason="项目根目录缺 schedule.xlsx")
def test_real_table_capacity_covers_weekends(monkeypatch):
    """真实表周末有课（71/433）且存在上午时段 ⇒ 分母 16×7×4=448，利用率应低于旧口径。"""
    db = _db([])
    db.load_from_excel(REAL_XLSX)
    monkeypatch.setattr(api, "_db", db)

    data = _stats()
    assert data["schedulable_days"] == 7, f"真实表应覆盖 7 天，实际 {data['schedulable_days']}"
    assert data["schedulable_slots"] == 4, f"真实表应有 4 个时段，实际 {data['schedulable_slots']}"
    assert data["capacity_basis"] == "16 周 × 7 天 × 4 时段 = 448 格/间"

    # 占用格数按 blocked 计（跨节次算 2 格）；avg 是**各教室利用率的均值**，不是总格数/总容量
    per_room = {}
    for e in db.entries:
        if (e.room or "").strip():
            per_room[e.room] = per_room.get(e.room, 0) + len(
                getattr(e, "blocked", None) or [e.time_slot]
            )
    expect_new = sum(c / 448 * 100 for c in per_room.values()) / len(per_room)
    old_style = sum(c / (16 * 5 * 3) * 100 for c in per_room.values()) / len(per_room)
    assert data["avg_room_utilization"] < old_style, \
        f"利用率没有因纳入周末/上午而下降（{data['avg_room_utilization']} vs 旧口径 {old_style:.1f}）"
    assert abs(data["avg_room_utilization"] - round(expect_new, 1)) < 0.05, \
        f"利用率不是按 448 格/间 算出来的（{data['avg_room_utilization']} vs {expect_new:.1f}）"
    # 旧口径 240 格/间 会把 9.4% 报成 17.6% —— 差 87%，教秘会据此误判"场地还很空"
    assert old_style > 17 and data["avg_room_utilization"] < 10, \
        f"口径修正的幅度不对（旧 {old_style:.1f}% → 新 {data['avg_room_utilization']}%）"
