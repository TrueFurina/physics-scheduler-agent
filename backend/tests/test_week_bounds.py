"""周次范围校验（R-32）—— 范围外不许出"看起来全可用"的沙盘

历史缺陷（实测，非推演）
--------------------
真实数据覆盖第 1–16 周。同一位老师、同一门课：

    输入「我是吴琳，把声速测量调到第2周」→ 118 个可用 / 32 个被排除
    输入「…调到第7周」                →  78 个可用 / 36 个被排除
    输入「…调到第-1周」               → 150 个可用 /  0 个被排除   ← 满屏绿格

最后一行不是"负数的周特别空"，而是**三重冲突检查全部空转**：
教室/教师占用按 `e.week == target_week` 过滤、学生课表按周查，
周次落在表外就一条也查不到 ⇒ 排除列表为空 ⇒ 界面显示"全是空闲"。
教秘若信了，选中的时段极可能撞课，而系统刚刚才对他宣称做过三重检查。

成因是两条解析路径待遇不同：规则解析有 `min(target_week, 上界)` 夹取，
**LLM 解析出来的 target_week 完全不受约束**（负数正是这样进来的，线上带 key 时必现）。
静默夹取同样有害：他说第 17 周，沙盘画第 16 周，他很可能没注意就提交了。

现在统一语义：
1. 两条路径汇合后统一校验，范围外 → `success=False` + 明说覆盖范围；
2. 拒绝时**不把非法周次写进 context**，否则下一轮会合并回来，改口也救不回；
3. 影响分析同样拒绝 —— 那里的"冲突数 0"会被读成"这周没影响"，比报错更危险。
"""

import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import app.api.reschedule as api
from app.agent.reschedule_agent import RescheduleAgent
from app.utils.excel_parser import ScheduleDatabase, ScheduleEntry

MY_NAME = "吴琳"
COURSE = "声速测量"
LO, HI = 1, 16


def _db() -> ScheduleDatabase:
    """覆盖第 1–16 周的最小世界：两端各一条记录，周次区间就是 (1, 16)。"""
    db = ScheduleDatabase()
    db.entries = [
        ScheduleEntry(1, "周一", "567", "24电子1班", "1组", COURSE, "A101", MY_NAME),
        ScheduleEntry(8, "周三", "567", "24电子2班", "2组", COURSE, "A102", "陈老师"),
        ScheduleEntry(HI, "周五", "91011", "24电子3班", "3组", COURSE, "A103", "陈老师"),
    ]
    db.loaded = True
    return db


class _FakeLlmParser:
    """模拟 LLM 解析器：`process_request` 只在规则拿不到周次时才用它。"""

    def __init__(self, payload: dict):
        self.payload = payload
        self.calls = 0
        # 生成 AI 候选方案那一步会看这个开关；这里只需要"解析"能力
        self.configured = False

    def parse(self, text):                      # noqa: D102 - 玩具实现
        self.calls += 1
        return dict(self.payload)


@pytest.fixture()
def agent(monkeypatch):
    db = _db()
    a = RescheduleAgent(db)
    monkeypatch.setattr(api, "_db", db)
    monkeypatch.setattr(api, "_agent", a)
    return a


def _ask(a: RescheduleAgent, text: str, context=None) -> dict:
    return a.process_request(text, True, context)


# ---------- 1. 拒绝：范围外 ----------

def test_negative_week_from_llm_is_refused(agent):
    """线上实测的原始成因：规则解析不出周次 → LLM 给了 -1 → 150 个"可用"。

    `LLM_DISABLED=1` 的测试环境里规则路径不会失败，所以这里直接注入一个
    返回 -1 的解析器，复现的是**同一个入口**。
    """
    agent.llm_parser = _FakeLlmParser({"target_week": -1})

    r = _ask(agent, f"把{COURSE}调一下")

    assert r["success"] is False
    assert r["available_slots"] == [], "范围外的周次绝不能给出时段（那是没做冲突检查的结果）"
    assert r["excluded_slots"] == []
    assert r["target_week"] is None
    assert f"第{LO}–{HI}周" in r["message"], "必须把实际覆盖范围说清楚"


def test_week_above_range_from_llm_is_refused_not_clamped(agent):
    """LLM 给 99：不许再静默夹成 16（夹取等于替教秘改口）。"""
    agent.llm_parser = _FakeLlmParser({"target_week": 99})

    r = _ask(agent, f"把{COURSE}调一下")

    assert r["success"] is False
    assert "99" in r["message"]
    assert r["target_week"] is None


def test_rules_path_above_range_is_refused_too(agent):
    """规则能解析的越界值（第99周）走同一条拒绝路径 —— 两条解析路径语义必须一致。"""
    r = _ask(agent, f"我是{MY_NAME}，把{COURSE}调到第99周")

    assert r["success"] is False
    assert r["available_slots"] == []
    assert "99" in r["message"]
    # 同理：越界的"源周次"也不许留在上下文里，否则下一轮「下周」会算成第 100 周
    assert "source_week" not in (r.get("context") or {})


def test_refusal_does_not_echo_illegal_week_into_context(agent):
    """非法周次不能随 context 回传，否则下一轮合并回来，教秘改口也救不回来。"""
    agent.llm_parser = _FakeLlmParser({"target_week": -1})
    r1 = _ask(agent, f"把{COURSE}调一下", {"teacher_name": MY_NAME})

    assert "target_week" not in (r1.get("context") or {}), "非法值被写回上下文 = 死循环"

    # 教秘改口说第 5 周 → 必须能正常出结果
    r2 = _ask(agent, "第5周", r1["context"])
    assert r2.get("need_info") is not True
    assert r2["target_week"] == 5
    assert r2["available_slots"], "改到合法周次后应当能算出时段"


def test_illegal_week_in_incoming_context_is_also_rejected(agent):
    """前端把历史 context 原样带回时，非法值同样要被拦（不能只防 LLM）。

    只是合并规则是"本轮说到的覆盖历史"，所以这一轮不能自己再说一个合法周次
    ——否则历史值根本不会被用到。
    """
    r = _ask(agent, f"查一下{COURSE}的空闲时段", {"teacher_name": MY_NAME, "target_week": -3})

    assert r["success"] is False
    assert r["available_slots"] == []
    assert "-3" in r["message"]


# ---------- 2. 不回归：合法周次照常工作 ----------

@pytest.mark.parametrize("week", [LO, 5, HI])
def test_in_range_weeks_still_work(agent, week):
    r = _ask(agent, f"我是{MY_NAME}，把{COURSE}调到第{week}周")

    assert r["success"] is True
    assert r["target_week"] == week
    assert r["available_slots"], "合法周次必须仍能算出时段"


def test_boundaries_are_inclusive(agent):
    """1 和 16 都在表内，不许被"范围外"误伤。"""
    for w in (LO, HI):
        r = _ask(agent, f"我是{MY_NAME}，把{COURSE}调到第{w}周")
        assert r["success"] is True, f"第{w}周明明在表内却被拒绝"


def test_unloaded_db_is_not_treated_as_range_00(agent, monkeypatch):
    """未加载时 `get_week_range()` 返回 (0,0)：不许把它当成"有效区间 0–0"。"""
    empty = ScheduleDatabase()          # loaded=False, entries=[]
    a = RescheduleAgent(empty)
    monkeypatch.setattr(api, "_db", empty)
    monkeypatch.setattr(api, "_agent", a)

    assert a.week_bounds() is None

    r = a.process_request("我是吴琳，把声速测量调到第6周", True, None)
    assert r["success"] is False
    assert "尚未加载" in r["message"], "此时该说「数据没加载」，而不是「第6周超出范围」"


# ---------- 3. 影响分析同样拒绝 ----------

def test_impact_analysis_rejects_out_of_range_week(agent, monkeypatch):
    """「冲突数 = 0」会被读成"这周没影响"，所以这里必须 400 而不是返回空结果。"""
    monkeypatch.setattr(agent, "parse_request",
                        lambda text: {"target_week": -1, "teacher_name": MY_NAME})

    with pytest.raises(Exception) as ei:
        asyncio.run(api.impact_analysis(api.ChatRequest(
            message="我是吴琳，把声速测量调到第-1周", fast=True,
        )))

    detail = str(getattr(ei.value, "detail", ei.value))
    assert "-1" in detail
    assert f"{LO}–{HI}" in detail
