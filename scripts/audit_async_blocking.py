"""审计：哪些 async 路由其实是"同步函数披了 async 皮"。

判据（硬信号，不靠猜）：
  1. handler 是 async def；
  2. 函数体内**没有任何 await**（含嵌套的 async with/async for）；
  3. 函数体内有实质工作量（有函数调用），不是纯 return 常量。
满足 1+2 的 async 函数，其全部工作都在事件循环线程上同步执行 —— 并发下必然阻塞全站。
"""
import ast
import sys
from pathlib import Path

path = sys.argv[1] if len(sys.argv) > 1 else "backend/app/api/reschedule.py"
tree = ast.parse(Path(path).read_text(encoding="utf-8"))

ROUTE_DECOS = {"get", "post", "put", "delete", "patch"}


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


HEAVY = {"save", "load_workbook", "build_workbook", "solve", "solve_all", "solve_school",
         "read", "write", "dump", "load", "append", "sleep", "urlopen", "execute"}

rows = []
for fn in [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
    decos = [d for d in fn.decorator_list if is_route(d)]
    if not decos:
        continue
    rows.append((fn.lineno, route_path(decos[0]), fn.name,
                 isinstance(fn, ast.AsyncFunctionDef), has_await(fn),
                 sorted(set(calls_in(fn)) & HEAVY)))

rows.sort()
print(f"{'line':>5s}  {'path':34s} {'handler':30s} {'async':5s} {'await':5s}  heavy")
print("-" * 118)
bad = []
for lineno, p, name, is_async, aw, heavy in rows:
    flag = ""
    if is_async and not aw:
        flag = "  ← 零 await（纯阻塞）"
        bad.append((lineno, p, name, heavy))
    print(f"{lineno:5d}  {p[:34]:34s} {name[:30]:30s} {is_async!s:5s} {aw!s:5s}  "
          f"{','.join(heavy) or '-'}{flag}")

print()
print(f"共 {len(rows)} 个路由；零 await 的 async 路由 {len(bad)} 个：")
for lineno, p, name, heavy in bad:
    print(f"  L{lineno:<5d} {p:34s} {name:26s} 重调用: {','.join(heavy) or '（轻）'}")
