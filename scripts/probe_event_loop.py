"""事件循环阻塞探针：`async def` 里做同步 CPU 工作，会把整个事件循环占住多久？

用法（仓库根）：

    OPENBLAS_NUM_THREADS=1 python scripts/probe_event_loop.py

原理：不需要起 HTTP 服务 —— 在同一事件循环里并发跑「被测接口」和「每 10ms 醒一次的
心跳任务」。心跳的**实际间隔与期望间隔之差**，就是别的请求要排队的时间。

判据：心跳最长滞后应当远小于被测接口耗时（正常 < 50ms）。若滞后 ≈ 接口耗时，
说明这段工作跑在事件循环线程上 —— 期间 `/health`、别的教师的页面全部排队。

R-70 用它抓到的：导出族 5 个接口各占住 0.07–1.3 秒（心跳最长 834ms）；
修复后同一张表跑出来是 8–22ms。
"""
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
os.chdir(os.path.join(os.path.dirname(__file__), "..", "backend"))

from app.api import reschedule as R  # noqa: E402

R._ensure_loaded()
R._ensure_master_data()
print(f"entries={len(R._db.entries)}", flush=True)


async def heartbeat(stop: asyncio.Event, gaps: list, tick=0.01):
    while not stop.is_set():
        t0 = time.perf_counter()
        await asyncio.sleep(tick)
        gaps.append(((time.perf_counter() - t0) - tick) * 1000)


async def measure(label, coro_factory):
    stop = asyncio.Event()
    gaps: list = []
    hb = asyncio.create_task(heartbeat(stop, gaps))
    await asyncio.sleep(0.05)  # 先跑几拍，拿到空载基线
    base = max(gaps) if gaps else 0.0
    gaps.clear()
    t0 = time.perf_counter()
    await coro_factory()
    cost = (time.perf_counter() - t0) * 1000
    stop.set()
    await hb
    worst = max(gaps) if gaps else 0.0
    over = sum(1 for g in gaps if g > 50)
    print(f"{label:28s} 耗时{cost:7.0f}ms | 心跳最长被拖 {worst:7.0f}ms | "
          f"被拖>50ms 的拍数 {over}/{len(gaps)} | 空载基线 {base:5.1f}ms", flush=True)
    return worst


async def main():
    for i in range(4):
        await measure(f"export_class_schedule #{i}", R.export_class_schedule)
    for i in range(2):
        await measure(f"export_room_schedule #{i}", R.export_room_schedule)
    await measure("export_master_data", R.export_master_data)
    await measure("export_master_workbook", R.export_master_workbook)
    await measure("stats", R.stats)
    await measure("data_audit", R.data_audit)
    await measure("schedule_grid(w=3)", lambda: R.schedule_grid(week=3))
    await measure("parallel_overview", R.parallel_overview)


asyncio.run(main())
