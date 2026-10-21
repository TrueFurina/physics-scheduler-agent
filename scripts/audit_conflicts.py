"""独立冲突校验器（oracle）—— 不听后端自述，自己从原始 xlsx 重算一遍。

为什么需要
----------
这个产品对教秘的**唯一承诺**是"推荐的时段不会撞课"。前十一批全在问
"界面显示得对不对 / 提示说不说人话"，从没人独立验过这个承诺本身。
更糟的是：如果后端漏掉了某类占用，界面会理直气壮地显示"可用"，
任何 UI 测试都会通过 —— 因为界面确实诚实地展示了后端给它的（错的）结论。

所以这里刻意不复用 `app.utils.excel_parser`：直接 openpyxl 读原始表、
用**真实时间区间**判定重叠，与后端的"时段标签 + 字符串相等"完全解耦。
两边算法一致只能证明代码自洽；两边独立才叫交叉验证。

⚠️ 但"独立"必须独立到底：本文件第一版把 `14:00-17:00` 也映射成 `567`，
然后拿**标签**判重叠 —— 跟后端犯同一个错，于是 6 条跨节次记录永远"不重叠"，
沙盘把 `678` 判为可用也照过。校验器只要在某一处跟被验对象共用同一个错误假设，
那一处就等于没有校验。（R-40）

判定口径
--------
记录的真实区间 = 原表写法能解析成 `H:MM-H:MM` 就用它，否则查标签表：
    567   13:30-15:45
    678   15:45-18:00
    78    15:50-18:05   ← 与 678 重叠 2 小时 10 分
    MORNING 09:00-11:15
    14:00-17:00  ⇒ (840, 1020)  → 同时压住 567 和 678 的一部分
任意两条记录只要**区间有交**且同教室/同教师，就是真冲突（首尾相接不算）。

用法
----
    D:/miniconda3_new/python.exe scripts/audit_conflicts.py [--site URL] [--week 7,9]

默认扫**全部周次**。早先只抽 1/3/5/7/9，而 R-40 的漏点在第 12/14/16 周 ——
采样法天生看不见。全周次 ≈ 208 次 /chat，按 0.55s 节流（站内额度 120/分钟），
一次约两分钟；`AUDIT_CHAT_INTERVAL` 可调。

覆盖三条"承诺"：
    沙盘     /chat 给出的每个「可用时段」都不撞已排的课
    自动排课 /auto-schedule 给出的每条方案都带周次、且该周该格不撞已排的课
    一键排课 /parallel/solve 的方案内部不自撞、也不撞别的班

退出码：0 = 未发现冲突；1 = 发现冲突（可直接挂 CI）。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict
from typing import Dict, List, Tuple

import openpyxl

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
XLSX = os.path.join(REPO, "schedule.xlsx")
SHEET = "大学物理实验安排表 (周) "

# 时段 → (起始分钟, 结束分钟)
CLOCK = {
    "567": (13 * 60 + 30, 15 * 60 + 45),
    "678": (15 * 60 + 45, 18 * 60),
    "78": (15 * 60 + 50, 18 * 60 + 5),
    "91011": (18 * 60 + 30, 20 * 60 + 45),
    "MORNING": (9 * 60, 11 * 60 + 15),
}

# 原始表里的各种写法 → 标准标签（与后端 parse 结果解耦：这里自己写一遍，
# 后端改错了不会连带把校验器一起改错）
RAW_TO_SLOT = {
    "567": "567",
    "678": "678",
    "78": "78",
    "91011": "91011",
    "13:30-15:45": "567",
    "15:45-18:00": "678",
    "18:30-20:45": "91011",
    "15:50-18:05": "78",
    "18:00-21:00": "91011",
    "14:00-17:00": "567",
    "9:00-11:15": "MORNING",
    "9:00-12:00": "MORNING",
}


def overlap(a: str, b: str) -> bool:
    """两个**标签**的真实区间是否有交（保留给只拿得到标签的场景）。"""
    if a not in CLOCK or b not in CLOCK:
        return False
    s1, e1 = CLOCK[a]
    s2, e2 = CLOCK[b]
    return max(s1, s2) < min(e1, e2)


_RANGE_RE = re.compile(r"^\s*(\d{1,2})\s*[:：]\s*(\d{2})\s*[-~—－]\s*(\d{1,2})\s*[:：]\s*(\d{2})\s*$")


def row_interval(row: Dict) -> Tuple[int, int]:
    """一条原始记录的**真实**时钟区间。

    ⚠️ 这里刻意不读后端算好的 `slot`：校验器若跟着后端把 `14:00-17:00`
    也当成 `567`，两边一起错就等于没校验（第一版就是这样，`overlap()`
    拿标签比，6 条跨节次记录永远"不重叠"）。
    """
    m = _RANGE_RE.match(row.get("raw_ts") or "")
    if m:
        h1, m1, h2, m2 = (int(x) for x in m.groups())
        s, e = h1 * 60 + m1, h2 * 60 + m2
        if e > s:
            return (s, e)
    return CLOCK.get(row.get("slot"), (0, 0))


def hits_row(label: str, row: Dict) -> bool:
    """候选节次 `label` 是否撞上这条记录 —— 比真实区间，不比标签。"""
    c = CLOCK.get(label)
    if not c:
        return False
    iv = row.get("iv") or row_interval(row)
    return max(c[0], iv[0]) < min(c[1], iv[1])


def load_rows() -> Tuple[List[Dict], List[Dict]]:
    """返回 (可用行, 无法识别时段的行)。"""
    wb = openpyxl.load_workbook(XLSX, data_only=True)
    ws = wb[SHEET]
    rows, unknown = [], []
    for r in ws.iter_rows(min_row=3, values_only=True):
        week, weekday, ts, cls, group, exp, room, teacher = r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7]
        if not isinstance(week, (int, float)) or not weekday or not cls:
            continue
        raw = str(ts).strip() if ts else ""
        item = {
            "week": int(week),
            "weekday": str(weekday).strip(),
            "raw_ts": raw,
            "class": str(cls).strip(),
            "experiment": str(exp).strip() if exp else "",
            "room": str(room).strip() if room else "",
            "teacher": str(teacher).strip() if teacher else "",
        }
        slot = RAW_TO_SLOT.get(raw)
        if slot:
            item["slot"] = slot
            item["iv"] = row_interval({**item, "slot": slot})
            rows.append(item)
        else:
            unknown.append(item)
    return rows, unknown


def http_json(url: str, payload=None, timeout=120):
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


# ---- 沙盘是"每人每周一次 /chat"，全周次扫下来会撞上站内限流 ----------------
# 后端 /chat 的额度是 120 次 / 60 秒（见 app/ratelimit.py）。串行请求跑满
# 16 个周次 × 13 位教师 ≈ 208 次，不节流就会有一半变成 429 —— 而 429 被
# 记成"请求失败"后，那部分承诺等于**没被校验**（却也不会红）。（R-40 期间实测）
_CHAT_INTERVAL = float(os.getenv("AUDIT_CHAT_INTERVAL", "0.55"))
_last_chat = [0.0]


def chat_json(url: str, payload=None, retries: int = 2):
    """按限流节奏发 /chat；429 时读 Retry-After 等一等再试。

    宁可慢，也不能把"被限流"当成"校验通过"。重试用尽仍 429 时抛错，
    由调用方记成失败项（失败项会进 problems，退出码照样非 0）。
    """
    for attempt in range(retries + 1):
        gap = _CHAT_INTERVAL - (time.time() - _last_chat[0])
        if gap > 0:
            time.sleep(gap)
        _last_chat[0] = time.time()
        try:
            return http_json(url, payload)
        except urllib.error.HTTPError as e:
            if e.code != 429 or attempt >= retries:
                raise
            wait = 0
            try:
                wait = int(e.headers.get("Retry-After") or 0)
            except (TypeError, ValueError):
                wait = 0
            wait = max(wait, 5)
            print(f"    … 触发限流，等 {wait}s 后重试（第 {attempt + 1} 次）")
            time.sleep(wait + 0.5)
            _last_chat[0] = time.time()
    raise RuntimeError("unreachable")


def teacher_hits(entry_teacher: str, name: str) -> bool:
    """教师字段可能是"吴琳、张三"这类多名共用，按分隔符切开比，
    不做子串匹配（"李明" 不该命中 "李明华"）。"""
    if not entry_teacher or not name:
        return False
    parts = [p.strip() for p in entry_teacher.replace("，", ",").replace("、", ",").replace("/", ",").split(",")]
    return name in parts or name == entry_teacher


def audit_sandbox(site: str, rows: List[Dict], weeks: List[int]) -> Tuple[List[str], Dict[str, int]]:
    """验"推荐可用时段"：拿后端给的 available_slots 逐条跟原始表对。"""
    problems = []
    stat = {"teachers": 0, "queries": 0, "slots": 0, "failed": 0}
    teachers = http_json(f"{site}/api/reschedule/teachers")["teachers"]
    names = [t["name"] if isinstance(t, dict) else t for t in teachers]
    stat["teachers"] = len(names)
    for name in names:
        for week in weeks:
            stat["queries"] += 1
            try:
                resp = chat_json(
                    f"{site}/api/reschedule/chat",
                    {"message": f"我是{name}，把这门课调到第{week}周", "fast": True},
                )
            except Exception as e:  # 网络/限流不该被当成"没冲突"
                stat["failed"] += 1
                problems.append(f"[{name} 第{week}周] 请求失败：{e}")
                continue
            data = resp.get("data") or {}
            avail = data.get("available_slots") or []
            stat["slots"] += len(avail)
            for a in avail:
                slot, room, wd = a["time_slot"], a.get("room") or "", a["weekday"]
                # ① 教室：该周该天该教室是否有别的班在用（区间重叠即冲突）
                for e in rows:
                    if e["week"] != week or e["weekday"] != wd or e["room"] != room:
                        continue
                    if not hits_row(slot, e):
                        continue
                    problems.append(
                        f"[{name} 第{week}周] 推荐「{wd} {slot} {room}」可用，"
                        f"但该教室 {e['slot']}({e['raw_ts']}) 已被 {e['class']} {e['experiment']} 占用"
                    )
                    break
                # ② 教师：本人当周当天是否有别的课（区间重叠即冲突）
                for e in rows:
                    if e["week"] != week or e["weekday"] != wd:
                        continue
                    if not teacher_hits(e["teacher"], name):
                        continue
                    if not hits_row(slot, e):
                        continue
                    problems.append(
                        f"[{name} 第{week}周] 推荐「{wd} {slot} {room}」可用，"
                        f"但 {name} 本人 {e['slot']}({e['raw_ts']}) 在 {e['class']} 有课"
                    )
                    break
    return problems, stat


def audit_auto_schedule(site: str, rows: List[Dict]) -> List[str]:
    """验"一键自动排课"（首屏第一个按钮）。

    这一路原本不在校验范围内，因为它**不返回周次** —— 没有周次就没法跟原始表对。
    R-37 修好后方案带 `week`，于是"推荐的每一格都不撞"这条承诺也能被独立核对了。
    在旧实现上跑本函数会直接报"方案条目不周日次"，这本身就是回归信号。
    """
    problems: List[str] = []
    try:
        resp = http_json(f"{site}/api/reschedule/auto-schedule", {})
    except Exception as e:
        return [f"[自动排课] 请求失败：{e}"]

    sched = (resp.get("data") or {}).get("schedule") or []
    if not sched:
        return ["[自动排课] 没有返回任何排课条目"]

    weeks_in_table = sorted({r["week"] for r in rows})
    lo, hi = weeks_in_table[0], weeks_in_table[-1]
    missing_week = [s for s in sched if not isinstance(s.get("week"), int)]
    if missing_week:
        problems.append(
            f"[自动排课] {len(missing_week)} 条方案没有周次 —— 教秘不知道该用到哪一周，"
            f"也无法与已有安排表比对"
        )

    # ① 与原始表逐条对：同周、同天、真实时钟区间重叠 ⇒ 教室/教师不得撞
    for u in sched:
        if not isinstance(u.get("week"), int):
            continue
        if not (lo <= u["week"] <= hi):
            problems.append(f"[自动排课] 第{u['week']}周超出表范围（第{lo}–{hi}周）")
            continue
        for e in rows:
            if e["week"] != u["week"] or e["weekday"] != u["weekday"]:
                continue
            if not hits_row(u["time_slot"], e):
                continue
            if e["room"] and e["room"] == u.get("room"):
                problems.append(
                    f"[自动排课] 第{u['week']}周 {u['weekday']} {u['time_slot']} 排进教室 {u['room']}，"
                    f"但 {e['class']} 当天 {e['slot']}({e['raw_ts']}) 已在该教室"
                )
            if u.get("teacher") and teacher_hits(e["teacher"], u["teacher"]):
                problems.append(
                    f"[自动排课] 第{u['week']}周 {u['weekday']} {u['time_slot']} 排了 {u['teacher']}，"
                    f"但此人当天 {e['slot']}({e['raw_ts']}) 在 {e['class']} 有课"
                )

    # ② 方案内部：同一周同一时段，教室/教师/班级都不得重复
    for field, label in (("room", "教室"), ("teacher", "教师"), ("class_name", "班级")):
        seen: Dict[Tuple, List[str]] = defaultdict(list)
        for u in sched:
            if not isinstance(u.get("week"), int) or not u.get(field):
                continue
            seen[(u["week"], u["weekday"], u["time_slot"], u[field])].append(u.get("course", ""))
        for k, courses in seen.items():
            if len(courses) > 1:
                problems.append(f"[自动排课] 方案自撞：第{k[0]}周 {k[1]} {k[2]} {label} {k[3]} 同时排了 {courses}")

    return problems


def audit_solver(site: str, rows: List[Dict]) -> List[str]:
    """验"一键排课"：方案内部不得自撞，也不得撞别的班已排的课。"""
    problems = []
    courses = http_json(f"{site}/api/reschedule/parallel/overview")["data"]["courses"]
    for c in courses:
        cls = c["class_name"]
        try:
            plan = http_json(f"{site}/api/reschedule/parallel/solve", {"class_name": cls})
        except Exception as e:
            problems.append(f"[{cls}] 求解失败：{e}")
            continue
        units = plan.get("schedule") or []
        # ① 方案内部：同周同天同时段，教室/教师不得重复
        seen_room = defaultdict(list)
        seen_teacher = defaultdict(list)
        for u in units:
            key = (u["week"], u["weekday"], u["time_slot"])
            seen_room[key + (u["room"],)].append(u["group"])
            if u.get("teacher"):
                seen_teacher[key + (u["teacher"],)].append(u["group"])
        for k, groups in seen_room.items():
            if len(groups) > 1:
                problems.append(f"[{cls}] 方案自撞：{k[0]}周 {k[1]} {k[2]} 教室 {k[3]} 同时排了 {groups}")
        for k, groups in seen_teacher.items():
            if len(groups) > 1:
                problems.append(f"[{cls}] 方案自撞：{k[0]}周 {k[1]} {k[2]} 教师 {k[3]} 同时带 {groups}")
        # ② 与"别的班已排记录"的真实区间重叠
        for u in units:
            for e in rows:
                if e["week"] != u["week"] or e["weekday"] != u["weekday"]:
                    continue
                if e["class"] == cls:  # 本人正在被重排的旧记录不算
                    continue
                if not hits_row(u["time_slot"], e):
                    continue
                if e["room"] and e["room"] == u["room"]:
                    problems.append(
                        f"[{cls}] {u['week']}周 {u['weekday']} {u['time_slot']} 排了教室 {u['room']}，"
                        f"但 {e['class']} 当天 {e['slot']}({e['raw_ts']}) 已在该教室"
                    )
                if u.get("teacher") and teacher_hits(e["teacher"], u["teacher"]):
                    problems.append(
                        f"[{cls}] {u['week']}周 {u['weekday']} {u['time_slot']} 排了教师 {u['teacher']}，"
                        f"但此人当天 {e['slot']}({e['raw_ts']}) 在 {e['class']} 有课"
                    )
    return problems


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--site", default=os.getenv("SITE", "http://127.0.0.1:8023"))
    ap.add_argument("--week", default="")
    ap.add_argument("--skip-sandbox", action="store_true")
    ap.add_argument("--skip-auto", action="store_true")
    args = ap.parse_args()
    site = args.site.rstrip("/")

    rows, unknown = load_rows()
    print(f"原始表：{len(rows)} 条可识别时段 / {len(unknown)} 条时段无法识别")
    for u in unknown:
        print(f"  ⚠️ 第{u['week']}周 {u['weekday']} 时段「{u['raw_ts']}」{u['class']} @ {u['room'] or '无教室'}")

    all_problems = []
    if not args.skip_sandbox:
        # 默认扫**全表所有周次**。原先只抽 1/3/5/7/9 —— 而 R-40 的漏点正好在
        # 第 12/14/16 周，采样法天生看不见。13 位教师 × 16 周 ≈ 208 次请求，
        # 一次几十秒，比"上线后被教秘撞见"便宜得多。
        weeks = ([int(w) for w in args.week.split(",")] if args.week
                 else sorted({r["week"] for r in rows}))
        found, stat = audit_sandbox(site, rows, weeks)
        all_problems += found
        print(
            f"沙盘：校验 {stat['teachers']} 位教师 × {len(weeks)} 个周次 = {stat['queries']} 次请求，"
            f"逐条核对 {stat['slots']} 个「可用时段」（失败 {stat['failed']} 次）"
        )
    if not args.skip_auto:
        auto = audit_auto_schedule(site, rows)
        all_problems += auto
        print(f"自动排课：一键自动排课方案逐条核对（{len(auto)} 处问题）")
    plans = audit_solver(site, rows)
    all_problems += plans

    print(f"求解器：一键排课方案逐单元核对（{len(plans)} 处问题）")
    if not all_problems:
        print("\n✅ 未发现冲突：所有推荐时段与求解方案都与原始表不重叠")
        return 0
    print(f"\n❌ 发现 {len(all_problems)} 处冲突：")
    for p in all_problems:
        print("  -", p)
    return 1


if __name__ == "__main__":
    sys.exit(main())
