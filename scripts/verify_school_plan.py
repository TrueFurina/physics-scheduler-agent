"""全校一键方案 × 真实数据 交叉核查（独立到底）

与 verify_graybox.py / audit_conflicts.py 同哲学：方案由 API 产生，核查用
openpyxl 直读原始表 + 真实时钟区间判重叠，不复用后端解析器。

核查项（对 /parallel/solve-all 的每一条结果）：
    W1-W5  周次/星期/时段/场地/教师都在真实表词汇表内
    C1/C2  该格场地/教师不与**别的班**的真实课程区间重叠
    C3     不与**本班自己的绪论/考试**重叠（本班旧实验课可复用，绪论/考试不行）
    I1     方案内部不自撞（同场地/同教师/同学生组 同时段不重复）
    K1     班级覆盖：真实表的每个班级都出现在 per_class 里

用法：
    D:/miniconda3_new/python.exe scripts/verify_school_plan.py [--site URL]
退出码：0 = 全通过；1 = 有核查不过。
"""

import argparse
import json
import os
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import audit_conflicts as ac
import verify_graybox as vg

SITE = "http://127.0.0.1:8023"

# 不参与重排、必须原样保留的课（与后端 `_is_intro` / `looks_like_exam` 同口径，
# 但这里按原始表文本自判，保持"独立复核"不依赖 app.*）。
KEPT_KEYWORDS = ("绪论", "考试")


def kept_hit_own_class(baseline, week, weekday, label, own_class):
    """本班在该格是否正上**绪论/考试**。

    为什么单列一条（C3）：C1/C2 刻意跳过本班所有真实行（本班旧实验课会被重排复用），
    但绪论/考试**不参与重排**、是留下来的 —— 跳过它们就等于这一格无人校验。
    求解器那边同样只按"别的班"建占用，于是"本班绪论格"成了一个双方都不看的盲区。
    2026-10-28 实测真实数据该盲区当前 0 命中（410 条方案逐条核过），本条把它锁死。
    """
    if not own_class:
        return False
    for r in baseline["rows"]:
        if r["class"] != own_class:
            continue
        if not any(k in (r.get("experiment") or "") for k in KEPT_KEYWORDS):
            continue
        if r["week"] == week and r["weekday"] == weekday and ac.hits_row(label, r):
            return True
    return False


def room_hit_other(baseline, week, weekday, label, room, own_class):
    if not room:
        return False
    for r in baseline["rows"]:
        if r["class"] == own_class:
            continue
        if r["week"] == week and r["weekday"] == weekday and r["room"] == room                 and ac.hits_row(label, r):
            return True
    return False


def teacher_hit_other(baseline, week, weekday, label, teacher, own_class):
    if not teacher:
        return False
    for r in baseline["rows"]:
        if r["class"] == own_class:
            continue
        if r["week"] == week and r["weekday"] == weekday                 and ac.teacher_hits(r["teacher"], teacher) and ac.hits_row(label, r):
            return True
    return False


def verify(plan, baseline):
    problems = []
    checked = 0

    def bad(row, code, detail):
        problems.append("[%s] 第%s周%s%s %s %s %s/%s: %s" % (
            code, row.get("week"), row.get("weekday"), row.get("time_slot"),
            row.get("room"), row.get("teacher"), row.get("class_name"),
            row.get("group"), detail))

    for row in plan.get("schedule") or []:
        checked += 1
        w, wd, label = row["week"], row["weekday"], row["time_slot"]
        if w not in baseline["weeks"]:
            bad(row, "W1", "周次不在真实表范围")
        if wd not in baseline["weekdays"]:
            bad(row, "W2", "星期真实表没用过")
        if label not in ac.CLOCK:
            bad(row, "W3", "时段解析不出真实区间")
        if row["room"] not in baseline["rooms"]:
            bad(row, "W4", "场地不在真实场地表里")
        if row["teacher"] and row["teacher"] not in baseline["teachers"]:
            bad(row, "W5", "教师不在真实教师表里")
        # 只跟**别的班**的真实课程比：本方案是在重排本班自己的实验，
        # 本班旧行占过的格子本来就会被复用（求解器也只承诺"不撞别的班"）。
        if room_hit_other(baseline, w, wd, label, row["room"], row.get("class_name")):
            bad(row, "C1", "场地该格与别的班的真实课程重叠")
        if teacher_hit_other(baseline, w, wd, label, row["teacher"], row.get("class_name")):
            bad(row, "C2", "教师该格与别的班的真实课程重叠")
        if kept_hit_own_class(baseline, w, wd, label, row.get("class_name")):
            bad(row, "C3", "本班该格正在上绪论/考试（不参与重排，不能占用）")

    seen = {"room": {}, "teacher": {}, "group": {}}
    for row in plan.get("schedule") or []:
        key3 = (row["week"], row["weekday"], row["time_slot"])
        for dim, seen_map, val in (
            ("场地", seen["room"], row.get("room")),
            ("教师", seen["teacher"], row.get("teacher")),
            ("学生组", seen["group"], (row.get("class_name"), row.get("group"))),
        ):
            if not val:
                continue
            k = (key3, val)
            if k in seen_map:
                bad(row, "I1", "%s %s 与另一条同时段重复" % (dim, val))
            seen_map[k] = True

    # K1 班级覆盖
    plan_classes = {c["class_name"] for c in plan.get("per_class") or []}
    real_classes = {r["class"] for r in baseline["rows"] if r["class"]}
    missing = real_classes - plan_classes
    if missing:
        problems.append("[K1] 真实表班级未进全校方案：%s" % "、".join(sorted(missing)[:5]))

    return problems, checked


def main():
    global SITE
    ap = argparse.ArgumentParser()
    ap.add_argument("--site", default=SITE)
    args = ap.parse_args()
    SITE = args.site

    baseline = vg.real_baseline()
    print("真实表基线：%d 行 / %d 周 / %d 间场地 / %d 位教师"
          % (len(baseline["rows"]), len(baseline["weeks"]),
             len(baseline["rooms"]), len(baseline["teachers"])))

    req = urllib.request.Request(
        SITE + "/api/reschedule/parallel/solve-all",
        data=b"{}", headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as resp:
        plan = json.loads(resp.read())

    print("全校方案：%s/%s 班排定，%s 条，%s 秒" % (
        plan.get("classes_ok"), plan.get("classes_total"),
        plan.get("scheduled"), plan.get("elapsed_s")))
    failed = plan.get("classes_failed") or []
    for c in failed:
        print("  ⚠️ 未排定：", c.get("class_name"), "|", (c.get("error") or "")[:100])

    problems, checked = verify(plan, baseline)
    print("\n逐条核查 %d 条全校方案（独立基线，按真实时钟区间判重叠）：" % checked)
    if problems:
        for p in problems[:30]:
            print("  ❌", p)
        if len(problems) > 30:
            print("  …… 另有 %d 条" % (len(problems) - 30))
        print("\n❌ No-Go：%d 处核查不过" % len(problems))
        return 1
    print("  ✅ W1-W5 词汇/存在性全过；C1/C2 与别的班零重叠；C3 本班绪论/考试零占用；I1 内部零自撞；K1 班级全覆盖")
    print("\n✅ Go：全校一键方案与真实数据交叉核查通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
