"""落地方案端点族的模块边界（R76：从 `reschedule.py` 搬到 `app/api/landing.py`）

为什么单独一组
--------------
搬迁本身不是缺陷修复，失败时也**不会报错** —— 它只会让已经修好的东西悄悄失效。
三个具体的失效形态，每个都对应下面一条用例：

一、状态被「值绑定」而不是「每次现取」
    `_db` / `_landing_store` / `_land_lock` 是 `reschedule` 的**可变模块状态**。
    运行时与测试靠 `monkeypatch.setattr(reschedule, "_db", db)` 把整条链路指向临时库
    （`test_landing_endpoints.py::env`、`test_event_loop_blocking.py::_land_env`）。
    新模块若写成 `from app.api.reschedule import _db`，拿到的是**导入那一刻**的对象：
    之后所有 patch 全部失明，而且没有任何报错 —— 测试可能仍然是绿的（它自己另建了一份
    库），线上却写进真库。守卫：`landing` 源码里不得出现从 `reschedule` 的
    `from ... import 名字` 形态。

二、锁被复制成两把
    `_land_lock` 同时是 `_db.entries` 的写锁（`_DB_WRITE_LOCK`，R28）。新模块若自己
    `threading.Lock()` 一个，就是两把锁 = 锁等于没锁：R28 修好的「同一间教室同一时段
    被排进两门课」重新打开，而任何单线程测试都发现不了。守卫：必须是**同一个对象**
    （`is` 恒等），不是「同名」「同类型」。

三、路由被搬回去、或者两份都留着
    同一个路径注册两次时 FastAPI 用先注册的那条，后一条静默进 `/openapi.json` ——
    表现为「改了新代码没生效」。守卫：5 条路由的 endpoint 必须都在 `app.api.landing`，
    且 `reschedule` 的模块字典里**没有**同名函数（旧名只经模块级 `__getattr__` 转发）。
"""

import ast
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import app.api.landing as landing
import app.api.reschedule as reschedule
from app.main import app

_BACKEND = os.path.join(os.path.dirname(__file__), "..")
_API_DIR = os.path.join(_BACKEND, "app", "api")

# (方法, 不带前缀的路径, 端点函数名) —— 搬迁前在 reschedule.py 里的那 5 条，一条不多一条不少
_LAND_ROUTES = [
    ("POST", "/parallel/land", "land_commit"),
    ("POST", "/parallel/land/preview", "land_preview"),
    ("POST", "/parallel/land/undo", "land_undo"),
    ("GET", "/parallel/land/history", "land_history"),
    ("GET", "/parallel/land/status", "land_status"),
]
_PREFIX = "/api/reschedule"


# ==================== 一、路由属于新模块，且没有第二份 ====================

def test_land_routes_are_served_by_the_new_module():
    """5 条落地路由必须由 `app.api.landing` 提供；路径与方法一个都不许变。

    注意 FastAPI 这一版把 `include_router` 包成了 `_IncludedRouter`（延迟展开），
    所以这里不去翻 `app.routes`，而是分别看两个 router 自己的 route 表 + openapi
    的最终结果 —— 两头都钉住：**没挂上**和**挂了两份**都能抓到。
    """
    have = {}
    for r in landing.router.routes:
        have.setdefault(r.path, []).append(r)

    for method, path, fn_name in _LAND_ROUTES:
        routes = have.get(path) or []
        assert routes, "landing.router 里没有 %s" % path
        assert len(routes) == 1, (
            "%s 在 landing.router 里注册了 %d 次" % (path, len(routes)))
        route = routes[0]
        assert method in route.methods, "%s 的方法变成了 %s" % (path, sorted(route.methods))
        assert route.endpoint.__module__ == "app.api.landing", (
            "%s 的端点来自 %s" % (path, route.endpoint.__module__))
        assert route.endpoint.__name__ == fn_name, (
            "%s 的端点函数名变了：%s（预期 %s）"
            % (path, route.endpoint.__name__, fn_name))

    assert len(landing.router.routes) == len(_LAND_ROUTES), (
        "landing.router 的路由数不是 5：%d" % len(landing.router.routes))


def test_reschedule_router_no_longer_serves_landing_paths():
    """`reschedule.router` 里不能还有 `/parallel/land*` —— 那就是第二份实现。"""
    leftover = [r.path for r in reschedule.router.routes
                if r.path.startswith("/parallel/land")]
    assert leftover == [], (
        "旧模块仍在提供这些落地路由：%s —— 搬迁留了第二份，"
        "改一边另一边不动（FastAPI 只会命中先注册的那条）" % leftover)


def test_openapi_still_exposes_the_same_five_paths():
    """对前端与文档而言必须一模一样：5 条完整路径 + 方法。"""
    spec = app.openapi()["paths"]
    for method, path, _fn in _LAND_ROUTES:
        full = _PREFIX + path
        assert full in spec, "openapi 里没有 %s —— 前端会 404" % full
        assert method.lower() in spec[full], (
            "%s 的 %s 方法不见了：%s" % (full, method, sorted(spec[full])))


def test_reschedule_keeps_no_second_copy_of_the_handlers():
    """`reschedule` 里不能留同名函数 —— 留着就是两份实现，改一边另一边不动。"""
    names = ["land_commit", "land_preview", "land_undo", "land_history",
             "land_status", "LandPlanRequest", "_land_plan_or_400"]
    still_defined = [n for n in names if n in vars(reschedule)]
    assert still_defined == [], (
        "reschedule 里仍有这些定义：%s —— 搬迁留下了第二份实现" % still_defined)


def test_old_names_are_forwarded_lazily():
    """旧名必须照旧可用（测试用 `api.land_commit` 直接驱动端点），且指向同一个函数。"""
    assert reschedule.land_commit is landing.land_commit
    assert reschedule.LandPlanRequest is landing.LandPlanRequest
    # `from module import name` 走的是同一条 `__getattr__`，不是只有属性访问好用
    from app.api.reschedule import land_undo
    assert land_undo is landing.land_undo


# ==================== 二、状态是现取的，锁是同一把 ====================

def test_landing_does_not_value_bind_reschedule_state():
    """新模块不得 `from app.api.reschedule import <可变状态>`。

    这是搬迁最自然的写法，也是最坏的写法：值绑定之后 monkeypatch 全部失明，
    而且失明是静默的。只允许 `from app.api import reschedule as _api` 这种**模块对象**
    引用，用的时候再取属性（`_api._db`）。

    用 AST 而不是逐行字符串匹配：文档字符串里就写着这个反例（"若这里写
    `from app.api.reschedule import _db`"），逐行扫会把解释文字当成违规。
    """
    path = os.path.join(_API_DIR, "landing.py")
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=path)

    bad = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        mod = node.module or ""
        if mod == "app.api.reschedule" or mod.endswith(".reschedule"):
            names = ", ".join(a.name for a in node.names)
            bad.append("line %d: from %s import %s" % (node.lineno, mod, names))

    assert bad == [], (
        "landing.py 里出现了从 reschedule 的名字级导入 —— 拿到的是导入那一刻的对象，"
        "之后 monkeypatch 全部失明：\n" + "\n".join(bad) +
        "\n应改为模块属性访问（`from app.api import reschedule as _api` + `_api._db`）")


def test_landing_only_locks_the_shared_lock():
    """新模块里每一处**锁**的 `with` 都必须是 `_api._land_lock`（共享的那一把）。

    `is` 恒等只能证明**模块级引用**没错，真正拿锁的是函数体里的表达式：
    `with threading.Lock():` 完全合法、完全静默、且是第二把锁 —— 恒等断言看不见它。
    这里按 AST 看每个 `with` 的上下文表达式（名字里不含 lock 的，如 `open(...)`，跳过）。
    """
    path = os.path.join(_API_DIR, "landing.py")
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=path)

    bad = []
    seen = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.With):
            continue
        for item in node.items:
            label = ast.unparse(item.context_expr)
            seen.append(label)
            if "lock" not in label.lower():
                continue                       # open(...) / transaction() 之类
            if label != "_api._land_lock":
                bad.append("line %d: with %s:" % (node.lineno, label))

    assert "_api._land_lock" in seen, (
        "landing.py 里一处共享锁都没拿到 —— 落地的原子性没了（%s）" % seen)
    assert bad == [], (
        "landing.py 用了共享锁以外的东西当锁（第二把锁 = 锁等于没锁）：\n"
        + "\n".join(bad))


def test_landing_and_reschedule_share_one_lock_object():
    """必须**恒等于是**同一把锁。复制一把 = 两把锁 = 锁等于没锁。"""
    assert landing._api is reschedule, "landing 引用的不是 reschedule 模块本体"
    assert landing._api._land_lock is reschedule._land_lock
    # R28：这把锁同时是 entries 的写锁，别名也必须指同一个对象
    assert reschedule._DB_WRITE_LOCK is reschedule._land_lock


# ==================== 三、R75 的形状不许复发（顺带覆盖新模块） ====================

def _self_scope_nodes(fn_node):
    """`async def` **自身作用域**的节点；不进入内层函数/类/lambda。

    只走一层作用域：`async def` 体内嵌一个**同步**函数、锁（或阻塞活）在那个同步
    函数里，是**正确**形状（R75 的改法：`def _locked_apply(): with _DB_WRITE_LOCK: ...`
    + `run_in_threadpool(_locked_apply)`），不是违规。第一版忘了在第 1 层 `FunctionDef`
    处停住，于是把 `approve_request` / `apply_move` 这两处**正确**写法报成了违规。
    """
    stack = list(fn_node.body)
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                            ast.Lambda, ast.ClassDef)):
            continue
        yield node
        stack.extend(ast.iter_child_nodes(node))


def _expr_name(expr):
    """`f` → "f"；`a.b.f` → "f"（前缀不重要，名字才是被守卫的东西）。"""
    if isinstance(expr, ast.Name):
        return expr.id
    if isinstance(expr, ast.Attribute):
        return expr.attr
    return ""


def _call_name(node):
    """`f(...)` → "f"；`a.b.f(...)` → "f"。"""
    return _expr_name(node.func)


def _api_trees():
    """(文件名, AST) —— `app/api/` 下每个模块。"""
    for name in sorted(os.listdir(_API_DIR)):
        if not name.endswith(".py") or name == "__init__.py":
            continue
        path = os.path.join(_API_DIR, name)
        with open(path, encoding="utf-8") as f:
            yield name, ast.parse(f.read(), filename=path)


def test_no_async_function_holds_the_sync_land_lock():
    """`async def` 里不许出现同步 `with _land_lock:` / `with _DB_WRITE_LOCK:`。

    R75 的缺陷就是这个形状：锁在**事件循环线程**上等，一次被锁的落地/撤销 =
    整站 11 秒不响应（实测心跳滞后 11076 ms）。正确的形状是「锁和活一起进线程池」
    （`run_in_threadpool(_locked_land_commit, ...)`）。

    为什么扫整个 `app/api/`：`test_event_loop_blocking.py` 的源码守卫只读
    `reschedule.__file__`，R76 搬出的 `landing.py` 落在它视野之外 —— 搬家顺手
    把头号缺陷的守卫一起丢了，是最典型的"搬迁静默削弱"。这条把范围补回来。
    """
    guarded = {"_land_lock", "_DB_WRITE_LOCK"}
    offenders = []

    for name, tree in _api_trees():
        for node in ast.walk(tree):
            if not isinstance(node, ast.AsyncFunctionDef):
                continue
            for n in _self_scope_nodes(node):
                if not isinstance(n, ast.With):
                    continue
                for item in n.items:
                    lock = _expr_name(item.context_expr)
                    if lock in guarded:
                        offenders.append("%s:%d async def %s: with %s:"
                                         % (name, n.lineno, node.name, lock))

    assert offenders == [], (
        "异步函数在事件循环线程上等同步锁（R75 的缺陷形态，整站会被按住）：\n"
        + "\n".join(offenders) +
        "\n改法：把 `with <锁>:` 连同受保护的活整段放进同步函数，用 run_in_threadpool 调。")


def test_no_async_endpoint_calls_blocking_helpers_directly():
    """`async def` 里不许直接调 `_ensure_loaded()` / `_persist_master_state()`。

    这两条在 `test_event_loop_blocking.py` 里已有守卫，但它们比的是**整行文本**
    （`line.strip() == "_ensure_loaded()"`），对搬家后的写法 `_api._ensure_loaded()`
    完全失明 —— 而新模块里最自然的写法恰好就是带前缀的那种。
    这里按 AST 看**函数名**，前缀不重要。
    """
    blocking = {"_ensure_loaded", "_persist_master_state"}
    offenders = []

    for name, tree in _api_trees():
        for node in ast.walk(tree):
            if not isinstance(node, ast.AsyncFunctionDef):
                continue
            for n in _self_scope_nodes(node):
                if isinstance(n, ast.Call) and _call_name(n) in blocking:
                    offenders.append("%s:%d async def %s → %s()"
                                     % (name, n.lineno, node.name, _call_name(n)))

    assert offenders == [], (
        "async 端点直接同步调用了阻塞函数（冷启动整表重解析 / 落盘会冻住事件循环）：\n"
        + "\n".join(offenders) +
        "\n应改为 `await ..._async()` 或丢进 run_in_threadpool。")
