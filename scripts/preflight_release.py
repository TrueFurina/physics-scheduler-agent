"""D-9 发布预检（只读，不改任何状态）。

为什么需要
----------
发布参数与 sites 的坑散落在记忆里（appId 必须复用 / dist 被排除 / .gitignore
不生效 / cache 要先清 / DISABLE_DOCS 必须在 import app.main 之前），每次发布
靠人肉回忆 = 每次都可能踩一遍。本脚本把「发布前的就绪度」固化成可重复执行
的检查，说"发布"之前先跑它，绿了再发。

用法
----
    D:/miniconda3_new/python.exe scripts/preflight_release.py
    D:/miniconda3_new/python.exe scripts/preflight_release.py --offline   # 不打线上

退出码：0 = 就绪（可发布），1 = 有检查不过。
注意：「线上版本落后」是**信息项**不是失败项 —— 它正是发布的动机。
"""

import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEBUI = os.path.join(ROOT, "frontend", "webui")
LIVE = "https://physics-scheduler.app.workbuddy.host"

results = []  # (name, ok, detail, blocking)


def check(name, ok, detail, blocking=True):
    results.append((name, ok, detail, blocking))


def main():
    offline = "--offline" in sys.argv

    # 1. git 工作区干净
    st = subprocess.run(["git", "status", "--short"], cwd=ROOT,
                        capture_output=True, text=True).stdout.strip()
    head = subprocess.run(["git", "log", "--oneline", "-1"], cwd=ROOT,
                          capture_output=True, text=True).stdout.strip()
    check("git 工作区", not st, f"{head}" + (f"｜未提交：{st[:80]}" if st else "｜干净"))

    # 2. webui/index.html 引用的资源全存在
    html_path = os.path.join(WEBUI, "index.html")
    if os.path.exists(html_path):
        html = open(html_path, encoding="utf-8").read()
        refs = re.findall(r'(?:src|href)="(/assets/[^"]+)"', html)
        missing = [r for r in refs if not os.path.exists(os.path.join(WEBUI, r.lstrip("/")))]
        check("webui 产物完整性", not missing,
              f"index.html 引用 {len(refs)} 个资源" + (f"｜缺失 {missing}" if missing else "｜全在"))
        local_js = re.search(r"index-[A-Za-z0-9_-]+\.js", html)
        local_js = local_js.group(0) if local_js else "?"
    else:
        check("webui 产物完整性", False, "frontend/webui/index.html 不存在（先 npm run build）")

    # 3. run.py：自读 PORT + DISABLE_DOCS 在 import app.main 之前
    run_py = os.path.join(ROOT, "run.py")
    if os.path.exists(run_py):
        src = open(run_py, encoding="utf-8").read()
        port_ok = re.search(r"\bPORT\b", src) is not None or "os.environ" in src
        imp = src.find("import app.main")
        dis = src.find("DISABLE_DOCS")
        check("run.py 启动脚本", port_ok and (imp == -1 or dis < imp),
              "读 PORT ✓" if port_ok else "没读 PORT" +
              ("" if imp == -1 or dis < imp else "｜⚠️ DISABLE_DOCS 在 import app.main 之后"))
    else:
        check("run.py 启动脚本", False, "run.py 不存在")

    # 4. requirements.txt
    check("backend/requirements.txt",
          os.path.exists(os.path.join(ROOT, "backend", "requirements.txt")),
          "发布参数 installCmd 依赖它")

    # 5. cache 状态（发布前建议清；信息项）
    cache = os.path.join(ROOT, "backend", "app", "data", "cache")
    n = len(os.listdir(cache)) if os.path.isdir(cache) else 0
    check("解析缓存", True,
          f"cache/ 有 {n} 个文件" + ("｜发布前建议 rm -rf（旧表缓存会遮蔽新表）" if n else "｜已清"),
          blocking=False)

    # 6. 线上现状（信息项：线上落后 = 发布动机）
    if not offline:
        import urllib.request
        try:
            req = urllib.request.Request(f"{LIVE}/health", headers={"User-Agent": "preflight"})
            urllib.request.urlopen(req, timeout=15).read()
            live_html = urllib.request.urlopen(urllib.request.Request(
                LIVE + "/", headers={"User-Agent": "preflight"}), timeout=15).read().decode("utf-8", "ignore")
            m = re.search(r"index-[A-Za-z0-9_-]+\.js", live_html)
            live_js = m.group(0) if m else "?"
            stale = local_js != live_js
            check("线上版本对比", True,
                  f"线上 {live_js} vs 本地 {local_js}" +
                  ("｜⚠️ 线上落后，待发布（这正是 D-9）" if stale else "｜已一致"),
                  blocking=False)
        except Exception as e:
            check("线上可达性", True, f"打不到线上（{e}）—— 离线环境跳过", blocking=False)

    # 7. 本地 8023 预览（信息项）
    if not offline:
        import urllib.request
        try:
            urllib.request.urlopen("http://127.0.0.1:8023/health", timeout=5).read()
            check("本地 8023", True, "在跑（探针可用）", blocking=False)
        except Exception:
            check("本地 8023", True, "没在跑（发布不依赖它）", blocking=False)

    # ---- 汇总 ----
    print()
    blocking_fail = False
    for name, ok, detail, blocking in results:
        mark = "✅" if ok else ("❌" if blocking else "⚠️")
        print(f" {mark} {name}：{detail}")
        if not ok and blocking:
            blocking_fail = True
    print()
    print("Go —— 可以发布" if not blocking_fail else "No-Go —— 先修上面的 ❌")
    return 0 if not blocking_fail else 1


if __name__ == "__main__":
    sys.exit(main())
