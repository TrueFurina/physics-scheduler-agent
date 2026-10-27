"""跨课程合并（R-57）—— 导师点名的后续难点

「若同一班同时上多门含实验的课程，需跨课程合并并标冲突」。

先说清楚这条测的是什么（不粉饰现状）
------------------------------------
课程归属以 R-66 显式外键（`ScheduleEntry.course`）为权威，主数据
`experiment.course` 反推为兜底。2026-10-24 产品决策 B 起，真实表经「课程
类别」分类码列逐行显式归属三门课（大学物理实验B/C、普通物理实验3，见
test_course_category.py）；无该列的学校仍全部归默认课程「大学物理实验」。

- 跨课程撞车恒 0 是**数据现状**（每班单一类别），不是"功能生效了"。
- 教秘一旦用 R-55 的台账导回把某个项目改归另一门课，跨课程立刻会真实出现。
  所以能力必须先落地，且**必须能证明它真的会报**，而不是永远 0。

三条硬判据
----------
1. 跨课程撞车按 **(班级, 组)** 判，不按班级判 —— 真实表「同班不同组同格」
   **47 处**是正常分批教学（1 组 A504 扭摆法 / 2 组 A516 声速测量）。按班级判
   = 立刻 47 处假阳性（R-26「空场地归 '' 报假冲突」同族错误）。用例 4 反向锁死。
2. 必须**课程不同**才报 —— 课程相同的同班同组撞格是同学科内的排课错误，归
   `conflict_model` 的 group 类管，这里不重复报。用例 3 双向锁死。
3. 跨课程撞车是 group 冲突的**子集**（同班同组同格必然已被 group 抓到），
   所以 `core_conflict_count` **不得**把它重复计一遍。用例 7 锁死。
4. 主数据未初始化时课程全部回落默认 ⇒ 跨课程恒 0 处。那是"没查"，不是"没问题"，
   必须由 `course_coverage.master_ready=False` 说出来（R-35 同族错误）。用例 5。

R-66 更新（显式课程外键）
--------------------------
`ScheduleEntry` 现在**有** course 字段：源安排表自带课程列时逐行落入，成为权威归属；
`resolve_course` 优先用它，没有才回落主数据 `experiment.course` 反推。这修掉了
"主数据 experiments 以项目名为 key、同名项目跨两门课会塌缩"的歧义（见下方
`test_same_project_two_courses_disambiguated_by_entry_course`）。

2026-10-24 产品决策 B 追加：真实表无真课程列、但有「课程类别」分类码列 ⇒ 经
`COURSE_CODE_MAP` 翻译后 433 条全部显式归属三门课，不再回落反推（向后兼容：
两列都没有的学校行为与 R-57 一致）。
"""

import asyncio
import io
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.api import reschedule as R
from app.course_bridge import (course_coverage, group_by_course, resolve_course,
                               scan_cross_course)
from app.master_data import DEFAULT_COURSE, MasterData
from app.utils.excel_parser import ScheduleDatabase, ScheduleEntry


def _e(week, weekday, slot, cls, group, exp, room="A504", teacher="吴琳"):
    return ScheduleEntry(week, weekday, slot, cls, group, exp, room, teacher)


def _md(pairs):
    """主数据：{项目名: 课程名}。"""
    md = MasterData()
    for exp, course in pairs.items():
        md.upsert_experiment(exp, course=course)
    return md


# ---------------------------------------------------------------- 1. 课程归属

def test_course_comes_from_master_data_not_from_the_row():
    """安排表行没有课程字段，课程只能靠项目名查主数据反推。"""
    md = _md({"扭摆法": "大学物理实验A", "声速测量": "大学物理实验B"})
    assert resolve_course(_e(1, "周二", "567", "C1", "C1_1", "扭摆法"),
                          {k: v for k, v in
                           ((n, e["course"]) for n, e in md.experiments.items())}) \
        == "大学物理实验A"


def test_unknown_project_falls_back_to_default_course():
    """项目不在主数据里 → 回落默认课，绝不变成"没有课程的幽灵行"被静默丢掉。"""
    md = _md({"扭摆法": "大学物理实验A"})
    courses = {n: (e["course"] or DEFAULT_COURSE) for n, e in md.experiments.items()}
    assert resolve_course(_e(1, "周二", "567", "C1", "C1_1", "没登记的项目"), courses) \
        == DEFAULT_COURSE
    # 主数据整个没初始化时同理
    assert resolve_course(_e(1, "周二", "567", "C1", "C1_1", "扭摆法"), None) == DEFAULT_COURSE


# ---------------------------------------------------------------- 2. 跨课程撞车

def test_cross_course_clash_is_reported():
    """同一班同一组同一格，分属两门课 → 必须报出来（这是导师点名的难点）。"""
    md = _md({"扭摆法": "大学物理实验A", "声速测量": "大学物理实验B"})
    entries = [
        _e(5, "周四", "567", "24电信1班", "24电信1_1", "扭摆法", room="A504"),
        _e(5, "周四", "567", "24电信1班", "24电信1_1", "声速测量", room="A516"),
    ]
    res = scan_cross_course(entries, md)
    assert len(res["conflicts"]) == 1, res
    c = res["conflicts"][0]
    assert c["kind"] == "cross_course" and c["severity"] == "core"
    assert c["place"] == "24电信1班/24电信1_1"
    assert set(c["involved"]) == {"大学物理实验A", "大学物理实验B"}
    assert "协同改时间" in c["fix_hint"]
    # 两条记录各自的课程都要摊出来，否则教秘不知道该找谁
    assert any("大学物理实验A" in d for d in c["detail"])
    assert any("大学物理实验B" in d for d in c["detail"])


def test_same_course_clash_is_not_cross_course():
    """反向：同班同组同格但**同一门课** → 是同学科内的整组冲突，不是跨课程。

    这类由 conflict_model 的 group 类管，这里不重复报（core 总数不能翻倍）。
    """
    md = _md({"扭摆法": "大学物理实验A", "牛顿环": "大学物理实验A"})
    entries = [
        _e(5, "周四", "567", "24电信1班", "24电信1_1", "扭摆法", room="A504"),
        _e(5, "周四", "567", "24电信1班", "24电信1_1", "牛顿环", room="A516"),
    ]
    assert scan_cross_course(entries, md)["conflicts"] == []


def test_cross_course_different_groups_is_parallel_not_clash():
    """反向：同班**不同组**跨课程同格 = 正常分批并行教学，绝不能报冲突。

    这是本文件最要紧的一条 —— 真实表同班不同组同格有 47 处，按班级判会全成假阳性。
    """
    md = _md({"扭摆法": "大学物理实验A", "声速测量": "大学物理实验B"})
    entries = [
        _e(5, "周四", "567", "24电信1、2班", "24电信1_1", "扭摆法", room="A504"),
        _e(5, "周四", "567", "24电信1、2班", "24电信2_1", "声速测量", room="A516"),
    ]
    assert scan_cross_course(entries, md)["conflicts"] == [], \
        "同班不同组跨课程被误判成撞车"


def test_cross_course_uses_real_blocked_slots():
    """跨节次记录（`14:00-17:00` 占 567+678）按真实占格判重叠。"""
    md = _md({"扭摆法": "大学物理实验A", "声速测量": "大学物理实验B"})
    a = _e(5, "周四", "567", "C1", "C1_1", "扭摆法")
    a.blocked = ["567", "678"]
    entries = [a, _e(5, "周四", "678", "C1", "C1_1", "声速测量")]
    assert len(scan_cross_course(entries, md)["conflicts"]) == 1


# ---------------------------------------------------------------- 3. 口径：没查 ≠ 没问题

def test_coverage_says_not_checked_when_master_empty():
    """主数据未初始化 → 课程全回落默认，跨课程恒 0。必须标 master_ready=False。

    否则教秘看到"跨课程 0 处"会以为真查过了 —— R-35「看不懂就静默丢弃」同族。
    """
    entries = [
        _e(5, "周四", "567", "C1", "C1_1", "扭摆法"),
        _e(5, "周四", "567", "C1", "C1_1", "声速测量"),
    ]
    res = scan_cross_course(entries, None)
    assert res["conflicts"] == []
    assert res["coverage"]["master_ready"] is False
    assert res["coverage"]["distinct_courses"] == 1
    assert res["coverage"]["fallback_default"] == 2
    assert course_coverage(entries, None)["project_in_master"] == 0


def test_coverage_counts_explicit_courses():
    md = _md({"扭摆法": "大学物理实验A", "声速测量": "大学物理实验B"})
    entries = [_e(1, "周二", "567", "C1", "C1_1", "扭摆法"),
               _e(2, "周二", "567", "C1", "C1_1", "声速测量")]
    cov = course_coverage(entries, md)
    assert cov["master_ready"] is True
    assert cov["explicit_course"] == 2 and cov["fallback_default"] == 0
    assert cov["distinct_courses"] == 2


# ---------------------------------------------------------------- 4/5. 导出行列表

@pytest.fixture
def db(monkeypatch):
    d = ScheduleDatabase()
    d.entries = [
        _e(3, "周二", "567", "本班", "本班_1", "扭摆法", room="A504"),
        _e(4, "周二", "567", "本班", "本班_1", "声速测量", room="A516"),
    ]
    d.loaded = True
    monkeypatch.setattr(R, "_db", d)
    return d


def _sheet_of(resp):
    """StreamingResponse 的 body_iterator 是 async generator，得异步消费。"""
    import openpyxl

    async def _read():
        return b"".join([chunk async for chunk in resp.body_iterator])

    return openpyxl.load_workbook(io.BytesIO(asyncio.run(_read())))


def test_export_has_course_column_even_for_single_course(db, monkeypatch):
    """单课程也要有课程列 —— 否则教秘无从判断这行属于哪门课。"""
    md = _md({"扭摆法": DEFAULT_COURSE, "声速测量": DEFAULT_COURSE})
    md.build_from_entries(db.entries)
    md.initialized = True
    monkeypatch.setattr(R, "_master_data", md)

    wb = _sheet_of(asyncio.run(R.export_class_schedule()))
    ws = wb["本班"]
    header = [c.value for c in ws[1]]
    assert "课程" in header, header
    rows = [[c.value for c in r] for r in ws.iter_rows(min_row=2)]
    assert all(r[header.index("课程")] == DEFAULT_COURSE for r in rows if r[0]), rows
    # 单课程时不多插分块标题行
    assert not any(isinstance(r[0], str) and r[0].startswith("【") for r in rows), rows


def test_export_blocks_by_course_when_class_has_multi_courses(db, monkeypatch):
    """一个班跨两门课 → 按课程分块，块首有「【课程名】」标题行。"""
    md = _md({"扭摆法": "大学物理实验A", "声速测量": "大学物理实验B"})
    md.build_from_entries(db.entries)
    md.upsert_experiment("扭摆法", course="大学物理实验A")
    md.upsert_experiment("声速测量", course="大学物理实验B")
    md.initialized = True
    monkeypatch.setattr(R, "_master_data", md)

    wb = _sheet_of(asyncio.run(R.export_class_schedule()))
    ws = wb["本班"]
    header = [c.value for c in ws[1]]
    rows = [[c.value for c in r] for r in ws.iter_rows(min_row=2)]
    titles = [r[0] for r in rows if r[0] and str(r[0]).startswith("【")]
    assert titles == ["【大学物理实验A】", "【大学物理实验B】"], titles
    # 每行都带自己的课程，能归因
    courses = [r[header.index("课程")] for r in rows if r[0] and not str(r[0]).startswith("【")]
    assert courses == ["大学物理实验A", "大学物理实验B"], courses


def test_export_lists_cross_course_clash_at_sheet_tail(db, monkeypatch):
    """该班出现跨课程撞车 → 表尾单独列出（两门课的老师互不知情，最难自发发现）。"""
    db.entries.append(_e(4, "周二", "567", "本班", "本班_1", "牛顿环", room="A517"))
    # 第4周同格两条：声速测量(B) + 牛顿环(A) ⇒ 跨课程撞车
    # （若两条同课，那只是同学科内的整组冲突，不属跨课程 —— 见 test_same_course_...）
    md = MasterData().build_from_entries(db.entries)
    for n, c in (("扭摆法", "大学物理实验A"), ("声速测量", "大学物理实验B"),
                 ("牛顿环", "大学物理实验A")):
        md.upsert_experiment(n, course=c)
    md.initialized = True
    monkeypatch.setattr(R, "_master_data", md)

    wb = _sheet_of(asyncio.run(R.export_class_schedule()))
    ws = wb["本班"]
    text = "\n".join(str(c.value) for r in ws.iter_rows() for c in r if c.value)
    assert "跨课程撞车" in text, text
    assert "大学物理实验A" in text and "大学物理实验B" in text


# ---------------------------------------------------------------- 6. 体检不重复计数

def test_audit_counts_cross_course_without_double_counting(db, monkeypatch):
    """跨课程是整组冲突的子集 → core_conflict_count 不能把它数两遍。

    否则教秘看到「整组 5 处 + 跨课程 3 处」会以为一共 8 处。
    """
    db.entries.append(_e(4, "周二", "567", "本班", "本班_1", "牛顿环", room="A517"))
    md = MasterData().build_from_entries(db.entries)
    for n, c in (("扭摆法", "大学物理实验A"), ("声速测量", "大学物理实验B"),
                 ("牛顿环", "大学物理实验A")):
        md.upsert_experiment(n, course=c)
    md.initialized = True
    monkeypatch.setattr(R, "_master_data", md)

    data = asyncio.run(R.data_audit())["data"]
    assert data["group_conflict_count"] == 1, data
    assert data["cross_course_conflict_count"] == 1, data
    assert data["core_conflict_count"] == 1, f"跨课程被重复计入 core：{data}"
    assert data["group_conflicts"][0]["cross_course"] is True
    assert "不重复计" in data["cross_course_note"], data["cross_course_note"]


def test_audit_says_course_is_fallback_when_project_not_in_master(db, monkeypatch):
    """项目不在主数据里 → 课程是**回落默认**，不是查出来的，必须说出口。

    R-58 后真实表已无此缺口（绪论类也登记），但缺口机制必须保留：教秘手工
    新增项目不进台账、或台账导入漏页时，同样会出现"查不出课程"的记录。
    沉默地让一部分记录"查不出课程"比报假冲突更危险（R-35 同族）。
    """
    # 「绪论课」故意不进主数据：md 只建其余项目
    md = MasterData().build_from_entries([e for e in db.entries if e.experiment != "声速测量"])
    md.initialized = True
    monkeypatch.setattr(R, "_master_data", md)

    data = asyncio.run(R.data_audit())["data"]
    cov = data["course_coverage"]
    assert cov["project_in_master"] < cov["total_entries"], cov
    assert "不在主数据" in data["cross_course_note"], data["cross_course_note"]


# ---------------------------------------------------------------- 7. 真实表基线

def test_real_table_baseline():
    """真实 433 条：三门课程显式归属，跨课程撞车 0 处（口径锁死）。

    ⚠️ distinct_courses==3 是**产品决策 B**（2026-10-24 用户拍板：采用「课程
    类别」分类码列 + COURSE_CODE_MAP 翻译），不是代码漂移 —— 决策文档
    docs/决策-课程类别归属-20261024.md，反假绿判据见 test_course_category.py。
    改这个数字前先读那份文档。
    ⚠️ 0 处撞车是**数据现状**（每班单一类别），不是"功能生效" —— 功能生效由上面的用例证明。
    """
    real = os.path.join(os.path.dirname(__file__), "..", "..", "schedule.xlsx")
    if not os.path.exists(real):
        pytest.skip("仓库根没有 schedule.xlsx")
    d = ScheduleDatabase()
    d.load_from_excel(real)
    md = MasterData().build_from_entries(d.entries)

    res = scan_cross_course(d.entries, md)
    assert res["conflicts"] == [], res["conflicts"]
    assert res["coverage"]["total_entries"] == len(d.entries)
    assert res["coverage"]["master_ready"] is True
    # 产品决策 B：C→大学物理实验C / B→大学物理实验B / 普物3→普通物理实验3
    assert res["coverage"]["distinct_courses"] == 3, res["coverage"]
    # 433 条全部显式归属（源表分类码列逐行给出），零回落默认
    assert res["coverage"]["explicit_in_entry"] == res["coverage"]["total_entries"], res["coverage"]
    assert res["coverage"]["fallback_default"] == 0, res["coverage"]
    courses = {e.course for e in d.entries}
    assert courses == {"大学物理实验C", "大学物理实验B", "普通物理实验3"}, courses
    # 口径锁死（R-58 后）：3 个绪论类项目也登记为主数据实体 ⇒ 433 条全部查得课程，
    # 不再有"回落默认而非查得"的 17 条缺口。绪论项目 marked schedulable=False。
    assert res["coverage"]["project_in_master"] == res["coverage"]["total_entries"], res["coverage"]
    for intro in ("绪论-光学实验预备知识", "绪论课（教室已安排，请查系统）", "绪论课（教室自己申请）"):
        assert intro in md.experiments, intro
        assert md.overview()  # overview 可渲染
    # 每班单一类别（决策文档 §2.4-1）⇒ 即使 3 门课，单班仍只有一个块，无分块标题
    blocks = group_by_course(
        [e for e in d.entries if e.class_name == d.entries[0].class_name], md)
    assert len(blocks) == 1


# ---------------------------------------------------------------- 8. R-66 显式课程外键

def test_resolve_course_prefers_explicit_entry_course():
    """条目自带 course（源表课程列）时**优先**用它，不靠项目名反推。

    缺陷版（resolve_course 忽略 entry.course）下会回落到主数据反推的默认课 ⇒ 本用例红。
    """
    e = _e(1, "周二", "567", "C1", "C1_1", "扭摆法")
    e.course = "光学实验"                       # 源表显式给出
    courses = {"扭摆法": DEFAULT_COURSE}        # 主数据反推会给默认课
    assert resolve_course(e, courses) == "光学实验"
    # 主数据整个没初始化时，显式 course 仍然权威
    assert resolve_course(e, None) == "光学实验"


def test_resolve_course_falls_back_to_master_when_entry_course_empty():
    """条目无显式 course（真实表无课程列）→ 回落主数据反推（向后兼容）。"""
    e = _e(1, "周二", "567", "C1", "C1_1", "扭摆法")   # course 默认 ""
    assert e.course == ""
    assert resolve_course(e, {"扭摆法": "大学物理实验A"}) == "大学物理实验A"


def test_same_project_two_courses_disambiguated_by_entry_course():
    """同名项目分属两门课：主数据 name-keyed 会塌缩，靠 entry.course 区分（R-66 核心价值）。

    缺陷版（resolve_course 忽略 entry.course）下两条都反推到同一门默认课 ⇒
    by_course 只有 1 个 key ⇒ 不报跨课程撞车 ⇒ 本用例红。
    """
    e1 = _e(1, "周二", "567", "C1", "C1_1", "设计性实验")
    e2 = _e(1, "周二", "567", "C1", "C1_1", "设计性实验")
    e1.course = "物理实验A"
    e2.course = "物理实验B"
    md = MasterData().build_from_entries([e1, e2])
    # 主数据里"设计性实验"只有一门课（name-keyed 塌缩）—— 正是歧义来源
    assert md.experiments["设计性实验"]["course"] == DEFAULT_COURSE

    res = scan_cross_course([e1, e2], md)
    assert len(res["conflicts"]) == 1, res["conflicts"]
    assert set(res["conflicts"][0]["involved"]) == {"物理实验A", "物理实验B"}


def test_course_coverage_counts_explicit_in_entry():
    """coverage 要能区分"源表显式给课程"与"反推"——显式最可信，单独计数。"""
    e1 = _e(1, "周二", "567", "C1", "C1_1", "扭摆法")
    e1.course = "光学实验"
    e2 = _e(1, "周三", "567", "C1", "C1_1", "声速测量")   # 无显式 course
    md = _md({"扭摆法": DEFAULT_COURSE, "声速测量": DEFAULT_COURSE})
    cov = course_coverage([e1, e2], md)
    assert cov["explicit_in_entry"] == 1, cov
    assert cov["total_entries"] == 2


def test_parser_reads_course_column_when_present(tmp_path, monkeypatch):
    """源安排表**自带课程列**时，按表头别名逐行落进 ScheduleEntry.course（R-66 探测）。

    课程列刻意放在第 9 列（索引 8，非固定列序）—— 验证靠表头别名而非列序。
    """
    import openpyxl
    from app.utils import parse_cache
    monkeypatch.setenv("PARSE_CACHE", "0")     # 隔离：不写默认缓存目录
    parse_cache.reset_memo()

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "实验安排表"
    ws.append(["周次", "星期", "时段", "班级", "组别", "实验项目", "场地", "教师", "课程"])
    ws.append([None] * 9)                       # 第二行表头（解析从第 3 行读数据）
    ws.append([1, "周二", "567", "C1", "g1", "扭摆法", "A504", "吴琳", "光学实验"])
    ws.append([2, "周三", "678", "C1", "g1", "声速测量", "A516", "陈老师", "声学实验"])
    p = tmp_path / "sched_with_course.xlsx"
    wb.save(str(p))

    db = ScheduleDatabase()
    db.load_from_excel(str(p))
    assert len(db.entries) == 2
    assert {e.course for e in db.entries} == {"光学实验", "声学实验"}
    # 显式 course 无需主数据即权威
    assert resolve_course(db.entries[0], None) in {"光学实验", "声学实验"}


def test_parser_without_course_column_leaves_entry_course_empty(tmp_path, monkeypatch):
    """源表**没有**课程列（真实表情形）→ 全部条目 course 留空，回落反推（向后兼容）。"""
    import openpyxl
    from app.utils import parse_cache
    monkeypatch.setenv("PARSE_CACHE", "0")
    parse_cache.reset_memo()

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "实验安排表"
    ws.append(["周次", "星期", "时段", "班级", "组别", "实验项目", "场地", "教师"])
    ws.append([None] * 8)
    ws.append([1, "周二", "567", "C1", "g1", "扭摆法", "A504", "吴琳"])
    p = tmp_path / "sched_no_course.xlsx"
    wb.save(str(p))

    db = ScheduleDatabase()
    db.load_from_excel(str(p))
    assert len(db.entries) == 1
    assert db.entries[0].course == ""
    # 回落主数据反推
    assert resolve_course(db.entries[0], {"扭摆法": "大学物理实验A"}) == "大学物理实验A"


# ---------------------------------------------------------------- R-70 并发快照
#
# 导出已经跑在工作线程上（R-70）。而教秘的 CRUD / 台账导入会**同时**改写
# `_master_data.experiments`，`project_courses()` 要遍历这个字典 —— 边改边遍历
# 会抛 `RuntimeError: dictionary changed size during iteration`，表现为导出 500。
# 处置：调用方在 `_master_lock` 内取一次「项目→课程」快照，再传给下面三个函数。
# 这三条用例把"必须用传入的快照、不得再去遍历主数据"变成可执行判据：
# master 一律传 None，实现若回退到现场遍历就会立刻拿到空表 / 判错课程。

def test_precomputed_course_map_drives_grouping():
    entries = [_e(1, "周二", "567", "C1", "g1", "扭摆法"),
               _e(1, "周三", "678", "C1", "g2", "声速测量")]
    courses = {"扭摆法": "大学物理实验A", "声速测量": "大学物理实验B"}

    blocks = group_by_course(entries, None, courses)
    assert [c for c, _ in blocks] == ["大学物理实验A", "大学物理实验B"]
    assert sum(len(items) for _, items in blocks) == 2


def test_precomputed_course_map_drives_cross_course_scan():
    # 同班同组同格、分属两门课 → 必须报跨课程撞车
    entries = [_e(1, "周二", "567", "C1", "g1", "扭摆法"),
               _e(1, "周二", "567", "C1", "g1", "声速测量")]
    courses = {"扭摆法": "大学物理实验A", "声速测量": "大学物理实验B"}

    out = scan_cross_course(entries, None, courses)
    assert len(out["conflicts"]) == 1, "快照里两门课不同，必须报撞车"
    assert out["coverage"]["master_ready"] is True, "快照非空 ⇒ 覆盖度必须承认「查过了」"


def test_precomputed_course_map_drives_coverage():
    entries = [_e(1, "周二", "567", "C1", "g1", "扭摆法")]
    cov = course_coverage(entries, None, {"扭摆法": "大学物理实验A"})
    assert cov["master_ready"] is True
    assert cov["project_in_master"] == 1
    assert cov["fallback_default"] == 0
