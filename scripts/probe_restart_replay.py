"""闭环探针：写进安排表的改动，**换一个进程**还认不认（真重启，不是同进程重载）。

为什么必须真重启
----------------
R-68-A 的教训：主数据被一份脏落盘清空，内存态一切正常，**只有重启后才暴露**。
现有 `test_landed_plan_survives_simulated_restart` 是"同进程再 load 一次"，
走的是同一段重放代码，但测不出"进程级"的初始化顺序问题（比如落盘恢复把重放覆盖）。
所以这里起两个独立的 uvicorn 进程，中间把第一个杀掉。

判据（一条，够硬）
------------------
进程 B 看到的周次，必须等于进程 A 写完的周次。不等 = "审批通过"在重启后蒸发。

安全
----
全程只动 `requests.db` 里的 applied_moves 一行，并且**开工前备份、收工后还原**
（`finally` 里做，异常也还原）。不碰 schedule.xlsx。
"""
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(REPO, "backend", "app", "data", "requests.db")
BACKUP = os.path.join(REPO, ".tmp", "requests.db.restart-backup")
PY = sys.executable
ADMIN = {"username": "admin", "password": "admin123"}


def http(site, method, path, payload=None, token=""):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(site + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")


def start(port):
    env = dict(os.environ)
    env.update({"DEMO_MODE": "1", "ADMIN_PASS": "admin123",
                "OPENBLAS_NUM_THREADS": "1", "SCHEDULE_WARMUP": "0"})
    proc = subprocess.Popen(
        [PY, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port),
         "--log-level", "warning"],
        cwd=os.path.join(REPO, "backend"), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    site = f"http://127.0.0.1:{port}"
    for _ in range(120):
        try:
            code, _ = http(site, "GET", "/health")
            if code == 200:
                return proc, site
        except Exception:
            pass
        time.sleep(0.5)
    proc.kill()
    raise SystemExit(f"服务没起来：{port}")


def stop(proc):
    proc.terminate()
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()


def pick_course(site):
    """找一个"可调 + 有原时间 + 有教室"的真实课程。"""
    _, t = http(site, "GET", "/api/reschedule/teachers")
    for teacher in t.get("teachers") or []:
        name = teacher.get("name") or teacher.get("teacher_name")
        if not name:
            continue
        _, c = http(site, "GET", f"/api/reschedule/teachers/{urllib.parse.quote(name)}/courses")
        for course in c.get("courses") or []:
            if course.get("adjustable") and course.get("original_time") and course.get("room"):
                return name, course
    return None, None


def week_range(site, teacher, course_name):
    _, c = http(site, "GET", f"/api/reschedule/teachers/{urllib.parse.quote(teacher)}/courses")
    for course in c.get("courses") or []:
        if course.get("course_name") == course_name:
            return course.get("week_range")
    return None


def main():
    os.makedirs(os.path.dirname(BACKUP), exist_ok=True)
    shutil.copy2(DB, BACKUP)
    print(f"已备份 requests.db → {os.path.relpath(BACKUP, REPO)}")
    ok = True
    try:
        # ---- 进程 A：写 ----
        proc, site = start(8028)
        try:
            http(site, "POST", "/api/reschedule/load-default")
            _, login = http(site, "POST", "/api/reschedule/admin-login", ADMIN)
            token = login.get("token") or ""
            assert token, "拿不到管理员令牌"

            teacher, course = pick_course(site)
            assert course, "没有可调课程"
            name = course["course_name"]
            before = week_range(site, teacher, name)
            nums = [int(x) for x in __import__("re").findall(r"\d+", before or "1")]
            base_week = max(nums)
            # 目标周不能随手 +1：可能撞场地/教师。逐个试，直到真的写进去（applied>0）
            code, applied, target = 0, {}, None
            for t in [w for w in range(1, 17) if w != base_week]:
                rec = {"teacher": teacher, "course_name": name,
                       "class_name": course.get("class_name", ""),
                       "original_time": course["original_time"],
                       "target_week": t, "key": f"restart-probe-{teacher}-{name}-{t}"}
                code, applied = http(site, "POST", "/api/reschedule/apply-move", rec, token)
                if (applied.get("data") or {}).get("applied"):
                    target = t
                    break
            after = week_range(site, teacher, name)
            print(f"进程A：{teacher} / {name}  {before} → {after}  "
                  f"(目标第 {target} 周, HTTP {code}, "
                  f"applied={(applied.get('data') or {}).get('applied')})")
            ok &= code == 200 and after != before
        finally:
            stop(proc)

        # ---- 进程 B：全新进程，只看重放 ----
        proc, site = start(8029)
        try:
            http(site, "POST", "/api/reschedule/load-default")
            after_restart = week_range(site, teacher, name)
            print(f"进程B（全新进程）：{teacher} / {name} 周次 = {after_restart}")
            same = after_restart == after
            ok &= same
            print(("✅ 重启后仍在" if same else "❌ 重启后丢了") +
                  f"（期望 {after}，实际 {after_restart}）")
        finally:
            stop(proc)
    finally:
        shutil.copy2(BACKUP, DB)
        os.remove(BACKUP)
        print("已还原 requests.db")
    print("\n结论：", "闭环成立" if ok else "闭环断了")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
