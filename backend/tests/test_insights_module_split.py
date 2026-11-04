"""R79 守卫：统计/体检族搬进 `app/api/insights.py` 后必须仍然成立的不变量。

为什么这一族要单独钉
--------------------
`/data-audit`、`/stats`、`/schedule-grid`、`/impact-analysis` 是四种**只读视图**，
共用同一份内存排课表 `_db` 与主数据 `_master_data`。它们没有共享缓存，搬家时
最容易出的不是 ImportError，而是三类"全绿但已经不对"的退化：

1. **值绑定**：新模块写成 `from app.api.reschedule import _db`。此后测试里
   `monkeypatch.setattr(reschedule, "_db", ...)` 对新模块**静默失明** ——
   用例还在过，量的却是另一份状态。R77/R78 都栽在这一条上。
2. **路由留两份**：旧模块没删干净。同路径注册两次，FastAPI 只用先注册那条，
   后一条静默进 openapi —— 表现为"改了新代码没生效"。
3. **旧名没转发**：`tests/test_data_audit.py`、`test_stats_capacity.py`、
   `test_impact_analysis.py`、`test_slot_span.py`、`test_week_bounds.py` 里有 7 处
   `api.data_audit()` / `api.stats()` / `api.impact_analysis()` 模块属性直调。
   不转发就会在收集期炸掉一整片测试。

搬家前一度以为变异体 M50（"负载基线不认系统自己补的教师"）的锚点在 `/stats`
里、会随本轮搬家失配。**实测推翻**：它的原文在 `reschedule.py:930`，属于教师
角色补全（`_enrich_teacher_roles`），不在本轮 334 行区间。全表 56 条锚点由
`test_master_module_split.py::test_every_mutation_anchor_still_matches_its_file`
逐条守着，本文件不重复锁同一件事。
"""

import ast
import io
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))  # mutation_check 不在包里，按文件导入

from app.api import reschedule
from app.main import app

try:
    from app.api import insights
except ImportError:  # 搬家前：让用例干净地红，而不是收集期 ImportError
    insights = None


def _need():
    assert insights is not None, (
        "app/api/insights.py 尚未创建 —— 统计/体检族仍留在 reschedule.py")


# 搬迁前挂在 `app.api.reschedule` 上、现在必须由 `app.api.insights` 提供的路由。
_INSIGHTS_ROUTES = [
    ("GET", "/data-audit", "data_audit"),
    ("GET", "/stats", "stats"),
    ("GET", "/schedule-grid", "schedule_grid"),
    ("POST", "/impact-analysis", "impact_analysis"),
]
_PREFIX = "/api/reschedule"

# 从 reschedule 搬走的**顶层名字**（不含仍留在旧模块的 ChatRequest）。
_MOVED = frozenset({
    "data_audit", "stats", "schedule_grid", "impact_analysis", "_course_gap_note",
})


# ==================== 一、路由属于新模块，且没有第二份 ====================

def test_insights_routes_are_served_by_the_new_module():
    """4 条路由必须由 insights.router 提供；路径、方法、函数名一个不许变。"""
    _need()
    have = {}
    for r in insights.router.routes:
        have.setdefault(r.path, []).append(r)
    for method, path, fn in _INSIGHTS_ROUTES:
        assert path in have, f"{path} 没挂到 insights.router 上"
        routes = have[path]
        assert len(routes) == 1, f"{path} 在 insights.router 里注册了 {len(routes)} 次"
        assert method in routes[0].methods, (path, routes[0].methods)
        assert routes[0].endpoint.__name__ == fn, (path, routes[0].endpoint.__name__)


def test_openapi_paths_are_unchanged_and_unique():
    """最终 openapi 里这 4 个路径存在、方法正确，且没有重复注册。

    `app.routes` 在 FastAPI 0.141 里是延迟展开的 `_IncludedRouter`，所以看 openapi
    的结果才作数 —— 它同时能抓到"没挂上"和"挂了两份"。
    """
    _need()
    spec = app.openapi()
    for method, path, _fn in _INSIGHTS_ROUTES:
        full = _PREFIX + path
        assert full in spec["paths"], f"{full} 不在 openapi 里"
        assert method.lower() in spec["paths"][full], (full, spec["paths"][full].keys())
    # 全表内任何路径都不许出现两条同方法的路由（同路径 GET+POST 是合法的）
    for r in insights.router.routes:
        assert sum(1 for x in insights.router.routes if x.path == r.path) == 1


def test_old_module_no_longer_serves_insights_paths():
    """旧模块不许留第二份：否则先注册的那条会静默胜出。"""
    _need()
    old = {r.path for r in reschedule.router.routes}
    for _m, path, _fn in _INSIGHTS_ROUTES:
        assert path not in old, f"{path} 仍留在 reschedule.router 里"


# ==================== 二、状态必须现取，不许值绑定 ====================

def test_no_value_binding_of_reschedule_state():
    """AST：不许 `from app.api.reschedule import _db`，也不许模块级 `_db = _api._db`。

    值绑定之后 monkeypatch 旧模块会失明，而且**测试全绿**——这是本族最危险的一种
    退化，因为所有读表用例都靠替换 `reschedule._db` 造数据。
    """
    _need()
    with io.open(insights.__file__, encoding="utf-8") as f:
        src = f.read()
    tree = ast.parse(src)
    state = {"_db", "_master_data", "_agent", "_master_tasks", "_landings"}
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("app.api"):
            for a in node.names:
                assert a.name not in state, f"值绑定了状态 {a.name}"
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id in state:
                    pytest.fail(f"模块级绑定了状态 {t.id}")
    assert "_api._db" in src, "新模块必须经 _api 现取排课表"


def test_state_is_read_live_from_reschedule(monkeypatch):
    """行为锁：替换 reschedule._db 之后，新模块的 /stats 必须读到**替换后**的那份。

    AST 守卫只能证明"没写值绑定"，证明不了"运行时确实现取"。这条补上后者。
    """
    _need()
    import asyncio

    class FakeEntry:
        week, weekday, room, teacher, experiment = 3, "周五", "A999", "探针老师", "探针项目"
        time_slot, blocked = "567", None

    class FakeDB:
        def __init__(self):
            self.entries = [FakeEntry()]
            self.skipped = []
            self.loaded = True

    fake = FakeDB()
    monkeypatch.setattr(reschedule, "_db", fake)
    out = asyncio.run(insights.stats())["data"]
    assert out["n_weeks"] == 1
    assert out["room_count"] == 1 and out["busiest_room"]["room"] == "A999"
    assert out["teacher_count"] == 1 and out["top_teachers"][0]["teacher"] == "探针老师"


def test_chat_request_stays_shared():
    """ChatRequest 仍属 reschedule（/chat 也在用），新模块不许自建第二份模型。"""
    _need()
    assert "ChatRequest" not in vars(insights), "新模块另建了 ChatRequest，请求体会分叉"
    assert reschedule.ChatRequest is not None


# ==================== 三、旧名转发与守卫自身的覆盖面 ====================

def test_forwarded_names_equal_what_moved():
    """`_INSIGHTS_MOVED` 必须恰好等于搬走的顶层名字，少一个就漏转、多一个就骗人。

    用 `vars(new_module)` 判"真的定义在这里"，不用 `getattr` —— 后者会被
    `__getattr__` 转发骗过去，看起来什么都在。
    """
    _need()
    declared = getattr(reschedule, "_INSIGHTS_MOVED", None)
    assert declared is not None, "reschedule 没有 _INSIGHTS_MOVED 转发清单"
    assert set(declared) == _MOVED, (set(declared) ^ _MOVED)
    for name in _MOVED:
        assert name in vars(insights), f"{name} 没真的定义在 insights 里"
        assert getattr(reschedule, name) is getattr(insights, name), f"{name} 转发拿到的不是同一对象"


def test_legacy_module_attribute_calls_still_work():
    """5 个测试文件里的 `api.data_audit()` / `api.stats()` 等直调不许炸。"""
    _need()
    for name in ("data_audit", "stats", "schedule_grid", "impact_analysis"):
        assert callable(getattr(reschedule, name))


def test_shared_state_stays_defined_in_reschedule_and_is_the_same_object():
    """状态只许有**一份**：`_db` / `_master_data` / `_agent` / `ChatRequest` 仍在
    reschedule 顶层真定义，且新模块按 `_api.xxx` 取到的就是同一个对象。

    这一族没有共享缓存，搬家时最容易"顺手把 `_db` 一起搬过来"—— 于是 `/chat`
    与 `/stats` 各持一份排课表，功能全对，直到有人上传新表才发现统计没跟着变。
    用 `vars()` 判"真的定义在这里"，不用 `getattr`（会被 `__getattr__` 转发骗过）。
    """
    _need()
    for name in ("_db", "_master_data", "_agent", "ChatRequest"):
        assert name in vars(reschedule), f"{name} 被一起搬走了，状态会分叉"
    assert insights._api is reschedule, "新模块取状态的 _api 不是同一个模块对象"
    assert insights.impact_analysis.__annotations__["req"] is reschedule.ChatRequest


def test_every_api_router_module_is_scanned_by_the_audit_gate():
    """审计门不给参数时扫整个目录，输出必须列出 insights.py。

    R77 的教训：CI 里"记得给每个新模块加一行 run"迟早会漏；目录自发现才会自动覆盖。
    """
    _need()
    import subprocess

    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    script = os.path.join(repo_root, "scripts", "audit_async_blocking.py")
    assert os.path.isfile(script), "审计脚本不见了：%s" % script
    # 脚本按相对路径找 backend/app/api，必须以仓库根为 CWD（R77 同款）
    r = subprocess.run([sys.executable, script], cwd=repo_root,
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "insights.py" in (r.stdout + r.stderr), "审计门输出里没有 insights.py"


def test_impact_analysis_still_dispatches_parsing_to_a_worker():
    """`/impact-analysis` 的 LLM 解析必须仍在线程池上（R-19 第 6 处）。

    搬家中如果把 `run_in_threadpool(_api._agent.parse_request, ...)` 顺手改成直调，
    规则解析不出来时同步打 LLM（上限 60s）会把整个事件循环拖住。
    """
    _need()
    with io.open(insights.__file__, encoding="utf-8") as f:
        src = f.read()
    tree = ast.parse(src)
    fn = next(n for n in tree.body
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "impact_analysis")
    body = ast.unparse(fn)
    assert "run_in_threadpool" in body and "parse_request" in body
    assert "await _api._agent.parse_request" not in body
