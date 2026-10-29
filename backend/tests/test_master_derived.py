"""主数据新增实验项目 → 自动派生待排任务的闭环（R41）。

这是导师一号行动项的核心：
教秘在台账里新增一个实验项目、指定适用班级，**不必伪造旧课表行**，
系统就应把它变成可进求解器的待排任务。

粒度是 **组**，不是 **班级**
------------------------
`_master_derived_tasks` 的注释写着判据按 `(班级, 项目)` 检查「是否已排」，
但派生时**按组展开**：

    某班有 4 个组 → 新增项目派生出 4 条任务（每组 1 条）

我第一眼以为这是重复（4 条看起来一模一样：同班、同项目、教师场地都空），
实际 `group` 字段各不相同：

    ['24人工1_1', '24人工1_2', '24人工2_1', '24人工2_2']

这是对的 —— 实验课本来就是一个组分时上，每组要占一次课时。若按班级只派生 1 条，
一个班的 4 个组只会被排一次，另外 3 组没课上。

本文件锁住这条语义，防止将来"优化"成按班级去重。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.utils.excel_parser import ScheduleDatabase

REAL = os.path.join(os.path.dirname(__file__), "..", "..", "schedule.demo.xlsx")


@pytest.fixture()
def api_env(tmp_path, monkeypatch):
    import app.api.reschedule as api
    import app.master_store as ms

    if not os.path.isfile(REAL):
        pytest.skip("缺少 schedule.demo.xlsx")
    db = ScheduleDatabase()
    db.load_from_excel(REAL)

    monkeypatch.setattr(api, "_DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setattr(ms, "STORE_PATH", str(tmp_path / "s.json"))
    monkeypatch.setattr(api, "_db", db)
    monkeypatch.setattr(api, "_schedule_fp", "fp-test")
    api._master_data.__init__()
    api._master_tasks.clear()
    api._ensure_master_data()
    return api


def test_new_experiment_derives_one_task_per_group(api_env):
    """新增项目 → 每个组一条待排任务（不是每班一条）。"""
    api = api_env
    cls = sorted(api._master_data.classes)[0]
    groups = sorted(api._master_data.groups_of(cls))
    assert len(groups) > 1, "测试前提：该班有多个组"

    assert api._master_derived_tasks() == [], "前提：基线没有派生任务"

    api._master_data.upsert_experiment("新实验X", course="大学物理实验", classes=[cls])
    tasks = [t for t in api._master_derived_tasks() if t.experiment == "新实验X"]

    assert len(tasks) == len(groups), (
        f"{cls} 有 {len(groups)} 个组，但只派生了 {len(tasks)} 条任务 —— "
        "按班级去重会让其余组没有课时"
    )
    assert {t.group for t in tasks} == set(groups), "派生的组集合与该班不符"
    assert {t.class_name for t in tasks} == {cls}


def test_derived_task_carries_course(api_env):
    """派生任务要带上课程归属（否则导出/归因会落回默认课）。"""
    api = api_env
    cls = sorted(api._master_data.classes)[0]
    api._master_data.upsert_experiment("新实验Y", course="大学物理实验", classes=[cls])
    tasks = [t for t in api._master_derived_tasks() if t.experiment == "新实验Y"]
    assert tasks
    assert all(t.course == "大学物理实验" for t in tasks), "派生任务丢了课程归属"


def test_already_scheduled_pair_is_not_derived(api_env):
    """安排表里已排过的 (班级, 项目) 不再派生 —— 否则同一门课会被排两次。"""
    api = api_env
    from collections import Counter
    pair = Counter((e.class_name, e.experiment) for e in api._db.entries
                   if e.class_name and e.experiment).most_common(1)[0][0]
    cls, exp = pair

    api._master_data.upsert_experiment(exp, classes=[cls])
    derived = [t for t in api._master_derived_tasks()
               if t.class_name == cls and t.experiment == exp]
    assert derived == [], (
        f"{cls} 的「{exp}」已排过，却又派生了 {len(derived)} 条待排任务"
    )


def test_intro_and_exam_projects_never_derived(api_env):
    """绪论课/考试类绝不派生（R-48 口径）：它们不该进求解器。"""
    api = api_env
    cls = sorted(api._master_data.classes)[0]
    for name in ("绪论课（教室已安排，请查系统）", "大物实验B 考试"):
        api._master_data.upsert_experiment(name, classes=[cls])
        derived = [t for t in api._master_derived_tasks() if t.experiment == name]
        assert derived == [], f"「{name}」被派生成待排任务，求解器会去排它"
