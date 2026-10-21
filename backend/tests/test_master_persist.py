"""主数据落盘持久化：会话内修改跨重启存活 + 换表自动失效

缺口（R-52 收尾时确认）：主数据/灰箱清单是内存态，重启（线上沙箱回收）后
教秘补录的教师/场地、上传的清单全部丢失，灰箱闭环有个洞。

设计：单 JSON 文件（backend/app/data/master_store.json，整目录 gitignore）+
安排表指纹失效 + 坏文件 fail-closed。落盘只保存"教秘在基线之上做的修改"，
安排表永远是权威来源。
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import master_store
from app.master_data import MasterData
from app.utils.excel_parser import PendingTask, ScheduleDatabase, ScheduleEntry

import app.api.reschedule as api


def _entry(week, weekday, slot, cls, group, room, teacher="吴老师", experiment="扭摆法"):
    return ScheduleEntry(week, weekday, slot, cls, group, experiment, room, teacher)


@pytest.fixture
def store_path(tmp_path, monkeypatch):
    """把落盘路径指到 tmp：save/load/clear 都在调用时读 STORE_PATH。"""
    p = str(tmp_path / "master_store.json")
    monkeypatch.setattr(master_store, "STORE_PATH", p)
    return p


# ---------------------------------------------------------------- 序列化

def test_master_dict_roundtrip_preserves_everything():
    md = MasterData()
    md.build_from_entries([
        _entry(1, "周四", "567", "一班", "1组", "A410", teacher="吴老师"),
        _entry(5, "周四", "678", "一班", "2组", "A411", teacher="林老师", experiment="声速测量"),
    ])
    md.upsert_experiment("新增项目", teachers=["鄢老师"], rooms=["A502"], classes=["灰箱班"])
    md.upsert_class("灰箱班", groups=["1组"], weekday="周五", time_slots=["567"],
                    week_min=3, week_max=12)

    md2 = MasterData().from_dict(md.to_dict())
    assert md2.initialized
    assert md2.experiments["新增项目"]["teachers"] == {"鄢老师"}
    assert md2.experiments["新增项目"]["classes"] == {"灰箱班", "一班"} or \
           md2.experiments["新增项目"]["classes"] == {"灰箱班"}
    c = md2.classes["灰箱班"]
    assert c["groups"] == {"1组"} and c["weekday"] == "周五"
    assert c["time_slots"] == {"567"} and c["week_min"] == 3 and c["week_max"] == 12
    assert md2.courses == md.courses or set(md2.courses) == set(md.courses)
    # 反向引用一致
    assert "新增项目" in md2.teachers["鄢老师"]["projects"]


def test_from_dict_corrupt_falls_back_closed(tmp_path):
    """坏文件 fail-closed：不崩、不产半态，initialized=False 让调用方回退反推。"""
    md = MasterData()
    md.from_dict({"experiments": "not-a-dict", "classes": [{"bad": 1}]})
    assert md.initialized is False


# ---------------------------------------------------------------- 落盘恢复

def test_persist_then_restore_survives_restart(store_path, tmp_path, monkeypatch):
    """CRUD + 灰箱清单 + 补全标记 → 模拟重启（清空模块态）→ 全部恢复。"""
    db = ScheduleDatabase()
    db.entries = [_entry(1, "周四", "567", "一班", "1组", "A410")]
    db.loaded = True
    db._filepath = str(tmp_path / "schedule.xlsx")
    open(db._filepath, "wb").close()
    monkeypatch.setattr(api, "_db", db)
    monkeypatch.setattr(api, "_master_data", MasterData())
    monkeypatch.setattr(api, "_master_tasks", [])
    monkeypatch.setattr(api, "_schedule_fp", master_store.schedule_fingerprint(db._filepath))

    api._master_data.build_from_entries(db.entries)
    api._master_data.upsert_experiment("光纤传感", teachers=["鄢老师"], classes=["一班"])
    api._master_tasks.append(PendingTask("一班", "光纤传感", "1组"))
    api._persist_master_state()

    # ── 模拟重启：模块态全部归零 ──
    monkeypatch.setattr(api, "_master_data", MasterData())
    monkeypatch.setattr(api, "_master_tasks", [])
    api._ensure_master_data()

    assert api._master_data.initialized
    assert "光纤传感" in api._master_data.experiments
    assert len(api._master_tasks) == 1
    assert api._master_tasks[0].experiment == "光纤传感"


def test_restore_discarded_when_schedule_replaced(store_path, tmp_path, monkeypatch):
    """安排表被替换（指纹变化）→ 旧落盘作废，按新表反推基线。"""
    old_xlsx = str(tmp_path / "old.xlsx")
    open(old_xlsx, "wb").close()

    db = ScheduleDatabase()
    db.entries = [_entry(1, "周四", "567", "一班", "1组", "A410")]
    db.loaded = True
    db._filepath = old_xlsx
    monkeypatch.setattr(api, "_db", db)
    monkeypatch.setattr(api, "_master_data", MasterData())
    monkeypatch.setattr(api, "_master_tasks", [])
    monkeypatch.setattr(api, "_schedule_fp", master_store.schedule_fingerprint(old_xlsx))

    api._master_data.build_from_entries(db.entries)
    api._master_data.upsert_experiment("旧表项目")
    api._persist_master_state()
    assert master_store.load() is not None

    # 换新表：指纹变了
    new_xlsx = str(tmp_path / "new.xlsx")
    open(new_xlsx, "wb").close()
    db2 = ScheduleDatabase()
    db2.entries = [_entry(2, "周五", "678", "新班", "1组", "B101", experiment="新表实验")]
    db2.loaded = True
    db2._filepath = new_xlsx
    monkeypatch.setattr(api, "_db", db2)
    monkeypatch.setattr(api, "_master_data", MasterData())
    monkeypatch.setattr(api, "_master_tasks", [])
    monkeypatch.setattr(api, "_schedule_fp", master_store.schedule_fingerprint(new_xlsx))

    api._ensure_master_data()
    assert "旧表项目" not in api._master_data.experiments
    assert "新表实验" in api._master_data.experiments  # 按新表反推
    assert master_store.load() is None  # 旧落盘已清


def test_tasks_roundtrip_keeps_enrichment_markers():
    """补全标记跨落盘存活 —— 假设报告（R-52 的核心）重启后不丢。"""
    t = PendingTask("灰箱班", "扭摆法", "1组")
    t.teacher = "吴老师"
    t.room = "A410"
    t._teacher_enriched = True
    t._room_enriched = True

    rows = master_store.tasks_to_rows([t])
    back = master_store.rows_to_tasks(rows)
    assert back[0].teacher == "吴老师" and back[0].room == "A410"
    assert getattr(back[0], "_teacher_enriched", False) is True
    assert getattr(back[0], "_room_enriched", False) is True
