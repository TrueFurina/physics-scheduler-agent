"""周课表网格（R-62）回归测试。

锁死的契约
----------
1. **7 天 × 4 时段**：周末条目（真实表 71/433）和上午时段（4 条）必须在网格里
   ——R-41「拿枚举当现实」在展示层的回归（前端 labels.ts 的 5 天 × 3 时段常量
   就是那个错误的现存样本，本模块刻意不复用它们）；
2. **跨节次记录在每个占用的节次各出现一次**（R-40：`14:00-17:00` 占 567+678，
   只显示主标签会让另一格看起来是空的）；
3. **没填场地的记录照常入格**并打 `missing_room`（R-35：不静默丢）；
4. **冲突标记吃 scan_table 的结论**，不自己重算；且**同班不同组同格不标**
   （真实表 47 处正常分批，标了就是假阳性——R-26 同族）；
5. **周次越界报错不夹取**（R-32）；
6. 时段无法识别的行（skipped）按周报数，不静默少显示；
7. 班级/场地筛选只筛单元格，**冲突计数是全周全表口径**（按班筛选显示
   "0 冲突"会让教秘以为别的班也没冲突）。
"""

import os
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import schedule_grid
from app.api import reschedule as api
from app.conflict_model import scan_table
from app.main import app
from app.utils.excel_parser import ScheduleDatabase, ScheduleEntry


def _entry(week, weekday, slot, cls, group, room, teacher, experiment,
           blocked=None, time_raw=""):
    return ScheduleEntry(week, weekday, slot, cls, group, experiment, room, teacher,
                         time_raw=time_raw, blocked=blocked or [slot])


def _grid_entries():
    """11 行，覆盖：三类核心冲突、同班不同组并行、周末、无场地、跨节次、跨周。"""
    return [
        # 第2周周一 567：教室冲突（甲乙同室不同班）+ 教师冲突（吴老师两地）
        _entry(2, "周一", "567", "甲班", "1组", "A410", "吴老师", "项目A"),
        _entry(2, "周一", "567", "乙班", "1组", "A410", "王老师", "项目B"),
        _entry(2, "周一", "567", "丙班", "1组", "A412", "吴老师", "项目C"),
        # 第2周周一 678：同班不同组同格、**不同教师** ⇒ 正常分批，不是冲突
        _entry(2, "周一", "678", "甲班", "1组", "A504", "林老师", "项目D"),
        _entry(2, "周一", "678", "甲班", "2组", "A516", "陈老师", "项目E"),
        # 周末条目（R-41）：周六有课 + 周日上午绪论（且没填场地）
        _entry(2, "周六", "567", "甲班", "1组", "A410", "吴老师", "周末项目"),
        _entry(2, "周日", "MORNING", "乙班", "1组", "", "", "周日绪论"),
        # 跨节次（R-40）：一条记录占 567+678 两格
        _entry(3, "周三", "567", "甲班", "1组", "A410", "吴老师", "长实验",
               blocked=["567", "678"], time_raw="14:00-17:00"),
        # 整组冲突：同班同组同一格两条（周二 567，不同教室）
        _entry(2, "周二", "567", "甲班", "1组", "A410", "吴老师", "项目G"),
        _entry(2, "周二", "567", "甲班", "1组", "A413", "吴老师", "项目H"),
        # 其它周（周次过滤用）
        _entry(5, "周四", "91011", "甲班", "1组", "A410", "吴老师", "晚课"),
    ]


def _db(entries, skipped=None):
    d = ScheduleDatabase()
    d.entries = list(entries)
    d.pending = []
    d.skipped = list(skipped or [])
    d.loaded = True
    return d


@pytest.fixture
def env(monkeypatch):
    db = _db(_grid_entries(), skipped=[
        {"week": 2, "weekday": "周五", "raw_ts": "、‘", "class_name": "甲班",
         "experiment": "扭摆法", "room": "A504", "teacher": "翁铭华"},
        {"week": 9, "weekday": "周一", "raw_ts": "??", "class_name": "乙班",
         "experiment": "某实验", "room": "", "teacher": ""},
    ])
    monkeypatch.setattr(api, "_db", db)
    client = TestClient(app)
    return db, client


# ---------------------------------------------------------------- 1. 结构口径

def test_grid_is_seven_days_four_slots():
    """7 天 × 4 时段——R-41 在展示层的回归：周末和上午不能丢。"""
    g = schedule_grid.build_grid(_grid_entries(), week=2)
    assert g["weekdays"] == ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    assert [s["slot"] for s in g["slots"]] == ["MORNING", "567", "678", "91011"]
    assert g["slots"][0]["clock"] == "09:00-11:15"   # 上午时段真实存在（真实表 4 条）


def test_weekend_and_morning_entries_appear_in_cells():
    """周六/周日上午的条目必须出现在对应格（缺陷版：只渲染 5 天会丢它们）。"""
    g = schedule_grid.build_grid(_grid_entries(), week=2)
    sat = g["cells"].get("周六|567", [])
    assert len(sat) == 1 and sat[0]["experiment"] == "周末项目"
    sun = g["cells"].get("周日|MORNING", [])
    assert len(sun) == 1 and sun[0]["experiment"] == "周日绪论"
    assert sun[0]["missing_room"] is True            # 没填场地：可见 + 标注


def test_multi_slot_entry_occupies_every_blocked_slot():
    """跨节次记录（blocked 567+678）在两个格都出现，各带 blocked/time_raw（R-40）。"""
    g = schedule_grid.build_grid(_grid_entries(), week=3)
    for slot in ("567", "678"):
        cell = g["cells"].get(f"周三|{slot}", [])
        assert len(cell) == 1, f"周三|{slot} 应该有这条长实验"
        assert cell[0]["blocked"] == ["567", "678"]
        assert cell[0]["time_raw"] == "14:00-17:00"   # 原表写法要回显，不改写


def test_no_room_entries_stay_visible_with_marker():
    """无场地记录入格 + missing_room（R-35：不静默丢；真实表 16 条绪论课）。"""
    g = schedule_grid.build_grid(_grid_entries(), week=2)
    assert g["totals"]["missing_room"] == 1
    assert any("没填场地" in n for n in g["notes"])


def test_week_filter_excludes_other_weeks():
    g2 = schedule_grid.build_grid(_grid_entries(), week=2)
    g5 = schedule_grid.build_grid(_grid_entries(), week=5)
    assert g5["totals"]["entries"] == 1
    assert g5["cells"]["周四|91011"][0]["experiment"] == "晚课"
    assert "周六|567" not in g2 or all(  # 第5周没有周末条目
        e["experiment"] != "周末项目" for e in g5["cells"].get("周六|567", []))


# ---------------------------------------------------------------- 2. 冲突标记

def test_conflict_marks_come_from_scan_table():
    """三类核心冲突标到对应记录的格上；标记来源是 scan_table（唯一权威）。"""
    entries = _grid_entries()
    g = schedule_grid.build_grid(entries, week=2, conflicts=scan_table(entries))
    mon567 = {e["experiment"]: e["conflicts"] for e in g["cells"]["周一|567"]}
    assert mon567["项目A"] == ["room", "teacher"]    # 同室 + 同师
    assert mon567["项目B"] == ["room"]
    assert mon567["项目C"] == ["teacher"]
    tue = {e["experiment"]: e["conflicts"] for e in g["cells"]["周二|567"]}
    assert tue["项目G"] == ["group"] and tue["项目H"] == ["group"]
    assert g["conflict_counts"] == {"room": 1, "teacher": 1, "group": 1}
    assert g["totals"]["conflict_entries"] == 5      # 甲乙丙 + GH


def test_parallel_groups_same_cell_are_not_marked():
    """同班不同组同格（不同教师）是正常分批，不标冲突——R-56 口径。"""
    entries = _grid_entries()
    g = schedule_grid.build_grid(entries, week=2, conflicts=scan_table(entries))
    for e in g["cells"]["周一|678"]:
        assert e["conflicts"] == []


def test_without_conflicts_arg_nothing_is_marked():
    """不传 conflicts 就不标（端点默认总会传；这里锁"没有权威结论就不乱标"）。"""
    g = schedule_grid.build_grid(_grid_entries(), week=2)
    assert g["totals"]["conflict_entries"] == 0
    assert all(e["conflicts"] == [] for cell in g["cells"].values() for e in cell)


# ---------------------------------------------------------------- 3. 筛选与边界

def test_class_filter_narrows_cells_but_not_conflict_counts():
    """按班筛选只筛格子；冲突计数保持全周全表口径。"""
    entries = _grid_entries()
    g = schedule_grid.build_grid(entries, week=2, class_name="甲班",
                                 conflicts=scan_table(entries))
    shown = {e["experiment"] for cell in g["cells"].values() for e in cell}
    assert "项目B" not in shown and "项目A" in shown     # 乙班的被滤掉
    assert g["conflict_counts"] == {"room": 1, "teacher": 1, "group": 1}
    assert g["conflict_scope"].startswith("全周全表")


def test_room_filter():
    entries = _grid_entries()
    g = schedule_grid.build_grid(entries, week=2, room="A410")
    rooms = {e["room"] for cell in g["cells"].values() for e in cell}
    assert rooms == {"A410"}


def test_filter_options_come_from_whole_table():
    """筛选项从全表取：按某个班筛过之后，下拉里还剩全部班级。

    被当前筛选吃掉选项是经典陷阱——筛过一次就切不回去，教秘会以为只剩一个班。
    """
    entries = _grid_entries()
    g = schedule_grid.build_grid(entries, week=2, class_name="甲班")
    # 关键性质：被当前筛选滤掉的班仍在选项里（顺序是展示细节，不断言）
    assert set(g["class_options"]) == {"甲班", "乙班", "丙班"}
    assert "乙班" in g["class_options"] and "丙班" in g["class_options"]
    assert set(g["room_options"]) == {"A410", "A412", "A413", "A504", "A516"}


def test_out_of_range_week_raises_with_range():
    """周次越界报错并说清范围（R-32：不静默夹取）。"""
    with pytest.raises(ValueError, match="不在安排表覆盖范围"):
        schedule_grid.build_grid(_grid_entries(), week=99)
    with pytest.raises(ValueError, match="第 2~5 周"):
        schedule_grid.build_grid(_grid_entries(), week=0)


def test_skipped_rows_reported_per_week():
    """时段无法识别的行按周报数（不静默少显示）。"""
    g = schedule_grid.build_grid(_grid_entries(), week=2,
                                 skipped=[{"week": 2, "weekday": "周五", "raw_ts": "、‘"}])
    assert g["totals"]["unplaced"] == 1
    assert any("时段无法识别" in n for n in g["notes"])
    g9 = schedule_grid.build_grid(_grid_entries(), week=5,
                                  skipped=[{"week": 2, "weekday": "周五", "raw_ts": "x"}])
    assert g9["totals"]["unplaced"] == 0


def test_empty_entries_gives_empty_grid_not_crash():
    g = schedule_grid.build_grid([], week=1)
    assert g["cells"] == {} and g["totals"]["entries"] == 0


# ---------------------------------------------------------------- 4. API

def test_api_grid_returns_structure(env):
    _, client = env
    r = client.get("/api/reschedule/schedule-grid?week=2")
    assert r.status_code == 200
    d = r.json()["data"]
    assert d["week"] == 2 and d["week_min"] == 2 and d["week_max"] == 5
    assert len(d["weekdays"]) == 7 and len(d["slots"]) == 4
    assert d["totals"]["entries"] == 9               # 第2周共 9 条（周一5+周二2+周六1+周日1）
    assert "周一|567" in d["cells"]
    assert d["cells"]["周六|567"][0]["experiment"] == "周末项目"


def test_api_grid_is_free_auth(env):
    """读路径免鉴权（与 /data-audit /stats 同族：教秘主路径）。"""
    _, client = env
    assert client.get("/api/reschedule/schedule-grid?week=2").status_code == 200


def test_api_out_of_range_week_400(env):
    _, client = env
    r = client.get("/api/reschedule/schedule-grid?week=99")
    assert r.status_code == 400
    assert "第 2~5 周" in r.json()["detail"]          # 说清范围，不夹取


def test_api_class_filter(env):
    _, client = env
    r = client.get("/api/reschedule/schedule-grid?week=2&class_name=甲班")
    d = r.json()["data"]
    assert d["filters"]["class_name"] == "甲班"
    assert all(e["class_name"] == "甲班"
               for cell in d["cells"].values() for e in cell)
