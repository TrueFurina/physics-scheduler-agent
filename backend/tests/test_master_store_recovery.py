"""主数据落盘的"坏了怎么办"：fail-closed 与丢弃脏文件（R50）。

链路
----
`_ensure_master_data()` 的恢复顺序：

    1. 读落盘 master_store.json
    2. 指纹一致 → `_master_data.from_dict(...)` 恢复（含教秘的 CRUD）
    3. `from_dict` 抛异常 / 恢复后未初始化 → **回退到安排表反推基线**，
       并 `master_store.clear()` 丢弃这份落盘
    4. 指纹不一致（换表）→ 同样丢弃，按新表反推

第 3 步的 `clear()` 是**不可逆操作**（删文件），所以必须满足两个条件才安全：

| 条件 | 为什么 |
|---|---|
| `from_dict` 是 fail-closed | 坏数据时不留半态，否则会带着矛盾状态继续跑 |
| 判定的是"坏"而不是"暂时读不到" | 只是权限/占用导致的读失败，删了就是丢数据 |

### 已核实的事实

`from_dict` 的实现（2026-10-29 修）正是 fail-closed：先 build 到临时对象，
任何一步失败都不污染 `self`。实测：

    只把 `classes` 弄坏（其余完好）→ initialized=False，
    experiments/teachers/rooms 全部保持 0 —— 没有半态

所以"坏就丢"是安全的：那份落盘里没有任何可救的信息
（它是从同一份安排表反推出来的，重推一遍即可）。

本文件把这条判断链钉住 —— 它同时是"不丢数据"和"不留脏数据"的边界。
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import app.api.reschedule as api
import app.master_store as ms
from app.master_data import MasterData
from app.utils.excel_parser import ScheduleDatabase

REAL = os.path.join(os.path.dirname(__file__), "..", "..", "schedule.demo.xlsx")


@pytest.fixture()
def env(tmp_path, monkeypatch):
    if not os.path.isfile(REAL):
        pytest.skip("缺少 schedule.demo.xlsx")
    db = ScheduleDatabase()
    db.load_from_excel(REAL)

    store = tmp_path / "store.json"
    monkeypatch.setattr(ms, "STORE_PATH", str(store))
    monkeypatch.setattr(api, "_db", db)
    monkeypatch.setattr(api, "_schedule_fp", "fp-test")
    monkeypatch.setattr(api, "_master_tasks", [])
    api._master_data.__init__()
    return db, store


def _good_master():
    md = MasterData()
    md.upsert_experiment("测试项目", course="测试课", classes=["某班"])
    return md.to_dict()


# ---------------------------------------------------------------- fail-closed

def test_from_dict_never_leaves_partial_state():
    """坏数据时四张表一个字段都不改（只断言 initialized 是不够的）。"""
    md = MasterData()
    md.upsert_experiment("旧项目")
    before_exp = set(md.experiments)
    before_teach = set(md.teachers)

    md.from_dict({
        "courses": {"新课": {"experiments": ["旧项目"]}},
        "experiments": "not-a-dict",          # ← 这里炸
        "teachers": {"新师": {"projects": []}},
    })

    assert md.initialized is False
    assert set(md.experiments) == before_exp, "experiments 被写了一半"
    assert set(md.teachers) == before_teach, "teachers 被写了一半"


# ------------------------------------------------------- 坏落盘 → 回退基线

def test_corrupt_store_falls_back_to_schedule_baseline(env):
    """落盘解析失败 → 按安排表反推，且主数据可用（不是空表）。"""
    db, store = env
    store.write_text(json.dumps({
        "schema_version": ms.SCHEMA_VERSION,
        "schedule_fingerprint": "fp-test",
        "master": {"experiments": {"X": {"course": 1}}, "classes": "坏数据"},
        "tasks": [],
    }, ensure_ascii=False), encoding="utf-8")

    api._master_data.__init__()
    api._ensure_master_data()

    assert api._master_data.initialized is True, "回退后应已初始化"
    assert len(api._master_data.experiments) > 0, "回退后不该是空表 —— 那等于把台账清了"
    # 反推出来的项目应来自安排表
    assert len(api._master_data.experiments) == len(
        {e.experiment for e in db.entries if e.experiment})


def test_corrupt_store_file_is_discarded(env):
    """坏落盘必须被丢弃 —— 否则每次启动都重跑一遍失败恢复。"""
    db, store = env
    store.write_text("{ 这不是 JSON", encoding="utf-8")
    api._master_data.__init__()
    api._ensure_master_data()
    assert not store.exists(), "坏落盘没被清掉 —— 下次启动还会读到它"


def test_empty_master_in_store_is_not_treated_as_authority(env):
    """落盘里是空主数据（历史 bug 产物）→ 不能当权威恢复。

    否则一次重启就把 17 个班 / 25 个项目全清空（注释里记录过实测）。
    """
    db, store = env
    store.write_text(json.dumps({
        "schema_version": ms.SCHEMA_VERSION,
        "schedule_fingerprint": "fp-test",
        "master": {},                      # ← 空
        "tasks": [],
    }, ensure_ascii=False), encoding="utf-8")

    api._master_data.__init__()
    api._ensure_master_data()

    assert api._master_data.initialized is True
    assert len(api._master_data.classes) > 0, "空落盘被当权威 → 班级全清空"


# ---------------------------------------------------- 好落盘 → 原样恢复

def test_good_store_is_restored_not_discarded(env):
    """对照组：好的落盘必须被采用，不能被误删。"""
    db, store = env
    master = _good_master()
    ms.save({"schedule_fingerprint": "fp-test", "master": master, "tasks": []})

    api._master_data.__init__()
    api._ensure_master_data()

    assert store.exists(), "好落盘被误删了 —— 教秘的会话内修改会丢"
    assert "测试项目" in api._master_data.experiments, "没从落盘恢复"
