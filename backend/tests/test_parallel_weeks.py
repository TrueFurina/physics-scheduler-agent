"""并行排课的星期口径与周次窗口（R-41）＋ 工单反解精度（R-42/R-43）

三组缺陷，同一个根源：**拿"枚举里有的"当"现实里有的"**。
------------------------------------------------------
R-41a 🟠 周末课被当成不存在
    `WEEKDAYS` 只有周一~周五，于是"这个班平时哪天上课"的投票把周六/周日全丢了。
    真实表 433 条里 71 条（16%）在周末；24级电子信息工程1、2班 46 节课里 42 节在
    周六/周日，概览却显示"常上周一"。

R-41b 🔴 周次窗口写死成 14 周，差一周就整体判负
    `for attempt_weeks in (weeks, weeks + 4)`，默认 start_week=2 / weeks=10 ⇒ 最多到
    第 15 周。而真实表 17 个班有 2 个需要 15 周（23物本 30 单元/2 组；24电信 45 单元/
    5 组/1 教师）⇒ 一键排课对这两个班永远失败，错误文案还写"请尝试其他星期"——
    教秘把 7 个星期挨个点一遍，得到 7 个一模一样的失败。
    同一段代码也不看表里到底有几个周次：weeks=14 时会排出第 17–19 周。

R-41c 🟡 `weeks_used` 填的是窗口宽度
    界面写"18/18 个单元已排定，用 10 周"，而沙盘上只有第 2–7 周有格子
    （实测 2024级制药工程）——"用了 10 周"是假的。

R-42 🔴 工单反解把周末和教室号都读丢了
    云端工单表只存给人读的文案，`_parse_original_time` 是兜底反解：
    · `周[一二三四五]` 不认周六/周日 ⇒ 星期为空 ⇒ 星期不参与匹配；
    · 兜底正则从**教室号**里抠节次：`A412-2` → "412"；
    · 上午时段的卡片文案是"周六 上午 · A412-2"（没有 HH:MM）⇒ 时段读成 "412"
      ⇒ 一个字都匹配不上 ⇒ 审批"通过"了，课表一个字节没变。

R-43 🟠 一张工单搬走两天的课
    星期反解为空时"全都搬"：第 6 周周六、周日各有一条同名课（同教师同教室同实验），
    一张只申请周六那节的工单把周日的也搬走了（实测 applied=2）。
    少动一条教秘能自己补，多动一条他会以为系统在乱改课表。
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.utils.excel_parser import ScheduleDatabase, ScheduleEntry
from app.engine.parallel_scheduler import ParallelScheduler

import app.api.reschedule as R


# ---------------------------------------------------------------------------
# 小表构造 helpers
# ---------------------------------------------------------------------------

def _units_db(n_units: int, n_groups: int, teacher: str = "T",
              cls: str = "排课班", week_lo: int = 2, week_hi: int = 16) -> ScheduleDatabase:
    """n_units 个单元（项目×组）铺在 week_lo..week_hi，一位教师、一间教室。"""
    db = ScheduleDatabase()
    entries = []
    for i in range(n_units):
        g = f"{cls}_g{i % n_groups}"
        p = f"实验项目{i // n_groups:02d}"
        w = week_lo + (i % max(1, week_hi - week_lo + 1))
        entries.append(ScheduleEntry(w, "周六", "567", cls, g, p, "A410", teacher))
    # 把整张表的覆盖范围撑到 week_hi（其余周的记录属于别的班，不占用目标班的资源）
    have = {e.week for e in entries}
    for w in range(week_lo, week_hi + 1):
        if w not in have:
            entries.append(ScheduleEntry(w, "周一", "567", "别的班", "f1", "", "B201", "别人"))
    db.entries = entries
    db.loaded = True
    return db


def _apply_db(tmp_path, monkeypatch, entries):
    db = ScheduleDatabase()
    db.entries = entries
    db.loaded = True
    monkeypatch.setattr(R, "_db", db)
    monkeypatch.setattr(R, "_DB_PATH", str(tmp_path / "t.db"))
    R._init_db()
    return db


# ---------------------------------------------------------------------------
# R-41a 周末课必须算进"平时哪天上课"
# ---------------------------------------------------------------------------

def test_overview_counts_weekend_sessions():
    """一个班所有课都在周六 ⇒ 概览必须说"常上周六"，而不是回退成"周四"。"""
    db = ScheduleDatabase()
    db.entries = [
        ScheduleEntry(5 + i, "周六", "567", "24级电子信息工程1、2班", f"g{i}", f"实验{i}", "A410", "鄢仁文")
        for i in range(6)
    ]
    db.loaded = True
    ov = {c["class_name"]: c for c in ParallelScheduler(db).get_course_overview()}
    c = ov["24级电子信息工程1、2班"]
    assert c["weekday"] == "周六", f"周末课没被统计进来，显示成 {c['weekday']}"
    assert c["weekend_sessions"] == 6
    assert c["weekday_mix"] == {"周六": 6}


def test_solver_keeps_the_weekday_the_class_actually_uses():
    """solve 不传 weekday 时沿用历史最常用星期 —— 周末班就该排在周末。"""
    db = _units_db(3, 1, cls="周末班")
    r = ParallelScheduler(db).solve("周末班")
    assert r["success"] is True, r.get("error")
    assert r["weekday"] == "周六"


# ---------------------------------------------------------------------------
# R-41b 周次窗口：够用才行，也不许发明表里没有的周
# ---------------------------------------------------------------------------

def test_solver_reaches_the_last_week_when_the_course_needs_it():
    """45 单元 / 3 组 / 1 位教师 ⇒ 每周最多 3 个 ⇒ 至少 15 周。

    旧代码窗口写死 (10, 14)：两种都不可行 ⇒ 这个班永远排不出来。
    本表覆盖第 2–16 周（15 周），新代码按需要放宽到 15 周后应当排满。
    """
    db = _units_db(45, 3, cls="长课班")
    r = ParallelScheduler(db).solve("长课班")
    assert r["success"] is True, r.get("error")
    assert r["scheduled"] == 45
    assert r["weeks_window"] == 15
    assert max(s["week"] for s in r["schedule"]) <= 16, "排出了本表不存在的周次"


def test_weeks_used_counts_weeks_actually_used():
    """3 个单元、1 个组 ⇒ 实际只占 3 周；窗口有 10 周也不能说"用了 10 周"。"""
    db = _units_db(3, 1, cls="短课班", week_lo=2, week_hi=4)
    for w in range(5, 17):  # 别的班把表撑到第 16 周
        db.entries.append(ScheduleEntry(w, "周一", "567", "别的班", "f1", "", "B201", "别人"))
    r = ParallelScheduler(db).solve("短课班")
    assert r["success"] is True, r.get("error")
    assert r["weeks_used"] == 3, f"实际用了 {r['weeks_used']} 周"
    assert r["weeks_window"] == 10
    assert r["weeks_used"] != r["weeks_window"]


def test_never_invents_weeks_beyond_the_table():
    """需要 16 周而本表只到第 16 周（从第 2 周起仅剩 15 周）⇒ 明确说差几周，
    而不是把第 17 周排出来，更不说"换个星期试试"。"""
    db = _units_db(48, 3, cls="超长课班")
    r = ParallelScheduler(db).solve("超长课班")
    assert r["success"] is False
    assert "至少需要 16 周" in r["error"], r["error"]
    assert "第16周" in r["error"]
    assert "把起始周调早" in r["error"]
    assert "请尝试其他星期" not in r["error"], "换星期解决不了周数不够"


def test_start_week_beyond_the_table_is_refused():
    db = _units_db(6, 2, cls="排课班")
    r = ParallelScheduler(db).solve("排课班", start_week=17)
    assert r["success"] is False
    assert "已超出本表覆盖范围" in r["error"], r["error"]


# ---------------------------------------------------------------------------
# R-42 工单文案反解：星期 / 时段 / 教室
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text,weekday,slot,room", [
    ("周六 9:00-11:15 · A412-2", "周六", "MORNING", "A412-2"),
    ("周六 上午 · A412-2", "周六", "MORNING", "A412-2"),      # 没有 HH:MM，也不能读成 412
    ("周日 14:00-17:00 · A410", "周日", "567", "A410"),
    ("周四 13:30-15:45 · A504", "周四", "567", "A504"),
    ("周五 18:30-20:45 · A412-2", "周五", "91011", "A412-2"),
])
def test_parse_original_time_weekends_and_rooms(text, weekday, slot, room):
    got = R._parse_original_time(text)
    assert got == {"weekday": weekday, "time_slot": slot, "room": room}, got


def test_parse_original_time_does_not_read_slot_from_room_number():
    """`A412-2` 里藏着 "412" —— 旧兜底正则会把时段读成它，于是什么都匹配不上。"""
    got = R._parse_original_time("周六 上午 · A412-2")
    assert got["time_slot"] == "MORNING", got
    assert got["room"] == "A412-2"


# ---------------------------------------------------------------------------
# R-43 写路径：不许一张工单搬走两天的课
# ---------------------------------------------------------------------------

VAC = "伏安法测二极管的特性曲线"


def _weekend_entries():
    """第 6 周周六、周日各一节同名课（同教师、同教室、同实验、同班不同组）。"""
    return [
        ScheduleEntry(6, "周六", "MORNING", "24服工", "24服工_4", VAC, "A412-2", "林珠云",
                      time_raw="9:00-11:15", blocked=["MORNING"]),
        ScheduleEntry(6, "周日", "MORNING", "24服工", "24服工_5", VAC, "A412-2", "林珠云",
                      time_raw="9:00-11:15", blocked=["MORNING"]),
    ]


def test_application_moves_only_the_day_it_names(tmp_path, monkeypatch):
    db = _apply_db(tmp_path, monkeypatch, _weekend_entries())
    r = R._apply_reschedule({
        "teacher": "林珠云", "class_name": "24服工", "course_name": VAC,
        "original_time": "周六 9:00-11:15 · A412-2", "target_week": 8, "source_week": 6,
    }, persist=False)
    assert r["applied"] == 1, r
    weeks = {(e.weekday, e.week) for e in db.entries}
    assert weeks == {("周六", 8), ("周日", 6)}, f"周日的课被一起搬走了：{weeks}"


def test_application_without_weekday_refuses_instead_of_moving_both_days(tmp_path, monkeypatch):
    """申请没写星期 ⇒ 两天各一节同名课 ⇒ 宁可不动，也不能一次搬两条。"""
    db = _apply_db(tmp_path, monkeypatch, _weekend_entries())
    r = R._apply_reschedule({
        "teacher": "林珠云", "class_name": "24服工", "course_name": VAC,
        "original_time": "9:00-11:15 · A412-2", "target_week": 8, "source_week": 6,
    }, persist=False)
    assert r["applied"] == 0, r
    assert r["skipped"] == 2
    assert "周六" in r["details"][0] and "周日" in r["details"][0]
    assert "未做任何改动" in r["details"][0]
    assert {e.week for e in db.entries} == {6}, "拒绝了还动了数据"


def test_application_skips_target_conflict_by_real_span_not_label(tmp_path, monkeypatch):
    """源记录 `14:00-17:00`（主标签 567，真实占 567+678）搬到第 8 周，
    而第 8 周同教室的 678 已有课 —— 主标签相等会判"不冲突"，把两节课叠进同一间屋。"""
    entries = [
        ScheduleEntry(6, "周六", "567", "24服工", "g1", "设计性实验", "A410", "林珠云",
                      time_raw="14:00-17:00", blocked=["567", "678"]),
        ScheduleEntry(8, "周六", "678", "别的班", "x", "声速测量", "A410", "陈老师"),
    ]
    db = _apply_db(tmp_path, monkeypatch, entries)
    r = R._apply_reschedule({
        "teacher": "林珠云", "class_name": "24服工", "course_name": "设计性实验",
        "original_time": "周六 14:00-17:00 · A410", "target_week": 8, "source_week": 6,
    }, persist=False)
    assert r["applied"] == 0 and r["skipped"] == 1, r
    assert [e.week for e in db.entries if e.class_name == "24服工"] == [6]


def test_application_positive_control_other_weekday_is_free(tmp_path, monkeypatch):
    """同上，但第 8 周那节占位课在周日 ⇒ 周六搬得过去（修要修准，不能一律跳过）。"""
    entries = [
        ScheduleEntry(6, "周六", "567", "24服工", "g1", "设计性实验", "A410", "林珠云",
                      time_raw="14:00-17:00", blocked=["567", "678"]),
        ScheduleEntry(8, "周日", "678", "别的班", "x", "声速测量", "A410", "陈老师"),
    ]
    db = _apply_db(tmp_path, monkeypatch, entries)
    r = R._apply_reschedule({
        "teacher": "林珠云", "class_name": "24服工", "course_name": "设计性实验",
        "original_time": "周六 14:00-17:00 · A410", "target_week": 8, "source_week": 6,
    }, persist=False)
    assert r["applied"] == 1, r
    assert [e.week for e in db.entries if e.class_name == "24服工"] == [8]
