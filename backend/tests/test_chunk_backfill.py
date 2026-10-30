"""分片排课的片间回填：三个维度都不能漏（R48）。

背景
----
`AutoScheduler.solve_all` 把 171 个任务分片求解（一次性建模在 6 秒时限内
连可行解都拿不到）。分片之间**不是独立的**：

    前一片排定的 (周, 星期, 时段) 上的教室/教师/班级
    必须回填成后一片的占用，否则两片会互相"看不见"而重排同一格。

代码里已经踩过一次（2026-10-28 修）：班级维度原先只统计同批任务之间，
漏掉了"绪论课/考试类这些不参与重排、但占着座位"的行，
实测撞上 3 处。修法是把这类行单列进 `class_busy`。

本文件的作用
------------
这段逻辑很难在单测里构造 —— 它依赖分片边界与真实数据规模。
所以这里做的是**端到端校验**：跑一次全量分片排课，
然后在结果上独立重算三个维度的冲突（不复用求解器的判断）。

实测（真实表 433 条，chunk_size=60）：
  排定 410 条，status=OPTIMAL
  教室冲突 0 / 教师冲突 0 / 班级冲突 0
  与"保留不动的 17 行"（绪论+考试）撞车：0 处

这是"结果对不对"的校验，不是"代码怎么写的"的校验 ——
正因为独立重算，才能发现回填漏了一个维度。
"""
import os
import sys
from collections import defaultdict

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.utils.excel_parser import ScheduleDatabase

REAL = os.path.join(os.path.dirname(__file__), "..", "..", "schedule.demo.xlsx")


@pytest.fixture(scope="module")
def solved():
    if not os.path.isfile(REAL):
        pytest.skip("缺少 schedule.demo.xlsx")
    from app.engine.auto_scheduler import AutoScheduler

    db = ScheduleDatabase()
    db.load_from_excel(REAL)
    r = AutoScheduler(db).solve_all(chunk_size=60)
    return db, r


def _dups(rows, key):
    m = defaultdict(list)
    for s in rows:
        m[key(s)].append(s)
    return {k: v for k, v in m.items() if len(v) > 1}


def test_chunked_schedule_has_no_internal_collisions(solved):
    """分片结果内部：同一格不能有两门课抢同一间教室/教师/班级。"""
    _db, r = solved
    sch = r.get("schedule") or []
    assert sch, "没排定任何课，测试前提失效"

    for label, key in (
        ("教室", lambda s: (s["week"], s["weekday"], s["time_slot"], s["room"])),
        ("教师", lambda s: (s["week"], s["weekday"], s["time_slot"], s["teacher"])),
        ("班级", lambda s: (s["week"], s["weekday"], s["time_slot"], s["class_name"])),
    ):
        d = _dups(sch, key)
        assert not d, (
            f"分片排课在{label}维度出现 {len(d)} 处重复 —— "
            f"说明片间回填漏了这一维度。示例：{list(d.items())[:1]}"
        )


def test_chunked_schedule_never_collides_with_untouched_rows(solved):
    """新方案不能撞上"保留不动"的行（绪论课 / 考试类）。

    这些行不参与重排，但确实占着教室/班级/教师。漏算的表现是
    "新方案排进了这个班正在上绪论的那一节"。
    """
    db, r = solved
    from app.engine.auto_scheduler import _is_intro, looks_like_exam

    sch = r.get("schedule") or []
    kept = [e for e in db.entries
            if _is_intro(e.experiment or "") or looks_like_exam(e.experiment or "")]
    if not kept:
        pytest.skip("真实表里没有绪论/考试行")

    room_busy, cls_busy, tch_busy = set(), set(), set()
    for e in kept:
        for ts in (list(getattr(e, "blocked", None) or []) or [e.time_slot]):
            if e.room:
                room_busy.add((e.week, e.weekday, ts, e.room))
            if e.class_name:
                cls_busy.add((e.week, e.weekday, ts, e.class_name))
            if e.teacher:
                tch_busy.add((e.week, e.weekday, ts, e.teacher))

    for label, key, busy in (
        ("教室", lambda s: (s["week"], s["weekday"], s["time_slot"], s["room"]), room_busy),
        ("班级", lambda s: (s["week"], s["weekday"], s["time_slot"], s["class_name"]), cls_busy),
        ("教师", lambda s: (s["week"], s["weekday"], s["time_slot"], s["teacher"]), tch_busy),
    ):
        hits = [s for s in sch if key(s) in busy]
        assert not hits, (
            f"新方案撞上保留行（{label}维度）{len(hits)} 处 —— "
            f"片间回填把绪论/考试行漏掉了。示例：{hits[0]}"
        )


def test_chunk_report_totals_add_up(solved):
    """片报告里的排定数之和应等于总排定数（账要对得上）。"""
    _db, r = solved
    chunks = r.get("chunks") or []
    if not chunks:
        pytest.skip("未分片")
    per_chunk = sum(c.get("solved", 0) for c in chunks)
    total = len(r.get("schedule") or [])
    assert per_chunk == total, (
        f"各片排定数之和 {per_chunk} ≠ 总排定数 {total} —— 有课在片间丢了或重复计入"
    )
