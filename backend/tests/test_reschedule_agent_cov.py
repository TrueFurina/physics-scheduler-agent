"""reschedule_agent 长尾覆盖补测（R-104）

覆盖率探针定位的 26 行未覆盖，按价值挑出可证契约的：
  ▸ _cn_to_int 96-103（中文数字「十一/二十/二十三…」各分支，静默错周次的高危点）
  ▸ week_bounds 116（未加载 ⇒ None，下游 `if week_bounds:` 才能正确失败）
  ▸ find_slots_with_reasons 267（未加载 ⇒ 空，不能抛）
  ▸ week_to_date 58-59 / _get_semester_start 84-85（日期解析异常的兜底，不能把异常漏给教秘）
  ▸ parse_request 223-224（"我是/我叫"正则没抓住时，子串兜底仍能认出教师）
  ▸ process_request 561（数据未加载时明确报错，而不是空跑）
  ▸ _need_info 415（槽位都齐了、但有 LLM 追问时的兜底文案）
  ▸ process_request 466 / 469（规则解析失败 → LLM 兜底解析周次 / LLM 追问）
  ▸ process_request 601-611（AI 候选方案生成，需 llm_parser.configured）

全部用合成小表 + FakeLLM，不依赖 schedule.xlsx，干净检出也能跑。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.agent import reschedule_agent as ra
from app.agent.reschedule_agent import RescheduleAgent
from app.utils.excel_parser import ScheduleDatabase, ScheduleEntry


def _entry(week, weekday, slot, cls, group, exp, room, teacher):
    return ScheduleEntry(week, weekday, slot, cls, group, exp, room, teacher)


def _loaded_db(entries):
    db = ScheduleDatabase()
    db.entries = entries
    db.loaded = True
    return db


def _widened_db(entries):
    """撑开周次范围到 1–16，避免 target_week 被范围校验以「超出表范围」拒绝。

    真实表覆盖第 1–16 周；合成小表只有一两条记录时 get_week_range 会缩成单周，
    导致「调到第3/5周」被误判为越界。这里给「别的班」补两条边界记录把范围撑开。
    """
    base = [
        ScheduleEntry(1, "周一", "567", "别的班", "1组", "E1", "B201", "别人"),
        ScheduleEntry(16, "周一", "567", "别的班", "1组", "E16", "B201", "别人"),
    ]
    return _loaded_db(entries + base)


class FakeLLM:
    """不联网的 LLM 替身，只实现 RescheduleAgent 实际调用的两个方法。"""

    def __init__(self, configured=True, parse_result=None, plans=None):
        self.configured = configured
        self._parse = parse_result or {
            "error": None, "target_week": None, "source_week": None,
            "teacher": None, "course": None, "needs_followup": False,
        }
        self._plans = plans or [{
            "weekday": "周四", "time_slot": "567", "room": "A410",
            "reason": "系统推荐：该时段教室空闲",
        }]

    def parse(self, text):
        return dict(self._parse)

    def suggest_plans(self, teacher, course, target_week, available_slots):
        return {"plans": list(self._plans)}


class RaisingLLM(FakeLLM):
    """configured=True 但 suggest_plans 直接抛异常，用于验证 AI 候选生成的降级路径。"""

    def suggest_plans(self, teacher, course, target_week, available_slots):
        raise RuntimeError("LLM 服务挂了")


# ---------------------------------------------------------------------------
# 中文数字解析：每个分支都要守住，否则「二十」被读成 2 会悄无声息错一周
# ---------------------------------------------------------------------------

def test_cn_to_int_all_branches():
    agent = RescheduleAgent(ScheduleDatabase())
    cases = [
        ("十", 10), ("十一", 11), ("十二", 12),
        ("二十", 20), ("二十一", 21), ("二十三", 23),
        ("三十", 30), ("三十五", 35),
        ("一", 1), ("九", 9),
        ("XYZ", 0), ("", 0),
    ]
    for s, expect in cases:
        assert agent._cn_to_int(s) == expect, f"_cn_to_int({s!r}) 应为 {expect}"


# ---------------------------------------------------------------------------
# 未加载时的两个失败语义：必须显式返回 None / 空，不能抛
# ---------------------------------------------------------------------------

def test_week_bounds_none_when_unloaded():
    agent = RescheduleAgent(ScheduleDatabase())  # 未 load
    assert agent.week_bounds() is None


def test_find_slots_empty_when_unloaded():
    agent = RescheduleAgent(ScheduleDatabase())
    assert agent.find_slots_with_reasons(3) == {"available": [], "excluded": []}


# ---------------------------------------------------------------------------
# 日期解析异常兜底：异常必须被吞掉返回空串/默认值，不能漏给教秘
# ---------------------------------------------------------------------------

def test_week_to_date_swallows_bad_input(monkeypatch):
    monkeypatch.setattr(ra, "SEMESTER_START", None)  # 制造 base=None 触发算术异常
    assert ra.week_to_date(5, "周四") == ""


def test_semester_start_fallback_on_exception(monkeypatch):
    db = ScheduleDatabase()
    db.entries = [_entry(2, "周四", "567", "C", "1组", "E1", "A410", "吴琳")]
    db.loaded = True

    def _boom():
        raise RuntimeError("日期列读不出来")

    monkeypatch.setattr(db, "get_semester_start_date", _boom)
    agent = RescheduleAgent(db)
    # 异常被吞，回退到默认学期起始日，而不是把 RuntimeError 抛给调用方
    assert agent._get_semester_start() == ra.SEMESTER_START


# ---------------------------------------------------------------------------
# parse_request：正则没抓住教师时，子串兜底仍能认出
# ---------------------------------------------------------------------------

def test_parse_teacher_by_substring_fallback():
    """文本没有「我是/我叫」、教师名却是正文一部分时，子串兜底要能认出教师。"""
    db = _loaded_db([_entry(2, "周四", "567", "C", "1组", "E1", "A410", "吴琳")])
    agent = RescheduleAgent(db)
    parsed = agent.parse_request("吴琳调课到第3周")
    assert parsed["teacher_name"] == "吴琳"
    assert parsed["target_week"] == 3


# ---------------------------------------------------------------------------
# process_request：数据未加载必须明确报错（561），不能空跑或 500
# ---------------------------------------------------------------------------

def test_process_request_reports_not_loaded():
    db = _loaded_db([_entry(2, "周四", "567", "C", "1组", "E1", "A410", "吴琳")])
    db.loaded = False  # 模拟进程被回收后 loaded 标志丢失、但内存里还有数据
    agent = RescheduleAgent(db)
    r = agent.process_request("我是吴琳，把课调到第3周")
    assert r["success"] is False
    assert "尚未加载" in r["message"]


# ---------------------------------------------------------------------------
# _need_info：槽位都齐了但 LLM 仍追问时，用 LLM 的问题兜底（415）
# ---------------------------------------------------------------------------

def test_need_info_carries_llm_questions():
    agent = RescheduleAgent(ScheduleDatabase())
    r = agent._need_info(
        {"teacher_name": "吴琳", "target_week": 3, "course_name": "E1"},
        ["请确认具体实验项目", "请选择场地"],
    )
    assert r["need_info"] is True
    assert r["followup_questions"] == ["请确认具体实验项目", "请选择场地"]
    # 课程名字段必须透传，下游靠它区分契约版本
    assert r["course_name"] == "E1"
    assert r["course_unrecognized"] is False


# ---------------------------------------------------------------------------
# process_request：规则解析失败 → LLM 兜底（466 解析出周次 / 469 LLM 追问）
# ---------------------------------------------------------------------------

def test_llm_fallback_sets_target_week():
    """规则没解析出周次时，LLM 给出的周次要被并进来（466）。"""
    db = _widened_db([_entry(2, "周四", "567", "C", "1组", "E1", "A410", "吴琳")])
    llm = FakeLLM(configured=False, parse_result={
        "error": None, "target_week": 5, "needs_followup": False,
    })
    agent = RescheduleAgent(db, llm_parser=llm)
    r = agent.process_request("我是吴琳，调个课")  # 没有「第N周」
    assert r["target_week"] == 5
    assert r["success"] is True


def test_llm_followup_returns_need_info():
    """LLM 说需要追问且缺周次/教师时，直接转追问分支（469）。

    注：到达此分支时必缺槽位，_need_info 内部会用槽位缺省问题覆盖 LLM 的
    followup_questions（设计取向：槽位驱动的问题更稳）。本测试只验证「走了追问分支」，
    不验证具体文案——具体问什么由槽位决定。
    """
    db = _widened_db([_entry(2, "周四", "567", "C", "1组", "E1", "A410", "吴琳")])
    llm = FakeLLM(configured=False, parse_result={
        "error": None, "target_week": None, "needs_followup": True,
        "followup_questions": ["请说明要调哪一周"],
    })
    agent = RescheduleAgent(db, llm_parser=llm)
    r = agent.process_request("我是吴琳，调个课")
    assert r["success"] is False
    assert r["need_info"] is True
    assert len(r["followup_questions"]) >= 1


# ---------------------------------------------------------------------------
# process_request：AI 候选方案生成（601-611）
# ---------------------------------------------------------------------------

def test_process_request_generates_ai_plans():
    """with_ai=True 且 llm_parser.configured 时，必须真的去生成候选方案。"""
    # 教师在目标周（第3周）没有课 ⇒ 可用时段非空，AI 才有素材
    db = _widened_db([_entry(2, "周四", "567", "C", "1组", "E1", "A410", "吴琳")])
    llm = FakeLLM(configured=True, plans=[
        {"weekday": "周四", "time_slot": "567", "room": "A410", "reason": "与原课时段接近"},
    ])
    agent = RescheduleAgent(db, llm_parser=llm)
    r = agent.process_request("我是吴琳，把课调到第3周")
    assert r["success"] is True
    assert len(r["ai_plans"]) >= 1
    assert r["ai_plans"][0]["reason"] == "与原课时段接近"


# ---------------------------------------------------------------------------
# week_bounds：已加载但周次上界为 0 ⇒ 必须返回 None（116）
# （test_week_bounds_none_when_unloaded 只覆盖「未加载」分支 113，这里补「加载了但无有效周次」）
# ---------------------------------------------------------------------------

def test_week_bounds_none_when_loaded_but_empty_range():
    db = ScheduleDatabase()
    db.loaded = True
    db.entries = []           # 已加载但没有任何记录 ⇒ get_week_range 返回 (0, 0) ⇒ hi=0
    agent = RescheduleAgent(db)
    # 走到 116：if not hi: return None —— 否则下游 `if week_bounds:` 会把 (0,0) 当真范围
    assert agent.week_bounds() is None


# ---------------------------------------------------------------------------
# process_request：规则没解析出课程，但 LLM 给了 ⇒ 必须被并进来（466）
# ---------------------------------------------------------------------------

def test_llm_fallback_merges_course_field():
    """规则解析不出课程名、周次也没提，但 LLM 的 parse 补回了 course ⇒ 465-466 的合并必须发生。"""
    db = _widened_db([_entry(2, "周四", "567", "C", "1组", "E1", "A410", "吴琳")])
    llm = FakeLLM(configured=False, parse_result={
        "error": None, "target_week": None, "source_week": None,
        "teacher": "吴琳", "course": "E2", "needs_followup": False,
    })
    agent = RescheduleAgent(db, llm_parser=llm)
    # 文本里没提课程，规则解析 course_name=None；LLM 给 course="E2" ⇒ 被并到 parsed
    r = agent.process_request("我是吴琳，调个课")
    # 466 把 course 合并进 parsed；最终回到 _need_info（缺周次）时 course_name 必须透传
    assert r.get("course_name") == "E2"


# ---------------------------------------------------------------------------
# process_request：已加载、槽位齐全，但目标周确实没有空闲时段 ⇒ 走零结果契约分支（561）
# ---------------------------------------------------------------------------

def test_process_request_no_slots_returns_consistent_contract(monkeypatch):
    db = _widened_db([_entry(2, "周四", "567", "C", "1组", "E1", "A410", "吴琳")])
    agent = RescheduleAgent(db)
    # 强制「没有空闲时段」，命中 if not slots:（554）→ 561 的零结果返回体
    monkeypatch.setattr(
        agent, "find_slots_with_reasons",
        lambda tw, teacher_name=None: {"available": [], "excluded": []},
    )
    r = agent.process_request("我是吴琳，把课调到第3周")
    assert r["success"] is True
    assert "没有空闲时段" in r["message"]
    assert r["total"] == 0
    # 零结果分支的字段集必须与正常分支一致（R31 接缝事故）
    assert "course_name" in r and "course_unrecognized" in r


# ---------------------------------------------------------------------------
# process_request：AI 候选生成时 LLM 抛异常 ⇒ 必须降级为空候选而不是把异常漏给教秘（610/611）
# ---------------------------------------------------------------------------

def test_ai_plan_generation_degrades_on_llm_error(monkeypatch):
    db = _widened_db([_entry(2, "周四", "567", "C", "1组", "E1", "A410", "吴琳")])
    llm = RaisingLLM(configured=True, plans=[{"weekday": "周四", "time_slot": "567",
                                             "room": "A410", "reason": "x"}])
    agent = RescheduleAgent(db, llm_parser=llm)
    # 给一个非空可用时段，确保越过 561 进入 AI 候选生成段；suggest_plans 会抛异常
    monkeypatch.setattr(
        agent, "find_slots_with_reasons",
        lambda tw, teacher_name=None: {
            "available": [{"weekday": "周四", "time_slot": "567", "room": "A410"}],
            "excluded": [],
        },
    )
    # 关键断言：LLM 异常被 610/611 兜住，process_request 仍正常返回（不抛、success=True）
    r = agent.process_request("我是吴琳，把课调到第3周", with_ai=True)
    assert r["success"] is True
    assert isinstance(r["ai_plans"], list)


# ---------------------------------------------------------------------------
# R-119 · 学期起始日的**来源**必须可判别
#
# 背景：`_get_semester_start()` 无法区分"从表头解析成功"和"解析失败后回退到
# 硬编码 SEMESTER_START = 2025-02-24" —— 两者返回同一个 date 对象。
# 后果不是周次错（周次/星期/教室/教师全都从表里读），而是界面每个可用时段
# 旁边那行小字日期错一整年而**毫无提示**。教秘核对"是不是这周"唯一的视觉
# 锚点就是那行小字。
# ---------------------------------------------------------------------------

def test_semester_state_reports_from_table_when_parsed():
    """表头解析成功时，必须明确说 from_table=True。"""
    db = ScheduleDatabase()
    db.loaded = True
    db.entries = []
    db.get_semester_start_date = lambda: ra.date(2026, 9, 7)

    st = RescheduleAgent(db).semester_start_state()
    assert st["from_table"] is True
    assert st["date"] == "2026-09-07"
    assert "note" not in st, "解析成功时不该出现警告文案"


def test_semester_state_flags_fallback():
    """← 核心：解析失败时必须自曝 from_table=False 并给出可显示的原因。

    这条守的是"静默降级"。若哪天有人把 fallback 改成静默返回默认日期，
    界面又会把 2025 年的日期当成真的显示出来。
    """
    db = ScheduleDatabase()
    db.loaded = True
    db.entries = []
    db.get_semester_start_date = lambda: None

    st = RescheduleAgent(db).semester_start_state()
    assert st["from_table"] is False, f"回退到默认值却仍声称来自表头：{st}"
    assert st["date"] == ra.SEMESTER_START.isoformat()
    assert st.get("note"), "回退时必须有可显示的原因"
    assert "可能不对" in st["note"], f"提示要说清后果：{st['note']}"


def test_semester_state_survives_exception():
    """解析抛异常时同样要降级 + 自曝，不能让异常漏到 /status。"""
    db = ScheduleDatabase()
    db.loaded = True
    db.entries = []

    def _boom():
        raise RuntimeError("表头读不出来")

    db.get_semester_start_date = _boom
    st = RescheduleAgent(db).semester_start_state()
    assert st["from_table"] is False
    assert "note" in st


def test_status_exposes_semester_state():
    """`/status` 必须带上 semester 字段（供界面判断日期可不可信）。"""
    from app.main import app
    from fastapi.testclient import TestClient

    body = TestClient(app).get("/api/reschedule/status").json()
    assert "semester" in body, f"/status 缺少 semester 字段：{list(body)}"
    # 未加载时为 None（没有表就没有起始日可言），加载后应是 dict
    assert body["semester"] is None or set(body["semester"]) & {"date", "from_table"}


def test_slots_omit_date_when_semester_start_untrusted():
    """← 起始日不可信时，时段**不带 date 字段**（而不是带一个错日期）。

    前端 4 处独立渲染 `slot.date`，靠 `{s.date && ...}` 天然跳过 undefined。
    这是最省事也最不会漏的约定：不需要每个渲染点都记得判断可信度。
    """
    db = ScheduleDatabase()
    db.loaded = True
    db.entries = [_entry(2, "周四", "567", "C", "1组", "E1", "A410", "吴琳")]
    db.get_semester_start_date = lambda: None
    agent = RescheduleAgent(db)

    got = agent.find_slots_with_reasons(3)
    slots = got["available"] + got["excluded"]
    assert slots, "夹具应当能产出至少一个时段（否则这条测试测不到东西）"
    for s in slots:
        assert s.get("date") is None, (
            f"起始日不可信却仍给了日期 {s.get('date')!r} —— 界面会显示错一年的日期"
        )


def test_slots_include_date_when_semester_start_trusted():
    """反向证明：起始日可信时**必须**有日期，别把功能一起关掉。"""
    db = ScheduleDatabase()
    db.loaded = True
    db.entries = [_entry(2, "周四", "567", "C", "1组", "E1", "A410", "吴琳")]
    db.get_semester_start_date = lambda: ra.date(2026, 9, 7)
    agent = RescheduleAgent(db)

    slots = agent.find_slots_with_reasons(3)
    all_slots = slots["available"] + slots["excluded"]
    dated = [s for s in all_slots if s.get("date")]
    assert dated, "起始日可信时应当带上日期"

    # ⚠️ 不能断言 `dated[0]`：`find_slots_with_reasons` 返回的是**全部七个星期**
    # 的时段（周一→周日），第一条是周一而不是夹具里那条周四的课。
    # 第一版就是这么写的，得到 09月21日（周一）然后以为公式错了。
    thu = [s for s in all_slots if s.get("weekday") == "周四" and s.get("date")]
    assert thu, f"没有周四的时段：{[(s.get('weekday'), s.get('date')) for s in all_slots[:5]]}"
    # 第 3 周周四 = 2026-09-07（周一）+ 14 天 + 3 天 = 2026-09-24
    assert thu[0]["date"] == "09月24日", (
        f"第3周周四是 09月24日，实际 {thu[0]['date']}"
    )
