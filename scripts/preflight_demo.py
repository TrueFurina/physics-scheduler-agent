"""演示前自检（现场版）—— 60 秒内回答"能不能上台"

为什么单独写一个
----------------
`run_acceptance.py` 是**回归**（15 项、6 分钟），回答"代码有没有坏"。
现场要回答的是另一组问题：服务起了吗？数据在吗？构建是不是最新的？
LLM 现在通不通、要等多久？审批台有没有待办？任一项不满足，台上就会冷场。
所以这个脚本只做**演示关键路径**的 8 项探测，每项给数字和修复命令。

用法：
    D:/miniconda3_new/python.exe scripts/preflight_demo.py
    D:/miniconda3_new/python.exe scripts/preflight_demo.py --with-llm   # 含 30 秒 LLM 实测
退出码：0 = Go；1 = 有阻塞项。
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SITE = os.environ.get("DEMO_SITE", "http://127.0.0.1:8023")

results = []


def rec(name, ok, detail, fix=""):
    results.append((name, ok, detail, fix))
    print(("  ✅ " if ok else "  ❌ ") + name + (f" —— {detail}" if detail else ""))
    if not ok and fix:
        print("     修复：" + fix)


def _get(path, token=None, timeout=60):
    req = urllib.request.Request(f"{SITE}{path}")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _post(path, payload=None, token=None, timeout=120):
    data = json.dumps(payload or {}).encode()
    req = urllib.request.Request(f"{SITE}{path}", data=data,
                                 headers={"Content-Type": "application/json"})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def check_build():
    idx = os.path.join(REPO, "frontend", "webui", "index.html")
    if not os.path.exists(idx):
        return rec("前端构建可用", False, "frontend/webui/index.html 不存在",
                   "cd frontend && npm run build")
    html = Path(idx).read_text(encoding="utf-8")
    hashes = re.findall(r"assets/(index-[A-Za-z0-9_-]+\.(?:js|css))", html)
    missing = [h for h in hashes
               if not os.path.exists(os.path.join(REPO, "frontend", "webui", "assets", h))]
    newest_src = 0.0
    for root, _, files in os.walk(os.path.join(REPO, "frontend", "src")):
        for f in files:
            newest_src = max(newest_src, os.path.getmtime(os.path.join(root, f)))
    built = min((os.path.getmtime(os.path.join(REPO, "frontend", "webui", "assets", h))
                 for h in hashes if os.path.exists(os.path.join(REPO, "frontend", "webui", "assets", h))),
                default=0)
    stale = built and newest_src > built + 60
    ok = bool(hashes) and not missing and not stale
    rec("前端构建可用（产物与源码同步）", ok,
        f"{len(hashes)} 个入口资源，缺 {len(missing)} 个" + ("，产物落后于源码" if stale else ""),
        "cd frontend && npm run build")


def main():
    global SITE
    ap = argparse.ArgumentParser()
    ap.add_argument("--site", default=SITE)
    ap.add_argument("--with-llm", action="store_true", help="含 LLM 实测（约 30 秒）")
    args = ap.parse_args()
    SITE = args.site

    print("=" * 62)
    print(f"演示前自检 · {SITE}")
    print("=" * 62)

    # 1) 服务与数据
    try:
        h = _get("/health", timeout=10)
        loaded = h["data"].get("loaded")
        n = h["data"].get("total_entries")
        rec("服务在跑 + 安排表已加载", bool(loaded) and n == 433,
            f"entries={n}（期望 433）",
            "curl -X POST " + SITE + "/api/reschedule/load-default")
    except Exception as e:
        rec("服务在跑 + 安排表已加载", False, f"{type(e).__name__}: {e}",
            "cd backend && OPENBLAS_NUM_THREADS=1 python -m uvicorn app.main:app --port 8023")
        print("\n服务都没起，后面不用查了。")
        return 1

    # 2) 三个必看页面的数据接口
    for name, path, judge, _expect in [
        ("首屏教师列表", "/api/reschedule/teachers", lambda d: len(d.get("teachers") or []), "13 位"),
        # data-audit 的载荷在 data 层（曾经在这里取错层级，把"通"报成"不通"）
        ("数据看板", "/api/reschedule/data-audit",
         lambda d: ((d.get("data") or d).get("total_entries"),
                    (d.get("data") or d).get("core_conflict_count")), "433 条"),
        ("并行排课总览", "/api/reschedule/parallel/overview",
         lambda d: len(d.get("data", {}).get("courses") or []), "17 个班"),
    ]:
        try:
            d = _get(path, timeout=60)
            v = judge(d)
            ok = (v == 13) if "教师" in name else (v[0] == 433 if isinstance(v, tuple) else v > 0)
            rec(name, ok, f"{v}")
        except Exception as e:
            rec(name, False, f"{type(e).__name__}: {e}")

    # 3) 主数据五张表
    try:
        ov = _get("/api/reschedule/master/overview", timeout=60)["data"]
        counts = (len(ov["courses"]), len(ov["experiments"]), len(ov["teachers"]),
                  len(ov["rooms"]), len(ov["classes"]))
        rec("主数据五张表", counts[1] >= 20 and counts[3] == 10 and counts[4] >= 17,
            f"课程 {counts[0]} / 项目 {counts[1]} / 教师 {counts[2]} / 场地 {counts[3]} / 班级 {counts[4]}",
            "python scripts/prepare_demo.py")
    except Exception as e:
        rec("主数据五张表", False, f"{type(e).__name__}: {e}")

    # 4) 演示模式：审批台必须能离线进（否则现场要手机验证码）
    try:
        dm = _get("/api/reschedule/demo-mode", timeout=10)
        rec("演示模式已开启（审批台可离线）", bool(dm.get("demo_mode")),
            f"demo_mode={dm.get('demo_mode')}",
            "重启后端时加 DEMO_MODE=1；否则现场需云端登录（手机验证码）")
    except Exception as e:
        rec("演示模式已开启（审批台可离线）", False, f"{type(e).__name__}: {e}")

    # 5) 审批台待办（演示必须有事可做）
    try:
        tok = _post("/api/reschedule/admin-login",
                    {"username": "admin", "password": "admin123"})["token"]
        reqs = _get("/api/reschedule/requests", token=tok, timeout=30).get("requests") or []
        pending = [r for r in reqs if r.get("status") == "pending"]
        rec("审批台有待办工单", len(pending) >= 1,
            f"待审批 {len(pending)} 条 / 共 {len(reqs)} 条",
            "python scripts/prepare_demo.py")
    except Exception as e:
        rec("审批台有待办工单", False, f"{type(e).__name__}: {e}")

    # 5) 求解器可用（挑一个班实跑，2 秒内出结果）
    try:
        t0 = time.time()
        r = _post("/api/reschedule/parallel/solve",
                  {"class_name": "2024级纺织工程1、2班"}, timeout=120)
        rec("求解器实跑（单班）", bool(r.get("success")),
            f"{r.get('status')} {r.get('scheduled')}/{r.get('total_units')}，{time.time() - t0:.1f}s")
    except Exception as e:
        rec("求解器实跑（单班）", False, f"{type(e).__name__}: {e}")

    # 6) 构建新鲜度
    check_build()

    # 7) 截图目录可写（万一现场要留证据）
    shot = os.path.join(REPO, ".tmp", "demo_screens")
    try:
        os.makedirs(shot, exist_ok=True)
        probe_file = os.path.join(shot, ".w")
        open(probe_file, "w").close()
        os.remove(probe_file)
        rec("截图目录可写", True, shot)
    except OSError as e:
        rec("截图目录可写", False, str(e))

    # 8) LLM（可选但建议至少跑一次心里有数）
    if args.with_llm:
        try:
            t0 = time.time()
            r = _post("/api/reschedule/experiment-tutor",
                      {"question": "示波器使用前要注意什么？"}, timeout=90)
            secs = time.time() - t0
            ok = bool(r.get("success")) and bool(r.get("reply"))
            rec("实验规范查询（LLM）", ok,
                f"{secs:.1f}s" + ("（超过 20 秒，讲稿要留够等待时间）" if secs > 20 else ""))
        except Exception as e:
            rec("实验规范查询（LLM）", False, f"{type(e).__name__}: {e}",
                "检查 .env 的 ZHIPU_API_KEY；现场可跳过该项")

    print("\n" + "=" * 62)
    bad = [r for r in results if not r[1]]
    for name, ok, detail, _fix in results:
        print(f"{'PASS' if ok else 'FAIL'}  {name:<28}{detail}")
    print("=" * 62)
    if bad:
        print(f"❌ No-Go：{len(bad)} 项要处理")
        return 1
    print("✅ Go —— 可以上台。开场顺序：跑 prepare_demo → 开 http://127.0.0.1:8023/ → 按演示脚本走")
    return 0


if __name__ == "__main__":
    sys.exit(main())
