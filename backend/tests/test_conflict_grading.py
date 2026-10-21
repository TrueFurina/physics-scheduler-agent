"""冲突分级 —— 核心冲突（场地 / 教师 / 整组）分别识别并说清"该怎么改"

导师业务规则（2026-10-21 座谈）：核心冲突标「必须调整」，仅涉及个别学生的
轻微冲突标「可强制通过」。后者不做（需学生课表，涉及 PII，用户已明确"暂时不管"），
所以本文件锁的是**核心冲突的分级与口径**。

三条硬判据（每条都有对应的真实数据证据）
--------------------------------------
1. 整班冲突按 **(班级, 组)** 判，不按班级判 —— 真实表 433 行里「同班不同组同格」
   有 **47 处**，那是正常分批教学（1 组 A504 扭摆法 / 2 组 A516 声速测量）。
   按班级判会立刻造出 47 处假阳性（R-26 同族错误）。用例 2/3 双向锁死。
2. 空场地不产生场地冲突 —— 旧写法 `o.room == e.room` 在两条都无场地时恒真，
   凭空报冲突（R-26 的教训：没填场地不可能撞场地）。
3. 跨节次按真实占格判重叠（`14:00-17:00` 占 567+678），首尾相接（567 vs 678）不算。
"""

import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.api import reschedule as R
from app.conflict_model import classify_move, scan_table, slots_of
from app.utils.excel_parser import ScheduleDatabase, ScheduleEntry


def _e(week, weekday, slot, cls, group, room, teacher="吴琳", exp="扭摆法", blocked=None):
    e = ScheduleEntry(week, weekday, slot, cls, group, exp, room, teacher)
    if blocked:
        e.blocked = blocked
    return e


# ---------------------------------------------------------------- 1. 三类分别识别

def test_classify_reports_each_core_kind_separately():
    """同一格撞上三种资源 → 三类都要报出来，且各自带处理建议。"""
    entries = [
        _e(9, "周二", "567", "别的班", "别班_1", "A504", teacher="陈老师", exp="声速测量"),
        _e(9, "周二", "567", "又一班", "又班_1", "A999", teacher="吴琳", exp="光的衍射"),
        _e(9, "周二", "567", "本班", "本班_1", "A888", teacher="林老师", exp="牛顿环"),
    ]
    hits = classify_move(entries, week=9, weekday="周二", slots={"567"},
                         room="A504", teacher="吴琳", class_name="本班", group="本班_1")
    kinds = {h["kind"] for h in hits}
    assert kinds == {"room", "teacher", "group"}, kinds
    assert all(h["severity"] == "core" and h["can_force"] is False for h in hits)
    for h in hits:
        assert h["label"] and h["fix_hint"] and h["detail"]
    # 最难解决的排最前：整组 > 教师 > 场地
    assert [h["kind"] for h in hits][0] == "group"


def test_classify_returns_nothing_when_cell_free():
    entries = [_e(9, "周二", "567", "别的班", "别班_1", "A504", teacher="陈老师")]
    assert classify_move(entries, week=9, weekday="周二", slots={"678"},
                         room="A504", teacher="陈老师", class_name="别的班",
                         group="别班_1") == []


# ---------------------------------------------------------------- 2. 反向验证

def test_same_class_different_group_is_not_a_conflict():
    """同班不同组同格 = 正常分批教学，绝不能报整组冲突。"""
    entries = [
        _e(4, "周五", "567", "2024级软件工程1、2班", "24软工1_1", "A504", teacher="吴琳"),
        _e(4, "周五", "567", "2024级软件工程1、2班", "24软工2_1", "A516", teacher="林老师"),
    ]
    hits = classify_move(entries, week=4, weekday="周五", slots={"567"},
                         room="A777", teacher="陈老师",
                         class_name="2024级软件工程1、2班", group="24软工3_1")
    assert hits == [], f"同班不同组被误判成冲突：{[h['kind'] for h in hits]}"


def test_group_conflict_by_class_would_be_false_positive_on_real_table():
    """判据选择的证据：真实表上"按班级判"会报出几十处，按 (班级,组) 判是 0。

    如果有人把整组判据改成按班级判，这条会立刻红。
    """
    real = os.path.join(os.path.dirname(__file__), "..", "..", "schedule.xlsx")
    if not os.path.exists(real):
        pytest.skip("仓库根没有 schedule.xlsx")
    db = ScheduleDatabase()
    db.load_from_excel(real)

    naive = 0          # 同班级同格、涉及多个不同组（按班级判会报的）
    real_hits = 0      # 同班级**同组**同格两条（真正的整组冲突）
    cells = {}
    for e in db.entries:
        for s in slots_of(e):
            cells.setdefault((e.week, e.weekday, s, (e.class_name or "").strip()), []).append(e)
    for _k, v in cells.items():
        pairs = {(x.class_name, x.group) for x in v if x.group}
        if len(pairs) > 1:
            naive += 1
        if len(v) > 1 and len(pairs) == 1:
            real_hits += 1

    assert naive > 0, "真实表里应该有同班不同组的并行教学（否则本用例失去意义）"
    assert real_hits == 0, f"真实表不该有整组冲突，却出现 {real_hits} 处"
    assert scan_table(db.entries)["group_conflicts"] == []


# ---------------------------------------------------------------- 3/4. 真实表基线

def test_real_table_baseline():
    """真实 433 条：场地 0 / 教师 1 / 整组 0 / 同班并行 47（口径锁死）。"""
    real = os.path.join(os.path.dirname(__file__), "..", "..", "schedule.xlsx")
    if not os.path.exists(real):
        pytest.skip("仓库根没有 schedule.xlsx")
    db = ScheduleDatabase()
    db.load_from_excel(real)
    s = scan_table(db.entries)

    assert len(s["room_conflicts"]) == 0
    assert len(s["teacher_conflicts"]) == 1
    assert len(s["group_conflicts"]) == 0
    assert s["parallel_groups"] == 47
    # 那 1 处教师冲突确实是两条绪论课（无场地），把它摊开说清楚
    c = s["teacher_conflicts"][0]
    assert c["severity"] == "core" and c["kind"] == "teacher"
    assert c["week"] == 1 and c["weekday"] == "周五"


# ---------------------------------------------------------------- 5/6/7. 时段与空场地

def test_cross_slot_record_blocks_both_slots():
    """`14:00-17:00` 同时占 567 和 678：目标落在 678 也要判冲突（R-40）。"""
    entries = [_e(9, "周二", "567", "别的班", "别班_1", "A504", teacher="陈老师",
                  blocked=["567", "678"])]
    assert classify_move(entries, week=9, weekday="周二", slots={"678"},
                         room="A504", teacher="陈老师") != []


def test_adjacent_slots_are_not_a_conflict():
    """567（13:30-15:45）与 678（15:45-18:00）首尾相接，不算重叠。"""
    entries = [_e(9, "周二", "567", "别的班", "别班_1", "A504", teacher="陈老师")]
    assert classify_move(entries, week=9, weekday="周二", slots={"678"},
                         room="A504", teacher="陈老师") == []


def test_missing_room_never_reports_room_conflict():
    """两条都无场地 → 旧写法 `o.room == e.room` 恒真会误判；现在只报教师冲突。"""
    entries = [_e(9, "周二", "567", "别的班", "别班_1", "", teacher="吴琳", exp="绪论课")]
    hits = classify_move(entries, week=9, weekday="周二", slots={"567"},
                         room="", teacher="吴琳", class_name="本班", group="本班_1")
    assert {h["kind"] for h in hits} == {"teacher"}, [h["kind"] for h in hits]


# ---------------------------------------------------------------- 8/9. apply-move 分级

@pytest.fixture
def db(tmp_path, monkeypatch):
    d = ScheduleDatabase()
    d.entries = [
        ScheduleEntry(3, "周二", "567", "本班", "本班_1", "扭摆法", "A504", "吴琳"),
        ScheduleEntry(4, "周二", "567", "本班", "本班_1", "扭摆法", "A504", "吴琳"),
        # 第 9 周：同一间教室同格被两个班占了 —— 这是**表内已存在**的场地冲突
        ScheduleEntry(9, "周二", "567", "别的班", "别班_1", "声速测量", "A504", "陈老师"),
        ScheduleEntry(9, "周二", "567", "第三班", "三班_1", "光的衍射", "A504", "林老师"),
        # 第 10 周：同一班级同一组已有课（整组冲突 —— 此前完全不查）
        ScheduleEntry(10, "周二", "567", "本班", "本班_1", "牛顿环", "A999", "林老师"),
        # 第 11 周：同班不同组同格 —— 正常分批教学，不是冲突（口径要说得出口）
        ScheduleEntry(11, "周二", "567", "并行班", "并行1_1", "扭摆法", "A504", "吴琳"),
        ScheduleEntry(11, "周二", "567", "并行班", "并行2_1", "声速测量", "A516", "陈老师"),
    ]
    d.loaded = True
    monkeypatch.setattr(R, "_db", d)
    monkeypatch.setattr(R, "_DB_PATH", str(tmp_path / "t.db"))
    R._init_db()
    return d


def test_apply_reports_room_conflict_with_kind(db):
    res = R._apply_reschedule({
        "teacher": "吴琳", "course_name": "扭摆法", "class_name": "本班",
        "original_time": "周二 13:30-15:45", "target_week": 9, "source_week": 0,
    }, persist=False)
    assert res["applied"] == 0 and res["skipped"] == 1
    assert {c["kind"] for c in res["conflicts"]} == {"room"}
    assert "场地被占" in res["details"][0], res["details"]
    assert "换一间教室" in res["details"][0], res["details"]


def test_apply_reports_group_conflict_that_used_to_be_invisible(db):
    """整组冲突此前不在判定里 —— 这条在旧代码上会"移动成功"，制造分身。"""
    res = R._apply_reschedule({
        "teacher": "吴琳", "course_name": "扭摆法", "class_name": "本班",
        "original_time": "周二 13:30-15:45", "target_week": 10, "source_week": 0,
    }, persist=False)
    assert res["applied"] == 0, "整组冲突被漏掉了，课被挪到该组已有的格子上"
    assert {c["kind"] for c in res["conflicts"]} == {"group"}
    assert "同一班级同一组" in res["details"][0]


def test_apply_still_moves_when_no_conflict(db):
    res = R._apply_reschedule({
        "teacher": "吴琳", "course_name": "扭摆法", "class_name": "本班",
        "original_time": "周二 13:30-15:45", "target_week": 7, "source_week": 0,
    }, persist=False)
    assert res["applied"] == 1 and res["skipped"] == 0 and res["conflicts"] == []


# ---------------------------------------------------------------- 10. 体检口径

def test_data_audit_exposes_group_dimension_and_parallel_note(db):
    """体检要把整组维度 + 「同班并行不算冲突」的口径一起说出来。"""
    data = asyncio.run(R.data_audit())["data"]
    assert data["group_conflict_count"] == 0
    assert data["core_conflict_count"] == 1          # 本表里第 9 周那处场地冲突
    assert data["room_conflict_count"] == 1
    assert "同班级不同组" in (data["parallel_note"] or "")
    assert data["room_conflicts"][0]["severity"] == "core"

    real = os.path.join(os.path.dirname(__file__), "..", "..", "schedule.xlsx")
    if os.path.exists(real):
        big = ScheduleDatabase()
        big.load_from_excel(real)
        db.entries = big.entries
        d2 = asyncio.run(R.data_audit())["data"]
        assert d2["parallel_group_count"] == 47
        assert d2["group_conflict_count"] == 0
        assert "47" in d2["parallel_note"]
