"""R80 守卫：工单族搬进 `app/api/requests_board.py` 后必须仍然成立的不变量。

为什么这一族比前四族危险
------------------------
`/requests*` 五条路由不是纯读视图：它们背后是一整套**留在 reschedule.py 的
SQLite 基础设施**（`_DB_PATH` / `_get_db` / `_init_db` / `applied_moves` 重放 /
`/demo/reset` 清表 / 落地方案同库）。二十多个测试用
`monkeypatch.setattr(reschedule, "_DB_PATH", ...)` 把库指到临时目录。

所以这一族搬家有两条独有的失败方式：

1. **状态分叉**：把 `_DB_PATH` 一起搬走。`monkeypatch.setattr` 会在旧模块上
   **新建**一个同名属性，而新模块读的是自己的那一份 ⇒ 测试全绿，写的是真实库。
   值绑定（R77/R78 的老坑）在这里只是它的一个变种。
2. **源码守卫失明**：`test_event_loop_blocking.py` 的
   `test_every_apply_reschedule_call_site_holds_write_lock` 只读
   `reschedule.__file__`。`approve_request` 一搬走，它唯一会抓的
   "调课应用没拿写锁"形态就彻底离开视野 —— **绿着失守**，与 R77 同源。

另外，R79 在模块头与 `_INSIGHTS_MOVED` 注释里写过"M50 锚点在 `/stats`、搬家
要改靶文件"，实测是错的（M50 在 `reschedule.py:929` 的教师角色补全里）。
本轮**真正**随工单族搬家失配的是 **M15（重复审批拦截）与 M16（审批自动应用）**，
它们的锚点分别在 `reschedule.py:1987` / `:2001`。本文件的第 10 条钉住这件事。
"""

import ast
import asyncio
import importlib.util
import io
import json
import os
import pathlib
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
sys.path.insert(0, os.path.dirname(__file__))

from app.api import reschedule
from app.main import app

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_REPO = os.path.dirname(_BACKEND)

try:
    from app.api import requests_board
except ImportError:  # 搬家前：让用例干净地红，而不是收集期 ImportError
    requests_board = None


def _need():
    assert requests_board is not None, (
        "app/api/requests_board.py 尚未创建 —— 工单族仍留在 reschedule.py")


_PREFIX = "/api/reschedule"
_REQUESTS_ROUTES = [
    ("POST", "/requests", "create_request"),
    ("DELETE", "/requests", "clear_requests"),
    ("GET", "/requests", "list_requests"),
    ("GET", "/requests/export", "export_requests"),
    ("POST", "/requests/{request_id}/approve", "approve_request"),
]
_MOVED = frozenset({
    "RescheduleRequestCreate", "RescheduleRequestApprove",
    "create_request", "clear_requests", "list_requests", "export_requests",
    "approve_request",
})
# 只许留一份、且必须仍定义在旧模块里的共享状态与基础设施。
_SHARED = (
    "_DB_PATH", "_get_db", "_init_db", "_DB_WRITE_LOCK", "_apply_reschedule", "_db",
)


def _src_of(mod):
    with io.open(mod.__file__, encoding="utf-8", newline="") as f:
        return f.read().replace("\r\n", "\n")


def _mutants():
    """从 mutation_check.py 源码里读 MUTANTS（不导入，避免跑批副作用）。"""
    p = os.path.join(_BACKEND, "tests", "mutation_check.py")
    with io.open(p, encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=p)
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                getattr(t, "id", "") == "MUTANTS" for t in node.targets):
            return ast.literal_eval(node.value)
    raise AssertionError("mutation_check.py 里找不到 MUTANTS 列表")


# ==================== 一、路由属于新模块，且没有第二份 ====================

def test_requests_routes_are_served_by_the_new_module():
    """5 条路由必须由 requests_board.router 提供；路径 / 方法 / 函数名一字不变。"""
    _need()
    have = {(r.path, m): r for r in requests_board.router.routes for m in r.methods}
    for method, path, fn in _REQUESTS_ROUTES:
        r = have.get((path, method))
        assert r is not None, f"{method} {path} 没挂到 requests_board.router 上"
        assert r.endpoint.__name__ == fn, (path, r.endpoint.__name__)


def test_no_duplicate_method_and_path_registration():
    """`/requests` 一个路径挂 3 个方法是合法的；同 (path, method) 两份不合法。"""
    _need()
    seen = set()
    for r in requests_board.router.routes:
        for m in r.methods:
            key = (r.path, m)
            assert key not in seen, f"{key} 在新模块里注册了两次"
            seen.add(key)


def test_openapi_paths_are_unchanged():
    """最终 openapi 里这 5 个方法/路径都在 —— 能同时抓到「没挂上」和「挂错前缀」。"""
    _need()
    spec = app.openapi()
    for method, path, _fn in _REQUESTS_ROUTES:
        full = _PREFIX + path
        assert full in spec["paths"], f"{full} 不在 openapi 里"
        assert method.lower() in spec["paths"][full], (full, spec["paths"][full].keys())


def test_old_module_no_longer_serves_requests_paths():
    """旧模块不许留第二份：否则先注册的那条会静默胜出。"""
    _need()
    old = {r.path for r in reschedule.router.routes}
    for _m, path, _fn in _REQUESTS_ROUTES:
        assert path not in old, f"{path} 仍留在 reschedule.router 里"


def test_apply_move_stays_in_the_old_module():
    """`/apply-move` 是审批台闭环，不属于工单族；搬过头会连 M27 锚点一起丢。"""
    _need()
    assert "/apply-move" in {r.path for r in reschedule.router.routes}
    assert "/apply-move" not in {r.path for r in requests_board.router.routes}


def test_requests_routes_still_require_admin():
    """搬家最容易漏的是装饰器上的 `_ADMIN_ONLY`：路径还在，匿名可写。"""
    _need()
    client = TestClient(app)
    assert client.get("/api/reschedule/requests").status_code == 401
    assert client.get("/api/reschedule/requests/export").status_code == 401


# ==================== 二、状态必须留在旧模块、且每次现取 ====================

def test_sqlite_infrastructure_stays_defined_in_reschedule():
    """共享状态真定义在旧模块的 `__dict__`，新模块一份都不许自己存。

    `monkeypatch.setattr(reschedule, "_DB_PATH", ...)` 会在旧模块上写属性；
    新模块若自己也定义了 `_DB_PATH`，这个 patch 对它**完全不可见**。
    """
    _need()
    for name in _SHARED:
        assert name in vars(reschedule), f"{name} 被搬出了 reschedule"
        assert name not in vars(requests_board), f"{name} 在新模块里出现了第二份"


def test_no_value_binding_of_reschedule_state():
    """AST：不许 `from app.api.reschedule import _db` 或模块级 `_db = _api._db`。"""
    _need()
    tree = ast.parse(_src_of(requests_board))
    banned = set(_SHARED) | {"_master_data", "_master_tasks", "_landings",
                             "_land_lock", "_landing_store", "_agent"}
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("app.api"):
            for a in node.names:
                assert a.name not in banned, f"状态 {a.name} 被值导入了"
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in targets:
                if isinstance(t, ast.Name) and t.id in banned:
                    raise AssertionError(f"状态 {t.id} 在新模块里被绑成了模块级值")


def test_get_db_is_looked_up_on_every_call(monkeypatch):
    """行为锁：替换旧模块的 `_get_db`，`list_requests()` 必须立刻用到替换版。

    AST 只能证明"没写值绑定"，证明不了运行时代价。写成
    `from app.api.reschedule import _get_db` 时这条会安静地读真实库。
    """
    _need()
    seen = []

    def fake_get_db():
        seen.append(1)
        raise RuntimeError("PROBE_GET_DB")

    monkeypatch.setattr(reschedule, "_get_db", fake_get_db)
    with pytest.raises(RuntimeError, match="PROBE_GET_DB"):
        asyncio.run(requests_board.list_requests())
    assert seen == [1]


def test_approve_uses_the_current_apply_reschedule(monkeypatch, tmp_path):
    """行为锁：审批的自动应用必须调到旧模块**当下**的 `_apply_reschedule`。

    这不是为了好看 —— `test_request_workflow.py` 的三条用例正是靠
    `monkeypatch.setattr(api, "_apply_reschedule", fake)` 才能覆盖同意分支。
    """
    _need()
    monkeypatch.setattr(reschedule, "_DB_PATH", str(tmp_path / "probe.db"))
    monkeypatch.setattr(reschedule, "_landing_store", None)
    reschedule._init_db()
    conn = reschedule._get_db()
    conn.execute(
        "INSERT INTO requests (teacher, course_name, target_week, status, created_at) "
        "VALUES (?, ?, ?, ?, ?)", ("T", "C", 5, "pending", "t"))
    conn.commit()
    conn.close()

    monkeypatch.setattr(reschedule, "_db", type("_D", (), {"loaded": True})())
    calls = []

    def fake_apply(rec):
        calls.append(rec)
        return {"applied": 1, "skipped": 0, "details": []}

    monkeypatch.setattr(reschedule, "_apply_reschedule", fake_apply)
    out = asyncio.run(requests_board.approve_request(
        1, requests_board.RescheduleRequestApprove(approve=True)))
    assert calls, "审批没有调用旧模块当下的 _apply_reschedule"
    assert out["success"] is True and "已同意并自动应用" in out["message"]


# ==================== 三、旧名转发与请求体不分叉 ====================

def test_forwarding_set_matches_the_moved_names():
    _need()
    assert reschedule._REQUESTS_MOVED == _MOVED


def test_old_names_resolve_to_the_new_module():
    """`api.create_request(...)` 这类直调必须拿到同一个对象，不是拷贝。"""
    _need()
    for name in _MOVED:
        assert getattr(reschedule, name) is getattr(requests_board, name), name


def test_request_bodies_are_not_duplicated():
    _need()
    assert "ApplyMoveRequest" in vars(reschedule)
    assert "ApplyMoveRequest" not in vars(requests_board)
    assert reschedule.RescheduleRequestCreate is requests_board.RescheduleRequestCreate
    assert reschedule.RescheduleRequestApprove is requests_board.RescheduleRequestApprove


# ==================== 四、变异锚点真的跟着搬（R79 写错了对象） ====================

def test_m15_m16_anchors_followed_the_move():
    """M15 / M16 的靶文件必须是新模块，且锚点在其中恰好命中一次。

    mutation_check.py 按 (文件, 原文片段) 定位；失配的表现是跑批时**跳过**
    （不是失败），而变异跑批不在 CI 里。R79 注释点名的是 M50，实测它不在
    搬家区间 —— 本轮真正需要改的只有这两条。
    """
    _need()
    muts = _mutants()
    by_tag = {m[0]: m for m in muts}
    src = _src_of(requests_board)
    for tag in ("M15", "M16"):
        m = by_tag[tag]
        assert m[2] == "app/api/requests_board.py", f"{tag} 靶文件没跟着搬"
        assert src.count(m[3]) == 1, f"{tag} 的锚点在新模块里命中 {src.count(m[3])} 次"
    moved = sorted(m[0] for m in muts if m[2] == "app/api/requests_board.py")
    assert moved == ["M15", "M16"], f"不该跟着搬的锚点也被改了：{moved}"
    assert by_tag["M27"][2] == "app/api/reschedule.py", "M27（/apply-move）应留在旧模块"


def test_every_mutation_anchor_still_matches_its_file():
    """56 条锚点全表自检：任何一条失配都意味着对应防护暂时没人守。"""
    _need()
    problems = []
    for tag, title, rel, old, _new, _expect in _mutants():
        p = os.path.join(_BACKEND, rel.replace("/", os.sep))
        if not os.path.isfile(p):
            problems.append(f"{tag}: 文件不存在 {rel}")
            continue
        with io.open(p, encoding="utf-8", newline="") as f:
            text = f.read().replace("\r\n", "\n")
        if text.count(old) != 1:
            problems.append(f"{tag} {title}: 命中 {text.count(old)} 次")
    assert problems == [], "锚点失配：\n  - " + "\n  - ".join(problems)


# ==================== 五、目录级门不许因搬家缩小 ====================

def test_source_level_lock_guard_scans_the_whole_api_package():
    """R28 的"每个 _apply_reschedule 调用点都持写锁"守卫必须扫整个 api 目录。

    它原先只读 `reschedule.__file__`。`approve_request` 一搬走，守卫视野里
    就一个调用点都不剩 —— **绿着失守**，与 R77 的"三道门静默缩小"同源。
    """
    _need()
    p = os.path.join(_BACKEND, "tests", "test_event_loop_blocking.py")
    spec = importlib.util.spec_from_file_location("_teb_probe_r80", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    it = getattr(mod, "_iter_api_sources", None)
    assert it is not None, (
        "test_event_loop_blocking.py 的源码守卫仍只读 reschedule.__file__，"
        "新模块里的调用点不在它的视野内")
    names = {n for n, _src in it()}
    expected = {f.name for f in pathlib.Path(
        os.path.join(_BACKEND, "app", "api")).glob("*.py") if f.name != "__init__.py"}
    assert names == expected, f"源码守卫扫描集合 {sorted(names)} != {sorted(expected)}"


def test_async_blocking_audit_discovers_the_new_module():
    """CI 里的审计门一行没改，扫描文件数必须自己从 7 变 8。"""
    _need()
    script = os.path.join(_REPO, "scripts", "audit_async_blocking.py")
    r = subprocess.run([sys.executable, script], capture_output=True, text=True,
                       cwd=_REPO, timeout=120)
    assert r.returncode == 0, (r.stdout[-1200:] + r.stderr[-1200:])
    assert "requests_board.py" in r.stdout, "审计门没发现新模块"


def test_zero_await_baseline_still_names_the_request_routes():
    """零 await 基线的键是"路径 + 函数名"，搬家不该让它多出/少掉任何一条。"""
    _need()
    p = os.path.join(_REPO, "scripts", "async_blocking_baseline.json")
    with io.open(p, encoding="utf-8") as f:
        routes = json.load(f)["routes"]
    for key in ("/requests  clear_requests", "/requests  create_request",
                "/requests  list_requests", "/requests/export  export_requests"):
        assert key in routes, f"基线里少了 {key}"
    assert len(routes) == 8, f"零 await 基线从 8 条变成了 {len(routes)} 条"


def test_export_requests_keeps_its_measured_exemption():
    """`/requests/export` 是有实测数字的豁免，搬家时说明不许被删成一句空话。"""
    _need()
    doc = requests_board.export_requests.__doc__ or ""
    assert "15.2ms" in doc and "50ms" in doc, "实测数字丢了，豁免会变成无据可查的洗白"
