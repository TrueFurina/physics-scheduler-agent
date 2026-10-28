"""独立审计「自动排课」判据的回归锁（R-78）。

背景：`/auto-schedule` 默认走 `solve_all`，而 R-72 定的口径是**替换**那 410 个
非绪论单元（非绪论旧格可以复用，只有绪论/考试必须原样保留）。审计脚本却一直在
"与旧表逐条对"，于是 R-72 之后每次跑都报 3 条假冲突 —— 而验收一直带
`--skip-audit` 跑，红了两天没人看见。

正确的判据是"**落地后的表**不冲突"：被初稿覆盖的旧行会消失，不参与比对；
留下来的旧行（绪论/考试 + 初稿没覆盖到的单元）一条都不许撞。

这里直接喂构造数据测纯函数，不碰 HTTP。
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts"))

import audit_conflicts as ac


def _row(week, wd, slot, cls, exp, group, room, teacher):
    r = {"week": week, "weekday": wd, "raw_ts": slot, "class": cls,
         "experiment": exp, "group": group, "room": room, "teacher": teacher}
    r["slot"] = slot
    r["iv"] = ac.row_interval({**r})
    return r


def _plan(week, wd, slot, cls, exp, group, room, teacher):
    return {"week": week, "weekday": wd, "time_slot": slot, "class_name": cls,
            "course": exp, "group": group, "room": room, "teacher": teacher}


ROWS = [
    # 普通实验课：初稿会替换掉它 ⇒ 复用它的格子不算冲突
    _row(10, "周四", "567", "23物本", "光的偏振", "1组", "A411", "鄢仁文"),
    # 绪论：不参与重排，留下来 ⇒ 占了就是真冲突
    _row(10, "周四", "678", "23物本", "大学物理实验绪论", "1组", "A411", "鄢仁文"),
    # 初稿没覆盖到的单元（下面 A/D 两个用例里它不在初稿里）⇒ 留下来
    _row(10, "周四", "91011", "人工智能班", "光的衍射", "1组", "A502", "肖刚"),
]


def test_reusing_replaced_cell_is_not_a_conflict():
    """初稿落在"自己会替换掉的旧格"上，是 R-72 之后的正常行为，不许报。"""
    sched = [
        _plan(10, "周四", "567", "23物本", "光的偏振", "1组", "A411", "鄢仁文"),
        _plan(10, "周四", "91011", "人工智能班", "光的衍射", "1组", "A512", "肖刚"),
    ]
    assert ac.auto_schedule_problems(sched, ROWS) == []


def test_intro_cell_is_still_a_conflict():
    """绪论不参与重排 —— 初稿排进去必须报（这条是重写判据时最容易丢的一条）。"""
    sched = [
        _plan(10, "周四", "567", "23物本", "光的偏振", "1组", "A411", "鄢仁文"),
        _plan(10, "周四", "678", "23物本", "声速测量", "1组", "A411", "吴琳"),
        _plan(10, "周四", "91011", "人工智能班", "光的衍射", "1组", "A512", "肖刚"),
    ]
    problems = ac.auto_schedule_problems(sched, ROWS)
    assert len(problems) == 1 and "678" in problems[0], problems


def test_uncovered_unit_is_still_a_conflict():
    """初稿没覆盖到的旧课会留在表里 —— 撞它就是真冲突（教室 + 教师各一条）。"""
    sched = [
        _plan(10, "周四", "567", "23物本", "光的偏振", "1组", "A411", "鄢仁文"),
        _plan(10, "周四", "91011", "人工智能班", "声速测量", "1组", "A502", "肖刚"),
    ]
    problems = ac.auto_schedule_problems(sched, ROWS)
    assert len(problems) == 2, problems
    assert any("排进教室 A502" in p for p in problems), problems
    assert any("排了 肖刚" in p for p in problems), problems


def test_self_collision_is_still_a_conflict():
    sched = [
        _plan(10, "周四", "567", "23物本", "光的偏振", "1组", "A411", "鄢仁文"),
        _plan(10, "周四", "567", "另一个班", "声速测量", "1组", "A411", "吴琳"),
        _plan(10, "周四", "91011", "人工智能班", "光的衍射", "1组", "A512", "肖刚"),
    ]
    problems = ac.auto_schedule_problems(sched, ROWS)
    assert len(problems) == 1 and "方案自撞" in problems[0], problems


def test_missing_week_is_reported():
    sched = [{"weekday": "周四", "time_slot": "567", "class_name": "23物本",
              "course": "光的偏振", "group": "1组", "room": "A411", "teacher": "鄢仁文"}]
    problems = ac.auto_schedule_problems(sched, ROWS)
    assert any("没有周次" in p for p in problems), problems
