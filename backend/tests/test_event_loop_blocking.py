"""事件循环不能被同步重活冻住 —— 一个慢请求不该让全站一起卡

实测过的真实症状
----------------
`/chat`（fast=false）会同步发起 LLM HTTP 请求，实测 7.6s。而它是 `async def`，
同步调用直接占住事件循环 —— 并发期间 `/stats`（平时 4ms）被拖到 **8.67s**。
翻译成人话：**甲老师点了"申请调课"，乙老师连"数据看板"都刷不出来**，
连 `/health` 探活都不响应（平台会因此判服务不健康）。

`suggest_plans` / `consult` / CP-SAT 求解都是同步阻塞函数，正确做法是丢到
线程池（`run_in_threadpool`）。这组用例锁住"确实丢出去了"这个事实：
断言阻塞函数运行在**别的线程**，而不是跑在事件循环所在的那条线程上。
"""

import asyncio
import os
import sys
import threading


sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import app.api.reschedule as api


def _fake_chat_result():
    return {
        "success": True, "message": "ok", "target_week": 3,
        "available_slots": [], "excluded_slots": [], "ai_plans": [], "total": 0,
    }


def _run(coro):
    """在一条**全新的**事件循环上跑，同时记下"事件循环所在线程"。"""
    box = {}

    async def main():
        box["loop_thread"] = threading.current_thread()
        return await coro

    return asyncio.run(main()), box["loop_thread"]


def test_chat_dispatches_llm_work_off_event_loop(monkeypatch):
    """`/chat` 的阻塞主体必须跑在线程池，不能跑在事件循环线程上。"""
    seen = {}

    def fake_process(text, with_ai=True, context=None):
        seen["thread"] = threading.current_thread()
        seen["with_ai"] = with_ai
        seen["context"] = context
        return _fake_chat_result()

    monkeypatch.setattr(api._agent, "process_request", fake_process)

    resp, loop_thread = _run(api.reschedule_chat(api.ChatRequest(
        message="第3周", fast=False, context={"teacher_name": "吴琳"},
    )))

    assert resp.success is True
    assert "thread" in seen, "process_request 没被调用"
    assert seen["thread"] is not loop_thread, \
        "LLM 调用跑在事件循环线程上 —— 一个慢请求会冻住全站"
    assert seen["with_ai"] is True, "fast=False 应透传成 with_ai=True"
    assert seen["context"] == {"teacher_name": "吴琳"}, \
        "多轮追问的 context 没透传到 agent（位置参数错位？）"


def test_experiment_tutor_consult_off_event_loop(monkeypatch):
    seen = {}

    def fake_consult(question, context=None, role=None):
        seen["thread"] = threading.current_thread()
        return {"reply": "答"}

    monkeypatch.setattr(api._agent.llm_parser, "consult", fake_consult)

    resp, loop_thread = _run(api.experiment_tutor_consult(
        api.ConsultRequest(question="声速测量实验的数据处理规范是什么？")))

    assert resp.success is True
    assert seen["thread"] is not loop_thread, "实验规范查询（LLM）跑在事件循环线程上"


def test_parallel_solve_off_event_loop(monkeypatch):
    """CP-SAT 求解同样是同步 CPU 活（实测 0.9–1.7s），也得丢线程池。"""
    seen = {}

    class FakePS:
        def __init__(self, *a, **kw):
            pass

        def solve(self, class_name, weekday=None, start_week=None, weeks=None,
                  time_slots=None, week_max_override=None, max_time_seconds=10.0):
            seen["thread"] = threading.current_thread()
            return {"status": "OPTIMAL", "scheduled": 24, "total_units": 24}

    import app.engine.parallel_scheduler as ps_mod
    monkeypatch.setattr(ps_mod, "ParallelScheduler", FakePS)

    resp, loop_thread = _run(api.parallel_solve(api.ParallelSolveRequest(class_name="2024级")))

    assert resp["scheduled"] == 24
    assert seen["thread"] is not loop_thread, "CP-SAT 求解跑在事件循环线程上"


def test_auto_schedule_off_event_loop(monkeypatch):
    seen = {}

    class FakeScheduler:
        def __init__(self, *a, **kw):
            pass

        def solve(self, max_sections=30):
            seen["thread"] = threading.current_thread()
            return {"summary_text": "ok", "scheduled": 12}

        # 默认走全表分片（R-59）：假求解器也要有这个入口，否则测的就不是线程池
        def solve_all(self, chunk_size=60):
            seen["thread"] = threading.current_thread()
            return {"summary_text": "ok", "solved": 171, "total_sections": 171,
                    "total_sections_all": 171, "schedule": [], "conflicts": []}

        def summary(self, result):
            return "summary"

    import app.engine.auto_scheduler as as_mod
    monkeypatch.setattr(as_mod, "AutoScheduler", FakeScheduler)

    resp, loop_thread = _run(api.auto_schedule())

    # 返回体是 {success, message(=summary_text), data}，定位说明拼在 message 里
    assert "定位说明" in resp["message"]
    assert "summary" in resp["message"]
    assert seen["thread"] is not loop_thread, "自动排课求解跑在事件循环线程上"


# ---------- R25：懒加载自愈也是同步重活 ----------
#
# `_ensure_loaded()` 在冷启动时（进程被平台回收后重启、或缓存文件不在）要
# **整表重解析 + 重放已审批调课**。实测真实表 433 行：
#
#     冷解析 193 ms（本地热盘）  冷沙箱约 3.8 s（已在 /load-default 的注释里记录）
#
# 而它在 27 个 `async def` 路由里被**直接同步调用**。只在首次访问时发生，
# 但那一次会把整个事件循环按住 —— 包括 `/health` 探活。
#
# `/load-default` 自己早就改成 run_in_threadpool 了，`_ensure_loaded()` 漏了：
# 它在代码里位置更靠上（辅助函数），看起来"不算业务接口"。

def _fake_load_default(monkeypatch, seen):
    def fake():
        seen["thread"] = threading.current_thread()
        return ("已加载 433 条", 433)
    monkeypatch.setattr(api, "_load_default_schedule", fake)
    monkeypatch.setattr(api._db, "loaded", False, raising=False)
    # entries 也清掉：模拟真实冷启动，不只是翻一个开关
    monkeypatch.setattr(api._db, "entries", [], raising=False)


def test_ensure_loaded_runs_off_event_loop(monkeypatch):
    """懒加载自愈必须在线程池里跑，不能在事件循环线程上重解析整表。"""
    seen = {}
    _fake_load_default(monkeypatch, seen)

    async def main():
        await api._ensure_loaded_async()

    asyncio.run(main())

    assert "thread" in seen, "冷启动时没触发加载"
    # 事件循环线程在 asyncio.run 里是主线程；加载跑在别处才算合格
    assert seen["thread"] is not threading.main_thread(), (
        "懒加载在事件循环线程上重解析整表（冷启动 193ms~3.8s）—— "
        "那一次会让全站卡住，连 /health 都不响应"
    )


def test_ensure_loaded_is_noop_when_already_loaded(monkeypatch):
    """已加载时必须是零开销直接返回：不能每来一个请求就排一次线程池。"""
    calls = []
    monkeypatch.setattr(api, "_load_default_schedule",
                        lambda: calls.append(1) or ("ok", 0))
    monkeypatch.setattr(api._db, "loaded", True, raising=False)

    async def main():
        for _ in range(3):
            await api._ensure_loaded_async()

    asyncio.run(main())
    assert calls == [], "已加载还在重复加载"


def test_no_async_route_calls_ensure_loaded_directly():
    """静态守卫：async 路由里不许再出现裸 `_ensure_loaded()`。

    R25 修复了 28 处，但这是个**会复发**的模式 —— 下次谁新增一个数据端点，
    顺手写 `_ensure_loaded()` 是最自然的动作（它就在文件里、名字最直观）。
    而后果只在冷启动那一次出现，本地常驻 uvicorn 复现不到。

    这组用例前四条也都是"锁住已修好的事实"，但它们是运行时断言，
    只能覆盖**已存在的**路由。这条扫源码，覆盖**将来新增的**。
    """
    import re

    src = open(api.__file__, "r", encoding="utf-8").read()
    offenders = []
    cur = None
    for i, line in enumerate(src.splitlines(), 1):
        m = re.match(r"(async )?def (\w+)", line)
        if m:
            cur = (m.group(2), bool(m.group(1)))
        if line.strip() == "_ensure_loaded()" and cur and cur[1]:
            offenders.append(f"{cur[0]}:{i}")

    assert offenders == [], (
        "async 路由直接同步调用了 _ensure_loaded()（冷启动整表重解析会冻住事件循环）：\n"
        + "\n".join(offenders)
        + "\n应改为 `await _ensure_loaded_async()`"
    )


# ---------- R26：主数据落盘也是同步文件 IO ----------
#
# `_persist_master_state()` 实测 28–88 ms，且**与文件体积无关**（16KB 和 512KB 一样慢，
# 成本全在单次 write + os.replace 的固定开销）。它在 13 个 admin 路由里被同步调用，
# 而 admin CRUD 一次请求会连着调 `_ensure_master_data()` + CRUD + 落盘。
#
# 事件循环层面的实测对照（1ms 心跳探针）：
#     同步直调 → 事件循环最长卡顿 87.3 ms
#     线程池   → 事件循环最长卡顿 15.2 ms（只剩调度抖动）
#
# 同步期间别人刷看板、点审批、平台探活 /health 全部排在这次写文件后面。

def test_persist_master_state_runs_off_event_loop(monkeypatch):
    seen = {}

    def fake_persist():
        seen["thread"] = threading.current_thread()
        seen["done"] = True

    monkeypatch.setattr(api, "_persist_master_state", fake_persist)

    async def main():
        await api._persist_master_state_async()

    asyncio.run(main())

    assert seen.get("done"), "落盘没被执行"
    assert seen["thread"] is not threading.main_thread(), (
        "主数据落盘跑在事件循环线程上 —— 一次写文件会把全站冻住 28–88ms"
    )


def test_schedulable_extra_tasks_off_event_loop(monkeypatch):
    """灰箱补全可能触发落盘，整段也得在线程池里跑。"""
    seen = {}

    def fake_enrich():
        seen["thread"] = threading.current_thread()
        return ([], [])

    monkeypatch.setattr(api, "_settle_marker", 1, raising=False)
    monkeypatch.setattr(api, "_enrich_extra_tasks", fake_enrich, raising=False)

    async def main():
        await api._schedulable_extra_tasks_async()

    asyncio.run(main())
    assert seen["thread"] is not threading.main_thread(), \
        "灰箱补全（含可能的落盘）跑在事件循环线程上"


def test_no_async_route_calls_blocking_io_directly():
    """静态守卫：async 路由里不许裸调落盘 / 懒加载 / 补全这三个阻塞函数。

    R25/R26 各修了一批，但这是**会复发**的模式：新写一个 admin 端点，
    顺手 `_persist_master_state()` 是最自然的动作。而后果只在并发时出现。
    """
    import re

    BLOCKING = {
        "_persist_master_state()": "_persist_master_state_async()",
        "_ensure_loaded()": "_ensure_loaded_async()",
        "_schedulable_extra_tasks()": "_schedulable_extra_tasks_async()",
    }
    src = open(api.__file__, "r", encoding="utf-8").read()
    offenders = []
    cur = None
    for i, line in enumerate(src.splitlines(), 1):
        m = re.match(r"(async )?def (\w+)", line)
        if m:
            cur = (m.group(2), bool(m.group(1)))
        s = line.strip()
        if cur and cur[1] and s in BLOCKING:
            offenders.append(f"{cur[0]}:{i}  {s} → 应用 {BLOCKING[s]}")

    assert offenders == [], (
        "async 路由直接同步调用了阻塞函数（会冻住事件循环）：\n"
        + "\n".join(offenders)
    )
