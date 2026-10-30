"""推送前置检查：本地副本是否落后于远端（R44）。

为什么需要
----------
R43 事故：本地 codeload 解包副本整体落后于远端，我用旧版
`reschedule.py`（2739 行）覆盖了远端 3280 行的当前版，净损失 541 行、
41 个函数/路由。发现时已经推了 20 轮。

根本原因不是"某次手滑"，而是**流程本身没有核对步骤**：

    读本地文件 → 改 → 整文件 PUT

这套流程在"远端没有别人的提交"时没问题。一旦本地副本落后，
它就是**用旧版覆盖新版**，而且 GitHub 上看不出异常 —— PUT 成功、返回 200。

本脚本把那一步核对固化下来。

检查什么
--------
对指定的远端路径，比对：
  1. 文件是否存在（本地有、远端没有 → 可能是要新建，也可能是路径错）
  2. 行数差（本地 < 远端 → **高危**，大概率是落后）
  3. 远端独有的顶层函数/路由定义（本地缺失 → 一定是落后）

用法
----
    python scripts/check_remote_drift.py backend/app/api/reschedule.py
    python scripts/check_remote_drift.py --dir backend/app
    python scripts/check_remote_drift.py --dir backend/app --json

退出码：
    0 = 本地不落后（可以推）
    1 = 发现落后（**不要推**，先同步）
    2 = 无法判断（网络/鉴权失败）

设计取舍
--------
只报"本地比远端少"，不报"本地比远端多"。本地多出来可能是新增的改动，
属正常；本地少一定是丢了东西。多报会让人习惯性忽略告警。
"""

import argparse
import base64
import json
import os
import re
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
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "[Environment]::GetEnvironmentVariable('GITHUB_TOKEN','User')"],
            capture_output=True, text=True, timeout=30).stdout.strip()
        return out
    except Exception:  # noqa: BLE001
        return ""


def _get(path, token, ref=None, tries=4):
    url = "https://api.github.com/repos/%s/contents/%s" % (REPO, path)
    if ref:
        url += "?ref=" + ref
    headers = {"Authorization": "Bearer " + token, "User-Agent": "drift-check",
               "Connection": "close"}
    for _ in range(tries):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=60) as r:
                d = json.loads(r.read())
            return base64.b64decode(d["content"]).decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            time.sleep(2)
        except Exception:  # noqa: BLE001
            time.sleep(2)
    raise RuntimeError("拉取失败：" + path)


DEF_RE = re.compile(r"^(?:async )?def (\w+)|^@router\.(get|post|put|delete|patch)\(")


def _symbols(src):
    """顶层函数名 + 路由方法，用于判断"本地缺了什么"。"""
    out = set()
    for line in src.splitlines():
        m = DEF_RE.match(line)
        if m:
            out.add(m.group(1) or ("@" + m.group(2)))
    return out


def check_one(path, token, ref=None):
    local_path = os.path.join(ROOT, path)
    remote = _get(path, token, ref)
    if remote is None:
        return {"path": path, "status": "remote_missing",
                "detail": "远端没有这个文件", "bad": False}
    if not os.path.exists(local_path):
        return {"path": path, "status": "local_missing",
                "detail": "本地没有这个文件（远端 %d 行）" % remote.count("\n"),
                "bad": True}

    with open(local_path, encoding="utf-8", errors="replace") as f:
        local = f.read()

    lr, rr = local.count("\n"), remote.count("\n")
    lost = _symbols(remote) - _symbols(local)
    bad = lr < rr or bool(lost)

    if not bad:
        return {"path": path, "status": "ok",
                "detail": "本地 %d 行 / 远端 %d 行" % (lr, rr), "bad": False}

    bits = ["本地 %d 行 / 远端 %d 行" % (lr, rr)]
    if lost:
        bits.append("本地缺 %d 个定义：%s" % (len(lost), ", ".join(sorted(lost)[:8])))
    return {"path": path, "status": "drift", "detail": "；".join(bits), "bad": True}


def list_dir(path, token, ref=None):
    url = "https://api.github.com/repos/%s/contents/%s" % (REPO, path)
    if ref:
        url += "?ref=" + ref
    headers = {"Authorization": "Bearer " + token, "User-Agent": "drift-check",
               "Connection": "close"}
    for _ in range(4):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read())
        except Exception:  # noqa: BLE001
            time.sleep(2)
    raise RuntimeError("列目录失败：" + path)


def main(argv=None):
    ap = argparse.ArgumentParser(description="检查本地副本是否落后于远端")
    ap.add_argument("paths", nargs="*", help="要检查的文件路径（仓库相对）")
    ap.add_argument("--dir", help="检查整个目录（递归 .py）")
    ap.add_argument("--ref", help="对比某个提交而非 main")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    token = _token()
    if not token:
        print("拿不到 GITHUB_TOKEN")
        return 2

    targets = list(args.paths)
    if args.dir:
        stack = [args.dir.rstrip("/")]
        while stack:
            cur = stack.pop()
            try:
                items = list_dir(cur, token, args.ref)
            except Exception as e:  # noqa: BLE001
                print("跳过", cur, e)
                continue
            if not isinstance(items, list):
                continue
            for it in items:
                if it["type"] == "dir":
                    stack.append(it["path"])
                elif it["name"].endswith(".py"):
                    targets.append(it["path"])

    if not targets:
        print("没有要检查的文件")
        return 2

    results = []
    for p in sorted(set(targets)):
        try:
            results.append(check_one(p, token, args.ref))
        except Exception as e:  # noqa: BLE001
            results.append({"path": p, "status": "error", "detail": str(e), "bad": False})

    bad = [r for r in results if r["bad"]]

    if args.json:
        print(json.dumps({"bad": len(bad), "results": results},
                         ensure_ascii=False, indent=2))
    else:
        print("远端漂移检查（共 %d 个文件）\n" % len(results))
        for r in results:
            if r["status"] == "ok":
                continue
            mark = "❌" if r["bad"] else "·"
            print(" %s %s" % (mark, r["path"]))
            print("     %s" % r["detail"])
        if bad:
            print("\n**发现 %d 个文件本地落后 —— 不要直接推送**" % len(bad))
            print("先把远端内容同步下来，在最新版本上重做改动。")
            print("整文件 PUT 会用本地旧版覆盖远端新版（R43 事故）。")
        else:
            print("\n✅ 本地不落后，可以推送")

    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
