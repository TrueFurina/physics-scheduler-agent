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


# --------------------------------------------------------------------------- R43
# 「报出的冲突数 N」 vs 「拿同一份数据独立重算」
#
# 上面 4 条锁的是**聚合口径**（谁进哪个桶）；这一组锁的是**数量**：
# 影响分析报 `conflict_count = N`，那么 N 必须能用同一份数据独立算出来，
# 且每一处都要能被 `conflict_model.classify_move` 复核。
#
# 三者关系：
#   conflict_count  = len(excluded)          —— 被排除的候选格（含自身冲突）
#   affected_*/self_busy                     —— 从 excluded 聚合出来
#   conflict_model.classify_move             —— 写路径判"能不能落进去"的权威
# 前两者若与第三者不是同一套判据，教秘就会看到"影响分析说没事、点了提交被拒"。
# （已确认的一类差异见本文件末尾 `test_..._no_room_or_teacher_conflict` 的说明：
#   group 冲突影响分析**不看**，因为请求里没有班级/组，属遗留，不是本轮改的。）

from app.conflict_model import classify_move  # noqa: E402  （放在这里只为贴近用例）

TEACHERS = ("陈老师", "赵老师", "孙老师", "周老师")


def _world(monkeypatch, entries, skipped=None):
    """构造一个可控世界，返回 (agent, db)。"""
    db = ScheduleDatabase()
    db.entries = entries
    db.skipped = skipped or []
    db.loaded = True
    agent = RescheduleAgent(db)
    monkeypatch.setattr(api, "_db", db)
    monkeypatch.setattr(api, "_agent", agent)
    return agent, db


def _impact(teacher=MY_NAME) -> dict:
    req = api.ChatRequest(message=f"我是{teacher}，把大学物理实验调到第{WEEK}周")
    out = asyncio.run(api.impact_analysis(req))
    assert out["success"] is True
    assert out["target_week"] == WEEK
    return out["data"]


def _place(wd, ts, room, teacher, cls="24材料1班", grp="1组", exp="拉伸法测杨氏模量"):
    return ScheduleEntry(WEEK, wd, ts, cls, grp, exp, room, teacher)


def test_conflict_count_equals_hand_placed_blocked_cells(monkeypatch):
    """摆 3 个已知的被占格子，报出的 N 必须是 3 —— 且就是这 3 个格子。"""
    placed = [
        _place("周一", "567", "A101", TEACHERS[0]),
        _place("周二", "678", "A102", TEACHERS[1]),
        _place("周三", "91011", "A103", TEACHERS[2]),
    ]
    agent, _ = _world(monkeypatch, placed)
    data = _impact()

    assert data["conflict_count"] == 3, "手摆了 3 个被占格子，报出的冲突数应等于 3"
    # 独立重算：按 (星期, 节次, 教室) 三元组数被占的格子
    cells = {(e.weekday, s, e.room) for e in placed for s in e.blocked}
    reported = {(c["weekday"], c["time_slot"], c["room"]) for c in data["conflict_slots"]}
    assert reported == cells, f"报出的格子 {reported} 与实际被占格子 {cells} 对不上"


def test_conflict_count_is_not_truncated_with_the_10_slot_preview(monkeypatch):
    """预览只给前 10 条，但 N 必须仍是全量 —— 否则"冲突 12 处，下面只列了 10 条"。"""
    days = ("周一", "周二", "周三", "周四")
    placed = [_place(wd, ts, "A101", TEACHERS[i % len(TEACHERS)])
              for i, (wd, ts) in enumerate((w, t) for w in days for t in ("567", "678", "91011"))]
    assert len(placed) == 12
    _world(monkeypatch, placed)

    data = _impact()
    assert data["conflict_count"] == 12, "N 被预览长度截断了"
    assert len(data["conflict_slots"]) == 10, "预览本身仍应截断到 10 条"


def test_every_reported_conflict_is_reproducible_by_conflict_model(monkeypatch):
    """每一处被报出的冲突，都要能用写路径那套 `classify_move` 独立复现。"""
    placed = [
        _place("周一", "567", "A101", TEACHERS[0]),          # 场地被占
        _place("周二", "678", "A102", MY_NAME),              # 本人有课
        _place("周三", "91011", "A103", TEACHERS[1]),        # 场地被占
    ]
    agent, db = _world(monkeypatch, placed)
    data = _impact()
    excluded = agent.find_slots_with_reasons(WEEK, teacher_name=MY_NAME)["excluded"]

    # 报出的 N 必须等于实际排除条数（没有哪一类被静默漏掉）
    assert data["conflict_count"] == len(excluded)

    for e in excluded:
        hits = classify_move(
            db.entries, week=WEEK, weekday=e["weekday"], slots={e["time_slot"]},
            room=e["room"] or None, teacher=MY_NAME,
        )
        kinds = {h["kind"] for h in hits}
        if e["reason_type"] == "classroom_busy":
            assert "room" in kinds, f"{e['weekday']} {e['time_slot']} {e['room']} 报了教室冲突，" \
                                    f"但 conflict_model 复现不出来"
        elif e["reason_type"] == "teacher_busy":
            assert "teacher" in kinds, f"{e['weekday']} {e['time_slot']} 报了本人有课，" \
                                       f"但 conflict_model 复现不出来"


def test_unparsed_row_teacher_matches_conflict_model_criterion(monkeypatch):
    """时段看不懂的行也要按「精确同名」判教师，不能退回子串。

    R43 实测：这一路原本写 `teacher_name in te`，于是「吴琳」命中「吴琳琳」⇒
    凭空多报 3 处 teacher_busy，文案还写成「您（吴琳琳）当天已有课」。
    而 `conflict_model` 用精确比较，复核结果是 0 —— 两套判据不一致。
    """
    same_name = {"week": WEEK, "weekday": "周一", "raw_ts": "下午",
                 "class_name": "24材料1班", "experiment": "拉伸法测杨氏模量",
                 "room": "A101", "teacher": "吴琳琳"}
    _world(monkeypatch, [], skipped=[same_name])

    # 「吴琳」≠「吴琳琳」：不该算本人的课
    data = _impact(teacher=MY_NAME)
    assert data["conflict_count"] == 0, "别人的课被算成了本人有课（子串匹配）"
    assert data["self_busy_count"] == 0

    # 反向护栏：真的同名时必须还算占用（不能为了修上面那条把这个检查删掉）
    mine = dict(same_name, teacher=MY_NAME)
    _world(monkeypatch, [], skipped=[mine])
    data = _impact(teacher=MY_NAME)
    assert data["conflict_count"] == 3, "整日占用应覆盖 3 个时段"
    assert data["self_busy_count"] == 3


def test_available_cells_have_no_room_or_teacher_conflict(monkeypatch):
    """反向：说"可用"的格子，用写路径那套判据复核，场地/教师两类确实不撞。

    ⚠️ 只复核 room / teacher 两类。第三类 group（同班同组该时段已有课）
    影响分析**目前看不到** —— 请求里没有班级/组，`find_slots_with_reasons`
    也不接这两个参数。已实测：impact 认为周一567/A102 可用，而把 C1/1组
    放进去 `classify_move` 会判 group 冲突。这条差异作为遗留登记，未在本轮修。
    """
    placed = [
        _place("周一", "567", "A101", TEACHERS[0]),
        _place("周二", "678", "A102", MY_NAME),     # 本人有课 → 该格全天段排除
        _place("周三", "91011", "A101", TEACHERS[1]),
    ]
    agent, db = _world(monkeypatch, placed)
    result = agent.find_slots_with_reasons(WEEK, teacher_name=MY_NAME)

    assert result["available"], "这个场景本该有可用格子，否则这条测试退化成空断言"
    for a in result["available"]:
        hits = classify_move(
            db.entries, week=WEEK, weekday=a["weekday"], slots={a["time_slot"]},
            room=a["room"], teacher=MY_NAME,
        )
        assert not hits, (
            f"影响分析说 {a['weekday']} {a['time_slot']} {a['room']} 可用，"
            f"但 conflict_model 判 {[h['label'] for h in hits]}"
        )
