"""影响分析语义回归 —— 「受影响教师」里不许出现班级名

历史缺陷
--------
`/impact-analysis` 曾经这样聚合：`re.search(r"（([^）]+) ", e["reason"])`，
从**人类可读的中文文案**里反解结构化字段。而 `teacher_busy` 的文案是
「教师该时段已有课（{班级} {实验}）」——里面根本没有教师名，于是班级名被
塞进了 `affected_teachers`，教秘在界面上看到：

    👨‍🏫 受影响教师：2024级应用化学1、2班

两条结论锁死在这里：
1. `affected_teachers` 只允许出现真实教师姓名，绝不允许是班级名；
2. 「本人该时段已有课」是**自身冲突**，单列 `self_busy_slots`，不混进"别人受影响"。

数据是构造的（两条 ScheduleEntry），不依赖 schedule.xlsx，
这样换真实表/脱敏表都不会让断言漂移。
"""

import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import app.api.reschedule as api
from app.agent.reschedule_agent import RescheduleAgent
from app.utils.excel_parser import ScheduleDatabase, ScheduleEntry

WEEK = 3
MY_NAME = "吴琳"
OCCUPY_TEACHER = "陈老师"
OCCUPY_CLASS = "24软件1班"
SELF_CLASS = "24电子1班"


@pytest.fixture()
def env(monkeypatch):
    """构造一个只有两条排课记录的世界，语义完全可控。"""
    db = ScheduleDatabase()
    db.entries = [
        # 周一 567 / A101：被别人占着 → 应进 affected_rooms + affected_classes + affected_teachers
        ScheduleEntry(WEEK, "周一", "567", OCCUPY_CLASS, "1组",
                      "扭摆法测转动惯量", "A101", OCCUPY_TEACHER),
        # 周二 567 / A102：被调课教师**本人**有课 → 属于自身冲突，不是"受影响教师"
        ScheduleEntry(WEEK, "周二", "567", SELF_CLASS, "2组",
                      "示波器的使用", "A102", MY_NAME),
    ]
    db.loaded = True

    agent = RescheduleAgent(db)
    # 只替换模块级单例，不动 _ensure_loaded：让「数据已加载」这条真实分支参与测试
    monkeypatch.setattr(api, "_db", db)
    monkeypatch.setattr(api, "_agent", agent)
    return db, agent


def _run_impact() -> dict:
    req = api.ChatRequest(message=f"我是{MY_NAME}，把大学物理实验调到第{WEEK}周")
    out = asyncio.run(api.impact_analysis(req))
    assert out["success"] is True
    assert out["target_week"] == WEEK, "周次解析应命中第3周"
    return out["data"]


def test_affected_teachers_are_real_teachers_not_class_names(env):
    """核心回归：教室占用方的教师进「相关教师」，班级名绝不越界。"""
    data = _run_impact()
    assert OCCUPY_TEACHER in data["affected_teachers"], "占着教室的教师应出现在相关教师里"
    assert OCCUPY_CLASS not in data["affected_teachers"], \
        "班级名混进了 affected_teachers —— 说明又回去从 reason 文案反解了"
    assert OCCUPY_CLASS in data["affected_classes"]


def test_self_busy_is_its_own_bucket(env):
    """本人该时段已有课 = 自身冲突，不是"别人受影响"。"""
    data = _run_impact()
    assert data["self_busy_count"] == 1
    slot = data["self_busy_slots"][0]
    assert slot["weekday"] == "周二" and slot["time_slot"] == "567"
    assert slot["class_name"] == SELF_CLASS
    assert SELF_CLASS not in data["affected_classes"], "自身冲突的班级不该算作受影响班级"
    assert MY_NAME not in data["affected_teachers"], "自己不该出现在受影响教师里"


def test_reason_text_names_the_teacher(env):
    """teacher_busy 的文案里也要有人名 —— 原文案只写"教师该时段已有课"，看不出是谁。"""
    _, agent = env
    result = agent.find_slots_with_reasons(WEEK, teacher_name=MY_NAME)
    teacher_busy = [e for e in result["excluded"] if e["reason_type"] == "teacher_busy"]
    assert teacher_busy, "第3周周二567应被判为教师自身冲突"
    assert MY_NAME in teacher_busy[0]["reason"]
    # 结构化字段必须存在，供下游聚合（不要再从 reason 反解）
    assert teacher_busy[0]["teacher"] == MY_NAME
    assert teacher_busy[0]["class_name"] == SELF_CLASS


def test_room_conflict_reason_keeps_structured_fields(env):
    """教室冲突同时给结构化字段与中文原因，两者不冲突。"""
    _, agent = env
    result = agent.find_slots_with_reasons(WEEK, teacher_name=MY_NAME)
    room_busy = [e for e in result["excluded"] if e["reason_type"] == "classroom_busy"]
    assert room_busy
    e = room_busy[0]
    assert e["room"] == "A101"
    assert e["class_name"] == OCCUPY_CLASS
    assert e["teacher"] == OCCUPY_TEACHER
    assert OCCUPY_CLASS in e["reason"]
