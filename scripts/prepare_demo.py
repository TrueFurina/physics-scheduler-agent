"""演示数据一键准备脚本

功能：
1. 启动后端（若未运行）
2. 加载默认实验安排表（433条）
3. 预置一条演示调课申请单（待审批，便于演示审批流程）

用法：
    python scripts/prepare_demo.py
"""

import os
import sys
import time

import httpx

BASE_URL = os.environ.get("DEMO_BASE_URL", "http://127.0.0.1:8000")

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
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"],
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


def seed_demo_request():
    """预置演示调课申请单（闽江大学真实教师场景）"""
    demo_requests = [
        {
            "teacher": "吴琳",
            "course_name": "大学物理实验C",
            "class_name": "2024级信息安全+海洋资源与环境",
            "original_time": "周四 13:30-15:45 · A504",
            "target_week": 7,
            "reason": "演示：周四下午有会议，需调课",
        },
        {
            "teacher": "林珠云",
            "course_name": "大学物理实验B",
            "class_name": "2024级电子科学与技术",
            "original_time": "周五 13:30-15:45 · A513",
            "target_week": 8,
            "reason": "演示：外出参加教学研讨会",
        },
        {
            "teacher": "郑辉",
            "course_name": "大学物理实验C",
            "class_name": "2024级应用化学1、2班",
            "original_time": "周三 15:45-18:00 · A410",
            "target_week": 9,
            "reason": "演示：实验室设备维护，需错峰",
        },
    ]

    created = 0
    for req in demo_requests:
        r = httpx.post(f"{BASE_URL}/api/reschedule/requests", json=req, timeout=10)
        if r.status_code == 200:
            created += 1
            data = r.json()
            print(f"✅ 预置演示申请单: #{data.get('request', {}).get('id')} "
                  f"{req['teacher']} {req['course_name']} → 第{req['target_week']}周（待审批）")

    if created == 0:
        print("⚠️ 预置申请单失败（可能已有数据）")
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
