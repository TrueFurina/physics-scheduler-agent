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
        with open(html_path, encoding="utf-8") as f:
            html = f.read()
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
        with open(run_py, encoding="utf-8") as f:
            src = f.read()
        port_ok = re.search(r"\bPORT\b", src) is not None or "os.environ" in src
        imp = src.find("import app.main")
        dis = src.find("DISABLE_DOCS")
        check("run.py 启动脚本", port_ok and (imp == -1 or dis < imp),
              "读 PORT ✓" if port_ok else "没读 PORT" +
              ("" if imp == -1 or dis < imp else "｜⚠️ DISABLE_DOCS 在 import app.main 之后"))
    else:
        check("run.py 启动脚本", False, "run.py 不存在")

    # 4. requirements.txt：不只看文件在不在，还要**真的能导入**
    #    （2026-10-24 交付评审 P0-1：python-multipart 缺失时文件存在、服务起不来，
    #     旧检查只查文件存在所以全绿——发布参数 installCmd 依赖这个文件）
    check("backend/requirements.txt",
          os.path.exists(os.path.join(ROOT, "backend", "requirements.txt")),
          "发布参数 installCmd 依赖它")
    smoke = subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.path.insert(0, 'backend');"
         "from app.main import app;"
         # 数 OpenAPI 路径而非 app.routes：本版本 FastAPI 的 include_router
         # 在 app.routes 里是懒加载的 _IncludedRouter 对象（实测只有 8 条），
         # 打印那个数会让人误以为路由丢了；openapi 路径数才是真实端点面。
         "n = len(app.openapi()['paths']);"
         "assert n >= 40, f'routes dropped: {n}';"
         "print(f'{n} endpoints')"],
        cwd=ROOT, capture_output=True, text=True)
    smoke_ok = smoke.returncode == 0
    if smoke_ok:
        detail = "app 可导入，%s" % smoke.stdout.strip()
    else:
        last = smoke.stderr.strip().splitlines()[-1] if smoke.stderr.strip() else "?"
        detail = "导入失败：%s" % last
    check("依赖可导入（import app.main）", smoke_ok, detail)

    # 4b. 上传面敏感文件：发布上传器只排除 node_modules/.git/构建产物，
    #     **不读 .gitignore**（R-02 证据链，docs/安全风险清单）—— 所以
    #     .gitignore 里的文件会被一起打包上传。
    #     - schedule.xlsx：真实表，「教师联系表」携带 17 个教师手机号
    #       （2026-10-24 交付评审 P0-2：当前无 HTTP 端点泄露，但属"已上膛
    #       未击发"）。线上要显示真实姓名是产品目标（R-09/D-2 有决策记录），
    #       所以这里是**显式拦截**：发布前必须二选一 —— 移出仓库并把
    #       SCHEDULE_PATH 指向外部路径，或先把联系表手机号列脱敏。
    #     - .env / env.local：智谱密钥明文。沙箱不能注入环境变量，线上 LLM
    #       依赖它随包上传（docs/安全风险清单 v4 记录）；线上不可下载已实测。
    #       按 D-6 当作"可轮换的演示 key"管理 —— 警告级，不拦截。
    if os.path.exists(os.path.join(ROOT, "schedule.xlsx")):
        check("上传面·真实排课表（17 个手机号）", False,
              "仓库根 schedule.xlsx 会被上传（上传器不读 .gitignore）；"
              "发布前移出仓库+SCHEDULE_PATH 外置，或脱敏「教师联系表」手机号列")
    else:
        check("上传面·真实排课表（17 个手机号）", True, "不在仓库根（无上传暴露）")
    env_files = [n for n in (".env", "env.local")
                 if os.path.exists(os.path.join(ROOT, n))]
    check("上传面·密钥配置文件", not env_files,
          "发现 %s（智谱 key 明文随包上传；线上 LLM 依赖它，按 D-6 当作可轮换"
          "演示 key，建议发布前轮换）" % ", ".join(env_files) if env_files else "无",
          blocking=False)

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
