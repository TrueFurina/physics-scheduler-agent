"""CP-SAT 求解器预热。

为什么需要
----------
`ortools` 的首次导入 + 求解器原生库初始化是一次性成本：本地实测导入 0.70s，
而**进程内第一次真正求解**还要再付一次初始化（首跑 ~4.9s，第二次起 ~0.95s）。

对使用者来说这表现为：「一键排课」第一次点要等 5 秒、之后都只要 1 秒。
而等待最久的恰恰是最重要的第一次（演示第一次点击）。

做法：进程启动后在**后台线程**跑一个两变量的小模型，把导入与初始化提前付掉。
- 不阻塞启动、不占请求路径：daemon 线程，失败也只是退回"首跑慢一点"。
- 不写任何业务状态：只用局部变量，碰不到 `_db`。
- 结果记录在模块状态里，由 `/health` 的 `solver` 字段对外可见
  （`pending` / `ok` / `failed`），便于判断"是不是预热没成功才慢"。

环境变量
--------
    SCHEDULE_WARMUP=0     关闭预热（例如本地调试时不想要后台噪音）
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any, Dict

_STATE: Dict[str, Any] = {
    "status": "pending",   # pending / running / ok / failed
    "elapsed_ms": None,
    "error": None,
}
_LOCK = threading.Lock()
_STARTED = False


def _enabled() -> bool:
    return os.getenv("SCHEDULE_WARMUP", "1").strip().lower() not in {"0", "false", "no", "off"}


def state() -> Dict[str, Any]:
    """只读快照（供 /health 展示）。"""
    with _LOCK:
        return dict(_STATE)


def _run() -> None:
    started = time.perf_counter()
    try:
        from ortools.sat.python import cp_model

        model = cp_model.CpModel()
        x = model.NewIntVar(0, 10, "x")
        model.Add(x >= 3)
        model.Maximize(x)

        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = 3.0
        status = solver.Solve(model)
        ok = solver.StatusName(status) == "OPTIMAL" and solver.Value(x) == 10

        with _LOCK:
            _STATE.update({
                "status": "ok" if ok else "unexpected",
                "elapsed_ms": int((time.perf_counter() - started) * 1000),
                "error": None if ok else f"unexpected result: {solver.StatusName(status)}",
            })
    except Exception as exc:  # 预热失败绝不能影响服务可用性
        with _LOCK:
            _STATE.update({
                "status": "failed",
                "elapsed_ms": int((time.perf_counter() - started) * 1000),
                "error": f"{type(exc).__name__}: {exc}"[:200],
            })


def start_background() -> bool:
    """启动一次后台预热；已启动过/被禁用则返回 False（幂等）。"""
    global _STARTED
    if not _enabled():
        with _LOCK:
            _STATE["status"] = "disabled"
        return False

    with _LOCK:
        if _STARTED:
            return False
        _STARTED = True
        _STATE["status"] = "running"

    threading.Thread(target=_run, name="solver-warmup", daemon=True).start()
    return True
