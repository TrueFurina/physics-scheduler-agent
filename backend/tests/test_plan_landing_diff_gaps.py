"""`plan_landing` 差异预览/归一化的「静默失败」守卫（R-106）。

为什么单独一个文件：`test_plan_landing.py` 已锁住"新增/调整/未覆盖/原样"四类账与
长实验截短警告，但**四个"静默丢"分支一直没人看着** —— 而它们正是本项目反复踩的
那一类：接口/预览说"没问题"，实际把一行悄悄吞了，教秘到教务系统才发现少了一节课。

  ▸ `normalize_plan` 收到非 dict 行（102-103）：必须报「整行不是对象」，绝不静默丢；
  ▸ `diff_plan` 方案里有表里没有的**新增单元**（154-155，同时走到长实验循环的 175）：
    必须进 `added` 账，不能被当成"无变化"吞掉；
  ▸ 方案行**没有场地**（206）/ **没有教师**（209）：必须各出一条 warning，不能静默落一张缺信息的表；
  ▸ `moves_after` 空时间戳（399）与"库里还没有 applied_moves 表"（409-410）：必须返回 0，
    而不是抛异常把撤销流程搞崩。

全是纯函数，不碰 HTTP / xlsx / 数据库文件（`moves_after` 用临时空库）。
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import plan_landing
from app.utils.excel_parser import ScheduleEntry


def _entry(week, weekday, slot, cls, group, room, teacher, experiment):
    return ScheduleEntry(week, weekday, slot, cls, group, experiment, room, teacher,
                         blocked=[slot])


def _entries():
    """两个可排单元（项目A 的 1组/2组），够做"新增/无场地/无教师"三组对照。"""
    return [
        _entry(2, "周一", "567", "甲班", "1组", "A410", "吴老师", "项目A"),
        _entry(2, "周一", "678", "甲班", "2组", "A410", "林老师", "项目A"),
    ]


def _row(**kw):
    base = {"class_name": "甲班", "project": "项目A", "group": "1组", "week": 1,
            "weekday": "周一", "time_slot": "567", "room": "A410", "teacher": "吴老师"}
    base.update(kw)
    return base


# ---------------------------------------------------------------------------
# 1. normalize_plan：非 dict 行必须报错，不能静默丢弃（102-103）
# ---------------------------------------------------------------------------

def test_normalize_rejects_non_dict_row():
    """传入一行字符串而不是对象 → 必须抛 PlanValidationError 且说明「整行不是对象」。"""
    with pytest.raises(plan_landing.PlanValidationError) as ei:
        plan_landing.normalize_plan(["这一行本该是个对象"])

    bad = ei.value.bad
    assert len(bad) == 1
    assert bad[0]["index"] == 0
    assert bad[0]["missing"] == ["整行不是对象"]


# ---------------------------------------------------------------------------
# 2. diff_plan：方案里表里没有的新增单元必须进 added 账（154-155 / 175）
# ---------------------------------------------------------------------------

def test_diff_reports_added_units_not_silently_dropped():
    """方案有一个当前表里根本没有的单元 → 必须进 added，且不算 changed。"""
    plan = [_row(project="新项目X", room="A999")]
    d = plan_landing.diff_plan(plan, _entries())

    assert len(d["added"]) == 1, d
    assert "新项目X" in d["added"][0]["unit"], d["added"]
    assert d["added"][0]["rows"] == plan
    # 新增单元不能同时被算成"调整"（否则前端会显示"改了 N 个"而非"新增 N 个"）
    assert d["changed"] == [], d
    assert d["has_changes"] is True


# ---------------------------------------------------------------------------
# 3. diff_plan：方案行没有场地 / 没有教师，必须各出一条 warning（206 / 209）
# ---------------------------------------------------------------------------

def test_diff_warns_plan_rows_without_room():
    """方案行 room 为空（灰箱场景）→ 必须警告「没有场地」，不能静默落地一张缺场地的表。"""
    plan = [_row(room="")]
    d = plan_landing.diff_plan(plan, _entries())

    assert any("没有场地" in w for w in d["warnings"]), d["warnings"]


def test_diff_warns_plan_rows_without_teacher():
    """方案行 teacher 为空 → 必须警告「没有教师」。"""
    plan = [_row(teacher="")]
    d = plan_landing.diff_plan(plan, _entries())

    assert any("没有教师" in w for w in d["warnings"]), d["warnings"]


# ---------------------------------------------------------------------------
# 4. moves_after：空时间戳 / 缺表，必须返回 0 而不是抛异常（399 / 409-410）
# ---------------------------------------------------------------------------

def test_moves_after_empty_timestamp_is_zero(tmp_path):
    """撤销时若落地时间戳为空 → 计 0 条，不该去查库。"""
    store = plan_landing.LandingStore(str(tmp_path / "l.db"))
    assert store.moves_after("") == 0


def test_moves_after_missing_table_returns_zero(tmp_path):
    """全新临时库还没有 applied_moves 表 → 查不到就返回 0，不能让撤销 500。"""
    store = plan_landing.LandingStore(str(tmp_path / "fresh.db"))
    assert store.moves_after("2026-01-01T00:00:00") == 0
