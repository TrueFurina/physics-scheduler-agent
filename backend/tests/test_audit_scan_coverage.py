"""R-112：异步阻塞审计的**扫描完整性**契约。

为什么需要这个文件
------------------
`scripts/audit_async_blocking.py` 是 CI 里的一道硬门（`ci.yml` 的
`Event-loop blocking audit` 步骤），它负责找出"async 路由把重活放在事件循环上"。
R-81/R-84 各抓到过一处真实缺陷（`_ensure_loaded` 整表解析 545–790ms、
`get_teacher_roles` 开工作簿 402ms），所以这道门本身是有效的。

但它有一个**结构性前提**此前没人验证：

    `path = args[0] if args else "backend/app/api/reschedule.py"`

它只吃**单个文件**。而整个后端 53 个端点全在这一个 3382 行的文件里
（`backend/app/api/` 目录下只有它一个 .py）。这两件事凑在一起时是自洽的；
一旦有人把文件拆成多个模块——那是迟早的事，3382 行已经排到第二大文件的 3 倍——
审计只会扫到其中一份，**其余几份的重活完全逃检，而且不报错**。

实测（拆出 3 个端点后的分片单独扫描）：

    ✅ 有 5 个零 await 路由被消除（基线可变好）:
       - /admin-login  admin_login
       - /demo-mode  demo_mode
       ...                              ← 这些端点没消失，只是不在这个文件里
    exit = 0

把"没扫到"包装成"已修好"，还给了绿色通过。这比不做检查更坏：它会在
下一次重构时给出虚假的安全感，而真正的阻塞缺陷就埋在没扫到的那两个文件里。

本文件锁两件事：
  1. 传**目录**时，扫到的路由数必须与逐个文件扫的总和一致（不漏扫）。
  2. 扫到的路由数低于下限时，必须 exit=1 且明确说"疑似漏扫"，而不是报"改进"。
"""

import subprocess
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
REPO = BACKEND.parent
SCRIPT = REPO / "scripts" / "audit_async_blocking.py"
API_DIR = BACKEND / "app" / "api"


def _run(target) -> subprocess.CompletedProcess:
    """跑审计脚本，返回完整结果（不 raise，退出码由调用方断言）。"""
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(target)],
        capture_output=True, text=True, encoding="utf-8", cwd=str(REPO),
    )


def _route_count(stdout: str) -> int:
    """从输出里取"共 N 个路由"。取不到就是 None（让断言给出清晰失败）。"""
    for line in stdout.splitlines():
        if line.startswith("共 ") and "个路由" in line:
            return int(line.split("共 ")[1].split(" 个路由")[0])
    return None


def test_script_exists():
    assert SCRIPT.is_file(), f"审计脚本不在预期位置：{SCRIPT}"


def test_single_file_mode_still_works():
    """基线行为：单个文件仍能扫，且找到全部 53 个端点。

    这条防的是"改目录支持时把单文件路径改坏了"。
    """
    r = _run(API_DIR / "reschedule.py")
    assert r.returncode == 0, f"单文件模式失败：\n{r.stdout[-1500:]}\n{r.stderr[-500:]}"
    n = _route_count(r.stdout)
    assert n is not None, f"输出里没有路由统计：\n{r.stdout[-1000:]}"
    assert n >= 45, f"只扫到 {n} 个路由，明显少于预期"


def test_directory_mode_covers_all_routes(tmp_path):
    """← 本文件的核心：传目录必须扫全，且结论与单文件一致。

    做法：把真实的 reschedule.py 按**顶层节点边界**切成 3 份放进临时目录，
    然后拿目录去扫。因为切分点是函数/类的边界，语法完整，切完的端点总面
    应当与切之前**完全一致**（不多不少）。

    这一条如果失败，说明目录模式漏扫了文件——那么拆分大文件之后，
    漏掉那份里的阻塞缺陷就再也没人发现了。
    """
    src = (API_DIR / "reschedule.py").read_text(encoding="utf-8")

    import ast
    tree = ast.parse(src)
    lines = src.splitlines(keepends=True)
    top = [n for n in tree.body
           if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]

    def start_of(node):
        return min([d.lineno for d in getattr(node, "decorator_list", [])] + [node.lineno])

    header = "".join(lines[:start_of(top[0]) - 1])

    parts = 3
    groups = [[] for _ in range(parts)]
    for i, node in enumerate(top):
        groups[min(i * parts // len(top), parts - 1)].append(node)

    made = []
    for gi, group in enumerate(groups):
        if not group:
            continue
        chunks = ["".join(lines[start_of(n) - 1:n.end_lineno]) for n in group]
        p = tmp_path / f"part_{chr(97 + gi)}.py"
        p.write_text(header + "\n" + "\n\n".join(chunks), encoding="utf-8")
        made.append(p)
    assert len(made) == parts, f"分片数不对：{[p.name for p in made]}"

    # 先逐个文件扫，得到"真实总和"
    per_file = 0
    for p in made:
        r = _run(p)
        n = _route_count(r.stdout)
        assert n is not None, f"{p.name} 扫描输出异常：\n{r.stdout[-800:]}"
        per_file += n

    # 再扫整个目录
    r = _run(tmp_path)
    dir_total = _route_count(r.stdout)
    assert dir_total is not None, f"目录模式输出异常：\n{r.stdout[-1000:]}"

    assert dir_total == per_file, (
        f"目录模式扫到 {dir_total} 个路由，逐个文件扫是 {per_file} 个 —— 目录模式漏扫了"
    )
    # 并且要等于原文件的端点面（切分不增不减）
    r_orig = _run(API_DIR / "reschedule.py")
    assert dir_total == _route_count(r_orig.stdout), "切分后的端点总数与原文件不一致"
    assert r.returncode == 0, f"目录模式判红：\n{r.stdout[-1200:]}"


def test_incomplete_scan_fails_loudly(tmp_path):
    """← 第二条核心：扫不全时必须 exit=1，且不许把漏扫说成"改进"。

    造一个只有 1 行、不含任何路由的文件。旧脚本会：
        · 看到"基线里的路由都没出现" → 报 "✅ 有 8 个路由被消除（基线可变好）"
        · exit=0
    也就是漏扫 = 通过。现在必须是 exit=1 并说明疑似漏扫。
    """
    tiny = tmp_path / "tiny.py"
    tiny.write_text("x = 1\n", encoding="utf-8")

    r = _run(tiny)
    out = r.stdout

    assert r.returncode == 1, f"扫不到路由竟然通过了：\n{out[-1200:]}"
    assert "疑似漏扫" in out, f"没有明确指出漏扫：\n{out[-1200:]}"
    # 关键：不能再用 "✅ ... 被消除（基线可变好）" 来描述这次缺失
    assert "✅ 有" not in out.split("疑似漏扫")[-1], (
        f"漏扫时仍报告'✅ 被消除'，把没扫到说成改好了：\n{out[-1200:]}"
    )
    assert "不能" in out, f"没有提示本次结论不可信：\n{out[-1200:]}"


def test_unparsable_file_fails_loudly(tmp_path):
    """语法错的文件必须报错退出，不能静默跳过。

    如果悄悄跳过，一个改坏的模块就会让审计"看不见"它的端点，而 CI 仍然绿。
    """
    bad = tmp_path / "bad.py"
    bad.write_text("def broken(:\n    pass\n", encoding="utf-8")

    r = _run(bad)
    assert r.returncode == 1, f"语法错的文件竟然通过了：\n{r.stdout[-800:]}"
    assert "无法解析" in r.stdout, f"没有报出解析失败：\n{r.stdout[-800:]}"


def test_default_target_is_directory():
    """不带参数运行时的**默认值**必须是目录，不能是单个 .py。

    为什么直接读源码而不是"跑一次看端点够不够"：
    我第一版就是这么写的（断言 `n >= 45`），然后做缺陷注入 —— 把默认值改回
    `backend/app/api/reschedule.py` —— 测试**仍然 7 passed**。因为当前所有端点
    恰好都在这一个文件里，单文件扫出来也是 53 个。它测的是"此刻恰好等价"，
    不是"默认值正确"。等真拆分那天，这条测试会安静地通过，而默认值已经错了。

    所以判据要落到**写法**上：默认值必须是目录。
    """
    src = SCRIPT.read_text(encoding="utf-8")
    lines = [ln for ln in src.splitlines()
             if ln.strip().startswith("target = args[0] if args else")]
    assert lines, "找不到默认目标的赋值语句（可能被改名了，请同步本测试）"
    default = lines[0].split("else", 1)[1].strip().strip('"\'')
    assert not default.endswith(".py"), (
        f"默认目标仍是单个文件（{default}）—— 拆分后会静默退回只扫一份。应为目录 backend/app/api"
    )


def test_all_invocations_use_directory():
    """← 所有调用入口都必须传目录，一个都不能漏。

    为什么是"所有入口"而不是只看 CI：
    上一轮（R-112 第一版）只改了 `.github/workflows/ci.yml`，漏了
    `scripts/run_acceptance.py` —— 而后者是**真实交付的回归入口**，比 CI 更常被跑。
    只改一处等于没修：`run_acceptance.py` 仍会拿单个文件去扫，拆分后照样漏掉
    其余模块，并且打印绿色 PASS。

    这条测试把"哪些地方调用了这个审计"变成可枚举的：以后新增任何入口
    （Makefile、pre-commit、另一个验收脚本）只要传了 `.py` 结尾的路径就会红。
    """
    import re

    suspects = []
    patterns = ["*.yml", "*.yaml", "*.py", "*.sh", "*.toml", "*.cfg", "Makefile"]
    scan_dirs = [REPO / ".github", REPO / "scripts"]
    files = []
    for d in scan_dirs:
        for pat in patterns:
            files.extend(d.rglob(pat))
    # 根目录的构建/脚本入口也扫一眼
    for name in ("Makefile", "pre-commit-config.yaml"):
        p = REPO / name
        if p.is_file():
            files.append(p)

    for f in files:
        if f.name == "audit_scan_coverage.py":
            continue        # 本测试文件自身会提到脚本名，跳过
        try:
            text = f.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if "audit_async_blocking.py" not in line:
                continue
            if line.lstrip().startswith("#"):
                continue    # 注释里提到脚本名不算调用
            after = line.split("audit_async_blocking.py", 1)[1]
            # 去掉引号/逗号/括号等分隔符后取第一个非开关参数
            tokens = [t.strip("'\",[](){}") for t in re.split(r"[\s,]+", after)]
            tokens = [t for t in tokens if t and not t.startswith("-")]
            if not tokens:
                # 形如 ["...audit_async_blocking.py", "backend/app/api"] 的列表写法，
                # 参数在下一行；这里只记录可疑点交给断言提示人工确认
                suspects.append(f"{f.relative_to(REPO)}:{lineno} 参数在别处（需人工确认）")
                continue
            target = tokens[0]
            if target.endswith(".py"):
                suspects.append(f"{f.relative_to(REPO)}:{lineno} 传了单文件 {target}")

    assert not suspects, (
        "以下调用入口仍在传单个 .py 文件（拆分后会静默漏扫）：\n  "
        + "\n  ".join(suspects)
        + "\n应改为传目录 backend/app/api"
    )
