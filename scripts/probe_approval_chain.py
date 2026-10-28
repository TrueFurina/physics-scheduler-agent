"""审批闭环探针：从"教师说要调课"到"排课数据真的变了"。

为什么需要单独一把尺子：
  前十一批验的是沙盘推荐得对不对、错误提示说不说人话。但产品还有一条**写路径**
  ——教师提交申请 → 教秘审批 → 排课数据变更。这条路上任何一环断了，界面都
  照样显示"操作成功"，因为写路径的失败不会让页面报错，只会让课表悄悄没变。

本脚本只做一件事：拿真实排课数据，走一遍真实接口，然后**回到数据本身**看它有没有变。
不复用前端、不复用 `_apply_reschedule`，用 `/teachers/{name}/courses` 的前后对比当判据。

用法：
  D:/miniconda3_new/python.exe scripts/probe_approval_chain.py
  ... --site http://127.0.0.1:8023              # 默认打本地
  ... --no-restore                              # 默认自动还原；要留痕迹才显式关

数据卫生（2026-10-24 踩坑记录）：
  本探针会**真的移动一条排课记录**。--restore 曾经默认关，忘了传就跑 ⇒ 探针
  移动留在了服务端（且 applied_moves 里的 probe-% 记录让第二轮 discover 失效），
  schedule_grid 对账探针立刻抓到"第 2 周多 1 条 / 第 3 周少 1 条"。现已默认开：
  本地库自动 DELETE probe-% + load-default；远端站点找不到 db 时按原样跳过
  （第 207 行那条分支），不会误删任何东西。
"""
import argparse
import json
import os
import re
import sqlite3
import sys
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_SITE = "http://127.0.0.1:8023"
ADMIN_USER = "admin"
ADMIN_PASS = "admin123"
HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(HERE, "..", "backend", "app", "data", "requests.db")


def http(method: str, url: str, payload=None, token: str = ""):
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read()
        try:
            return e.code, json.loads(body)
        except Exception:
            return e.code, {"raw": body[:300].decode("utf-8", "replace")}


class Report:
    def __init__(self):
        self.rows = []

    def add(self, ok, name, detail):
        self.rows.append((ok, name, detail))
        print(f"{'PASS' if ok else 'FAIL'}  {name}  —  {detail}")

    def done(self):
        bad = [r for r in self.rows if not r[0]]
        print()
        print(f"{len(self.rows) - len(bad)}/{len(self.rows)} 通过")
        for _, n, d in bad:
            print(f"  未通过：{n} — {d}")
        return 0 if not bad else 1


def discover(site: str):
    """挑一个"可调整 + 有明确原时间 + 有教室"的真实课程。"""
    _, teachers = http("GET", f"{site}/api/reschedule/teachers")
    for t in teachers.get("teachers") or []:
        name = t["name"] if isinstance(t, dict) else t
        code, resp = http("GET", f"{site}/api/reschedule/teachers/{urllib.parse.quote(name)}/courses")
        if code != 200:
            continue
        for c in resp.get("courses") or []:
            if c.get("adjustable") and c.get("original_time") and c.get("room"):
                return name, c
    return None, None


def find_movable_week(site: str, teacher: str, course: dict):
    """找到一个"目标周、同时段、同教室真的空着"的周次。

    必须这么做而不是随手写个第 9 周：`_apply_reschedule` 只改周次、不改星期/时段/教室，
    所以"能移过去"等价于"那一周同一格空着"——这正是沙盘 `available_slots` 的判据。
    模拟教秘的真实动作：看沙盘挑一个它说可用的周次。
    """
    m = re.search(r"(周[一二三四五])", course.get("original_time", ""))
    weekday = m.group(1) if m else ""
    room = course.get("room", "")
    for wk in (2, 5, 6, 7, 9, 10, 11, 12, 13, 14, 15, 16):
        _code, resp = http("POST", f"{site}/api/reschedule/chat",
                           {"message": f"我是{teacher}，把{course['course_name']}调到第{wk}周",
                            "fast": True})
        data = (resp or {}).get("data") or {}
        for s in data.get("available_slots") or []:
            if s.get("weekday") == weekday and s.get("room") == room:
                return wk, s.get("time_slot")
    return None, None


def current_course(site: str, teacher: str, course_name: str):
    """回查这门课当前的（原时间, 周次范围）。

    ⚠️ 判据必须用 `week_range` 而不是 `original_time`：`original_time` 只有
    "周二 13:30-15:45"，**不含周次**。调课动的是周次，所以拿 original_time
    比较会永远相等 —— 第一版探针就栽在这（应用成功却报"时间没变"）。
    """
    code, resp = http("GET", f"{site}/api/reschedule/teachers/{urllib.parse.quote(teacher)}/courses")
    if code != 200:
        return None
    for c in resp.get("courses") or []:
        if c.get("course_name") == course_name:
            return c
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--site", default=DEFAULT_SITE)
    ap.add_argument("--restore", action=argparse.BooleanOptionalAction, default=True,
                    help="测完撤掉探针造成的移动（默认开；--no-restore 留痕检查用）")
    args = ap.parse_args()
    site = args.site.rstrip("/")
    rep = Report()

    code, login = http("POST", f"{site}/api/reschedule/admin-login",
                       {"username": ADMIN_USER, "password": ADMIN_PASS})
    token = (login or {}).get("token") or ""
    rep.add(code == 200 and bool(token), "管理员登录拿到令牌",
            f"HTTP {code}" + ("" if token else "，但没有 token，后续写接口全会被拒"))
    if not token:
        return rep.done()

    teacher, course = discover(site)
    if not course:
        print("没有可用的真实场景，跳过")
        return 1
    name = course["course_name"]
    orig = course["original_time"]
    week, slot = find_movable_week(site, teacher, course)
    print(f"\n场景：{teacher} / {name} / 原时间 {orig} / 教室 {course['room']}")
    print(f"沙盘说第 {week} 周 {slot} 空着 → 就用它当目标\n")
    if week is None:
        print("找不到可移动的周次（沙盘全程说这一格被占），跳过")
        return 1

    base = current_course(site, teacher, name)
    base_range = (base or {}).get("week_range")
    rep.add(base is not None and base_range, "审批前能查到这门课",
            f"{orig} / 周次 {base_range}")

    # ① 驳回不该动课表
    code, created = http("POST", f"{site}/api/reschedule/requests", {
        "teacher": teacher, "course_name": name, "class_name": course.get("class_name", ""),
        "original_time": orig, "target_week": week, "reason": "探针-驳回路径",
    }, token)
    rid = (created.get("request") or {}).get("id")
    rep.add(code == 200 and rid is not None, "能提交调课申请", f"HTTP {code} id={rid}")
    if rid is not None:
        code, _ = http("POST", f"{site}/api/reschedule/requests/{rid}/approve",
                       {"approve": False, "comment": "探针：驳回"}, token)
        rep.add(code == 200, "能驳回申请", f"HTTP {code}")
        rej = (current_course(site, teacher, name) or {}).get("week_range")
        rep.add(rej == base_range, "驳回后课表一字未动", f"周次仍是 {rej}")

    # ② 云端审批的写路径：/apply-move 必须真的改数据
    key = f"probe-{teacher}-{name}-{week}"
    body = {"teacher": teacher, "course_name": name,
            "class_name": course.get("class_name", ""), "original_time": orig,
            "target_week": week, "key": key}
    code, applied = http("POST", f"{site}/api/reschedule/apply-move", body, token)
    data = (applied or {}).get("data") or {}
    rep.add(code == 200, "调课应用接口可用",
            f"HTTP {code} {str((applied or {}).get('message'))[:76]}")
    rep.add(data.get("applied", 0) > 0, "应用真的移动了排课记录",
            f"applied={data.get('applied')} skipped={data.get('skipped')}"
            f"（0 条 = 审批只是标记，课表没变）"
            + ("｜⚠️ applied=0 且 skipped>0：这条记录多半已被上一次运行/别的探针挪过 —— "
               "先跑 scripts/prepare_demo.py（含 demo/reset）再复跑，不要当成产品缺陷"
               if data.get("applied", 0) == 0 and data.get("skipped") else ""))

    after = (current_course(site, teacher, name) or {}).get("week_range")
    rep.add(after is not None and after != base_range,
            "应用后周次确实变了", f"{base_range} → {after}")

    # ③ 幂等：同一个 key 再打一次，不能再撬动更早的一周
    code2, again = http("POST", f"{site}/api/reschedule/apply-move", body, token)
    d2 = (again or {}).get("data") or {}
    rep.add(code2 == 200 and d2.get("applied", 0) == 0, "重复应用被幂等挡住",
            f"第二次 applied={d2.get('applied')}（>0 = 又把更早一周的课挪了）")
    again_range = (current_course(site, teacher, name) or {}).get("week_range")
    rep.add(again_range == after, "重复应用后课表没有二次变动",
            f"{after} → {again_range}")

    # ④ 写接口不能匿名可调
    code3, _ = http("POST", f"{site}/api/reschedule/apply-move",
                    {"teacher": teacher, "course_name": name,
                     "target_week": week, "key": key})
    rep.add(code3 in (401, 403), "匿名调用应用接口被拒", f"HTTP {code3}（200/400 都算漏了门禁）")

    if args.restore:
        db = os.path.abspath(DEFAULT_DB)
        if os.path.exists(db):
            conn = sqlite3.connect(db)
            n = conn.execute("DELETE FROM applied_moves WHERE idem_key LIKE 'probe-%'").rowcount
            conn.commit()
            conn.close()
            http("POST", f"{site}/api/reschedule/load-default", {})
            print(f"\n已撤掉 {n} 条探针移动并重载排课表")
        else:
            print(f"\n未找到 {db}，跳过恢复（远端库需手动清理）")

    return rep.done()


if __name__ == "__main__":
    sys.exit(main())
