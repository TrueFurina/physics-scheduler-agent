"""在满载下跑单个验收探针，并保留**完整输出**（诊断用）。

为什么需要它
------------
`acceptance_under_load.py` 只打印 `run_acceptance.py` stdout 的最后 22 行，
所以当某个探针失败时，**看不到它为什么失败**。2026-11-07 实测：满载 10 项里
`verify_intro_filter` FAIL，但日志里只有汇总表，拿不到断言详情。

本脚本是它的"单探针 + 完整日志"版本，用于定位。不是验收入口，日常不要用它。

用法：
    python scripts/probe_under_load_once.py verify_intro_filter.py [并发数]

清理：broker 900s 自毁；uvicorn 与忙等进程在 finally 里 kill。
"""
import os
import subprocess
import sys
import time
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = sys.argv[1] if len(sys.argv) > 1 else "verify_intro_filter.py"
N = int(sys.argv[2]) if len(sys.argv) > 2 else 28
PORT = 8023
BURN = "import time\nt=time.time()+900\nwhile time.time()<t:\n    pass\n"

env = dict(os.environ, DEMO_MODE="1", ADMIN_PASS="admin123",
           OPENBLAS_NUM_THREADS="1", SITE=f"http://127.0.0.1:{PORT}")

burners = [subprocess.Popen([sys.executable, "-c", BURN],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
           for _ in range(N)]
print(f"[load-once] 已起 {N} 个忙等进程", flush=True)

server = subprocess.Popen(
    [sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(PORT)],
    cwd=os.path.join(REPO, "backend"), env=env,
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)

rc = 1
try:
    ready = False
    t0 = time.time()
    for _ in range(180):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/health", timeout=5) as r:
                if r.status == 200:
                    ready = True
                    break
        except Exception:
            time.sleep(1)
    print(f"[load-once] health={'OK' if ready else 'TIMEOUT'} after {time.time()-t0:.0f}s", flush=True)
    if not ready:
        raise SystemExit(2)

    # 探针要求"已 load-default"。满载下这一步本身也可能慢，所以单独计时。
    t1 = time.time()
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{PORT}/api/reschedule/load-default",
            data=b"{}", headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=600) as r:
            body = r.read().decode(errors="replace")
        print(f"[load-once] load-default {time.time()-t1:.0f}s: {body[:160]}", flush=True)
    except Exception as e:
        print(f"[load-once] load-default 失败: {e}", flush=True)

    names = [x for x in SCRIPT.split(",") if x]
    rc = 0
    for name in names:
        print(f"[load-once] ==== 运行 {name} ====", flush=True)
        r = subprocess.run([sys.executable, os.path.join("scripts", name)],
                           cwd=REPO, env=env, capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        print("---- stdout ----", flush=True)
        print(r.stdout, flush=True)
        if r.stderr.strip():
            print("---- stderr ----", flush=True)
            print(r.stderr[-4000:], flush=True)
        print(f"[load-once] {name} rc={r.returncode}", flush=True)
        if r.returncode != 0:
            rc = r.returncode
finally:
    server.kill()
    for p in burners:
        p.kill()
    print("[load-once] 服务与忙等进程已清理", flush=True)

sys.exit(rc)
