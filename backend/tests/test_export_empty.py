"""导出空表契约：安排表为空时，导出必须返回「无数据」单页，而不是崩溃或 0 字节（R-104）。

为什么这条值得锁
----------------
教秘在**全新部署 / reset 之后**立刻点「导出班级课表 / 场地课表」，此时内存里没有任何安排
记录。若导出把空表当成「正常空结果」吐出一个只有默认页（甚至 0 字节）的 xlsx，教秘会以为
系统坏了、或从空白文件里误判「全表都扫到了」。正确行为：返回**一个写着
『当前安排表里没有可导出的记录』的「无数据」页**——一眼就能看出是「还没排」而非「排了空的」。

这条此前 0 覆盖：`_class_schedule_build` / `_room_schedule_build` 的 `if not grouped:` 分支
（`ws = wb.create_sheet("无数据")`）从未被执行过。
"""
import io
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import openpyxl
import app.api.reschedule as api
from app.master_data import MasterData
from app.utils.excel_parser import ScheduleDatabase


@pytest.fixture(autouse=True)
def _fresh_cache():
    api._EXPORT_CACHE.clear()
    yield
    api._EXPORT_CACHE.clear()


def _empty_db(monkeypatch):
    """把一个「已加载但 0 条记录」的库挂到模块全局，绕过 _ensure_loaded 的磁盘自愈。"""
    db = ScheduleDatabase()
    db.entries = []
    db.loaded = True
    monkeypatch.setattr(api, "_db", db)
    monkeypatch.setattr(api, "_master_data", MasterData())
    monkeypatch.setattr(api, "_schedule_fp", None)
    return db


def _sheet_titles_and_first_row(data: bytes):
    wb = openpyxl.load_workbook(io.BytesIO(data))
    out = {}
    for ws in wb.worksheets:
        rows = list(ws.iter_rows(values_only=True))
        out[ws.title] = rows[0][0] if rows else None
    return out


def test_export_class_schedule_empty_db(monkeypatch):
    """空表导出班级课表：必须有一页「无数据」，且写明没有记录。"""
    _empty_db(monkeypatch)
    data = api._class_schedule_bytes(None)
    sheets = _sheet_titles_and_first_row(data)
    assert "无数据" in sheets, f"空表导出竟没有「无数据」页：{list(sheets)}"
    assert sheets["无数据"] == "当前安排表里没有可导出的记录"


def test_export_room_schedule_empty_db(monkeypatch):
    """空表导出场地课表：场地维度同样必须有「无数据」页。"""
    _empty_db(monkeypatch)
    data = api._room_schedule_bytes(None)
    sheets = _sheet_titles_and_first_row(data)
    assert "无数据" in sheets, f"空表导出竟没有「无数据」页：{list(sheets)}"
    assert sheets["无数据"] == "当前安排表里没有可导出的记录"


def test_export_class_schedule_empty_db_is_one_sheet(monkeypatch):
    """空表导出必须恰好一个页（无数据页），不能多塞默认页迷惑教秘。"""
    _empty_db(monkeypatch)
    data = api._class_schedule_bytes(None)
    wb = openpyxl.load_workbook(io.BytesIO(data))
    assert len(wb.worksheets) == 1, f"空表导出页数异常：{[w.title for w in wb.worksheets]}"
