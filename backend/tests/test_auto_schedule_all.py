"""全校一键排课（R-59）：全表覆盖 + 分片不互相撞车 + 初稿可导出

缺口：`/auto-schedule` 长时间 `max_sections=30`，真实表 171 个教学任务里
**141 个被静默略过**（文案写了"取前 30"，但教秘要的是全校排完）。
单次 171 个任务实测 6 秒时限内连可行解都拿不到（UNKNOWN），所以改分片，
片与片之间必须回填已排定的 教室/教师/班级 占用 —— 这是本批最容易写错的地方：
少回填一项，两片就会把同一个格子排给两个人，而单看每片都"成功"。
"""

import asyncio
import io
import os
import sys
from collections import Counter

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.engine.auto_scheduler import AutoScheduler
from app.master_data import MasterData
from app.utils.excel_parser import ScheduleDatabase, ScheduleEntry

import app.api.reschedule as api

REAL_XLSX = os.path.join(os.path.dirname(__file__), "..", "..", "schedule.xlsx")


def _db(entries):
    d = ScheduleDatabase()
    d.entries = list(entries)
    d.skipped = []
    d.loaded = True
    return d


def _entry(week, weekday, slot, cls, room, teacher, experiment):
    return ScheduleEntry(week, weekday, slot, cls, "1组", experiment, room, teacher)


def _synthetic_db(n_teachers=4, n_classes=5, weeks=6):
    """无重复 (教师, 项目, 班级) 的合成表：任务数 = 教师 × 班级。"""
    entries = []
    for w in range(1, weeks + 1):
        for ti in range(n_teachers):
            for ci in range(n_classes):
                entries.append(_entry(
                    w, "周一", "567", f"班{ci}", f"A{400 + ci}",
                    f"师{ti}", f"项目{ti}"))
    return _db(entries)


# ---------------------------------------------------------------- 全表覆盖

def test_solve_all_covers_every_section():
    """合成 20 个任务 → 分 4 片（每片 5 个）全部排定，不是前 N 个。"""
    db = _synthetic_db()  # 4 教师 × 5 班 = 20 个 (教师, 项目, 班级)
    r = AutoScheduler(db).solve_all(chunk_size=5)

    assert r["success"] is True
    assert r["total_sections"] == 20
    assert r["solved"] == 20, f"只排了 {r['solved']}/20"
    assert len(r["chunks"]) == 4
    assert all(c["solved"] == c["size"] for c in r["chunks"]), r["chunks"]
    assert not r["conflicts"]


def test_chunks_do_not_double_book_across_boundaries():
    """分片最容易犯的错：第 2 片看不见第 1 片排过的格子，于是排到同一格。

    判据按 场地 / 教师 / 班级 各算一遍 —— 三个维度都必须零重复。
    """
    db = _synthetic_db()
    r = AutoScheduler(db).solve_all(chunk_size=3)

    rooms = Counter((s["week"], s["weekday"], s["time_slot"], s["room"]) for s in r["schedule"])
    teachers = Counter((s["week"], s["weekday"], s["time_slot"], s["teacher"]) for s in r["schedule"])
    classes = Counter((s["week"], s["weekday"], s["time_slot"], s["class_name"]) for s in r["schedule"])
    assert max(rooms.values()) == 1, "同一格同一场地被排了两次（片间未回填 room_busy）"
    assert max(teachers.values()) == 1, "同一格同一教师被排了两次（片间未回填 teacher_busy）"
    assert max(classes.values()) == 1, "同一格同一班级被排了两次（片间未回填 class_busy）"


def test_empty_db_reports_no_tasks():
    r = AutoScheduler(_db([])).solve_all(chunk_size=10)
    assert r["success"] is False and "没有可排课" in r["error"]


# ---------------------------------------------------------------- API 覆盖口径

@pytest.fixture
def api_db(monkeypatch):
    db = _synthetic_db()
    monkeypatch.setattr(api, "_db", db)
    monkeypatch.setattr(api, "_student", api.StudentSchedule())
    monkeypatch.setattr(api, "_master_tasks", [])
    md = MasterData()
    monkeypatch.setattr(api, "_master_data", md)
    return db


def test_api_default_is_full_coverage(api_db):
    out = asyncio.run(api.auto_schedule(None))
    assert out["success"] is True
    d = out["data"]
    assert d["truncated"] is False
    assert d["coverage"] == f"{d['solved']}/{d['total_sections_all']}"
    assert d["solved"] == d["total_sections_all"] == 20
    assert len(d["schedule"]) == 20


def test_api_max_sections_still_truncates(api_db):
    """调试/演示仍可只要前 N 个，但必须显式标 truncated。"""
    out = asyncio.run(api.auto_schedule(api.AutoScheduleRequest(max_sections=3)))
    d = out["data"]
    assert d["truncated"] is True
    assert d["total_sections"] == 3
    assert d["total_sections_all"] == 20


# ---------------------------------------------------------------- 初稿导出

def _export_rows():
    return [
        {"week": 2, "weekday": "周一", "time_slot": "567", "room": "A410",
         "teacher": "师0", "course": "项目0", "class_name": "班0"},
        {"week": 2, "weekday": "周一", "time_slot": "678", "room": "A410",
         "teacher": "师0", "course": "项目0", "class_name": "班1"},
        {"week": 3, "weekday": "周二", "time_slot": "91011", "room": "A411",
         "teacher": "师1", "course": "项目1", "class_name": "班0"},
    ]


def _drain(resp):
    async def _collect():
        chunks = []
        async for chunk in resp.body_iterator:
            chunks.append(chunk)
        return b"".join(chunks)
    return asyncio.run(_collect())


def test_export_workbook_has_master_sheet_and_one_sheet_per_class():
    import openpyxl

    resp = asyncio.run(api.auto_schedule_export(api.AutoScheduleExportRequest(schedule=_export_rows())))
    wb = openpyxl.load_workbook(io.BytesIO(_drain(resp)))
    assert wb.sheetnames == ["总表", "班0", "班1"]
    master = [[c for c in row] for row in wb["总表"].iter_rows(values_only=True)]
    assert len(master) == 1 + 3  # 表头 + 3 条
    assert master[0] == ["周次", "星期", "时段", "场地", "教师", "实验项目", "班级"]
    assert len(list(wb["班0"].iter_rows(values_only=True))) == 1 + 2


def test_export_rejects_empty_schedule():
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as ei:
        asyncio.run(api.auto_schedule_export(api.AutoScheduleExportRequest(schedule=[])))
    assert ei.value.status_code == 400


def test_export_sanitizes_illegal_sheet_names():
    """班级名带 Excel 非法字符（真实表就有「2024级应用化学1、2班」这类）不能崩。"""
    import openpyxl

    rows = [dict(_export_rows()[0], class_name="24级/实验[1]班")]
    resp = asyncio.run(api.auto_schedule_export(api.AutoScheduleExportRequest(schedule=rows)))
    wb = openpyxl.load_workbook(io.BytesIO(_drain(resp)))
    assert "总表" in wb.sheetnames and len(wb.sheetnames) == 2
    assert "/" not in wb.sheetnames[1] and "[" not in wb.sheetnames[1]


# ---------------------------------------------------------------- 真实表规模

@pytest.mark.skipif(not os.path.exists(REAL_XLSX), reason="真实排课表不在仓库里")
def test_real_table_full_coverage():
    """真实表 171 个任务全表分片：全部排定、零跨片撞车（约 25 秒）。"""
    db = ScheduleDatabase()
    db.load_from_excel(os.path.abspath(REAL_XLSX))
    r = AutoScheduler(db).solve_all(chunk_size=60)

    assert r["success"] is True
    assert r["solved"] == r["total_sections"] >= 150
    rooms = Counter((s["week"], s["weekday"], s["time_slot"], s["room"]) for s in r["schedule"])
    teachers = Counter((s["week"], s["weekday"], s["time_slot"], s["teacher"]) for s in r["schedule"])
    classes = Counter((s["week"], s["weekday"], s["time_slot"], s["class_name"]) for s in r["schedule"])
    assert max(rooms.values()) == 1 and max(teachers.values()) == 1 and max(classes.values()) == 1
