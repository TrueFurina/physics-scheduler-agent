"""教师名解析的边界（R20 走完整用户流程时发现）。

背景：真实教师名有「教师H」「教师K」这类**中英文混合**的，
而原先的正则只匹配中文 `[\u4e00-\u9fff]{2,4}`：

    "我是教师H，把…调到第2周"  →  抓到 "教师"

"教师" 是所有教师名的子串，于是 `teacher_occupied` 把**别的教师**的课
也记成了"您自己的课"，沙盘显示「您（教师K）该时段已有课」，而真实原因是
教室被占。实测：教师H 调课，周一 567 被误标为 teacher_busy。

修复后要求：
  1. 中英文混合名能完整识别（教师H）
  2. 返回名单里的**原样大小写**（函数开头有 text.lower()，不能返回 teacherh）
  3. 纯中文名不受影响（吴琳）
  4. 前缀不能被当成完整姓名（"教师" → 补全为名单里的具体教师）
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.agent.reschedule_agent import RescheduleAgent
from app.utils.excel_parser import ScheduleDatabase

REAL = os.path.join(os.path.dirname(__file__), "..", "..", "schedule.demo.xlsx")


@pytest.fixture(scope="module")
def agent():
    if not os.path.isfile(REAL):
        pytest.skip("缺少 schedule.demo.xlsx")
    db = ScheduleDatabase()
    db.load_from_excel(REAL)
    return RescheduleAgent(db)


@pytest.mark.parametrize("msg,expect", [
    ("我是教师H，把扭摆法测物体转动惯量调到第2周", "教师H"),
    ("我是教师K", "教师K"),
    ("我是吴琳，把声速测量调到第2周", "吴琳"),
])
def test_teacher_name_parsed_exactly(agent, msg, expect):
    p = agent.parse_request(msg)
    assert p.get("teacher_name") == expect, (
        f"{msg!r} 应解析出 {expect!r}，实际 {p.get('teacher_name')!r}"
    )


def test_mixed_case_name_keeps_original_case(agent):
    """函数开头 text.lower() 会让 '教师H' 变小写，返回值必须是名单里的原样。"""
    p = agent.parse_request("我是教师H")
    name = p.get("teacher_name")
    assert name is not None
    assert name != name.lower(), f"大小写丢失：{name!r}"
    assert name in agent.db.get_all_teachers(), f"{name!r} 不在教师名单里"


def test_bare_prefix_is_not_treated_as_full_name(agent):
    """只说「我是教师」时，不能把它当成一个叫「教师」的人。"""
    p = agent.parse_request("我是教师，把课调到第2周")
    name = p.get("teacher_name")
    # 要么解析出名单里的具体教师，要么为 None（触发追问），
    # 绝不能是 "教师" 这个前缀本身
    assert name != "教师", "'教师' 是前缀，不能当作完整姓名"
