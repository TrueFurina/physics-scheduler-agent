"""只读接口延迟基线：每项跑 N 次，报 min/median/p95 与响应字节数。

用法（需先起服务）：

    python scripts/bench_endpoints.py http://127.0.0.1:8025 7

R-70 用它把"导出接口比其他只读接口高一个量级"这件事先量出来（837ms vs 16–40ms），
再去 cProfile 定位到 `wb.save()` —— 顺序很重要：先量，再猜。
"""
import statistics
import sys
import time
import urllib.error
import urllib.request

SITE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8023"
N = int(sys.argv[2]) if len(sys.argv) > 2 else 7

GETS = [
    "/health",
    "/api/reschedule/teachers",
    "/api/reschedule/stats",
    "/api/reschedule/data-audit",
    "/api/reschedule/master/overview",
    "/api/reschedule/schedule-grid?week=3",
    "/api/reschedule/parallel/overview",
    "/api/reschedule/status",
    "/api/reschedule/master-status",
    "/api/reschedule/parallel/land/status",
    "/api/reschedule/requests",
    "/api/reschedule/export/class-schedule",
    "/api/reschedule/export/room-schedule",
    "/api/reschedule/export/master-workbook",
]


def call(path):
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(SITE + path, timeout=180) as r:
            body = r.read()
            return (time.perf_counter() - t0) * 1000, len(body), r.status
    except urllib.error.HTTPError as e:
        body = e.read()
        return (time.perf_counter() - t0) * 1000, len(body), e.code


print(f"{'path':46s} {'min':>8s} {'p50':>8s} {'p95':>8s} {'bytes':>10s}  st")
print("-" * 92)
rows = []
for p in GETS:
    times, size, st = [], 0, 0
    for _ in range(N):
        ms, size, st = call(p)
        times.append(ms)
    times.sort()
    p50 = statistics.median(times)
    p95 = times[int(len(times) * 0.95) - 1] if len(times) > 1 else times[0]
    rows.append((p, times[0], p50, p95, size, st))
    print(f"{p:46s} {times[0]:8.1f} {p50:8.1f} {p95:8.1f} {size:10d}  {st}")

print()
print("总耗时(串行一次) ms:", round(sum(r[2] for r in rows), 1))
