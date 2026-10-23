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
