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

def test_old_version_store_invalidated(store_path):
    """旧版本落盘必须作废（R-58）：落盘是**整体恢复**语义，不设版本门的话，
    旧基线快照（如 22 个项目）会一直遮蔽新反推基线，基线逻辑变更等于没生效。
    """
    from app.master_data import MasterData

    md = MasterData()
    md.build_from_entries([_entry(1, "周四", "567", "一班", "1组", "A410", experiment="扭摆法")])
    assert master_store.save({"schedule_fingerprint": "fp::1", "master": md.to_dict(), "tasks": []})
    assert master_store.load() is not None  # 当前版本可读

    # 模拟旧版本文件：抹掉 schema_version
    import json
    with open(store_path, encoding="utf-8") as f:
        d = json.load(f)
    d.pop("schema_version", None)
    with open(store_path, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False)

    assert master_store.load() is None  # 旧版本 → 作废，调用方回退新反推基线


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


def test_from_dict_corrupt_leaves_every_table_untouched():
    """坏数据时**五张表都不许被改** —— 这才是 fail-closed 的严格含义。

    2026-10-29 实测的缺陷：旧实现边解析边赋值，喂
    `{"courses": {...新的...}, "experiments": "not-a-dict"}` 之后得到
        experiments=['旧项目'] / courses=['新课'] / teachers=['旧师']
    而 `initialized` 仍是 True —— **三张表互相矛盾，还被当成已初始化继续用**。

    上一版 `test_from_dict_corrupt_falls_back_closed` 的 docstring 就写着"不产半态"，
    但它只断言了 `initialized is False`，完全没查其余四张表。
    **断言不足让这个缺陷活了下来** —— 这类"测了一个指标就以为测了整个承诺"
    是最常见的假安全感。
    """
    md = MasterData()
    md.experiments = {"旧项目": {"course": "旧课", "teachers": set(),
                                 "rooms": set(), "classes": set()}}
    md.courses = {"旧课": {"experiments": {"旧项目"}}}
    md.teachers = {"旧师": {"projects": {"旧项目"}}}
    md.rooms = {"旧房": {"capacity": 40}}
    md.classes = {"旧班": {"groups": {"1组"}, "weekday": "周一", "weekdays": {"周一"},
                           "time_slots": {"567"}, "week_min": 2, "week_max": 10}}
    md.initialized = True
    before = md.to_dict()

    # courses 是合法的（会被先解析），experiments 坏掉 ⇒ 旧实现会留下半态
    md.from_dict({"courses": {"新课": {"experiments": []}}, "experiments": "not-a-dict"})

    after = md.to_dict()
    assert after == before, (
        "坏数据之后主数据被改了（出现半态）：\n"
        f"  before: {before}\n  after : {after}"
    )
    assert md.initialized is True, "对象原本是已初始化，坏数据不该把它改掉"


def test_from_dict_success_replaces_all_tables():
    """正向控制：数据合法时五张表都要被替换（别把守卫做成"永远不动"）。"""
    md = MasterData()
    md.experiments = {"旧项目": {"course": "旧课", "teachers": set(),
                                 "rooms": set(), "classes": set()}}
    md._rebuild_courses()
    md.initialized = True

    md.from_dict({
        "experiments": {"新项目": {"course": "新课", "teachers": ["新师"],
                                   "rooms": ["新房"], "classes": ["新班"]}},
        "teachers": {"新师": {"projects": ["新项目"]}},
        "rooms": {"新房": {"capacity": 60}},
        "classes": {"新班": {"groups": ["1组"], "weekday": "周二",
                             "weekdays": ["周二"], "time_slots": ["678"],
                             "week_min": 3, "week_max": 12}},
    })

    assert set(md.experiments) == {"新项目"}
    assert set(md.teachers) == {"新师"}
    assert set(md.rooms) == {"新房"}
    assert set(md.classes) == {"新班"}
    assert set(md.courses) == {"新课"}, "课程聚合没跟着重建"
    assert md.initialized is True


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


# ---------------------------------------------------------------- 脏落盘 fail-safe
#
# 实测事故（2026-10-24）：上一版 `_persist_master_state` 在"主数据未初始化、
# 但有待排清单"时也会落盘，写出的是一份**空主数据**；下次启动把它当权威恢复，
# 25 个实验项目 / 17 个班级被整体清空（日志：`恢复（实验项目 0 / 待排任务 3）`，
# 随后全校一键 0/0 班）。下面两条把 fail-safe 锁死。

def test_empty_master_dict_is_not_initialized():
    md = MasterData().from_dict({})
    assert md.initialized is False
    md2 = MasterData().from_dict({"experiments": {}, "classes": {}, "teachers": {},
                                  "rooms": {}, "courses": {}})
    assert md2.initialized is False


def test_persist_with_uninitialized_master_does_not_write_empty_master(store_path, tmp_path, monkeypatch):
    db = ScheduleDatabase()
    db.entries = [_entry(1, "周四", "567", "一班", "1组", "A410")]
    db.loaded = True
    db._filepath = str(tmp_path / "schedule.xlsx")
    open(db._filepath, "wb").close()
    monkeypatch.setattr(api, "_db", db)
    monkeypatch.setattr(api, "_master_data", MasterData())   # 未初始化
    monkeypatch.setattr(api, "_master_tasks", [PendingTask("一班", "扭摆法", "1组")])
    monkeypatch.setattr(api, "_schedule_fp", master_store.schedule_fingerprint(db._filepath))

    api._persist_master_state()
    store = master_store.load()
    assert store is not None
    assert store["master"] is None, "未初始化时不得写空主数据（否则重启会清空主数据）"
    assert len(store["tasks"]) == 1


def test_stale_empty_master_store_is_discarded_and_rebuilt(store_path, tmp_path, monkeypatch):
    """历史脏落盘：master 是空表但指纹一致 → 必须丢弃并按安排表反推。"""
    db = ScheduleDatabase()
    db.entries = [
        _entry(1, "周四", "567", "一班", "1组", "A410"),
        _entry(2, "周四", "678", "二班", "1组", "A411", experiment="声速测量"),
    ]
    db.loaded = True
    db._filepath = str(tmp_path / "schedule.xlsx")
    open(db._filepath, "wb").close()
    fp = master_store.schedule_fingerprint(db._filepath)

    master_store.save({"schedule_fingerprint": fp,
                       "master": {"courses": {}, "experiments": {}, "teachers": {},
                                  "rooms": {}, "classes": {}},
                       "tasks": []})

    monkeypatch.setattr(api, "_db", db)
    monkeypatch.setattr(api, "_master_data", MasterData())
    monkeypatch.setattr(api, "_master_tasks", [])
    monkeypatch.setattr(api, "_schedule_fp", fp)

    api._ensure_master_data()
    assert api._master_data.initialized is True
    assert "扭摆法" in api._master_data.experiments
    assert set(api._master_data.classes) == {"一班", "二班"}
    assert master_store.load() is None, "脏落盘应被清掉"


# ---------------------------------------------------------------- build 必须原子（R-91）
#
# 2026-10-29 实测：`build_from_entries` 原来是"五次 clear + 逐行 setdefault"，
# 期间并发读者看到"主数据被清空、只填了一部分"。真实表 433 行、20 轮重建、
# 3 个读者线程下**462192 次**读到实验项目数不是 25（集中在 9 和 13）。
#
# 调用方：`/master/init`（一键初始化）、`_ensure_master_data_locked`（冷启动反推基线）；
# 并发读者：`/master/overview`、`/parallel/*`、`/export/*` —— 它们会拿到残缺主数据，
# 算出的班级覆盖/课程归属都是错的，且不报错。

def test_build_from_entries_builds_on_a_temp_instance():
    """结构锁：`build_from_entries` 必须把实际逻辑委托给**临时实例**上的填充函数。

    为什么不用"函数体里不许有 .clear()"这种判据（第一版就是那样，结果漏判）：
    把逻辑抽到 `_build_from_entries_into` 之后，`build_from_entries` 里本来就没有
    `clear()` 了 —— 于是"退回就地改写"（`self._build_from_entries_into(...)`）
    也能通过。**判据必须盯"是否用临时实例"，而不是"有没有某个调用"。**

    正确形态：函数体里出现 `MasterData()`（新建实例）且对**它**调用填充函数。
    """
    import ast
    import inspect
    import textwrap

    src = textwrap.dedent(inspect.getsource(MasterData.build_from_entries))
    tree = ast.parse(src)

    makes_temp = any(
        isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "MasterData"
        for n in ast.walk(tree)
    )
    assert makes_temp, (
        "build_from_entries 没有新建临时 MasterData —— 就地改写会让并发读者"
        "看到「主数据被清空、只填了一半」（实测 462192 次命中）"
    )

    fills_on_temp = any(
        isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        and n.func.attr.startswith("_build_from_entries")
        and isinstance(n.func.value, ast.Name) and n.func.value.id != "self"
        for n in ast.walk(tree)
    )
    assert fills_on_temp, (
        "填充逻辑没有作用在临时实例上（应形如 `stage._build_from_entries_into(...)`）"
    )


def test_build_from_entries_replaces_all_tables():
    """行为锁：重建后五张表都应反映新安排表，且课程聚合一致。"""
    from app.utils.excel_parser import ScheduleEntry

    md = MasterData()
    md.experiments = {"旧项目": {"course": "旧课", "teachers": set(),
                                 "rooms": set(), "classes": set()}}
    md._rebuild_courses()
    md.initialized = True

    entries = [
        ScheduleEntry(1, "周一", "567", "一班", "1组", "声速测量", "A410", "吴琳"),
        ScheduleEntry(1, "周一", "678", "二班", "1组", "牛顿环测透镜曲率半径", "A411", "柯达"),
    ]
    md.build_from_entries(entries)

    assert set(md.experiments) == {"声速测量", "牛顿环测透镜曲率半径"}
    assert set(md.teachers) == {"吴琳", "柯达"}
    assert set(md.rooms) == {"A410", "A411"}
    assert set(md.classes) == {"一班", "二班"}
    assert "旧项目" not in md.experiments, "旧数据没被清掉"
    assert md.initialized is True


def test_build_from_entries_failure_leaves_object_untouched():
    """坏输入（entries 里有非预期对象）不许留下半态 —— 与 `from_dict` 同一条承诺。"""
    md = MasterData()
    md.experiments = {"旧项目": {"course": "旧课", "teachers": set(),
                                 "rooms": set(), "classes": set()}}
    md._rebuild_courses()
    md.initialized = True
    before = md.to_dict()

    class Bad:
        @property
        def experiment(self):
            raise RuntimeError("坏对象")

    try:
        md.build_from_entries([Bad()])
    except RuntimeError:
        pass                        # 异常可以冒出来，但状态必须干净

    assert md.to_dict() == before, "失败后主数据被改了（半态）"
