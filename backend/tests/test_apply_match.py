"""工单匹配的精度：教师名不许子串、班级/课程名精确优先且歧义即拒绝。

为什么单开一个文件
------------------
`test_apply_move.py` 锁的是"移动有没有发生"，`test_parallel_weeks.py` 锁的是
"星期/时段反解准不准"。这一组锁的是第三件事：**到底匹配到了哪几条记录**。

背景（2026-10-29 实测，R-73 的 P2-1 漏项）
--------------------------------------------
`conflict_model._same_teacher` 早在 R-73 就改成了精确匹配 + 显式别名表，
但 `_apply_reschedule` 里的 `teacher not in e.teacher` 没跟着改。构造
「吴琳」与「吴琳琳」两位教师同一时段的两条记录、用一张「吴琳」的工单：

    applied=2
    after weeks: [('吴琳琳', 3), ('吴琳', 3)]

**一条工单挪走了两位老师的课。** 真实表当前 13 位教师互不为子串，所以线上不触发
——但那是数据巧合，不是逻辑正确。

班级/课程名此前是**双向包含**（`a in b or b in a`），两个方向都有问题：
  · `class_name in e.class_name` 让「应用化学1班」命中「应用化学1、2班」；
  · `e.class_name in class_name` 让短名反过来命中超集。
所以改成两档：**精确（按个体名拆开比）优先，包含档兜底，且兜底后判歧义**。
退让档不能去掉 —— 教秘写「扭摆法」而表里是「扭摆法测物体转动惯量」是常态。
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.utils.excel_parser import ScheduleDatabase, ScheduleEntry, name_tokens, same_teacher, same_label

from app.api import reschedule as R


def _db(tmp_path, monkeypatch, entries):
    d = ScheduleDatabase()
    d.entries = entries
    d.loaded = True
    monkeypatch.setattr(R, "_db", d)
    monkeypatch.setattr(R, "_DB_PATH", str(tmp_path / "t.db"))
    R._init_db()
    return d


def _rec(**kw):
    rec = {"teacher": "", "course_name": "", "class_name": "",
           "original_time": "", "target_week": 6, "source_week": 3}
    rec.update(kw)
    return rec


# ---------------------------------------------------------------- 教师：只认精确

def test_teacher_substring_never_matches_another_teacher(tmp_path, monkeypatch):
    """「吴琳」不得匹配「吴琳琳」——两条记录、一张工单，只能动一条。"""
    _db(tmp_path, monkeypatch, [
        ScheduleEntry(3, "周一", "567", "2024级应用化学1班", "g1", "声速测量", "A101", "吴琳琳"),
        ScheduleEntry(3, "周一", "567", "2024级应用化学1班", "g2", "声速测量", "A102", "吴琳"),
    ])
    out = R._apply_reschedule(_rec(
        teacher="吴琳", course_name="声速测量", class_name="2024级应用化学1班",
        original_time="周一 567", target_week=6, source_week=3), persist=False)

    assert out["applied"] == 1, out
    got = sorted((e.teacher, e.week) for e in R._db.entries)
    assert got == [("吴琳", 6), ("吴琳琳", 3)], f"吴琳琳的课被一起搬走了：{got}"


def test_teacher_exact_match_still_works(tmp_path, monkeypatch):
    """正向控制：名字完全一致时照常移动（别把守卫做成"永远匹配不上"）。"""
    db = _db(tmp_path, monkeypatch, [
        ScheduleEntry(3, "周一", "567", "X班", "g1", "声速测量", "A101", "吴琳"),
    ])
    out = R._apply_reschedule(_rec(
        teacher="吴琳", course_name="声速测量", class_name="X班",
        original_time="周一 567", target_week=6, source_week=3), persist=False)
    assert out["applied"] == 1, out
    assert [e.week for e in db.entries] == [6]


def test_teacher_name_with_trailing_space_is_normalised(tmp_path, monkeypatch):
    """云端工单里偶尔带空格：去空白后仍算同一位教师。"""
    _db(tmp_path, monkeypatch, [
        ScheduleEntry(3, "周一", "567", "X班", "g1", "声速测量", "A101", "吴琳"),
    ])
    out = R._apply_reschedule(_rec(
        teacher=" 吴琳 ", course_name="声速测量", class_name="X班",
        original_time="周一 567", target_week=6, source_week=3), persist=False)
    assert out["applied"] == 1, out


def test_multi_teacher_cell_matches_each_individual(tmp_path, monkeypatch):
    """一个格子写两位教师时，按个体名拆开分别可比（不是整串相等）。"""
    db = _db(tmp_path, monkeypatch, [
        ScheduleEntry(3, "周一", "567", "X班", "g1", "声速测量", "A101", "吴琳、吴琼"),
    ])
    for claim in ("吴琳", "吴琼"):
        db.entries[0].week = 3
        out = R._apply_reschedule(_rec(
            teacher=claim, course_name="声速测量", class_name="X班",
            original_time="周一 567", target_week=6, source_week=3), persist=False)
        assert out["applied"] == 1, f"{claim} 未能命中：{out}"


# ---------------------------------------------------------------- 班级/课程：两档

def test_class_cell_with_two_classes_matches_either_half(tmp_path, monkeypatch):
    """真实写法 `2024级计科3班和2024软工3班`：写其中任一半都应精确命中。"""
    db = _db(tmp_path, monkeypatch, [
        ScheduleEntry(3, "周四", "678", "2024级计科3班和2024软工3班", "g", "声速测量", "A502", "鄢仁文"),
    ])
    for claim in ("2024级计科3班和2024软工3班", "2024级计科3班", "2024软工3班"):
        db.entries[0].week = 3
        out = R._apply_reschedule(_rec(
            teacher="鄢仁文", course_name="声速测量", class_name=claim,
            original_time="周四 678 A502", target_week=6, source_week=3), persist=False)
        assert out["applied"] == 1, f"班级「{claim}」未能命中：{out}"


def test_short_course_name_still_matches_by_containment(tmp_path, monkeypatch):
    """退让档必须保留：教秘写「扭摆法」，表里是「扭摆法测物体转动惯量」。"""
    _db(tmp_path, monkeypatch, [
        ScheduleEntry(3, "周二", "567", "本班", "g1", "扭摆法测物体转动惯量", "A504", "吴琳"),
    ])
    out = R._apply_reschedule(_rec(
        teacher="吴琳", course_name="扭摆法", class_name="本班",
        original_time="周二 567 A504", target_week=6, source_week=3), persist=False)
    assert out["applied"] == 1, out


def test_ambiguous_course_is_refused_not_guessed(tmp_path, monkeypatch):
    """简称能对上两门不同课 ⇒ 拒绝并说清，绝不挑一条挪走。

    `分光计` 同时是「分光计的调节」与「分光计测量光栅常数及光波波长」的前缀。
    """
    db = _db(tmp_path, monkeypatch, [
        ScheduleEntry(3, "周四", "678", "X班", "g1", "分光计的调节", "A502", "吴琳"),
        ScheduleEntry(3, "周四", "91011", "X班", "g2", "分光计测量光栅常数及光波波长", "A503", "吴琳"),
    ])
    out = R._apply_reschedule(_rec(
        teacher="吴琳", course_name="分光计", original_time="周四",
        target_week=6, source_week=3), persist=False)

    assert out["applied"] == 0 and out["skipped"] == 0, out
    assert any("不止一条" in d for d in out["details"]), out["details"]
    assert [e.week for e in db.entries] == [3, 3], "拒绝了还动了数据"


def test_ambiguity_check_does_not_block_legit_multi_week(tmp_path, monkeypatch):
    """同一门课跨周多条**不是**歧义 —— 那是常态，必须照常移动。"""
    db = _db(tmp_path, monkeypatch, [
        ScheduleEntry(w, "周二", "567", "本班", "g1", "扭摆法测物体转动惯量", "A504", "吴琳")
        for w in (3, 4, 5)
    ])
    out = R._apply_reschedule(_rec(
        teacher="吴琳", course_name="扭摆法", class_name="本班",
        original_time="周二 567 A504", target_week=8, source_week=0), persist=False)
    assert out["applied"] == 1, out
    assert sorted(e.week for e in db.entries) == [3, 4, 8], "应只搬最近一周（第5周）"


# ---------------------------------------------------------------- 名字拆分的边界

@pytest.mark.parametrize("raw", ["", "   ", "、", "和"])
def test_name_tokens_never_returns_empties(raw):
    """拆分不能产出空串 —— 空串一旦进集合就会与"任何空字段"相等，变成万能匹配。

    测的是 `excel_parser.name_tokens`（全仓唯一实现）。此前它叫 `R._name_tokens`
    （api 层自留一份），2026-10-29 合并成一处后，测试跟着指向唯一实现。
    """
    assert name_tokens(raw) == []


def test_empty_actual_name_never_matches():
    """记录里教师为空（绪论课）时，任何申请都不能"因为两边都空而匹配"。"""
    assert same_teacher("吴琳", "") is False
    assert same_teacher("", "吴琳") is False
    assert same_label("本班", "") is False


# ---------------------------------------------------------------- 幂等键必须真的防重

def test_idem_key_has_unique_index(tmp_path, monkeypatch):
    """库层面兜住并发：`idem_key` 非空时必须唯一，空串互不冲突。"""
    import sqlite3

    _db(tmp_path, monkeypatch, [])
    conn = R._get_db()
    try:
        conn.execute("INSERT INTO applied_moves (request_data, applied_at, idem_key) VALUES ('a','t','k1')")
        conn.commit()
        # 同一个键再写一次必须被拒
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO applied_moves (request_data, applied_at, idem_key) VALUES ('b','t','k1')")
        conn.rollback()
        # 空串可以有任意多行（历史行与"没给键"的调用）
        for _ in range(3):
            conn.execute("INSERT INTO applied_moves (request_data, applied_at, idem_key) VALUES ('c','t','')")
        conn.commit()
        total = conn.execute("SELECT COUNT(*) c FROM applied_moves").fetchone()["c"]
    finally:
        conn.close()
    assert total == 4, f"空串不该互撞，实际 {total} 行"


def test_concurrent_same_key_writes_one_ledger_row(tmp_path, monkeypatch):
    """并发同一张工单：账本只能有一行、不许抛异常、真正被搬走的记录总数必须是 1。

    断言的是**用户可见的结果**（一共动了几条课、账本几行），不是某个分支的消息文案：
    400 条同格记录会让"输家"的 `applied` 为 0 的原因可能是冲突而非幂等，盯着文案
    会写出一个恒假的断言（这版第一稿就这么翻的）。
    """
    import threading

    _db(tmp_path, monkeypatch, [
        # 400 条把"查重 → 写账本"之间的窗口拉到能被并发撞上
        ScheduleEntry(1, "周一", "567", "X班", f"g{i}", "声速测量", f"A{i:03d}", "吴琳")
        for i in range(400)
    ])
    rec = {"teacher": "吴琳", "course_name": "声速测量", "class_name": "X班",
           "original_time": "周一 567 A000", "target_week": 8, "source_week": 1}

    bar = threading.Barrier(8)
    results: list = []

    def work():
        bar.wait()
        try:
            results.append(R._apply_reschedule(dict(rec), persist=True, idem_key="cloud-777"))
        except Exception as e:                                  # 要断言"没异常"
            results.append(e)

    threads = [threading.Thread(target=work) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    errors = [r for r in results if isinstance(r, Exception)]
    assert not errors, f"并发下抛异常了（教秘会以为审批坏了）：{errors[:2]}"

    conn = R._get_db()
    try:
        n = conn.execute(
            "SELECT COUNT(*) c FROM applied_moves WHERE idem_key = 'cloud-777'").fetchone()["c"]
    finally:
        conn.close()
    assert n == 1, f"同一工单落了 {n} 行账（期望 1）"

    # 真正被搬走的总数：只有赢家那一次有效，输家不得再撬动任何一条
    moved = sum(r["applied"] for r in results if not isinstance(r, Exception))
    assert moved == 1, f"同一工单被搬走 {moved} 条（期望 1）——重放会重复撬动周次"

    # 输家必须留下可读的解释，不能默默返回一个"成功但没动"
    losers = [r for r in results if not isinstance(r, Exception) and r["applied"] == 0]
    assert losers, "所有并发请求都报 applied>0，说明幂等没生效"
    assert all(r["details"] for r in losers), "输家没有任何说明"


# ---------------------------------------------------------------- 工单时间戳

def test_create_request_stores_a_real_timestamp(tmp_path, monkeypatch):
    """`created_at` 曾经是硬编码的学期待码 `'2026-2027-1'`（不是时间）。

    界面当时不展示它，所以没人发现；但按它排序、或加一句"提交于 …"就是一行错数据。
    这里断言的是"能被当成时间解析"，不是某个具体值。
    """
    import asyncio
    from datetime import datetime

    _db(tmp_path, monkeypatch, [])

    class Req:
        teacher = "吴琳"
        course_name = "声速测量"
        class_name = "X班"
        original_time = "周一 567 A101"
        target_week = 3
        reason = "出差"

    out = asyncio.run(R.create_request(Req()))
    created = out["request"]["created_at"]

    try:
        datetime.fromisoformat(created)
    except ValueError:
        pytest.fail(f"created_at 不是合法时间：{created!r}")
    assert created != "2026-2027-1", "学期待码被当成时间戳写进库了"


# ---------------------------------------------------------------- 读路径同一判据
# 上一节锁的是**写路径**（`_apply_reschedule` 别挪错人）。但同一个子串判据在
# **读路径**上也有一份，后果是反的：
#   · 沙盘 `find_slots_with_reasons`：把「吴琳琳」的课当成"吴琳本人有课"
#     ⇒ **隐去一个本来可用的时段**，教秘看到的是"这周没空"，而系统从没解释为什么；
#   · 课程列表 `get_teacher_courses`：「吴琳」名下混进「吴琳琳」的课。
# 2026-10-29 实测（修复前）：`find_slots_with_reasons(3, teacher_name="吴琳")` 把
# 周一 567 标成 `teacher_busy`，该时段不再出现在可用列表里。
# 下面四条把"读路径也走同一个判据"变成可执行的事实。

def _agent_db(monkeypatch, entries):
    from app.agent.reschedule_agent import RescheduleAgent
    d = ScheduleDatabase()
    d.entries = entries
    d.loaded = True
    d._build_index()
    monkeypatch.setattr(R, "_db", d)
    return d, RescheduleAgent(d)


def test_sandbox_does_not_hide_slot_when_only_a_longer_name_busy(monkeypatch):
    """「吴琳琳」的课不得让沙盘把「吴琳」的可用时段标成"本人有课"。"""
    _d, agent = _agent_db(monkeypatch, [
        ScheduleEntry(3, "周一", "567", "A班", "g1", "声速测量", "A101", "吴琳琳"),
        ScheduleEntry(3, "周一", "678", "B班", "g2", "声速测量", "A102", "陈胜钰"),
    ])
    res = agent.find_slots_with_reasons(3, teacher_name="吴琳")

    self_busy = [(e["weekday"], e["time_slot"]) for e in res["excluded"]
                 if e.get("reason_type") == "teacher_busy"]
    assert self_busy == [], f"把别人的课算成本人有课了：{self_busy}"
    # 两层占用要分开看：A101 周一 567 仍会被**教室**挡掉（吴琳琳在那间上课，
    # 对吴琳来说这间教室确实不可用，排除是对的）。被错误挡掉的是**教师**这一维 ——
    # 同一时段换个空教室就该可选。
    assert any(s["weekday"] == "周一" and s["time_slot"] == "567" and s["room"] != "A101"
               for s in res["available"]), \
        "教师维度被误判后，整个周一 567 都从可用列表消失了（本该只剩 A101 不可用）"


def test_sandbox_still_hides_slot_when_the_teacher_really_busy(monkeypatch):
    """正向控制：本人真有课时必须继续排除（别把守卫做成"永远不排除"）。"""
    _d, agent = _agent_db(monkeypatch, [
        ScheduleEntry(3, "周一", "567", "A班", "g1", "声速测量", "A101", "吴琳"),
    ])
    res = agent.find_slots_with_reasons(3, teacher_name="吴琳")

    assert any(e.get("reason_type") == "teacher_busy" for e in res["excluded"]), res["excluded"][:3]
    assert not any(s["weekday"] == "周一" and s["time_slot"] == "567"
                   for s in res["available"]), "本人有课的时段不该被推荐"


def test_teacher_courses_do_not_mix_two_similar_names(monkeypatch):
    """课程列表：两位名字相近的教师各拿各的课。"""
    _d, _ = _agent_db(monkeypatch, [
        ScheduleEntry(3, "周一", "567", "A班", "g1", "声速测量", "A101", "吴琳琳"),
        ScheduleEntry(3, "周一", "678", "B班", "g2", "扭摆法测物体转动惯量", "A102", "吴琳"),
    ])
    assert [c["course_name"] for c in _d.get_teacher_courses("吴琳")] == ["扭摆法测物体转动惯量"]
    assert [c["course_name"] for c in _d.get_teacher_courses("吴琳琳")] == ["声速测量"]


def test_shared_judgement_is_the_only_implementation():
    """三处调用点必须**共用**同一份判据，不许各写一份子串比较。

    这条防的是"修了一处、另两处还在"——本轮之前正是这个状态（写路径修过，
    读路径与课程列表还是 `in e.teacher`）。

    判据用 **AST 而不是 grep**：注释与文档字符串里会**合法地**提到旧写法
    （"旧写法 `teacher not in e.teacher` 在…"），按文本 grep 会把说明本身判成违规。
    这里只看真正的比较表达式：`Compare(left=Name|Attribute, ops=[In|NotIn])`，
    且右操作数涉及 `.teacher` 属性。这样注释怎么写都不影响，而代码里真做子串比较就必红。
    """
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "app"
    offenders = []
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Compare):
                continue
            if not any(isinstance(op, (ast.In, ast.NotIn)) for op in node.ops):
                continue
            left = node.left
            right = node.comparators[0] if node.comparators else None
            # 只看"某值 in 某记录的教师字段"这一形态：右操作数含 Attribute(.teacher)
            for operand in (left, right):
                if isinstance(operand, ast.Attribute) and operand.attr == "teacher":
                    offenders.append(f"{path.name}:{node.lineno}")
                    break
    assert not offenders, (
        "还有子串判教师的写法（应改用 excel_parser.same_teacher）："
        + ", ".join(sorted(set(offenders)))
    )


# ---------------------------------------------------------------- 判据只许有一份实现
# 2026-10-29 的教训（作者自己犯的）：R-81 在 `api/reschedule.py` 里写了一组
# `_name_tokens/_teacher_matches/_label_matches`，R-83 又在 `utils/excel_parser.py`
# 写了**一模一样的** `name_tokens/same_teacher/same_label`（因为读路径在另一个模块、
# 不能反向依赖 api）。两套并存了两轮，直到覆盖率图把 api 那份显示成"未覆盖"
# 才被发现 —— 而它当时**正在被写路径使用**。
# 更糟的是我合并时先删掉了 api 那份的导入，测试 `R._name_tokens` 立刻红 4 条：
# 说明测试保护了它，却没保护"不该有两份"。

def test_only_one_teacher_matching_implementation():
    """全仓只允许一处"拆名 + 精确比"的实现。

    判据：同时定义 `name_tokens` **和** `same_teacher` 的模块只许有一个
    （`app/utils/excel_parser.py`）。别处要用就 import 它。
    """
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "app"
    owners = []
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
        if {"name_tokens", "same_teacher"} <= names:
            owners.append(path.relative_to(root).as_posix())
    assert owners == ["utils/excel_parser.py"], (
        "判据实现必须唯一，实际出现在：" + ", ".join(owners or ["（无）"])
    )


def test_api_layer_imports_the_shared_judgement():
    """写路径（`api/reschedule.py`）必须**导入**共用判据，而不是自己再写一份。"""
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "app" / "api" / "reschedule.py").read_text(encoding="utf-8")
    assert "same_teacher as _teacher_matches" in src, "写路径没接到共用判据上"
    assert "same_label as _label_matches" in src

    import ast
    tree = ast.parse(src)
    defined = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
    for dup in ("_teacher_matches", "_label_matches", "_name_tokens"):
        assert dup not in defined, f"{dup} 又在 api 层定义了一遍（应 import）"


# ---------------------------------------------------------------- 覆盖率探针自身的口径
# 探针坏了会**静默说谎**（R-25 的同一族）：2026-10-29 实测 `--show-missing` 把某个
# 文件的文档字符串行号列成"未覆盖"，看着像"这块没人测"，实际是命中集与分母集
# 用了两个不同的文件名键（`abspath` vs `resolve`，Windows 上大小写可能不同），
# 导致整份文件查不到命中。下面两条把口径钉住。

def test_coverage_probe_key_is_case_insensitive_on_windows():
    """命中集与分母集必须用同一个键 —— `..` 要归一，Windows 上大小写也要折叠。

    注意断言用的是**绝对路径**：pytest 的 cwd 是 `backend/`，写相对路径会被拼错
    （本测试第一版就是这么翻的 —— 用相对路径断言 `_key` 的归一化，等于在测拼错）。
    """
    import os
    import sys
    from pathlib import Path

    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts"))
    from coverage_probe import _key

    real = Path(__file__).resolve().parents[1] / "app" / "api" / "reschedule.py"
    a = _key(real)
    b = _key(real.parent / ".." / "api" / "reschedule.py")     # 含 `..`
    c = _key(Path(str(real)).as_posix())                        # 正斜杠写法
    assert a == b, "_key 没把 `..` 归一，命中集会查不到"

    variants = {a, b, c}
    if os.name == "nt":
        variants.add(_key(str(real).replace(os.sep + "app" + os.sep, os.sep + "APP" + os.sep)))
    assert len(variants) == 1, f"同一文件出现了多个键：{variants}"


def test_coverage_probe_excludes_docstrings_from_denominator():
    """文档字符串不算可执行行（否则 `--show-missing` 会把它列成"未覆盖"）。"""
    import os
    import sys
    from pathlib import Path

    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts"))
    from coverage_probe import executable_lines

    target = Path(__file__).resolve().parents[1] / "app" / "utils" / "time_model.py"
    lines = executable_lines(target)
    src = target.read_text(encoding="utf-8").splitlines()

    assert 1 not in lines, "模块 docstring 第一行被算成可执行行了"
    # 找模块 docstring 的正文行（标题下那几行），它们都不该在分母里
    doc_text = [i + 1 for i, text in enumerate(src[:30])
                if text.strip() and not text.strip().startswith('"""') and i > 0 and i < 20]
    inside = sorted(set(doc_text) & lines)
    assert not inside, f"docstring 正文进了分母：{inside}"


# ---------------------------------------------------------------- 每个数据入口都要自愈
# 2026-10-29 实测：进程刚起时 `POST /chat` 返回「排课数据尚未加载，请先上传实验安排表」，
# 而同一进程里 `GET /teachers` 能自动恢复。原因是 `/chat` 少了
# `await _ensure_loaded_async()` —— 而前端**没有上传入口**（`/upload` 要管理员且无 UI），
# 教秘无法自救。这类遗漏平时测不出来：单测里 `_db.loaded` 总是 True。

def test_every_data_reading_route_self_heals():
    """凡是**读内存排课表**的 `async` 路由，都必须（直接或经由同步 helper）自愈。

    判据分两层，因为真实实现有两种合法形态：
      1. 端点自己 `await _ensure_loaded_async()`；
      2. 端点调用一个**同步 helper**（导出族统一走 `_*_bytes()`），helper 里自愈。

    所以这里沿本模块调用图做一层展开：端点体内出现自愈调用，**或**它调到的
    同步函数里有，都算通过。

    为什么用"黑名单"而不是白名单：写死的豁免名单会随新端点立刻过期。
    这里反过来 —— 列出**确实不读排课表**的端点作为豁免，其余一律要求自愈。
    新增端点若忘了自愈，这条会直接报出它的名字。
    """
    import ast
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "app" / "api" / "reschedule.py").read_text(encoding="utf-8")
    tree = ast.parse(src)

    # 本模块内的同步说明符（用于展开"端点 → helper"这一层）
    sync_fns = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}

    def calls_of(node):
        out = set()
        for n in ast.walk(node):
            if isinstance(n, ast.Call):
                f = n.func
                if isinstance(f, ast.Name):
                    out.add(f.id)
                elif isinstance(f, ast.Attribute):
                    out.add(f.attr)
                elif isinstance(f, ast.Call):        # run_in_threadpool(_foo, ...) 的实参
                    pass
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)                     and n.func.id == "run_in_threadpool" and n.args:
                a = n.args[0]
                if isinstance(a, ast.Name):
                    out.add(a.id)
        return out

    def heals(node, depth=0, seen=None):
        """node 里（含一层 helper 展开）是否调用了自愈。"""
        seen = seen or set()
        if depth > 2:
            return False
        body = ast.unparse(node)
        if "_ensure_loaded_async" in body or "_ensure_loaded(" in body:
            return True
        for name in calls_of(node):
            fn = sync_fns.get(name)
            if fn is not None and name not in seen:
                seen.add(name)
                if heals(fn, depth + 1, seen):
                    return True
        return False

    # 不需要排课数据的端点（逐条给理由，不能只是"懒得加"）
    EXEMPT = {
        "/load-default": "它本身就是加载动作",
        "/upload": "换表：先落文件再整表重载",
        "/upload-master": "只写清单，不读排课表",
        "/demo/reset": "自己调 _load_default_schedule",
        "/demo-mode": "只读一个环境变量",
        "/status": "它就是用来报告是否已加载的，不能先自愈",
        "/admin-login": "认证与排课数据无关",
        "/requests": "工单 CRUD 走 SQLite",
        "/requests/export": "工单导出走 SQLite，不读排课表",
        "/requests/{request_id}/approve": "同上（应用调课会自查 _db.loaded）",
        # 十个主数据 CRUD（增/删）：只动主数据，`_master_write` 内部自愈
        "/master/courses": "主数据 CRUD",
        "/master/experiments": "主数据 CRUD",
        "/master/teachers": "主数据 CRUD",
        "/master/rooms": "主数据 CRUD",
        "/master/classes": "主数据 CRUD",
        "/master/courses/{name}": "主数据 CRUD",
        "/master/experiments/{name}": "主数据 CRUD",
        "/master/teachers/{name}": "主数据 CRUD",
        "/master/rooms/{name}": "主数据 CRUD",
        "/master/classes/{name}": "主数据 CRUD",
        "/master/import-preview": "只读上传的字节，不碰内存状态",
        "/master/tasks-preview": "同上",
        "/parallel/export": "导出的是前端传上来的方案，不读排课表",
        # 初稿导出同样是"方案由前端带上来"，服务端不存会话态
        "/auto-schedule/export": "导出前端传来的初稿，不读排课表",
    }

    missing = []
    for node in tree.body:
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        decos = [ast.unparse(d) for d in node.decorator_list]
        route = next((d for d in decos if "router." in d), None)
        if route is None:
            continue
        path = route.split("'")[1] if "'" in route else ""
        if path in EXEMPT:
            continue
        if not heals(node):
            missing.append(f"{path} ({node.name})")

    assert not missing, (
        "这些端点会读内存排课表但没有自愈，沙箱回收后会回「请先上传实验安排表」：\n  - "
        + "\n  - ".join(missing)
    )


def test_chat_self_heals_when_data_not_loaded(monkeypatch):
    """行为锁：`_db.loaded=False` 时 `/chat` 必须自己加载，而不是让用户去上传。

    修前实测：进程刚起时 `/chat` 回「排课数据尚未加载，请先上传实验安排表」。
    """
    import asyncio
    from app.agent.reschedule_agent import RescheduleAgent
    from app.utils.excel_parser import ScheduleDatabase

    d = ScheduleDatabase()
    d.entries = [
        ScheduleEntry(3, "周一", "567", "A班", "g1", "声速测量", "A101", "吴琳"),
    ]
    d.loaded = False                       # ← 冷启动态

    def fake_load():
        d.loaded = True                    # 模拟"自愈"做了什么
        d._build_index()
        return "loaded", 1

    monkeypatch.setattr(R, "_db", d)
    monkeypatch.setattr(R, "_agent", RescheduleAgent(d))
    monkeypatch.setattr(R, "_load_default_schedule", fake_load)

    class Req:
        message = "我是吴琳，把声速测量调到第3周"
        fast = True
        context = None

    out = asyncio.run(R.reschedule_chat(Req()))
    assert d.loaded is True, "自己没有加载，还是把「去上传」甩给用户"
    assert out.success is True, out.message
