"""导入向导探针：预览（只读）→ 确认导入 → 清理还原。

为什么需要单独一把尺子
----------------------
导入是**写路径**：外校格式的 Excel 一传，实体就进主数据五张表。R-63 之前
的痛点是"文件传完才知道被解析成什么"——警告只在事后出现。向导把解析变成
可见的一步（预览），但这也引入了新的失败方式：

  * **预览说了谎**：界面显示"将导入 12 行"，确认后实际只进 9 行
    （预览与写库两套解析代码漂移）；
  * **预览本身就在写**：只读承诺被破坏 —— 教秘还没点确认，数据已经进库，
    而界面上没有任何迹象。

本探针拿一份**外校格式**文件（别名列名 + 统计列 + 未识别列）走真实接口：

  W1  预览返回完整映射：字段←列名、未识别列、统计列忽略、行数汇总
  W2  预览之后主数据**一个字节都没变**（只读承诺；开发期验证过敢红 ——
      临时让预览端点调用 apply_tables，这条立刻红）
  W3  确认导入：标记实体真的出现，且导入行数 == 预览行数（预览没说谎）
  W4  清理还原：删掉标记实体，五张表回到基线（探针不污染后续基线）
  T1  任务清单预览：列名映射 / 空行计数 / 未识别列 / 样本
  T2  清单预览同样不写 _master_tasks
  T3  确认上传后进待排任务，再用空表头文件还原（基线非空时 SKIP：
      不覆盖服务器上可能已有的清单）
  T4  缺「班级/实验项目」列 → 400 且说清缺什么

用法：
  D:/miniconda3_new/python.exe scripts/probe_import_wizard.py
  D:/miniconda3_new/python.exe scripts/probe_import_wizard.py --site http://127.0.0.1:8023
  D:/miniconda3_new/python.exe scripts/probe_import_wizard.py --cleanup   # 只清理上一轮残留

前置：后端已启动且 load-default 完成。退出码 0=全过，1=有核查不过。
"""

import argparse
import io
import json
import os
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request

import openpyxl

DEFAULT_SITE = "http://127.0.0.1:8023"
ADMIN = {"username": "admin", "password": "admin123"}

MARK_EXP = "向导探针项目（可删）"
MARK_EXP2 = "向导探针项目二（可删）"
MARK_TEACHER = "向导探针教师（可删）"
MARK_ROOM = "向导探针场地（可删）"


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


def _post_file(path, content: bytes, token: str):
    """multipart/form-data 上传（不引第三方依赖，与项目其余脚本一致）。"""
    boundary = "----probeImportWizard"
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="probe.xlsx"\r\n'
        f"Content-Type: application/vnd.openxmlformats-officedocument."
        f"spreadsheetml.sheet\r\n\r\n"
    ).encode("utf-8") + content + f"\r\n--{boundary}--\r\n".encode("utf-8")
    req = urllib.request.Request(
        f"{API}{path}", data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}",
                 "Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read()
        try:
            return e.code, json.loads(body)
        except Exception:
            return e.code, {"raw": body[:300].decode("utf-8", "replace")}


def login() -> str:
    return _post_json("/admin-login", ADMIN)["token"]


def overview():
    return _get("/master/overview")["data"]


def master_status():
    return _get("/master-status")["data"]


def counts(ov):
    return {k: len(ov[k]) for k in ("courses", "experiments", "teachers", "rooms", "classes")}


# ---------------------------------------------------------------- 测试文件

def build_foreign_workbook() -> bytes:
    """外校格式台账：别名列名（项目名称/任课教师）+ 统计列（班级数）+ 未识别列（编号/备注）。"""
    buf = io.BytesIO()
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "实验项目"
    ws.append(["编号", "项目名称", "班级数", "任课教师", "备注"])
    ws.append(["E01", MARK_EXP, 5, MARK_TEACHER, "探针注入"])
    ws.append(["E02", MARK_EXP2, 3, MARK_TEACHER, ""])
    wb.create_sheet("教师").append(["教师", "可授项目"])
    wb["教师"].append([MARK_TEACHER, MARK_EXP])
    wb.create_sheet("场地").append(["场地", "容量"])
    wb["场地"].append([MARK_ROOM, 40])
    wb.save(buf)
    return buf.getvalue()


def build_task_list() -> bytes:
    """任务清单：别名表头 + 一行缺班级（应计入 blank_or_incomplete）+ 未识别列。"""
    buf = io.BytesIO()
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "实验项目清单"
    ws.append(["课程", "班级", "实验项目", "学生组", "备注"])
    ws.append(["大学物理实验", "探针班级甲", "迈克耳孙干涉", "1组", "x"])
    ws.append(["大学物理实验", "探针班级乙", "声速测量", "2组", ""])
    ws.append(["大学物理实验", "", "缺班级的行", "1组", ""])
    wb.save(buf)
    return buf.getvalue()


def build_header_only_list() -> bytes:
    """只有表头、没有数据行：用来把 _master_tasks 还原成空。"""
    buf = io.BytesIO()
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "实验项目清单"
    ws.append(["课程", "班级", "实验项目", "学生组"])
    wb.save(buf)
    return buf.getvalue()


def build_bad_list() -> bytes:
    """缺「班级/实验项目」两列 —— 必须 400 并说清缺什么。"""
    buf = io.BytesIO()
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Sheet1"
    ws.append(["序号", "备注"])
    ws.append([1, "甲"])
    wb.save(buf)
    return buf.getvalue()


# ---------------------------------------------------------------- 主流程

class Report:
    def __init__(self):
        self.rows = []

    def add(self, ok, name, detail):
        self.rows.append((ok, name, detail))
        print(f"{'PASS' if ok else 'FAIL'}  {name}  — {detail}")

    def skip(self, name, why):
        print(f"SKIP  {name}  — {why}")

    def done(self):
        bad = [r for r in self.rows if not r[0]]
        print()
        print(f"{len(self.rows) - len(bad)}/{len(self.rows)} 通过")
        for _, n, d in bad:
            print(f"  未通过：{n} — {d}")
        return 0 if not bad else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--site", default=DEFAULT_SITE)
    ap.add_argument("--cleanup", action="store_true",
                    help="只删除探针注入的标记实体（上一轮异常退出时用）")
    args = ap.parse_args()
    global API
    API = f"{args.site.rstrip('/')}/api/reschedule"
    rep = Report()

    code, login_resp = 0, {}
    try:
        token = login()
    except urllib.error.HTTPError as e:
        token = ""
        login_resp = {"raw": e.read()[:200].decode("utf-8", "replace")}
        code = e.code
    if not token:
        rep.add(False, "管理员登录拿到令牌",
                f"HTTP {code} {login_resp} —— 后续写接口全会被拒")
        return rep.done()
    rep.add(True, "管理员登录拿到令牌", "OK")

    if args.cleanup:
        for path in (f"/master/experiments/{urllib.parse.quote(MARK_EXP)}",
                     f"/master/experiments/{urllib.parse.quote(MARK_EXP2)}",
                     f"/master/teachers/{urllib.parse.quote(MARK_TEACHER)}",
                     f"/master/rooms/{urllib.parse.quote(MARK_ROOM)}"):
            try:
                _delete(path, token)
            except urllib.error.HTTPError:
                pass    # 本来就不存在
        print("✅ 已删除探针标记实体（若存在）")
        return 0

    # ── 基线 ──
    base = overview()
    base_counts = counts(base)
    base_tasks = master_status()["master_tasks"]
    print(f"基线：{base_counts}；主数据任务清单 {base_tasks} 条")

    # ── W1 预览：映射必须完整可见 ──
    content = build_foreign_workbook()
    code, prev = _post_file("/master/import-preview", content, token)
    ok = code == 200 and prev.get("kind") == "workbook"
    rep.add(ok, "W1a 台账预览端点 200", f"HTTP {code}")
    if not ok:
        return rep.done()
    tables = prev["data"]["tables"]
    exp = tables["experiments"]
    rep.add(exp["columns"].get("name") == "项目名称" and exp["columns"].get("teachers") == "任课教师",
            "W1b 外校列名按别名映射（项目名称→实验项目，任课教师→授课教师）",
            f"columns={exp['columns']}")
    rep.add("编号" in exp["unrecognized"] and "备注" in exp["unrecognized"]
            and "班级数" not in exp["unrecognized"],
            "W1c 未识别列明示（编号/备注不会导入；统计列不算未识别，避免同一列报两次）",
            f"unrecognized={exp['unrecognized']}")
    rep.add("班级数" in exp["stat_dropped"],
            "W1d 统计列被识别并忽略（不当班级名）", f"stat_dropped={exp['stat_dropped']}")
    prev_rows = sum(t.get("rows", 0) for t in tables.values())
    rep.add(prev_rows == 4, "W1e 预览行数汇总 = 4（项目 2 + 教师 1 + 场地 1）",
            f"prev_rows={prev_rows}")
    rep.add(any(MARK_EXP in (s.get("name") or "") for s in exp["samples"]),
            "W1f 样本行含标记项目（与写库同一段行转换代码）",
            f"samples={[s.get('name') for s in exp['samples']]}")

    # ── W2 只读承诺：预览之后主数据一个字节都没变 ──
    after_prev = overview()
    exp_names = {e["name"] for e in after_prev["experiments"]}
    leaked = [m for m in (MARK_EXP, MARK_EXP2) if m in exp_names]
    leaked += [MARK_TEACHER] if MARK_TEACHER in {t["name"] for t in after_prev["teachers"]} else []
    leaked += [MARK_ROOM] if MARK_ROOM in {r["name"] for r in after_prev["rooms"]} else []
    rep.add(not leaked and counts(after_prev) == base_counts,
            "W2 预览后主数据一个字节都没变（只读承诺）",
            f"counts {base_counts} → {counts(after_prev)}；泄漏={leaked or '无'}")

    # ── W3 确认导入：实体真的出现，且行数与预览一致 ──
    code, imp = _post_file("/master/import", content, token)
    imp_rows = sum(s.get("rows", 0) for s in (imp.get("stats") or {}).values())
    rep.add(code == 200, "W3a 确认导入 200", f"HTTP {code} {imp.get('message', '')[:80]}")
    rep.add(imp_rows == prev_rows,
            "W3b 导入行数 == 预览行数（预览没说谎）", f"预览 {prev_rows} vs 导入 {imp_rows}")
    after = overview()
    rep.add(MARK_EXP in {e["name"] for e in after["experiments"]}
            and MARK_TEACHER in {t["name"] for t in after["teachers"]}
            and MARK_ROOM in {r["name"] for r in after["rooms"]},
            "W3c 标记实体（项目/教师/场地）全部写入", f"counts={counts(after)}")

    # ── W4 清理还原 ──
    for path in (f"/master/experiments/{urllib.parse.quote(MARK_EXP)}",
                 f"/master/experiments/{urllib.parse.quote(MARK_EXP2)}",
                 f"/master/teachers/{urllib.parse.quote(MARK_TEACHER)}",
                 f"/master/rooms/{urllib.parse.quote(MARK_ROOM)}"):
        try:
            _delete(path, token)
        except urllib.error.HTTPError as e:
            print(f"  ⚠️ 清理 {path} 失败：HTTP {e.code}")
    final = overview()
    rep.add(counts(final) == base_counts,
            "W4 清理后五张表回到基线（探针不污染后续基线）",
            f"{counts(final)} vs {base_counts}")

    # ── T1 清单预览 ──
    task_content = build_task_list()
    code, tprev = _post_file("/master/tasks-preview", task_content, token)
    d = (tprev or {}).get("data") or {}
    rep.add(code == 200 and d.get("tasks") == 2
            and d.get("columns", {}).get("class_name") == "班级"
            and "备注" in (d.get("unrecognized_columns") or [])
            and d.get("blank_or_incomplete_rows") == 1,
            "T1 清单预览：列名映射/空行计数/未识别列",
            f"HTTP {code} tasks={d.get('tasks')} blank={d.get('blank_or_incomplete_rows')}")

    # ── T2 清单预览同样不写 ──
    st = master_status()
    rep.add(st["master_tasks"] == base_tasks,
            "T2 清单预览后 _master_tasks 不变（只读承诺）",
            f"{base_tasks} → {st['master_tasks']}")

    # ── T3 确认上传（仅基线无清单时才做，不覆盖线上数据） ──
    if base_tasks != 0:
        rep.skip("T3 确认上传后进待排任务",
                 f"服务器上已有 {base_tasks} 条主数据任务清单，跳过写入部分以免覆盖")
    else:
        code, up = _post_file("/upload-master", task_content, token)
        st2 = master_status()
        rep.add(code == 200 and st2["master_tasks"] == 2,
                "T3 确认上传后进入待排任务", f"HTTP {code} master_tasks={st2['master_tasks']}")
        # 还原：只有表头、没有数据行
        _post_file("/upload-master", build_header_only_list(), token)
        st3 = master_status()
        rep.add(st3["master_tasks"] == 0,
                "T3b 空表头文件还原到基线", f"master_tasks={st3['master_tasks']}")

    # ── T4 缺必需列 → 400 说清缺什么 ──
    code, err = _post_file("/master/tasks-preview", build_bad_list(), token)
    detail = str((err or {}).get("detail", ""))
    rep.add(code == 400 and "班级" in detail and "实验项目" in detail,
            "T4 缺「班级/实验项目」列 → 400 且说清缺什么", f"HTTP {code} {detail[:80]}")

    return rep.done()


if __name__ == "__main__":
    sys.exit(main())
