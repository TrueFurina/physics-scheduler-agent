"""跨节次时段的占用口径（R-40）—— 一条记录可以同时占住两个节次

缺陷
----
原表里有 6 条时段写成 `14:00-17:00`。解析时被"吸附"成 `567`（13:30-15:45），
真实结束时间 17:00 就丢了。而 `567` 与 `678`（15:45-18:00）是这样的关系：

    14:00-17:00  ⊃  567 的 14:00-15:45
                 ⊃  678 的 15:45-17:00    ← 1 小时 15 分，整整一段课

于是该教室当天的 `678` 被判为空闲。实测第 12 周周四 A410 原表有"设计性实验
（14:00-17:00）"，沙盘仍把 A410 的 678 列进"可用时段"—— 教秘照着调课，
就是把两个班塞进同一间教室。同类缺陷已经是第三次（R-34 标签分裂、
R-35 整行丢弃），共同点是**用标签相等代替真实时间重叠**。

本文件锁四件事
--------------
1. 区间→节次的换算（含"首尾相接不算重叠"这条边界）；
2. 沙盘不再把被跨走的那个节次推荐成可用；
3. 数据体检能把它报成真冲突（而不是继续 0 冲突）；
4. 审批应用（写路径）在目标周撞上跨节次记录时会跳过，而不是硬塞。
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.utils.excel_parser import ScheduleDatabase, ScheduleEntry
from app.utils.time_model import span_labels, primary_label
from app.agent.reschedule_agent import RescheduleAgent

import app.api.reschedule as api


# ---------------------------------------------------------------------------
# 1. 换算本身
# ---------------------------------------------------------------------------

def test_range_crossing_slot_boundary_occupies_both_slots():
    """14:00-17:00 横跨 15:45 —— 567 和 678 都得算被占。"""
    assert span_labels("14:00-17:00") == ["567", "678"]
    assert primary_label("14:00-17:00") == "567", "主标签取起始时刻所在节次，用于展示"


def test_adjacent_slots_are_not_overlapping():
    """567 结束 15:45、678 开始 15:45 —— 首尾相接不算重叠，别把整张表判满。"""
    assert span_labels("13:30-15:45") == ["567"]
    assert span_labels("15:45-18:00") == ["678"]


def test_single_slot_ranges_stay_single():
    assert span_labels("18:00-21:00") == ["91011"]   # 678 到 18:00 结束，只是相接
    assert span_labels("9:00-12:00") == ["MORNING"]  # 上午不与下午重叠


def test_unparseable_text_is_not_guessed():
    """看不懂就返回空，由调用方按"数据缺口/整日占用"处理 —— 不许瞎猜一个节次。"""
    assert span_labels("、‘") == []
    assert span_labels("") == []


# ---------------------------------------------------------------------------
# 2. 沙盘：被跨走的节次不许再推荐
# ---------------------------------------------------------------------------

def _cross_slot_db() -> ScheduleDatabase:
    """一间教室、一条 `14:00-17:00` 记录（周四，第 12 周）。"""
    db = ScheduleDatabase()
    db.entries = [
        ScheduleEntry(
            12, "周四", "567", "2024级电子科学与技术", "24电科_1",
            "设计性实验（电磁学方向）", "A410", "林珠云",
            time_raw="14:00-17:00", blocked=["567", "678"],
        ),
    ]
    db.loaded = True
    return db


def test_sandbox_does_not_offer_the_half_slot_it_actually_occupies():
    res = RescheduleAgent(_cross_slot_db()).find_slots_with_reasons(12, "林珠云")
    keys = {(s["weekday"], s["time_slot"], s["room"]) for s in res["available"]}
    assert ("周四", "678", "A410") not in keys, \
        "14:00-17:00 已经占住 15:45-17:00，却仍把 678 推荐成可用（R-40 原样复现）"
    assert ("周四", "567", "A410") not in keys
    # 别的天不受影响 —— 修得要准，不能顺手把整周判死
    assert ("周三", "678", "A410") in keys


def test_exclusion_reason_points_back_to_the_original_cell():
    """教秘回表里找的是 678 那一格，而原表写的是 14:00-17:00 —— 必须说清对应关系。"""
    res = RescheduleAgent(_cross_slot_db()).find_slots_with_reasons(12, "林珠云")
    busy = [x for x in res["excluded"]
            if x["weekday"] == "周四" and x["time_slot"] == "678"
            and x["reason_type"] == "teacher_busy"]
    assert busy, "教师维度没有被挡住 —— 说明 blocked 没生效"
    assert "14:00-17:00" in busy[0]["reason"]


def test_note_is_not_added_when_the_record_does_not_cross_any_boundary():
    """原表写 `13:30-15:45` 的记录也是"时间区间"格式，但它只占 567 ——
    对它说"跨到本时段"是把话说重了（第一版凡是区间格式就加注解）。"""
    db = ScheduleDatabase()
    db.entries = [
        ScheduleEntry(12, "周四", "567", "A班", "a1", "设计性实验", "A410", "林珠云",
                      time_raw="13:30-15:45", blocked=["567"]),
    ]
    db.loaded = True
    res = RescheduleAgent(db).find_slots_with_reasons(12, "林珠云")
    busy = [x for x in res["excluded"]
            if x["weekday"] == "周四" and x["time_slot"] == "567"
            and x["reason_type"] == "teacher_busy"]
    assert busy, "567 应该被教师本人挡住"
    assert "跨到本时段" not in busy[0]["reason"]


# ---------------------------------------------------------------------------
# 3. 数据体检：跨节次重叠是真冲突，不能继续报 0
# ---------------------------------------------------------------------------

@pytest.fixture()
def make_db(monkeypatch):
    def _make(entries):
        db = ScheduleDatabase()
        db.entries = entries
        db.loaded = True
        monkeypatch.setattr(api, "_db", db)
        return db
    return _make


def test_data_audit_reports_the_overlap_it_used_to_miss(make_db):
    make_db([
        ScheduleEntry(1, "周四", "567", "A班", "a1", "设计性实验", "A410", "林珠云",
                      time_raw="14:00-17:00", blocked=["567", "678"]),
        ScheduleEntry(1, "周四", "678", "B班", "b1", "声速测量", "A410", "陈老师"),
    ])
    data = __import__("asyncio").run(api.data_audit())["data"]
    assert data["room_conflict_count"] == 1, \
        "14:00-17:00 与 678 在 15:45-17:00 真重叠，体检却说没冲突"
    conflict = data["room_conflicts"][0]
    assert conflict["time_slot"] == "678"
    assert sorted(conflict["involved"]) == ["A班/a1", "B班/b1"]


def test_data_audit_still_says_zero_when_slots_only_touch(make_db):
    """相邻不算冲突：567 结束 15:45 与 678 开始 15:45 是同一间教室的正常连排。"""
    make_db([
        ScheduleEntry(1, "周四", "567", "A班", "a1", "实验一", "A410", "林珠云",
                      time_raw="13:30-15:45", blocked=["567"]),
        ScheduleEntry(1, "周四", "678", "B班", "b1", "实验二", "A410", "陈老师"),
    ])
    data = __import__("asyncio").run(api.data_audit())["data"]
    assert data["room_conflict_count"] == 0


# ---------------------------------------------------------------------------
# 4. 写路径：审批应用时目标周撞上跨节次记录 → 跳过，不许硬塞
# ---------------------------------------------------------------------------

TEACHER = "吴琳"
COURSE = "设计性实验（电磁学方向）"
CLASS = "2024级电子科学与技术"


@pytest.fixture()
def write_db(tmp_path, monkeypatch):
    db = ScheduleDatabase()
    db.entries = [
        # 吴琳的课连排三周，写的是 14:00-17:00（真实占 567+678）
        ScheduleEntry(3, "周二", "567", CLASS, "g1", COURSE, "A504", TEACHER,
                      time_raw="14:00-17:00", blocked=["567", "678"]),
        ScheduleEntry(4, "周二", "567", CLASS, "g1", COURSE, "A504", TEACHER,
                      time_raw="14:00-17:00", blocked=["567", "678"]),
        ScheduleEntry(5, "周二", "567", CLASS, "g1", COURSE, "A504", TEACHER,
                      time_raw="14:00-17:00", blocked=["567", "678"]),
        # 第 7 周同一教室的 678 已被别人占了（15:45 起，与 14:00-17:00 重叠）
        ScheduleEntry(7, "周二", "678", "别的班", "o1", "声速测量", "A504", "陈老师"),
    ]
    db.loaded = True
    monkeypatch.setattr(api, "_db", db)
    monkeypatch.setattr(api, "_DB_PATH", str(tmp_path / "t.db"))
    api._init_db()
    return db


def test_apply_reschedule_skips_when_the_target_half_slot_is_taken(write_db):
    rec = {
        "teacher": TEACHER,
        "course_name": COURSE,
        "class_name": CLASS,
        "original_time": "周二 14:00-17:00",
        "target_week": 7,
        "source_week": 0,
    }
    r = api._apply_reschedule(rec, persist=False)
    assert r["applied"] == 0, f"把 14:00-17:00 的课塞进了别人 678 的教室：{r['details']}"
    assert r["skipped"] == 1
    # 数据本身没被改坏
    assert [e.week for e in write_db.entries if e.teacher == TEACHER] == [3, 4, 5]
