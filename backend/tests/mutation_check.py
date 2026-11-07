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
import re
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
    "M27": ["tests/test_event_loop_blocking.py"],
    "M28": ["tests/test_event_loop_blocking.py"],
    "M29": ["tests/test_api_auth.py"],
    "M30": ["tests/test_response_contract.py"],
    "M31": ["tests/test_response_contract.py"],
    "M32": ["tests/test_weekend_coverage.py"],
    "M33": ["tests/test_scheduler_scope.py"],
    "M34": ["tests/test_master_delete.py"],
    "M35": ["tests/test_master_consistency.py"],
    "M36": ["tests/test_master_derived.py"],
    # --- R70 补齐：以下 11 个变异体此前没有指定文件，实际是"跑全量 + -k 过滤"，
    # 慢，而且 -k 匹配不到用例时 pytest 退出码 5 也会被当成"检出"（假阳性）。
    "M37": ["tests/test_master_derived.py", "tests/test_master_data.py"],
    "M38": ["tests/test_import_wizard.py"],
    "M39": ["tests/test_remote_drift.py", "tests/test_safe_push.py"],
    "M40": ["tests/test_remote_drift.py"],
    "M41": ["tests/test_env_order.py", "tests/test_admin_password_guard.py"],
    "M42": ["tests/test_chunk_backfill.py"],
    "M43": ["tests/test_land_rollback.py"],
    "M44": ["tests/test_master_store_recovery.py"],
    "M45": ["tests/test_week_basis.py", "tests/test_schedule_failure_message.py"],
    "M46": ["tests/test_enrich_graybox.py"],
    "M47": ["tests/test_import_drift.py"],
    "M48": ["tests/test_impact_analysis.py"],
    "M49": ["tests/test_land_undo_atomicity.py"],
    "M50": ["tests/test_enrich_graybox.py"],
    "M51": ["tests/test_teacher_course_attribution.py"],
    "M52": ["tests/test_teacher_course_attribution.py"],
    "M53": ["tests/test_import_wizard.py"],
    "M54": ["tests/test_import_wizard.py"],
    "M55": ["tests/test_replay_outer_guard.py"],
    "M56": ["tests/test_teacher_roles_retry.py"],
    # R81：审批台三处静默错误的守卫，测试集中在同一个文件里。
    "M57": ["tests/test_request_approval_integrity.py"],
    "M58": ["tests/test_request_approval_integrity.py"],
    "M59": ["tests/test_request_approval_integrity.py"],
    "M60": ["tests/test_request_approval_integrity.py"],
    # R82：/apply-move 重复提交的诚实性与「不带 key 也要幂等」。
    "M61": ["tests/test_apply_move_honesty.py"],
    "M62": ["tests/test_apply_move_honesty.py"],
    "M63": ["tests/test_apply_move_honesty.py"],
    "M64": ["tests/test_apply_move_honesty.py"],
    "M65": ["tests/test_apply_move_honesty.py", "tests/test_approval_idempotency.py"],
    # R83：两条闭环共用幂等键 + 审批认 already_applied。
    "M66": ["tests/test_approval_idempotency.py"],
    "M67": ["tests/test_approval_idempotency.py"],
    "M68": ["tests/test_approval_idempotency.py", "tests/test_apply_move_honesty.py"],
    "M69": ["tests/test_approval_idempotency.py", "tests/test_apply_move_honesty.py"],
    "M70": ["tests/test_approval_idempotency.py"],
    "M71": ["tests/test_approval_idempotency.py"],
    # R84：升级前空键账本行的按内容回认。
    "M72": ["tests/test_legacy_ledger_idempotency.py"],
    "M73": ["tests/test_legacy_ledger_idempotency.py"],
    "M74": ["tests/test_legacy_ledger_idempotency.py"],
    "M75": ["tests/test_legacy_ledger_idempotency.py"],
    "M76": ["tests/test_legacy_ledger_idempotency.py"],
    "M77": ["tests/test_legacy_ledger_idempotency.py"],
    # R85：审批自动应用失败的结构化信号与诚实文案。
    "M78": ["tests/test_approval_apply_failure.py"],
    "M79": ["tests/test_approval_apply_failure.py"],
    "M80": ["tests/test_approval_apply_failure.py"],
    "M81": ["tests/test_approval_apply_failure.py"],
    "M82": ["tests/test_approval_apply_failure.py"],
    "M83": ["tests/test_approval_apply_failure.py"],
    "M84": ["tests/test_approval_apply_failure.py"],
    "M85": ["tests/test_approval_apply_failure.py"],
    # R86：/load-default 的结构化降级信号（后端部分；前端接线由 vitest 源码守卫守）。
    "M86": ["tests/test_load_default_degraded.py"],
    "M87": ["tests/test_load_default_degraded.py"],
    "M88": ["tests/test_load_default_degraded.py"],
    "M89": ["tests/test_load_default_degraded.py"],
    "M90": ["tests/test_load_default_degraded.py"],
    "M91": ["tests/test_load_default_degraded.py"],
    "M92": ["tests/test_load_default_degraded.py"],
    "M93": ["tests/test_load_default_degraded.py"],
    # R87：主数据落盘失败的结构化信号（前端接线由 vitest 纯函数 + 渲染用例守）。
    "M94": ["tests/test_master_persist_failure.py"],
    "M95": ["tests/test_master_persist_failure.py"],
    "M96": ["tests/test_master_persist_failure.py"],
    "M97": ["tests/test_master_persist_failure.py"],
    "M98": ["tests/test_master_persist_failure.py"],
    "M99": ["tests/test_master_persist_failure.py"],
    "M100": ["tests/test_master_persist_failure.py"],
    "M101": ["tests/test_master_persist_failure.py"],
    "M102": ["tests/test_master_persist_failure.py"],
    "M103": ["tests/test_master_restore_degraded.py"],
    "M104": ["tests/test_master_restore_degraded.py"],
    "M105": ["tests/test_master_restore_degraded.py"],
    "M106": ["tests/test_master_restore_degraded.py"],
    "M107": ["tests/test_master_restore_degraded.py"],
    "M108": ["tests/test_master_restore_degraded.py"],
    "M109": ["tests/test_master_restore_degraded.py"],
    "M110": ["tests/test_master_restore_degraded.py"],
    "M111": ["tests/test_teacher_course_attribution.py"],
    "M112": ["tests/test_teacher_course_attribution.py"],
    "M113": ["tests/test_export_warmup_state.py"],
    "M114": ["tests/test_export_warmup_state.py"],
    "M115": ["tests/test_master_restore_warning.py"],
    "M116": ["tests/test_master_restore_warning.py"],
    "M117": ["tests/test_master_restore_warning.py"],
    "M118": ["tests/test_master_restore_warning.py"],
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
        new_db.load_from_excel(save_path)      # 225ms，锁外做，不占写锁""",
        """        new_db = ScheduleDatabase()
        globals()["_db"] = new_db  # MUTANT
        new_db.load_from_excel(save_path)""",
        "blank_db",
    ),
    (
        "M15", "不拦重复审批",
        # R80：工单族搬到 app/api/requests_board.py，锚点原文照搬、靶文件跟着改。
        "app/api/requests_board.py",
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
        # R80：靶文件随工单族搬家；两处状态改 `_api.xxx` 现取，锚点同步改写。
        "app/api/requests_board.py",
        # R83：审批调用改成三参数（带共用幂等键），锚点跟着换。
        """                def _locked_apply_approve():
                    with _api._DB_WRITE_LOCK:
                        # R83：演示模式审批也必须带幂等键。原先只传 record_dict，
                        # `idem_key` 落成 ''，而库层唯一索引刻意放过空串 ——
                        # 两张内容相同的工单先后批准时第二条会去撬更早一周，
                        # 也认不出 /apply-move 已经写下的 auto: 账。
                        # 用共用 helper 按调课六字段算键，两条闭环互相认账；
                        # 状态一律 `_api.xxx` 现取，别在模块里绑值副本。
                        return _api._apply_reschedule(
                            record_dict, True, _api._resolve_idem_key(record_dict))
                apply_result = await run_in_threadpool(_locked_apply_approve)""",
        """                apply_result = {"applied": 0, "skipped": 0, "details": []}""",
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
        """            base = source_week if source_week is not None else current_week\n            if base is None:\n                target_week = None          # 无基准 → 不猜\n            else:\n                target_week = base - 1 if source_week is not None else base""",
        """            base = source_week if source_week is not None else current_week\n            target_week = base  # MUTANT: 原地打转，不往前挪""",
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
        "    return await run_in_threadpool(_persist_master_state)",
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
    (
        "M27", "调课写路径不拿写锁",
        # R82：`req.key or ""` 改成端点层算好的 `key`（内容指纹），
        # 锚点必须跟着改 —— 失配只会静默 SKIP，等于这道锁没人守。
        "app/api/reschedule.py",
        "    def _locked_apply():\n        with _DB_WRITE_LOCK:\n            return _apply_reschedule(record, True, key)",
        "    def _locked_apply():\n        return _apply_reschedule(record, True, key)",
        "holds_write_lock",
    ),
    (
        "M28", "换表不持写锁（幽灵落地记录）",
        "app/api/reschedule.py",
        """        with _DB_WRITE_LOCK:
            _db = new_db
            # 换了安排表 → 旧表上的主数据落盘全部作废（班级/项目可能已不存在），按新表反推
            _schedule_fp = _ms.schedule_fingerprint(save_path)
            _master_data.__init__()      # 回到未初始化态，下次访问按新表反推
            _master_tasks.clear()
            _ms.clear()
            # 换了安排表 → 旧表上的落地方案一并作废：方案里的单元在新表可能根本不
            # 存在，重放它等于把旧方案强加到新表上。与 master_store 同一失效规则。
            discarded = _landings().clear()""",
        """        _db = new_db  # MUTANT: 整段脱锁
        _schedule_fp = _ms.schedule_fingerprint(save_path)
        _master_data.__init__()
        _master_tasks.clear()
        _ms.clear()
        discarded = _landings().clear()""",
        "swap_section or share_a_lock",
    ),
    (
        "M29", "API 层不透传 course_unrecognized",
        "app/api/reschedule.py",
        '            "course_unrecognized": result.get("course_unrecognized", False),',
        '            # MUTANT: 不透传',
        "course_fields or unrecognized_course",
    ),
    (
        "M30", "追问分支少返回契约字段",
        "app/agent/reschedule_agent.py",
        # R49：锚点补上后来新增的 `ai_plans_source` —— 少了这一行锚点就整段失配，
        # 变异体被静默 SKIP（`mutation_check.py` 的退出码不看 SKIP），
        # 于是"追问分支少返回契约字段"这条一直在报"全检出"，其实什么都没验。
        """            "course_name": parsed.get("course_name"),
            "course_unrecognized": not parsed.get("course_name"),
            "ai_plans": [],
            "ai_plans_source": "none",
            "total": 0,
        }""",
        """        }  # MUTANT: 追问分支退回到 8 键""",
        "identical_keys or contract_keys",
    ),
    (
        "M31", "course_unrecognized 与 course_name 脱钩",
        "app/agent/reschedule_agent.py",
        """            "course_unrecognized": course_unrecognized,""",
        """            "course_unrecognized": False,  # MUTANT""",
        "contradicts",
    ),
    (
        "M32", "沙盘退回只排 5 天（周末消失）",
        "app/agent/reschedule_agent.py",
        "WEEKDAYS_ALL = tuple(WEEKDAY_OFFSET)  # 保持 周一→周日 的顺序",
        "WEEKDAYS_ALL = tuple(WEEKDAY_OFFSET)[:5]  # MUTANT: 只排工作日",
        "weekend",
    ),
    (
        "M33", "自动排课 note 不声明只排工作日",
        "app/engine/auto_scheduler.py",
        'WEEKDAY_ONLY_NOTE = "；**只在工作日（周一至周五）排课，周末不作候选**（周末已有课仍计入占用）"',
        'WEEKDAY_ONLY_NOTE = ""  # MUTANT',
        "note_declares or summary_message",
    ),
    (
        "M34", "删默认课程静默空转（返回成功但没删）",
        "app/master_data.py",
        """        if name == DEFAULT_COURSE:
            raise ValueError(""",
        """        if False:  # MUTANT: 不拦默认课程
            raise ValueError(""",
        "delete_default_course",
    ),
    (
        "M35", "删教师后不再清理实验里的关联",
        "app/master_data.py",
        """        for ex in self.experiments.values():
            ex.get("teachers", set()).discard(name)
        self.teachers.pop(name, None)""",
        """        self.teachers.pop(name, None)  # MUTANT: 不清理实验关联""",
        "consistency",
    ),
    (
        "M36", "派生任务按班级去重（丢掉其余组）",
        "app/api/reschedule.py",
        "            for g in _master_data.groups_of(cls):",
        "            for g in sorted(_master_data.groups_of(cls))[:1]:  # MUTANT",
        "per_group",
    ),
    (
        "M37", "绪论/考试类也派生（会被求解器排课）",
        "app/api/reschedule.py",
        """        if not is_schedulable_project(exp_name):
            continue""",
        """        if False:  # MUTANT: 不过滤
            continue""",
        "intro_and_exam",
    ),
    (
        "M38", "导入只报数量不点名新增实体",
        "app/api/master.py",
        '        msg += f"\\n🆕 本次新建：{shown}"',
        """        pass  # MUTANT: 不点名""",
        "names_newly_created",
    ),
    (
        "M39", "漂移检查只看行数、不看丢了哪些函数",
        "../scripts/check_remote_drift.py",
        "    lost = _symbols(remote) - _symbols(local)",
        "    lost = set()  # MUTANT: 不比对符号",
        "drift",
    ),
    (
        "M40", "审计模式不比对定义（只比行数）",
        "../scripts/check_remote_drift.py",
        "    lost = _symbols(base) - _symbols(head)",
        "    lost = set()  # MUTANT: 审计模式不比定义",
        "ref_to_ref",
    ),
    (
        "M41", "conftest 不注入 ADMIN_PASS（口令随机化）",
        "tests/conftest.py",
        'os.environ.setdefault("ADMIN_PASS", "admin123")',
        'pass  # MUTANT: 不注入',
        "env_order or test_admin_pass_is_set",
    ),
    (
        "M42", "片间不回填班级占用（新方案撞绪论课）",
        "app/engine/auto_scheduler.py",
        "                extra_class_busy=class_busy,",
        "                extra_class_busy=None,  # MUTANT: 不回填班级",
        "chunked_schedule or chunk_report",
    ),
    (
        "M43", "落盘失败不回滚内存（方案神秘消失）",
        # R76：锚点随落地方案一族搬到 app/api/landing.py（原文在 reschedule.py，
        #       且 `_db` 现在按模块属性取成 `_api._db`）。
        "app/api/landing.py",
        "            plan_landing.restore(_api._db, before)",
        "            pass  # MUTANT: 不回滚",
        "rolls_back or rollback_restores",
    ),
    (
        "M44", "损坏的落盘不丢弃（永久豁免）",
        "app/api/reschedule.py",
        """    if store is None and master_store.consume_corrupt_flag():\n        master_store.clear()""",
        """    if False:  # MUTANT: 不清理损坏落盘\n        master_store.clear()""",
        "corrupt_store_file_is_discarded",
    ),
    (
        "M45", "空表时用 (0,0) 当当前周（这周→0，下周→1）",
        "app/agent/reschedule_agent.py",
        """        bounds_now = self.week_bounds()\n        current_week = bounds_now[0] if bounds_now else None""",
        """        week_range = self.db.get_week_range()\n        current_week = week_range[0] if week_range else 1""",
        "empty_table or week_basis",
    ),
    (
        "M46", "补全时不打标记（假设二次消失）",
        "app/api/reschedule.py",
        "                t._teacher_enriched = True",
        "                pass  # MUTANT: 不打标记",
        "assumptions_repeat",
    ),
    (
        "M47", "导入不说明「预览之后数据变了」",
        "app/api/master.py",
        "    if flat_pre and (added == 0 or flat):",
        "    if False:  # MUTANT: 不提示已存在项",
        "reports_drift",
    ),
    (
        "M48", "无法识别时段的行按子串判教师（吴琳 命中 吴琳琳）",
        "app/agent/reschedule_agent.py",
        "                if teacher_name and te and same_teacher(teacher_name, te):",
        "                if teacher_name and te and teacher_name in te:  # MUTANT: 退回子串",
        "unparsed_row_teacher or matches_conflict_model",
    ),
    (
        "M49", "撤销标记写失败不兜底（课表退了、记录还在 → 重放复活）",
        "app/plan_landing.py",
        "    except Exception as first:\n        try:",
        "    except Exception as first:\n"
        "        raise  # MUTANT: 不兜底，直接抛给调用方\n        try:",
        "undo or revoke",
    ),
    (
        "M50", "负载基线不认系统自己补的教师（每次调用从空负载重算 → 后到的任务抢回同一人）",
        "app/api/reschedule.py",
        "        if t.teacher:\n            teacher_load[t.teacher] += 1",
        "        if t.teacher and not getattr(t, "
        '"_teacher_enriched", False):  # MUTANT: 不把自动补的算进负载\n'
        "            teacher_load[t.teacher] += 1",
        "arriving_in_batches or late_arrivals",
    ),
    (
        "M51", "我的课程按子串认教师（吴琳 命中 吴琳琳 → 别人的课进我的卡片）",
        "app/utils/excel_parser.py",
        "            if not e.teacher or not same_teacher(teacher_name, e.teacher):",
        "            if not e.teacher or not (teacher_name in e.teacher):  # MUTANT: 退回子串",
        "similar_teacher_names or co_taught_cell",
    ),
    (
        "M52", "卡片只报第一个时段（同一天下午的第二次课被吞掉）",
        "app/utils/excel_parser.py",
        '                "time_ambiguous": len(time_slots) > 1,',
        '                "time_ambiguous": False,  # MUTANT: 不标记多时段',
        "every_period or single_period",
    ),
    (
        "M53", "清单上传回执不说明整批替换掉了几条",
        "app/api/reschedule.py",
        "    replaced = len(_master_tasks)",
        "    replaced = 0  # MUTANT: 不报替换了多少条",
        "replaced",
    ),
    (
        "M54", "清单预览不报现有清单条数（看不出提交会整批换掉什么）",
        "app/api/master.py",
        '    report["existing_tasks"] = len(_api._master_tasks)',
        '    report["existing_tasks"] = 0  # MUTANT: 预览不报现有条数',
        "will_replace",
    ),
    (
        "M55", "外层重放中断不记录（数据库读不到时仍只说「✅ 成功加载」，"
                "与「没有调课要重放」分不出来）",
        "app/api/reschedule.py",
        '        replay_aborted = f"{type(e).__name__}: {e}"',
        "        replay_aborted = None  # MUTANT: 外层中断不留痕",
        "get_db_failure_is_reported or landings_failure_is_reported",
    ),
    (
        "M56", "教师备注打开失败也定格（退回只判路径 → 一次瞬时 IO 错误永久钉成空，"
                "注释承诺的「下一次重试」永不发生）",
        "app/utils/excel_parser.py",
        "        if path and opened:",
        "        if path:  # MUTANT: 退回只判路径非空",
        "open_failure_does_not_mark_loaded or open_failure_really_retries_next_time",
    ),
    # ---------------------------------------------------------------- R81 ---
    # 四条都打在 app/api/requests_board.py：它们的靶子不是"读源码看一眼"，
    # 而是 `test_request_approval_integrity.py` 里的并发/HTTP 行为用例。
    # M57 / M58 必须靠 `test_cas_rejects_loser_when_both_read_pending` 检出 ——
    # 那条用例在 SELECT 前拦 Barrier，强制两个请求都读到 pending 快照，
    # 否则 M15 的 400 会先挡下第二个请求，CAS 根本没被执行（守卫假绿）。
    (
        "M57", "并发审批不做条件抢占（退回无条件 UPDATE）",
        "app/api/requests_board.py",
        """        "UPDATE requests SET status = ?, admin_comment = ? "
        "WHERE id = ? AND status = 'pending'",
""",
        """        "UPDATE requests SET status = ?, admin_comment = ? "
        "WHERE id = ?",
""",
        "cas_rejects_loser or concurrent_approvals",
    ),
    (
        "M58", "抢占结果不检查 rowcount（输家照样继续应用调课）",
        "app/api/requests_board.py",
        """    if claimed.rowcount != 1:
        conn.close()
        raise HTTPException(status_code=409, detail="该申请已被其他管理员审批，请刷新后重试")
""",
        """    if False:  # MUTANT: 不检查抢占结果
        conn.close()
        raise HTTPException(status_code=409, detail="该申请已被其他管理员审批，请刷新后重试")
""",
        "cas_rejects_loser or concurrent_approvals",
    ),
    (
        "M59", "导出状态过滤放回任意串（用户可控内容重新进响应头）",
        "app/api/requests_board.py",
        """    if status and status not in ("pending", "approved", "rejected"):
        raise HTTPException(status_code=400, detail=f"不支持的状态过滤：{status}")
""",
        """    if False:  # MUTANT: 不校验状态白名单
        raise HTTPException(status_code=400, detail=f"不支持的状态过滤：{status}")
""",
        "export_rejects_status_outside_whitelist",
    ),
    (
        "M60", "数据未加载又谎报成「未找到匹配记录」",
        "app/api/requests_board.py",
        """            if not db_loaded:
                message = ("✅ 已同意调课申请，但排课数据尚未加载，本次未自动应用调课；"
""",
        """            if False:  # MUTANT: 未加载与没匹配混为一谈
                message = ("✅ 已同意调课申请，但排课数据尚未加载，本次未自动应用调课；"
""",
        "unloaded_db_approval_says_data_not_loaded",
    ),
    # ---------------------------------------------------------------- R82 ---
    # 五条都打在 app/api/reschedule.py：靶子是 test_apply_move_honesty.py 的
    # HTTP 行为用例，不是"读源码看一眼"。
    (
        "M61", "不带 key 时不生成内容指纹（退回无保护的空串）",
        "app/api/reschedule.py",
        # R83：指纹算法抽进 `_resolve_idem_key`，锚点改指端点调用行。
        """    key = _resolve_idem_key(record, req.key or "")""",
        """    key = req.key or ""  # MUTANT: 没给键就不设防""",
        "repeat_without_key or without_key_ledger or whitespace_key or concurrent_without_key",
    ),
    (
        "M62", "端点忽略 already_applied 结构化信号",
        "app/api/reschedule.py",
        "    already_applied = bool(result.get(\"already_applied\"))",
        "    already_applied = False  # MUTANT: 重复提交又变回「没找到记录」",
        "repeat_with_key_says_already_applied or repeat_without_key",
    ),
    (
        "M63", "查重命中不带结构化信号（端点只能靠文案反解）",
        "app/api/reschedule.py",
        """            return {
                "applied": 0, "skipped": 0, "already_applied": True,
                "details": [f"该申请已于 {hit['applied_at']} 应用过，本次未重复移动"],
""",
        """            return {
                "applied": 0, "skipped": 0,
                "details": [f"该申请已于 {hit['applied_at']} 应用过，本次未重复移动"],
""",
        "helper_marks_already_applied or repeat_with_key_says_already_applied",
    ),
    (
        "M64", "并发撞唯一索引时不带结构化信号",
        "app/api/reschedule.py",
        """            return {
                "applied": 0, "skipped": 0, "already_applied": True,
                "details": ["该申请已由另一个并发请求应用过，课表已是目标周状态，本次未重复记账"],
""",
        """            return {
                "applied": 0, "skipped": 0,
                "details": ["该申请已由另一个并发请求应用过，课表已是目标周状态，本次未重复记账"],
""",
        "integrity_error_branch_marks_already_applied",
    ),
    (
        "M65", "内容指纹退化成固定值（内容变了也被误挡）",
        # R83：靶子随算法搬进 `_resolve_idem_key`；两条闭环都调它，检出面更广。
        "app/api/reschedule.py",
        "    return \"auto:\" + hashlib.sha256(canonical.encode(\"utf-8\")).hexdigest()[:32]",
        "    return \"auto:constant\"  # MUTANT: 所有无 key 请求共用一个键",
        "different_target_week_is_not_swallowed_as_repeat or helper_changes_when_any_content_field_changes or different_content_still_really_applies",
    ),
    # ------------------------------------------------------------------ R83
    (
        "M66", "审批不带幂等键（退回无保护的空串）",
        "app/api/requests_board.py",
        """                            record_dict, True, _api._resolve_idem_key(record_dict))""",
        """                            record_dict)  # MUTANT: 审批退回空串键""",
        "approval_passes_nonempty_idem_key or two_identical_tickets or approval_after_apply_move or approval_ledger_row",
    ),
    (
        "M67", "审批忽略 already_applied（幂等命中又叫人手工改）",
        "app/api/requests_board.py",
        """        if apply_result.get("already_applied"):""",
        """        if False:  # MUTANT: 幂等命中又变回「请人工修改」""",
        "approval_reports_already_applied or approval_does_not_repeat_the_detail or two_identical_tickets",
    ),
    (
        "M68", "目标周不参与内容指纹（不同周次被误判重复）",
        "app/api/reschedule.py",
        """                    "target_week", "source_week")""",
        """                    "source_week")  # MUTANT: 目标周不参与指纹""",
        "helper_changes_when_any_content_field_changes or different_target_week_is_not_swallowed_as_repeat or different_content_still_really_applies",
    ),
    (
        "M69", "显式幂等键被内容指纹覆盖",
        "app/api/reschedule.py",
        """    key = (explicit or "").strip()
    if key:
        return key""",
        """    key = (explicit or "").strip()
    if False:  # MUTANT: 显式键被内容指纹覆盖
        return key""",
        "helper_explicit_key_wins or explicit_key_wins_over_fingerprint",
    ),
    (
        "M70", "幂等命中又叠回「该申请已于…应用过」",
        "app/api/requests_board.py",
        """        if apply_result["details"] and not apply_result.get("already_applied"):""",
        """        if apply_result["details"]:  # MUTANT: 诚实文案又变自相矛盾""",
        "approval_does_not_repeat_the_detail or approval_reports_already_applied",
    ),
    (
        "M71", "两条闭环各算各的键（跨路径重复应用）",
        "app/api/requests_board.py",
        """                        return _api._apply_reschedule(
                            record_dict, True, _api._resolve_idem_key(record_dict))""",
        """                        return _api._apply_reschedule(
                            record_dict, True, "approval:%s" % request_id)  # MUTANT: 不共用键""",
        "approval_after_apply_move or two_identical_tickets or helper_key_is_shared",
    ),
    # ------------------------------------------------------------------ R84
    (
        "M72", "升级前账本行不回认（重复提交又撬一周）",
        "app/api/reschedule.py",
        """        legacy_at = _find_legacy_applied_move(_resolve_idem_key(record))""",
        """        legacy_at = None  # MUTANT: 升级前账本行不回认""",
        "legacy_row_blocks_repeat_apply_with_auto_key or endpoint_reports_already_applied_for_legacy or approval_reports_already_applied_for_legacy",
    ),
    (
        "M73", "回认按调用方的键比而不是按内容（云端 cloud- 键认不回老账）",
        "app/api/reschedule.py",
        """        legacy_at = _find_legacy_applied_move(_resolve_idem_key(record))""",
        """        legacy_at = _find_legacy_applied_move(idem_key)  # MUTANT: 按调用方的键比""",
        "legacy_row_blocks_repeat_apply_with_explicit_key",
    ),
    (
        "M74", "helper 一行老账都不扫",
        "app/api/reschedule.py",
        "            \"WHERE idem_key IS NULL OR idem_key = ''\"",
        "            \"WHERE 0\"  # MUTANT: 老账本行一行都不扫",
        "helper_recognizes_legacy_row_by_content or helper_recognizes_null_idem_key_row or legacy_row_blocks_repeat_apply_with_auto_key",
    ),
    (
        "M75", "一条脏老行让整段查重崩掉",
        "app/api/reschedule.py",
        "        except (TypeError, ValueError):\n            continue\n        return r[\"applied_at\"] or \"\"",
        "        except (TypeError, ValueError):\n            raise  # MUTANT: 脏老行不许跳过\n        return r[\"applied_at\"] or \"\"",
        "helper_ignores_corrupt_json or helper_ignores_non_dict_payload",
    ),
    (
        "M76", "helper 越过职责边界把带键新行也当老账",
        "app/api/reschedule.py",
        "            \"WHERE idem_key IS NULL OR idem_key = ''\"",
        "            \"WHERE 1\"  # MUTANT: 带键新行也按内容回认",
        "helper_does_not_match_rows_with_a_real_key",
    ),
    (
        "M77", "老账命中不带结构化信号（两条闭环又变回谎报）",
        "app/api/reschedule.py",
        """            return {
                "applied": 0, "skipped": 0, "already_applied": True,
                "details": [f"该调课已于 {legacy_at} 应用过（历史账本），本次未重复移动"],
            }""",
        """            return {
                "applied": 0, "skipped": 0,
                "details": [f"该调课已于 {legacy_at} 应用过（历史账本），本次未重复移动"],
            }  # MUTANT: 老账命中丢掉结构化信号""",
        "legacy_hit_returns_structured_signal_and_honest_detail or endpoint_reports_already_applied_for_legacy or approval_reports_already_applied_for_legacy",
    ),
    # R85：审批自动应用调课失败时，不许把「调用崩了」伪装成「查无记录」。
    (
        "M78", "应用失败不带结构化信号",
        "app/api/requests_board.py",
        "                \"applied\": 0, \"skipped\": 0, \"apply_failed\": True,",
        "                \"applied\": 0, \"skipped\": 0,  # MUTANT: 丢掉失败信号",
        "test_apply_failure_first_line_does_not_claim_no_matching_record or test_apply_failure_first_line_says_failed_not_success",
    ),
    (
        "M79", "失败文案分支永不进入",
        "app/api/requests_board.py",
        "        elif apply_result.get(\"apply_failed\"):",
        "        elif False:  # MUTANT: 失败分支永不进入",
        "test_apply_failure_first_line_does_not_claim_no_matching_record or test_apply_failure_first_line_says_failed_not_success",
    ),
    (
        "M80", "失败文案改回查无记录谎报",
        "app/api/requests_board.py",
        "            message = (\"⚠️ 已同意调课申请，但自动应用调课失败，本次未移动任何记录；\"",
        "            message = (\"✅ 已同意调课申请（未找到可自动移动的记录，请在教务系统中人工修改）；\"  # MUTANT: 失败伪装成查无记录",
        "test_apply_failure_first_line_does_not_claim_no_matching_record or test_apply_failure_first_line_says_failed_not_success",
    ),
    (
        "M81", "失败文案丢掉失败二字",
        "app/api/requests_board.py",
        "            message = (\"⚠️ 已同意调课申请，但自动应用调课失败，本次未移动任何记录；\"",
        "            message = (\"⚠️ 已同意调课申请，但自动应用未生效，本次未移动任何记录；\"  # MUTANT: 失败二字消失",
        "test_apply_failure_first_line_says_failed_not_success",
    ),
    (
        "M82", "失败时丢掉具体异常原因",
        "app/api/requests_board.py",
        "                \"details\": [f\"自动应用失败：{e}（可人工在教务系统调整）\"],",
        "                \"details\": [\"自动应用失败\"],  # MUTANT: 丢掉具体异常原因",
        "test_apply_failure_keeps_exception_reason",
    ),
    (
        "M83", "失败时把 details 一起吞掉",
        "app/api/requests_board.py",
        "        if apply_result[\"details\"] and not apply_result.get(\"already_applied\"):",
        "        if apply_result[\"details\"] and not apply_result.get(\"already_applied\") and not apply_result.get(\"apply_failed\"):  # MUTANT: 失败不叠原因",
        "test_apply_failure_keeps_exception_reason",
    ),
    (
        "M84", "真查无记录也说自动应用失败",
        "app/api/requests_board.py",
        "                message = \"✅ 已同意调课申请（未找到可自动移动的记录，请在教务系统中人工修改）\"",
        "                message = \"⚠️ 已同意调课申请，但自动应用调课失败，本次未移动任何记录\"  # MUTANT: 真查无记录也说失败",
        "test_real_no_match_still_says_no_record",
    ),
    (
        "M85", "未加载降级也说自动应用失败",
        "app/api/requests_board.py",
        "                message = (\"✅ 已同意调课申请，但排课数据尚未加载，本次未自动应用调课；\"",
        "                message = (\"⚠️ 已同意调课申请，但自动应用调课失败，本次未移动任何记录；\"  # MUTANT: 未加载也说失败",
        "test_unloaded_db_still_says_data_not_loaded",
    ),
    # R86：/load-default 的降级信号必须来自结构化状态，且真的回到响应里。
    (
        "M86", "不把重放失败落进 load_report",
        "app/api/reschedule.py",
        "    rep[\"replay_failed\"] = list(replay_failed)\n"
        "    rep[\"replay_aborted\"] = replay_aborted",
        "    rep.pop(\"replay_failed\", None)  # MUTANT: 不落结构化失败列表\n"
        "    rep[\"replay_aborted\"] = replay_aborted",
        "test_replay_failure_is_structurally_degraded or test_load_report_holds_structured_replay_state",
    ),
    (
        "M87", "丢掉重放中断的结构化原因",
        "app/api/reschedule.py",
        "    rep[\"replay_aborted\"] = replay_aborted",
        "    rep[\"replay_aborted\"] = None  # MUTANT: 丢掉中断原因",
        "test_replay_abort_is_structurally_degraded",
    ),
    (
        "M88", "端点不看结构化状态（退化为嗅探中文）",
        "app/api/reschedule.py",
        "    rep = _db.load_report\n    warnings: list[str] = []",
        "    rep = {}  # MUTANT: 不看结构化状态\n    warnings: list[str] = []",
        "test_degraded_reads_report_not_message_wording or test_replay_failure_is_structurally_degraded",
    ),
    (
        "M89", "老账本缺键时直接 500",
        "app/api/reschedule.py",
        "    failed = rep.get(\"replay_failed\") or []",
        "    failed = rep[\"replay_failed\"]  # MUTANT: 老账本缺键直接 500",
        "test_warnings_are_empty_list_when_no_replay_state",
    ),
    (
        "M90", "少量重放失败不报警",
        "app/api/reschedule.py",
        "    if failed:\n"
        "        warnings.append(\n"
        "            f\"{len(failed)} 条已审批调课重放失败，它们的调整未生效（{failed[0]}）\")",
        "    if len(failed) > 3:  # MUTANT: 少量失败不报\n"
        "        warnings.append(\n"
        "            f\"{len(failed)} 条已审批调课重放失败，它们的调整未生效（{failed[0]}）\")",
        "test_replay_failure_is_structurally_degraded or test_replay_failure_count_is_not_lost",
    ),
    (
        "M91", "重放中断不报警",
        "app/api/reschedule.py",
        "    if aborted:\n"
        "        warnings.append(f\"已审批调课重放中断，本期一条都未重放（{aborted}）\")",
        "    if False:  # MUTANT: 重放中断不报\n"
        "        warnings.append(f\"已审批调课重放中断，本期一条都未重放（{aborted}）\")",
        "test_replay_abort_is_structurally_degraded",
    ),
    (
        "M92", "degraded 恒为 False",
        "app/api/reschedule.py",
        "            \"degraded\": bool(warnings), \"warnings\": warnings}",
        "            \"degraded\": False, \"warnings\": warnings}  # MUTANT: 恒不降级",
        "test_replay_failure_is_structurally_degraded or test_replay_abort_is_structurally_degraded",
    ),
    (
        "M93", "响应里不回 warnings 明细",
        "app/api/reschedule.py",
        "    return {\"success\": True, \"message\": msg, \"total_entries\": total,\n"
        "            \"degraded\": bool(warnings), \"warnings\": warnings}",
        "    # MUTANT: 不回 warnings 明细\n"
        "    return {\"success\": True, \"message\": msg, \"total_entries\": total,\n"
        "            \"degraded\": bool(warnings)}",
        "test_replay_failure_is_structurally_degraded or test_replay_abort_is_structurally_degraded",
    ),
    # R87：落盘结果必须一路透传到响应；只在内存生效 ≠ 已保存。
    (
        "M94", "同步落盘吞掉 save 的 False",
        "app/api/reschedule.py",
        "    return master_store.save({",
        "    master_store.save({  # MUTANT: 吞掉 save 结果",
        "test_sync_persist_returns_false_when_save_fails",
    ),
    (
        "M95", "无事可做被当成落盘失败",
        "app/api/reschedule.py",
        "        return True  # 本来就不用写盘：无事可做 ≠ 写失败",
        "        return False  # MUTANT: 无事可做也报失败",
        "test_sync_persist_returns_true_when_nothing_to_save",
    ),
    (
        "M96", "线程池版本不透传落盘结果",
        "app/api/reschedule.py",
        "    return await run_in_threadpool(_persist_master_state)",
        "    await run_in_threadpool(_persist_master_state)  # MUTANT: 不透传",
        "test_async_persist_propagates_false",
    ),
    (
        "M97", "helper 用 not persisted 制造假警告",
        "app/api/reschedule.py",
        "    if persisted is False:",
        "    if not persisted:  # MUTANT: None 也当成落盘失败",
        "test_no_false_alarm_when_persist_result_is_none",
    ),
    (
        "M98", "helper 恒加降级信号",
        "app/api/reschedule.py",
        "    if persisted is False:\n"
        "        payload = dict(payload)\n"
        "        payload[\"persisted\"] = False",
        "    if True:  # MUTANT: 成功路径也报警\n"
        "        payload = dict(payload)\n"
        "        payload[\"persisted\"] = False",
        "test_healthy_write_has_no_warning",
    ),
    (
        "M99", "新建教师假装落盘成功",
        "app/api/master.py",
        "    try:\n"
        "        _api._master_data.upsert_teacher(req.name, projects=req.projects)\n"
        "    except ValueError as e:\n"
        "        raise HTTPException(status_code=400, detail=str(e))\n"
        "    persisted = await _api._persist_master_state_async()",
        "    try:\n"
        "        _api._master_data.upsert_teacher(req.name, projects=req.projects)\n"
        "    except ValueError as e:\n"
        "        raise HTTPException(status_code=400, detail=str(e))\n"
        "    persisted = True  # MUTANT: 不看真实落盘结果",
        "test_upsert_teacher_reports_persist_failure",
    ),
    (
        "M100", "台账导入假装落盘成功",
        "app/api/master.py",
        "    except ValueError as e:\n"
        "        raise HTTPException(status_code=400, detail=f\"台账写入失败：{e!s}\")\n"
        "    persisted = await _api._persist_master_state_async()",
        "    except ValueError as e:\n"
        "        raise HTTPException(status_code=400, detail=f\"台账写入失败：{e!s}\")\n"
        "    persisted = True  # MUTANT: 导入不接落盘结果",
        "test_every_persist_call_consumes_its_result",
    ),
    (
        "M101", "清空任务清单假装落盘成功",
        "app/api/reschedule.py",
        "    _master_tasks.clear()\n"
        "    persisted = await _persist_master_state_async()",
        "    _master_tasks.clear()\n"
        "    persisted = True  # MUTANT: 清空清单不接落盘结果",
        "test_clear_tasks_reports_persist_failure or test_every_persist_call_consumes_its_result",
    ),
    (
        "M102", "警告文案谎称已安全保存",
        "app/api/reschedule.py",
        "    \"⚠️ 本次修改已在当前会话生效，但未能写入磁盘；\"\n"
        "    \"服务重启或沙箱回收后会回退到安排表基线，请尽快重试或联系管理员检查磁盘。\"",
        "    \"⚠️ 修改已安全保存，无需任何处理。\"  # MUTANT: 谎称已存住",
        "test_upsert_teacher_reports_persist_failure",
    ),
    # R88：主数据落盘恢复失败（用户已保存的修改被丢弃）不许静默。
    # 三条异常分支各自记因、helper 只在真丢数据时加信号、overview 必须接线。
    (
        "M103", "落盘损坏分支不记降级原因",
        "app/api/reschedule.py",
        '        _note_master_restore_loss("落盘文件损坏，已丢弃")',
        '        pass  # MUTANT: 坏了也不说',
        "test_loss_branches_report_restored_false or test_every_loss_branch_records_the_reason",
    ),
    (
        "M104", "恢复抛异常分支不记降级原因",
        "app/api/reschedule.py",
        '            _note_master_restore_loss(f"恢复时出错：{type(e).__name__}")',
        '            pass  # MUTANT: 恢复失败也不说',
        "test_from_dict_exception_reports_restored_false or test_every_loss_branch_records_the_reason",
    ),
    (
        "M105", "空主数据分支不记降级原因",
        "app/api/reschedule.py",
        '            _note_master_restore_loss("落盘里是空的主数据，已丢弃")',
        '            pass  # MUTANT: 空表也不说',
        "test_loss_branches_report_restored_false or test_every_loss_branch_records_the_reason",
    ),
    (
        "M106", "换表被误记成数据丢失（狼来了）",
        "app/api/reschedule.py",
        '        print("ℹ️ 安排表已更换，丢弃旧表上的主数据落盘，按新表反推基线")\n'
        '        master_store.clear()',
        '        print("ℹ️ 安排表已更换，丢弃旧表上的主数据落盘，按新表反推基线")\n'
        '        master_store.clear()\n'
        '        _note_master_restore_loss("换表")  # MUTANT: 正常降级也报警',
        "test_normal_paths_keep_response_byte_shape or test_loss_note_is_not_attached_to_stale_fingerprint_branch",
    ),
    (
        "M107", "helper 恒加降级信号（每次首屏都报警）",
        "app/api/reschedule.py",
        "    if not _master_restore_warning:\n"
        "        return payload",
        "    if False:  # MUTANT: 成功路径也报警\n"
        "        return payload",
        "test_helper_is_silent_and_does_not_copy_when_no_loss or test_normal_paths_keep_response_byte_shape",
    ),
    (
        "M108", "helper 就地改写调用方的 payload",
        "app/api/reschedule.py",
        "    if not _master_restore_warning:\n"
        "        return payload\n"
        "    payload = dict(payload)\n"
        '    payload["restored"] = False',
        "    if not _master_restore_warning:\n"
        "        return payload\n"
        "    pass  # MUTANT: 不复制，就地改调用方的 dict\n"
        '    payload["restored"] = False',
        "test_helper_does_not_mutate_caller_payload",
    ),
    (
        "M109", "overview 不接恢复降级 helper",
        "app/api/master.py",
        '    return _api._master_restore_result(\n'
        '        {"success": True, "data": _api._master_data.overview()}\n'
        '    )',
        '    return {"success": True, "data": _api._master_data.overview()}  # MUTANT: 降级状态到不了界面',
        "test_loss_branches_report_restored_false or test_overview_is_wired_to_the_restore_helper",
    ),
    (
        "M110", "警告文案谎称已安全恢复（整段替换，R87 教训）",
        "app/api/reschedule.py",
        '_MASTER_RESTORE_WARNING_TEMPLATE = (\n'
        '    "⚠️ 服务重启后未能恢复上次保存的主数据（{reason}）："\n'
        '    "已回退到按当前安排表反推的基线，之前新增或调整的实验项目 / 教师 / 场地"\n'
        '    "可能已丢失。请核对下方五张表是否完整，必要时重新维护并确认保存成功。"\n'
        ')',
        '_MASTER_RESTORE_WARNING_TEMPLATE = (\n'
        '    "ℹ️ 主数据已从磁盘安全恢复（{reason}）："\n'
        '    "五张表与上次保存时完全一致，没有任何改动被丢弃，无需核对。"\n'
        ')  # MUTANT: 谎称已恢复',
        "test_loss_branches_report_restored_false",
    ),
    (
        "M111", "可调性判定被反转（恒不可调）",
        "app/utils/excel_parser.py",
        '                "adjustable": not course_ambiguous,',
        '                "adjustable": course_ambiguous,  # MUTANT: 恒不可调',
        "test_single_course_card_stays_adjustable_by_default",
    ),
    (
        "M112", "不可调却不给原因（谎称无碍）",
        "app/utils/excel_parser.py",
        '                "adjustable_reason": (\n'
        '                    "该卡片横跨多门实验项目（%s），标准按课程名调课流程无法定位到"\n'
        '                    "单一目标，请先拆分或联系教务" % "、".join(course_list)\n'
        '                    if course_ambiguous else ""\n'
        '                ),',
        '                "adjustable_reason": (\n'
        '                    "" if course_ambiguous else ""  # MUTANT: 不可调也不说原因\n'
        '                ),',
        "test_ambiguous_course_card_is_not_adjustable",
    ),
    # R91：导出预热状态（与 solver 预热对称）。原实现失败只 print，无任何结构化记录。
    (
        "M113", "导出预热失败却谎称成功",
        "app/api/export.py",
        '        _EXPORT_WARM_STATE["status"] = "failed"',
        '        _EXPORT_WARM_STATE["status"] = "ok"  # MUTANT: 失败谎称成功',
        "test_export_warmup_records_failure",
    ),
    (
        "M114", "导出预热成功却不记 ok",
        "app/api/export.py",
        '        _EXPORT_WARM_STATE["status"] = "ok"',
        '        pass  # MUTANT: 成功不记 ok',
        "test_export_warmup_records_ok",
    ),
    (
        "M115", "结构损坏 fail-closed 不记失败标志",
        "app/master_data.py",
        '            self._from_dict_failed = True',
        '            pass  # MUTANT: 不记失败标志',
        "test_from_dict_marks_failed_flag_on_corrupt",
    ),
    (
        "M116", "损坏不报损坏（谎称空）",
        "app/api/reschedule.py",
        '                _note_master_restore_loss("落盘文件结构损坏，已丢弃")',
        '                _note_master_restore_loss("落盘里是空的主数据，已丢弃")  # MUTANT: 谎称空',
        "corrupt_store_warning_says_corrupt",
    ),
    # R93：旧版本落盘作废 —— 原实现只 print，与"文件不存在"混同，用户看不到编辑为何消失。
    (
        "M117", "旧版本落盘不标记版本不匹配",
        "app/master_store.py",
        '            _mark_version_mismatch(store_path, d.get("schema_version"))',
        '            pass  # MUTANT: 不标记版本不匹配',
        "test_version_mismatch_store_surfaces_warning",
    ),
    (
        "M118", "旧版本作废却谎称损坏",
        "app/api/reschedule.py",
        '                f"落盘为旧版本格式（v{_vm} ≠ v{master_store.SCHEMA_VERSION}），已作废")',
        '                "落盘文件损坏，已丢弃")  # MUTANT: 谎称损坏',
        "test_version_mismatch_store_surfaces_warning",
    ),
]


def _run_tests(files: list, kexpr: str) -> tuple:
    """返回 (判定, 摘要)，判定 ∈ {检出, 存活, 无效}。

    为什么不能只看 returncode（R70 加固）：
        pytest 的退出码含义是「非 0 即有事」——
        5 = 一条用例都没选中（-k 写错/被测用例改名）、
        2 = 被中断、3 = 内部错误、4 = 用法错误，
        这些都会让「删掉防护」看起来像「检出」，其实是仪表盘在骗人。
        另外，注入把模块搞成导入错误时，pytest 报的是 error 不是 failed ——
        那也不叫"测试抓住了行为"，叫"代码根本跑不起来"。
    """
    cmd = [PY, "-m", "pytest", "-q", "--no-header",
           "-p", "no:cacheprovider", *files, "-k", kexpr]
    proc = subprocess.run(cmd, cwd=BACKEND, capture_output=True,
                          text=True, timeout=900)
    out = proc.stdout + proc.stderr
    lines = [ln.strip() for ln in out.splitlines()
             if "passed" in ln or "failed" in ln or "error" in ln]
    summary = lines[-1] if lines else "?"

    failed = int(m.group(1)) if (m := re.search(r"(\d+) failed", out)) else 0
    errors = int(m.group(1)) if (m := re.search(r"(\d+) error", out)) else 0
    passed = int(m.group(1)) if (m := re.search(r"(\d+) passed", out)) else 0

    if proc.returncode == 5 or "no tests ran" in out:
        return "无效", f"没选中任何用例（-k 失效）: {summary}"
    if failed > 0:
        return "检出", summary
    if errors > 0:
        return "无效", f"注入导致报错而非断言失败（{errors} error）: {summary}"
    if proc.returncode != 0:
        return "无效", f"pytest 退出码 {proc.returncode}: {summary}"
    if passed > 0:
        return "存活", summary
    return "无效", summary


def main() -> int:
    results = []
    for mid, desc, rel, old, new, kexpr in MUTANTS:
        # rel 可以是相对 backend/ 的路径，也可以带 ../ 指到仓库其他目录
        # （如 scripts/ 下的工具脚本）—— 它们同样需要变异守护。
        path = os.path.join(BACKEND, rel)
        if not os.path.isfile(path):
            results.append((mid, desc, "SKIP", "文件缺失"))
            continue
        with io.open(path, encoding="utf-8") as _fh:
            orig = _fh.read()
        if old not in orig:
            results.append((mid, desc, "SKIP", "锚点未匹配（代码已重构？）"))
            continue

        backup = path + ".mutbak"
        shutil.copy2(path, backup)
        try:
            with io.open(path, "w", encoding="utf-8") as _fh:
                _fh.write(orig.replace(old, new, 1))
            verdict, summary = _run_tests(FILES.get(mid, []), kexpr)
            results.append((mid, desc, verdict, summary[:64]))
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
        mark = {"检出": "PASS", "存活": "XXXX", "SKIP": "--", "超时": "--",
                "无效": "????"}[verdict]
        print(f"{mark} {mid:3s} {desc:30s} {verdict:6s} {summary}")
    print("=" * 88)

    tested = [r for r in results if r[2] in ("检出", "存活")]
    alive = [r for r in results if r[2] == "存活"]
    invalid = [r for r in results if r[2] == "无效"]
    skipped = [r for r in results if r[2] == "SKIP"]
    timeout = [r for r in results if r[2] == "超时"]
    if tested:
        # 分母只放真正跑过的。SKIP/超时**不进分母** —— 一旦混进去，
        # 「47/47」就会把一个根本没执行的变异体算成通过（R43 实测：M30 锚点已失效，
        # 脚本照样打印「全部检出」并退出 0）。这里把它们单列出来，让总数对得上。
        print(f"检出 {len(tested) - len(alive)}/{len(tested)}"
              f"（另有 {len(skipped)} 个跳过、{len(timeout)} 个超时未计入分母）")
    if skipped:
        print("\n以下变异体被跳过 —— 目标文件缺失或锚点失配（通常是代码被重构了），"
              "这道防护现在是没人在守的：")
        for mid, desc, _, summary in skipped:
            print(f"  - {mid} {desc}（{summary}）")
    if timeout:
        print("\n以下变异体超时 —— 同上，本轮没有得出任何结论：")
        for mid, desc, _, _ in timeout:
            print(f"  - {mid} {desc}")
    if alive:
        print("\n以下变异体存活 —— 对应防护缺少有效测试：")
        for mid, desc, _, _ in alive:
            print(f"  - {mid} {desc}")
    if invalid:
        print("\n以下变异体判定无效 —— 不是防护有效，是这轮根本没测到：")
        for mid, desc, _, summary in invalid:
            print(f"  - {mid} {desc}（{summary}）")
    # 「没被执行」和「执行了但没拦住」同样是失败：
    # 前者意味着这条防护的守护状态未知，不能算在通过里。
    if alive or invalid or skipped or timeout:
        print(f"\n未通过（共 {len(results)} 个变异体）："
              f"{len(alive)} 个存活、{len(invalid)} 个无效、"
              f"{len(skipped)} 个跳过、{len(timeout)} 个超时。")
        return 1
    print("\n全部检出：每个安全防护都有测试真正守着。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
