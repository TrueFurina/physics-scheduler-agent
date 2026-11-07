"""主数据落盘恢复时的**诚实降级信号**（R92）。

R88 给 `/master/overview` 加了 `warning` 字段，把"磁盘上有、但没能用上"这件事
说给用户听。但恢复链路里有一处**误诊**：`_master_data.from_dict()` 在落盘文件
**结构损坏**时 fail-closed（返回 self、不抛），于是 `initialized` 仍是 False ——
和"真·空主数据"完全分不出来。结果损坏被误报成"空的"，用户看不到真实损坏原因。

本文件钉住这个区分：`from_dict` 用 `_from_dict_failed` 标志暴露 fail-closed，
恢复路径据此报"结构损坏"而非"空主数据"。
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import app.api.reschedule as api
import app.master_store as ms
from app.master_data import MasterData

REAL = os.path.join(os.path.dirname(__file__), "..", "..", "schedule.demo.xlsx")


@pytest.fixture()
def env(tmp_path, monkeypatch):
    if not os.path.isfile(REAL):
        pytest.skip("缺少 schedule.demo.xlsx")
    from app.utils.excel_parser import ScheduleDatabase
    db = ScheduleDatabase()
    db.load_from_excel(REAL)

    store = tmp_path / "store.json"
    monkeypatch.setattr(ms, "STORE_PATH", str(store))
    monkeypatch.setattr(api, "_db", db)
    monkeypatch.setattr(api, "_schedule_fp", "fp-test")
    monkeypatch.setattr(api, "_master_tasks", [])
    api._master_data.__init__()
    return db, store


# --------------------------------------------------------- from_dict 标志

def test_from_dict_marks_failed_flag_on_corrupt():
    """结构损坏 → fail-closed 且 `_from_dict_failed` 置位（供恢复路径区分）。"""
    md = MasterData()
    md.from_dict({"experiments": "not-a-dict", "classes": [{"bad": 1}]})
    assert md._from_dict_failed is True
    # fail-closed：标志置位的同时不能改任何表、不能误标 initialized
    assert md.initialized is False


def test_from_dict_resets_failed_flag_on_success():
    """一次成功后标志复位，否则会污染后续恢复判定。"""
    md = MasterData()
    md.from_dict({"experiments": "not-a-dict"})  # 先失败一次，置位
    assert md._from_dict_failed is True
    good = MasterData()
    good.upsert_experiment("项目A", course="课A", classes=["班A"])
    md.from_dict(good.to_dict())  # 再成功一次，应复位
    assert md._from_dict_failed is False
    assert md.initialized is True


# ------------------------------------------- 恢复路径的诚实降级信号

def test_corrupt_store_warning_says_corrupt_not_empty(env):
    """类型损坏的落盘 → 警告说'损坏'，绝不说成'空'。"""
    _db, store = env
    store.write_text(json.dumps({
        "schema_version": ms.SCHEMA_VERSION,
        "schedule_fingerprint": "fp-test",
        "master": {"experiments": {"X": {"course": 1}}, "classes": "坏数据"},
        "tasks": [],
    }, ensure_ascii=False), encoding="utf-8")

    api._master_data.__init__()
    api._ensure_master_data()

    warn = api._master_restore_warning
    assert "损坏" in warn, f"损坏恢复应报'损坏'，却报：{warn}"
    assert "空主数据" not in warn, f"损坏被误报成'空'：{warn}"
    # 行为不变：仍回退到安排表反推，主数据可用（不是空表）
    assert api._master_data.initialized is True
    assert len(api._master_data.experiments) > 0


def test_empty_store_warning_says_empty_not_corrupt(env):
    """空主数据（历史脏落盘）→ 警告说'空主数据'，绝不说成'损坏'。"""
    _db, store = env
    store.write_text(json.dumps({
        "schema_version": ms.SCHEMA_VERSION,
        "schedule_fingerprint": "fp-test",
        "master": {},
        "tasks": [],
    }, ensure_ascii=False), encoding="utf-8")

    api._master_data.__init__()
    api._ensure_master_data()

    warn = api._master_restore_warning
    assert "落盘里是空的主数据" in warn, f"空落盘应报'空主数据'，却报：{warn}"
    assert "结构损坏" not in warn, f"空落盘被误报成'损坏'：{warn}"


def test_good_store_has_no_warning(env):
    """好落盘 → 无降级警告，且确实从落盘恢复。"""
    _db, _store = env
    md = MasterData()
    md.upsert_experiment("测试项目", course="测试课", classes=["某班"])
    ms.save({"schedule_fingerprint": "fp-test", "master": md.to_dict(), "tasks": []})

    api._master_data.__init__()
    api._ensure_master_data()

    assert api._master_restore_warning == "", \
        f"好落盘不该有降级警告：{api._master_restore_warning}"
    assert "测试项目" in api._master_data.experiments, "没从落盘恢复"


# ------------------------------------------- 版本过旧落盘：静默作废 → 如实告知

def test_version_mismatch_store_surfaces_warning(env):
    """旧版本落盘（schema_version 不符）→ 警告说'旧版本/作废'，绝不说成'空'或'损坏'。"""
    _db, store = env
    store.write_text(json.dumps({
        "schema_version": ms.SCHEMA_VERSION - 1,  # 故意旧版本
        "schedule_fingerprint": "fp-test",
        "master": {"experiments": {"X": {"course": "课X"}}, "classes": {}},
        "tasks": [],
    }, ensure_ascii=False), encoding="utf-8")

    api._master_data.__init__()
    api._ensure_master_data()

    warn = api._master_restore_warning
    assert "旧版本" in warn and "作废" in warn, f"旧版本落盘应报'旧版本/作废'，却报：{warn}"
    assert "空主数据" not in warn, f"旧版本被误报成'空'：{warn}"
    assert "损坏" not in warn, f"旧版本被误报成'损坏'：{warn}"
    # 行为不变：仍回退到安排表反推，主数据可用（不是空表）
    assert api._master_data.initialized is True
    assert len(api._master_data.experiments) > 0


def test_missing_store_is_not_version_mismatch(env):
    """文件根本不存在 → 不算版本不匹配（两者处置不同，必须分得开）。"""
    _db, store = env  # store 未写入，等价于不存在
    assert ms.last_load_was_version_mismatch(str(store)) is False
    api._master_data.__init__()
    api._ensure_master_data()
    assert api._master_restore_warning == "", \
        f"无落盘不该有降级警告：{api._master_restore_warning}"


def test_version_mismatch_flag_reset_after_consume(env):
    """标记被 consume 后复位，不会泄漏到后续一次正常恢复。"""
    _db, store = env
    store.write_text(json.dumps({
        "schema_version": ms.SCHEMA_VERSION - 1,
        "schedule_fingerprint": "fp-test",
        "master": {"experiments": {"X": {"course": "课X"}}, "classes": {}},
        "tasks": [],
    }, ensure_ascii=False), encoding="utf-8")
    api._master_data.__init__()
    api._ensure_master_data()
    assert ms.last_load_was_version_mismatch(str(store)) is False, "consume 后标记应复位"

