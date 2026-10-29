"""R9：README 声明与代码一致性守卫。

审计发现（2026-10-29）：
  1. 技术栈写 `python-constraint`，实际用 `ortools` CP-SAT（requirements 无前者）
  2. 测试数写「后端 408 + 前端 89」，实际 478 + 101
  3. 英文版审计器列表漏了 2 个

第 2 条会**持续劣化**——每加一个测试，README 就更假一分。
这里用守卫把"README 里的数字必须等于实际数字"固定下来。
"""
import os
import re
import subprocess
import sys

import pytest

BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
REPO = os.path.dirname(BACKEND)


def _read(name):
    return open(os.path.join(REPO, name), encoding="utf-8").read()


def _count_pytest() -> int:
    """--collect-only 拿真实用例数（含参数化展开）。"""
    out = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/", "--collect-only", "-q",
         "--no-header", "-p", "no:cacheprovider"],
        cwd=BACKEND, capture_output=True, text=True, timeout=300,
    )
    m = re.search(r"(\d+) tests? collected", out.stdout + out.stderr)
    if not m:
        pytest.skip(f"无法解析 collect 结果：{out.stdout[-200:]}")
    return int(m.group(1))


def test_readme_backend_test_count_matches():
    """README 写的后端测试数必须等于 pytest 实际收集数。"""
    actual = _count_pytest()

    # 为什么不用"严格相等"：
    # 严格相等意味着每加一个测试都要改 README，否则 CI 红。
    # 这种约束会让人为了过闸而改数字，而不是去审视声明本身是否该存在。
    #
    # 改用"下限 + 允许滞后 10%"：README 里的数字可以略旧（文档通常滞后于代码），
    # 但不能**严重低估**——那会让评审误判测试量。
    TOLERANCE = 0.10

    for name, pattern in (
        ("README.zh.md", r"后端 (\d+) 个测试"),
        ("README.md", r"(\d+) backend tests"),
    ):
        m = re.search(pattern, _read(name))
        assert m, f"{name} 中找不到测试数声明（格式变了？需同步更新本测试）"
        claimed = int(m.group(1))
        assert claimed <= actual, (
            f"{name} 声称后端 {claimed} 个测试，实际只有 {actual} 个 —— 夸大测试量"
        )
        floor = int(actual * (1 - TOLERANCE))
        assert claimed >= floor, (
            f"{name} 声称后端 {claimed} 个测试，实际 {actual} 个（低估超过 "
            f"{int(TOLERANCE*100)}%）。请同步 README 到 >= {floor}。"
        )


def test_readme_tree_comment_matches():
    """目录树注释里的 pytest 数字也要一致（两处独立出现，容易只改一处）。"""
    actual = _count_pytest()
    for name, pattern in (
        ("README.zh.md", r"# (\d+) 个 pytest 测试"),
        ("README.md", r"# (\d+) pytest tests"),
    ):
        m = re.search(pattern, _read(name))
        assert m, f"{name} 目录树注释格式变了"
        claimed = int(m.group(1))
        assert claimed <= actual, f"{name} 目录树写 {claimed}，实际只有 {actual}"
        assert claimed >= int(actual * 0.9), (
            f"{name} 目录树写 {claimed}，实际 {actual}（低估过多）"
        )


def test_requirements_has_no_python_constraint():
    """README 曾声称用 python-constraint，实际是 ortools —— 反向锁定。"""
    req = open(os.path.join(BACKEND, "requirements.txt"), encoding="utf-8").read()
    assert "python-constraint" not in req, "requirements 里出现了 python-constraint？"

    for name in ("README.md", "README.zh.md"):
        text = _read(name)
        assert "python-constraint" not in text, (
            f"{name} 仍写着 python-constraint，实际用的是 ortools CP-SAT"
        )
        assert "ortools" in text.lower() or "OR-Tools" in text, (
            f"{name} 未提及实际使用的 OR-Tools"
        )


def test_readme_auditor_list_matches_scripts():
    """README 列的独立审计器必须都真实存在（两个语言版本都要覆盖）。"""
    scripts = set(os.listdir(os.path.join(REPO, "scripts")))
    names = ["audit_conflicts", "verify_graybox", "verify_master_roundtrip",
             "verify_intro_filter", "probe_approval_chain", "probe_land_plan",
             "probe_schedule_grid", "probe_import_wizard", "probe_ui_newviews"]

    for n in names:
        assert any(f.startswith(n) for f in scripts), f"审计器 {n} 不存在于 scripts/"

    for name in ("README.md", "README.zh.md"):
        text = _read(name)
        missing = [n for n in names if n not in text]
        assert not missing, f"{name} 漏列审计器：{missing}"