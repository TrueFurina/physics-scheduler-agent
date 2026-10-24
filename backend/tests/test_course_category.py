"""「课程类别」分类码列 → 课程名（产品决策 B，2026-10-24 用户拍板）

背景
----
真实安排表第 9 列「课程类别」携带 C/B/普物3 三门课的分班码。三轴分离证据
（教师/班级/项目，docs/决策-课程类别归属-20261024.md §2.2）表明这是三门不同
的课，不是一门课的两个轨；学校公文（「任课老师安排表」公共课程名称）与考试
班名（「大物实验B/C班级」）给出映射值。

解析器把该列当**分类码**翻译成课程名，逐行落进 `ScheduleEntry.course`
（R-66 显式外键的权威归属）。`test_course_bridge.py::test_real_table_baseline`
的 distinct_courses==3 依赖本文件锁定的映射。

本文件锁四件事
--------------
1. 码→名映射本身（含未知码**原样返回** —— 不静默吞成默认课，R-35 同族）；
2. 分类码列探测（精确匹配「课程类别」，与课程名列探测刻意分开、不误认）；
3. 合成表：分类码列正确翻译进 entries；**真课程列存在时优先**（B 是兜底不是覆盖）；
4. 真实表：433 条全部显式归属三门课（R-55：合成全绿 ≠ 真实过）。

反假绿判据（R-25，开发期已执行并记录在案）
----------------------------------------
缺陷版 = 删掉 `COURSE_CODE_MAP` 或 `_detect_course_category_col` ⇒ 本文件真实表
用例与 `test_real_table_baseline`（distinct==3）必须红；改回即绿。
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.utils import excel_parser
from app.utils.excel_parser import ScheduleDatabase

REAL_XLSX = os.path.join(os.path.dirname(__file__), "..", "..", "schedule.xlsx")

EXPECTED_COURSES = {"大学物理实验C", "大学物理实验B", "普通物理实验3"}


# ---------------------------------------------------------------- 1. 映射本身

def test_translate_known_codes():
    """C/B/普物3 → 学校公文证据的课程名；前后空白不算异常值。"""
    assert excel_parser.translate_course_code("C") == "大学物理实验C"
    assert excel_parser.translate_course_code("B") == "大学物理实验B"
    assert excel_parser.translate_course_code("普物3") == "普通物理实验3"
    assert excel_parser.translate_course_code(" 普物3 ") == "普通物理实验3"


def test_translate_unknown_code_passes_through():
    """未知码**原样返回**：异常值必须在 data-audit / 导出里可见。

    吞成默认课 = 把"表里出现了没听说过的码"藏起来（R-35 同族错误）。
    """
    assert excel_parser.translate_course_code("D") == "D"
    assert excel_parser.translate_course_code("") == ""
    assert excel_parser.translate_course_code(None) == ""


# ---------------------------------------------------------------- 2. 列探测

def _one_row_sheet(header, tmp_path):
    """造一个只带表头行的工作表对象（openpyxl，不落盘）。"""
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(header)
    return ws


def test_detect_category_col_found(tmp_path):
    ws = _one_row_sheet(
        ["周次", "周别", "时段", "班级", "组别", "项目", "场地", "教师", "课程类别"],
        tmp_path)
    assert excel_parser._detect_course_category_col(ws) == 8


def test_detect_category_col_absent_returns_none(tmp_path):
    """没有「课程类别」列 ⇒ None（老学校的表原样走反推路径，行为不变）。"""
    ws = _one_row_sheet(
        ["周次", "周别", "时段", "班级", "组别", "项目", "场地", "教师"],
        tmp_path)
    assert excel_parser._detect_course_category_col(ws) is None


def test_detect_category_col_does_not_confuse_with_course_col(tmp_path):
    """「课程」列不是「课程类别」列：两个探测函数各认各的，互不误认。

    这正是 R-66「模糊子串把课程类别误认成课程列」事故的锁死用例。
    """
    ws = _one_row_sheet(
        ["周次", "周别", "时段", "班级", "组别", "项目", "场地", "教师", "课程"],
        tmp_path)
    assert excel_parser._detect_course_category_col(ws) is None
    assert excel_parser._detect_course_col(ws) == 8


# ---------------------------------------------------------------- 3. 合成表

_HEADER_WITH_CATEGORY = [
    "周次", "周别", "时段", "班级", "组别", "项目", "场地", "教师", "课程类别",
    "实验筹备人员", "备注", "班级序号", "周序号", "备注",
]
_ROWS = [
    (2, "周一", "567", "甲班", "1组", "扭摆法", "A504", "吴琳", "B"),
    (2, "周一", "678", "甲班", "2组", "声速测量", "A516", "吴琳", "B"),
    (3, "周二", "567", "乙班", "1组", "伏安法测二极管的特性曲线", "A410", "林珠云", "C"),
    (3, "周三", "567", "丙班", "1组", "光的衍射", "A412", "吴琼", "普物3"),
    (4, "周四", "567", "丁班", "1组", "未知项目", "A413", "王某", "Z"),  # 未知码
]


def _write_xlsx(path, header, rows):
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "大学物理实验安排表 (周)"
    ws.append(header)
    ws.append([f"占位{r}" for r in range(len(header))])  # 第 2 行表头占位
    for r in rows:
        ws.append(list(r))
    wb.save(path)


@pytest.fixture()
def no_cache(monkeypatch):
    """合成表用例不落解析缓存（PARSE_CACHE=0，与 test_parse_cache 同口径）。"""
    monkeypatch.setenv("PARSE_CACHE", "0")
    excel_parser.parse_cache.reset_memo()
    yield
    excel_parser.parse_cache.reset_memo()


def test_category_column_translated_into_entries(tmp_path, no_cache):
    """分类码列逐行翻译成课程名落进 entries；未知码原样保留可见。"""
    path = tmp_path / "synthetic.xlsx"
    _write_xlsx(path, _HEADER_WITH_CATEGORY, _ROWS)

    db = ScheduleDatabase()
    db.load_from_excel(str(path))

    by_exp = {e.experiment: e.course for e in db.entries}
    assert by_exp["扭摆法"] == "大学物理实验B"
    assert by_exp["声速测量"] == "大学物理实验B"
    assert by_exp["伏安法测二极管的特性曲线"] == "大学物理实验C"
    assert by_exp["光的衍射"] == "普通物理实验3"
    assert by_exp["未知项目"] == "Z", "未知码必须原样保留（不吞成默认课）"


def test_real_course_column_takes_precedence_over_category(tmp_path, no_cache):
    """源表同时有真「课程」列和「课程类别」列 ⇒ 课程列优先，分类码不覆盖。

    B 是"没有课程列时的兜底"，不是覆盖 —— 否则外校带了真课程名的表会被
    分类码翻译篡改。
    """
    header = ["周次", "周别", "时段", "班级", "组别", "项目", "场地", "教师",
              "课程", "课程类别"]
    rows = [
        (2, "周一", "567", "甲班", "1组", "扭摆法", "A504", "吴琳", "大学物理实验A", "B"),
        (3, "周二", "567", "乙班", "1组", "声速测量", "A516", "吴琳", "大学物理实验A", "C"),
    ]
    path = tmp_path / "both.xlsx"
    _write_xlsx(path, header, rows)

    db = ScheduleDatabase()
    db.load_from_excel(str(path))

    assert {e.course for e in db.entries} == {"大学物理实验A"}


def test_no_category_column_keeps_legacy_behavior(tmp_path, no_cache):
    """两列都没有 ⇒ course 恒空、回落主数据反推（R-57 行为，向后兼容锁死）。"""
    header = ["周次", "周别", "时段", "班级", "组别", "项目", "场地", "教师"]
    rows = [(2, "周一", "567", "甲班", "1组", "扭摆法", "A504", "吴琳")]
    path = tmp_path / "legacy.xlsx"
    _write_xlsx(path, header, rows)

    db = ScheduleDatabase()
    db.load_from_excel(str(path))

    assert [e.course for e in db.entries] == [""]


# ---------------------------------------------------------------- 4. 真实表

def test_real_table_all_entries_explicitly_categorized():
    """真实表 433 条全部显式归属三门课（R-55：真实数据必须过，不是合成自嗨）。

    本用例同时是 CACHE_VERSION  bump 的哨兵：忘了 bump 则磁盘上的旧缓存
    （course 全空）签名仍"有效"被命中 ⇒ 本用例红（R-66 同款事故的锁死）。
    """
    if not os.path.exists(REAL_XLSX):
        pytest.skip("仓库根没有 schedule.xlsx")
    db = ScheduleDatabase()
    db.load_from_excel(REAL_XLSX)

    assert len(db.entries) > 100
    assert all(e.course for e in db.entries), \
        f"存在未归属条目：{[e.experiment for e in db.entries if not e.course][:5]}"
    assert {e.course for e in db.entries} == EXPECTED_COURSES
    # 每个班单一类别（决策文档 §2.4-1）⇒ 班级课表导出仍每班一个块
    by_class = {}
    for e in db.entries:
        by_class.setdefault(e.class_name, set()).add(e.course)
    multi = {c: cs for c, cs in by_class.items() if len(cs) > 1}
    assert not multi, f"出现跨类别班级（与决策文档 §2.4-1 矛盾）：{multi}"
