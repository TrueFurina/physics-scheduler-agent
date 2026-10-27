"""导出接口不得占住事件循环（R-70）

病是什么
--------
5 个导出接口是 `async def`，但函数体里**一个 await 都没有** —— 建表 + openpyxl
序列化（实测 65–830ms）全部跑在事件循环线程上。这期间任何请求（`/health`、
别的教师点开的页面、前端轮询）都只能排队等着。这是 R-19 的同一族缺陷：R-19 当时
修了 5 处 `run_in_threadpool`，但导出这一族是之后（R-46/R-59/R-69）才加的，
没有跟着改，于是同一个病以新面孔回来了。

为什么两道锁
------------
1. **结构锁**：建表工作必须经 `run_in_threadpool` 派发。缺陷版永不调用 → 直接红。
   探针记下被派发的函数名，同时**用桩数据顶掉真实建表** —— 本组只验"派发去了哪里"。
2. **行为锁**：导出进行中，心跳必须明显比内联写法更稳。判据用**比值**而不是绝对毫秒
   （同一台机器上 22ms 与 156ms 都出现过，那是 GIL 被 C 写入占住的正常波动）。
   测试会先跑一遍内联写法当基准；基准不成立就判测试本身失效，不给假绿（R-40 铁律）。

⚠️ 两条锁都**刻意不真建表**，理由有两条：
- 真建表会制造大量 openpyxl 临时文件（每写一个 sheet 建一个再删一个）。本机沙箱有
  「单次工具调用内删除超过 50 个文件即判批量删除」的守卫（`SAFE_DELETE_BULK_...`，
  抛 SystemExit），全量跑的基线已用掉约 45 次 —— 多出来的删除会把**后面**的用例
  打成 `ERROR at setup`（实测：test_week_bounds 5 条全红，单独跑却 11 passed）。
- 真建表要 1–2 秒/次，而这里要证明的是"重活有没有离开事件循环"，用 0.5 秒纯 CPU
  忙等等价，且快得多、零副作用。

⚠️ 夹具必须把 `_master_data` / `_schedule_fp` 一起 monkeypatch：
导出内部会调 `_ensure_master_data()`，它会把**全局**主数据按合成表反推成假项目/假班级。
不隔离的话，跑完本文件后其它用例读到的是脏主数据。
"""

import asyncio
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import app.api.reschedule as api
from app.master_data import MasterData
from app.utils.excel_parser import ScheduleDatabase, ScheduleEntry

WD = ["周一", "周二", "周三", "周四", "周五"]
SLOTS = ["567", "678", "MORNING"]

STUB_XLSX = b"PK\x03\x04"  # 只要求"像字节"，不要求是合法 xlsx


def _entries(n, classes=4):
    return [
        ScheduleEntry(
            week=(i % 16) + 1,
            weekday=WD[i % len(WD)],
            time_slot=SLOTS[i % len(SLOTS)],
            class_name=f"物理{i % classes:02d}班",
            group=f"第{(i % 4) + 1}组",
            experiment=f"实验项目{i % 25}",
            room=f"实验室{(i % 10) + 1}",
            teacher=f"教师{i % 13}",
        )
        for i in range(n)
    ]


@pytest.fixture()
def small_db(monkeypatch):
    db = ScheduleDatabase()
    db.entries = _entries(60)
    db.loaded = True
    monkeypatch.setattr(api, "_db", db)
    # 关键：别让 `_ensure_master_data()` 把全局主数据改成合成表反推的结果
    monkeypatch.setattr(api, "_master_data", MasterData())
    monkeypatch.setattr(api, "_schedule_fp", None)
    return db


def _rows(n=40):
    return [
        {"week": e.week, "weekday": e.weekday, "time_slot": e.time_slot, "room": e.room,
         "teacher": e.teacher, "project": e.experiment, "class_name": e.class_name}
        for e in _entries(n)
    ]


HEAVY_EXPORTS = [
    ("班级课表", lambda: api.export_class_schedule()),
    ("场地课表", lambda: api.export_room_schedule()),
    ("统计台账", lambda: api.export_master_data()),
    ("可编辑台账", lambda: api.export_master_workbook()),
    ("自动排课初稿", lambda: api.auto_schedule_export(
        api.AutoScheduleExportRequest(schedule=_rows()))),
]


def test_all_heavy_exports_dispatch_to_threadpool(monkeypatch, small_db):
    """结构锁：5 个导出都必须把建表交给线程池。缺陷版（内联建表）永不调用 → 红。

    五个接口合并成一条用例（而不是 parametrize 成五条）是为了少占 tmp 目录 ——
    见模块 docstring 里"删除守卫"那段：本套件全量跑时，删除计数离沙箱阈值只差几次。
    """
    missing = []
    for name, factory in HEAVY_EXPORTS:
        calls = []

        async def spy(func, *args, _sink=calls, **kwargs):
            _sink.append(getattr(func, "__name__", repr(func)))
            return STUB_XLSX  # 不真建表（理由见模块 docstring）

        monkeypatch.setattr(api, "run_in_threadpool", spy)
        resp = asyncio.run(factory())
        assert resp is not None
        if not calls:
            missing.append(name)

    assert not missing, (
        f"这些导出没有把建表工作交给线程池：{'、'.join(missing)} —— "
        f"它们会在事件循环上同步跑完，期间全站请求一起排队（R-70）"
    )


def _burn(seconds=0.5):
    """纯 CPU 忙等：等价于"一段跑满 0.5 秒的同步重活"，且不碰任何文件。"""
    end = time.perf_counter() + seconds
    n = 0
    while time.perf_counter() < end:
        n += 1
    return STUB_XLSX


def _measure_gaps(coro_factory, tick=0.01):
    """跑一次 coro_factory，返回期间"心跳"的滞后毫秒数列表。"""

    async def go():
        stop = asyncio.Event()
        gaps = []

        async def heartbeat():
            while not stop.is_set():
                t0 = time.perf_counter()
                await asyncio.sleep(tick)
                gaps.append(((time.perf_counter() - t0) - tick) * 1000)

        task = asyncio.create_task(heartbeat())
        await asyncio.sleep(0.05)
        gaps.clear()  # 丢掉启动阶段
        await coro_factory()
        stop.set()
        await task
        return gaps

    return asyncio.run(go())


def test_export_keeps_event_loop_responsive(small_db, monkeypatch):
    """行为锁：重活走线程池时，心跳被拖住的时间必须显著短于内联写法。

    判据是**比值**（> 3 倍），另加 400ms 绝对上限兜底：
    - 比值：对机器快慢免疫；
    - 绝对上限：防止"两边都很烂"时比值仍然成立。

    重活用 0.5 秒纯 CPU 忙等代替真实建表（见模块 docstring），
    派发路径仍然是真实的端点 + 真实的 `run_in_threadpool`。
    """
    real = api.run_in_threadpool

    async def dispatch_to_burn(func, *args, **kwargs):
        # 端点要派发的"建表函数"被换成忙等：等价重活，零文件副作用
        return await real(_burn, 0.5)

    monkeypatch.setattr(api, "run_in_threadpool", dispatch_to_burn)
    gaps_ok = _measure_gaps(lambda: api.export_class_schedule())
    assert len(gaps_ok) > 3, f"心跳样本太少（{len(gaps_ok)}），测量无效"
    worst_ok = max(gaps_ok)

    async def inline_defect():
        # 修复前的写法：重活直接跑在事件循环上
        return _burn(0.5)

    worst_bad = max(_measure_gaps(inline_defect))

    assert worst_bad > 300, (
        f"反证失败：内联重活只把心跳拖住 {worst_bad:.0f}ms —— "
        f"判据本身没有鉴别力，本测试失效（别让它变成假绿）"
    )
    assert worst_ok < 400, (
        f"导出期间事件循环被拖住 {worst_ok:.0f}ms（上限 400ms）—— 重活没离开事件循环"
    )
    assert worst_ok * 3 < worst_bad, (
        f"走线程池时心跳被拖 {worst_ok:.0f}ms，内联写法 {worst_bad:.0f}ms，"
        f"两者同量级 ⇒ 重活实际上仍在事件循环上"
    )
