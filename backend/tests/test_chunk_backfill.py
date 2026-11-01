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

from app.utils.excel_parser import ScheduleDatabase, ScheduleEntry

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


def test_chunked_schedule_backfills_class_between_tight_chunks():
    """片间**班级**回填必须是硬约束，不能靠"求解器恰好避开"（R58）。

    为什么不复用上面的真实表夹具
    ----------------------------
    真实表 433 条里，第 2 片会不会排进第 1 片已经用过的格子，取决于 CP-SAT
    在一批同等最优解里挑了哪个。实测同一份代码连跑 12 次，把
    `extra_class_busy=class_busy` 改成 `None` 的变异体只被检出 8 次 ——
    "回填漏了一整个维度"这件事，真实表有约三分之一的时间看不见。

    所以这里把候选格压到只剩一个，让结论与求解器的选择无关：
      1 周 × 5 星期 × 3 时段 = 15 格，两间教室；
      14 格用绪论课（绪论不参与重排，但确实占着教室）把两间都占满；
      只剩 (周一, 567) 一格还有空教室；
      班级 X 有两个教学段，`chunk_size=1` ⇒ 必然落进两片。

    · 回填生效 → 第 2 片看见 X 已在 (周一,567) → 无格可用 → 只排 1 段，另 1 段进冲突
    · 回填漏掉 → 第 2 片看不见 → 排进同一格的**另一间**教室（教室维度不冲突）
      → 同班同格两门课

    `assert len(sch) == 1` 是防"构造失效后空过"：一旦格子够用了，这一条先红，
    而不是让"无重复"悄悄通过。
    """
    from app.engine.auto_scheduler import TIME_SLOTS, WEEKDAYS

    free_cell = ("周一", "567")
    rooms = ["A401", "A402"]
    entries = []
    for day in WEEKDAYS:
        for slot in TIME_SLOTS:
            if (day, slot) == free_cell:
                continue
            for room in rooms:
                # 绪论课：`_is_intro` 只看项目名里有没有"绪论"，不参与重排但占着教室
                entries.append(
                    ScheduleEntry(1, day, slot, "别班-" + room, "1组", "绪论", room, "别师"))
    entries.append(ScheduleEntry(1, "周三", "678", "X班", "1组", "项目甲", "A401", "教师甲"))
    entries.append(ScheduleEntry(1, "周四", "91011", "X班", "1组", "项目乙", "A401", "教师乙"))

    db = ScheduleDatabase()
    db.entries = entries
    db.skipped = []
    db.loaded = True

    from app.engine.auto_scheduler import AutoScheduler

    r = AutoScheduler(db).solve_all(chunk_size=1)
    sch = r.get("schedule") or []
    assert len(sch) == 1, (
        f"预期只排出 1 段（另一段无格可用），实际 {len(sch)} 段 —— "
        f"构造前提已失效，这个结果不能用来判回填。rows={sch}"
    )
    dup = _dups(sch, lambda s: (s["week"], s["weekday"], s["time_slot"], s["class_name"]))
    assert not dup, (
        f"班级在片间被重复排进同一格 {len(dup)} 处 —— "
        f"第 2 片没看见第 1 片占掉的班级格。示例：{list(dup.items())[:1]}"
    )
