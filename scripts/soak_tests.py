"""Soak：CPU 饱和下跑全量后端测试，找出"只在机器忙时红"的用例。

为什么需要（两次都靠它抓到真 bug）
---------------------------------
R-74：`test_real_table_full_coverage` 平时绿、满载必红 —— 红的是"贪心兜底把段排进
      绪论格"，不是覆盖率。闲时 CP-SAT 能解完，根本不走兜底路径。
R-75：`test_real_table_plan_has_zero_collisions_with_existing_entries` 满载红在
      `success is False` —— 单批 `solve()` 没有确定性兜底，机器一忙就对外报"排课失败"。
结论：**"负载敏感"不是判词，是线索**。这类缺陷在空闲机器上永远测不出来。

用法（仓库根）
--------------
    python scripts/soak_tests.py [并发忙等进程数]   # 默认 28

忙等进程自带 900s 自毁：父进程被强杀也不会留下永久残留（Windows 上 `kill` 子进程
不一定生效，这点踩过两次 —— 残留的忙等会把后续所有测试拖慢一倍）。
退出码：与 pytest 一致（0 = 全绿）。
"""
import os
import subprocess
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
N = int(sys.argv[1]) if len(sys.argv) > 1 else 28

# 纯忙等，不占内存、不写磁盘：只抢 CPU，模拟"教秘的机器同时在跑别的东西"
BURN = "import time\nt=time.time()+900\nwhile time.time()<t:\n    pass\n"

burners = [
    subprocess.Popen([sys.executable, "-c", BURN],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(N)
]
print(f"[soak] 已起 {N} 个忙等进程，3 秒后开跑", flush=True)
rc = 1
try:
    time.sleep(3)
    env = dict(os.environ, OPENBLAS_NUM_THREADS="1")
    t0 = time.time()
    r = subprocess.run(
        [sys.executable, "-m", "pytest", "backend/tests", "-q", "-p", "no:cacheprovider"],
        cwd=REPO, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace")
    dt = time.time() - t0
    print("\n".join(r.stdout.strip().splitlines()[-40:]), flush=True)
    if r.returncode != 0:
        print("---- stderr ----", flush=True)
        print("\n".join((r.stderr or "").strip().splitlines()[-40:]), flush=True)
    print(f"[soak] rc={r.returncode} elapsed={dt:.1f}s", flush=True)
    rc = r.returncode
finally:
    for p in burners:
        p.kill()
    print("[soak] 忙等进程已清理", flush=True)

sys.exit(rc)
