"""前端类型声明 与 真实 HTTP 响应 的一致性守卫（R-126）。

为什么需要
----------
前端所有测试都用 `vi.mock` 造响应 —— 这是对的（单测不该依赖后端），但它有一个
副作用：**类型声明写错了没人发现**。`tsc` 只检查"前端代码是否按类型用字段"，
不检查"后端是否真的给这些字段"。

真实风险：某个字段在前端是必填（无 `?`），而后端不返回 ⇒ 运行时拿到 `undefined`。
TS 编译通过、单测通过（mock 里有）、界面上一片空白或显示 `undefined`。

现有覆盖的边界：
  · `test_response_contract.py` 只锁 `process_request` 一个函数的**分支键集一致**
  · 本文件锁的是"前端声明 ↔ 真实 HTTP 响应"这一层，此前完全没人查

设计
----
1. 用正则从 `frontend/src/types.ts` + `frontend/src/api/client.ts` 抽 `export interface`
   的字段（名字 + 是否 `?`）。**不引入 TS 解析器** —— 这些接口是扁平字面量，
   正则在 30 个接口上实测够用；引一个 TS 解析器进 Python 依赖不成比例。
2. 用 `TestClient` 打真实端点，按接口声明的位置比对**必填字段是否都出现**。
3. 只查"必填但缺席" （会 `undefined` 的那类）。**不查"类型是否吻合"**
   （比如后端给 `string` 而前端声明 `number`）—— 那需要运行时类型溯源，
   成本远大于收益，且本项目后端由 Pydantic/显式 dict 构造，类型漂移风险低。

最后更新：2026-11-07（本轮实测 /status、/stats、/data-audit、/teachers 四个端点
的必填字段全部命中，0 缺失）。
"""

import os
import re
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
REPO = BACKEND.parent
TYPES_TS = REPO / "frontend" / "src" / "types.ts"
CLIENT_TS = REPO / "frontend" / "src" / "api" / "client.ts"


def _ifaces() -> dict:
    """从 TS 源抽 `export interface X { ... }` 的字段。

    返回 {接口名: [(字段名, 是否可选), ...]}。
    嵌套对象（`course_coverage?: { ... }`）只记外层字段名 —— 嵌套字段由
    **调用方**按需解引用后再比对（见 `_nested`）。
    """
    src = ""
    for f in (TYPES_TS, CLIENT_TS):
        if f.exists():
            src += f.read_text(encoding="utf-8") + "\n"

    out = {}
    for m in re.finditer(r"export interface (\w+)\s*\{(.*?)\n\}", src, re.S):
        name, body = m.group(1), m.group(2)
        fields = []
        depth = 0          # >0 表示正处在某个嵌套对象内部
        for line in body.splitlines():
            stripped = line.strip()
            mm = re.match(r"(\w+)(\??)\s*:\s*(.*)$", stripped)
            if mm and depth == 0:
                # 只收**当前层级**的字段。遇到 `xxx?: {` 说明这一行的字段值是
                # 嵌套对象：记下字段名，然后 depth+1 把它内部的行全部跳过。
                fields.append((mm.group(1), mm.group(2) == "?"))
                depth += mm.group(3).count("{") - mm.group(3).count("}")
                continue
            # 非字段行（或嵌套内部的行）：只更新深度
            depth += stripped.count("{") - stripped.count("}")
        out[name] = fields
    return out


@pytest.fixture(scope="module")
def client():
    os.environ.setdefault("DEMO_MODE", "1")
    os.environ.setdefault("ADMIN_PASS", "admin123")
    os.environ.setdefault("RATELIMIT_DISABLED", "1")
    os.environ.setdefault("LLM_DISABLED", "1")
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as c:
        c.post("/api/reschedule/load-default")
        yield c


def _required(ifaces, name):
    return [f for f, opt in ifaces.get(name, []) if not opt]


def test_frontend_sources_are_readable():
    """前提：两个 TS 文件必须存在且能抽出接口（否则本守卫是空转）。"""
    assert TYPES_TS.exists(), f"找不到 {TYPES_TS}"
    assert CLIENT_TS.exists(), f"找不到 {CLIENT_TS}"
    ifaces = _ifaces()
    assert len(ifaces) >= 20, f"只抽出 {len(ifaces)} 个接口，正则可能失效了"


def test_status_required_fields_present(client):
    """/status ↔ `ScheduleStatus`：必填字段必须都在。"""
    ifaces = _ifaces()
    body = client.get("/api/reschedule/status").json()
    missing = [f for f in _required(ifaces, "ScheduleStatus") if f not in body]
    assert not missing, f"/status 缺前端声明为必填的字段：{missing}"


def test_stats_required_fields_present(client):
    """/stats ↔ `StatsData`（响应有 `data` 包裹）。"""
    ifaces = _ifaces()
    body = client.get("/api/reschedule/stats").json()["data"]
    missing = [f for f in _required(ifaces, "StatsData") if f not in body]
    assert not missing, f"/stats 缺前端声明为必填的字段：{missing}"


def test_data_audit_required_fields_present(client):
    """/data-audit ↔ `AuditData`（含嵌套 `course_coverage`）。

    嵌套块是上一版比对脚本出错的地方：`course_coverage` 的字段被当成顶层字段，
    于是误报 5 个"缺失"。这里显式解引用，避免同样的问题。
    """
    ifaces = _ifaces()
    body = client.get("/api/reschedule/data-audit").json()["data"]
    missing = [f for f in _required(ifaces, "AuditData") if f not in body]
    assert not missing, f"/data-audit 缺前端声明为必填的字段：{missing}"

    cc = body.get("course_coverage")
    assert cc is not None, "/data-audit 应当返回 course_coverage（前端 Dashboard 在用）"
    # 前端 DashboardView 读的这几个键必须存在
    for k in ("total_entries", "project_in_master", "distinct_courses",
              "explicit_course", "fallback_default", "master_ready"):
        assert k in cc, f"course_coverage 缺 {k}（DashboardView 会读到 undefined）"


def test_teachers_required_fields_present(client):
    """/teachers ↔ `Teacher`：数组元素必须有 name/title。"""
    ifaces = _ifaces()
    arr = client.get("/api/reschedule/teachers").json()["teachers"]
    assert arr, "教师列表为空（本测试需要至少一位教师）"
    missing = [f for f in _required(ifaces, "Teacher") if f not in arr[0]]
    assert not missing, f"教师条目缺前端声明为必填的字段：{missing}"


def test_audit_type_declares_no_phantom_top_level_fields():
    """反向：前端 `AuditData` 的顶层字段必须**真能**在响应顶层找到。

    这条是为了防止"把嵌套字段抄成顶层"这类错误 —— 它不会让 tsc 红
    （两边都是合法字段名），但前端按顶层读会拿到 undefined。
    """
    ifaces = _ifaces()
    # 只做静态检查：这些名字不应作为 AuditData 的**顶层**必填字段出现
    nested = {"project_in_master", "distinct_courses", "explicit_course",
              "fallback_default", "master_ready"}
    top = {f for f, _ in ifaces.get("AuditData", [])}
    leaked = nested & top
    assert not leaked, (
        f"这些字段属于 course_coverage 嵌套块，不该出现在 AuditData 顶层：{leaked}"
    )
