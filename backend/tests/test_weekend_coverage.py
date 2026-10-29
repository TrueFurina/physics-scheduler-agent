"""调课沙盘的星期覆盖：周末不能凭空消失（R36）。

背景
----
真实数据 433 条里，**71 条在周末**（周六 36 / 周日 35）。这不是边角数据 ——
每 6 条课就有 1 条在周末。

而 `reschedule_agent.py` 里有三处硬编码：

    weekdays = ["周一", "周二", "周三", "周四", "周五"]

分别在 `find_available_slots` / `find_slots_with_reasons` / `process_request`
的展示分组里，都用在**调课主路径**上。

实测（教师H，第 2 周真实有周六课）：

    find_slots_with_reasons(2, teacher_name='教师H')
      周六可用：0 个
      周六被排除：0 个

**周六既不在"可用"里，也不在"被排除"里 —— 它从沙盘上消失了。**

这不是"少推荐几个时段"，而是两种不同的错误叠在一起：

1. 教秘看到的沙盘只有 5 天 —— 他不会意识到周六还有课要处理，
   更不会想到"这周六可能没空"。
2. 系统也从不*说*它跳过了周末。一份"看起来完整"的 5 天沙盘，
   比一份明确标注"未覆盖周末"的更危险。

第 2 点与项目反复踩的「静默不实」完全同族：显示的是真实数据的一份裁剪，
但不告诉看的人裁掉了什么。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.agent.reschedule_agent import RescheduleAgent
from app.utils.excel_parser import ScheduleDatabase

REAL = os.path.join(os.path.dirname(__file__), "..", "..", "schedule.demo.xlsx")
WEEKEND = ("周六", "周日")


@pytest.fixture(scope="module")
def agent():
    if not os.path.isfile(REAL):
        pytest.skip("缺少 schedule.demo.xlsx")
    db = ScheduleDatabase()
    db.load_from_excel(REAL)
    return RescheduleAgent(db)


def _weekend_entries(db, week, teacher=None):
    return [e for e in db.entries
            if e.week == week and e.weekday in WEEKEND
            and (not teacher or teacher in (e.teacher or ""))]


def test_weekend_slots_are_not_silently_dropped(agent):
    """有周末课的周，沙盘必须让这些时段出现在"可用"或"被排除"里。"""
    # 找一个真实存在周末课的周与教师
    target = None
    for e in agent.db.entries:
        if e.weekday in WEEKEND:
            target = (e.week, e.teacher)
            break
    assert target, "真实表里没有周末课，测试前提失效"

    week, teacher = target
    r = agent.find_slots_with_reasons(week, teacher_name=teacher)

    seen = {s["weekday"] for s in r["available"]} | {x["weekday"] for x in r["excluded"]}
    missing = [d for d in WEEKEND if d not in seen]
    # 只有当那一周该教师确实在那天有课时，才算"被吞掉"
    truly_missing = [d for d in missing
                     if _weekend_entries(agent.db, week, teacher)
                     and any(e.weekday == d for e in _weekend_entries(agent.db, week, teacher))]
    assert not truly_missing, (
        f"第{week}周 {teacher} 在 {truly_missing} 有课，但沙盘里这些星期"
        "既不可用也不在排除列表 —— 它们从界面上消失了"
    )


def test_weekend_is_either_recommended_or_explained(agent):
    """若确实不推荐周末，必须有一句话说清楚"未覆盖周末"。"""
    week, teacher = None, None
    for e in agent.db.entries:
        if e.weekday in WEEKEND:
            week, teacher = e.week, e.teacher
            break
    if week is None:
        pytest.skip("无周末课")

    res = agent.process_request(f"我是{teacher}，把课调到第{week}周", False)
    msg = res.get("message") or ""
    has_weekend_hint = ("周末" in msg) or ("周六" in msg) or ("周日" in msg)
    weekends_in_slots = any(s["weekday"] in WEEKEND for s in res.get("available_slots") or [])
    assert has_weekend_hint or weekends_in_slots, (
        "沙盘不含周末时段，也没有任何「未覆盖周末」的说明 —— "
        "教秘会以为这周周末没课"
    )