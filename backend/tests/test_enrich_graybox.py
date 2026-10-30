"""灰箱补全：替用户做决定，但必须说得出口且可复现（R53）。

背景
----
教秘只给部分信息（"这个项目要排，教师/场地你看着办"）时，
`_enrich_extra_tasks` 会从主数据补全：

    缺指导教师 → 该项目的可授教师里挑**本批负载最轻**的（平局取字典序）
    缺候选场地 → 补该项目的第一间
    两处都没有 → 不编造，如实报告"结果中该列为空"

三条不变量
----------
1. **幂等**：同一批任务算两次，分配结果必须一样（否则重启后方案漂移）
2. **假设可见**：补全时说出口（`assumptions`），且**每次调用都要重算并返回**
   —— 不能"第一次说了、第二次不说"
3. **负载均衡**：同一项目的多个任务要摊到不同人身上，不能全砸给一个

第 2 条最容易做错：任务对象在会话内共享，第一次补全后字段就不空了。
若只看"字段是否为空"来判断是否需要说明，第二次调用就会静默丢掉假设 ——
学生（教秘）再问一遍"系统帮我定了什么"，系统答不出来了。
代码用 `_teacher_enriched` / `_room_enriched` 标记解决了这个问题，
本文件把它钉住。

实测（真实表）：
  单教师项目 × 2 任务   → 都分给同一人，假设 4 条，二次调用一致
  三教师项目 × 3 任务   → 分给 3 个人（字典序平局），二次调用一致
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import app.api.reschedule as api
from app.master_data import is_schedulable_project
from app.utils.excel_parser import PendingTask, ScheduleDatabase

REAL = os.path.join(os.path.dirname(__file__), "..", "..", "schedule.demo.xlsx")


@pytest.fixture()
def env(monkeypatch):
    if not os.path.isfile(REAL):
        pytest.skip("缺少 schedule.demo.xlsx")
    db = ScheduleDatabase()
    db.load_from_excel(REAL)
    monkeypatch.setattr(api, "_db", db)
    api._master_data.__init__()
    api._master_tasks.clear()
    api._ensure_master_data()
    yield api
    api._master_tasks.clear()


def _pick_project(api_mod):
    for n in api_mod._master_data.experiments:
        if is_schedulable_project(n):
            return n
    pytest.skip("没有可排实验项目")


def _mine(tasks):
    return [t for t in tasks if str(getattr(t, "class_name", "")).startswith("测试班")]


def test_enrich_is_idempotent(env):
    """同一批任务算两次，教师/场地分配必须一致。"""
    ex = _pick_project(env)
    env._master_tasks.extend([PendingTask(f"测试班{i}", ex, f"g{i}") for i in range(3)])

    t1, _ = env._enrich_extra_tasks()
    first = [(t.class_name, t.teacher, t.room) for t in _mine(t1)]
    t2, _ = env._enrich_extra_tasks()
    second = [(t.class_name, t.teacher, t.room) for t in _mine(t2)]

    assert first == second, f"两次补全结果不同：\n  {first}\n  {second}"


def test_assumptions_repeat_on_every_call(env):
    """假设必须每次调用都返回 —— 不能"第一次说了、第二次不说"。

    ⚠️ 必须用 `_db.pending`（对象在会话内**持久**），不能用 `_master_tasks`：
    后者每次调用都经 `_master_derived_tasks()` 重建为新对象，标记天然是新的 ——
    那样即使实现退化成"只看字段是否为空"，测试也照样通过（实测过，是假绿）。

    真实场景正是 `_db.pending`：上传清单里的任务对象在进程内一直存在，
    第一次补全把它填满了，第二次调用若不看标记就会沉默。
    """
    ex = _pick_project(env)
    task = PendingTask("测试班A", ex, "g1")
    env._db.pending = [task]          # ← 持久对象，不是每次重建
    env._master_tasks.clear()

    _, a1 = env._enrich_extra_tasks()
    assert a1, "第一次补全没给任何假设说明"
    assert task.teacher, "前提：第一次应已补上教师"

    _, a2 = env._enrich_extra_tasks()
    assert a2, (
        "第二次补全的 assumptions 空了 —— 字段已被上次填满，"
        "只看「字段是否为空」就会静默丢掉说明"
    )
    # ⚠️ 光断言"条数一样"不够：教师那条没了、场地那条还在时，条数可能都是 1，
    # 用例照样通过（实测过）。必须**逐条**确认两类假设都还在。
    assert any("教师" in x for x in a2), (
        f"第二次补全丢了「自动分配教师」的说明：{a2} —— "
        "教秘再问一次「系统帮我定了什么」就答不出来了"
    )
    assert len(a1) == len(a2), f"两次假设条数不同：{len(a1)} vs {len(a2)}"
    assert sorted(a1) == sorted(a2), "两次假设内容不同"


def test_load_is_spread_across_teachers(env):
    """同一项目的多个任务要摊开，不能全给负载最轻的那一个（会越补越重）。"""
    ex = _pick_project(env)
    pool = {"教师Z", "教师A", "教师M"}
    env._master_data.experiments[ex]["teachers"] = set(pool)
    env._master_tasks.extend([PendingTask(f"测试班{i}", ex, f"g{i}") for i in range(3)])

    tasks, _ = env._enrich_extra_tasks()
    assigned = [t.teacher for t in _mine(tasks)]
    assert len(set(assigned)) == 3, (
        f"3 个任务只分给了 {set(assigned)} —— 负载均衡失效，"
        "补全会把新任务全堆给同一个人"
    )
    assert set(assigned) == pool, "分配结果里有池外的教师"


def test_does_not_override_explicit_values(env):
    """教秘明确给了教师/场地时，补全不得覆盖。"""
    ex = _pick_project(env)
    env._master_tasks.append(PendingTask("测试班X", ex, "g1", teacher="指定老师", room="指定教室"))

    tasks, _ = env._enrich_extra_tasks()
    got = [t for t in tasks if t.class_name == "测试班X"][0]
    assert got.teacher == "指定老师", "覆盖了教秘显式指定的教师"
    assert got.room == "指定教室", "覆盖了教秘显式指定的场地"


def test_no_pool_reports_instead_of_inventing(env):
    """主数据里没有可授教师 → 不编造，如实留空。"""
    ex = _pick_project(env)
    env._master_data.experiments[ex]["teachers"] = set()
    env._master_tasks.append(PendingTask("测试班Y", ex, "g1"))

    tasks, assumptions = env._enrich_extra_tasks()
    got = [t for t in tasks if t.class_name == "测试班Y"][0]
    assert not got.teacher, f"池子为空却编了一个教师：{got.teacher}"
    assert any("未指定" in a or "留空" in a or "为空" in a for a in assumptions), (
        f"没补教师也没说明 —— 教秘会以为系统替他定了。假设：{assumptions}"
    )
