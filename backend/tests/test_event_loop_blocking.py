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
import sqlite3
import sys
import threading
import time


sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import app.api.reschedule as api
def _read_api_src() -> str:
    """读 reschedule.py 源码。

    4 条 AST-lite 检查共用 —— 原先 4 份 `open(...).read()` 各写一遍，
    既漏 `with`（SIM115）也漏清理。收成一处。
    """
    with open(api.__file__, "r", encoding="utf-8") as f:
        return f.read()


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
            # R65：路由现在会先判 success，成功路径必须显式给 True
            return {"success": True, "summary_text": "ok", "scheduled": 12}

        # 默认走全表分片（R-59）：假求解器也要有这个入口，否则测的就不是线程池
        def solve_all(self, chunk_size=60):
            seen["thread"] = threading.current_thread()
            return {"success": True, "summary_text": "ok", "solved": 171,
                    "total_sections": 171, "total_sections_all": 171,
                    "schedule": [], "conflicts": []}

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

    src = _read_api_src()
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
    src = _read_api_src()
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


# ---------- R27：R25 的并发副作用 —— 冷启动会加载两次 ----------
#
# R25 把加载挪到线程池后，`_db.loaded` 只在**所有并发请求都返回后**才由
# `_ensure_loaded_async` 设置。于是：
#
#   t=0   N 个请求同时进入 → 都看到 loaded=False → 都去排队
#   t=6   线程 1 拿锁、加载完、设 loaded=True、返回
#   t=6   线程 2 拿锁 → 双检看到 loaded=True → 正确快速返回
#   t=6   线程 1 回到 async 端 → 设 loaded=True
#   t=?   另一个**尚未进入线程池**的请求在 async 端判 loaded=False → 又排队
#         → 线程 3 拿锁，看到 loaded=True → 空转返回
#
# 实测（400ms 模拟冷解析）：
#     10/40/100/300 并发 → 总耗时都是 ~810 ms（理论 400），加载次数都是 **2**
#
# 不会无限膨胀（锁内双检挡住了），但首次请求的延迟平白翻倍。
# 真实冷解析 3.8s 时，这是 3.8s → 7.6s。
#
# 修法：由**拿到锁的那个 worker 自己**在出锁前把 loaded 置真，
# 而不是回到事件循环后再设。这样 async 端的复检立刻能看到。

def _make_slow_loader(calls: list, delay=0.05):
    """模拟冷解析：计数 + 占住锁。"""
    def loader():
        calls.append(1)
        threading.Event().wait(delay)   # 可被唤醒的 sleep，别用 time.sleep 卡死退出
        return ("ok", 433)
    return loader


def test_concurrent_cold_start_loads_exactly_once(monkeypatch):
    """N 个请求同时冷启动，整表只该解析 1 次（双检锁的意义就在这里）。"""
    calls = []

    def fake_load():
        calls.append(threading.current_thread().name)
        threading.Event().wait(0.05)
        return ("ok", 433)

    monkeypatch.setattr(api, "_load_default_schedule", fake_load)
    # 冷启动：先让 db 处于未加载态
    monkeypatch.setattr(api._db, "loaded", False, raising=False)
    monkeypatch.setattr(api._db, "entries", [], raising=False)

    async def main():
        await asyncio.gather(*[api._ensure_loaded_async() for _ in range(20)])

    asyncio.run(main())
    assert len(calls) == 1, (
        f"20 个并发请求冷启动，整表解析了 {len(calls)} 次 —— "
        "双检锁失效，首次请求延迟被放大（真实冷解析 3.8s → 7.6s）"
    )
    assert api._db.loaded is True, "加载完没把 loaded 置真"


def test_concurrent_cold_start_records_loaded_inside_worker(monkeypatch):
    """`loaded` 必须在**下一个 worker 拿锁之前**已经为真。

    这就是上面那条时序的关键点：如果只在 async 端设，等锁的 worker 出来后
    又会有请求看到 False 再排一次队。
    """
    seen_states = []

    def fake_load():
        # 加载结束的那一刻，锁还没释放；loaded 应该已经由调用方在内部置真，
        # 或者至少由拿到锁的 worker 置真 —— 这里记录锁内的观察值
        seen_states.append(api._db.loaded)
        threading.Event().wait(0.03)
        return ("ok", 1)

    monkeypatch.setattr(api, "_load_default_schedule", fake_load)
    monkeypatch.setattr(api._db, "loaded", False, raising=False)
    monkeypatch.setattr(api._db, "entries", [], raising=False)

    async def main():
        await asyncio.gather(*[api._ensure_loaded_async() for _ in range(8)])

    asyncio.run(main())
    assert len(seen_states) == 1, f"进入了 {len(seen_states)} 次加载，应为 1 次"



# ---------- R28：改 _db.entries 的三条写路径互不加锁 ----------
#
# 会改 `_db.entries` 的写路径有三条：
#   · `land_commit`         → `apply_plan(_db, plan)`            （拿 _land_lock）
#   · `apply_move`          → `_apply_reschedule(record, ...)`    （原先无锁）
#   · `/requests/{id}/approve` → 同样调 `_apply_reschedule`        （原先无锁）
#
# `_apply_reschedule` 是"读 entries 挑候选 → classify_move 判定 → 写 e.week"
# 三步流程。两个线程若都基于**旧** entries 判定，双方都看不到对方即将占用那一格
# ⇒ 同一间教室同一时段被排进两门课，而两个接口都返回 success。


def test_two_teachers_moving_into_same_room_do_not_both_win():
    """两条调课并发搬进同一间教室时，冲突检查不能失明。

    实测（决策与写入之间注入 10ms 窗口，模拟真实表遍历耗时）：
        无锁 → 20/20 次双占同一间教室
        加锁 →  0/20 次

    注入延时是必要的：不注入时 GIL 下两个线程几乎不会落在那个窗口里，
    测试会假绿。这里显式复现的是**已证实的时序**，不靠概率撞。
    用例先断言"无锁时确实能复现"，否则说明场景失效、这条测试失去意义。
    """
    from app.utils.excel_parser import ScheduleDatabase, ScheduleEntry
    from app.conflict_model import classify_move

    def build():
        db = ScheduleDatabase()
        db.entries = [
            ScheduleEntry(2, "周一", "567", "cA", "g", "实验A", "A101", "T1"),
            ScheduleEntry(2, "周一", "567", "cB", "g", "实验B", "A101", "T2"),
        ]
        db.loaded = True
        return db

    def move(db, teacher, experiment, delay=0.01):
        src = [e for e in db.entries
               if e.teacher == teacher and e.experiment == experiment][0]
        hits = classify_move(
            db.entries, week=5, weekday="周一", slots={"567"},
            room=src.room, teacher=teacher, class_name=src.class_name,
            group=src.group, exclude=src)
        threading.Event().wait(delay)      # 判定与写入之间的真实窗口
        if hits:
            return False
        src.week = 5
        return True

    def run(use_lock):
        collisions = 0
        for _ in range(10):
            db = build()

            # B023：db 是循环变量，按默认参数绑定 —— 闭包按名查会读到下一轮的值
            def go(teacher, experiment, db=db):
                if use_lock:
                    with api._DB_WRITE_LOCK:
                        move(db, teacher, experiment)
                else:
                    move(db, teacher, experiment)

            ts = [threading.Thread(target=go, args=("T1", "实验A")),
                  threading.Thread(target=go, args=("T2", "实验B"))]
            for x in ts:
                x.start()
            for x in ts:
                x.join()
            if len([e for e in db.entries if e.week == 5]) == 2:
                collisions += 1
        return collisions

    assert run(False) > 0, "无锁时没复现出竞争 —— 场景失效，需重新确认"
    assert run(True) == 0, "用了写锁仍然双占同一间教室 —— 锁没生效"


def test_every_apply_reschedule_call_site_holds_write_lock():
    """静态守卫：`_apply_reschedule` 的每个调用点都必须在写锁内。

    R28 修了三处。这是**会复发**的模式：`_apply_reschedule` 名字听着像
    "纯函数算一下"，实际它改 `_db.entries`。下一个写审批路径的人
    顺手 `_apply_reschedule(record)` 是最自然的动作。

    允许两种形态：
      1. 处在 `with _DB_WRITE_LOCK:` / `with _land_lock:` 块内；
      2. 处在 `_load_default_schedule()` 内 —— 那是启动重放，整段被
         `_load_lock` 覆盖，且此时 `loaded` 尚为 False，没人能并发拿到 `_db`。
    """
    src = _read_api_src()
    lines = src.splitlines()

    def enclosing_def(idx):
        for j in range(idx, max(0, idx - 200), -1):
            if lines[j].startswith("def "):
                return lines[j]
        return ""

    def in_with_block(idx, marker):
        """从 idx 往回找，判断是否落在 marker 的 with 块内（按缩进）。"""
        target_indent = len(lines[idx]) - len(lines[idx].lstrip())
        for j in range(idx - 1, max(0, idx - 200), -1):
            line = lines[j]
            if not line.strip():
                continue
            indent = len(line) - len(line.lstrip())
            if indent < target_indent:
                if marker in line and line.lstrip().startswith("with "):
                    return True
                target_indent = indent
        return False

    offenders = []
    for i, line in enumerate(lines):
        if "_apply_reschedule(" not in line:
            continue
        s = line.strip()
        if s.startswith("#") or s.startswith("def "):
            continue
        if "run_in_threadpool(_locked_apply" in s:
            continue
        if in_with_block(i, "_DB_WRITE_LOCK") or in_with_block(i, "_land_lock"):
            continue
        if "_load_default_schedule" in enclosing_def(i):
            continue
        offenders.append("line {}: {}".format(i + 1, s[:80]))

    assert offenders == [], (
        "_apply_reschedule 调用点没持写锁（并发调课会把两条课排进同一间教室）："
        + chr(10) + chr(10).join(offenders)
    )



# ---------- R29：上传新表与调课应用之间的实例替换窗口 ----------
#
# `upload_schedule` 的写法是"先构建新实例 new_db，全部成功后再 `_db = new_db`
# 替换引用"。这个模式解决了 R4 的中间态问题（读取方不会看到空表），
# **但没有解决"替换那一刻"**：
#
#   t=0   land_commit 拿到 _db（旧实例），开始 apply_plan
#   t=1   upload 完成解析，`_db = new_db`
#   t=2   land_commit 继续在**旧实例**上写 e.week
#   t=3   旧实例被丢弃 → 这次落地永久消失，接口却返回 success
#
# 实测 upload 解析耗时 225 ms（缓存命中 1 ms），窗口不小。
#
# 另一面：`apply_move` 里 `if _db.loaded` 与 `run_in_threadpool(...)` 之间
# 有 await 点，`_apply_reschedule` 读的是模块级 `_db`（延迟绑定）——
# 若此刻正好被替换，它会在**两个不同的实例**上各读写一部分。


def test_upload_and_landing_share_a_lock():
    """上传换表与落地/调课必须互斥，不能一边换引用一边往旧实例写。

    R4 修的是"读到中间态"（空表），这轮修的是"写进被丢弃的实例"。
    两者是同一个实例替换操作的读侧和写侧。
    """
    src = _read_api_src()
    lines = src.splitlines()

    def body_of(name):
        start = None
        for i, line in enumerate(lines):
            if line.startswith("def {}(".format(name)) or \
                    line.startswith("async def {}(".format(name)):
                start = i
                break
        assert start is not None, "找不到函数 {}".format(name)
        for j in range(start + 1, len(lines)):
            if lines[j].startswith("def ") or lines[j].startswith("async def ") \
                    or lines[j].startswith("@"):
                return "\n".join(lines[start:j])
        return "\n".join(lines[start:])

    # R-70 重构后重活（含 `_db` 切换）拆进了 `_apply_uploaded_schedule`（线程池同步函数），
    # 端点体只剩"净化文件名 + 限量读取 + 丢线程池"。审计对象是整条上传路径，
    # 两个函数体合起来扫，锁在哪个函数里都认。
    body = body_of("upload_schedule") + body_of("_apply_uploaded_schedule")
    assert "_DB_WRITE_LOCK" in body or "_land_lock" in body or "_load_lock" in body, (
        "upload_schedule 替换 _db 引用时没拿锁 —— "
        "此刻并发的 land_commit 会把落地写进即将被丢弃的旧实例，"
        "而接口仍返回 success"
    )







def test_upload_swap_section_holds_write_lock():
    """换表的"替换 + 作废旧状态"段必须在 `_DB_WRITE_LOCK` 内。

    坏情形只有一种顺序：

        land_commit 读旧表 → [换表完成，旧方案被 clear] → land_commit 才 save
        ⇒ 这条落在**新表**上，成为旧表方案的幽灵

    解析阶段（225ms）必须在锁外，否则每个上传都会拖住全部落地/调课；
    但"替换引用 + clear 旧方案"这一小段必须在锁内，否则上面的顺序成立。

    测试锚点选在 `_landings().clear()` —— 它是这一段里最后一个动作，
    此刻锁必须已被持有。
    """
    import app.plan_landing as pl

    observed = {}
    orig_clear = pl.LandingStore.clear

    def probe_clear(self, *a, **kw):
        # 换表正在执行 clear：此刻 _DB_WRITE_LOCK 必须**不可获取**
        got = api._DB_WRITE_LOCK.acquire(timeout=0)
        if got:
            api._DB_WRITE_LOCK.release()
        observed["lock_free_during_clear"] = got
        return orig_clear(self, *a, **kw)

    import tempfile

    class _FakeUpload:
        filename = "swap.xlsx"

        def __init__(self):
            self._chunks = [b"x"]

        async def read(self, size=-1):
            return self._chunks.pop() if self._chunks else b""

    tmpdir = tempfile.mkdtemp()
    saved = (api._DB_PATH, api._landing_store, api.ScheduleDatabase,
             api._upload_target, api._db)
    try:
        api._DB_PATH = os.path.join(tmpdir, "swap.db")
        api._landing_store = None
        api._landings()

        class _Db:
            def __init__(self):
                self.entries = []
                self.pending = []
                self.loaded = True
                self.load_report = {}
                self._filepath = os.path.join(tmpdir, "swap.xlsx")

            def load_from_excel(self, path):
                return None

            def get_all_rooms(self):
                return []

            def get_all_teachers(self):
                return []

            def get_week_range(self):
                return (1, 16)

        pl.LandingStore.clear = probe_clear
        api.ScheduleDatabase = _Db
        api._upload_target = lambda *a, **kw: os.path.join(tmpdir, "swap.xlsx")
        try:
            asyncio.run(api.upload_schedule(file=_FakeUpload()))
        finally:
            pl.LandingStore.clear = orig_clear

        assert observed.get("lock_free_during_clear") is False, (
            "换表执行 `_landings().clear()` 时写锁是空闲的 —— "
            "并发的 land_commit 会在这之后把旧表方案 save 到新表上，成为幽灵记录"
        )
    finally:
        api._DB_PATH, api._landing_store = saved[0], saved[1]
        api.ScheduleDatabase, api._upload_target = saved[2], saved[3]
        api._db = saved[4]      # 换表真的替换了全局 _db，必须还原


# ---------- R75：落地 / 撤销的锁必须在**工作线程**里拿 ----------
#
# R74 量出来的那条：真实 sqlite 锁下 `plan_landing.undo` 要等两次 busy_timeout
# （单次 5.50s ⇒ 一次 undo 11.04s）。但 R74 只测了**函数**，端点层还是
# `async def` 里同步 `with _land_lock:` —— 于是这 11 秒里整个事件循环停摆。
#
# 更隐蔽的是"等锁"这一半：`_land_lock` 同时是 `_db.entries` 的写锁
# （`_DB_WRITE_LOCK`，R28），别的请求在锁内跑时，这里就在**事件循环线程**上原地等。
# 所以正确的改法不是"把 undo 包进 run_in_threadpool"，而是**连拿锁一起**进去。
#
# R75 取证（`R75-new/probe_endpoint_loop.py`，同一份业务代码，只改跑在哪条线程）：
#
#     A 旧形状（事件循环） busy=0.15s  undo 0.469s  心跳最大滞后 **459.0 ms**
#     B 新形状（线程池）   busy=0.15s  undo 0.484s  心跳最大滞后    6.0 ms
#     B 生产值             busy=5s     undo 11.047s 心跳最大滞后   22.0 ms（714 拍）
#
# 耗时一模一样（同一段 sqlite 等待），差别只在事件循环有没有被按住。

_LAND_PLAN = {"schedule": [
    {"class_name": "甲班", "project": "项目A", "group": "1组",
     "week": 3, "weekday": "周三", "time_slot": "91011",
     "room": "A201", "teacher": "吴老师"},
]}

# busy_timeout 从 5s 调到 0.15s —— **只改等待时长，不改被测分支**（真锁、
# 同一个 sqlite 异常、同一段 except）。不改的话一条用例白等 11s；
# 生产值 11.047s 已在 probe 里量过，数字写在文档里。
_LOCK_WAIT = 0.15


class _ShortBusyStore(api.plan_landing.LandingStore):
    """busy timeout 可调的 LandingStore（默认 5s 会让用例白等 11s）。"""

    def __init__(self, path, timeout=_LOCK_WAIT):
        super().__init__(path)
        self._timeout = timeout

    def _conn(self):
        conn = sqlite3.connect(self.path, timeout=self._timeout)
        conn.row_factory = sqlite3.Row
        return conn


def _land_env(tmp_path, monkeypatch, timeout=_LOCK_WAIT):
    """把落地链路的依赖整体指向临时库（与 test_landing_endpoints 的 env 同构）。"""
    from app.utils.excel_parser import ScheduleDatabase, ScheduleEntry

    db = ScheduleDatabase()
    db.entries = [
        ScheduleEntry(1, "周一", "567", "甲班", "1组", "项目A", "A101", "吴老师"),
        ScheduleEntry(1, "周二", "567", "甲班", "2组", "项目A", "A101", "吴老师"),
    ]
    db.pending = []
    db.loaded = True
    store = _ShortBusyStore(str(tmp_path / "land.db"), timeout=timeout)
    store.init_schema()
    monkeypatch.setattr(api, "_db", db)
    monkeypatch.setattr(api, "_DB_PATH", str(tmp_path / "land.db"))
    monkeypatch.setattr(api, "_landing_store", store)
    return db, store


def _hold_reserved(path):
    """另一条连接拿 RESERVED 锁（`BEGIN IMMEDIATE`）：真锁，让 sqlite 自己抛错。"""
    conn = sqlite3.connect(path, timeout=0.0, isolation_level=None)
    conn.execute("BEGIN IMMEDIATE")
    return conn


def _land_once():
    """先落一次地，撤销才有记录可撤。"""
    asyncio.run(api.land_commit(api.LandPlanRequest(**_LAND_PLAN), land_by="admin"))


def _run_with_heartbeat(coro_factory, beat=0.01):
    """跑一段活儿，同时记录事件循环心跳的最大滞后（毫秒）。

    返回 (结果, 异常文本, 耗时秒, 最大滞后毫秒)。
    """
    stop = False
    lags = []

    async def beat_loop():
        while not stop:
            t0 = time.monotonic()
            await asyncio.sleep(beat)
            lags.append(time.monotonic() - t0 - beat)

    async def main():
        nonlocal stop
        task = asyncio.create_task(beat_loop())
        await asyncio.sleep(0.05)          # 让心跳先跑起来，别把启动开销算进去
        t0 = time.monotonic()
        out, err = None, ""
        try:
            out = await coro_factory()
        except Exception as e:              # 失败分型要照实看，别吞
            err = "{}: {}".format(type(e).__name__, e)
        dur = time.monotonic() - t0
        stop = True
        await task
        return out, err, dur, max(lags) * 1000

    return asyncio.run(main())


def test_land_undo_runs_off_event_loop(monkeypatch, tmp_path):
    """撤销的业务（含拿写锁）必须跑在工作线程上，不能在事件循环线程上。"""
    _land_env(tmp_path, monkeypatch)
    seen = {}

    def fake_undo(db, store, actor=""):
        seen["thread"] = threading.current_thread()
        seen["actor"] = actor
        return {"success": True, "landed_at": "2026-11-02T21:00:00",
                "restored_entries": 2, "rolled_back_moves": 0}

    monkeypatch.setattr(api.plan_landing, "undo", fake_undo)

    out, loop_thread = _run(api.land_undo(actor="admin"))

    assert out["success"] is True
    assert seen["actor"] == "admin", "操作人没透到撤销里（要追责的写操作）"
    assert seen["thread"] is not loop_thread, (
        "撤销跑在事件循环线程上 —— 锁竞争时整站会跟着停摆（实测 11.08s）"
    )
    assert "已撤销" in out["message"], "回执丢了（R73 那条不许回退）"


def test_land_commit_runs_off_event_loop(monkeypatch, tmp_path):
    """落地同理：`apply_plan` + 落盘整段都在锁内，锁也不能在事件循环上等。"""
    _land_env(tmp_path, monkeypatch)
    seen = {}

    real_apply = api.plan_landing.apply_plan

    def fake_apply(db_arg, plan):
        seen["thread"] = threading.current_thread()
        return real_apply(db_arg, plan)

    monkeypatch.setattr(api.plan_landing, "apply_plan", fake_apply)

    out, loop_thread = _run(
        api.land_commit(api.LandPlanRequest(**_LAND_PLAN), land_by="admin"))

    assert out["success"] is True, out
    assert out["landed_by"] == "admin"
    assert seen["thread"] is not loop_thread, "落地的锁与写入跑在事件循环线程上"


def test_land_undo_waiting_on_real_lock_does_not_stall_heartbeat(monkeypatch, tmp_path):
    """真锁挡住撤销时：撤销**照样等**（~0.3s），但事件循环只滞后几毫秒。

    这条是 R75 的**判据**，两半缺一不可：
      · 只断言"滞后小" —— 撤销要是根本没等（提前返回），也能过；
      · 只断言"等了"   —— 旧形状也等了，正是它的问题。
    R75 实测同一段业务：旧形状滞后 459ms（≈ 耗时），新形状 6ms。
    """
    _land_env(tmp_path, monkeypatch)
    _land_once()

    holder = _hold_reserved(api._DB_PATH)
    try:
        out, err, dur, lag_ms = _run_with_heartbeat(
            lambda: api.land_undo(actor="admin"))
    finally:
        holder.rollback()
        holder.close()

    assert not err, "撤销不该把异常漏出端点（失败分型要回 500 + 完整结构）：" + err
    assert getattr(out, "status_code", None) == 500, (
        "真锁下撤销应走 revoke_failed 的 500，实际 %r" % (out,))
    assert dur >= 0.3, (
        "撤销只用了 %.3fs —— 它没在等锁，那这条用例证明不了任何事情" % dur)
    assert lag_ms < 0.15 * 1000, (
        "撤销等锁期间事件循环被按住 %.1fms（耗时 %.3fs）—— 锁是在事件循环线程上等的"
        % (lag_ms, dur))


def test_land_undo_waiting_on_write_lock_does_not_stall_heartbeat(monkeypatch, tmp_path):
    """另一条请求正持着写锁时：撤销**等在锁上**，事件循环照样能跑。

    上一条守的是"锁内的 sqlite 等待"，这条守的是另一半 —— **等锁本身**。
    两者是不同的失败面：

        out = _locked_land_undo(actor)                    ← 锁与活都在事件循环上（旧形状）
        with _land_lock:
            out = await run_in_threadpool(...)            ← 看起来改了，**等锁还在事件循环上**

    第二种是很自然的"顺手改法"，也确实是错的：`_land_lock` 同时是 `_db.entries`
    的写锁（`_DB_WRITE_LOCK`，R28），一次慢落地（实测 11s）持锁期间，所有落地/撤销
    入口都要在事件循环上原地排队。上一条用例对它**失明**（那条的锁是空的）。
    """
    _land_env(tmp_path, monkeypatch)
    monkeypatch.setattr(
        api.plan_landing, "undo",
        lambda db, store, actor="": {"success": True,
                                     "landed_at": "2026-11-02T21:00:00",
                                     "restored_entries": 2,
                                     "rolled_back_moves": 0})

    acquired = threading.Event()
    release = threading.Event()

    def holder():
        with api._land_lock:
            acquired.set()
            release.wait(2.0)          # 模拟"有人在锁内干慢活"

    t = threading.Thread(target=holder)
    t.start()
    assert acquired.wait(2.0), "拿锁的辅助线程没起来，场景失效"
    try:
        # 0.35s 后放锁：撤销必须等这么久，但不许把事件循环一起按着
        threading.Timer(0.35, release.set).start()
        out, err, dur, lag_ms = _run_with_heartbeat(
            lambda: api.land_undo(actor="admin"))
    finally:
        release.set()
        t.join()

    assert not err, err
    assert out["success"] is True
    assert dur >= 0.3, (
        "撤销只用了 %.3fs —— 它没等在写锁上，这条用例证明不了任何事情" % dur)
    assert lag_ms < 0.15 * 1000, (
        "等锁期间事件循环被按住 %.1fms —— 锁是在事件循环线程上等的" % lag_ms)


def test_two_concurrent_undos_still_serialize(monkeypatch, tmp_path):
    """下沉线程池**不能顺手把锁丢了**：两个撤销并发进来仍然要排队。

    变异体会写成"把 `with _land_lock:` 留在 async 端、只把 undo 丢线程池" ——
    那样锁确实还在，但等锁发生在事件循环上；反过来"整段下沉但忘了带锁"更安静：
    两个撤销同时改 `_db.entries`，后一个基于前一个的中间状态算快照。
    """
    _land_env(tmp_path, monkeypatch)
    state = {"now": 0, "peak": 0}
    guard = threading.Lock()

    def fake_undo(db, store, actor=""):
        with guard:
            state["now"] += 1
            state["peak"] = max(state["peak"], state["now"])
        threading.Event().wait(0.08)        # 可被唤醒的 sleep，别卡死退出
        with guard:
            state["now"] -= 1
        return {"success": True, "landed_at": "2026-11-02T21:00:00",
                "restored_entries": 2, "rolled_back_moves": 0}

    monkeypatch.setattr(api.plan_landing, "undo", fake_undo)

    async def main():
        return await asyncio.gather(
            api.land_undo(actor="a"), api.land_undo(actor="b"))

    outs = asyncio.run(main())

    assert [o["success"] for o in outs] == [True, True]
    assert state["peak"] == 1, (
        "两个撤销同时在锁内跑（峰值 %d）—— 写锁没跟着下沉" % state["peak"])
