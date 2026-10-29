"""`process_request` 所有返回分支的字段集必须一致（R32）。

为什么需要
----------
R31 手工补了两处缺失，但根因是结构性的：
**`process_request` 有 5 个 return，每个都是一份手写的 dict 字面量。**

    正常出结果   → 10 键
    零空闲时段   → 10 键（R31 补的）
    数据未加载   → 9 键（R31 补的）
    周次越界     → 9 键
    信息不足追问 → 8 键

漏一个键的后果不是报错，而是**下游拿到 None**。而 None 在不同分支上
含义完全不同：「没识别出课程」和「还没算」和「请求失败」全是 None ——
调用方无法区分，只能去正则解析 message（这正是 R21 想消除的）。

这个测试不针对某个具体字段，而是**枚举所有分支，比对键集**。
将来新增分支/新增字段，只要不一致就会红。

关于"新增字段"这件事：我特意写成"所有分支必须包含同一集合"，
而不是"每个分支必须包含我列出的 N 个键"。后者需要维护清单，
前者是自洽的 —— 但前者会放过"所有分支都漏了同一个字段"的情况，
所以下面另外有一条针对关键字段的显式断言。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.agent.reschedule_agent import RescheduleAgent
from app.utils.excel_parser import ScheduleDatabase, ScheduleEntry

REAL = os.path.join(os.path.dirname(__file__), "..", "..", "schedule.demo.xlsx")

# 调用方依赖契约的字段：即使所有分支都漏，也要能测出来
CONTRACT_KEYS = {
    "success", "message", "target_week", "context",
    "available_slots", "excluded_slots", "need_info",
    "followup_questions", "course_name", "course_unrecognized",
    "ai_plans", "total",
}


@pytest.fixture(scope="module")
def db():
    if not os.path.isfile(REAL):
        pytest.skip("缺少 schedule.demo.xlsx")
    d = ScheduleDatabase()
    d.load_from_excel(REAL)
    return d


@pytest.fixture()
def agent(db):
    return RescheduleAgent(db)


def _branches(agent):
    """构造五条分支，返回 [(分支名, 结果 dict)]。"""
    out = []

    # 1. 正常出结果
    out.append(("正常", agent.process_request("我是吴琳，把声速测量调到第3周", False)))

    # 2. 周次越界
    out.append(("越界", agent.process_request("我是吴琳，把声速测量调到第99周", False)))

    # 3. 信息不足（只给周次，不给教师）
    out.append(("追问", agent.process_request("调到第3周", False)))

    # 4. 数据未加载
    empty = ScheduleDatabase()
    empty.loaded = False
    out.append(("未加载", RescheduleAgent(empty).process_request(
        "我是吴琳，把声速测量调到第3周", False)))

    # 5. 零空闲时段：把一个教师的一整周全部占满
    full = ScheduleDatabase()
    rows = []
    for wd in ("周一", "周二", "周三", "周四", "周五"):
        for ts in ("567", "678", "91011"):
            for rm in ("R1", "R2"):
                rows.append(ScheduleEntry(3, wd, ts, "c1", "g", "实验", rm, "T9"))
    full.entries = rows
    full.loaded = True
    out.append(("零时段", RescheduleAgent(full).process_request(
        "我是T9，把实验调到第3周", False)))

    return out


def test_all_branches_have_identical_keys(agent):
    """五条分支的键集必须完全一致。"""
    branches = _branches(agent)
    key_sets = {name: set(r) for name, r in branches}
    union = set().union(*key_sets.values())
    diffs = {n: sorted(union - s) for n, s in key_sets.items() if union - s}
    assert not diffs, (
        "process_request 的返回分支键集不一致 —— 少一个键，下游拿到 None，"
        "而 None 在'未识别/未计算/失败'之间无法区分：\n"
        + "\n".join(f"  {n}: 缺 {m}" for n, m in diffs.items())
    )


def test_all_branches_cover_contract_keys(agent):
    """所有分支都必须覆盖调用方依赖的契约字段。

    与上一条互补：上一条防"某分支漏"，这条防"所有分支一起漏"。
    """
    for name, r in _branches(agent):
        missing = CONTRACT_KEYS - set(r)
        assert not missing, f"分支「{name}」缺契约字段：{sorted(missing)}"


def test_course_flag_never_contradicts_course_name(agent):
    """`course_unrecognized` 必须恒等于 `not course_name`，任何分支都不例外。"""
    for name, r in _branches(agent):
        cn = r.get("course_name")
        flag = r.get("course_unrecognized")
        assert flag is (not bool(cn)), (
            f"分支「{name}」自相矛盾：course_name={cn!r} 但 "
            f"course_unrecognized={flag!r}"
        )
