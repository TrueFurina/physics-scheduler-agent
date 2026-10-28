"""满载跑完整验收链路：压满 CPU → 起服务 → `run_acceptance.py` 8 项。

为什么需要（三轮都是这么发现问题的）
----------------------------------
R-74：满载下 `test_real_table_full_coverage` 红 —— 贪心兜底把段排进绪论格。
R-75：满载下 `test_auto_schedule...` 红 —— 单批 solve() 没有兜底。
R-76：满载下 `/parallel/solve-all` 只排 358/410，接口却报"17/17 班排定"。
共同点：**机器忙时才走兜底/超时路径**，空闲跑一百遍都是绿的。

`scripts/soak_tests.py` 压的是**单元测试**；本脚本压的是**验收链路**（8 个探针，
含全校方案、落地采纳、导入向导、审批链）。演示那天机器忙不忙，看这个。

用法（仓库根）
--------------
    python scripts/acceptance_under_load.py [并发忙等进程数]   # 默认 28

忙等进程 900s 自毁、uvicorn 由本进程 kill，异常退出也不留残留。
退出码：与 `run_acceptance.py` 一致（0 = 8 项全过）。
"""
import os
import subprocess
import sys
import time
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
N = int(sys.argv[1]) if len(sys.argv) > 1 else 28
BURN = "import time\nt=time.time()+900\nwhile time.time()<t:\n    pass\n"
PORT = 8023

burners = [subprocess.Popen([sys.executable, "-c", BURN],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
           for _ in range(N)]
print(f"[acc-load] 已起 {N} 个忙等进程", flush=True)

env = dict(os.environ, DEMO_MODE="1", ADMIN_PASS="admin123", OPENBLAS_NUM_THREADS="1")
server = subprocess.Popen(
    [sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(PORT)],
    cwd=os.path.join(REPO, "backend"), env=env,
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

rc = 1
try:
    t0 = time.time()
    ready = False
    for _ in range(180):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/health", timeout=3) as r:
                ready = r.status == 200
                if ready:
                    break
        except Exception:
            time.sleep(1)
    print(f"[acc-load] health={'OK' if ready else 'TIMEOUT'} "
          f"after {time.time() - t0:.0f}s", flush=True)
    if not ready:
        raise SystemExit(2)

    t1 = time.time()
    r = subprocess.run(
        [sys.executable, "scripts/run_acceptance.py",
         "--skip-tests", "--skip-ui", "--skip-audit"],
        cwd=REPO, env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace")
    dt = time.time() - t1
    print("\n".join(r.stdout.strip().splitlines()[-22:]), flush=True)
    if r.returncode != 0:
        print("---- stderr ----", flush=True)
        print("\n".join((r.stderr or "").strip().splitlines()[-30:]), flush=True)
    print(f"[acc-load] rc={r.returncode} acceptance_elapsed={dt:.0f}s", flush=True)
    rc = r.returncode
finally:
    server.kill()
    for p in burners:
        p.kill()
    print("[acc-load] 服务与忙等进程已清理", flush=True)

sys.exit(rc)
