"""未捕获异常的可观测性：500 不能是"什么都没发生"（R53）

缺陷（本轮实测，修复前）
------------------------
让 `/api/reschedule/teachers/{name}/courses` 内部抛 `RuntimeError`，然后请求它：

```
status      : 500
content-type: text/plain
响应体      : Internal Server Error
响应头      : （没有任何 x- 头）
计数器      : requests 0→0 / errors 0→0      ← 一个都不动
日志        : （一行都没有）
异常类型/栈 : 没有
```

根因在中间件的写法：`await self.app(scope, receive, send_wrapper)` 一旦抛异常，
**后面那段计数与日志代码根本不会执行**。于是运维侧"什么都没发生"，
用户侧"点了没反应" —— 两边的信息同时断掉。

修复的两半
----------
1. 中间件把计数与日志搬进 `finally`，并给每个请求一个 8 位编号（`x-request-id`）；
   异常**不吞**，原样抛给 ServerErrorMiddleware。
2. 注册 `Exception` 处理器：用 `exc_info=(type, exc, tb)` 显式打栈（`logger.exception`
   依赖 `sys.exc_info()`，而处理器是在 except 块之外被回调的），
   对外只回一个 JSON + 编号，不回栈。

为什么这些用例不能只测"状态码是 500"
--------------------------------------
500 本来就有。缺的是**留痕**：计数器、日志、编号、栈。只断言状态码的测试，
在修复前后都会绿 —— 那种测试守不住任何东西。所以下面每一条都盯着"痕迹"。
"""

import logging
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi.testclient import TestClient

from app.api import reschedule as R
from app.main import app, metrics

PATH = "/api/reschedule/teachers/%E5%90%B4%E7%90%B3/courses"  # 吴琳


@pytest.fixture
def boom(monkeypatch):
    """让课程卡片接口内部炸掉 —— 模拟"落盘时数据库被锁"这类真实故障。"""
    def _boom(*_a, **_kw):
        raise RuntimeError("落盘时 sqlite 数据库被锁住")

    monkeypatch.setattr(R._db, "get_teacher_courses", _boom, raising=False)
    return _boom


@pytest.fixture
def client():
    # raise_server_exceptions=False：让异常处理器真的跑一遍并产出 500 响应
    return TestClient(app, raise_server_exceptions=False)


def test_unhandled_exception_returns_500_json(client, boom):
    """500 必须是 JSON（带编号），而不是 text/plain 的 "Internal Server Error"。"""
    r = client.get(PATH)

    assert r.status_code == 500
    assert "application/json" in r.headers.get("content-type", ""), \
        f"仍是 {r.headers.get('content-type')} —— 调用方拿不到任何可报的信息"
    body = r.json()
    assert body.get("error_id"), f"没有 error_id：{body}"
    assert "error_id" in body["detail"] or "编号" in body["detail"]


def test_error_id_is_also_the_request_id_header(client, boom):
    """响应体里的编号必须等于 `x-request-id` 头 —— 否则前端报的和日志里的对不上。"""
    r = client.get(PATH)

    rid = r.headers.get("x-request-id", "")
    assert rid, "500 响应没带 x-request-id"
    assert rid == r.json()["error_id"], f"头={rid} 体={r.json()['error_id']}"


def test_unhandled_exception_logs_the_traceback(client, boom, caplog):
    """栈必须落进日志 —— 这是"为什么错"的唯一来源。"""
    caplog.set_level(logging.ERROR, logger="app.error")
    client.get(PATH)

    errors = [rec for rec in caplog.records if rec.name == "app.error"]
    assert errors, f"app.error 一条都没记；实际记录：{[r.name for r in caplog.records]}"
    rec = errors[-1]
    assert rec.exc_info, "记了 ERROR 却没有栈（exc_info 为空）—— 修之前就是这样"
    assert "RuntimeError" in rec.getMessage() or "RuntimeError" in caplog.text
    assert rec.getMessage().startswith("{"), "日志行应是结构化 JSON，便于 grep"


def test_unhandled_exception_counts_as_request_and_error(client, boom):
    """计数器必须动：修复前 requests/errors 一个都不涨，监控里这批请求不存在。"""
    before = metrics()
    client.get(PATH)
    after = metrics()

    assert after["requests"] == before["requests"] + 1
    assert after["errors"] == before["errors"] + 1


def test_http_log_line_marks_the_exception(client, boom, caplog):
    """`app.http` 那行必须带 rid 且标出 exception:true。

    只写 status=500 不够：500 也可能是别处返回的（比如自己 raise HTTPException）。
    标出 "exception":true 才能区分"接口主动拒绝"和"代码炸了"。
    """
    caplog.set_level(logging.WARNING, logger="app.http")
    r = client.get(PATH)

    lines = [rec.getMessage() for rec in caplog.records if rec.name == "app.http"]
    assert lines, "app.http 一行都没记"
    line = "\n".join(lines)
    assert '"exception":true' in line, line
    assert r.headers["x-request-id"] in line, f"日志里没有 rid：{line}"


def test_normal_response_also_carries_request_id(caplog):
    """反向保护：正常响应也要带编号（前后端对账靠它），耗时头不能丢，
    而且**不该**往 warning 日志里塞东西（正常访问不刷屏）。"""
    caplog.set_level(logging.WARNING, logger="app.http")
    r = TestClient(app).get("/health")

    assert r.status_code == 200
    assert len(r.headers.get("x-request-id", "")) == 8
    assert r.headers.get("x-response-time-ms", "").isdigit()
    assert [rec for rec in caplog.records if rec.name == "app.http"] == [], \
        "一个正常的 200 请求被写进了 app.http 的 warning 日志"


def test_exception_is_not_swallowed(client, boom):
    """异常必须继续往上抛 —— 中间件只负责"记下来"，不负责"吃掉"。

    这条防的是最糟的修法：在中间件里 `except Exception: pass`。
    那样 500 变成 200，问题从"看不见"升级成"看不见且假装成功"。
    """
    with pytest.raises(RuntimeError):
        TestClient(app).get(PATH)
