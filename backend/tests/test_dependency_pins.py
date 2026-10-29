"""依赖版本守卫（R12 后果固化）。

背景：`python-multipart 0.0.20` 有无界 part 头 DoS，且发生在**鉴权之前**，
匿名请求即可触发。实测单个 10MB header 耗时 1532ms，升级到 0.0.32 后降到 2ms。

这类问题不会让任何测试变红——测试只验证功能，不验证依赖版本。
所以需要显式的版本断言。
"""
import os
import re

import pytest

BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def _pins():
    """解析 requirements.txt 的 name==version。"""
    path = os.path.join(BACKEND, "requirements.txt")
    out = {}
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^([A-Za-z0-9_.\-]+)(\[[^\]]+\])?==([0-9A-Za-z.\-]+)$", line)
        if m:
            out[m.group(1).lower()] = m.group(3)
    return out


def _ver_tuple(v):
    return tuple(int(x) for x in re.findall(r"\d+", v)[:3])


def test_python_multipart_is_patched():
    """python-multipart 必须 >= 0.0.32（无界 part 头 DoS 的修复版本）。"""
    pins = _pins()
    assert "python-multipart" in pins, "requirements.txt 里找不到 python-multipart"
    v = pins["python-multipart"]
    assert _ver_tuple(v) >= (0, 0, 32), (
        f"python-multipart 锁定为 {v}，低于 0.0.32。"
        "0.0.20 存在无界 part 头 DoS（匿名可达，见 docs/依赖安全-20261029.md）"
    )


def test_python_dotenv_is_patched():
    """python-dotenv 必须 >= 1.2.2（set_key 符号链接覆盖的修复版本）。"""
    pins = _pins()
    assert "python-dotenv" in pins
    v = pins["python-dotenv"]
    assert _ver_tuple(v) >= (1, 2, 2), f"python-dotenv 锁定为 {v}，低于 1.2.2"


def test_ortools_stays_on_known_good_version():
    """ortools 必须留在 9.11.x —— 9.15 在 Python 3.12 会段错误。

    这是反向守卫：审计工具会把 ortools 标为「落后」，但升级会崩。
    """
    pins = _pins()
    assert "ortools" in pins
    v = pins["ortools"]
    assert _ver_tuple(v) < (9, 15), (
        f"ortools 锁定为 {v}。9.15.x 在 Python 3.12 调用 CpSolver.Solve() "
        "会 Segmentation fault（见 docs/ortools-compat.md）"
    )
    assert _ver_tuple(v) >= (9, 11), f"ortools {v} 过旧，建议 9.11.4210"


def test_security_doc_exists():
    """升级依据必须留档，否则下一个人不知道为什么锁这个版本。"""
    doc = os.path.join(BACKEND, "..", "docs", "依赖安全-20261029.md")
    assert os.path.isfile(doc), "缺少 docs/依赖安全-20261029.md"
    text = open(doc, encoding="utf-8").read()
    assert "1532" in text or "1,532" in text, "文档里应包含 0.0.20 的实测耗时"
    assert "鉴权之前" in text, "文档里应说明计算发生在鉴权之前"
