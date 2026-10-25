"""演示数据一键准备脚本

功能：
1. 启动后端（若未运行）
2. 加载默认实验安排表（433条）
3. 预置一条演示调课申请单（待审批，便于演示审批流程）

用法：
    python scripts/prepare_demo.py
"""

import os
import re
import sys
import time

import httpx

BASE_URL = os.environ.get("DEMO_BASE_URL", "http://127.0.0.1:8023")

# 项目根目录（scripts/ 的上一级）
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def ensure_backend():
    """检查后端是否运行，未运行则启动"""
    try:
        r = httpx.get(f"{BASE_URL}/health", timeout=2)
        if r.status_code == 200:
            print("✅ 后端已在运行")
            return
    except Exception:
        pass

    print("⏳ 后端未运行，正在启动...")
    # 启动后端（后台进程）
    backend_dir = os.path.join(ROOT, "backend")
    import subprocess
    subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8023"],
        cwd=backend_dir,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )
    # 等待后端就绪
    for _ in range(15):
        time.sleep(1)
        try:
            r = httpx.get(f"{BASE_URL}/health", timeout=2)
            if r.status_code == 200:
                print("✅ 后端启动成功")
                return
        except Exception:
            continue
    print("❌ 后端启动超时，请手动启动")


def load_default_schedule():
    """加载默认实验安排表"""
    r = httpx.post(f"{BASE_URL}/api/reschedule/load-default", timeout=10)
    if r.status_code == 200:
        data = r.json()
        print(f"✅ 加载实验安排表: {data.get('message')}")
        return True
    print(f"❌ 加载失败: {r.text[:100]}")
    return False


def _admin_token():
    """/requests 是管理员接口（_ADMIN_ONLY）——不带令牌就是 401，演示数据造不出来。"""
    r = httpx.post(f"{BASE_URL}/api/reschedule/admin-login",
                   json={"username": "admin", "password": "admin123"}, timeout=10)
    r.raise_for_status()
    return r.json()["token"]


def seed_demo_request(reset: bool = True):
    """从**真实课程数据**造 3 条待审批工单。

    为什么不能写死：审批通过会调 `/apply-move` 按 (教师/课程/班级/原时间/目标周)
    去匹配排课行。手写的班级名（如"2024级信息安全+海洋资源与环境"）在真实表里
    对不上（真名是"和"），于是"同意"之后课表一条都没动 —— 台上最尴尬的一屏。
    这里取真实教师的可调课程，原样带回 original_time。
    """
    token = _admin_token()
    if reset:
        # 演示复位：清申请 + 撤销上一场"已应用"的调课（否则下一场带着旧改动开局）
        try:
            r = httpx.post(f"{BASE_URL}/api/reschedule/demo/reset", timeout=60,
                           headers={"Authorization": f"Bearer {token}"})
            print("🧹 " + (r.json().get("message") or "演示已复位"))
        except Exception as e:
            print(f"⚠️ 演示复位失败（DEMO_MODE=1 才可用）：{e}")

    # 借审批探针的"找可移动周次"（沙盘判可用）：否则目标周撞车，点"同意"只会
    # 弹出"目标周已有安排，未自动移动"——演示要的是"同意 → 课表真的变了"。
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import probe_approval_chain as pa

    teachers = [t["name"] for t in
                (httpx.get(f"{BASE_URL}/api/reschedule/teachers", timeout=20).json().get("teachers") or [])]

    def _reset():
        httpx.post(f"{BASE_URL}/api/reschedule/demo/reset", timeout=60,
                   headers={"Authorization": f"Bearer {token}"})

    def _create(payload):
        return httpx.post(f"{BASE_URL}/api/reschedule/requests", json=payload, timeout=10,
                          headers={"Authorization": f"Bearer {token}"})

    # 候选：每位老师挑一门"沙盘说能移"的可调课程
    candidates = []
    for name in teachers:
        if len(candidates) >= 8:
            break
        try:
            courses = httpx.get(f"{BASE_URL}/api/reschedule/teachers/{name}/courses",
                                timeout=30).json().get("courses") or []
        except Exception:
            continue
        for cand in courses:
            if not (cand.get("adjustable") and cand.get("original_time") and cand.get("room")):
                continue
            week, _slot = pa.find_movable_week(BASE_URL, name, cand)
            if week is None:
                continue
            candidates.append({
                "teacher": name,
                "course_name": cand.get("course_name") or "大学物理实验",
                "class_name": cand.get("class_name") or "",
                "original_time": cand["original_time"],
                "target_week": week,
                "reason": "演示：本周有会议/设备维护，申请调至下一周",
            })
            break

    # 逐条**真批一次**验证"同意后课表真的会动"，通过的才留下 —— 台上不能点出
    # "目标周已有安排，未移动"。验证用的是同一套接口，验证后复位不会留痕。
    verified = []
    for payload in candidates:
        r = _create(payload)
        if r.status_code != 200:
            continue
        rid = r.json()["request"]["id"]
        ap = httpx.post(f"{BASE_URL}/api/reschedule/requests/{rid}/approve",
                        json={"approve": True, "comment": "演示预检"},
                        headers={"Authorization": f"Bearer {token}"}, timeout=60)
        msg = (ap.json() or {}).get("message") or ""
        ok = "自动应用调课" in msg
        _reset()
        if ok:
            verified.append(payload)
            print(f"  ✓ 已验证可落地：{payload['teacher']} {payload['class_name']} → 第{payload['target_week']}周")
        if len(verified) >= 3:
            break

    # 用验证通过的候选重建"待审批"工单（不审批）
    created = 0
    for payload in verified:
        r = _create(payload)
        if r.status_code == 200:
            created += 1
            print(f"✅ 演示工单 #{r.json()['request']['id']}：{payload['teacher']} "
                  f"{payload['class_name']} {payload['original_time']} → 第{payload['target_week']}周")

    if created == 0:
        print("⚠️ 没有造出工单（可能没有可调课程）")
    return created > 0


def main():
    print("=" * 50)
    print("演示数据一键准备")
    print("=" * 50)

    ensure_backend()
    load_default_schedule()
    seed_demo_request()

    print()
    print("=" * 50)
    print("演示准备完成！")
    print("  前端: http://localhost:5173")
    print(f"  后端: {BASE_URL}")
    print("  演示路径：身份选择 → 我的课程 → 调课 → 审批台")
    print("=" * 50)


if __name__ == "__main__":
    main()
