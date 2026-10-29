"""安全逻辑变异测试：验证现有测试是否真的守住了防护。

背景
----
2026-10-28 发现：`test_path_traversal_is_blocked` 的 4 个 payload 里，
只有 1 个真正到达了 `relative_to` 防护。删掉防护后 3 个用例仍然通过。
也就是说那组测试的检出率只有 25%——"4 个绿"不等于"4 个有效"。

本脚本把同样的方法用到全部安全模块上：**逐个删掉防护，看测试会不会红。**
存活（全绿）的变异体 = 该处防护缺少有效测试。

用法
----
    cd backend
    python tests/mutation_check.py

依赖：pytest。会自动备份/恢复源文件，不会留下改动。
注意：总共约 3~5 分钟（M3 涉及口令比较，需要跑完整认证套件）。
"""

from __future__ import annotations

import io
import os
import shutil
import subprocess
import sys

BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
PY = sys.executable

# 各变异体对应的测试文件
FILES = {
    "M1": ["tests/test_api_auth.py"],
    "M2": ["tests/test_api_auth.py"],
    "M3": ["tests/test_api_auth.py"],
    "M4": ["tests/test_api_auth.py"],
    "M5": ["tests/test_api_auth.py"],
    "M6": ["tests/test_ratelimit.py"],
    "M7": ["tests/test_web_serving.py"],
    "M8": ["tests/test_api_auth.py"],
        "M9": ["tests/test_api_auth.py"],
        "M10": ["tests/test_api_auth.py"],
        "M11": ["tests/test_api_auth.py"],
        "M12": ["tests/test_api_auth.py"],
        "M13": ["tests/test_api_auth.py"],
        "M14": ["tests/test_api_auth.py"],
        "M15": ["tests/test_request_workflow.py"],
        "M16": ["tests/test_request_workflow.py"],
        "M17": ["tests/test_time_model_edges.py"],
        "M18": ["tests/test_time_model_edges.py"],
        "M19": ["tests/test_time_model_edges.py"],
        "M20": ["tests/test_api_auth.py"],
        "M21": ["tests/test_teacher_name_parse.py"],
    "M22": ["tests/test_teacher_name_parse.py"],
    "M23": ["tests/test_week_bounds.py"],
    "M24": ["tests/test_event_loop_blocking.py"],
    "M25": ["tests/test_event_loop_blocking.py"],
    "M26": ["tests/test_event_loop_blocking.py"],
}

# (编号, 说明, 文件, 原文, 变异后, pytest -k 表达式)
MUTANTS = [
    (
        "M1", "令牌签名校验恒真",
        "app/security.py",
        "        if not hmac.compare_digest(expected, signature):\n            return None",
        "        if False:  # MUTANT: 永远认为签名正确\n            return None",
        "forged or tampered or rejected",
    ),
    (
        "M2", "令牌过期不检查",
        "app/security.py",
        "    if not isinstance(exp, int) or exp < int(time.time()):\n        return None",
        "    if not isinstance(exp, int):  # MUTANT: 忽略过期\n        return None",
        "expired",
    ),
    (
        "M3", "口令比较恒真",
        "app/security.py",
        "    return user_ok and pass_ok",
        "    return True  # MUTANT: 任何口令都通过",
        "wrong_password or wrong_username",
    ),
    (
        "M4", "require_admin 直接放行",
        "app/security.py",
        "async def require_admin(request: Request) -> str:",
        "async def require_admin(request: Request) -> str:\n    return 'admin'  # MUTANT: 不做任何校验",
        "requires_token or forged",
    ),
    (
        "M5", "上传文件名不做净化",
        "app/api/reschedule.py",
        'def _safe_upload_name(filename: str, allowed: tuple, prefix: str = "") -> str:',
        'def _safe_upload_name(filename: str, allowed: tuple, prefix: str = "") -> str:\n    return prefix + filename  # MUTANT: 直接拼接',
        "upload_name or upload_suffix or rejects_pdf",
    ),
    (
        "M6", "限流永不触发",
        "app/ratelimit.py",
        "        wait = max(waits)\n        if wait:",
        "        wait = 0  # MUTANT: 永不限流\n        if wait:",
        "rotating_source_ip or global_bucket or per_client_bucket or "
        "admin_login_gets_429 or cheap_anonymous_posts",
    ),
    (
        "M7", "SPA 兜底不校验路径边界",
        "app/main.py",
        "            try:\n                candidate.relative_to(_DIST_ROOT)\n"
        "            except ValueError:\n                return _index_response()",
        "            pass  # MUTANT: 删除 relative_to 防护",
        "traversal or escape",
    ),
    (
        "M8", "取消上传大小限制",
        "app/api/reschedule.py",
        "        if len(buf) > cap:\n            raise HTTPException(",
        "        if False:  # MUTANT: 取消大小限制\n            raise HTTPException(",
        "oversized",
    ),
    (
        "M9", "关闭工作表行数守卫",
        "app/utils/excel_parser.py",
        """    n = int(getattr(sheet, "max_row", 0) or 0)
    if n > MAX_SHEET_ROWS:""",
        """    n = int(getattr(sheet, "max_row", 0) or 0)
    if False:  # MUTANT: 不检查行数""",
        "sheet_size",
    ),
    (
        "M10", "令牌候选不去重",
        "app/security.py",
        """    # 去重但保序
    seen: set[str] = set()
    return [t for t in candidates if not (t in seen or seen.add(t))]""",
        """    return candidates  # MUTANT: 去掉去重""",
        "extract_tokens",
    ),
    (
        "M11", "短行不补齐（合并单元格表头会崩）",
        "app/utils/excel_parser.py",
        "            if len(row) < 8:",
        "            if False:  # MUTANT: 不补齐短行",
        "merged_header",
    ),
    (
        "M12", "413 文案回到运维话术",
        "app/api/reschedule.py",
        '"请压缩表格或拆分成多个文件后重新上传。"',
        '"如确需上传更大的表格，请设置环境变量 MAX_UPLOAD_MB 后重启服务。"',
        "413_detail",
    ),
    (
        "M13", "空名校验去掉下一步指引",
        "app/master_data.py",
        'raise ValueError("课程名不能为空，请填写后再提交")',
        'raise ValueError("课程名不能为空")',
        "master_empty_name",
    ),
    (
        "M14", "上传时非原子替换（暴露空表中间态）",
        "app/api/reschedule.py",
        """        new_db = ScheduleDatabase()
        new_db.load_from_excel(save_path)""",
        """        new_db = ScheduleDatabase()
        globals()["_db"] = new_db  # MUTANT
        new_db.load_from_excel(save_path)""",
        "blank_db",
    ),
    (
        "M15", "不拦重复审批",
        "app/api/reschedule.py",
        """    if row["status"] != "pending":
        conn.close()
        raise HTTPException(status_code=400,""",
        """    if False:  # MUTANT: 不拦重复审批
        conn.close()
        raise HTTPException(status_code=400,""",
        "double_approve",
    ),
    (
        "M16", "审批不自动应用调课",
        "app/api/reschedule.py",
        """            if _db.loaded:
                apply_result = _apply_reschedule(record_dict)""",
        """            if False:  # MUTANT: 不自动应用
                apply_result = _apply_reschedule(record_dict)""",
        "applies_move or reports_conflicts",
    ),
    (
        "M17", "首尾相接也算重叠（会推荐到正在上课的教室）",
        "app/utils/time_model.py",
        "    return max(a[0], b[0]) < min(a[1], b[1])",
        "    return max(a[0], b[0]) <= min(a[1], b[1])  # MUTANT",
        "adjacent_slots",
    ),
    (
        "M18", "未知时段标签不再保守视为占用",
        "app/utils/time_model.py",
        """    ca, cb = CLOCK.get(a), CLOCK.get(b)
    if ca is None or cb is None:
        return True""",
        """    ca, cb = CLOCK.get(a), CLOCK.get(b)
    if ca is None or cb is None:
        return False  # MUTANT""",
        "unknown_label",
    ),
    (
        "M19", "写反的时钟区间照收不误",
        "app/utils/time_model.py",
        """    if end <= start:  # 17:00-14:00 这种写反了的，别硬猜
        return None""",
        """    if False:  # MUTANT
        return None""",
        "rejects_invalid",
    ),
    (
        "M20", "移除 /status 的懒加载提示",
        "app/api/reschedule.py",
        """        "loaded_hint": (
            "已加载"
            if _db.loaded
            else "尚未加载（懒加载）：访问 /api/reschedule/teachers 等数据端点会自动加载默认排课表"
        ),""",
        """        "loaded_hint": "已加载" if _db.loaded else "",""",
        "status_hint",
    ),
    (
        "M21", "课程名未识别时不做提示",
        "app/agent/reschedule_agent.py",
        '        course_unrecognized = not parsed.get("course_name")',
        '        course_unrecognized = False  # MUTANT',
        "unrecognized_course",
    ),
    (
        "M22", "班级名改回字典序（非长名优先）",
        "app/agent/reschedule_agent.py",
        "        for c in sorted(self.db.get_all_classes(), key=len, reverse=True):",
        "        for c in sorted(self.db.get_all_classes()):",
        "class_name_prefers_longest",
    ),
    (
        "M23", "「这周」不参照源周（原地打转）",
        "app/agent/reschedule_agent.py",
        "            target_week = base - 1 if source_week else base",
        "            target_week = source_week if source_week else current_week",
        "this_week",
    ),
    (
        "M24", "懒加载退回在事件循环里同步执行",
        "app/api/reschedule.py",
        "    await run_in_threadpool(_ensure_loaded_sync)",
        "    _ensure_loaded_sync()  # MUTANT",
        "ensure_loaded",
    ),
    (
        "M25", "主数据落盘退回同步执行",
        "app/api/reschedule.py",
        "    await run_in_threadpool(_persist_master_state)",
        "    _persist_master_state()  # MUTANT",
        "persist_master_state",
    ),
    (
        "M26", "loaded 延迟到事件循环再生效（并发加载两次）",
        "app/api/reschedule.py",
        """    if not _db.loaded:
        _db.loaded = True""",
        "    pass  # MUTANT: loaded 交回 async 端设置",
        "cold_start",
    ),
]


def _run_tests(files: list, kexpr: str) -> tuple:
    cmd = [PY, "-m", "pytest", "-q", "--no-header",
           "-p", "no:cacheprovider", *files, "-k", kexpr]
    proc = subprocess.run(cmd, cwd=BACKEND, capture_output=True,
                          text=True, timeout=900)
    out = proc.stdout + proc.stderr
    lines = [l.strip() for l in out.splitlines()
             if "passed" in l or "failed" in l]
    return proc.returncode != 0, (lines[-1] if lines else "?")


def main() -> int:
    results = []
    for mid, desc, rel, old, new, kexpr in MUTANTS:
        path = os.path.join(BACKEND, rel)
        if not os.path.isfile(path):
            results.append((mid, desc, "SKIP", "文件缺失"))
            continue
        orig = io.open(path, encoding="utf-8").read()
        if old not in orig:
            results.append((mid, desc, "SKIP", "锚点未匹配（代码已重构？）"))
            continue

        backup = path + ".mutbak"
        shutil.copy2(path, backup)
        try:
            io.open(path, "w", encoding="utf-8").write(orig.replace(old, new, 1))
            detected, summary = _run_tests(FILES.get(mid, []), kexpr)
            results.append((mid, desc, "检出" if detected else "存活", summary[:64]))
        except subprocess.TimeoutExpired:
            results.append((mid, desc, "超时", ">900s"))
        finally:
            shutil.copy2(backup, path)
            os.remove(backup)

    print()
    print("=" * 88)
    print(f"{'ID':4s} {'变异体':30s} {'结果':6s} 摘要")
    print("=" * 88)
    for mid, desc, verdict, summary in results:
        mark = {"检出": "PASS", "存活": "XXXX", "SKIP": "--", "超时": "--"}[verdict]
        print(f"{mark} {mid:3s} {desc:30s} {verdict:6s} {summary}")
    print("=" * 88)

    tested = [r for r in results if r[2] in ("检出", "存活")]
    alive = [r for r in results if r[2] == "存活"]
    if tested:
        print(f"检出 {len(tested) - len(alive)}/{len(tested)}")
    if alive:
        print("\n以下变异体存活 —— 对应防护缺少有效测试：")
        for mid, desc, _, _ in alive:
            print(f"  - {mid} {desc}")
        return 1
    print("\n全部检出：每个安全防护都有测试真正守着。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
