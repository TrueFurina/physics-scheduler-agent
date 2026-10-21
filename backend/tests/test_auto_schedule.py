"""一键自动排课的回归锁（R-37 / R-38 / R-39）。

这一批之前，`/auto-schedule` 是首屏第一个按钮，也是全项目**唯一没有任何
正确性测试**的引擎：测试只覆盖过"它没有阻塞事件循环"（test_event_loop_blocking）。

原实现的四个问题都不是崩溃型，而是"自述与事实不符"型 —— 界面永远显示成功：

R-37 没有周次维度
    决策变量只有 (星期, 时段, 教室)，返回的方案里没有 `week` 字段。教秘拿到一份
    "不知道属于哪一周"的课表。用真实表实测同一份方案：落到第 2 周 7/15 条撞课、
    第 8–10 周 10/15 条，第 1 周 2 条 —— 因为它**从不看已有安排表**，
    约束只在本次新生成的任务之间生效。

R-38 容量是编的
    房间容量按"名字里有没有'大'字"（120 / 否则 60），班级人数按"名字里有没有'、'"
    （85 / 否则 60）。真实表 10 间教室（A410…A516）没有一个含"大"字 ⇒ 全部按 60 人
    ⇒ 所有合班任务（合班 ≈ 85 人）永久不可排：实测 30 个任务里 15 个排不进，
    而原因统一写成"资源不足或约束过紧"，把编造的数字伪装成资源问题。
    安排表里根本没有容量字段。

R-39 学生维度只是名义上的
    `self.student` 默认空对象（上传学生课表是管理员接口，前端从不调用），
    `get_busy_slots(1)` 恒为空。note 却写着"教室/教师/学生三重不冲突"。

判据刻意分两类：
  - 用内存小表构造的**因果**用例（放回旧实现必然失败），证明约束真的在起作用；
  - 用真实表的**规模**用例（仅在表存在时跑），证明不会在真实数据上退化。
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.engine.auto_scheduler import AutoScheduler, WEEKDAYS, TIME_SLOTS
from app.utils.excel_parser import ScheduleDatabase, ScheduleEntry

REAL_XLSX = os.path.join(os.path.dirname(__file__), "..", "..", "schedule.xlsx")


def _db(entries, skipped=None):
    d = ScheduleDatabase()
    d.entries = list(entries)
    d.skipped = list(skipped or [])
    d.loaded = True
    return d


def _entry(week, weekday, slot, cls, room, teacher, experiment="扭摆法测物体转动惯量", group="1组"):
    return ScheduleEntry(week, weekday, slot, cls, group, experiment, room, teacher)


def _cells(schedule):
    return {(s["week"], s["weekday"], s["time_slot"]) for s in schedule}


# ---------------------------------------------------------------------------
# R-37 方案必须带周次，且周次落在表内
# ---------------------------------------------------------------------------

def test_plan_always_carries_week_inside_table_range():
    """旧实现的返回值里根本没有 `week` 这个键 —— 教秘不知道该用到哪一周。"""
    db = _db([
        _entry(1, "周一", "567", "一班", "A410", "张老师"),
        _entry(5, "周三", "678", "二班", "A411", "李老师"),
    ])
    res = AutoScheduler(db).solve(max_sections=10)
    assert res["success"] is True
    assert res["weeks"] == [1, 5]
    for item in res["schedule"]:
        assert isinstance(item.get("week"), int)
        assert 1 <= item["week"] <= 5


def test_solver_avoids_rooms_already_used_by_existing_schedule():
    """核心反证：只留一个空格子，求解器必须落到那一格。

    旧实现完全不读 `db.entries`，会把课排到任意格（上面例子之外的地方），
    本条必然失败 —— 这就是"首屏方案按第 2 周落地有 7/15 条撞课"的最小复现。
    """
    # 一间教室 A410，第 1–2 周所有 星期×时段 都被占，只留 (第2周, 周四, 678)
    entries = []
    for week in (1, 2):
        for wd in WEEKDAYS:
            for slot in TIME_SLOTS:
                if (week, wd, slot) == (2, "周四", "678"):
                    continue
                entries.append(_entry(week, wd, slot, f"占位班{week}{wd}{slot}", "A410", "陈老师"))
    db = _db(entries)

    s = AutoScheduler(db)
    s._get_course_sections = lambda: (
        [{"teacher": "吴琳", "course": "声速测量", "class_name": "23物本"}], 0)
    res = s.solve(max_sections=5)

    assert res["success"] is True
    assert len(res["schedule"]) == 1
    got = res["schedule"][0]
    assert (got["week"], got["weekday"], got["time_slot"], got["room"]) == (2, "周四", "678", "A410")
    assert got["teacher"] == "吴琳"


def test_solver_avoids_teacher_already_busy_in_that_week():
    """教师占用是**逐周**的：第 1 周有课不代表第 2 周也有课。"""
    entries = [_entry(1, "周一", "567", "别的班", "A410", "吴琳")]
    db = _db(entries)
    s = AutoScheduler(db)
    s._get_course_sections = lambda: (
        [{"teacher": "吴琳", "course": "声速测量", "class_name": "23物本"}], 0)
    res = s.solve(max_sections=1)
    assert res["success"] is True
    got = res["schedule"][0]
    assert (got["week"], got["weekday"], got["time_slot"]) != (1, "周一", "567")


# ---------------------------------------------------------------------------
# 方案内部的三重互斥
# ---------------------------------------------------------------------------

def test_same_teacher_never_scheduled_twice_in_one_cell():
    db = _db([_entry(1, "周一", "567", "占位", "A410", "别人"),
              _entry(2, "周一", "567", "占位", "A410", "别人")])
    secs = [
        {"teacher": "吴琳", "course": "声速测量", "class_name": "一班"},
        {"teacher": "吴琳", "course": "透镜焦距", "class_name": "二班"},
    ]
    s = AutoScheduler(db)
    s._get_course_sections = lambda: (secs, 0)          # 直接喂任务，隔离任务抽取逻辑
    res = s.solve(max_sections=10)
    assert res["solved"] == 2
    by_cell = {}
    for item in res["schedule"]:
        by_cell.setdefault((item["week"], item["weekday"], item["time_slot"]), []).append(item["teacher"])
    for cell, teachers in by_cell.items():
        assert len(teachers) == len(set(teachers)), f"{cell} 把同一位老师排了两次"


def test_same_class_never_gets_two_courses_in_one_cell():
    """旧实现没有班级互斥：同一个班可以在同一时段被排两门不同的实验课。"""
    db = _db([_entry(1, "周一", "567", "占位", "A410", "别人")])
    secs = [
        {"teacher": "张老师", "course": "声速测量", "class_name": "23物本"},
        {"teacher": "李老师", "course": "透镜焦距", "class_name": "23物本"},
    ]
    s = AutoScheduler(db)
    s._get_course_sections = lambda: (secs, 0)
    res = s.solve(max_sections=10)
    assert res["success"] is True
    by_cell = {}
    for item in res["schedule"]:
        by_cell.setdefault((item["week"], item["weekday"], item["time_slot"]), []).append(item["class_name"])
    for cell, classes in by_cell.items():
        assert len(classes) == len(set(classes)), f"{cell} 把同一个班排了两门课"


def test_room_capacity_is_not_fabricated():
    """R-38 反证：合班任务（人数最多）也必须能排进去。

    旧实现里房间容量按"名字是否含'大'字"估（A410 这类全部 60 人），
    合班估 85 人 ⇒ 永久不可排，实测 30 个任务里 15 个被这样判死。
    现在不做容量校验（表中无容量字段），所以"名字最长的合班"照样能排。
    """
    db = _db([_entry(1, "周一", "567", "占位", "A410", "别人")])
    secs = [
        {"teacher": "张老师", "course": "声速测量", "class_name": "2024级电子信息工程1、2班"},
        {"teacher": "李老师", "course": "透镜焦距", "class_name": "2024级软件工程1、2班"},
    ]
    s = AutoScheduler(db)
    s._get_course_sections = lambda: (secs, 0)
    res = s.solve(max_sections=10)
    assert res["solved"] == 2, f"合班任务被容量规则判死：{res['conflicts']}"
    assert res["capacity_checked"] is False


# ---------------------------------------------------------------------------
# 任务抽取：绪论课不参与
# ---------------------------------------------------------------------------

def test_intro_lessons_are_excluded_and_counted():
    """"绪论课（教室已安排，请查系统）"这类记录的教室不在安排表里，硬排进实验室是错的。"""
    db = _db([
        _entry(1, "周一", "567", "一班", "A410", "张老师", experiment="绪论课（教室已安排，请查系统）"),
        _entry(2, "周一", "567", "一班", "A410", "张老师", experiment="声速测量"),
    ])
    res = AutoScheduler(db).solve(max_sections=10)
    assert res["intro_excluded"] == 1
    assert res["total_sections"] == 1
    assert [x["course"] for x in res["schedule"]] == ["声速测量"]


# ---------------------------------------------------------------------------
# R-39 学生维度如实标注
# ---------------------------------------------------------------------------

def test_student_dimension_reported_as_unchecked_without_data():
    db = _db([_entry(1, "周一", "567", "一班", "A410", "张老师")])
    res = AutoScheduler(db).solve(max_sections=1)
    assert res["student_checked"] is False
    text = AutoScheduler(db).summary(res)
    assert "未校验" in text and "学生" in text, "没校验就必须写出来，不能继续声称三重不冲突"


def test_student_busy_slots_are_avoided_when_data_exists():
    class _StubStudent:
        students = {"某人": object()}

        def get_busy_slots(self, week):
            if week == 1:
                return {"周一": {"567"}}
            return {}

    db = _db([_entry(1, "周一", "567", "占位", "A410", "别人")])
    res = AutoScheduler(db, student=_StubStudent()).solve(max_sections=1)
    assert res["student_checked"] is True
    assert (1, "周一", "567") not in _cells(res["schedule"])


# ---------------------------------------------------------------------------
# 看不懂的时段按整天占用（与沙盘同一口径，R-35）
# ---------------------------------------------------------------------------

def test_unparsed_slot_blocks_the_whole_day_for_that_room():
    db = _db(
        [_entry(2, "周一", "567", "占位", "A410", "别人"),
         _entry(2, "周一", "567", "占位", "A411", "别人")],
        skipped=[{"week": 1, "weekday": "周五", "raw_ts": "、‘", "room": "A410",
                  "teacher": "", "class_name": "某班", "experiment": "声速测量"}],
    )
    res = AutoScheduler(db).solve(max_sections=3)
    assert res["schedule"], "样例数据应能排出至少一条"
    for item in res["schedule"]:
        assert not (item["week"] == 1 and item["weekday"] == "周五" and item["room"] == "A410"), \
            "时段读不出来的记录必须按整日占用来避让"


# ---------------------------------------------------------------------------
# 周次摊平
# ---------------------------------------------------------------------------

def test_week_load_is_spread_instead_of_piled_into_week_one():
    """只最大化数量的话 CP-SAT 会把任务全塞进前几周 —— 那不是能交给教秘的学期初稿。"""
    db = _db([_entry(1, "周一", "567", "占位", "A410", "别人"),
              _entry(3, "周一", "567", "占位", "A410", "别人")])
    secs = [{"teacher": f"T{i}", "course": f"实验{i}", "class_name": f"班{i}"} for i in range(6)]
    s = AutoScheduler(db)
    s._get_course_sections = lambda: (secs, 0)
    res = s.solve(max_sections=10)
    assert res["solved"] == 6
    weeks = [item["week"] for item in res["schedule"]]
    assert max(weeks.count(w) for w in set(weeks)) == 2, f"周次没摊平：{weeks}"


# ---------------------------------------------------------------------------
# 文案
# ---------------------------------------------------------------------------

def test_summary_shows_week_and_unchecked_items():
    db = _db([_entry(1, "周一", "567", "占位", "A410", "别人")])
    s = AutoScheduler(db)
    res = s.solve(max_sections=1)
    text = s.summary(res)
    assert "第1周" in text
    assert "未校验" in text
    assert "容量" in text        # 容量没校验就必须说，不能让读者以为是硬约束


# ---------------------------------------------------------------------------
# 真实表上的规模回归（表不在仓库时自动跳过）
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not os.path.exists(REAL_XLSX), reason="真实排课表不在仓库里")
def test_real_table_plan_has_zero_collisions_with_existing_entries():
    """真实 433 条记录 × 30 个任务：方案不得与任何已有记录同教室/同教师。

    注意这条只做"同标签相等"的粗查，真实时钟区间的独立校验在
    `scripts/audit_conflicts.py --auto`（刻意不复用后端解析）。
    """
    db = ScheduleDatabase()
    db.load_from_excel(os.path.abspath(REAL_XLSX))
    res = AutoScheduler(db).solve(max_sections=30)

    assert res["success"] is True
    assert res["solved"] == res["total_sections"], f"仍有排不进的任务：{res['conflicts'][:3]}"

    room_busy = {(e.week, e.weekday, e.time_slot, e.room) for e in db.entries if e.room}
    teacher_busy = {(e.week, e.weekday, e.time_slot, e.teacher) for e in db.entries if e.teacher}
    for item in res["schedule"]:
        key = (item["week"], item["weekday"], item["time_slot"])
        assert key + (item["room"],) not in room_busy, f"排进了已占用的教室：{item}"
        assert key + (item["teacher"],) not in teacher_busy, f"排到了已有课的教师：{item}"
