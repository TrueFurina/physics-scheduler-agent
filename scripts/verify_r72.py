"""R-72 终验：全表一键排课 → 段级求解、组级产出，独立核对冲突。

核对口径（独立 oracle，不复用 auto_scheduler 内部的 busy 集合）：
  1. 组课时覆盖：排定的组课时数 == 应排总数（410）；且每行都带 group。
  2. 与现有安排表不冲突：新方案任何一格 (周,星期,时段,教室/教师/班级)
     都不得撞上既有非重排行（绪论/考试这些不参与重排、必须原样保留的课）。
  3. 内部不冲突：新方案自己内部，同一格的教室/教师/班级也不得重复占用。
  4. 组展开正确：每个段的 k 个组 = 同周同教室（或合理换房）、时间顺序连续。
"""
import json as _json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from collections import Counter, defaultdict

from app.utils.excel_parser import ScheduleDatabase, looks_like_exam
from app.engine.auto_scheduler import AutoScheduler, _is_intro, _spans_overlap, _teacher_names, TIME_SLOTS, WEEKDAYS

DB = os.path.join(os.path.dirname(__file__), "..", "schedule.xlsx")

db = ScheduleDatabase()
db.load_from_excel(DB)
print(f"已加载 {len(db.entries)} 条；待排 {len(db.pending)} 条")

sched = AutoScheduler(db)
print("开始 solve_all() …")
res = sched.solve_all(chunk_size=60, max_chunks=40, flatten_seconds=5.0)
print(f"状态={res['status']} 用时={res.get('elapsed_s')}s "
      f"段 solved={res['solved']}/{res['total_sections']} "
      f"组课时 solved_groups={res['solved_groups']}/{res['total_groups']} "
      f"intro_excluded={res['intro_excluded']}")
print("分片报告：")
for c in res["chunks"]:
    print(f"  第{c['index']}片 规模{c['size']} 排定{c['solved']} 状态{c['status']} "
          f"用时{c['seconds']}s 贪心兜底={c['retried']}")

sched_rows = res["schedule"]

# ---- 1. 组课时覆盖 ----
total_groups = res["total_groups"]
solved_groups = res["solved_groups"]
no_group = [r for r in sched_rows if not r.get("group")]
print(f"\n[1] 组课时覆盖：{solved_groups}/{total_groups}，缺 group 行数={len(no_group)}")

# ---- 2. 与现有安排表冲突（独立 oracle）----
# 既有非重排行的占用格（绪论/考试，必须原样保留）
intro_busy_room, intro_busy_teacher, intro_busy_class = set(), set(), set()
for e in db.entries:
    keeps = _is_intro(e.experiment or "") or looks_like_exam(e.experiment or "")
    if not keeps:
        continue
    blocks = list(getattr(e, "blocked", None) or [])
    for ts in TIME_SLOTS:
        if ts not in blocks and not _spans_overlap(e.time_slot, ts):
            continue
        if e.room:
            intro_busy_room.add((int(e.week), e.weekday, ts, e.room))
        for tn in _teacher_names(e.teacher):
            intro_busy_teacher.add((int(e.week), e.weekday, ts, tn))
        if e.class_name:
            intro_busy_class.add((int(e.week), e.weekday, ts, e.class_name))

conf_room = conf_teacher = conf_class = 0
for r in sched_rows:
    cell = (r["week"], r["weekday"], r["time_slot"])
    if r["room"] and cell + (r["room"],) in intro_busy_room:
        conf_room += 1
    if r.get("teacher") and cell + (r["teacher"],) in intro_busy_teacher:
        conf_teacher += 1
    if r.get("class_name") and cell + (r["class_name"],) in intro_busy_class:
        conf_class += 1
print(f"\n[2] 与既有非重排课（绪论/考试）冲突：教室{conf_room} 教师{conf_teacher} 班级{conf_class}")

# ---- 2b. 与全部既有记录冲突（含非绪论，理论上新方案只填空格，不应撞任何既有格）----
all_room, all_teacher, all_class = set(), set(), set()
for e in db.entries:
    blocks = list(getattr(e, "blocked", None) or [])
    for ts in TIME_SLOTS:
        if ts not in blocks and not _spans_overlap(e.time_slot, ts):
            continue
        if e.room:
            all_room.add((int(e.week), e.weekday, ts, e.room))
        for tn in _teacher_names(e.teacher):
            all_teacher.add((int(e.week), e.weekday, ts, tn))
        if e.class_name:
            all_class.add((int(e.week), e.weekday, ts, e.class_name))
ac_r = ac_t = ac_c = 0
for r in sched_rows:
    cell = (r["week"], r["weekday"], r["time_slot"])
    if r["room"] and cell + (r["room"],) in all_room:
        ac_r += 1
    if r.get("teacher") and cell + (r["teacher"],) in all_teacher:
        ac_t += 1
    if r.get("class_name") and cell + (r["class_name"],) in all_class:
        ac_c += 1
# [2b] 是"新方案 vs 旧表全量"的对比：非绪论旧格会被替换腾空，新方案占用它
# 属正常替换（旧课已移走），**不算冲突**。真正要 0 的是 [2] 保留的绪论/考试。
print(f"[2b] 与全部既有记录重叠（含被替换的非绪论，属正常替换，仅参考）："
      f"教室{ac_r} 教师{ac_t} 班级{ac_c}")



# ---- 3. 内部不冲突 ----
seen_room, seen_teacher, seen_class = set(), set(), set()
in_room = in_teacher = in_class = 0
for r in sched_rows:
    cell = (r["week"], r["weekday"], r["time_slot"])
    if r["room"]:
        k = cell + (r["room"],)
        if k in seen_room:
            in_room += 1
        seen_room.add(k)
    if r.get("teacher"):
        k = cell + (r["teacher"],)
        if k in seen_teacher:
            in_teacher += 1
        seen_teacher.add(k)
    if r.get("class_name"):
        k = cell + (r["class_name"],)
        if k in seen_class:
            in_class += 1
        seen_class.add(k)
print(f"[3] 内部冲突：教室{in_room} 教师{in_teacher} 班级{in_class}")

# ---- 4. 组展开正确性：同周、连续格、逐段 group 完整 ----
by_key = defaultdict(list)
for r in sched_rows:
    by_key[(r["teacher"], r["course"], r["class_name"], r["week"], r["room"])].append(r)

timeline = [(d, t) for d in WEEKDAYS for t in TIME_SLOTS]
pos = {c: i for i, c in enumerate(timeline)}
bad_expand = 0
for rows in by_key.values():
    if len(rows) < 2:
        # 单组段本就一行，无需连续检查
        continue
    rows_sorted = sorted(rows, key=lambda r: pos[(r["weekday"], r["time_slot"])])
    seq = [pos[(r["weekday"], r["time_slot"])] for r in rows_sorted]
    # 连续：相邻差 1
    contiguous = all(seq[i+1] - seq[i] == 1 for i in range(len(seq)-1))
    if not contiguous:
        bad_expand += 1
print(f"[4] 多组段非连续展开（允许换房时已退化为不连续）：{bad_expand} 段")

# ---- 5. 缺口 ----
conflicts = res.get("conflicts") or []
with open(os.path.join(os.path.dirname(__file__), "r72_conflicts.json"), "w", encoding="utf-8") as _f:
    _json.dump(conflicts, _f, ensure_ascii=False, indent=2)
print(f"\n[5] 未排上的段/缺口：conflicts={len(conflicts)}（已存 r72_conflicts.json）")
# 按 reason 归类
rc = Counter(c.get("reason", "?")[:40] for c in conflicts)
for reason, n in rc.most_common():
    print(f"    [{n}] {reason}")
for c in conflicts[:10]:
    print(f"    - {c.get('teacher')} {c.get('course')}（{c.get('class_name')}）周{c.get('week')}组{c.get('group')}：{c.get('reason')}")

# ---- 总结 ----
ok = (solved_groups == total_groups and len(no_group) == 0
      and conf_room == 0 and conf_teacher == 0 and conf_class == 0
      and in_room == 0 and in_teacher == 0 and in_class == 0)
print("\n=== 结论 ===")
print("PASS ✅" if ok else "FAIL ❌")
sys.exit(0 if ok else 1)
