"""灰箱排课 × 真实数据 交叉核查 —— 模拟结果可信吗？

用户要求（2026-10-22）：「模拟后的结果与真实结果对比，核查」。

做法与 audit_conflicts.py 同哲学：**独立到底**。灰箱方案由 API 产生，但核查
不用后端的任何结论 —— 用 openpyxl 直读原始安排表（load_rows），按**真实时钟
区间**（不是时段标签）判定教师/场地是否已被真实课程占用。

核查项（对灰箱方案里的每一条排课结果）：
    W1  周次在真实表的周次范围内
    W2  星期是真实表用过的合法值
    W3  时段标签可解析为真实时钟区间
    W4  场地真实存在（真实表 10 间之一）
    W5  教师真实存在（真实表 13 位之一）
    C1  该格该场地不与真实课程重叠（按区间，不是按标签）
    C2  该格该教师不与真实课程重叠
    I1  方案内部不自撞（同场地/同教师/同组 同时段不重复）

用法：
    D:/miniconda3_new/python.exe scripts/verify_graybox.py [--site http://127.0.0.1:8023]
前置：后端已启动且 load-default 完成（脚本自己会再调一次，幂等）。
退出码：0 = 全部通过；1 = 有核查不过的条目。
"""

import argparse
import json
import os
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import audit_conflicts as ac

import openpyxl

SITE = "http://127.0.0.1:8023"
GRAYBOX_CLASS = "2025级灰箱试验班"


# ---------------------------------------------------------------- 真实表基线

def real_baseline():
    """从原始 xlsx 提取核查基线：占用、词汇表。"""
    rows, unknown = ac.load_rows()
    weeks = sorted({r["week"] for r in rows})
    weekdays = {r["weekday"] for r in rows}
    rooms = {r["room"] for r in rows if r["room"]}
    teachers = set()
    for r in rows:
        for t in [p.strip() for p in r["teacher"].replace("，", ",").replace("、", ",")
                  .replace("/", ",").split(",") if p.strip()]:
            teachers.add(t)
    return {"rows": rows, "unknown": unknown, "weeks": weeks,
            "weekdays": weekdays, "rooms": rooms, "teachers": teachers}


def room_hit(baseline, week, weekday, label, room):
    """候选格 (week, weekday, label) 是否与该 room 的真实课程区间重叠。"""
    if not room:
        return False
    for r in baseline["rows"]:
        if r["week"] == week and r["weekday"] == weekday and r["room"] == room \
                and ac.hits_row(label, r):
            return True
    return False


def teacher_hit(baseline, week, weekday, label, teacher):
    if not teacher:
        return False
    for r in baseline["rows"]:
        if r["week"] == week and r["weekday"] == weekday and \
                ac.teacher_hits(r["teacher"], teacher) and ac.hits_row(label, r):
            return True
    return False


# ---------------------------------------------------------------- API 驱动

def http_json(url, payload=None, timeout=120, token=None, raw=False):
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read()
    return body if raw else json.loads(body)


def drive_graybox(site):
    """走完整灰箱流程：load-default → 预置主数据 → 上传灰箱清单 → solve。"""
    http_json(f"{site}/api/reschedule/load-default", payload={}, timeout=180)

    tok = http_json(f"{site}/api/reschedule/admin-login",
                    payload={"username": "admin", "password": "admin123"})["token"]

    http_json(f"{site}/api/reschedule/master/classes", token=tok,
              payload={"name": GRAYBOX_CLASS, "groups": ["1组", "2组"]})
    presets = [
        {"name": "灰箱项目A", "teachers": ["吴琳"], "rooms": ["A502", "A410"],
         "classes": [GRAYBOX_CLASS]},
        {"name": "灰箱项目B", "teachers": ["鄢仁文"], "rooms": ["A502"],
         "classes": [GRAYBOX_CLASS]},
        {"name": "灰箱项目C", "teachers": ["吴琳"], "rooms": ["A410"],
         "classes": [GRAYBOX_CLASS]},
    ]
    for p in presets:
        http_json(f"{site}/api/reschedule/master/experiments", token=tok, payload=p)

    # 灰箱清单：只有 班级/项目/组 三列（没有教师、场地、星期、节次、周次）
    list_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "..", ".tmp", "graybox_verify.xlsx")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "教学任务清单"
    ws.append(["行政班级", "项目名称", "学生组"])
    for proj in ["灰箱项目A", "灰箱项目B", "灰箱项目C"]:
        ws.append([GRAYBOX_CLASS, proj, "1组"])
        ws.append([GRAYBOX_CLASS, proj, "2组"])
    wb.save(list_path)

    # multipart 上传
    boundary = "----grayboxverify"
    with open(list_path, "rb") as f:
        file_bytes = f.read()
    part = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="graybox_verify.xlsx"\r\n'
        f"Content-Type: application/vnd.openxmlformats-officedocument.spreadsheetml.sheet\r\n\r\n"
    ).encode() + file_bytes + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        f"{site}/api/reschedule/upload-master", data=part,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}",
                 "Authorization": f"Bearer {tok}"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        up = json.loads(resp.read())
    assert up.get("success"), up

    result = http_json(f"{site}/api/reschedule/parallel/solve",
                       payload={"class_name": GRAYBOX_CLASS}, timeout=180)
    return result, up


# ---------------------------------------------------------------- 核查

def verify(result, baseline):
    problems = []
    stats = {"checked": 0}

    def bad(row, code, detail):
        problems.append(f"[{code}] 第{row['week']}周{row['weekday']}{row['time_slot']}"
                        f" {row['room']} {row['teacher']} {row['group']}/{row['project']}: {detail}")

    sched = result.get("schedule") or []
    for row in sched:
        stats["checked"] += 1
        w, wd, label = row["week"], row["weekday"], row["time_slot"]
        if w not in baseline["weeks"]:
            bad(row, "W1", f"周次 {w} 不在真实表范围内 {baseline['weeks'][0]}-{baseline['weeks'][-1]}")
        if wd not in baseline["weekdays"]:
            bad(row, "W2", f"星期 {wd} 真实表没用过")
        if label not in ac.CLOCK:
            bad(row, "W3", f"时段 {label} 解析不出真实时钟区间")
        if row["room"] not in baseline["rooms"]:
            bad(row, "W4", f"场地 {row['room']} 不在真实表 {len(baseline['rooms'])} 间场地里")
        if row["teacher"] and row["teacher"] not in baseline["teachers"]:
            bad(row, "W5", f"教师 {row['teacher']} 不在真实表 {len(baseline['teachers'])} 位教师里")
        if room_hit(baseline, w, wd, label, row["room"]):
            bad(row, "C1", f"场地 {row['room']} 该格与真实课程重叠")
        if teacher_hit(baseline, w, wd, label, row["teacher"]):
            bad(row, "C2", f"教师 {row['teacher']} 该格与真实课程重叠")

    # 内部自撞
    seen_room, seen_teacher, seen_group = {}, {}, {}
    for row in sched:
        key3 = (row["week"], row["weekday"], row["time_slot"])
        for seen, name, val in ((seen_room, "场地", row["room"]),
                                (seen_teacher, "教师", row["teacher"]),
                                (seen_group, "学生组", f"{row['group']}")):
            k = (key3, val)
            if val and k in seen:
                bad(row, "I1", f"{name} {val} 与另一条同时段重复")
            seen[k] = True

    return problems, stats


def main():
    global SITE
    ap = argparse.ArgumentParser()
    ap.add_argument("--site", default=SITE)
    args = ap.parse_args()
    SITE = args.site

    baseline = real_baseline()
    print(f"真实表基线：{len(baseline['rows'])} 行 / {len(baseline['weeks'])} 周"
          f" / {len(baseline['rooms'])} 间场地 / {len(baseline['teachers'])} 位教师"
          f"（另有 {len(baseline['unknown'])} 条时段无法识别）")

    result, up = drive_graybox(SITE)
    print(f"灰箱清单上传：{up.get('message', '')}")
    print(f"求解：success={result.get('success')} status={result.get('status')} "
          f"scheduled={result.get('scheduled')}/{result.get('total_units')}")
    if not result.get("success"):
        print("❌ 灰箱求解失败：", result.get("error"))
        return 1
    print(f"假设（assumptions）{len(result.get('assumptions') or [])} 条 —— 决策可见")

    problems, stats = verify(result, baseline)
    print(f"\n逐条核查 {stats['checked']} 条灰箱方案（真实表独立基线，按真实时钟区间判重叠）：")
    if problems:
        for p in problems:
            print("  ❌", p)
        print(f"\n❌ No-Go：{len(problems)} 处核查不过")
        return 1
    print("  ✅ W1-W5 词汇/存在性全过；C1/C2 与真实课程零重叠；I1 内部零自撞")
    print("\n✅ Go：灰箱模拟结果与真实数据交叉核查通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
