"""验收一键跑（真实交付的回归入口）

为什么需要
----------
探针有 10 个（UI 5 个 + Python 5 个）、核查器 4 个、独立校验器 1 个，散在
`npm run probe*` 和 `python scripts/*.py` 两套入口里。此前只能人肉按记忆顺序跑，
而且**顺序错了会互相污染**：`verify_graybox` 会把 6 条清单留在进程里，紧随其后的
`verify_intro_filter` 断言 `pending_total == 1` 就会读到 7 而误报 No-Go（实测踩过）。
这类"探针互相污染"的成本很高：看起来是产品红，实际是测试卫生问题。

本脚本按固定顺序跑，并在关键节点做状态复位（load-default + 清空清单），
最后打印一张表：每项 PASS/FAIL、耗时。退出码 0 = 全部通过。

用法：
    D:/miniconda3_new/python.exe scripts/run_acceptance.py
    D:/miniconda3_new/python.exe scripts/run_acceptance.py --skip-audit   # 跳过 2.5 分钟的全表校验
    D:/miniconda3_new/python.exe scripts/run_acceptance.py --skip-ui      # 无浏览器环境
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable
FRONTEND = os.path.join(REPO, "frontend")

SITE = "http://127.0.0.1:8023"


def _req(path, method="GET", payload=None, token=None, timeout=180):
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(f"{SITE}{path}", data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def reset_state(verbose=True):
    """复位到干净基线：重新读表 + 清空上传清单。"""
    _req("/api/reschedule/load-default", method="POST", payload={}, timeout=180)
    tok = _req("/api/reschedule/admin-login", method="POST",
               payload={"username": "admin", "password": "admin123"})["token"]
    _req("/api/reschedule/master/tasks", method="DELETE", token=tok, timeout=60)
    if verbose:
        print("  ↺ 状态已复位（安排表已重载、清单已清空）")


def _resolve(cmd):
    """Windows 上 npm 是 npm.cmd；找不到命令时不该把整个验收脚本崩掉。"""
    exe = shutil.which(cmd[0])
    if not exe and cmd[0] == "npm":
        exe = shutil.which("npm.cmd")
    return [exe or cmd[0]] + list(cmd[1:])


def run(cmd, cwd, timeout, env=None):
    t0 = time.time()
    cmd = _resolve(cmd)
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout, env=env)
    out = (proc.stdout or "") + (proc.stderr or "")
    tail = "\n".join([ln for ln in out.strip().splitlines() if ln.strip()][-3:])
    return proc.returncode, round(time.time() - t0, 1), tail


def main():
    global SITE
    ap = argparse.ArgumentParser()
    ap.add_argument("--site", default=SITE)
    ap.add_argument("--skip-audit", action="store_true", help="跳过 audit_conflicts（约 2.5 分钟）")
    ap.add_argument("--skip-ui", action="store_true", help="跳过浏览器探针（UI 5 项）")
    ap.add_argument("--skip-tests", action="store_true", help="跳过 pytest / verify（已在别处跑过）")
    args = ap.parse_args()
    SITE = args.site

    results = []

    # 探针脚本各自带默认 SITE（线上冻结站），验收必须显式指到被测站点，
    # 否则会"在本地验收、实际打线上"，读到旧功能还以为是产品红（实测踩过）。
    probe_env = dict(os.environ, SITE=SITE)

    def step(name, cmd, cwd=REPO, timeout=1800, reset=False, must=None, env=None):
        if reset:
            reset_state()
        print(f"\n▶ {name}")
        try:
            code, secs, tail = run(cmd, cwd, timeout, env=env)
        except subprocess.TimeoutExpired:
            results.append((name, False, timeout, "超时"))
            print(f"  ❌ 超时（{timeout}s）")
            return
        except OSError as e:
            results.append((name, False, 0, f"命令无法执行：{e}"))
            print(f"  ❌ 命令无法执行：{e}")
            return
        ok = code == 0
        if ok and must and must not in tail:
            ok = False
            tail += f"\n(输出缺少判据：{must})"
        results.append((name, ok, secs, tail))
        print(("  ✅ " if ok else "  ❌ ") + f"{secs}s")
        for line in tail.splitlines():
            print("     " + line)

    # 0) 服务可达
    try:
        health = _req("/health", timeout=15)
        print(f"服务：{health.get('status')} · 数据 {health['data']['total_entries']} 条")
        if not health["data"].get("loaded"):
            reset_state()
    except Exception as e:
        print(f"❌ 服务不可达：{e}")
        return 1

    # 1) 单测与类型检查
    if not args.skip_tests:
        # pytest 默认把临时目录丢在系统 %TEMP%\pytest-of-*，一次跑完 400+ 用例会有
        # 近 500 个残留文件，被环境的安全钩子判成"批量删除"而中断（表现为 100% 全绿
        # 但退出码非 0，前面已踩）。指到仓库 .tmp 下的独立 basetemp 可稳定退出 0。
        basetemp = os.path.join(".tmp", "pytest_bt_%d" % int(time.time()))
        step("pytest（后端全量）",
             [PY, "-m", "pytest", "backend/tests", "-q",
              "--basetemp=" + basetemp, "-p", "no:cacheprovider"],
             timeout=1800)
        step("verify（前端 tsc + vitest）", ["npm", "run", "verify"], cwd=FRONTEND, timeout=900)

    # 2) 独立核查器（写入类探针自清理 / 显式 --cleanup）
    #
    # 2a) 静态审计（不需要服务，`--skip-ui` 时也应跑）
    # 为什么它在验收里（2026-10-29）：`audit_async_blocking.py` 是 R-81/R-84 修
    # 「重活跑在事件循环上」时建立的判据，但一直**只靠人手工跑**。本轮把它改成可进门的形态
    # （`suspicious` 必须为 0；零 await 轻查询与基线比对、只拦新增），并接进来。
    # 这类"我建了判据却没人跑"是最典型的自欺：脚本在、结论对，但永远不会变红。
    step("audit_async_blocking（事件循环阻塞审计）",
         [PY, "scripts/audit_async_blocking.py", "backend/app/api/reschedule.py"])

    step("verify_intro_filter（绪论/考试不进求解器）",
         [PY, "scripts/verify_intro_filter.py"], reset=True)
    step("verify_master_roundtrip（台账可导回 + 清理）",
         [PY, "scripts/verify_master_roundtrip.py", "--cleanup"], reset=True)
    step("verify_graybox（灰箱方案 × 真实数据）",
         [PY, "scripts/verify_graybox.py"], reset=True)
    step("verify_school_plan（全校方案 × 真实数据）",
         [PY, "scripts/verify_school_plan.py"], reset=True)

    # 3) UI 探针
    if not args.skip_ui:
        for name, script in [
            ("probe_ui_flow（主流程 16 项）", "probe_ui_flow.cjs"),
            ("probe_reschedule_flow（调课 11 项）", "probe_reschedule_flow.cjs"),
            ("probe_faults（故障注入 8 项）", "probe_faults.cjs"),
            ("probe_ui_newviews（新视图 15 项）", "probe_ui_newviews.cjs"),
        ]:
            step(name, ["node", f"../scripts/{script}"], cwd=FRONTEND,
                 timeout=1200, reset=True, env=probe_env)

    # 4) Python 探针（落地/网格/导入/审批）
    step("probe_land_plan（落地采纳 19 项）", [PY, "scripts/probe_land_plan.py"],
         timeout=900, reset=True)
    step("probe_schedule_grid（网格 7 项）", [PY, "scripts/probe_schedule_grid.py"],
         timeout=600, reset=True)
    step("probe_import_wizard（导入向导 15 项）", [PY, "scripts/probe_import_wizard.py"],
         timeout=900, reset=True)
    step("probe_approval_chain（审批链 11 项）",
         [PY, "scripts/probe_approval_chain.py", "--restore"], timeout=900, reset=True)

    # 4b) 真重启闭环（进程 A 写 → 杀掉 → 进程 B 读）
    #
    # 为什么它必须在验收里（2026-10-29）：这个探针早就写好了，但一直**只靠人记得跑**。
    # 它测的是"审批通过 → 进程重启 → 课表还在吗"，即 R-68-A / R-18 那一族
    # 「内存态一切正常、只有重启才暴露」的缺陷。而 R-78（`--skip-audit` 藏了 20 小时红灯）
    # 与 R-79（`--skip-ui` 藏了"断言改瞎"）的教训正是：**不进门的检查等于没检查**。
    #
    # 它自己起 8028/8029 两个 uvicorn 子进程，与本脚本用的 8023 不冲突；
    # 开工前备份 `requests.db`、`finally` 里还原，异常也不留脏态。
    step("probe_restart_replay（真重启重放）",
         [PY, "scripts/probe_restart_replay.py"], timeout=900)

    # 5) 独立冲突校验器（最慢，放最后）
    if not args.skip_audit:
        step("audit_conflicts（全 16 周独立校验）", [PY, "scripts/audit_conflicts.py"], timeout=1800)

    # 汇总
    print("\n" + "=" * 64)
    print(f"{'项目':<44}{'结果':<8}{'耗时'}")
    for name, ok, secs, _ in results:
        print(f"{name:<44}{'PASS' if ok else 'FAIL':<8}{secs}s")
    failed = [r for r in results if not r[1]]
    print("=" * 64)
    if failed:
        print(f"❌ {len(failed)}/{len(results)} 项未通过：{[r[0] for r in failed]}")
        return 1
    print(f"✅ 全部通过（{len(results)} 项）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
