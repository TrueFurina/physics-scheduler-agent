"""统计 / 体检 / 影响分析的只读视图（R79 从 `app/api/reschedule.py` 搬出）。

为什么单独一个模块
------------------
`reschedule.py` 一度 3493 行、六族职责混在一起。R76 搬走落地方案、R77 搬走主数据
维护、R78 搬走导出，这一轮搬第四族：**对同一份内存排课表的四种只读投影**。

| 端点 | 投影 |
|---|---|
| `/data-audit` | 冲突体检 + 数据缺口 |
| `/stats` | 教室利用率 / 教师工作量 |
| `/schedule-grid` | 某周 星期×时段 网格 |
| `/impact-analysis` | "调到第 N 周"会撞到谁 |

它们**没有共享缓存**（与导出族不同），但共享两样东西：`_db` 与 `_master_data`。
所以本模块一行状态都不许自己存 —— 一律 `_api.xxx` 现取。写成
`from app.api.reschedule import _db` 会让所有 `monkeypatch.setattr(reschedule,
"_db", ...)` 静默失明：测试全绿，量的却是另一份数据。

没搬：`ChatRequest`（`/chat` 仍在用，留在 reschedule）、`_agent`（全局唯一，
由 `/chat` 与 `/experiment-tutor` 共用）。二者都按模块属性现取。

不变量（搬完必须仍然成立）
--------------------------
1. 四条路由的**路径 / 方法 / 函数名 / dependencies** 一字未变，openapi 不变。
2. 状态现取、同一对象，见 `tests/test_insights_module_split.py`。
3. `/impact-analysis` 的 `parse_request` 仍在线程池上（R-19 第 6 处：规则解析
   不出来时同步打 LLM，上限 60s，会拖死整个事件循环）。
4. 旧名经 `reschedule.__getattr__` 惰性转发 —— 5 个测试文件里有 7 处模块属性直调。
5. ~~M50 变异锚点跟着搬~~ —— **R80 更正：这是 R79 的错误推断**。M50 的锚点
   （`if t.teacher: teacher_load[t.teacher] += 1`）实测在 `reschedule.py:929`
   的教师角色补全里，从未离开旧模块；56 条锚点由
   `test_master_module_split.py::test_every_mutation_anchor_still_matches_its_file`
   逐条守着，本轮一条都没动。真正随 R80 工单族搬家迁移靶文件的是 **M15 / M16**。
"""

from fastapi import APIRouter, Depends, HTTPException
from starlette.concurrency import run_in_threadpool

from app.api import reschedule as _api
from app.ratelimit import rate_limit

router = APIRouter()


def _course_gap_note(cov: dict) -> str:
    """项目不在主数据里的那部分记录：它们的课程是**回落默认**，不是查出来的。

    真实表就有：主数据只建了 22 个项目，而安排表有 25 个项目名 —— 3 个绪论类
    项目（共 17 条）不在主数据里。教秘在台账里看不到它们，也就无法给它们指定
    课程；若哪天把绪论课改归另一门课，这 17 条不会跟着变。这必须说出口 ——
    沉默地让一部分记录"查不出课程"，比报一个假冲突更危险（R-35 同族）。
    """
    if not cov.get("master_ready"):
        return ""
    gap = cov.get("total_entries", 0) - cov.get("project_in_master", 0)
    if gap <= 0:
        return ""
    return (f"（⚠️ 有 {gap} 条记录的项目不在主数据中 —— 多为绪论课，其课程按默认课处理，"
            f"并非查得；台账里也看不到它们，无法单独指定课程）")


@router.get("/data-audit", summary="数据体检报告（扫描现有安排表冲突）")
async def data_audit():
    """一键审计现有实验安排表：教室冲突 / 教师时间冲突 / 数据覆盖情况"""
    await _api._ensure_loaded_async()


    weeks, teachers, rooms = set(), set(), set()
    missing_room = []                # 无场地条目：不能参与"同室"判定，但要作为数据缺口报出来

    for e in _api._db.entries:
        weeks.add(e.week)
        if e.teacher:
            teachers.add(e.teacher)
        if (e.room or '').strip():
            rooms.add(e.room)
        else:
            # ⚠️ 曾经无条件放进 room_map：于是一堆"空场地"被归到同一个 key
            # (week, weekday, slot, '')，凡两条以上就报"教室冲突（同室同段多班）"，
            # 而 place 是空串 —— 界面上显示成"第1周 周五 18:30-20:45 ·"，场地空着。
            # 没填场地不可能撞场地，这是纯假阳性；真正该说的是"这些条没场地"。
            # 冲突扫描本身已移到 `conflict_model.scan_table`（空场地同样不进图）。
            missing_room.append(e)

    from app.conflict_model import scan_table

    # 冲突分级（2026-10-22，导师业务规则）：三类核心冲突场地/教师/**整组**分别扫描，
    # 每条带 severity + 处理建议。整组维度此前完全没查 —— 调课把 A 组的课挪到
    # 该组已有课的格子时，系统会一声不吭地制造"一个组分身两处"。
    scan = scan_table(_api._db.entries)
    room_conflicts = scan["room_conflicts"]
    teacher_conflicts = scan["teacher_conflicts"]
    group_conflicts = scan["group_conflicts"]

    # 跨课程撞车（导师点名的后续难点）：同一班同一组同一格却分属两门课程。
    # ⚠️ 它是 group 冲突的**子集** —— 同班同组同格必然已被 group 维度抓到，
    # 所以这里**不并入 core_total**（否则同一处冲突被数两遍），只给 group 冲突
    # 打 `cross_course` 标记并单独计数：跨课程的两门课任课教师互不知情，
    # 谁都不会觉得自己排错了，比同学科内的撞格更难被发现。
    _api._ensure_master_data()
    from app.course_bridge import scan_cross_course
    cross = scan_cross_course(_api._db.entries, _api._master_data)
    cross_conflicts = cross["conflicts"]
    course_cov = cross["coverage"]
    cross_keys = {(c["week"], c["weekday"], str(c["time_slot"]), c["place"])
                  for c in cross_conflicts}
    for c in group_conflicts:
        c["cross_course"] = (c["week"], c["weekday"], str(c["time_slot"]), c["place"]) in cross_keys

    # 数据缺口（不是冲突）：这些条目没填场地，教室冲突扫描覆盖不到它们。
    # 单独报出来 —— 沉默地少扫 16 条，比报一个假冲突更危险。
    missing_samples = [
        f"{e.class_name}/{e.group} {e.experiment}（第{e.week}周 {e.weekday}）"
        for e in missing_room[:5]
    ]

    # 时段看不懂、已被剔除的行：它们没进冲突图（不知道具体节次），
    # 但只要它们占着教室，那个教室当天其实不空 —— 沙盘按"整日占用"处理了，
    # 体检这里必须把原文摊出来，否则教秘会以为冲突扫描覆盖了全表。
    unparsed = list(getattr(_api._db, "skipped", None) or [])
    unparsed_samples = [
        f"第{s['week']}周 {s['weekday']} {s.get('class_name') or ''} @ {s.get('room') or '未填场地'}"
        f"（时段原文「{s.get('raw_ts')}」）"
        for s in unparsed[:5]
    ]

    core_total = len(room_conflicts) + len(teacher_conflicts) + len(group_conflicts)
    return {
        "success": True,
        "data": {
            "healthy": core_total == 0,
            "total_entries": len(_api._db.entries),
            "teacher_count": len(teachers),
            "room_count": len(rooms),
            "week_min": min(weeks) if weeks else 0,
            "week_max": max(weeks) if weeks else 0,
            "room_conflict_count": len(room_conflicts),
            "teacher_conflict_count": len(teacher_conflicts),
            "group_conflict_count": len(group_conflicts),
            "core_conflict_count": core_total,
            # 同班**不同组**同格并行：正常的分批教学（真实表 47 处），不是冲突。
            # 必须说出来 —— 教秘自己按班级一数会看到重叠，若体检只报"0 冲突"
            # 而不解释这 47 处，他就不会再信这个体检。
            "parallel_group_count": scan["parallel_groups"],
            "parallel_note": (
                f"另有 {scan['parallel_groups']} 处「同班级不同组同一时段」属正常分批教学"
                f"（如 1 组在 A504 做扭摆法、2 组在 A516 做声速测量），不计入冲突"
                if scan["parallel_groups"] else ""),
            # 跨课程口径：**必须说清它是子集**，否则教秘看到「整组 5 处 + 跨课程 3 处」
            # 会以为一共 8 处。同时把「查没查得出来」摊开 —— 课程是项目名反推的，
            # 主数据没初始化时全部回落默认课，跨课程恒 0 处，那是「没查」不是「没问题」
            # （R-35「时段看不懂就整行静默丢弃」同族错误）。
            "cross_course_conflict_count": len(cross_conflicts),
            "cross_course_note": (
                f"整组冲突中有 {len(cross_conflicts)} 处属跨课程撞车（同一班同一组在两门"
                f"课程的安排撞车；已含在上面的整组计数里，不重复计）。两门课的任课教师"
                f"互不知情，需协同改时间"
                if cross_conflicts else
                f"当前共 {course_cov['distinct_courses']} 门课程，未出现同班同组跨课程撞车"
                + ("" if course_cov["master_ready"]
                   else "（⚠️ 主数据未初始化，课程归属全部回落默认课，跨课程维度未真正生效）")
                + _course_gap_note(course_cov)),
            "course_coverage": course_cov,
            "missing_room_count": len(missing_room),
            "missing_room_samples": missing_samples,
            "unparsed_slot_count": len(unparsed),
            "unparsed_slot_samples": unparsed_samples,
            "room_conflicts": room_conflicts[:30],
            "teacher_conflicts": teacher_conflicts[:30],
            "group_conflicts": group_conflicts[:30],
            "cross_course_conflicts": cross_conflicts[:30],
        },
    }


@router.get("/stats", summary="统计面板（教室利用率/教师工作量）")
async def stats():
    """统计现有安排表：教室利用率、教师工作量均衡度"""
    await _api._ensure_loaded_async()

    from collections import defaultdict, Counter

    weeks = {e.week for e in _api._db.entries}
    n_weeks = (max(weeks) - min(weeks) + 1) if weeks else 0

    # ⚠️ 分母曾经写死 `n_weeks * 5 * 3`（5 天 × 3 时段），而真实表**周末照常排课**
    # （433 条里周六 36、周日 35，占 16%；"24电信 42/46 节在周末"是常态）且存在上午时段
    # （MORNING 4 条）。把这两类排除在分母外 ⇒ 利用率系统性虚高：
    # 实测 17.6% vs 真实 9.4%（分母 240 vs 448/间），教秘会据此误判"场地还很空"。
    # 与 R-41 同族：拿"枚举里有的"（5 天 3 时段）当"现实里有的"（7 天 4 时段）。
    # 现在按**本表实际开放**的星期/时段推导，并把口径一并返回，界面上写清楚。
    schedulable_days = len({e.weekday for e in _api._db.entries if e.weekday}) or 5
    schedulable_slots = len(
        {s for e in _api._db.entries for s in (getattr(e, "blocked", None) or [e.time_slot]) if s}
    ) or 3
    total_slots = n_weeks * schedulable_days * schedulable_slots
    capacity_basis = f"{n_weeks} 周 × {schedulable_days} 天 × {schedulable_slots} 时段 = {total_slots} 格/间"

    room_sessions = Counter()
    room_weeks = defaultdict(set)
    teacher_sessions = Counter()
    teacher_experiments = defaultdict(set)
    teacher_weeks = defaultdict(set)

    for e in _api._db.entries:
        if e.room:
            # 按真实占用格数计：一条 `14:00-17:00` 同时占 567+678，算 2 格而不是 1 节。
            room_sessions[e.room] += len(getattr(e, "blocked", None) or [e.time_slot])
            room_weeks[e.room].add(e.week)
        if e.teacher:
            teacher_sessions[e.teacher] += 1
            teacher_experiments[e.teacher].add(e.experiment)
            teacher_weeks[e.teacher].add(e.week)

    rooms_stat = [
        {
            "room": rm,
            "sessions": n,
            "utilization": round(n / total_slots * 100, 1) if total_slots else 0,
        }
        for rm, n in room_sessions.most_common()
    ]
    teachers_stat = [
        {
            "teacher": t,
            "sessions": n,
            "experiments": len(teacher_experiments[t]),
            "active_weeks": len(teacher_weeks[t]),
        }
        for t, n in teacher_sessions.most_common()
    ]
    loads = [t["sessions"] for t in teachers_stat]
    avg_load = sum(loads) / len(loads) if loads else 0
    balance = round((1 - (max(loads) - min(loads)) / max(loads)) * 100) if loads and max(loads) > 0 else 100

    return {
        "success": True,
        "data": {
            "n_weeks": n_weeks,
            "room_count": len(rooms_stat),
            "teacher_count": len(teachers_stat),
            # 口径必须和利用率一起给出：只说"9.4%"而不说分母怎么来的，
            # 教秘无法判断这个数字对自己有没有意义（R-38 同族：算出来的数要说清没算什么）
            "capacity_basis": capacity_basis,
            "schedulable_days": schedulable_days,
            "schedulable_slots": schedulable_slots,
            "avg_room_utilization": round(sum(r["utilization"] for r in rooms_stat) / len(rooms_stat), 1) if rooms_stat else 0,
            "busiest_room": rooms_stat[0] if rooms_stat else None,
            "avg_teacher_load": round(avg_load, 1),
            "teacher_load_balance": balance,
            "top_rooms": rooms_stat[:10],
            "top_teachers": teachers_stat[:10],
        },
    }


@router.get("/schedule-grid", summary="周课表网格：某一周的 星期×时段 排课全景")
async def schedule_grid(week: int, class_name: str = "", room: str = ""):
    """把某一周的安排表重排成网格（README 路线图：排课结果可视化）。

    与导出的关系：导出给教务系统/打印用，这个给**当场看**用——落地（R-61）
    之后教秘要确认"方案真的写进安排表了"，看板只有统计数字，网格才是
    一眼可见的那一面。

    口径（详见 `app/schedule_grid.py` 模块头）：
    - 7 天 × 4 时段全列（周末 71 条、上午 4 条不能丢——R-41）；
    - 跨节次记录（`blocked` 如 ["567","678"]）在每个占用的节次各出现一次（R-40）；
    - 没填场地的记录照常入格并标注（R-35：不静默丢）；
    - 冲突标记来自 `conflict_model.scan_table`（唯一权威），本端点不重算；
    - 周次超出覆盖范围直接 400 并说清范围（R-32：不夹取）。
    """
    await _api._ensure_loaded_async()

    from app import schedule_grid
    from app.conflict_model import scan_table

    try:
        grid = schedule_grid.build_grid(
            _api._db.entries,
            week=week,
            class_name=class_name,
            room=room,
            conflicts=scan_table(_api._db.entries),
            skipped=getattr(_api._db, "skipped", None) or [],
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"success": True, "data": grid}


@router.post("/impact-analysis", summary="冲突影响分析（差异化功能）",
             dependencies=[Depends(rate_limit("impact-analysis"))])
async def impact_analysis(req: _api.ChatRequest):
    """调课影响分析：把某课调到第N周，会影响哪些班级/教室/教师

    基于三重冲突筛选结果，聚合出影响范围清单。
    """
    await _api._ensure_loaded_async()

    # 解析请求（复用调课智能体解析）
    #
    # ⚠️ 必须走线程池：`parse_request` 在**规则解析不出周次时会同步打 LLM**
    # （`httpx.Client(timeout=60)`），而它是 `async def` 路由里的一次普通调用。
    # 实测（LLM 后端 1s 延迟）：整段 1002ms，心跳被拖 1007ms —— 期间 `/health`、
    # 别人的页面全部排队；而上限是 60s。
    # 这是 R-19 那次"点修 5 处"漏掉的第 6 处，主路径（模板表单填好的句子）规则能解析，
    # 所以探针与单测一直是绿的 —— 只有当教秘在自由输入框里说人话才撞上。
    # 规则路径实测 1.7ms，多付一次线程切换（~0.1ms）换掉这个上限，值得。
    parsed = await run_in_threadpool(_api._agent.parse_request, req.message)
    if not parsed["target_week"]:
        raise HTTPException(status_code=400, detail="无法确定目标周次，请说明如'调到第5周'")

    target_week = parsed["target_week"]
    teacher_name = parsed.get("teacher_name")

    # 周次范围校验（R-32 的另一半）：影响分析同样按 `e.week == target_week` 过滤，
    # 周次落在表外时"冲突数 = 0"，而 0 冲突会被读成"这周完全没影响"——
    # 比报错更危险。这里直接 400，把"查不了"和"没影响"分开。
    _bounds = _api._agent.week_bounds()
    if _bounds and not (_bounds[0] <= target_week <= _bounds[1]):
        raise HTTPException(
            status_code=400,
            detail=(
                f"第{target_week}周不在实验安排表范围内"
                f"（本表覆盖第{_bounds[0]}–{_bounds[1]}周），无法做影响分析"
            ),
        )

    # 冲突筛选（含原因）
    result = _api._agent.find_slots_with_reasons(target_week, teacher_name=teacher_name)
    excluded = result["excluded"]
    # R99：学期起始日读失败的降级信号必须跟着冲突明细一起走。
    # 判据沿用 R98 —— 只认 detail（真读失败才有值）；表里没写起始日返回
    # None 是设计回退，老形状 result 不带顶层字段时同样安静，不误报。
    date_degraded_detail = result.get("date_degraded_detail") or None
    date_degraded = date_degraded_detail is not None

    # 聚合影响范围
    #
    # ⚠️ 这里**不允许**再从 `reason` 文案里正则反解字段（历史实现用
    # `re.search(r"（([^）]+) ", e["reason"])` 捞括号内容，结果把班级名塞进了
    # 「受影响教师」——因为 teacher_busy 的文案里本来就没有教师名）。
    # 文案是给人读的，改个标点就静默解析失败；数据必须来自结构化字段。
    affected_classes = set()      # 受影响班级（教室被占）
    affected_rooms = set()        # 受影响教室
    affected_teachers = set()     # 受影响教师（占着那间教室的教师）
    self_busy = []                # 教师本人该时段已有课 —— 是「自身冲突」，不是「别人受影响」
    conflict_slots = []

    for e in excluded:
        reason_type = e.get("reason_type", "")
        conflict_slots.append({
            "weekday": e["weekday"], "time_slot": e["time_slot"],
            "room": e.get("room", ""), "date": e.get("date", ""),
            "reason_type": reason_type, "reason": e["reason"],
            "date_degraded": e.get("date_degraded", False) is True,
        })
        if reason_type == "classroom_busy":
            affected_rooms.add(e.get("room", ""))
            if e.get("class_name"):
                affected_classes.add(e["class_name"])
            if e.get("teacher"):
                affected_teachers.add(e["teacher"])
        elif reason_type == "teacher_busy":
            self_busy.append({
                "weekday": e["weekday"], "time_slot": e["time_slot"],
                "class_name": e.get("class_name", ""),
                "experiment": e.get("experiment", ""),
            })

    return {
        "success": True,
        "target_week": target_week,
        "message": f"将课程调到第{target_week}周的影响分析：",
        "data": {
            "conflict_count": len(conflict_slots),
            "affected_classes": sorted(a for a in affected_classes if a),
            "affected_rooms": sorted(r for r in affected_rooms if r),
            "affected_teachers": sorted(t for t in affected_teachers if t),
            "self_busy_count": len(self_busy),
            "self_busy_slots": self_busy[:5],
            "conflict_slots": conflict_slots[:10],
            "date_degraded": date_degraded,
            "date_degraded_detail": date_degraded_detail,
        },
    }
