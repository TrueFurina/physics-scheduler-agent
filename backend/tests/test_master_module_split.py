"""R77：主数据维护端点族搬到 `app/api/master.py` 之后的结构守卫。

为什么这一族要单独守
--------------------
搬家的危险不是"代码跑不起来"，而是**守卫静默缩小覆盖面**：R77 动手时，三条
已有守卫都只读 `app/api/reschedule.py` ——

1. `test_api_auth.py::test_all_upload_endpoints_use_limited_reader`（受限读取）
   → 实际红了一次（5 个上传端点搬走 3 个，计数掉到 2）。红得对，但根因是判据
     写死了文件，不是端点出错。现已改为按目录发现 + 逐端点判定。
2. `test_apply_match.py::test_every_data_reading_route_self_heals`（自愈）
   → 不红。它读的还是 reschedule.py，那 15 个端点**不再接受"必须自愈"的检查**，
     而用例仍然绿。现已改为扫所有带 `APIRouter` 的模块。
3. `scripts/audit_async_blocking.py`（事件循环阻塞门，进 CI）
   → 不红。R76 的修法是"CI 里多写一行 run"，而 R77 又新增了一个模块 ——
     这正是"要记得改 CI"会失效的时刻。现已改为默认扫整个 api 目录。

本文件把这三件事的**结果**钉住：路由归属、状态现取、以及"门到底看没看见新模块"。
"""

import ast
import os
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import app.api.master as master
import app.api.reschedule as reschedule
from app.main import app

_BACKEND = os.path.join(os.path.dirname(__file__), "..")
_REPO_ROOT = os.path.join(_BACKEND, "..")
_API_DIR = os.path.join(_BACKEND, "app", "api")

# (方法, 不带前缀的路径, 端点函数名) —— 搬迁前在 reschedule.py 里的那 15 条。
# 新增/删除端点时这张表要一起改：它是"搬了什么"的清单，不是"随便列几条"。
_MASTER_ROUTES = [
    ("GET", "/master/overview", "master_overview"),
    ("POST", "/master/init", "master_init"),
    ("POST", "/master/courses", "master_upsert_course"),
    ("DELETE", "/master/courses/{name}", "master_delete_course"),
    ("POST", "/master/experiments", "master_upsert_experiment"),
    ("DELETE", "/master/experiments/{name}", "master_delete_experiment"),
    ("POST", "/master/teachers", "master_upsert_teacher"),
    ("DELETE", "/master/teachers/{name}", "master_delete_teacher"),
    ("POST", "/master/rooms", "master_upsert_room"),
    ("DELETE", "/master/rooms/{name}", "master_delete_room"),
    ("POST", "/master/classes", "master_upsert_class"),
    ("DELETE", "/master/classes/{name}", "master_delete_class"),
    ("POST", "/master/import-preview", "master_import_preview"),
    ("POST", "/master/tasks-preview", "master_tasks_preview"),
    ("POST", "/master/import", "master_import"),
]
_PREFIX = "/api/reschedule"

# 只有这 3 条可以免管理员：总览是纯实体清单（不含学生 PII），两个 preview 只读字节。
_PUBLIC_MASTER_PATHS = {"/master/overview", "/master/import-preview", "/master/tasks-preview"}


# ==================== 一、路由属于新模块，且没有第二份 ====================

def test_master_routes_are_served_by_the_new_module():
    """15 条主数据路由必须由 `app.api.master` 提供；路径与方法一个都不许变。

    既看 `master.router` 自己的表（防"没挂上"），也看 `app.openapi()` 的最终结果
    （防"挂了两份"——同一路径注册两次时 FastAPI 只用先注册的那条，后一条静默
    进不了路由表，表现为"改了新代码没生效"）。
    """
    have = {}
    for r in master.router.routes:
        have.setdefault(r.path, []).append(r)

    for method, path, fn_name in _MASTER_ROUTES:
        routes = have.get(path) or []
        assert routes, "master.router 里没有 %s" % path
        assert len(routes) == 1, "%s 在 master.router 里注册了 %d 次" % (path, len(routes))
        route = routes[0]
        assert method in route.methods, "%s 的方法变成了 %s" % (path, sorted(route.methods))
        assert route.endpoint.__module__ == "app.api.master", (
            "%s 的端点来自 %s —— 搬迁没做完，或者被搬回去了"
            % (path, route.endpoint.__module__))
        assert route.endpoint.__name__ == fn_name, (
            "%s 的端点函数名变了：%s（预期 %s）"
            % (path, route.endpoint.__name__, fn_name))

    assert len(master.router.routes) == len(_MASTER_ROUTES), (
        "master.router 的路由数不是 %d：%d"
        % (len(_MASTER_ROUTES), len(master.router.routes)))


def test_reschedule_router_no_longer_serves_master_paths():
    """/master/** 不能还挂在 `reschedule.router` 上 —— 那就是第二份实现。

    两个例外，**故意**留在 reschedule：`/master/tasks`（清空教学任务清单）与
    `/master-status`（清单是否已导入）。它们跟 `/upload-master` 是一条链
    （换清单 → 清清单 → 看状态），操作的是"本次导入的任务清单"这份内存态，
    不是五张实体表 —— 与主数据 CRUD 是两件事。这里把"允许留下的"写成清单，
    所以**多留一条**（真的漏搬）仍然会红。
    """
    _STAYS = {"/master/tasks", "/master-status"}
    leftover = [r.path for r in reschedule.router.routes if r.path.startswith("/master")]
    assert set(leftover) == _STAYS, (
        "reschedule.router 上的 /master* 路径变成了 %s（预期只有 %s）。\n"
        "多出来的说明搬迁漏了端点（会有两份实现）；少了的说明那两个也被搬走了，"
        "那条链（/upload-master → /master-status → /master/tasks）就断了。"
        % (sorted(leftover), sorted(_STAYS)))


def test_openapi_still_exposes_the_same_master_paths():
    """对前端与文档而言必须一模一样：完整路径 + 方法。"""
    spec = app.openapi()["paths"]
    for method, path, _fn in _MASTER_ROUTES:
        full = _PREFIX + path
        assert full in spec, "openapi 里没有 %s —— 前端会 404" % full
        assert method.lower() in spec[full], (
            "%s 的 %s 方法不见了：%s" % (full, method, sorted(spec[full])))


def test_reschedule_keeps_no_second_copy_of_the_handlers():
    """`reschedule` 里不能留同名函数/模型 —— 留着就是两份实现。

    `vars()` 看的是模块字典：`__getattr__` 转发不写进字典，所以这条能区分
    "真定义了一份"和"只是能取到"。
    """
    names = [fn for _m, _p, fn in _MASTER_ROUTES] + [
        "_CourseIn", "_ExperimentIn", "_TeacherIn", "_RoomIn", "_ClassIn"]
    still_defined = [n for n in names if n in vars(reschedule)]
    assert still_defined == [], (
        "reschedule 里仍有这些定义：%s —— 搬迁留下了第二份实现" % still_defined)


def test_old_names_are_forwarded_lazily():
    """旧名必须照旧可用，且指向同一个对象（测试里 `api.master_init` 这类写法很多）。"""
    assert reschedule.master_init is master.master_init
    assert reschedule._CourseIn is master._CourseIn
    # `from module import name` 走同一条 `__getattr__`，不是只有属性访问好用
    from app.api.reschedule import master_import
    assert master_import is master.master_import


# ==================== 二、状态现取，锁是同一把 ====================

def _import_from_reschedule(tree):
    """模块里所有"从 reschedule 名字级导入"的语句（行号 + 原文）。"""
    bad = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        mod = node.module or ""
        if mod == "app.api.reschedule" or mod.endswith(".reschedule"):
            names = ", ".join(a.name for a in node.names)
            bad.append("line %d: from %s import %s" % (node.lineno, mod, names))
    return bad


def test_every_api_module_reads_reschedule_state_through_the_module_object():
    """所有 api 模块都不得 `from app.api.reschedule import <可变状态>`。

    R76 只对 `landing.py` 定了这条，R77 把它推广到整个 `app/api/`：搬迁最自然的
    写法（名字级导入）恰恰是最坏的写法 —— 拿到的是**导入那一刻**的对象，之后
    `monkeypatch.setattr(reschedule, "_master_data", ...)` 全部失明，而且是静默失明
    （测试绿、跑的是真数据）。只允许 `from app.api import reschedule as _api` +
    `_api._master_data` 这种每次现取的写法。

    用 AST 而不是逐行匹配：文档字符串里就写着这个反例，逐行扫会把解释文字当违规。
    """
    offenders = []
    for name in sorted(os.listdir(_API_DIR)):
        if not name.endswith(".py"):
            continue
        path = os.path.join(_API_DIR, name)
        with open(path, encoding="utf-8") as f:
            tree = ast.parse(f.read(), filename=path)
        offenders += ["%s %s" % (name, b) for b in _import_from_reschedule(tree)]

    assert offenders == [], (
        "这些模块从 reschedule 做了名字级导入（之后 monkeypatch 全部失明）：\n  - "
        + "\n  - ".join(offenders)
        + "\n应改为模块属性访问（`from app.api import reschedule as _api` + `_api._db`）")


def test_master_write_still_goes_through_the_shared_lock_decorator():
    """12 个写端点必须仍挂 `_api._master_write`，且模块自己没造第二把锁。

    `_master_lock` 是**同一把** RLock：导出族（R-70 起跑在工作线程）读主数据时
    在锁内取快照，写在锁外就会插进"遍历 experiments"的几微秒里，抛
    `RuntimeError: dictionary changed size during iteration`。搬模块时若顺手
    `threading.RLock()` 造一把新的，两边互不阻塞 —— 锁等于没有，且用例照样绿。
    """
    with open(os.path.join(_API_DIR, "master.py"), encoding="utf-8") as f:
        src = f.read()
    tree = ast.parse(src, filename="master.py")

    # 1) 不许自己造锁（threading 都不该 import 进来）
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert "threading" not in imported, (
        "master.py 引入了 threading —— 它只该用 reschedule 的那把 `_master_lock`，"
        "另造一把锁 = 两把锁 = 锁等于没锁")

    # 2) 每个写端点都必须带 `_api._master_write`
    decos = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            decos[node.name] = [ast.unparse(d) for d in node.decorator_list]

    missing = [fn for _m, path, fn in _MASTER_ROUTES
               if path not in _PUBLIC_MASTER_PATHS
               and not any("_master_write" in d for d in decos.get(fn, []))]
    assert missing == [], (
        "这些写端点没挂 `_master_write`（写主数据时不持锁，导出可能读到半张表）：%s"
        % missing)

    # 3) 也只许有一把：装饰器必须来自 `_api.`（reschedule 的），不是本地 def
    assert "def _master_write" not in src, (
        "master.py 里自己定义了一份 `_master_write` —— 两份装饰器背后可能是两把锁")


def test_master_state_is_the_same_object_as_reschedule():
    """模块属性必须真的是 reschedule 的那几个对象（不是 import 时复制的快照）。"""
    assert master._api is reschedule, "master 引用的不是 reschedule 模块本体"
    # 同一次进程里两边读到的是同一个 dict —— 这是"现取"的直接证据
    reschedule._master_data.initialized = reschedule._master_data.initialized
    assert master._api._master_data is reschedule._master_data


# ==================== 三、门到底看没看见新模块 ====================

def test_audit_gate_scans_every_router_module_by_default():
    """事件循环阻塞门必须**默认扫整个 api 目录**，并且真的把 master.py 看进去了。

    这条是本轮最想钉住的一条：R76 的修法是"CI 里多写一行 run"，R77 又搬出一个
    模块 —— 靠人记得加行的门，在搬家的那一刻正好会瞎。所以这里不看注释、不看
    CI 配置，而是**真的跑一次脚本**，检查它的输出里有没有新模块。
    """
    script = os.path.join(_REPO_ROOT, "scripts", "audit_async_blocking.py")
    assert os.path.isfile(script), "审计脚本不见了：%s" % script
    proc = subprocess.run(
        [sys.executable, script],
        cwd=_REPO_ROOT, capture_output=True, text=True, timeout=120,
    )
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, (
        "审计门自己红了（rc=%d）：\n%s" % (proc.returncode, out[-2000:]))
    assert "master.py" in out, (
        "审计门的输出里没有 master.py —— 它没看见新模块：\n%s" % out[:1500])
    assert "landing.py" in out, "审计门漏了 landing.py：\n%s" % out[:1500]
    assert "reschedule.py" in out, "审计门漏了 reschedule.py：\n%s" % out[:1500]


def test_every_router_module_is_named_in_the_audit_output():
    """反向检查：目录里有几个带 `APIRouter` 的模块，输出里就得有几个文件名。

    这条防的是"扫描范围又写死回某个文件"。模块数由目录自己数出来，不写死。
    """
    router_modules = set()
    for name in sorted(os.listdir(_API_DIR)):
        if not name.endswith(".py"):
            continue
        with open(os.path.join(_API_DIR, name), encoding="utf-8") as f:
            if "APIRouter(" in f.read():
                router_modules.add(name)

    assert len(router_modules) >= 3, (
        "只发现 %d 个带路由的 api 模块（当前是 reschedule/landing/master）：%s"
        % (len(router_modules), sorted(router_modules)))

    script = os.path.join(_REPO_ROOT, "scripts", "audit_async_blocking.py")
    proc = subprocess.run(
        [sys.executable, script], cwd=_REPO_ROOT, capture_output=True, text=True, timeout=120,
    )
    out = proc.stdout
    missing = sorted(m for m in router_modules if m not in out)
    assert missing == [], (
        "审计门没扫这些带路由的模块：%s\n输出头部：\n%s"
        % (missing, out[:400]))


# ==================== 四、变异清单的锚点也没被搬空 ====================

def test_every_mutation_anchor_still_matches_its_file():
    """56 条变异体的锚点必须**在它声明的文件里恰好出现一次**。

    为什么要在 pytest 里查而不是只靠变异跑批：`mutation_check.py` 是按
    (文件, 原文片段) 定位的，代码一搬家锚点就失配，而失配的表现是
    **"跳过"**（skipped）—— 它不算通过，但只有真跑一遍 56 条的变异才会发现。
    本轮实测：搬完主数据后 M38 / M47 / M54 三条锚点留在旧文件里，其中 M54 的
    原文片段连内容都变了（`len(_master_tasks)` → `len(_api._master_tasks)`），
    三条防护等于暂时没人守。

    这条用例把"锚点失配"提前到每次 pytest：0.02 秒，56 次字符串计数。
    """
    mc = os.path.join(_BACKEND, "tests", "mutation_check.py")
    with open(mc, encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=mc)

    mutants = None
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                getattr(t, "id", "") == "MUTANTS" for t in node.targets):
            mutants = ast.literal_eval(node.value)
            break
    assert mutants, "mutation_check.py 里找不到 MUTANTS 列表（结构变了，这条守卫要跟着改）"

    problems = []
    for item in mutants:
        tag, title, rel, old, _new, _expect = item[:6]
        path = os.path.join(_BACKEND, rel.replace("/", os.sep))
        if not os.path.isfile(path):
            problems.append("%s %s：文件不存在 %s" % (tag, title, rel))
            continue
        with open(path, encoding="utf-8", newline="") as f:
            text = f.read().replace("\r\n", "\n")
        hits = text.count(old)
        if hits != 1:
            problems.append("%s %s：在 %s 里命中 %d 次（应为 1）  %r"
                            % (tag, title, rel, hits, old[:60]))

    assert problems == [], (
        "这些变异体的锚点失配 —— 跑批时它们只会「跳过」，对应防护暂时没人守：\n  - "
        + "\n  - ".join(problems))
