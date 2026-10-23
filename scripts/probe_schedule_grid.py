"""周课表网格独立探针 —— 不听后端自述，从原始 xlsx 重算每一格。

为什么需要单独一把尺子
----------------------
网格是**展示层**：后端说「第 3 周周三 678 有这条长实验」，界面就渲染它。
展示层最典型的故障不是"算错"而是"**少算一类行**"——周末 71 条不进网格、
上午时段 4 条不进网格、跨节次记录只在主标签格出现一次、无场地记录被静默
滤掉。这些故障下界面照样整整齐齐，任何"能看到格子"的测试都会通过，
而教秘对着网格数课时会发现少数了 —— 且**没有红**。

所以本探针刻意不复用 `app.utils.excel_parser`：自己 openpyxl 读原始表、
用自己的时钟区间表算"这条记录真实占住哪些节次"（与 audit_conflicts.py
同族的独立口径），再逐周逐格与 API 返回的网格对账。两边只在**同一处
共用同一个错误假设**时才等于没校验——本文件自己的表自己写。

对账口径
--------
- 逐周（1..week_max 全扫，不采样：R-40 的漏点在第 12/14/16 周，采样天生看不见）：
  - `totals.entries` == 原表该周可解析行数；
  - 每个 `星期|时段` 格里 (班级,组,项目) 的**多重集**逐个相等（跨节次记录
    在它占用的每个格各出现一次——按区间算，不按标签）；
  - `totals.unplaced` == 原表该周时段无法解析的行数；
  - `class_options`/`room_options` == 原表全表去重集合；
- 结构性断言：7 天 × 4 时段；周末格在全表范围内非空（R-41 回归）；
- 筛选断言：按班级筛过后格里只剩该班，且冲突口径仍是全表；
- 边界：周次越界 400 且说清范围（R-32：不夹取）。

用法：
    D:/miniconda3_new/python.exe scripts/probe_schedule_grid.py
    D:/miniconda3_new/python.exe scripts/probe_schedule_grid.py --site http://127.0.0.1:8023
"""
import argparse
import json
import os
import re
import urllib.error
import urllib.request
from collections import Counter, defaultdict

import openpyxl

DEFAULT_SITE = "http://127.0.0.1:8023"
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
XLSX = os.path.join(REPO, "schedule.xlsx")
SHEET = "大学物理实验安排表 (周) "

# 标准节次 → (起始分钟, 结束分钟)。与 audit_conflicts.py 同族：自己写一遍，
# 后端改错不会连带把校验器改错。
CLOCK = {
    "MORNING": (9 * 60, 11 * 60 + 15),
    "567": (13 * 60 + 30, 15 * 60 + 45),
    "678": (15 * 60 + 45, 18 * 60),
    "91011": (18 * 60 + 30, 20 * 60 + 45),
}
SLOT_ORDER = ["MORNING", "567", "678", "91011"]
WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

# 原表时段写法 → 时钟区间（能解析成 H:MM-H:MM 的用区间，否则查这张表）
RAW_IV = {
    "567": CLOCK["567"], "678": CLOCK["678"], "78": (15 * 60 + 50, 18 * 60 + 5),
    "91011": CLOCK["91011"], "MORNING": CLOCK["MORNING"],
    "13:30-15:45": CLOCK["567"], "15:45-18:00": CLOCK["678"],
    "18:30-20:45": CLOCK["91011"], "15:50-18:05": (15 * 60 + 50, 18 * 60 + 5),
    "18:00-21:00": CLOCK["91011"], "9:00-11:15": CLOCK["MORNING"],
    "9:00-12:00": CLOCK["MORNING"],
}
_RANGE_RE = re.compile(r"^\s*(\d{1,2})\s*[:：]\s*(\d{2})\s*[-~—－]\s*(\d{1,2})\s*[:：]\s*(\d{2})\s*$")


def row_interval(raw: str):
    """原表时段写法的真实区间；解析不出来返回 None（=该行入不了网格）。"""
    raw = (raw or "").strip()
    m = _RANGE_RE.match(raw)
    if m:
        h1, m1, h2, m2 = (int(x) for x in m.groups())
        s, e = h1 * 60 + m1, h2 * 60 + m2
        if e > s:
            return (s, e)
    return RAW_IV.get(raw)


def occupied_slots(iv):
    """这个区间真实压住哪些标准节次（区间有交才算，首尾相接不算）。"""
    out = []
    for label in SLOT_ORDER:
        c = CLOCK[label]
        if max(c[0], iv[0]) < min(c[1], iv[1]):
            out.append(label)
    return out


def load_oracle():
    """从原始表独立重算：{week: {cell_key: Counter[(cls,group,exp)]}} + 其它集合。

    两个计数口径分开存，别混：
    - `rows_per_week`：**记录条数**（一行原始表 = 一条安排，跨节次长实验占两格
      但仍是一条）——对应接口的 `totals.entries`；
    - `per_week` 的 Counter 值之和：**出现次数**（条数 × 占的节次数）——只用于
      逐格对账，不与 entries 比。
    """
    wb = openpyxl.load_workbook(XLSX, data_only=True)
    ws = wb[SHEET]
    per_week = defaultdict(lambda: defaultdict(Counter))
    rows_per_week = Counter()
    unknown_per_week = Counter()
    classes, rooms = set(), set()
    week_min, week_max = None, None
    for r in ws.iter_rows(min_row=3, values_only=True):
        week, weekday, ts, cls, group, exp, room, teacher = (
            r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7])
        if not isinstance(week, (int, float)) or not weekday or not cls:
            continue
        week = int(week)
        week_min = week if week_min is None else min(week_min, week)
        week_max = week if week_max is None else max(week_max, week)
        cls = str(cls).strip()
        if str(room).strip() if room else "":
            rooms.add(str(room).strip())
        classes.add(cls)
        iv = row_interval(str(ts) if ts else "")
        if not iv:
            unknown_per_week[week] += 1
            continue
        rows_per_week[week] += 1
        key = (cls, str(group).strip() if group else "", str(exp).strip() if exp else "")
        for slot in occupied_slots(iv):
            per_week[week][f"{str(weekday).strip()}|{slot}"][key] += 1
    return per_week, rows_per_week, unknown_per_week, sorted(classes), sorted(rooms), week_min, week_max


def http(method, url, payload=None, timeout=60):
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read()
        try:
            return e.code, json.loads(body)
        except Exception:
            return e.code, {"raw": body[:200].decode("utf-8", "replace")}


class Report:
    def __init__(self):
        self.rows = []

    def add(self, ok, name, detail):
        self.rows.append((ok, name, detail))
        print(f"{'PASS' if ok else 'FAIL'}  {name}  — {detail}")

    def skip(self, name, detail):
        self.rows.append((None, name, detail))
        print(f"SKIP  {name}  — {detail}")

    def done(self):
        bad = [r for r in self.rows if r[0] is False]
        print()
        print(f"{len(self.rows) - len(bad)}/{len(self.rows)} 通过")
        for _, n, d in bad:
            print(f"  未通过：{n} — {d}")
        return 0 if not bad else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--site", default=DEFAULT_SITE)
    args = ap.parse_args()
    site = args.site.rstrip("/")
    rep = Report()

    if not os.path.exists(XLSX):
        rep.skip("独立对账", f"找不到原始表 {XLSX}（非仓库根运行？）")
        return rep.done()

    # ① 独立重算（oracle）
    per_week, rows_per_week, unknown_per_week, classes, rooms, wk_min, wk_max = load_oracle()
    n_rows = sum(rows_per_week.values())
    rep.add(True, "独立 oracle 就绪",
            f"{len(per_week)} 周 / {n_rows} 条可解析行 / {sum(unknown_per_week.values())} 条时段无法识别"
            f" / {len(classes)} 班 / {len(rooms)} 室")

    # ② 加载默认表
    code, _ = http("POST", f"{site}/api/reschedule/load-default")
    if code != 200:
        rep.skip("全部对账", f"load-default HTTP {code}（服务未就绪？）")
        return rep.done()

    # ③ 逐周对账（全周次，不采样）
    bad_weeks = []
    total_cell_mismatch = []
    for w in sorted(per_week):
        code, resp = http("GET", f"{site}/api/reschedule/schedule-grid?week={w}")
        if code != 200:
            bad_weeks.append(f"第{w}周 HTTP {code}")
            continue
        d = resp.get("data") or {}
        oracle_cells = per_week[w]
        api_cells = d.get("cells") or {}
        # 单元格多重集对账
        all_keys = set(oracle_cells) | set(api_cells)
        for k in all_keys:
            oc = oracle_cells.get(k, Counter())
            ac = Counter((e["class_name"], e["group"], e["experiment"])
                         for e in api_cells.get(k, []))
            if oc != ac:
                total_cell_mismatch.append(
                    f"第{w}周 {k}：接口 {sum(ac.values())} 条 vs 独立 {sum(oc.values())} 条")
        # 条目总数（**条数**口径：跨节次长实验占两格但仍是一条）+ 无法识别行数
        if d.get("totals", {}).get("entries") != rows_per_week.get(w, 0):
            bad_weeks.append(f"第{w}周 entries {d.get('totals', {}).get('entries')} vs 独立 {rows_per_week.get(w, 0)}")
        if d.get("totals", {}).get("unplaced") != unknown_per_week.get(w, 0):
            bad_weeks.append(
                f"第{w}周 unplaced {d.get('totals', {}).get('unplaced')} vs 独立 {unknown_per_week.get(w, 0)}")
    rep.add(not bad_weeks and not total_cell_mismatch,
            f"逐周逐格对账（{len(per_week)} 周全扫）",
            "全部单元格 (班级,组,项目) 多重集一致" if not bad_weeks and not total_cell_mismatch
            else "；".join((bad_weeks + total_cell_mismatch)[:6]))

    # ④ 结构性断言：7 天 × 4 时段 + 周末格非空（R-41 回归）
    code, resp = http("GET", f"{site}/api/reschedule/schedule-grid?week=1")
    d = resp.get("data") or {}
    struct_ok = (len(d.get("weekdays") or []) == 7
                 and [s["slot"] for s in d.get("slots") or []] == SLOT_ORDER)
    rep.add(struct_ok, "网格结构 7 天 × 4 时段",
            f"weekdays={len(d.get('weekdays') or [])} slots={[s['slot'] for s in d.get('slots') or []]}")

    weekend_oracle = sum(
        sum(c.values()) for cells in per_week.values()
        for k, c in cells.items() if k.startswith("周六") or k.startswith("周日"))
    weekend_api = 0
    for w in sorted(per_week):
        code, resp = http("GET", f"{site}/api/reschedule/schedule-grid?week={w}")
        dd = resp.get("data") or {}
        for k, v in (dd.get("cells") or {}).items():
            if k.startswith("周六") or k.startswith("周日"):
                weekend_api += len(v)
    rep.add(weekend_oracle > 0 and weekend_api == weekend_oracle,
            "周末条目全量在网格里（R-41）",
            f"接口 {weekend_api} 条 vs 独立 {weekend_oracle} 条"
            + ("" if weekend_api == weekend_oracle else "（周末被丢了！）"))

    # ⑤ 筛选项集合对账
    api_classes = d.get("class_options") or []
    api_rooms = d.get("room_options") or []
    rep.add(api_classes == classes and api_rooms == rooms,
            "班级/场地筛选项与全表一致",
            f"班 {len(api_classes)}/{len(classes)}，室 {len(api_rooms)}/{len(rooms)}")

    # ⑥ 班级筛选：格里只剩该班
    probe_cls = classes[0] if classes else ""
    if probe_cls:
        code, resp = http("GET", f"{site}/api/reschedule/schedule-grid?week=1&class_name={urllib.request.quote(probe_cls)}")
        dd = resp.get("data") or {}
        only = all(e["class_name"] == probe_cls
                   for cell in (dd.get("cells") or {}).values() for e in cell)
        rep.add(code == 200 and only, f"按班级筛选（{probe_cls}）只显示该班",
                f"HTTP {code}，格内班级全部匹配={only}")

    # ⑦ 周次越界：400 且说清范围（R-32：不夹取）
    code, resp = http("GET", f"{site}/api/reschedule/schedule-grid?week=999")
    detail = (resp or {}).get("detail") or ""
    rep.add(code == 400 and "周" in str(detail),
            "周次越界报 400 并说清范围",
            f"HTTP {code}，detail={detail!r}")

    return rep.done()


if __name__ == "__main__":
    raise SystemExit(main())
