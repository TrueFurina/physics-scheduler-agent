"""诊断：一键自动排课初稿落地后为何出现教师重复占用。
不写结论，只打印证据。"""
import copy
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from collections import Counter

from app.utils.excel_parser import ScheduleDatabase
from app.engine.auto_scheduler import AutoScheduler
from app import plan_landing

DB = os.path.join(os.path.dirname(__file__), "..", "schedule.xlsx")
db = ScheduleDatabase()
db.load_from_excel(DB)
ev = db.entries
print(f"原表条数={len(ev)}")

# 原始可排单元（class,experiment,group） vs 非可排
sched_units = set()
nonsched = 0
for e in ev:
    if not plan_landing.is_schedulable_project(e.experiment):
        nonsched += 1
        continue
    sched_units.add((e.class_name, e.experiment, e.group))
print(f"非可排(绪论/考试)条数={nonsched}")
print(f"原始可排单元数(去重) = {len(sched_units)}")

# 跑全表
sched = AutoScheduler(db)
r = sched.solve_all(chunk_size=60)
plan = plan_landing.normalize_plan(r["schedule"])
plan_units = {(p["class_name"], p["project"], p["group"]) for p in plan}
print(f"初稿 plan 行数={len(plan)}  初稿单元数(去重)={len(plan_units)}")

missing = sched_units - plan_units
extra = plan_units - sched_units
print(f"原始有、初稿没有的单元数 = {len(missing)}")
for u in list(missing)[:10]:
    print("  MISSING:", u)
print(f"初稿有、原始没有的单元数 = {len(extra)}")
for u in list(extra)[:10]:
    print("  EXTRA  :", u)

# 落地到副本，找教师重复
db2 = copy.deepcopy(db)
land = plan_landing.apply_plan(db2, plan)
print(f"\napply_plan 结果: kept={land['kept']} replaced_with={land['replaced_with']} total={land['total']}")

# 教师维度重复（只列前若干）
seen = Counter((e.week, e.weekday, e.time_slot, e.teacher) for e in db2.entries if e.teacher)
dups = {k: c for k, c in seen.items() if c > 1}
print(f"\n落地后教师重复占用数 = {len(dups)}")
for k in list(dups)[:8]:
    print("  DUP teacher:", k, "x", dups[k])

# 具体看林珠云在 (1,周五,91011) 的两个来源
print("\n--- (1,周五,91011,林珠云) 的两个条目 ---")
for e in db2.entries:
    if e.teacher == "林珠云" and e.weekday == "周五" and e.time_slot == "91011":
        print("  ", "experiment=", e.experiment, "class=", e.class_name, "group=", e.group,
              "room=", e.room, "week=", e.week)
