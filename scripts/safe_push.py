"""安全推送：PUT 之前强制检查远端漂移（R58 的教训固化）。

为什么需要
----------
R43 和 R58 是**同一个错误犯了两次**：

    R43  用 2739 行的旧 reschedule.py 覆盖远端 3280 行
    R58  用 R54 的本地副本覆盖远端刚补回的 land/history 端点

第一次之后我建了 `check_remote_drift.py`，但**它要人去跑** ——
R54 推送时我没想到跑，于是又覆盖了一次。

真正的修法不是"下次记得"，而是让推送**必须**经过检查。

用法
----
    python scripts/safe_push.py \
        --message "fix: ..." \
        --file backend/app/api/reschedule.py \
        --file backend/tests/test_x.py

行为
----
1. 对每个 --file 先跑漂移检查（远端行数 / 顶层定义）
2. 任一文件落后 → **拒绝推送**，并打印同步建议
3. 全部通过 → 逐个 PUT

想强制推送（确认本地就是想要的版本）时加 `--force`，
但脚本会把"强制推送了 N 个落后文件"写进输出，方便事后追责。
"""
import argparse
import base64
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

REPO = "TrueFurina/physics-scheduler-agent"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _token():
    t = os.environ.get("GITHUB_TOKEN", "").strip()
    if t:
        return t
    return subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "[Environment]::GetEnvironmentVariable('GITHUB_TOKEN','User')"],
        capture_output=True, text=True, timeout=30).stdout.strip()


HEADERS = {}


def api(method, path, body=None, tries=4):
    url = "https://api.github.com/repos/%s/contents/%s" % (REPO, path)
    data = json.dumps(body).encode() if body else None
    for _ in range(tries):
        try:
            req = urllib.request.Request(url, data=data, method=method, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=90) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, e.read()[:200].decode("utf-8", "replace")
        except Exception:
            time.sleep(3)
    return 0, {"err": "retry exhausted"}


def drift_report(paths):
    """调 check_remote_drift 拿 JSON 结果。"""
    script = os.path.join(ROOT, "scripts", "check_remote_drift.py")
    if not os.path.isfile(script):
        return None
    cmd = [sys.executable, script, "--json"] + list(paths)
    proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=300)
    try:
        return json.loads(proc.stdout or "{}")
    except Exception:
        return None


def main(argv=None):
    ap = argparse.ArgumentParser(description="带漂移检查的推送")
    ap.add_argument("--message", required=True)
    ap.add_argument("--file", action="append", required=True,
                    help="仓库相对路径，可重复")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args(argv)

    global HEADERS
    HEADERS = {"Authorization": "Bearer " + _token(), "User-Agent": "safe-push",
               "Accept": "application/vnd.github+json",
               "Content-Type": "application/json", "Connection": "close"}

    report = drift_report(args.file)
    bad = []
    if report:
        bad = [r for r in report.get("results", []) if r.get("bad")]
    else:
        print("⚠️ 漂移检查不可用（脚本缺失或输出异常），按未检查处理")

    if bad:
        print("\n**拒绝推送：%d 个文件本地落后**\n" % len(bad))
        for r in bad:
            print("  ❌ %s\n     %s" % (r["path"], r.get("detail", "")))
        if not args.force:
            print("\n先把远端内容同步下来，在最新版本上重做改动。")
            print("确认本地就是想要的版本时，加 --force。")
            return 1
        print("\n⚠️ --force：强制推送 %d 个落后文件（可能覆盖远端更新）" % len(bad))

    failed = 0
    for p in args.file:
        local = os.path.join(ROOT, p)
        if not os.path.isfile(local):
            print("  %-50s SKIP 本地不存在" % p)
            failed += 1
            continue
        s, d = api("GET", p)
        with open(local, "rb") as f:
            raw = f.read()
        body = {"message": args.message,
                "content": base64.b64encode(raw).decode()}
        if s == 200:
            body["sha"] = d["sha"]
        s2, r2 = api("PUT", p, body)
        if s2 in (200, 201):
            print("  %-50s -> %s %s" % (p, s2, r2["commit"]["sha"][:10]))
        else:
            print("  %-50s -> %s %s" % (p, s2, r2))
            failed += 1
        time.sleep(0.5)

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
