"""导出字节缓存：命中要真的省事，失效要真的失效（R-71）

为什么单独锁这一条
------------------
导出实测 77–350ms（首调 830ms），教秘一次演示会把班级课表 / 场地课表 / 两份台账
各点一遍，还会重复点同一个 —— 重算没有任何新信息。加缓存后实测命中 0.4–2ms。

但缓存是本项目最怕的一类故障：R-66 就是"缓存键没覆盖全部输入"导致解析缓存
静默失效（代码改了、读到的还是旧结构）。所以这里的判据不是"有缓存"，而是：

1. 同一份数据第二次调用**不许重建**（否则缓存等于没加）；
2. 安排表任何一条改动后**必须重建**，且内容要跟着变（否则就是脏缓存）；
3. 主数据（课程归属）改动后，班级课表**必须重建**（课程列会变）；
4. 缓存条目数有上限（不能无界增长）。

判据用的是"重建次数"而不是耗时：耗时在 CI 上不稳，次数是确定的。
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import app.api.reschedule as api
from app.master_data import MasterData
from app.utils.excel_parser import ScheduleDatabase, ScheduleEntry


def _entry(week, weekday, slot, cls, group, exp, room, teacher="吴琳"):
    return ScheduleEntry(week, weekday, slot, cls, group, exp, room, teacher)


@pytest.fixture(autouse=True)
def _fresh_cache():
    api._EXPORT_CACHE.clear()
    yield
    api._EXPORT_CACHE.clear()


def test_export_cache_hits_and_invalidates(monkeypatch):
    db = ScheduleDatabase()
    db.entries = [
        _entry(1, "周二", "567", "一班", "1组", "伏安法", "A504"),
        _entry(2, "周三", "678", "二班", "1组", "声速测量", "A505"),
    ]
    db.loaded = True
    monkeypatch.setattr(api, "_db", db)
    monkeypatch.setattr(api, "_master_data", MasterData())
    monkeypatch.setattr(api, "_schedule_fp", None)

    builds = []
    real_build = api._class_schedule_build

    def counting_build(*args, **kwargs):
        builds.append(1)
        return real_build(*args, **kwargs)

    monkeypatch.setattr(api, "_class_schedule_build", counting_build)

    # 1) 第一次建、第二次命中
    first = api._class_schedule_bytes()
    second = api._class_schedule_bytes()
    assert first == second
    assert len(builds) == 1, "同一份数据第二次仍重建 —— 缓存没生效"

    # 2) 安排表改一个字段 → 必须重建，且内容要变
    db.entries[0].room = "B999"
    third = api._class_schedule_bytes()
    assert len(builds) == 2, "安排表变了还命中缓存 —— 这是脏缓存（R-66 同族）"
    assert third != first, "安排表变了但导出的字节一模一样 —— 数据没进指纹"

    # 3) 主数据改课程归属 → 班级课表的「课程」列要变，必须重建
    api._master_data.upsert_experiment("伏安法", course="另一门课")
    fourth = api._class_schedule_bytes()
    assert len(builds) == 3, "主数据变了还命中缓存 —— 课程列会停在旧值"
    assert fourth != third, "课程归属变了但字节没变 —— 主数据没进指纹"

    # 4) 其他导出同样走缓存，且各有各的键（不能串味）
    room1 = api._room_schedule_bytes()
    room2 = api._room_schedule_bytes()
    assert room1 == room2
    assert api._class_schedule_bytes() == fourth, "场地导出污染了班级导出的缓存"

    # 5) 上限：条目数不超过 _EXPORT_CACHE_MAX
    assert len(api._EXPORT_CACHE) <= api._EXPORT_CACHE_MAX
