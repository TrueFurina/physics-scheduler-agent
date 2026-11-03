"""R78 守卫：导出族搬进 `app/api/export.py` 后，这些不变量必须仍然成立。

为什么这一族要单独钉
--------------------
导出是**同一份数据的多个投影**，共用一套字节缓存 + 一套预热 + 一套门禁。
搬家时最容易出的不是 ImportError，而是三类"看起来完全正常"的退化：

1. **两份缓存**：只搬走一半端点，两个模块各有一个 `_EXPORT_CACHE`。功能全对，
   命中率腰斩 —— 既有的缓存用例（`test_export_cache.py`）照样绿，因为每一份
   内部都自洽。
2. **值绑定**：新模块写成 `from app.api.reschedule import _db`。之后所有
   `monkeypatch.setattr(reschedule, "_db", ...)` 对新模块**静默失明**，
   而失明的表现是"测试还在过，量的却是另一份状态"。
3. **路由留了两份**：旧模块没删干净。FastAPI 同一个路径注册两次只会用先注册的
   那条，后一条静默进 openapi —— 表现为"改了新代码没生效"。

本文件对这三类各有一条判据，另加"转发集合必须等于新模块的实际内容"（防漏转）。
"""

import ast
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.api import export, reschedule
from app.main import app

_API_DIR = os.path.dirname(os.path.abspath(export.__file__))


# 搬迁前挂在 `app.api.reschedule` 上、现在必须由 `app.api.export` 提供的路由。
# 一条不多一条不少：方法与路径都与 R77 之前逐字相同（`path` 不含挂载前缀）。
_EXPORT_ROUTES = [
    ("GET", "/export/class-schedule", "export_class_schedule"),
    ("GET", "/export/room-schedule", "export_room_schedule"),
    ("GET", "/export/master-data", "export_master_data"),
    ("GET", "/export/master-workbook", "export_master_workbook"),
    ("POST", "/auto-schedule/export", "auto_schedule_export"),
    ("POST", "/parallel/export", "parallel_export"),
]
_PREFIX = "/api/reschedule"

# 只有这 6 条算"导出一族"。`/requests/export` 名字里也有 export，但它属于工单族。
_FAMILY = ("/export/", "/auto-schedule/export", "/parallel/export")


# ==================== 一、路由属于新模块，且没有第二份 ====================

def test_export_routes_are_served_by_the_new_module():
    """6 条导出路由必须由 `app.api.export` 提供；路径、方法、函数名一个不许变。

    注意 FastAPI 这一版把 `include_router` 包成了 `_IncludedRouter`（延迟展开），
    所以这里不去翻 `app.routes`（那里看不到被包含的子路由），而是看 router 自己的
    route 表 + openapi 的最终结果 —— 两头都钉住：**没挂上**和**挂了两份**都能抓到。
    """
    have = {}
    for r in export.router.routes:
        have.setdefault(r.path, []).append(r)

    for method, path, fn_name in _EXPORT_ROUTES:
        routes = have.get(path) or []
        assert routes, "export.router 里没有 %s" % path
        assert len(routes) == 1, (
            "%s 在 export.router 里注册了 %d 次" % (path, len(routes)))
        route = routes[0]
        assert method in route.methods, "%s 的方法变成了 %s" % (path, sorted(route.methods))
        assert route.endpoint.__module__ == "app.api.export", (
            "%s 的端点来自 %s —— 搬迁没做完，或者被搬回去了"
            % (path, route.endpoint.__module__))
        assert route.endpoint.__name__ == fn_name, (
            "%s 的端点函数名变了：%s（预期 %s）"
            % (path, route.endpoint.__name__, fn_name))

    assert len(export.router.routes) == len(_EXPORT_ROUTES), (
        "export.router 的路由数不是 %d：%d —— 多出来的那条要么没写进清单，"
        "要么是重复注册" % (len(_EXPORT_ROUTES), len(export.router.routes)))


def test_openapi_paths_are_unchanged_and_unique():
    """`/openapi.json` 里这 6 条仍然各出现一次，且挂在 `/api/reschedule` 前缀下。

    路径变了前端就 404 —— 这是"搬家不改调用方"的最终判据。
    """
    paths = app.openapi()["paths"]
    problems = []
    for method, path, _ in _EXPORT_ROUTES:
        full = _PREFIX + path
        if full not in paths:
            problems.append("openapi 里没有 %s（可能是路径被改了）" % full)
            continue
        if method.lower() not in paths[full]:
            problems.append("%s 的方法里没有 %s" % (full, method))
    assert problems == [], "\n".join(problems)


def test_requests_export_stays_in_reschedule():
    """`/requests/export` **不**搬：它是 SQLite 工单的投影，不读排课表、不走导出缓存。

    这条是防止"看到 export 两个字就一起扫过来" —— 把它搬进 `export.py` 会让
    "导出一族"这个概念变糊，而它本身一次建表只要 5–30ms（docstring 里有实测）。
    """
    routes = [r for r in reschedule.router.routes if r.path == "/requests/export"]
    assert len(routes) == 1, "工单导出路由不见了，或被注册了多次"
    assert routes[0].endpoint.__module__ == "app.api.reschedule", (
        "`/requests/export` 被搬进 %s 了 —— 它属于工单族，不该进导出一族"
        % routes[0].endpoint.__module__)


def test_old_modules_no_longer_serve_export_paths():
    """`reschedule.router` 上不许还挂着 `/export` 或 `/auto-schedule/export`。"""
    leftover = [r.path for r in reschedule.router.routes
                if r.path.startswith(_FAMILY)]
    assert leftover == [], (
        "旧模块仍在提供这些导出路由：%s —— 搬迁留了第二份，改一边另一边不动" % leftover)


# ==================== 二、状态必须现取，不许值绑定 ====================

def test_export_does_not_value_bind_reschedule_state():
    """新模块不得 `from app.api.reschedule import <可变状态>`。

    这是搬迁最自然的写法，也是最坏的写法：值绑定之后 monkeypatch 全部失明，
    而且失明是**静默的**。只允许 `from app.api import reschedule as _api` 这种
    模块对象引用，用的时候再取属性（`_api._db`）。
    """
    with open(os.path.join(_API_DIR, "export.py"), encoding="utf-8") as f:
        src = f.read()

    bad = []
    for i, line in enumerate(src.splitlines(), 1):
        s = line.strip()
        if s.startswith("#"):
            continue
        if "from app.api.reschedule import" in s or "from .reschedule import" in s:
            bad.append("line %d: %s" % (i, s))

    assert bad == [], (
        "export.py 里出现了从 reschedule 的名字级导入 —— 拿到的是导入那一刻的对象，"
        "之后 monkeypatch 全部失明：\n" + "\n".join(bad) +
        "\n应改为模块属性访问（`from app.api import reschedule as _api` + `_api._db`）")


def test_export_reads_the_same_state_objects_as_reschedule():
    """`_api` 必须就是 reschedule 模块本体，取到的状态必须是同一个对象。"""
    assert export._api is reschedule, "export 引用的不是 reschedule 模块本体"
    assert export._api._db is reschedule._db
    assert export._api._master_data is reschedule._master_data


# ==================== 三、缓存与预热只有一份 ====================

def test_only_one_export_cache_exists():
    """缓存对象必须**唯一**：两份缓存 = 命中率腰斩，而功能完全正常。

    判据是同一个对象（`is`），不是"内容相等"：搬到新模块后若旧位置还留一份
    （哪怕已没人用），下一次有人往旧的那份里加东西就会两边漂移。
    """
    assert export._EXPORT_CACHE is reschedule._EXPORT_CACHE, (
        "`_EXPORT_CACHE` 不是同一个对象 —— 转发出错，或者又建了一份")
    assert export._cached_export is reschedule._cached_export
    assert reschedule._EXPORT_CACHE_MAX == export._EXPORT_CACHE_MAX == 12


def test_warmup_is_reachable_from_both_sides():
    """预热函数：新模块里是真身，旧名字转发到同一个对象。

    `_load_default_schedule` 在数据加载完后调 `start_export_warmup()` —— 那个调用点
    仍是**惰性导入**（函数内 import），不是模块级，否则两边模块级互引会成环。
    """
    assert reschedule.start_export_warmup is export.start_export_warmup
    assert reschedule._warm_export_cache is export._warm_export_cache

    with open(os.path.join(_API_DIR, "reschedule.py"), encoding="utf-8") as f:
        src = f.read()
    module_level = [ln for ln in src.splitlines()
                    if ln.startswith(("from app.api import export", "import app.api.export"))]
    assert module_level == [], (
        "reschedule 在模块级 import 了 export：%s\n"
        "export 顶部要 `from app.api import reschedule` 取状态 —— 两侧模块级互引会随"
        "「谁先被导入」分岔成 ImportError（R76 实测过）。改回函数内 import。"
        % module_level)


# ==================== 四、转发集合必须等于新模块的实际内容 ====================

def _module_level_names(path):
    """模块顶层的函数/类/赋值名（不含 import 与 router 本身）。"""
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=path)
    names = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    names.add(t.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    names.discard("router")
    return names


def test_forwarding_set_equals_what_actually_moved():
    """`reschedule._EXPORT_MOVED` 必须**恰好**等于 export.py 顶层定义的那批名字。

    两个方向都钉：
      · 少了 → `from app.api.reschedule import xxx` 会 AttributeError（老调用点炸）；
      · 多了 → 转发一个根本不存在的名字，AttributeError 被推到运行时才发现。
    新模块里加一个函数却忘了更新转发集合时，这条立刻红。
    """
    actual = _module_level_names(os.path.join(_API_DIR, "export.py"))
    declared = set(reschedule._EXPORT_MOVED)

    missing = sorted(actual - declared)
    extra = sorted(declared - actual)
    assert missing == [] and extra == [], (
        "转发集合与实际内容不一致：\n"
        "  漏转（在 export 里但没进 _EXPORT_MOVED）：%s\n"
        "  多转（在 _EXPORT_MOVED 里但 export 没有）：%s" % (missing, extra))


def test_xlsx_helpers_are_forwarded_not_duplicated():
    """R54 的 7 个 XLSX 纯函数：R78 之后 reschedule 已无使用者，改为转发。

    判据仍是**同一个对象**（`is`）—— 两份拷贝会各自漂移。
    """
    from app.api import xlsx_helpers as _xh

    for name in ("_entry_sort_key", "_safe_sheet_name", "_slot_text",
                 "_workbook_bytes", "_write_sheet", "_xlsx_bytes_response",
                 "_xlsx_response"):
        assert getattr(reschedule, name) is getattr(_xh, name), (
            "%s 在 reschedule 上不是 xlsx_helpers 里的同一个对象" % name)


# ==================== 五、重活必须走线程池（R-70） ====================

def test_heavy_export_bodies_are_dispatched_not_inlined():
    """5 个重活端点：async 函数体里必须出现 `run_in_threadpool`。

    行为锁在 `test_export_nonblocking.py`（用心跳比值量），这里只做**结构**兜底：
    语义很直接 —— 建表 ≥65ms 的东西不许直接跑在事件循环上。
    `parallel_export` 不在其中：实测 12–52ms，docstring 里写了为什么不上线程池。
    """
    with open(os.path.join(_API_DIR, "export.py"), encoding="utf-8") as f:
        tree = ast.parse(f.read())
    heavy = {"export_class_schedule", "export_room_schedule", "export_master_data",
             "export_master_workbook", "auto_schedule_export"}

    offenders = []
    seen = set()
    for node in tree.body:
        if not isinstance(node, ast.AsyncFunctionDef) or node.name not in heavy:
            continue
        seen.add(node.name)
        src = ast.dump(node)
        if "run_in_threadpool" not in src:
            offenders.append(node.name)

    assert seen == heavy, "这些端点没在 export.py 里找到：%s" % sorted(heavy - seen)
    assert offenders == [], (
        "这些导出没有把建表交给线程池：%s —— 它们会在事件循环上同步跑完，"
        "期间全站请求一起排队（R-70）" % offenders)
