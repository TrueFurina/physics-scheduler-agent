"""调课申请单工单族（R80 从 `app/api/reschedule.py` 搬出）。

为什么状态一行都不搬
------------------------
`_DB_PATH` / `_get_db` / `_init_db` / `_DB_WRITE_LOCK` / `_apply_reschedule`
都留在旧模块：它们不只服务这五条路由，还被
`_load_default_schedule()` 的重放、`/demo/reset` 清表、`/apply-move`
幂等、落地方案同库共用。二十多个测试用
`monkeypatch.setattr(reschedule, "_DB_PATH", ...)` 把库指到临时目录。

若把 `_DB_PATH` 一起搬走，`monkeypatch.setattr` 会在旧模块上
**新建**一个同名属性，而新模块读自己那一份 —— 测试全绿，
写的却是真实库。所以本模块一律 `_api.xxx` 现取，
包括装饰器上的 `_api._ADMIN_ONLY`（防止鉴权列表分叉）。

不搬：`/apply-move`（审批台闭环，脱离工单表）、
`ApplyMoveRequest`、SQLite 建表与重放逻辑。

不变量（见 `tests/test_requests_module_split.py`）
------------------------
1. 五条路由的路径 / 方法 / 函数名 / `dependencies` 不变。
2. 状态现取、同一对象；`_get_db` 与 `_apply_reschedule` 有行为锁。
3. `approve_request` 的锁 + 调课应用仍在线程池里（R28）。
4. `/requests/export` 的零 await 豁免带实测数字，不许把说明搬丢。
5. **M15 / M16 变异锚点跟着搬**：它们的原文在 `approve_request`
   里（旧文件 1987 / 2001 行）。R79 注释点名的 M50 实测在
   `reschedule.py:929`，不在任何搬家区间——那是一条错误推断。
"""

from datetime import datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from app.api import reschedule as _api

router = APIRouter()


class RescheduleRequestCreate(BaseModel):
    """教师提交调课申请"""
    teacher: str
    course_name: str
    class_name: str = ""
    original_time: str = ""
    target_week: int
    reason: str = ""
    source_week: int = 0  # 原上课周（0=自动选取目标周之前最近的一周）


class RescheduleRequestApprove(BaseModel):
    """管理员审批"""
    approve: bool  # True=同意, False=驳回
    comment: str = ""


@router.post("/requests", summary="教师提交调课申请（F6）", dependencies=_api._ADMIN_ONLY)
async def create_request(req: RescheduleRequestCreate):
    """教师提交调课申请，状态初始为 pending（SQLite 持久化）"""
    conn = _api._get_db()
    # 保留上限：超过 200 条时清理最早的已完成记录，避免演示数据膨胀
    try:
        count_row = conn.execute("SELECT COUNT(*) as c FROM requests").fetchone()
        if count_row["c"] >= 200:
            conn.execute(
                "DELETE FROM requests WHERE status != 'pending' "
                "AND id NOT IN (SELECT id FROM requests ORDER BY id DESC LIMIT 100)"
            )
            conn.commit()
    except Exception:
        pass  # 清理失败不影响主流程

    cur = conn.execute(
        # `created_at` 曾经被硬写成学期待码 `'2026-2027-1'`（一个**不是时间**的字符串）。
        # 界面当时不显示提交时间，所以没暴露；但只要有人按它排序、或在审批台加上
        # "提交于 …"，就是一行错数据。（2026-10-29 实测落库值 = '2026-2027-1'。）
        "INSERT INTO requests (teacher, course_name, class_name, original_time, target_week, reason, status, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)",
        (req.teacher, req.course_name, req.class_name, req.original_time, req.target_week,
         req.reason, datetime.now().isoformat(timespec="seconds")),
    )
    conn.commit()
    record = dict(cur.execute("SELECT * FROM requests WHERE id = ?", (cur.lastrowid,)).fetchone())
    conn.close()
    return {"success": True, "message": "✅ 调课申请已提交，等待管理员审批", "request": record}


@router.delete("/requests", summary="清空调课申请（调试/演示用）", dependencies=_api._ADMIN_ONLY)
async def clear_requests():
    """清空全部调课申请（调试/演示重置用）"""
    conn = _api._get_db()
    conn.execute("DELETE FROM requests")
    conn.commit()
    conn.close()
    return {"success": True, "message": "✅ 已清空全部调课申请"}


@router.get("/requests", summary="调课申请列表（F6）", dependencies=_api._ADMIN_ONLY)
async def list_requests(status: str = ""):
    """查看调课申请列表，可按状态过滤（pending/approved/rejected）"""
    conn = _api._get_db()
    if status:
        rows = conn.execute("SELECT * FROM requests WHERE status = ? ORDER BY id DESC", (status,)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM requests ORDER BY id DESC").fetchall()
    conn.close()
    items = [dict(r) for r in rows]
    return {"success": True, "requests": items, "total": len(items)}


@router.get("/requests/export", summary="导出调课申请记录 Excel（审批留档）", dependencies=_api._ADMIN_ONLY)
async def export_requests(status: str = ""):
    """把全部（或按状态）调课申请单导出为 Excel —— 管理员留档/归档的真实工作流。

    ⚠️ 与 `/parallel/export` 一样**刻意留在请求线程上**（不是漏改）。

    2026-11-02 实测建表耗时（openpyxl 建表 + 序列化，不含 SQL）：
        0 行 5.1ms / 50 行 7.1ms / 200 行 15.2ms / 500 行 30.2ms
    全部在 R-70 的 50ms 线以下。R-70 口径是"只下沉实测 ≥50ms **且**每次点击都会走的路径"，
    本路由只做审批留档，行数上限就是申请单数量，不在高频路径上 ⇒ 不下沉。

    但它此前**一句话都没写**，于是在 `scripts/audit_async_blocking.py` 眼里与
    "漏改的阻塞路由"完全一样（零 await + 直接干重活），审计门因此长期红着而没人能判断
    该修还是该登记。现在把数字写在这里，并登记进 `async_blocking_baseline.json` 的
    零 await 基线 —— **有数字的豁免**，不是把问题洗进基线。

    若申请单量级涨到千行以上（实测会越过 50ms），按 `_build_*_bytes()` +
    `run_in_threadpool` 下沉，同时把本路由从基线里删掉。
    """
    from io import BytesIO
    import openpyxl
    from openpyxl.styles import Font, Alignment
    from fastapi.responses import StreamingResponse
    from urllib.parse import quote

    # R81：status 原先原样拼进下载文件名，而 `quote` 默认 `safe='/'` 会保留斜杠。
    # 探针实测 `?status=../../pwned` 返回
    # `attachment; filename*=UTF-8''调课申请记录_../../pwned.xlsx` —— 下载器据此
    # 可能把文件落到预期目录之外。SQL 侧本来就参数化，问题只在响应头。
    # 白名单同时挡住大小写变形与任意串；`safe=""` 是纵深，别让以后有人放宽白名单
    # 时连这层也一起丢掉。
    if status and status not in ("pending", "approved", "rejected"):
        raise HTTPException(status_code=400, detail=f"不支持的状态过滤：{status}")

    conn = _api._get_db()
    if status:
        rows = conn.execute("SELECT * FROM requests WHERE status = ? ORDER BY id", (status,)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM requests ORDER BY id").fetchall()
    conn.close()

    status_cn = {"pending": "待审批", "approved": "已通过", "rejected": "已驳回"}
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "调课申请记录"
    headers = ["申请单号", "教师", "课程", "班级", "原时间", "目标周", "原因", "状态", "管理员批注", "提交时间"]
    ws.append(headers)
    for c in ws[1]:
        c.font = Font(bold=True)
        c.alignment = Alignment(horizontal="center")
    for r in rows:
        d = dict(r)
        ws.append([d.get("id"), d.get("teacher"), d.get("course_name"), d.get("class_name"),
                   d.get("original_time"), d.get("target_week"), d.get("reason"),
                   status_cn.get(d.get("status"), d.get("status") or ""),
                   d.get("admin_comment"), d.get("created_at")])
    for col, w in zip("ABCDEFGHIJ", [10, 10, 22, 28, 28, 8, 30, 10, 20, 20]):
        ws.column_dimensions[col].width = w

    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    label = f"_{status_cn[status]}" if status else ""
    filename = quote(f"调课申请记录{label}.xlsx", safe="")
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{filename}"})


@router.post("/requests/{request_id}/approve", summary="管理员审批（F6）", dependencies=_api._ADMIN_ONLY)
async def approve_request(request_id: int, req: RescheduleRequestApprove):
    """管理员审批调课申请：同意 → approved；驳回 → rejected（填写原因）"""
    conn = _api._get_db()
    row = conn.execute("SELECT * FROM requests WHERE id = ?", (request_id,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail=f"未找到申请单 #{request_id}")

    if row["status"] != "pending":
        conn.close()
        raise HTTPException(status_code=400, detail=f"该申请已审批（状态：{row['status']}），不能重复操作")

    # R81：并发审批的原子抢占（CAS，compare-and-set）。
    # 原先是「先应用调课 → 末尾无条件 UPDATE status」：两个请求同时读到 pending
    # 就各应用一次。探针实测（2026-11-05）两个响应都是 200、`_apply_reschedule`
    # 调用 2 次；而它的源周是「目标周之前最近的匹配周」，重复执行会去移**更早一周**
    # —— 一张工单能连撬好几周。顺序重复审批有 M15 的 400 挡着，并发没有。
    # 现在把状态写成条件更新并**立刻提交**：只有 rowcount==1 的赢家能继续；
    # 输家在应用调课之前就 409，不持有写事务、也不会覆盖赢家的批注。
    status = "approved" if req.approve else "rejected"
    comment = req.comment or ("同意调课" if req.approve else "未说明原因")
    claimed = conn.execute(
        "UPDATE requests SET status = ?, admin_comment = ? "
        "WHERE id = ? AND status = 'pending'",
        (status, comment, request_id),
    )
    conn.commit()
    if claimed.rowcount != 1:
        conn.close()
        raise HTTPException(status_code=409, detail="该申请已被其他管理员审批，请刷新后重试")

    record_dict = dict(row)
    db_loaded = bool(_api._db.loaded)
    if req.approve:
        # 审批通过 → 自动应用调课（移动排课记录到目标周，冲突时段跳过）
        apply_result = {"applied": 0, "skipped": 0, "details": []}
        try:
            if db_loaded:
                # R28：与 apply_move / land_commit 共用写锁；且这是同步阻塞调用，
                # 必须丢线程池（否则一次调课应用会占住事件循环，同 R25/R26）。
                def _locked_apply_approve():
                    with _api._DB_WRITE_LOCK:
                        # R83：演示模式审批也必须带幂等键。原先只传 record_dict，
                        # `idem_key` 落成 ''，而库层唯一索引刻意放过空串 ——
                        # 两张内容相同的工单先后批准时第二条会去撬更早一周，
                        # 也认不出 /apply-move 已经写下的 auto: 账。
                        # 用共用 helper 按调课六字段算键，两条闭环互相认账；
                        # 状态一律 `_api.xxx` 现取，别在模块里绑值副本。
                        return _api._apply_reschedule(
                            record_dict, True, _api._resolve_idem_key(record_dict))
                apply_result = await run_in_threadpool(_locked_apply_approve)
        except Exception as e:
            # R85：调用崩了 ≠ 查过没找到。旧代码把 applied/skipped 都置 0，
            # 末尾 else 于是说「未找到可自动移动的记录，请在教务系统中人工修改」，
            # 而 details 里明明写着「自动应用失败」—— 一条记录都没查过却让教秘
            # 以为查过了、去教务系统再改一遍（探针 `_r85_probe.py` 实测）。
            # 给结构化信号，让文案分支与「真的查无记录」分开，别靠中文反解状态。
            apply_result = {
                "applied": 0, "skipped": 0, "apply_failed": True,
                "details": [f"自动应用失败：{e}（可人工在教务系统调整）"],
            }
        if apply_result.get("already_applied"):
            # R83：R82 给 `_apply_reschedule` 加了结构化信号，但审批路径没认。
            # 幂等命中时 applied=0/skipped=0，旧分支会说「未找到可自动移动的
            # 记录，请在教务系统中人工修改」—— 明明是重复调课，却叫教秘再去
            # 改一遍。与 /apply-move 一样只说实话，也不叠加 details（见下方）。
            message = ("ℹ️ 已同意调课申请，但这条调课此前已应用过，"
                       "本次未重复移动，无需人工处理")
        elif apply_result.get("apply_failed"):
            # R85：失败就是失败，不许借用「查无记录」那句（那是真的查过才说的）。
            # 仍保留 details 里的具体异常，让管理员能定位账本/写库问题。
            message = ("⚠️ 已同意调课申请，但自动应用调课失败，本次未移动任何记录；"
                       "请在教务系统中人工调整或稍后重试")
        elif apply_result["applied"] > 0:
            n_resched = apply_result.get("rescheduled", 0)
            message = (f"✅ 已同意并自动应用调课：{apply_result['applied']} 条记录移至第{record_dict['target_week']}周"
                       + (f"（其中 {n_resched} 条因目标周冲突已自动改签到空闲时段）" if n_resched else "")
                       + (f"，{apply_result['skipped']} 条因目标周无空闲时段跳过" if apply_result["skipped"] else ""))
        elif apply_result["skipped"] > 0:
            message = (f"✅ 已同意调课申请，但目标周该时段已有安排，{apply_result['skipped']} 条记录未自动移动"
                       f"（详见下方，请在教务系统中人工协调）")
        else:
            # R81：数据没加载时上面那段 try 连进都没进，此时的「未找到可自动移动的
            # 记录」是谎报 —— 实际一条都没查过。探针实测返回体正是这句，工单却已
            # 变成 approved。行为不变（审批仍成功），但把降级原因说清楚。
            if not db_loaded:
                message = ("✅ 已同意调课申请，但排课数据尚未加载，本次未自动应用调课；"
                           "请刷新数据后重试，或人工在教务系统调整")
            else:
                message = "✅ 已同意调课申请（未找到可自动移动的记录，请在教务系统中人工修改）"
        # R83：幂等命中时 details 里那句「该申请已于 … 应用过」是重复解释，
        # 且 R82 之前正是「前半句叫人手工改 + 后半句说已应用过」的自相矛盾。
        if apply_result["details"] and not apply_result.get("already_applied"):
            message += "\n" + "\n".join(apply_result["details"][:6])
    else:
        message = f"❌ 已驳回调课申请（原因：{comment}）"

    # R81：status / admin_comment 已在 CAS 里写入并提交，末尾不再无条件 UPDATE ——
    # 否则并发输家虽被 409 挡下，仍可能把赢家的状态与批注盖回去。
    record = dict(conn.execute("SELECT * FROM requests WHERE id = ?", (request_id,)).fetchone())
    conn.close()
    return {"success": True, "message": message, "request": record}
