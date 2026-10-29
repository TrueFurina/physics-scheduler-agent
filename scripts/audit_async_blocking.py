"""审计：哪些 async 路由其实在事件循环上干同步重活。

两把尺子（第一把不够，本轮就是这么漏的）
------------------------------------------
**① 直查（原有判据）**：handler 是 `async def` 且函数体内**没有任何 await**
（含嵌套的 async with/async for），同时有实质工作量 ⇒ 全部工作都在事件循环线程上。

**② 调用传递闭包（2026-10-29 新增，必须）**：handler 里**有** await 也不代表安全。
R-19 修了 5 处 `run_in_threadpool`，看起来"这个问题解决了"；但真正的重活藏在
31 个端点共同调用的 `_ensure_loaded()` 里 —— 它同步解析 433 行 Excel，而它在
handler 里只是一行普通调用。①号判据对这种情况**永远报绿**：

    async def get_teachers():        # 有 await（_ensure_loaded_async）→ ① 判绿
        await _ensure_loaded_async()
        roles = _db.get_teacher_roles()    # ← 402ms 开工作簿，占着事件循环

实测（冷启动，缓存已清）：`/teachers` 让心跳被拖 **407ms**；`/impact-analysis`
在规则解析失败时同步打 LLM（`httpx.Client(timeout=60)`），心跳被拖 **1007ms**。

所以本脚本现在沿**本模块内的调用图**做一层传递闭包：从每个 async 路由出发，
把"它调到的同步函数"继续展开（深度见 `MAX_DEPTH`），只要闭包里出现
`load_workbook` / CP-SAT `solve` / `sqlite execute` / `sleep` 这类调用，
就判为**可疑阻塞**并要求 `run_in_threadpool`。

限制（写清楚，免得被当成万能）
------------------------------
* 只看本模块 `ast` 能解析到的函数，跨模块（`_db.load_from_excel` → excel_parser）只能
  按名字命中 `HEAVY`，不会真正展开 —— 所以它报"干净"不等于绝对干净，
  但报"可疑"一定是可疑。
* 它不测**冷/热**差异：`_ensure_loaded` 热的时候是纳秒级布尔判断，
  冷的时候是 800ms。判据只能是"这段代码**可能**有多慢"，不是"这次有多慢"。

用法：
    python scripts/audit_async_blocking.py [path]
退出码：有零 await 的 async 路由 或 有可疑阻塞调用 时为 1（可进 CI）。
"""

import ast
import json
import sys
from pathlib import Path

args = [a for a in sys.argv[1:] if not a.startswith("--")]
update_baseline = "--update-baseline" in sys.argv[1:]
path = args[0] if args else "backend/app/api/reschedule.py"
tree = ast.parse(Path(path).read_text(encoding="utf-8"))

ROUTE_DECOS = {"get", "post", "put", "delete", "patch"}

# 同步重活的硬信号。**只收"实测 ≥50ms"的入口**，否则这道门会变成噪音。
#
# 为什么刻意不收 `save` / `load` / `read` / `execute` 这类通用名：
# `_persist_master_state` 存的是 16 KB JSON（实测 0.4ms）、`build_from_entries` 反推
# 基线 4.3ms、`sqlite execute` 单条 insert <1ms —— 它们出现在闭包里是**常态**，
# 报出来等于一次报 18 个路由，第一次跑就没人看了。判据沿用项目既有口径
# （R-70 备忘："实测 ≥50ms 才动"）。
#
# 下面每条都有实测依据（2026-10-29，真实表 433 行）：
HEAVY = {
    # Excel 整表解析：冷解析 545–790ms（沙箱冷启动口径 3.8s）
    "load_from_excel", "_load_default_schedule", "_ensure_loaded",
    # 开整个工作簿只为读十几个备注：402ms
    "load_workbook", "get_teacher_roles",
    # LLM（httpx.Client(timeout=60)）：单次 1–7.6s
    "parse_request", "process_request", "suggest_plans", "consult",
    # CP-SAT：单次 1–17s
    "solve", "solve_all", "solve_school",
    # 导出建表：85–830ms
    "build_workbook", "_class_schedule_build", "_room_schedule_build",
    "_master_data_build", "_master_workbook_build", "_auto_schedule_build",
    # 明确不该在事件循环上出现的阻塞原语
    "sleep", "urlopen",
}

# 这些名字出现在**调用链**里时，说明"重活已交给线程池"，下游不再记到 handler 头上。
# 注意：`_ensure_loaded_async` 是 `_ensure_loaded` 的线程池包装 —— 必须显式豁免，
# 否则每个用了自愈的端点都会被误报（本轮修复前后的对照就靠这条区分）。
THREADPOOL_ENTRY = {"run_in_threadpool", "_ensure_loaded_async"}

MAX_DEPTH = 3


def is_route(dec):
    node = dec.func if isinstance(dec, ast.Call) else dec
    return (isinstance(node, ast.Attribute) and node.attr in ROUTE_DECOS
            and isinstance(node.value, ast.Name) and node.value.id == "router")


def route_path(dec):
    if isinstance(dec, ast.Call):
        for a in dec.args:
            if isinstance(a, ast.Constant) and isinstance(a.value, str):
                return a.value
        for kw in dec.keywords:
            if kw.arg == "path" and isinstance(kw.value, ast.Constant):
                return kw.value.value
    return "?"


def has_await(fn):
    for node in ast.walk(fn):
        if isinstance(node, (ast.Await, ast.AsyncWith, ast.AsyncFor)):
            return True
    return False


def calls_in(fn):
    out = []
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Attribute):
                out.append(f.attr)
            elif isinstance(f, ast.Name):
                out.append(f.id)
    return out


def threaded_calls(fn):
    """`run_in_threadpool(_foo, ...)` 的第一个参数 —— 这些调用**不算**阻塞。"""
    names = set()
    for node in ast.walk(fn):
        if (isinstance(node, ast.Call) and "run_in_threadpool" in calls_in(node)
                and node.args):
                a = node.args[0]
                if isinstance(a, ast.Name):
                    names.add(a.id)
                elif isinstance(a, ast.Attribute):
                    names.add(a.attr)
    return names


# 本模块内的同步函数表（用于展开闭包）
sync_fns = {}
for n in ast.walk(tree):
    if isinstance(n, ast.FunctionDef):
        sync_fns.setdefault(n.name, []).append(n)


def blocking_in_closure(fn, depth=0, seen=None):
    """返回 (重活名集合, 展开到的同步函数名集合)。已下沉线程池的调用被跳过。"""
    seen = seen or set()
    if depth > MAX_DEPTH:
        return set(), set()
    thr = threaded_calls(fn)
    heavy = set()
    walked = set()
    for name in calls_in(fn):
        if name in thr:
            continue
        if name in HEAVY:
            heavy.add(name)
        if name in sync_fns and name not in seen:
            seen.add(name)
            walked.add(name)
            sub_heavy, sub_walked = blocking_in_closure(sync_fns[name][0], depth + 1, seen)
            heavy |= sub_heavy
            walked |= sub_walked
    return heavy, walked


rows = []
for fn in [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
    decos = [d for d in fn.decorator_list if is_route(d)]
    if not decos:
        continue
    is_async = isinstance(fn, ast.AsyncFunctionDef)
    linear = sorted(set(calls_in(fn)) & HEAVY)
    deep, walked = blocking_in_closure(fn) if is_async else (set(), set())
    # 闭包里命中的、且**不在**本函数直接调用清单里的，才算"藏在辅助函数里"
    hidden = sorted(deep - set(linear))
    rows.append((fn.lineno, route_path(decos[0]), fn.name, is_async, has_await(fn),
                 linear, hidden, sorted(walked)))

rows.sort()
print(f"{'line':>5s}  {'path':34s} {'handler':30s} {'async':5s} {'await':5s}  heavy / 闭包内隐藏")
print("-" * 132)
zero_await, suspicious = [], []
for lineno, p, name, is_async, aw, linear, hidden, walked in rows:
    flag = ""
    if is_async and not aw:
        flag = "  ← 零 await（纯阻塞）"
        zero_await.append((lineno, p, name, linear))
    elif is_async and (linear or hidden):
        # ⚠️ 2026-10-29 修：原来只判 `hidden`（闭包内隐藏），于是**直接调用重活**反而漏判。
        # 实测：把 `/teachers` 的 `roles = await run_in_threadpool(_db.get_teacher_roles)`
        # 改回同步调用（402ms 开工作簿，正是 R-84 修掉的那处），脚本 rc=0 —— 完全没报。
        # 而 R-81 修的 `_ensure_loaded()`（31 个端点直接调它）同样属于"直接调用"。
        # 「重活藏在helper里」和「重活就摆在端点里」对事件循环是一样的伤害，不能只抓前者。
        culprit = ",".join(sorted(set(linear) | set(hidden)))
        flag = f"  ← 可疑：重活在事件循环上（{culprit}）"
        suspicious.append((lineno, p, name, sorted(set(linear) | set(hidden)), walked))
    shown = ",".join(linear) or "-"
    if hidden:
        shown += f"  |隐藏: {','.join(hidden)}"
    print(f"{lineno:5d}  {p[:34]:34s} {name[:30]:30s} {is_async!s:5s} {aw!s:5s}  {shown}{flag}")

print()
print(f"共 {len(rows)} 个路由；零 await 的 async 路由 {len(zero_await)} 个；"
      f"重活藏在辅助函数里的 async 路由 {len(suspicious)} 个：")
for lineno, p, name, heavy in zero_await:
    print(f"  L{lineno:<5d} {p:34s} {name:26s} 重调用: {','.join(heavy) or '（轻）'}")
for lineno, p, name, hidden, walked in suspicious:
    print(f"  L{lineno:<5d} {p:34s} {name:26s} 隐藏: {','.join(hidden)}")
    print(f"         展开路径: {' → '.join(walked)}")

# ---------------------------------------------------------------------------
# 退出码：区分"必须为 0"和"允许存在但没有恶化"
#
# 为什么不再"有任何一条就退出 1"（2026-10-29）：
# 这个脚本原来 `sys.exit(1 if (zero_await or suspicious) else 0)`，而实测有 13 个
# **零 await 的轻查询**（`/status`、`/admin-login`、`/requests` CRUD…，各 1–5ms）。
# 按项目既有判据（R-70 备忘："实测 ≥50ms 才动"）这些**本来就不该改** ——
# 于是这条门在写出来的那一刻就是红的，永不可能绿。而"永远红的门"等于没有门
# （R-25 的反面：会叫的哨子才有人听）。
#
# 现在的口径：
#   1. `suspicious`（重活在辅助函数里、跑在事件循环上）**必须为 0** —— 这是真缺陷，
#      R-81/R-84 各抓到一处（`_ensure_loaded`、`get_teacher_roles`、`parse_request`）。
#   2. `zero_await` 的轻查询**允许存在，但不许增加**：与基线快照比对，只报"新增的"。
#      基线文件 `scripts/async_blocking_baseline.json`，更新它需要显式 `--update-baseline`
#      （写在提交里，评审能看见"这次新增了一个零 await 端点"）。
# ---------------------------------------------------------------------------
BASELINE = Path(__file__).resolve().parent / "async_blocking_baseline.json"


def _label(lineno, path, name):
    # 用"路由路径 + 函数名"而不是行号做键：行号会随任何改动漂移，键会失效
    return f"{path}  {name}"


def load_baseline():
    if not BASELINE.exists():
        return None
    try:
        return json.loads(BASELINE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def save_baseline(names):
    BASELINE.write_text(
        json.dumps({"note": "零 await 的 async 路由基线（只允许减少，新增需显式更新）",
                    "routes": sorted(names)}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


current = {f"{r[1]}  {r[2]}" for r in zero_await}
failed = False

# 已登记的豁免：**确实会阻塞**，但经判定不改，且理由可复核。
# 与"基线"不同：基线的意思"这类本来就该允许"；豁免的意思是"这是真问题，我们决定先不改"。
# 每条必须带实测数字，否则下一个人无法判断要不要重开。
KNOWN_BLOCKING = {
    # 2026-10-29 起这里是空的：唯一的条目 `/upload` 已经下沉线程池（R-89）。
    # 它当初的豁免理由是"下沉会让并发读看见空表"，而那个窗口在 R-87（构建到暂存器、
    # 一次提交）修掉了；R-89 又给整表加载加了 `_LOAD_LOCK`。
    #
    # 保留这个空字典而不是删掉整段逻辑：下一个"确实会阻塞但决定先不改"的端点
    # 需要同一套机制 —— 豁免必须带**实测数字**，让后来者能判断要不要重开。
}

if suspicious:
    unknown = [x for x in suspicious if f"{x[1]}  {x[2]}" not in KNOWN_BLOCKING]
    for x in suspicious:
        key = f"{x[1]}  {x[2]}"
        if key in KNOWN_BLOCKING:
            print()
            print(f"ℹ️ 已登记豁免（{key}）：{KNOWN_BLOCKING[key]}")
    if unknown:
        print()
        print(f"❌ 有 {len(unknown)} 个 async 路由把重活放在事件循环上（必须为 0）：")
        for x in unknown:
            print(f"   - {x[1]}  {x[2]}  重活: {','.join(x[3])}")
        failed = True
    else:
        print()
        print(f"✅ 无未登记的阻塞（{len(suspicious)} 个已登记豁免）")

baseline = load_baseline()
if update_baseline:
    save_baseline(current)
    print()
    print(f"📝 已更新基线：{len(current)} 个零 await 轻查询 → {BASELINE.name}")
    print("   （把这个文件一起提交，让评审看见基线变化）")
elif baseline is None:
    print()
    print(f"⚠️ 没有基线文件 {BASELINE.name}；用 --update-baseline 生成一次。")
    print(f"   当前零 await 轻查询 {len(current)} 个（不判红，因为无从比较）")
else:
    base = set(baseline.get("routes") or [])
    added = sorted(current - base)
    removed = sorted(base - current)
    if added:
        print()
        print(f"❌ 新增了 {len(added)} 个零 await 的 async 路由"
              f"（若确实是轻查询，用 --update-baseline 更新并提交）：")
        for a in added:
            print(f"   + {a}")
        failed = True
    if removed:
        print()
        print(f"✅ 有 {len(removed)} 个零 await 路由被消除（基线可变好）：")
        for r in removed:
            print(f"   - {r}")
    if not added:
        print()
        print(f"✅ 零 await 轻查询与基线一致（{len(current)} 个，不允许增加）")

sys.exit(1 if failed else 0)
