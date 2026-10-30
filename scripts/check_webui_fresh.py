"""前端产物新鲜度：webui/ 是否真的由当前源码构建（R59）。

为什么需要
----------
R57 的事故：

```
webui/index.html   2026-10-30 05:33
之后改过           RescheduleView.tsx / App.tsx / types.ts / client.ts
```

源码改了、产物没重建。**所有测试都是绿的**（测试跑源码），
探针也绿（探针跑产物，但产物的功能是旧的）——
唯一能发现的是"拿产物里的内容对照源码"。

为什么不用时间戳
----------------
时间戳能骗人：重新构建了但内容没变（时间戳新、功能旧），
或者 checkout 之后时间戳全变（时间戳新、实际是旧代码）。
所以本脚本比的是**内容**：

1. `webui/index.html` 引用的资源必须都存在（否则页面白屏）
2. 产物里的 hash 文件名必须与"当前源码构建出的"一致 —— 但构建要跑 vite，
   太重。退一步：检查**若干源码里的关键标记字符串**是否出现在产物里。
3. 反向检查：`webui/assets/` 里不应有 index.html 不引用的旧 chunk（占体积）

第 2 条是核心。选标记的原则：
- 必须是**用户可见文案**（不会被压缩掉、不会改名）
- 每条对应某轮修复（这样失败时能指出"哪个功能没进产物"）

用法
----
    python scripts/check_webui_fresh.py
    python scripts/check_webui_fresh.py --json

退出码：0 = 新鲜；1 = 过期/缺文件；2 = 无法判断
"""

import argparse
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEBUI = os.path.join(ROOT, "frontend", "webui")
ASSETS = os.path.join(WEBUI, "assets")

# (文案, 对应轮次/功能) —— 每条都是用户可见的、不会被压缩改名
MARKERS = [
    ("冲突明细", "R47 影响分析明细"),
    ("确认驳回", "R34 驳回理由"),
    ("没认出是哪门课", "R33 课程未识别提示"),
    ("仅显示前", "R47 明细截断说明"),
]


def _main_js():
    idx = os.path.join(WEBUI, "index.html")
    if not os.path.isfile(idx):
        return None, ["webui/index.html 不存在"]
    with open(idx, encoding="utf-8") as f:
        html = f.read()
    refs = re.findall(r"assets/([A-Za-z0-9_.\-]+)", html)

    problems = []
    for r in refs:
        if not os.path.isfile(os.path.join(ASSETS, r)):
            problems.append("index.html 引用了不存在的资源：%s" % r)

    local = [r for r in refs if r.startswith("index-") and r.endswith(".js")]
    if not local:
        problems.append("index.html 里找不到主 JS 入口（index-*.js）")
        return None, problems
    return local[0], problems


def check():
    main, problems = _main_js()
    if main is None:
        return {"ok": False, "problems": problems, "markers": {}, "stale_chunks": []}

    with open(os.path.join(ASSETS, main), encoding="utf-8", errors="replace") as f:
        js = f.read()

    markers = {}
    for text, why in MARKERS:
        markers[text] = {"found": text in js, "why": why}

    # 未被引用的旧 chunk
    idx = os.path.join(WEBUI, "index.html")
    with open(idx, encoding="utf-8") as f:
        referenced = set(re.findall(r"assets/([A-Za-z0-9_.\-]+)", f.read()))
    stale = []
    if os.path.isdir(ASSETS):
        for name in os.listdir(ASSETS):
            if name in referenced:
                continue
            # 子 chunk 由主 JS 动态 import，不一定出现在 index.html 里 ——
            # 只要能在主 JS 里找到文件名引用，就不算残留。
            if name in js:
                continue
            stale.append(name)

    missing_markers = [t for t, v in markers.items() if not v["found"]]
    ok = not problems and not missing_markers
    return {"ok": ok, "problems": problems, "markers": markers,
            "missing_markers": missing_markers, "stale_chunks": sorted(stale),
            "main": main}


def main(argv=None):
    ap = argparse.ArgumentParser(description="检查前端产物是否由当前源码构建")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    r = check()

    if args.json:
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return 0 if r["ok"] else 1

    if not r.get("main"):
        print("❌ 产物不可用：")
        for p in r["problems"]:
            print("   " + p)
        return 1

    print("前端产物新鲜度检查\n")
    print("  主入口：%s" % r["main"])
    for p in r["problems"]:
        print("  ❌ " + p)
    for text, v in r["markers"].items():
        mark = "✅" if v["found"] else "❌"
        print("  %s 「%s」 — %s" % (mark, text, v["why"]))
    if r["stale_chunks"]:
        print("\n  ⚠️ 未被引用的旧 chunk（会随发布包上传）：")
        for s in r["stale_chunks"]:
            print("     " + s)

    if r["ok"]:
        print("\n✅ 产物与源码一致")
    else:
        print("\n❌ **产物过期** —— 先跑 `cd frontend && npx vite build --outDir webui --emptyOutDir`")
        print("   源码改了但产物没重建时，测试全绿、探针全绿，只有用户会看到旧功能。")
    return 0 if r["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
