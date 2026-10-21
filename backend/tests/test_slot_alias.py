"""时段标签分裂 / 时段看不懂 / 教师互斥只覆盖一间教室（R-33、R-34、R-35）

三个缺陷都属于同一族：**资源被占用这件事，在模型里少了**。
少了它不会报错、不会崩溃，只会让界面理直气壮地给出"可用"——
任何 UI 测试都会通过，因为界面诚实地展示了后端给它的（错的）结论。

R-33 并行排课「教师互斥」只对最后一间教室生效
----------------------------------------------
`model.Add(x[(i, w, t, r)] == 0)` 写在 `for r` 循环**之外**，于是 `r` 停在 n_r-1，
只有排序最后的那个教室被禁。真实表 17 个班里 **7 个班、共 48 处**教师同一时段
被排了两个班（独立校验脚本 `scripts/audit_conflicts.py` 实测，修复后 0 处）。

R-34 时段别名分裂
-----------------
`15:50-18:05` / `78`（下午 7-8 节）被单独留成一个标签，而沙盘/求解器/体检只枚举
567/678/91011 —— 真实表里 7 条这样写的记录（第 1/3/5/7/9/13/15 周 周四）完全隐形。
它跟 678（15:45-18:00）真实时间重叠 2 小时 10 分：教室已经被占，沙盘照样推荐。
实测：吴琼第 7 周周四在 A502 有课，沙盘把 A502 列成可用，且她自己那节课也没被算进
"教师该时段已有课"。

R-35 时段看不懂就整行丢掉
------------------------
第 3 周周五的时段格填的是「、‘」。整行被静默剔除 ⇒ 那天 A504 全天"空闲"，
而翁铭华本人正在这间教室上"扭摆法测物体转动惯量"。他自己申请调课时，
系统把 A504 周五 567 推荐给了他本人。
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.agent.reschedule_agent import RescheduleAgent
from app.utils.excel_parser import TIME_SLOT_NORMALIZE, ScheduleDatabase, ScheduleEntry

CANONICAL = {"567", "678", "91011", "MORNING"}
REAL_XLSX = os.path.join(os.path.dirname(__file__), "..", "..", "schedule.xlsx")


# ---------------------------------------------------------------------------
# R-34：任何写法都必须落在同一个标准标签上
# ---------------------------------------------------------------------------

def test_every_raw_label_maps_into_the_same_canonical_set():
    """标签一旦分裂，两类记录就互相看不见 —— 冲突检查用的是标签相等。"""
    mapped = set(TIME_SLOT_NORMALIZE.values())
    assert mapped <= CANONICAL, f"存在非标准标签：{mapped - CANONICAL}"


def test_afternoon_late_is_one_slot_not_two():
    """7-8 节与 6-7-8 节真实时间重叠 2h10m，必须是同一个标签。"""
    assert TIME_SLOT_NORMALIZE["15:50-18:05"] == TIME_SLOT_NORMALIZE["15:45-18:00"]
    assert TIME_SLOT_NORMALIZE["78"] == TIME_SLOT_NORMALIZE["678"]


@pytest.mark.skipif(not os.path.exists(REAL_XLSX), reason="真实排课表不在仓库里")
def test_real_sheet_has_no_legacy_78_entries():
    db = ScheduleDatabase()
    db.load_from_excel(os.path.abspath(REAL_XLSX))
    legacy = [e for e in db.entries if e.time_slot == "78"]
    assert legacy == [], f"仍有 {len(legacy)} 条记录停留在旧标签 78"


@pytest.mark.skipif(not os.path.exists(REAL_XLSX), reason="真实排课表不在仓库里")
def test_real_sheet_unparsed_rows_are_kept_not_dropped():
    """看不懂的时段要留痕：否则"少扫了几条"这件事没人知道。"""
    db = ScheduleDatabase()
    db.load_from_excel(os.path.abspath(REAL_XLSX))
    assert db.skipped, "时段无法识别的行被静默丢弃了（skipped 为空）"
    assert all(s["raw_ts"] for s in db.skipped)
    assert any(s["room"] for s in db.skipped), "skipped 里应保留场地，否则无法按整日占用处理"


# ---------------------------------------------------------------------------
# R-34 行为级：教师自己那节"78 时段"的课，必须挡住 678
# ---------------------------------------------------------------------------

def _agent(db: ScheduleDatabase) -> RescheduleAgent:
    return RescheduleAgent(db, student=None)


def test_teacher_busy_at_afternoon_late_blocks_the_same_slot():
    db = ScheduleDatabase()
    db.entries = [
        # 目标教师下午 7-8 节有课（旧代码里这条记录的时间标签是 "78"，与 678 互不可见）
        ScheduleEntry(7, "周四", "678", "23物本", "1组", "光的衍射", "A502", "吴琼"),
        # 另一间教室这天没课，用来确保时段本身还存在可用项
        ScheduleEntry(7, "周二", "567", "24电子1班", "1组", "声速测量", "A101", "陈老师"),
    ]
    db.loaded = True

    r = _agent(db).find_slots_with_reasons(7, "吴琼")
    thursday_678 = [e for e in r["excluded"] if e["weekday"] == "周四" and e["time_slot"] == "678"]
    assert thursday_678, "教师本人在周四 678 有课，整段时间必须被排除"
    assert all(e["reason_type"] == "teacher_busy" for e in thursday_678)
    assert not [a for a in r["available"] if a["weekday"] == "周四" and a["time_slot"] == "678"]


# ---------------------------------------------------------------------------
# R-35：时段看不懂 ⇒ 那天那间教室按整日占用
# ---------------------------------------------------------------------------

def _db_with_unparsed() -> ScheduleDatabase:
    db = ScheduleDatabase()
    db.entries = [
        ScheduleEntry(3, "周一", "567", "24电子9班", "1组", "占位课", "A999", "陈老师"),
        # A504 必须出现在 entries 里才会进入"候选教室"列表（真实表里它当然在）
        ScheduleEntry(3, "周一", "91011", "24电子8班", "1组", "占位课2", "A504", "李老师"),
    ]
    db.skipped = [{
        "week": 3, "weekday": "周五", "raw_ts": "、‘",
        "class_name": "2024级软件工程1、2班",
        "experiment": "扭摆法测物体转动惯量",
        "room": "A504", "teacher": "翁铭华",
    }]
    db.loaded = True
    return db


def test_unparsed_slot_blocks_the_room_for_the_whole_day():
    """别的老师也不能被推荐到那间教室 —— 不知道几点，就整天别用。"""
    r = _agent(_db_with_unparsed()).find_slots_with_reasons(3, "陈老师")

    room_hits = [e for e in r["excluded"] if e["weekday"] == "周五" and e.get("room") == "A504"]
    assert {e["time_slot"] for e in room_hits} == {"567", "678", "91011"}
    assert all(e["reason_type"] == "classroom_busy" for e in room_hits)
    # 必须把原文摊出来，否则教秘不知道该去改哪一格
    assert all("、‘" in e["reason"] for e in room_hits)
    assert not [a for a in r["available"] if a["weekday"] == "周五" and a["room"] == "A504"]


def test_unparsed_slot_also_blocks_the_teacher_for_the_whole_day():
    r = _agent(_db_with_unparsed()).find_slots_with_reasons(3, "翁铭华")

    teacher_hits = [e for e in r["excluded"] if e["weekday"] == "周五" and e["reason_type"] == "teacher_busy"]
    assert {e["time_slot"] for e in teacher_hits} == {"567", "678", "91011"}
    # 别把别的日子也封掉
    assert [a for a in r["available"] if a["weekday"] == "周一"]


# ---------------------------------------------------------------------------
# R-33：并行排课的教师互斥必须覆盖每一间教室
# ---------------------------------------------------------------------------

def test_parallel_solver_teacher_clash_blocks_all_rooms_not_just_the_last():
    """回归锁：约束写在 `for r` 循环外面时，只有排序最后的教室被禁。

    构造三间教室，排序后最后一间是 A103。第 2 周周五：
      · 567：A101 被别班甲占（任课教师正是 T 老师），A102/A103 空
      · 678、91011：三间教室全被别的班占满

    于是"第 2 周"唯一可用的格子是 (567, A102)。
    教师互斥若只覆盖最后一间教室（A103），求解器会把这个格子给目标班，
    T 老师就在同一时段同时给两个班上课（真实表实测过 48 处同款）。
    正确实现下第 2 周整体不可行，最早只能排到第 3 周。
    """
    from app.engine.parallel_scheduler import ParallelScheduler

    db = ScheduleDatabase()
    db.entries = [
        # 目标班的一个单元（任课教师 T）
        ScheduleEntry(9, "周五", "567", "目标班", "目标班_1", "光的偏振", "A101", "T老师"),
        # 567：T 老师在 A101 给别班甲上课
        ScheduleEntry(2, "周五", "567", "别班甲", "别班甲_1", "声速测量", "A101", "T老师"),
    ]
    # 678 / 91011 三间教室全占满
    for i, room in enumerate(("A101", "A102", "A103")):
        for j, slot in enumerate(("678", "91011")):
            db.entries.append(
                ScheduleEntry(2, "周五", slot, f"别班r{i}{j}", f"别班r{i}{j}_1",
                              "惠斯通电桥研究", room, f"其他{i}{j}老师")
            )
    db.loaded = True

    result = ParallelScheduler(db).solve("目标班", weekday="周五", start_week=2, weeks=4)
    assert result["success"], result
    units = result["schedule"]
    assert len(units) == 1

    u = units[0]
    assert not (u["week"] == 2 and u["time_slot"] == "567"), (
        f"把 T 老师排到了他已经在别班上课的时段：第{u['week']}周 {u['weekday']} "
        f"{u['time_slot']} {u['room']}（他此刻正在别班甲 A101 上课）"
    )
