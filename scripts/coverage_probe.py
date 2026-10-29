"""行覆盖率探针（只用标准库，不依赖 pytest-cov / coverage）。

为什么自己写
------------
项目铁律是"守卫必须敢红"（R-25），但没有覆盖率图时，"这段代码从没被执行过"
只能靠人想到去点 —— R-70 就是这样漏掉"主路径第 ③ 步零覆盖"的。
本轮想装 `pytest-cov`，实测 PyPI 镜像不可达（`No matching distribution found`），
而 Python 3.14 自带 `sys.monitoring`（PEP 669），足够做这件事。

**判据必须 fail-closed（这条是设计核心）**
------------------------------------------
最省事的做法是"只统计跑到的 code object"，但那会把**从未被 import 的模块**
整个漏掉 —— 覆盖率凭空变高，正是 R-25 那种"把没测伪装成测过"。
所以这里反过来：**从源码 AST 枚举所有可执行行**当作分母（不需要模块被导入），
分子才是监控到的命中行。没被导入的文件自然就是 0%。

可执行行的口径
--------------
AST 里每个 `stmt` 的 `lineno` 记为一行；跳过：
  * 模块 / 类 / 函数的**文档字符串**（`Expr(Constant(str))` 且是 body 第 0 条）；
  * `if TYPE_CHECKING:` 块（运行时永不执行，算进去必然永久红）；
  * 纯 `pass` / `...`；
  * 装饰器与 `def` 行只算一次（`def` 的 lineno 即该行）。
多行表达式只记起始行（与 coverage.py 的"语句级"口径接近，不追求逐列一致）。

用法
----
    # 跑全量测试并出报告（约 3–4 分钟）
    python scripts/coverage_probe.py

    # 只跑指定测试文件（快速迭代）
    python scripts/coverage_probe.py -- backend/tests/test_apply_move.py

    # 门槛：低于阈值的模块退出码非 0（可进 CI）
    python scripts/coverage_probe.py --fail-under 85 --fail-zero

退出码
------
0 = 达标；1 = 有模块低于 `--fail-under` 或存在零覆盖文件（且传了对应开关）。
"""

from __future__ import annotations

import argparse
import ast
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BACKEND = REPO / "backend"

# 分母范围：只算产品代码。测试自己覆盖自己毫无意义，scripts 是一次性工具。
TARGET_DIRS = [BACKEND / "app"]

# 这些文件/目录是"有意不测"或"测试不可达"，单列出来免得它们永远拉低均分
SKIP_PARTS = {"__pycache__"}


def _is_docstring(node: ast.stmt) -> bool:
    return (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str))


def _is_type_checking(node: ast.stmt) -> bool:
    """`if TYPE_CHECKING:` 块 —— 运行时不执行，算进分母会永久红。"""
    if not isinstance(node, ast.If):
        return False
    t = node.test
    return (isinstance(t, ast.Name) and t.id == "TYPE_CHECKING") or (
        isinstance(t, ast.Attribute) and t.attr == "TYPE_CHECKING")


def _is_noop(node: ast.stmt) -> bool:
    if isinstance(node, ast.Pass):
        return True
    return (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
            and node.value.value in (Ellipsis, None))


def _key(path: str | Path) -> str:
    """命中集与分母集共用的文件名键：绝对路径 + 文件系统真实大小写。

    ⚠️ 这个函数存在的唯一理由是一个真实 bug（2026-10-29）：`hit` 用
    `os.path.abspath(code.co_filename)`、`sources` 用 `str(p.resolve())`，
    在 Windows 上两者对同一文件可能给出**不同大小写**（相对路径 vs 绝对路径、
    盘符大小写不同），于是 `hit.get(path)` 取不到、**整份文件被算成 0% 已覆盖**，
    `--show-missing` 还会把文档字符串的行号列成"未覆盖"。
    症状极具误导性：数字看起来像"这块代码没人测"，实际是键没对上。
    """
    ap = os.path.abspath(str(path))
    try:
        ap = os.path.realpath(ap)
    except OSError:
        pass
    # Windows 文件系统大小写不敏感（NTFS 默认），但 `realpath` **不会**折叠大小写
    # ⇒ 同一文件写成 `Backend\...` 与 `backend\...` 会得到两个键，
    # 命中集查不到、整份文件被算成 0% 已覆盖。这里按平台折叠（POSIX 上保持原样，
    # 那边大小写敏感，折叠会把两个不同文件误并）。
    return os.path.normcase(ap) if os.name == "nt" else ap


def executable_lines(path: Path) -> set[int]:
    """源码里"应该被执行到"的行号集合（fail-closed 的分母）。"""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError):
        return set()

    lines: set[int] = set()

    class V(ast.NodeVisitor):
        def _body(self, body: list[ast.stmt]):
            for i, node in enumerate(body):
                if i == 0 and _is_docstring(node):
                    continue           # 模块/类/函数的文档字符串
                if _is_type_checking(node) or _is_noop(node):
                    continue
                self.visit(node)

        def visit_FunctionDef(self, node):
            lines.add(node.lineno)     # def 这一行本身（含装饰器已在 lineno 之上）
            self._body(node.body)
            # 不 visit decorator_list：装饰器在定义期执行，行号会与 def 重叠

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_ClassDef(self, node):
            lines.add(node.lineno)
            self._body(node.body)

        def visit_If(self, node):
            lines.add(node.lineno)
            self._body(node.body)
            self._body(node.orelse)

        def visit_For(self, node):
            lines.add(node.lineno)
            self._body(node.body)
            self._body(node.orelse)

        visit_AsyncFor = visit_For

        def visit_While(self, node):
            lines.add(node.lineno)
            self._body(node.body)
            self._body(node.orelse)

        def visit_With(self, node):
            lines.add(node.lineno)
            self._body(node.body)

        visit_AsyncWith = visit_With

        def visit_Try(self, node):
            lines.add(node.lineno)
            self._body(node.body)
            for h in node.handlers:
                lines.add(h.lineno)
                self._body(h.body)
            self._body(node.orelse)
            self._body(node.finalbody)

        def visit_Match(self, node):
            lines.add(node.lineno)
            for c in node.cases:
                self._body(c.body)

        def generic_visit(self, node):
            if isinstance(node, ast.stmt):
                lines.add(node.lineno)
            super().generic_visit(node)

    # ⚠️ 必须走 `_body(tree.body)` 而不是 `visit(tree)`：后者直接落到
    # `generic_visit`，绕过"跳过文档字符串"的那一步 —— 实测模块 docstring
    # 第一行因此被当成可执行行（`--show-missing` 里会显示"第 1 行未覆盖"）。
    V()._body(tree.body)
    return lines


class LineMonitor:
    """`sys.monitoring` 的行命中记录器。

    `(code, line)` 首次命中后返回 `DISABLE` —— 这个位置以后不再回调，
    所以开销随"已覆盖行数"收敛，跑全量测试只慢几秒。
    """

    TOOL_ID = 3     # COVERAGE_ID 是保留给覆盖率工具的槽位
    TOOL_NAME = "mini-cov"

    def __init__(self, roots: list[Path]):
        self.roots = [os.path.normcase(str(r.resolve())) for r in roots]
        self.hit: dict[str, set[int]] = {}
        self.seen: set[tuple] = set()
        self.active = False

    def _interesting(self, filename: str) -> bool:
        if not filename or filename.startswith("<"):
            return False
        nf = os.path.normcase(os.path.abspath(filename))
        return any(nf.startswith(r) for r in self.roots)

    def _on_line(self, code, line):
        if not self._interesting(code.co_filename):
            return self.mon.DISABLE
        key = (code, line)
        if key in self.seen:
            return self.mon.DISABLE
        self.seen.add(key)
        self.hit.setdefault(_key(code.co_filename), set()).add(line)
        return self.mon.DISABLE

    def start(self):
        if not hasattr(sys, "monitoring"):
            raise SystemExit(
                "本探针依赖 sys.monitoring（PEP 669，Python 3.12+），"
                f"当前 {sys.version.split()[0]}。\n"
                "CI 用 setup-python 3.12；本地若用旧解释器，请换 3.12+ 跑覆盖率（测试本身不受影响）。"
            )
        self.mon = sys.monitoring
        self.mon.use_tool_id(self.TOOL_ID, self.TOOL_NAME)
        self.mon.register_callback(self.TOOL_ID, self.mon.events.LINE, self._on_line)
        self.mon.set_events(self.TOOL_ID, self.mon.events.LINE)
        self.active = True

    def stop(self):
        if not self.active:
            return
        self.mon.set_events(self.TOOL_ID, 0)
        self.mon.register_callback(self.TOOL_ID, self.mon.events.LINE, None)
        self.mon.free_tool_id(self.TOOL_ID)
        self.active = False


def collect_sources() -> dict[str, set[int]]:
    out: dict[str, set[int]] = {}
    for d in TARGET_DIRS:
        for p in sorted(d.rglob("*.py")):
            if any(part in SKIP_PARTS for part in p.parts):
                continue
            lines = executable_lines(p)
            if lines:
                out[_key(p)] = lines
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="标准库行覆盖率（无第三方依赖）")
    ap.add_argument("--fail-under", type=float, default=0.0,
                    help="模块覆盖率低于该值即失败（0 = 不设门槛）")
    ap.add_argument("--fail-zero", action="store_true",
                    help="存在从未执行任何一行的文件即失败")
    ap.add_argument("--top", type=int, default=15, help="列出最差的 N 个文件")
    ap.add_argument("--show-missing", action="store_true", help="打印未覆盖的行号")
    ap.add_argument("pytest_args", nargs="*", help="传给 pytest 的参数（放在 -- 之后）")
    args = ap.parse_args()

    sources = collect_sources()
    mon = LineMonitor(TARGET_DIRS)
    mon.start()
    try:
        import pytest
        rc = pytest.main(list(args.pytest_args) + ["-q", "--no-header"])
    finally:
        mon.stop()

    # —— 报告 ——
    total_lines = total_hit = 0
    rows = []
    for path, lines in sources.items():
        hit = mon.hit.get(path, set()) & lines
        total_lines += len(lines)
        total_hit += len(hit)
        rows.append((100 * len(hit) / len(lines) if lines else 0.0, path, len(hit), len(lines)))

    rows.sort()
    pct = 100 * total_hit / total_lines if total_lines else 0.0
    print()
    print("=" * 96)
    print(f"产品代码行覆盖率：{total_hit}/{total_lines} = {pct:.1f}%"
          f"（{len(sources)} 个文件，分母＝AST 枚举的可执行语句行）")
    print("=" * 96)
    print(f"{'pct':>6s}  {'hit/lines':>12s}  文件")
    print("-" * 96)
    for p, path, h, n in rows[:args.top]:
        rel = os.path.relpath(path, REPO)
        print(f"{p:5.1f}%  {h:5d}/{n:<6d}  {rel}")
    if len(rows) > args.top:
        print(f"  … 另有 {len(rows)-args.top} 个文件更好的文件未列出")

    # 未被命中的行（可选：--show-missing 打印行号，用于精确补测）
    if args.show_missing:
        print()
        print("未覆盖行号（按文件，最多 40 行/文件）：")
        for _pct, path, _h, _n in rows:
            miss = sorted(sources[path] - mon.hit.get(path, set()))
            if not miss:
                continue
            shown = ", ".join(str(x) for x in miss[:40])
            more = f" …共 {len(miss)} 行" if len(miss) > 40 else ""
            print(f"  {os.path.relpath(path, REPO)}: {shown}{more}")

    zero = [(path, n) for _pct, path, h, n in rows if h == 0]
    if zero:
        print()
        print(f"⚠️ {len(zero)} 个文件**从未执行过任何一行**（分母非零、分子为 0）：")
        for path, n in sorted(zero, key=lambda x: -x[1]):
            print(f"   - {os.path.relpath(path, REPO)}（{n} 行）")

    failed = False
    if args.fail_zero and zero:
        print(f"\n❌ --fail-zero：存在 {len(zero)} 个零覆盖文件")
        failed = True
    if args.fail_under and pct < args.fail_under:
        print(f"\n❌ --fail-under {args.fail_under}：实测 {pct:.1f}%")
        failed = True

    if rc != 0:
        print(f"\n⚠️ pytest 退出码 {rc}（有测试失败）—— 覆盖率数字仅供参考")
        return rc
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
