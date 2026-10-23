"""落地闭环探针：从"全校一键排课方案"到"安排表真的换了"，再到"能原样撤回来"。

为什么需要单独一把尺子：
  排课引擎的正确性由 verify_school_plan.py（独立重解）担保，但**落地**是另一条
  写路径：方案 → 差异预览 → 写回安排表 → 撤销。这条路上任何一环断了，界面都
  照样显示"操作成功"——预览说改了 401 条、采纳返回 200，可课表一个字节没变
  （R-36「审批通过课表没变」同族）。本探针拿**真实表 + 真实全校方案**走一遍
  真实接口，然后**回到数据本身**（data-audit / sqlite 账本）看它有没有真变。

与 probe_approval_chain.py 的分工：那条管"调课审批"写路径，这条管"方案落地"写路径。

默认行为是**测完自动撤销**（与审批探针的 --restore 相反，理由见 --keep 说明）。

用法：
  D:/miniconda3_new/python.exe scripts/probe_land_plan.py
  D:/miniconda3_new/python.exe scripts/probe_land_plan.py --site http://127.0.0.1:8023
  D:/miniconda3_new/python.exe scripts/probe_land_plan.py --keep   # 测完不撤销
"""
import argparse
import json
import os
import sqlite3
import urllib.error
import urllib.request

DEFAULT_SITE = "http://127.0.0.1:8023"
ADMIN_USER = "admin"
ADMIN_PASS = "admin123"
HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(HERE, "..", "backend", "app", "data", "requests.db")


def http(method: str, url: str, payload=None, token: str = "", timeout: int = 120):
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
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
        print(f"{'PASS' if ok else 'FAIL'}  {name}  — {detail}")

    def done(self):
        bad = [r for r in self.rows if not r[0]]
        print()
        print(f"{len(self.rows) - len(bad)}/{len(self.rows)} 通过")
        for _, n, d in bad:
            print(f"  未通过：{n} — {d}")
        return 0 if not bad else 1


def audit(site: str) -> dict:
    _, resp = http("GET", f"{site}/api/reschedule/data-audit")
    return (resp or {}).get("data") or {}


def core(a: dict):
    """三类核心冲突数（场地/教师/同班同组）——方案不许引入新的。"""
    return (a.get("room_conflict_count"), a.get("teacher_conflict_count"),
            a.get("group_conflict_count"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--site", default=DEFAULT_SITE)
    ap.add_argument("--keep", action="store_true",
                    help="测完不撤销。默认撤销：落地会改写 400+ 行安排表，"
                         "留着会污染后续所有探针/核查的基线（parallel 47→53），"
                         "所以只有明确想观察落地态时才用")
    args = ap.parse_args()
    site = args.site.rstrip("/")
    rep = Report()

    # ① 管理员登录（写接口全都要）
    code, login = http("POST", f"{site}/api/reschedule/admin-login",
                       {"username": ADMIN_USER, "password": ADMIN_PASS})
    token = (login or {}).get("token") or ""
    rep.add(code == 200 and bool(token), "管理员登录拿到令牌",
            f"HTTP {code}" + ("" if token else "，但没有 token，后续写接口全会被拒"))
    if not token:
        return rep.done()

    # ② 清基线：上一轮 --keep 留下的落地必须先撤掉，否则"数据真的换了"无从判断
    _, st = http("GET", f"{site}/api/reschedule/parallel/land/status")
    if st.get("landed"):
        code, un = http("POST", f"{site}/api/reschedule/parallel/land/undo", {}, token)
        rep.add(code == 200, "清掉历史落地（获得干净基线）",
                f"HTTP {code}，恢复 {un.get('restored_entries')} 条"
                f"（连带回滚落地后调课 {un.get('rolled_back_moves')} 次）")

    # ③ 加载真实表 + 记基线
    code, _ = http("POST", f"{site}/api/reschedule/load-default")
    rep.add(code == 200, "加载默认安排表", f"HTTP {code}")
    base = audit(site)
    rep.add(bool(base), "拿到基线体检",
            f"{base.get('total_entries')} 条安排，核心冲突 "
            f"room={base.get('room_conflict_count')} teacher={base.get('teacher_conflict_count')} "
            f"group={base.get('group_conflict_count')}，同班分批 {base.get('parallel_group_count')} 处")

    # ④ 真实全校一键方案（约 15–40 秒，CP-SAT 逐班求解）
    code, plan = http("POST", f"{site}/api/reschedule/parallel/solve-all", {}, timeout=300)
    rows = (plan or {}).get("schedule") or []
    all_have_class = all(r.get("class_name") for r in rows)
    ok_plan = (code == 200 and plan.get("success")
               and plan.get("classes_ok") == plan.get("classes_total")
               and len(rows) > 300 and all_have_class)
    rep.add(ok_plan, "全校一键拿到真实方案",
            f"HTTP {code}，{plan.get('classes_ok')}/{plan.get('classes_total')} 班，"
            f"{len(rows)} 行" + ("" if all_have_class else "，有行缺 class_name！"))
    if not ok_plan:
        return rep.done()

    # ⑤ 差异预览（免鉴权）：四类账自洽 + 长实验警告 + **不写入**
    code, pv = http("POST", f"{site}/api/reschedule/parallel/land/preview",
                    {"schedule": rows})
    uncovered_rows = sum(len(u.get("rows") or []) for u in pv.get("uncovered") or [])
    expect_after = (pv.get("untouched_nonsched") or 0) + uncovered_rows + (pv.get("plan_rows") or 0)
    longs = pv.get("long_sessions") or []
    trunc_warn = any("截短" in w for w in pv.get("warnings") or [])
    rep.add(code == 200 and pv.get("has_changes"), "差异预览可用",
            f"HTTP {code}，新增 {len(pv.get('added') or [])} / 调整 {len(pv.get('changed') or [])} / "
            f"原样 {pv.get('unchanged_units')} / 未覆盖 {len(pv.get('uncovered') or [])} / "
            f"绪论考试原样保留 {pv.get('untouched_nonsched')} 条")
    rep.add(pv.get("after_entries") == expect_after, "预览账目自洽（after = 非可排 + 未覆盖 + 方案行）",
            f"after_entries={pv.get('after_entries')} vs 推算 {expect_after}"
            + ("" if pv.get("after_entries") == expect_after else "（对不上说明 diff 漏记了一类行）"))
    rep.add(bool(longs) and trunc_warn, "长实验截短被单列警告",
            f"{len(longs)} 个单元同周同天地占多个时段，方案每单元只排一个时段；"
            + ("有截短警告" if trunc_warn else "**没有截短警告**（静默砍课！）"))
    _, st2 = http("GET", f"{site}/api/reschedule/parallel/land/status")
    base2 = audit(site)
    rep.add(not st2.get("landed") and base2.get("total_entries") == base.get("total_entries"),
            "预览不写入任何数据",
            f"landed={st2.get('landed')}，安排表 {base2.get('total_entries')} 条"
            f"（基线 {base.get('total_entries')}）")

    # ⑥ 匿名采纳必须被拒（写接口不能裸奔）
    code, _ = http("POST", f"{site}/api/reschedule/parallel/land", {"schedule": rows})
    rep.add(code in (401, 403), "匿名采纳被门禁挡住", f"HTTP {code}（200/400 都算漏了门禁）")

    # ⑦ 采纳：diff 现算，且与预览一致（预览与采纳之间没有别的写入）
    code, cm = http("POST", f"{site}/api/reschedule/parallel/land",
                    {"schedule": rows, "note": "probe-land"}, token, timeout=180)
    same = (cm.get("after_entries") == pv.get("after_entries")
            and len(cm.get("added") or []) == len(pv.get("added") or [])
            and len(cm.get("changed") or []) == len(pv.get("changed") or []))
    rep.add(code == 200 and cm.get("success"), "管理员采纳成功",
            f"HTTP {code}，after_entries={cm.get('after_entries')}，landed_at={cm.get('landed_at')}")
    rep.add(same, "采纳时重算的 diff 与预览一致",
            f"调整 {len(cm.get('changed') or [])} 条 / 新增 {len(cm.get('added') or [])} 条"
            + ("" if same else "（与预览对不上——中间有东西改了表，或 diff 不稳定）"))

    # ⑧ 回到数据本身：安排表真的换了，且方案没有引入新冲突
    aft = audit(site)
    rep.add(aft.get("total_entries") == cm.get("after_entries"), "安排表条数 = 采纳返回值",
            f"体检 {aft.get('total_entries')} 条 vs 返回 {cm.get('after_entries')} 条")
    rep.add(core(aft) == core(base), "方案不引入新的核心冲突",
            f"落地前 room/teacher/group={core(base)} → 落地后 {core(aft)}"
            + ("" if core(aft) == core(base) else "（求解器三方互斥被破坏了？）"))
    rep.add(aft.get("parallel_group_count") != base.get("parallel_group_count"),
            "安排表内容真的换了（同班分批格位数变化）",
            f"{base.get('parallel_group_count')} → {aft.get('parallel_group_count')}"
            + ("" if aft.get("parallel_group_count") != base.get("parallel_group_count")
               else "（一字未变＝'批了但没生效'）"))

    # ⑨ 落盘账本：sqlite 里真有这条记录（否则重启即丢，重放无从谈起）
    db = os.path.abspath(DEFAULT_DB)
    n_ledger, latest = -1, ""
    if os.path.exists(db):
        try:
            con = sqlite3.connect(db)
            n_ledger = con.execute("SELECT COUNT(*) FROM landed_plans").fetchone()[0]
            latest = con.execute(
                "SELECT MAX(landed_at) FROM landed_plans").fetchone()[0] or ""
            con.close()
        except sqlite3.Error as e:
            print(f"（读账本失败：{e}）")
    rep.add(n_ledger >= 1 and latest == cm.get("landed_at"),
            "落地方案落盘（landed_plans 账本）",
            f"{n_ledger} 条记录，最新 landed_at={latest}"
            + ("" if n_ledger >= 1 else "（内存改了但没落盘，重启即丢）"))

    # ⑩ 撤销：快照原样恢复 + 状态清空 + 再撤销报 404
    if args.keep:
        print("\n--keep：保留落地态，跳过撤销验证")
        return rep.done()
    code, un = http("POST", f"{site}/api/reschedule/parallel/land/undo", {}, token)
    back = audit(site)
    _, st3 = http("GET", f"{site}/api/reschedule/parallel/land/status")
    rep.add(code == 200 and un.get("restored_entries") == base.get("total_entries"),
            "撤销恢复落地前快照",
            f"HTTP {code}，恢复 {un.get('restored_entries')} 条"
            f"（基线 {base.get('total_entries')} 条）")
    rep.add(back.get("total_entries") == base.get("total_entries")
            and back.get("parallel_group_count") == base.get("parallel_group_count")
            and core(back) == core(base),
            "撤销后体检回到基线",
            f"{back.get('total_entries')} 条 / 同班分批 {back.get('parallel_group_count')} 处 / "
            f"核心冲突 {core(back)}")
    rep.add(not st3.get("landed"), "撤销后落地状态清空", f"landed={st3.get('landed')}")
    code, _ = http("POST", f"{site}/api/reschedule/parallel/land/undo", {}, token)
    rep.add(code == 404, "无可撤销时再撤销报 404", f"HTTP {code}（200＝把更早的快照也弹了）")

    return rep.done()


if __name__ == "__main__":
    raise SystemExit(main())
