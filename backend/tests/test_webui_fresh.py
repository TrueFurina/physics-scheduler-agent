"""前端产物新鲜度检查（R59）。

R57 事故：源码改了、`webui/` 没重建 —— **测试全绿、探针全绿，
只有用户会看到旧功能**。

`scripts/check_webui_fresh.py` 用"产物里有没有源码的关键文案"来判断，
而不是时间戳（时间戳会骗人：重新构建但内容没变，或 checkout 后全变）。

本文件测判定逻辑：
- 标记齐全 → 新鲜
- 缺任一标记 → 过期，且指出是哪个功能
- index.html 引用不存在的资源 → 不可用
- 未被引用的旧 chunk 要报出来
"""
import json
import os
import subprocess
import sys

import pytest

SCRIPT = os.path.join(os.path.dirname(__file__), "..", "..",
                      "scripts", "check_webui_fresh.py")
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load():
    if not os.path.isfile(SCRIPT):
        pytest.skip("缺少 scripts/check_webui_fresh.py")
    import importlib.util
    spec = importlib.util.spec_from_file_location("cwf", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _fake_webui(tmp_path, main_js_body, html_refs, extra_files=()):
    """搭一个最小 webui 目录。"""
    webui = tmp_path / "webui"
    assets = webui / "assets"
    assets.mkdir(parents=True)
    (webui / "index.html").write_text(
        "<html>" + "".join('<script src="assets/%s"></script>' % r for r in html_refs)
        + "</html>", encoding="utf-8")
    for r in html_refs:
        (assets / r).write_text("// placeholder", encoding="utf-8")
    # 主 JS 写实际内容
    for r in html_refs:
        if r.startswith("index-") and r.endswith(".js"):
            (assets / r).write_text(main_js_body, encoding="utf-8")
    for f in extra_files:
        (assets / f).write_text("// old", encoding="utf-8")
    return str(webui)


def test_fresh_when_all_markers_present(tmp_path, monkeypatch):
    cwf = _load()
    body = "".join(t for t, _ in cwf.MARKERS)
    webui = _fake_webui(tmp_path, body, ["index-abc.js"])
    monkeypatch.setattr(cwf, "WEBUI", webui)
    monkeypatch.setattr(cwf, "ASSETS", os.path.join(webui, "assets"))

    r = cwf.check()
    assert r["ok"] is True, r
    assert r["missing_markers"] == []


def test_stale_when_a_marker_is_missing(tmp_path, monkeypatch):
    cwf = _load()
    # 故意漏掉第一个标记（模拟"源码改了但产物没重建"）
    missing = cwf.MARKERS[0][0]
    body = "".join(t for t, _ in cwf.MARKERS if t != missing)
    webui = _fake_webui(tmp_path, body, ["index-abc.js"])
    monkeypatch.setattr(cwf, "WEBUI", webui)
    monkeypatch.setattr(cwf, "ASSETS", os.path.join(webui, "assets"))

    r = cwf.check()
    assert r["ok"] is False
    assert missing in r["missing_markers"], (
        f"漏了「{missing}」却没报出来 —— 这个功能没进产物"
    )


def test_reports_dangling_reference(tmp_path, monkeypatch):
    """index.html 引用了不存在的文件 → 页面白屏，必须报。"""
    cwf = _load()
    body = "".join(t for t, _ in cwf.MARKERS)
    webui = _fake_webui(tmp_path, body, ["index-abc.js"])
    # 手工加一个引用但不建文件
    idx = os.path.join(webui, "index.html")
    with open(idx, "w", encoding="utf-8") as f:
        f.write('<script src="assets/index-abc.js"></script>'
                '<script src="assets/gone.js"></script>')
    monkeypatch.setattr(cwf, "WEBUI", webui)
    monkeypatch.setattr(cwf, "ASSETS", os.path.join(webui, "assets"))

    r = cwf.check()
    assert r["ok"] is False
    assert any("gone.js" in p for p in r["problems"]), r["problems"]


def test_reports_unreferenced_stale_chunks(tmp_path, monkeypatch):
    """未被 index.html 也不被主 JS 引用的旧 chunk → 报出来（会随包上传）。"""
    cwf = _load()
    body = "".join(t for t, _ in cwf.MARKERS)
    webui = _fake_webui(tmp_path, body, ["index-abc.js"],
                        extra_files=["OldView-deadbeef.js"])
    monkeypatch.setattr(cwf, "WEBUI", webui)
    monkeypatch.setattr(cwf, "ASSETS", os.path.join(webui, "assets"))

    r = cwf.check()
    assert "OldView-deadbeef.js" in r["stale_chunks"], r


def test_dynamic_chunk_referenced_from_main_js_is_not_stale(tmp_path, monkeypatch):
    """子 chunk 由主 JS 动态 import，不出现在 index.html —— 不能算残留。"""
    cwf = _load()
    body = "".join(t for t, _ in cwf.MARKERS) + '"ParallelView-live.js"'
    webui = _fake_webui(tmp_path, body, ["index-abc.js"],
                        extra_files=["ParallelView-live.js"])
    monkeypatch.setattr(cwf, "WEBUI", webui)
    monkeypatch.setattr(cwf, "ASSETS", os.path.join(webui, "assets"))

    r = cwf.check()
    assert "ParallelView-live.js" not in r["stale_chunks"], (
        "把动态 import 的子 chunk 误判成残留"
    )


def test_real_repo_artifacts_are_fresh():
    """对照：真实仓库的产物应当是新鲜的（本地构建过）。"""
    cwf = _load()
    if not os.path.isdir(os.path.join(ROOT, "frontend", "webui", "assets")):
        pytest.skip("没有构建产物")
    r = cwf.check()
    assert r["ok"], (
        "仓库里的前端产物过期：%s —— 先 npx vite build" % r.get("missing_markers")
    )
