"""冲突筛选核心逻辑 pytest 单测

覆盖：周次解析、时间归一化、冲突筛选（教室/教师）、
      自然语言解析（含中文数字）、申请单审批流
"""

import os
import sys
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.utils.excel_parser import ScheduleDatabase
from app.agent.reschedule_agent import RescheduleAgent

# 测试数据路径（项目根目录 schedule.xlsx）
SCHEDULE_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "schedule.xlsx")


@pytest.fixture(scope="module")
def agent():
    """加载真实实验安排表，构建调课智能体"""
    db = ScheduleDatabase()
    db.load_from_excel(SCHEDULE_PATH)
    return RescheduleAgent(db)


# ---------- 1. 周次解析 ----------

def test_parse_standard_next_week(agent):
    """标准话术：第六周的课上不了，调到下周 → 第7周"""
    r = agent.parse_request("我是吴琳，第六周的课上不了，调到下周")
    assert r["source_week"] == 6
    assert r["target_week"] == 7
    assert r["teacher_name"] == "吴琳"


def test_parse_direct_week(agent):
    """直接指定：调到第5周 → 第5周"""
    r = agent.parse_request("把实验课调到第5周")
    assert r["target_week"] == 5


def test_parse_chinese_number(agent):
    """中文数字：调到第十周 → 第10周"""
    r = agent.parse_request("调到第十周")
    assert r["target_week"] == 10


def test_parse_plain_next_week(agent):
    """无源周的"下周" → 当前周+1"""
    r = agent.parse_request("下周调课")
    assert r["target_week"] is not None


def test_parse_no_week(agent):
    """无周次信息 → target_week 为 None"""
    r = agent.parse_request("我想调个课")
    assert r["target_week"] is None


# ---------- 1b. 周次解析回归（2026-10-15 修的两个 bug） ----------

def test_parse_direct_week_when_db_not_loaded():
    """数据未加载时也必须能把"第N周"解析出来。

    回归：`get_week_range()` 在未加载时返回 `(0, 0)`，而**元组 (0,0) 是真值**，
    所以原来的 `min(target_week, week_range[1])` 会把目标周次夹成 0；
    0 是假值，下游就当成"没解析出周次"→ 白调一次 LLM（实测每次约 5 秒）
    并回一句"请补充信息"，正确答复应是"排课数据尚未加载"。
    """
    agent_empty = RescheduleAgent(ScheduleDatabase())  # 刻意不 load
    assert agent_empty.parse_request("第六周")["target_week"] == 6
    assert agent_empty.parse_request("调到第十周")["target_week"] == 10


def test_parse_week_after_next(agent):
    """'下下周' 应为 +2，而不是 +1。

    回归：'下下周' 包含子串 '下周'，原实现先判 '下周' → 命中 +1 分支，
    于是 '下下周' 的 +2 分支永远不可达（死代码）。
    """
    r = agent.parse_request("第六周的课要下下周上")
    assert r["source_week"] == 6
    assert r["target_week"] == 8


# ---------- 2. 时间归一化 ----------

def test_time_normalization(agent):
    """Excel 中的时间格式应归一化为节次标记（含 MORNING 上午标记）"""
    # 所有排课记录的时间段都应是标准归一化标记
    # 567/678/91011/78 = 下午/晚上实验时段；MORNING = 上午课程（实验课不排上午，筛选时忽略）
    valid_slots = {"567", "678", "91011", "78", "MORNING"}
    for e in agent.db.entries:
        assert e.time_slot in valid_slots, f"时间格式未归一化: {e.time_slot}"


# ---------- 3. 冲突筛选 ----------

def test_classroom_conflict(agent):
    """教室占用冲突应被标记并给出原因"""
    result = agent.find_slots_with_reasons(5)
    classroom_excluded = [e for e in result["excluded"] if e["reason_type"] == "classroom_busy"]
    assert len(classroom_excluded) > 0, "应检测到教室占用冲突"
    assert all("占用" in e["reason"] for e in classroom_excluded), "教室冲突原因应包含占用信息"


def test_teacher_busy_conflict(agent):
    """教师自身冲突应被筛掉（林珠云第5周有课）"""
    result = agent.find_slots_with_reasons(5, teacher_name="林珠云")
    teacher_excluded = [e for e in result["excluded"] if e["reason_type"] == "teacher_busy"]
    assert len(teacher_excluded) > 0, "教师自身冲突时段应被筛掉"


def test_available_slots_exist(agent):
    """第5周应有可用时段"""
    result = agent.find_slots_with_reasons(5)
    assert len(result["available"]) > 0, "第5周应有可用时段"


def test_available_slots_no_conflict(agent):
    """可用时段应无冲突：抽查20个可用时段人工核对"""
    result = agent.find_slots_with_reasons(5)
    available = result["available"][:20]
    occupied = set()
    for e in agent.db.entries:
        if e.week == 5:
            occupied.add(f"{e.weekday}_{e.time_slot}_{e.room}")
    for s in available:
        key = f"{s['weekday']}_{s['time_slot']}_{s['room']}"
        assert key not in occupied, f"可用时段存在冲突: {key}"


# ---------- 4. 完整调课流程 ----------

def test_process_request_full(agent):
    """完整调课流程：返回可用 + 被排除 + 候选方案"""
    r = agent.process_request("我是吴琳，第六周的课上不了，调到下周")
    assert r["success"] is True
    assert r["target_week"] == 7
    assert len(r["available_slots"]) > 0
    assert "excluded_slots" in r
    assert "ai_plans" in r  # B3 候选方案


def test_process_request_missing_info(agent):
    """信息缺失时应触发追问"""
    r = agent.process_request("我想调个课")
    assert r["success"] is False
    assert r.get("need_info") is True


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
