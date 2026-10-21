"""多轮追问（B2）语义回归 —— 追问不许把刚答过的信息再问一遍

历史缺陷（实测，非推演）
--------------------
`/chat` 每轮都从零解析，服务端不保留任何上下文。于是：

    教秘：我要把实验课往后挪一周
    系统：需要补充信息 • 请问是哪位老师 • 原定哪一周
    教秘：我是吴琳                      ← 照着提示回答的
    系统：需要补充信息 • 请问您需要调哪周的课 • 您需要调整到哪一周 • 您是哪位老师
                                          ↑ 刚说过的老师又被问一遍

界面上还写着"请在上方输入框补充回答，我会继续为您处理"，等于承诺了一个不存在的功能。

这里锁死的语义：
1. 已确认的槽位（`context`）必须参与下一轮合并，**已知的就不再问**；
2. 追问由槽位驱动生成，不直接复用 LLM 文案（LLM 会给出"调哪周"和"调到哪一周"这种
   同义重复，实测三条问题里两条是同一个意思）；
3. 教师是**必需槽位**：缺教师就直出时段，等于跳过"教师本人该时段有没有课"这一重
   检查，会把可能撞课的时段推荐给教秘（实测直接返回 94 个）。宁可多问一句；
4. 课程名要能识别，且**长名优先**——"分光计测量光栅常数及光波波长"不能被
   "分光计的调节"这类短名先截走。

数据是构造的（一份可控 db），不依赖 schedule.xlsx：换真实表/脱敏表都不会让断言漂移。
"""

import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import app.api.reschedule as api
from app.agent.reschedule_agent import RescheduleAgent
from app.agent.student_schedule import StudentSchedule
from app.utils.excel_parser import ScheduleDatabase, ScheduleEntry

WEEK = 3
MY_NAME = "吴琳"
LONG_COURSE = "分光计测量光栅常数及光波波长"
SHORT_COURSE = "分光计的调节"   # LONG_COURSE 不含它，但两者共享前缀，用来验证"长名优先"


@pytest.fixture()
def agent(monkeypatch):
    """一个只有几门课的可控世界：两门是同一教师共享前缀的课程，另有一门同名跨班。"""
    db = ScheduleDatabase()
    db.entries = [
        ScheduleEntry(WEEK, "周一", "567", "24电子1班", "1组", SHORT_COURSE, "A101", MY_NAME),
        ScheduleEntry(WEEK, "周二", "678", "24电子2班", "2组", LONG_COURSE, "A102", MY_NAME),
        ScheduleEntry(WEEK, "周三", "567", "24软件1班", "3组", "声速测量", "A103", "陈老师"),
        # 同名跨班：course_name 相同但班级不同，回找课程卡片时必须靠班级消歧
        ScheduleEntry(WEEK, "周四", "567", "24软件2班", "4组", "声速测量", "A104", MY_NAME),
    ]
    db.loaded = True

    student = StudentSchedule()
    student.students = [{
        "name": "测试学生", "student_id": "000",
        "courses": [("高等数学A2", "周五", "5-7节", "1-16周")],
    }]
    student.courses = student.students[0]["courses"]

    a = RescheduleAgent(db, student=student)
    monkeypatch.setattr(api, "_db", db)
    monkeypatch.setattr(api, "_student", student)
    monkeypatch.setattr(api, "_agent", a)
    return a


def test_turn1_only_asks_for_the_missing_slot(agent):
    """界面上已经选了身份 ⇒ 教师槽位一开始就有，追问里不许再问老师。"""
    r = agent.process_request("帮我把课调一下", True, {"teacher_name": MY_NAME})

    assert r["need_info"] is True
    assert r["context"] == {"teacher_name": MY_NAME}, "已确认的槽位必须回传，否则下一轮无从合并"
    joined = " ".join(r["followup_questions"])
    assert "周" in joined, "缺周次就该问周次"
    assert "老师" not in joined, "教师已知却还在问「是哪位老师」—— 多轮变成了复读"


def test_turn2_answer_completes_without_repeating_teacher(agent):
    """照提示回答一句"第3周"，整件事就该办完（而不是再问一遍老师）。"""
    r1 = agent.process_request("帮我把课调一下", True, {"teacher_name": MY_NAME})
    r2 = agent.process_request("第3周", True, r1["context"])

    assert r2.get("need_info") is not True
    assert r2["target_week"] == WEEK
    assert len(r2["available_slots"]) > 0
    assert r2["context"]["teacher_name"] == MY_NAME, "上下文里的教师不能因为这一句没提就丢掉"


def test_teacher_is_a_required_slot(agent):
    """反证：没有教师就不许出结果 —— 否则"教师自身冲突"这一重检查被静默跳过。"""
    r = agent.process_request(f"调到第{WEEK}周", True, None)

    assert r.get("need_info") is True
    assert any("老师" in q for q in r["followup_questions"])


def test_course_name_prefers_longest_match(agent):
    """长名优先：同一教师名下的两门课共享前缀时，别把短名当答案。"""
    r = agent.process_request(f"把{LONG_COURSE}调到第{WEEK}周", True, {"teacher_name": MY_NAME})
    assert r["context"]["course_name"] == LONG_COURSE

    r2 = agent.process_request(f"把{SHORT_COURSE}调到第{WEEK}周", True, {"teacher_name": MY_NAME})
    assert r2["context"]["course_name"] == SHORT_COURSE


def test_course_in_two_classes_needs_class_to_disambiguate(agent):
    """同名课跨班：课程名能认出来，但要靠班级才能唯一确定是哪一张课程卡片。

    前端拿 `course_name + class_name` 回找课程；只剩课程名时**不猜**——
    猜错的代价是把 A 班的课按 B 班提交上去。
    """
    r = agent.process_request(f"把声速测量调到第{WEEK}周", True, {"teacher_name": MY_NAME})
    assert r["context"]["course_name"] == "声速测量"
    assert not r["context"].get("class_name"), "没提班级就不该臆造一个"

    r2 = agent.process_request(
        f"把24软件2班的声速测量调到第{WEEK}周", True, {"teacher_name": MY_NAME})
    assert r2["context"]["class_name"] == "24软件2班"


def test_context_survives_the_api_boundary(agent):
    """接口层也要真的把 context 传进去、再把合并结果传出来。

    这一条防的是另一类回归：`run_in_threadpool` 的实参顺序被改成关键字/位置混用
    而没人发现（`process_request` 的第三个位置参数就是 context）。
    """
    out = asyncio.run(api.reschedule_chat(api.ChatRequest(
        message=f"第{WEEK}周", fast=True, context={"teacher_name": MY_NAME},
    )))

    assert out.data["target_week"] == WEEK
    assert out.data["context"]["teacher_name"] == MY_NAME
    assert len(out.data["available_slots"]) > 0


def test_no_context_means_no_merge(agent):
    """不带 context 就必须当作全新会话：不能凭空冒出上一个教秘的教师名。"""
    out = asyncio.run(api.reschedule_chat(api.ChatRequest(
        message=f"第{WEEK}周", fast=True, context=None,
    )))

    assert out.data.get("need_info") is True
    assert "teacher_name" not in (out.data.get("context") or {})
