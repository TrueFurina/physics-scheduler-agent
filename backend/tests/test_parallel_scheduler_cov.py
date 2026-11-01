"""parallel_scheduler 长尾覆盖补测（R-104）

此前用覆盖率探针定位，本文件 15 行未覆盖里：
  ▸ _unit_day_map 129-134（大纲新增单元按天负载均衡的兜底，从未被执行）
  ▸ solve 324 / 345（无数据 / 无场地的优雅失败；机器/脏数据下若崩了教秘只看到 500）
  ▸ solve_school 163（某班一条待排单元都没有时的兜底，否则空班直接把整轮拖红）
  ▸ get_course_overview 81 / 88（extra_tasks「大纲待排任务」并入总览的循环，整段 0 覆盖）
  ▸ solve 312（跳过不属本班 / 缺实验或组的记录）

都用确定性小表构造，不依赖 schedule.xlsx，因此干净检出也跑得起来。
329（wd 兜底成周四）/ 547（MODEL_INVALID 不再重试）/ 559（最终不可行返回）属于
防御性或 CP-SAT 罕见终态，见文末说明，本轮不强行覆盖。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.engine.parallel_scheduler import ParallelScheduler
from app.utils.excel_parser import ScheduleDatabase, ScheduleEntry, PendingTask


def _entry(week, weekday, slot, cls, group, exp, room, teacher="T"):
    return ScheduleEntry(week, weekday, slot, cls, group, exp, room, teacher)


def _db_with(entries):
    db = ScheduleDatabase()
    db.entries = entries
    db.loaded = True
    return db


# ---------------------------------------------------------------------------
# solve 的优雅失败：脏数据下必须"说清楚"而不是崩
# ---------------------------------------------------------------------------

def test_solve_unknown_class_returns_error_not_crash():
    """目标班在表里根本不存在 → 必须返回 success=False 并说明"未找到"，而不是抛异常。"""
    # 表里只有别的班的记录（顺带覆盖 312：跳过不属本班的记录）
    db = _db_with([_entry(2, "周四", "567", "别的班", "1组", "E1", "A410")])
    r = ParallelScheduler(db).solve("不存在的班")
    assert r["success"] is False
    assert "未找到" in r["error"]


def test_solve_no_rooms_returns_error_not_crash():
    """该班所有记录都没填场地 → get_all_rooms 为空 ⇒ 必须报"没有可用场地"，而不是空跑求解器。"""
    db = _db_with([
        _entry(2, "周四", "567", "排课班", "1组", "E1", ""),   # room 为空
        _entry(3, "周四", "567", "排课班", "2组", "E1", ""),
    ])
    r = ParallelScheduler(db).solve("排课班")
    assert r["success"] is False
    assert "没有可用场地" in r["error"]


# ---------------------------------------------------------------------------
# solve_school：某一班一条待排单元都没有时的兜底（163）
# ---------------------------------------------------------------------------

def test_solve_school_empty_class_does_not_abort_whole_run():
    """全校一键里夹着一个空班：该班如实记 failed，但整轮不能因它而崩。"""
    db = _db_with([_entry(2, "周四", "567", "正常班", "1组", "E1", "A410")])
    result = ParallelScheduler(db).solve_school(
        [{"class_name": "正常班", "weekdays": ["周四"]},
         {"class_name": "空班", "weekdays": ["周四"]}]
    )
    # 空班被如实记为 failed，而不是让整轮抛异常
    failed_names = {f["class_name"] for f in result["classes_failed"]}
    assert "空班" in failed_names
    # 正常班仍正常产出
    assert any(c["class_name"] == "正常班" and c["success"] for c in result["per_class"])


# ---------------------------------------------------------------------------
# _unit_day_map：大纲新增单元（无历史星期）按天负载最轻分配（129-134）
# ---------------------------------------------------------------------------

def test_unit_day_map_load_balances_new_units():
    """已有单元固定在历史星期，大纲新增单元（无历史）必须落到当前负载最轻的一天。"""
    db = _db_with([_entry(2, "周四", "567", "C", "1组", "E1", "A410")])
    # 大纲新增单元 E2（表里没有对应记录 ⇒ 无星期投票）
    sched = ParallelScheduler(db, extra_tasks=[
        PendingTask("C", "E2", "1组", weekday=""),
    ])
    mapping = sched._unit_day_map("C", ["周四", "周六"])
    # 历史单元 E1 固定在周四
    assert mapping[("E1", "1组")] == "周四"
    # 新增单元 E2 无历史 ⇒ 落到负载更轻的周六（周四已被 E1 占 1）
    assert mapping[("E2", "1组")] == "周六"


# ---------------------------------------------------------------------------
# get_course_overview：extra_tasks（大纲待排任务）并入总览（81 / 88）
# ---------------------------------------------------------------------------

def test_overview_counts_extra_tasks_pending_units():
    """总览必须把大纲待排任务算进 pending_units，且绪论/考试类不污染统计。"""
    db = _db_with([_entry(2, "周四", "567", "C", "1组", "E1", "A410")])
    sched = ParallelScheduler(db, extra_tasks=[
        PendingTask("C", "E2", "1组", weekday="周六"),   # 真实待排 ⇒ 计 pending
        PendingTask("C", "绪论导读", "1组", weekday="周一"),  # 绪论 ⇒ 跳过（81）
    ])
    ov = {c["class_name"]: c for c in sched.get_course_overview()}
    c = ov["C"]
    # E2 在周六有投票 ⇒ 星期分布含周六（88）
    assert c["weekday_mix"].get("周六") == 1
    # 绪论单元不进 projects，但 E2 作为待排被计入
    assert c["pending_units"] == 1
    proj_names = {p["project"] for p in c["projects"]}
    assert "E2" in proj_names
    assert "绪论导读" not in proj_names


# ---------------------------------------------------------------------------
# _unit_day_map：大纲任务与历史单元撞同一 (项目,组) ⇒ 已在 mapping 中，应跳过（131）
# （上面的 load_balances 测试只走了「新任务→分配天」的 132-134，没走「已投票→continue」）
# ---------------------------------------------------------------------------

def test_unit_day_map_skips_already_voted_unit():
    """大纲新增任务若与历史已排单元同 (项目,组)，不应重复占一天（131 的 continue）。"""
    db = _db_with([_entry(2, "周四", "567", "C", "1组", "E1", "A410")])
    sched = ParallelScheduler(db, extra_tasks=[
        PendingTask("C", "E1", "1组", weekday=""),   # 与历史单元同 (E1,1组) ⇒ 已在 mapping
    ])
    mapping = sched._unit_day_map("C", ["周四", "周六"])
    # (E1,1组) 只出现一次，落在历史星期周四；不因为大纲又列了一遍而多占周六
    assert mapping[("E1", "1组")] == "周四"
    assert len(mapping) == 1


# ---------------------------------------------------------------------------
# solve：待排任务与 db 已有单元撞同 (项目,组) ⇒ 去重时跳过（312）
# ---------------------------------------------------------------------------

def test_solve_skips_duplicate_pending_task_already_in_db():
    """大纲待排任务若和安排表里已有单元同 (项目,组)，求并集时应当去重（312 的 continue）。"""
    db = _db_with([_entry(2, "周四", "567", "C", "1组", "E1", "A410")])
    sched = ParallelScheduler(db, extra_tasks=[
        PendingTask("C", "E1", "1组", weekday="周四"),  # 与 db 已有 (E1,1组) 重复
    ])
    r = sched.solve("C")
    assert r["success"] is True
    # 去重后只有 1 个单元（E1/1组），不能排成 2 份
    assert r["total_units"] == 1


# ---------------------------------------------------------------------------
# solve：传入非法星期 ⇒ 必须兜底成周四，而不是把非法值带进求解器（329）
# ---------------------------------------------------------------------------

def test_solve_invalid_weekday_falls_back_to_thursday():
    """weekday 不是合法星期（如「火星曜」）→ 兜底成周四（329），不能让脏值污染 CP-SAT。"""
    db = _db_with([_entry(2, "周四", "567", "C", "1组", "E1", "A410")])
    r = ParallelScheduler(db).solve("C", weekday="火星曜")
    assert r["success"] is True
    assert r["weekday"] == "周四"
