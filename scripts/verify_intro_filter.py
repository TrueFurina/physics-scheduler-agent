"""灰箱排课的绪论/考试过滤 —— 真实进程端到端核查（可重复，防回归）。

背景（R-58）
------------
绪论/考试类项目登记进主数据（台账可见、可指定课程），但**绝不进求解器**
（R-48：绪论课已单独安排；考试类时间与场地逻辑不同）。防线在
`_raw_schedulable_tasks`：pending + 上传清单 + 主数据派生统一走
`is_schedulable_project` 过滤。

本脚本走真实 HTTP 验证这条防线端到端成立（单测只盖函数本身）：
    1. 上传含 1 正常 + 1 绪论 + 1 考试 的清单 → 读入 3 条；
    2. /parallel/overview 的 pending_total 必须 = 1，且总览单元无绪论/考试；
    3. /parallel/solve 实际求解 → 排出的项目里不得出现绪论/考试；
    4. 空清单重置 → pending_total 归 0（清理自身产物）。

用法
----
    D:/miniconda3_new/python.exe scripts/verify_intro_filter.py          # 默认本机
    D:/miniconda3_new/python.exe scripts/verify_intro_filter.py --site https://...  # 打线上（只读+自清理）
前置：后端已启动且 load-default 完成。退出码 0=全过，1=有核查不过。
"""

import os
import sys
import tempfile

import openpyxl

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from verify_master_roundtrip import _get, _post_file, _post_json

SITE = "http://127.0.0.1:8023"  # 仅作默认值展示；实际目标由 base.API 控制

CLS = "灰箱过滤核查班（可删）"
OK_EXP = "扭摆法"
BAD_ROWS = [
    ("绪论课（教室自己申请）", "绪论"),
    ("大物实验B考试", "考试"),
]
TMP = os.path.join(tempfile.gettempdir(), "intro_filter_list.xlsx")

failures = []


def check(name, ok, detail=""):
    print(f"  {'✅' if ok else '❌'} {name}" + (f" —— {detail}" if detail else ""))
    if not ok:
        failures.append(name)


def make_list(rows):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "项目清单"
    ws.append(["班级", "实验项目", "学生组"])
    for exp, _ in rows:
        ws.append([CLS, exp, "1组"])
    wb.save(TMP)


def upload(token, rows, label):
    make_list(rows)
    with open(TMP, "rb") as f:
        resp = _post_file("/upload-master", os.path.basename(TMP), f.read(), token)
    print(f"  ↳ 上传{label}：读入 {resp.get('total_tasks')} 条")


def schedulable_units(ov):
    units = []
    for course in ov.get("courses", []):
        for u in course.get("units") or []:
            units.append(str(u.get("experiment") or ""))
    return units


def main():
    import argparse

    import verify_master_roundtrip as base

    ap = argparse.ArgumentParser(description="绪论/考试类不进求解器 —— 真实进程 E2E")
    ap.add_argument("--site", default=base.SITE, help="后端地址（默认本机 8023）")
    args = ap.parse_args()
    # _get/_post_file/_post_json 都读 base.API，改它就是改全部请求目标
    base.API = f"{args.site.rstrip('/')}/api/reschedule"

    token = _post_json("/admin-login", {"username": "admin", "password": "admin123"})["token"]

    print("\n[1] 上传 1 正常 + 2 绪论/考试")
    rows = [(OK_EXP, "ok")] + BAD_ROWS
    upload(token, rows, "混合清单")

    print("\n[2] /parallel/overview 可排任务过滤")
    ov = _get("/parallel/overview")["data"]
    check("pending_total == 1（绪论/考试被滤掉）", ov.get("pending_total") == 1,
          f"实际 {ov.get('pending_total')}")
    units = schedulable_units(ov)
    check("总览单元无绪论", not any("绪论" in u for u in units), f"单元 {units[:6]}")
    check("总览单元无考试", not any("考试" in u for u in units))

    print("\n[3] /parallel/solve 实际求解")
    solve = _post_json("/parallel/solve", {"class_name": CLS})
    sched = (solve.get("data") or {}).get("schedule") or solve.get("schedule") or []
    names = sorted({str(s.get("project") or "") for s in sched})
    check("求解成功", bool(solve.get("success")), f"{(solve.get('message') or '')[:60]}")
    check("排出的项目不含绪论/考试",
          not any(("绪论" in n or "考试" in n) for n in names), f"项目 {names}")
    check("正常项目被排上", OK_EXP in names)

    print("\n[4] 清理：空清单重置")
    upload(token, [], "空清单")
    ov = _get("/parallel/overview")["data"]
    check("pending_total 归 0", ov.get("pending_total") == 0,
          f"实际 {ov.get('pending_total')}")
    if os.path.exists(TMP):
        os.remove(TMP)

    print()
    if failures:
        print(f"No-Go —— {len(failures)} 项不过：{failures}")
        return 1
    print("Go —— 绪论/考试类绝不进求解器（端到端）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
