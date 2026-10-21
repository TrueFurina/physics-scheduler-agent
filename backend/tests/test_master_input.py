"""从零排课的输入口：待排任务 + 教学大纲清单

为什么这批用例存在
------------------
两个求解器的教学任务清单，此前**只可能**来自 `db.entries`（上一次排完的课表行）：
  - `parallel_scheduler.get_course_overview` / `solve`：`for e in self.db.entries`
  - `auto_scheduler._get_course_sections`：`for e in self.db.entries`
于是系统只能"重排去年"。而导师座谈明确否定了"往年数据瞬移"（培养方案/专业方向/
实验顺序年年变）—— 那三条理由同样否定了"以去年课表行当输入"的我们。

更糟的是：教秘拿今年教学大纲的清单来上传时，那些"有班级+实验项目、但没有周次"
的行会被 `excel_parser` 的两个 `continue` **静默丢弃**，界面只回一个 total_entries，
于是表现为"成功加载 0 条"，没人知道为什么。

这批用例锁三件事：
  1. 没排时间的行不再消失，而是成为**待排任务**并被报告出来；
  2. 待排任务真的进得进求解器（这才是"从零排课"的闭环）；
  3. 换学校/换工作表名的文件不再直接 400（"支持其他学校"的承诺要能兑现）。
"""

import asyncio
import io
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import app.api.reschedule as api
from app.engine.auto_scheduler import AutoScheduler
from app.engine.parallel_scheduler import ParallelScheduler
from app.utils.excel_parser import (
    PendingTask,
    ScheduleDatabase,
    ScheduleEntry,
    find_schedule_sheet,
    parse_master_tasks,
)


def _entry(week, weekday, slot, cls, group, room, teacher="林老师", experiment="扭摆法"):
    return ScheduleEntry(week, weekday, slot, cls, group, experiment, room, teacher)


def _db(entries, pending=None):
    d = ScheduleDatabase()
    d.entries = list(entries)
    d.pending = list(pending or [])
    d.loaded = True
    return d


# ---------------------------------------------------------------------------
# 1. 没排时间的行：不消失、被报告
# ---------------------------------------------------------------------------

def _write_rows(rows, sheet_name="实验安排表"):
    """写一份最小 xlsx：第 1 行标题、第 2 行表头、第 3 行起数据。"""
    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = sheet_name
    ws.append(["大学物理实验安排表"])                                   # 第 1 行
    ws.append(["周次", "星期", "时段", "班级", "学生组", "实验项目", "场地", "教师"])  # 第 2 行
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


def test_rows_without_time_become_pending_not_silently_dropped(tmp_path):
    """有班级+实验项目、但没排周次/星期的行 ⇒ 待排任务，且被计数。"""
    p = tmp_path / "plan.xlsx"
    p.write_bytes(_write_rows([
        [1, "周一", "567", "一班", "1组", "扭摆法", "A410", "林老师"],   # 已排
        [None, None, None, "一班", "2组", "光的偏振", "", "吴老师"],      # 大纲新增，没排时间
        [None, None, None, "二班", "1组", "声速测量", "", "陈老师"],      # 同上
    ]).getvalue())

    db = ScheduleDatabase()
    db.load_from_excel(str(p))
    assert len(db.entries) == 1, "已排的行要正常入库"
    assert len(db.pending) == 2, "没排时间的行被静默丢弃了"
    assert db.load_report["pending"] == 2
    assert db.load_report["entries"] == 1
    # 界面必须能说出"是哪几条"，否则教秘还是两眼一抹黑
    assert db.load_report["pending_samples"], "待排任务要给出样例"


def test_exam_rows_are_reported_but_not_scheduled(tmp_path):
    """真实表里有 3 行「大物实验B/C 考试」没排时间。

    两条要求同时成立：① 不能被静默丢弃（要报出来）；② 不能当成实验项目丢进求解器
    （考试的时间/场地逻辑与实验课不同，且这些行连教师都没有）。
    """
    p = tmp_path / "exam.xlsx"
    p.write_bytes(_write_rows([
        [1, "周一", "567", "一班", "1组", "扭摆法", "A410", "林老师"],
        [None, None, None, "大物实验B班级", "", "大物实验B考试", "", ""],
    ]).getvalue())
    db = ScheduleDatabase()
    db.load_from_excel(str(p))

    assert db.load_report["pending"] == 0, "考试不该算进可排的待排任务"
    assert db.load_report["pending_excluded"] == 1, "考试被静默丢弃了（老毛病）"
    assert "考试" in db.load_report["pending_excluded_reason"]

    ps = ParallelScheduler(db, extra_tasks=db.pending)
    overview = ps.get_course_overview()
    assert all("考试" not in pr["project"] for c in overview for pr in c["projects"])


def test_fully_empty_rows_are_not_reported_as_pending(tmp_path):
    p = tmp_path / "empty.xlsx"
    p.write_bytes(_write_rows([[None, None, None, None, None, None, None, None]]).getvalue())
    db = ScheduleDatabase()
    db.load_from_excel(str(p))
    assert len(db.pending) == 0, "空行不是待排任务"


def test_other_schools_file_is_not_rejected_by_sheet_name(tmp_path):
    """换学校/另存导致工作表名对不上 ⇒ 不该直接抛 KeyError（产品使用指南承诺"支持其他学校"）。"""
    p = tmp_path / "other.xlsx"
    p.write_bytes(_write_rows(
        [[1, "周一", "567", "一班", "1组", "扭摆法", "A410", "林老师"]],
        sheet_name="2025春实验课表",
    ).getvalue())
    db = ScheduleDatabase()
    db.load_from_excel(str(p))          # 旧实现：KeyError → 400「文件解析失败：'大学物理实验安排表 (周) '」
    assert len(db.entries) == 1
    assert db.sheet_name == "2025春实验课表"


def test_find_schedule_sheet_prefers_exact_name():
    import openpyxl

    wb = openpyxl.Workbook()
    wb.active.title = "随便一个表"
    ws, name = find_schedule_sheet(wb)
    assert ws is not None and name == "随便一个表"


# ---------------------------------------------------------------------------
# 2. 待排任务真的进求解器（从零排课的闭环）
# ---------------------------------------------------------------------------

def test_pending_tasks_enter_parallel_overview_and_solve():
    """大纲里新增的项目要出现在总览里，并且被求解器排掉 —— 不只是"被记下来"。"""
    # 周次上限看**整张表**覆盖到第几周（真实表 1–16 周），所以这里要铺满 16 周，
    # 否则"本表只覆盖到第1周"会把待排任务一起卡住（这个报错本身是对的）。
    db = _db(
        [_entry(1, "周四", "567", "23物本", "1组", "A502", experiment="扭摆法")]
        + [_entry(w, "周一", "678", "别的班", "1组", "A503", teacher="陈老师", experiment="其他实验")
           for w in range(1, 17)],
        pending=[
            PendingTask("23物本", "光的偏振", "1组", teacher="吴老师"),
            PendingTask("23物本", "声速测量", "1组", teacher="吴老师"),
        ],
    )
    ps = ParallelScheduler(db, extra_tasks=db.pending)

    overview = ps.get_course_overview()
    cls = [c for c in overview if c["class_name"] == "23物本"][0]
    assert cls["total_units"] == 3, "待排单元没进总览"
    assert cls["pending_units"] == 2, "界面看不出哪些是新增的"

    res = ps.solve("23物本", weekday="周四", start_week=1, weeks=16)
    assert res["success"] is True, res.get("error")
    assert res["total_units"] == 3
    assert res["pending_units"] == 2
    projects = {s["project"] for s in res["schedule"]}
    assert {"光的偏振", "声速测量"} <= projects, "新增项目没被排进去"


def test_pending_only_class_can_be_scheduled_from_scratch():
    """一个班在安排表里**完全不存在**（只有大纲清单）也要能排出来。

    这是"从零排课"的硬指标：表里没有任何已排记录 ⇒ 不能拿"表里的最后一周"当上限
    （那样可用周数恒为 0，路被自己堵死）。
    """
    db = _db([], pending=[
        PendingTask("25级新专业", f"实验{i}", "1组", teacher="林老师", room="A410")
        for i in range(6)
    ])
    ps = ParallelScheduler(db, extra_tasks=db.pending)
    res = ps.solve("25级新专业", weekday="周一", start_week=1, weeks=16)
    assert res["success"] is True, res.get("error")
    assert res["scheduled"] == 6


def test_pending_tasks_enter_auto_scheduler():
    db = _db([
        _entry(1, "周一", "567", "一班", "1组", "A410", experiment="扭摆法"),
    ], pending=[PendingTask("一班", "新增实验", "1组", teacher="吴老师")])
    sections, _ = AutoScheduler(db, extra_tasks=db.pending)._get_course_sections()
    courses = {s["course"] for s in sections}
    assert "新增实验" in courses, "一键自动排课排不到大纲新增的任务"


# ---------------------------------------------------------------------------
# 3. 大纲清单解析（按表头别名，不靠列序）
# ---------------------------------------------------------------------------

def _write_master(rows, header=None, sheet_name="实验项目清单"):
    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = sheet_name
    ws.append(header or ["课程", "班级", "实验项目", "学生组", "授课教师", "场地"])
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


def test_master_list_is_parsed_by_header_aliases(tmp_path):
    """列名千奇百怪（"行政班级"/"项目名称"/"指导教师"）也要认得出来。"""
    p = tmp_path / "master.xlsx"
    p.write_bytes(_write_master(
        [["大学物理实验", "25级新专业", "迈克耳孙干涉", "1组", "吴老师", "A410"]],
        header=["课程名称", "行政班级", "项目名称", "组别", "指导教师", "实验室"],
    ).getvalue())
    tasks, report = parse_master_tasks(str(p))
    assert len(tasks) == 1
    t = tasks[0]
    assert (t.class_name, t.experiment, t.teacher, t.room) == ("25级新专业", "迈克耳孙干涉", "吴老师", "A410")
    assert report["tasks"] == 1
    assert t.source == "master"


def test_master_list_without_required_columns_says_which(tmp_path):
    """认不出"班级/实验项目"时要说清缺什么，而不是静默返回 0 条。"""
    p = tmp_path / "bad.xlsx"
    p.write_bytes(_write_master([["甲", "乙"]], header=["序号", "备注"]).getvalue())
    with pytest.raises(ValueError) as ei:
        parse_master_tasks(str(p))
    assert "班级" in str(ei.value) and "实验项目" in str(ei.value)


def test_upload_master_endpoint_stores_tasks(monkeypatch, tmp_path):
    """上传清单后主数据进内存，/master-status 能查到（端到端的最小闭环）。"""
    p = tmp_path / "m.xlsx"
    p.write_bytes(_write_master([
        ["大学物理实验", "25级新专业", "迈克耳孙干涉", "1组", "吴老师", "A410"],
        ["大学物理实验", "25级新专业", "声速测量", "2组", "陈老师", "A411"],
    ]).getvalue())

    class _Up:
        filename = "m.xlsx"

        async def read(self):
            return p.read_bytes()

    monkeypatch.setattr(api, "_upload_target", lambda name, suffixes: str(tmp_path / "saved.xlsx"))
    out = asyncio.run(api.upload_master(_Up()))
    assert out["success"] is True and out["total_tasks"] == 2

    st = asyncio.run(api.master_status())
    # /master-status 会 _ensure_loaded()：没有真实表就跳过
    assert st["data"]["master_tasks"] == 2
    assert st["data"]["master_classes"] == ["25级新专业"]
    api._master_tasks.clear()
