"""落地方案端点族（R76 从 `app/api/reschedule.py` 整块搬出）。

为什么单独成模块
----------------
`reschedule.py` 已近 3500 行，落地这一族（预览 / 采纳 / 撤销 / 历史 / 状态）
自带一套三段式语义（预览不写、采纳现算 diff、撤销弹一条），与调课工单、主数据、
导出、聊天是四件不相干的事。混在一个文件里时"改落地"要在 3500 行里定位；而
R73 / R74 / R75 三轮的改动全部落在这 200 行 —— 它已经是一个**独立的变化单元**。

搬迁原则：只搬家，不改行为
--------------------------
1. 路由路径、方法、summary、dependencies 一字未动；`main.py` 用同一个
   prefix + tags 挂载，`/openapi.json` 里这 5 条与搬迁前一致。
2. 函数体、文档字符串、注释原文照搬（除第 3 条的 `_api.` 前缀）。
3. 状态的读写**必须**写成本模块里的 `_api.xxx` 模块属性访问，不能写成
   `from app.api.reschedule import _db` 这类值绑定 —— 原因见下一节。
4. `reschedule.py` 末尾保留旧名惰性转发（模块级 `__getattr__`），
   `reschedule.land_commit` 这类既有引用照旧可用，测试零改动。

为什么状态必须走模块属性（最容易在搬迁时写错的一条）
----------------------------------------------------
`_db` / `_DB_PATH` / `_landing_store` / `_land_lock` / `_landings()` /
`_ensure_loaded_async()` 都是 `reschedule` 的**可变模块状态**。测试与运行时靠
`monkeypatch.setattr(reschedule, "_db", db)` 把整条链路指向临时库
（`tests/test_landing_endpoints.py` 的 `env`、`tests/test_event_loop_blocking.py`
的 `_land_env`）。若这里写 `from app.api.reschedule import _db`，拿到的是
**导入那一刻**的对象 —— 之后所有 patch 全部失明，表现为"测试通过、线上写错库"，
或者"patch 了但落地仍写进真库"。所以一律 `_api._db`，每次调用现取。

⚠️ `_land_lock` 同时是 `_db.entries` 的写锁（`_DB_WRITE_LOCK`，R28）。它必须与
`reschedule` 里上传换表、`apply_move` 用的是**同一个锁对象** —— 这也是不能
复制一份过来的原因：复制 = 两把锁 = 锁等于没锁。

依赖方向（单向，导入期不成环）
------------------------------
本模块 → `reschedule`（取状态、`export_gate`、`_ADMIN_ONLY`）。
反向只有 `reschedule.__getattr__` 里的**惰性**转发，导入期不触发。
`app/main.py` 里两者按序导入（reschedule 先、landing 后）。
"""

import json
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from app import plan_landing
from app.api import reschedule as _api
from app.ratelimit import rate_limit
from app.security import require_admin

router = APIRouter()

# ---------------------------------------------------------------------------
# 方案落地（预览 → 采纳 → 撤销）
#
# 为什么需要：全校一键算出的方案此前只能看、只能导出 —— 安排表（db.entries）
# 还是原来上传的那份，看板 / 班级课表 / 数据体检用的都是旧数据，教秘必须把
# Excel 人工录回教务系统。"排完了"和"能交付"之间差最后一公里。
#
# 三步的理由：这改的是**真实排课数据**（433 → 410 不是数字变化，是某些行的
# 时间/教师/场地被正式改写）。所以① 先出差异预览（不写入）② 教秘确认后才写
# ③ 写完可撤销（恢复落地前快照）。采纳时 diff **重新计算**：预览与采纳之间
# 若有审批调课生效，按旧 diff 写入就是偷换（R-36：写路径的不实必须可见）。
#
# 鉴权边界：预览/状态只读（随 /parallel/** 免鉴权）；采纳/撤销是写
# （与 /upload、/apply-move 一致挂 require_admin）。
#
# 粒度说明：只有**班级×项目×学生组**口径的方案（全校一键 / 单班并行排课）
# 能落地 —— 与安排表同构。首屏「一键自动排课」的初稿是教师×课程口径、
# 没有学生组，落进安排表会凭空编造组别，故不支持（前端只给全校方案开放入口）。
# ---------------------------------------------------------------------------

class LandPlanRequest(BaseModel):
    """落地方案请求。与 /parallel/export 同构：方案由前端带上，服务端不存会话态。"""
    schedule: list
    class_name: Optional[str] = None   # 单班方案补班级名（全校一键结果已自带）
    note: Optional[str] = None


def _land_plan_or_400(req: LandPlanRequest) -> List[dict]:
    """校验方案行。缺字段的行**报错列出**，绝不静默丢弃（R-35 同族）。"""
    try:
        plan = plan_landing.normalize_plan(req.schedule, default_class=req.class_name)
    except plan_landing.PlanValidationError as e:
        raise HTTPException(
            status_code=400,
            detail=(f"方案有 {len(e.bad)} 行缺少必填字段（班级/项目/组/周次/星期/时段），"
                    f"未做任何改动。问题行示例：{e.bad[:3]}"))
    if not plan:
        raise HTTPException(
            status_code=400,
            detail="方案为空：没有可落地的排课行。请先在「并行排课」中生成方案，再回到这里落地方案。",
        )
    return plan


@router.post("/parallel/land/preview", summary="落地预览：方案与当前安排表的差异（不写入）",
             dependencies=[Depends(_api.export_gate), Depends(rate_limit("parallel-land"))])
async def land_preview(req: LandPlanRequest):
    """差异预览：新增 / 调整 / 未覆盖 / 原样保留四类账 + 警告。绝不写入。"""
    await _api._ensure_loaded_async()
    plan = _land_plan_or_400(req)
    diff = plan_landing.diff_plan(plan, _api._db.entries)
    diff["success"] = True
    return diff


def _locked_land_commit(plan: List[dict], note: str, actor: str):
    """落地的阻塞主体（跑在线程池里）：`with _land_lock:` 里全是同步活。

    R75：为什么锁必须在**工作线程**里拿
    ------------------------------------
    这段以前直接写在 `async def land_commit` 体内 —— 而 `_land_lock` 是同步的
    `threading.Lock`。于是事件循环会被按在地上，两种情形都算：

    ① **等锁**：`_land_lock` 同时是 `_db.entries` 的写锁（`_DB_WRITE_LOCK`，R28）。
       另一个请求（调课 / 审批 / 撤销）正在锁内跑，这里就在**事件循环线程**上原地等。
    ② **锁内的 sqlite 写入**：`save()` 遇到锁竞争要等满 busy_timeout。R74 实测
       单次 5.50 s、`undo` 两次尝试 11.04 s（`R74-new/probe_lock.py`）。

    实测（R74 `probe_loop.py`，50 ms 心跳）：真锁 + 同步（即原来的形状）心跳最大滞后
    **11076 ms**；同样时长的等待丢进工作线程后 **14 ms**。一次被锁的落地/撤销 =
    整站 11 秒不响应，并发请求、健康检查、SSE 心跳全部排队。

    ⚠️ 反例（很容易写错的那种）：把 `with _land_lock:` 留在 async 函数里，
    再在锁内 `await run_in_threadpool(...)` —— 锁仍然是在**事件循环线程**上等到的，
    ①②一个都没解决。锁一定要和被保护的活待在**同一条工作线程**里。

    锁内顺序不能动：diff 现算（不信预览那一份，预览与采纳之间可能有审批调课生效）
    → snapshot（撤销用）→ apply → save（失败 restore 再抛）。
    """
    with _api._land_lock:
        diff = plan_landing.diff_plan(plan, _api._db.entries)
        before = plan_landing.snapshot(_api._db)
        applied = plan_landing.apply_plan(_api._db, plan)
        try:
            rec = _api._landings().save(plan, before, note=note, actor=actor)
        except Exception as e:
            plan_landing.restore(_api._db, before)
            raise HTTPException(
                status_code=500,
                detail=f"落地方案落盘失败，已回滚内存改动（安排表未被修改）：{e}")
    return diff, rec, applied


@router.post("/parallel/land", summary="落地采纳：把方案写回安排表（需管理员）",
             dependencies=_api._ADMIN_ONLY + [Depends(rate_limit("parallel-land"))])
async def land_commit(req: LandPlanRequest, land_by: str = Depends(require_admin)):
    """写入安排表并落盘（landed_plans 表）。落盘失败回滚内存改动。

    为什么落盘失败要回滚内存：否则"内存已改、重启丢失"，教秘会在下一次刷新后
    看到方案神秘消失 —— 与 R-36「审批通过课表一个字节没变」同族的不实。

    R75：锁 + 落盘整段下沉线程池（`_locked_land_commit`）。此前它是 `async def`
    里同步 `with _land_lock:`，锁竞争时把**事件循环**一起按住（实测 11076 ms）。
    """
    await _api._ensure_loaded_async()
    plan = _land_plan_or_400(req)
    diff, rec, applied = await run_in_threadpool(
        _locked_land_commit, plan, req.note or "", land_by or "")
    diff.update({"success": True, "landed_at": rec["landed_at"],
                 "after_entries": applied["total"],
                 # R58：谁落的 —— 撤销/历史面板要能对上人
                 "landed_by": rec.get("actor") or land_by or ""})
    # R60：这条链路唯一拿不到人话回执的一步（R50 断点②）。四个差异账、长实验
    # 截短、能不能撤，全部由 `landing_receipt` 一次说完。放在 update 之后算 ——
    # 用的是**实际写入后**的 after_entries，不是 diff 推算值。
    diff["message"] = plan_landing.landing_receipt(
        diff, landed_at=rec["landed_at"], actor=diff["landed_by"])
    return diff


def _locked_land_undo(actor: str) -> dict:
    """撤销的阻塞主体（跑在线程池里）。理由与 `_locked_land_commit` 同一条。

    R75 实测（R74 取证脚本）：另一条连接 `BEGIN IMMEDIATE` 持锁时，`undo` 里的
    `revoke` 单次等 **5.50 s** 后抛 `database is locked`，重试再一次 ⇒ 整个 `undo`
    **11.04 s**；而这段时间原先整段压在事件循环上（心跳最大滞后 11076 ms）。
    下沉之后 `undo` 仍要 11.04 s（sqlite 的 busy_timeout 没变），但事件循环只滞后 14 ms。

    锁内做两件事：撤销本身 + **在锁内**读 `latest()` 算回执。
    后者必须在锁内：锁外读会被并发落地插队，回执说的就不是"这次撤销之后"的状态。
    """
    with _api._land_lock:
        out = plan_landing.undo(_api._db, _api._landings(), actor=actor)
        if out.get("success"):
            # R73：撤销是"弹一条"，不是"清空" —— 库里可能还留着**更早**的落地。
            # 回执必须说清"撤销之后还剩什么"，否则横幅写"已撤销落地"、下面的状态栏
            # 照旧写"已是落地方案"，同一屏两句话互相打脸（实测过的那个组合）。
            out["message"] = plan_landing.undo_receipt(
                out, remaining=_api._landings().latest())
    return out


@router.post("/parallel/land/undo", summary="撤销最后一次落地（需管理员）",
             dependencies=_api._ADMIN_ONLY + [Depends(rate_limit("parallel-land"))])
async def land_undo(actor: str = Depends(require_admin)):
    """恢复落地前快照并弹出记录。落地之后生效的调课会一并回滚 —— 计数如实报出。

    `actor` 由 `require_admin` 依赖注入（它返回管理员账号）——
    撤销是一条**要追责的写操作**，不记"谁撤的"就等于没留痕。

    失败分两型（R61）：
    - `no_record`：库里没有可撤销的记录 → **404**，这是"资源不存在"，语义正确。
    - `revoke_failed`：课表**已经**回滚到落地前，只是撤销标记没写进 sqlite（重试
      两次都失败）→ **500**。此前这里一律 404，于是三种代价同时发生：
      ① 界面写"没有可撤销的记录"，而安排表其实已经退了；
      ② `rolled_back: true`、`restored_entries` 这些**已发生事实**随异常一起丢掉，
         前端无从判断"该不该重试撤销"（该），只会看到一句 generic 报错；
      ③ 教秘以为撤销没生效，转头去"重新落地"，把刚退掉的方案又落回去。
      所以这一型返回 500 + 完整结构，只额外补一个 `detail` 字符串给通用
      `describeApiError` 读取（前端不认结构也能拿到人话）。

    R73：成功分支补 `message`（`plan_landing.undo_receipt`）。此前成功只回字段，
    前端自己拼"已撤销落地，安排表恢复到 N 条" —— 既不说撤的是哪一次，也不说
    撤销之后**还有更早一次落地在生效**（此时 `/land/status` 仍报 landed=True）。

    R75：锁 + 撤销整段下沉线程池（`_locked_land_undo`）。此前是 `async def` 里同步
    `with _land_lock:`，而 `undo` 在锁竞争下要等两次 busy_timeout（实测 11.04 s）——
    这 11 秒里事件循环完全停摆（心跳最大滞后 11076 ms）。
    """
    await _api._ensure_loaded_async()
    out = await run_in_threadpool(_locked_land_undo, actor or "")
    if out.get("success"):
        return out
    if out.get("error_kind") == "no_record":
        raise HTTPException(status_code=404, detail=out["error"])
    return JSONResponse(status_code=500, content={**out, "detail": out["error"]})


@router.get("/parallel/land/history", summary="落地历史（含已撤销，需管理员）",
            dependencies=_api._ADMIN_ONLY + [Depends(rate_limit("parallel-land-status"))])
async def land_history(limit: int = 20):
    """最近的落地记录，**含已被撤销的**。

    为什么单独开这个接口：`/land/status` 只回答"现在有没有落地方案"。
    排查时要回答的是另一类问题 —— "上周谁落过方案、后来又撤了没有"。
    撤销不再删记录之后（R55），这两个问题的答案分别来自 `latest()` 和 `history()`。

    ⚠️ 这个端点曾被并发会话的一次推送整段删掉（对方拿着旧的 reschedule.py 覆盖），
    表现为前端历史面板**静默消失**（面板对 404 做了降级）—— 又是一个"没有报错的
    功能消失"。恢复时按"远端原文 + 补丁"重建，见 R58 台账。
    """
    await _api._ensure_loaded_async()
    rows = _api._landings().history(limit=limit)
    return {"success": True, "total": len(rows),
            "active": sum(1 for r in rows if not r["revoked"]),
            "revoked": sum(1 for r in rows if r["revoked"]),
            "history": rows}


@router.get("/parallel/land/status", summary="当前落地状态（供界面显示撤销入口）",
            dependencies=[Depends(rate_limit("parallel-land-status"))])
async def land_status():
    """是否已落地、何时、多少条。_ensure_loaded 保证重启重放之后再回答。"""
    await _api._ensure_loaded_async()
    rec = _api._landings().latest()
    if not rec:
        return {"landed": False}
    return {"landed": True, "landed_at": rec["landed_at"],
            "plan_rows": len(json.loads(rec["plan_json"])),
            "after_entries": rec["after_entries"], "note": rec["note"],
            # R58：界面显示撤销入口时，顺便说清"这是谁落的"
            "landed_by": rec.get("actor") or ""}
