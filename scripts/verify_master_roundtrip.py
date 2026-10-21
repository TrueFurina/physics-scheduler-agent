"""主数据台账 导出→编辑→导回 端到端核查（真实进程，不是单测里的模拟）

为什么不用单测代替
------------------
单测里的"重启"是 `monkeypatch` 换掉模块变量，**不等于进程真的重启**：
落盘路径、fingerprint 计算、`_ensure_master_data()` 的调用时机都是真的，
但 uvicorn 的生命周期、真实表的 mtime、真实表 433 行的班级名带顿号
（「2024级应用化学1、2班」）—— 这些只在真进程里才成立。

核查项
------
  R1  导出的台账能被自己导回（往返无损：五张表实体数不变）
  R2  改过的格子真的写回（新项目出现 / 容量变化）
  R3  导回**不产生碎班级**（真实表 5 个班名自带顿号，切碎了班级数会变多）
  R4  `--after-restart`：进程真的重启后，导入的实体还在（持久化闭环）

用法
----
    D:/miniconda3_new/python.exe scripts/verify_master_roundtrip.py            # 导入并核查
    # 重启后端（模拟线上沙箱回收）
    D:/miniconda3_new/python.exe scripts/verify_master_roundtrip.py --after-restart
    D:/miniconda3_new/python.exe scripts/verify_master_roundtrip.py --cleanup   # 清理测试产物

前置：后端已启动且 load-default 完成。退出码 0=全过，1=有核查不过。
"""

import argparse
import json
import os
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request

import openpyxl

SITE = "http://127.0.0.1:8023"
API = f"{SITE}/api/reschedule"
MARKER = os.path.join(tempfile.gettempdir(), "master_roundtrip_marker.json")
ADMIN = {"username": "admin", "password": "admin123"}

NEW_PROJECT = "台账往返核查项目（可删）"
NEW_TEACHER = "往返核查教师（可删）"
CAPACITY_ROOM = "A410"
CAPACITY_VALUE = 4321


# ---------------------------------------------------------------- HTTP

def _get(path, token=None):
    req = urllib.request.Request(f"{API}{path}")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def _post_json(path, payload):
    req = urllib.request.Request(
        f"{API}{path}", data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def _delete(path, token):
    req = urllib.request.Request(f"{API}{path}", method="DELETE")
    req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def _post_file(path, filename: str, content: bytes, token: str):
    """multipart/form-data 上传（不引第三方依赖，与项目其余脚本一致）。"""
    boundary = "----verifyMasterRoundtrip"
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        f"Content-Type: application/vnd.openxmlformats-officedocument."
        f"spreadsheetml.sheet\r\n\r\n"
    ).encode("utf-8") + content + f"\r\n--{boundary}--\r\n".encode("utf-8")
    req = urllib.request.Request(
        f"{API}{path}", data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}",
                 "Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read().decode("utf-8"))


def login() -> str:
    return _post_json("/admin-login", ADMIN)["token"]


def overview():
    return _get("/master/overview")["data"]


def counts(ov):
    return {k: len(ov[k]) for k in ("courses", "experiments", "teachers", "rooms", "classes")}


# ---------------------------------------------------------------- 主流程

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--site", default=SITE)
    ap.add_argument("--after-restart", action="store_true",
                    help="核查上一次导入的实体在真重启后是否还在")
    ap.add_argument("--cleanup", action="store_true", help="删除本次核查注入的测试实体")
    args = ap.parse_args()
    global API
    API = f"{args.site}/api/reschedule"

    fails = []

    def check(ok, label, detail=""):
        print(f"  {'✅' if ok else '❌'} {label}{(' —— ' + detail) if detail else ''}")
        if not ok:
            fails.append(label)

    if args.cleanup:
        token = login()
        _delete(f"/master/experiments/{urllib.parse.quote(NEW_PROJECT)}", token)
        _delete(f"/master/teachers/{urllib.parse.quote(NEW_TEACHER)}", token)
        print(f"✅ 已删除测试实体；落盘文件请手动删除：backend/app/data/master_store.json")
        if os.path.exists(MARKER):
            os.remove(MARKER)
        return 0

    if args.after_restart:
        if not os.path.exists(MARKER):
            print("❌ 没有找到上一次导入的标记文件，先跑一次不带参数的核查")
            return 1
        m = json.load(open(MARKER, encoding="utf-8"))
        ov = overview()
        check(NEW_PROJECT in {e["name"] for e in ov["experiments"]},
              "R4 重启后导入的实验项目仍在", f"项目数 {len(ov['experiments'])}")
        check(m["capacity_room"] in {r["name"] for r in ov["rooms"]}
              and dict((r["name"], r["capacity"]) for r in ov["rooms"]).get(m["capacity_room"]) == m["capacity_value"],
              "R4 重启后改过的容量仍是改后的值")
        check(counts(ov) == m["counts"], "R4 重启后五张表实体数与导入后一致",
              f"{counts(ov)} vs {m['counts']}")
        print("\n" + ("Go —— 持久化闭环完整" if not fails else "No-Go —— 重启后数据丢了"))
        return 1 if fails else 0

    token = login()
    base = overview()
    print(f"基线：{counts(base)}")

    # ── 导出 ──
    req = urllib.request.Request(f"{API}/export/master-workbook")
    with urllib.request.urlopen(req, timeout=120) as r:
        wb_bytes = r.read()
    tmp = os.path.join(tempfile.gettempdir(), "master_roundtrip.xlsx")
    open(tmp, "wb").write(wb_bytes)

    # ── 编辑：加一个新项目 + 改一间场地的容量 ──
    wb = openpyxl.load_workbook(tmp)
    ws = wb["实验项目"]
    first_cls = str(ws.cell(row=2, column=5).value or "").split(";")[0].strip()
    ws.append([NEW_PROJECT, "大学物理实验", NEW_TEACHER, CAPACITY_ROOM, first_cls])
    ws2 = wb["场地"]
    for row in ws2.iter_rows(min_row=2):
        if str(row[0].value).strip() == CAPACITY_ROOM:
            row[1].value = CAPACITY_VALUE
    wb.save(tmp)
    print(f"编辑：新增项目「{NEW_PROJECT}」（适用班级 {first_cls}），{CAPACITY_ROOM} 容量 → {CAPACITY_VALUE}")

    # ── 导回 ──
    res = _post_file("/master/import", "master.xlsx", open(tmp, "rb").read(), token)
    after = res["data"]
    print(f"导入回执：{res['message']}")
    for w in (res.get("report") or {}).get("warnings") or []:
        print(f"  ⚠️ {w}")

    check(NEW_PROJECT in {e["name"] for e in after["experiments"]}, "R2 新项目已写入")
    check(NEW_TEACHER in {t["name"] for t in after["teachers"]}, "R2 新教师已写入（反向引用）")
    check(dict((r["name"], r["capacity"]) for r in after["rooms"]).get(CAPACITY_ROOM) == CAPACITY_VALUE,
          "R2 改过的容量已写回")
    check(counts(after)["classes"] == counts(base)["classes"],
          "R3 导回没有多出碎班级（真实班名自带顿号）",
          f"{counts(base)['classes']} → {counts(after)['classes']}")
    check(not any(c["name"].strip() in ("2班", "3班", "2") for c in after["classes"]),
          "R3 没有出现「2班/3班」这类残片班级")
    check(counts(after)["experiments"] == counts(base)["experiments"] + 1,
          "R1 项目数只多出新增的那一个",
          f"{counts(base)['experiments']} → {counts(after)['experiments']}")

    json.dump({"counts": counts(after), "capacity_room": CAPACITY_ROOM,
               "capacity_value": CAPACITY_VALUE},
              open(MARKER, "w", encoding="utf-8"), ensure_ascii=False)

    print("\n" + ("Go —— 台账可导回。下一步：重启后端后跑 "
                  "`--after-restart` 验证持久化，再跑 `--cleanup` 清理"
                  if not fails else "No-Go"))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
