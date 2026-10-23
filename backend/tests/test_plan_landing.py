"""方案落地（预览 → 采纳 → 撤销）回归测试。

锁死的契约
----------
1. diff 口径：新增/调整/未覆盖/原样保留四类账齐全；**按 blocked 比**不按主标签
   （14:00-17:00 占 567+678 两节，只比主标签会漏掉半节——R-40 在落地层的回归）；
2. 跨时段长实验（原表同周同天地占多行）被方案截短时**必须单列警告**——
   真实表里有 6 个这样的单元，埋在 400 条差异里等于静默删课；
3. 绪论课/考试类行原样保留，绝不被方案替换；
4. 采纳真的写进 db.entries（含索引重建），且**重启重放后状态一致**；
5. 写事件按时间戳顺序重放：落地之后的调课不被吞，调课之后的落地不被抹；
6. 撤销恢复落地前快照，落地之后生效的调课如实计数；
7. 采纳/撤销需管理员；预览/状态免鉴权（教秘主路径）。
"""

import asyncio
import json
import os
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import plan_landing
from app.api import reschedule as api
from app.main import app
from app.master_data import MasterData
from app.security import issue_admin_token
from app.utils.excel_parser import PendingTask, ScheduleDatabase, ScheduleEntry

# ---------------------------------------------------------------- 夹具数据

def _entry(week, weekday, slot, cls, group, room, teacher, experiment, blocked=None, time_raw=""):
    return ScheduleEntry(week, weekday, slot, cls, group, experiment, room, teacher,
                         time_raw=time_raw, blocked=blocked or [slot])


def _base_entries():
    """7 行：4 个可排单元（其中「长实验」占两个时段）+ 绪论 + 考试。"""
    return [
        _entry(2, "周一", "567", "甲班", "1组", "A410", "吴老师", "项目A"),
        _entry(2, "周一", "678", "甲班", "2组", "A410", "林老师", "项目A"),
        _entry(3, "周二", "567", "甲班", "1组", "A412", "吴老师", "项目B"),
        _entry(4, "周三", "567", "甲班", "1组", "A504", "林晨", "长实验"),
        _entry(4, "周三", "678", "甲班", "1组", "A504", "林晨", "长实验"),
        _entry(5, "周四", "91011", "甲班", "1组", "", "", "绪论课（教室已安排）"),
        _entry(6, "周四", "567", "甲班", "1组", "A413", "", "项目A考试"),
    ]


def _plan_a():
    """求解器风格：每单元恰好一行（长实验也只拿一个时段）。"""
    return [
        {"class_name": "甲班", "project": "项目A", "group": "1组", "week": 1,
         "weekday": "周一", "time_slot": "567", "room": "A410", "teacher": "吴老师"},
        {"class_name": "甲班", "project": "项目A", "group": "2组", "week": 1,
         "weekday": "周一", "time_slot": "678", "room": "A410", "teacher": "林老师"},
        {"class_name": "甲班", "project": "项目B", "group": "1组", "week": 2,
         "weekday": "周二", "time_slot": "567", "room": "A412", "teacher": "吴老师"},
        {"class_name": "甲班", "project": "长实验", "group": "1组", "week": 4,
         "weekday": "周三", "time_slot": "567", "room": "A504", "teacher": "林晨"},
    ]


def _db(entries, pending=None):
    d = ScheduleDatabase()
    d.entries = list(entries)
    d.pending = list(pending or [])
    d.skipped = []
    d.loaded = True
    return d


def _write_xlsx(path, rows):
    """安排表格式：第 3 行起是数据，前 8 列＝周次/星期/时段/班级/组/项目/场地/教师。"""
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "大学物理实验安排表 (周) "
    ws.append(["表头占位1"])
    ws.append(["表头占位2"])
    for r in rows:
        ws.append(list(r))
    wb.save(path)


def _xlsx_rows():
    return [(2, "周一", "567", "甲班", "1组", "项目A", "A410", "吴老师"),
            (2, "周一", "678", "甲班", "2组", "项目A", "A410", "林老师"),
            (3, "周二", "567", "甲班", "1组", "项目B", "A412", "吴老师"),
            (4, "周三", "567", "甲班", "1组", "长实验", "A504", "林晨"),
            (4, "周三", "678", "甲班", "1组", "长实验", "A504", "林晨"),
            (5, "周四", "91011", "甲班", "1组", "绪论课（教室已安排）", "", ""),
            (6, "周四", "567", "甲班", "1组", "项目A考试", "A413", "")]


def _admin():
    return {"Authorization": f"Bearer {issue_admin_token('admin')['token']}"}


@pytest.fixture
def env(tmp_path, monkeypatch):
    """隔离的 API 环境：夹具 db + 临时 sqlite（landed_plans/applied_moves 都落这里）。"""
    db = _db(_base_entries(), pending=[
        PendingTask("甲班", "项目B", "1组", teacher="吴老师"),      # 会被落地剪掉
        PendingTask("甲班", "还没排的项目", "1组", teacher="吴老师"),  # 不在方案里，保留
    ])
    md = MasterData()
    md.build_from_entries(db.entries)
    monkeypatch.setattr(api, "_db", db)
    monkeypatch.setattr(api, "_master_data", md)
    monkeypatch.setattr(api, "_master_tasks", [])
    monkeypatch.setattr(api, "_student", api.StudentSchedule())
    monkeypatch.setattr(api, "_DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setattr(api, "_landing_store", None)
    api._init_db()  # 在临时库上建 requests/applied_moves/landed_plans
    client = TestClient(app)
    return db, client, tmp_path


# ---------------------------------------------------------------- 1. diff 口径

def test_diff_counts_four_ledgers():
    d = plan_landing.diff_plan(_plan_a(), _base_entries())
    assert d["plan_units"] == 4 and d["plan_rows"] == 4
    assert d["before_entries"] == 7
    assert d["after_entries"] == 6          # 2 行非可排 + 4 行方案
    assert d["added"] == []                 # 方案单元都在表里
    assert len(d["changed"]) == 4           # 全部改期
    assert d["unchanged_units"] == 0
    assert d["uncovered"] == []             # 没有漏排的可排单元
    assert d["untouched_nonsched"] == 2     # 绪论 + 考试原样保留
    assert d["has_changes"] is True


def test_diff_flags_long_session_truncation():
    """长实验（同周同天地占 567+678）被方案截短 → 必须单列警告并标记。

    真实表里有 6 个这样的单元（24海资_1 全部实验）。埋在 400 条差异里
    等于静默删课 —— 校验器口径错与假绿同罪。
    """
    d = plan_landing.diff_plan(_plan_a(), _base_entries())
    assert len(d["long_sessions"]) == 1
    ls = d["long_sessions"][0]
    assert "长实验" in ls["unit"]
    assert ls["old_slots"] == ["567", "678"]
    assert ls["plan_slots"] == ["567"]
    assert any("长实验" in w and "截短" in w for w in d["warnings"])
    hit = [c for c in d["changed"] if "长实验" in c["unit"]]
    assert hit and hit[0]["long_session"] is True
    # 非长实验单元不得被误标
    others = [c for c in d["changed"] if "长实验" not in c["unit"]]
    assert all(c["long_session"] is False for c in others)


def test_diff_compares_blocked_not_primary_label():
    """R-40 在落地层的回归：14:00-17:00 主标签 567 但真实占 567+678。

    方案只排 567 ⇒ 必须判"调整过"（会丢掉 678 的占用）。若按主标签比，
    会被误判成"一致"，落地后那半节课的占用悄悄消失。
    """
    entries = [_entry(4, "周三", "567", "甲班", "1组", "A504", "林晨", "跨节次实验",
                      blocked=["567", "678"], time_raw="14:00-17:00")]
    plan = [{"class_name": "甲班", "project": "跨节次实验", "group": "1组", "week": 4,
             "weekday": "周三", "time_slot": "567", "room": "A504", "teacher": "林晨"}]
    d = plan_landing.diff_plan(plan, entries)
    assert d["unchanged_units"] == 0, "主标签相同但 blocked 不同，不能算一致"
    assert len(d["changed"]) == 1
    assert d["long_sessions"] and d["long_sessions"][0]["old_slots"] == ["567", "678"]


def test_diff_reports_uncovered_units_as_kept():
    """方案没覆盖的单元：保留原行 + 单列（求解未覆盖就删＝静默丢课）。"""
    plan = [r for r in _plan_a() if r["project"] != "项目B"]  # 少一个单元
    d = plan_landing.diff_plan(plan, _base_entries())
    assert len(d["uncovered"]) == 1 and "项目B" in d["uncovered"][0]["unit"]
    # 2 非可排 + 1 未覆盖行 + 3 方案行；长实验原占 2 行，方案只排 1 行（截短）
    assert d["after_entries"] == 6
    assert any("未覆盖" in w or "不在方案里" in w for w in d["warnings"])
    # 未覆盖场景下长实验截短警告仍须在（截短与漏排是两件独立的事）
    assert any("长实验" in ls["unit"] for ls in d["long_sessions"])


def test_normalize_reports_missing_fields_and_rejects_empty():
    with pytest.raises(plan_landing.PlanValidationError) as e:
        plan_landing.normalize_plan([{"class_name": "甲班", "project": "X"}])
    assert e.value.bad and "周次" in e.value.bad[0]["missing"]
    assert plan_landing.normalize_plan([]) == []


# ---------------------------------------------------------------- 2. 应用与快照

def test_apply_keeps_nonsched_and_rebuilds_index():
    db = _db(_base_entries())
    out = plan_landing.apply_plan(db, _plan_a())
    assert out["total"] == 6
    kept = {(e.experiment, e.week) for e in db.entries}
    assert ("绪论课（教室已安排）", 5) in kept and ("项目A考试", 6) in kept
    # 索引现场重建：旧占用（W2 周一 567 A410）必须消失，新占用（W1）必须在
    assert not [e for e in db._by_room.get("A410", []) if e.week == 2]
    assert [e for e in db._by_room.get("A410", []) if e.week == 1]


def test_apply_prunes_pending_for_landed_units_only():
    db = _db(_base_entries(), pending=[
        PendingTask("甲班", "项目B", "1组", teacher="吴老师"),
        PendingTask("甲班", "还没排的项目", "1组", teacher="吴老师"),
    ])
    plan_landing.apply_plan(db, _plan_a())
    left = {(t.class_name, t.experiment) for t in db.pending}
    assert ("甲班", "项目B") not in left, "已排掉的待排任务必须剪掉"
    assert ("甲班", "还没排的项目") in left, "方案没覆盖的待排任务必须保留"


def test_snapshot_restore_roundtrip():
    db = _db(_base_entries(), pending=[PendingTask("甲班", "项目B", "1组")])
    snap = plan_landing.snapshot(db)
    plan_landing.apply_plan(db, _plan_a())
    assert len(db.entries) == 6
    plan_landing.restore(db, snap)
    assert len(db.entries) == 7
    assert {(e.experiment, e.week) for e in db.entries} == \
           {(e.experiment, e.week) for e in _base_entries()}
    assert [t.experiment for t in db.pending] == ["项目B"]
    assert db._by_room.get("A410"), "恢复后索引必须重建"


# ---------------------------------------------------------------- 3. API：预览/鉴权

def test_preview_does_not_write(env):
    db, client, _ = env
    r = client.post("/api/reschedule/parallel/land/preview", json={"schedule": _plan_a()})
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True and body["has_changes"] is True
    assert len(db.entries) == 7, "预览绝不写入"
    assert api._landings().latest() is None, "预览绝不落盘"


def test_preview_and_status_are_public(env):
    _, client, _ = env
    assert client.get("/api/reschedule/parallel/land/status").status_code == 200
    r = client.post("/api/reschedule/parallel/land/preview", json={"schedule": _plan_a()})
    assert r.status_code != 401


def test_commit_and_undo_require_admin(env):
    _, client, _ = env
    assert client.post("/api/reschedule/parallel/land",
                       json={"schedule": _plan_a()}).status_code == 401
    assert client.post("/api/reschedule/parallel/land/undo").status_code == 401
    # 带有效令牌放行（撤销在无落地时报 404，那是业务态不是鉴权态）
    assert client.post("/api/reschedule/parallel/land",
                       json={"schedule": _plan_a()}, headers=_admin()).status_code == 200
    assert client.post("/api/reschedule/parallel/land/undo", headers=_admin()).status_code == 200


def test_invalid_plan_rejected_without_writing(env):
    db, client, _ = env
    bad = _plan_a() + [{"class_name": "甲班", "project": "坏行", "group": "1组"}]
    r = client.post("/api/reschedule/parallel/land",
                    json={"schedule": bad}, headers=_admin())
    assert r.status_code == 400
    assert "必填字段" in r.json()["detail"]
    assert len(db.entries) == 7 and api._landings().latest() is None


def test_empty_plan_rejected(env):
    _, client, _ = env
    r = client.post("/api/reschedule/parallel/land", json={"schedule": []}, headers=_admin())
    assert r.status_code == 400 and "为空" in r.json()["detail"]


# ---------------------------------------------------------------- 4. API：采纳/撤销

def test_commit_writes_entries_and_persists(env):
    db, client, _ = env
    r = client.post("/api/reschedule/parallel/land",
                    json={"schedule": _plan_a(), "note": "测试落地"}, headers=_admin())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["success"] is True and body["landed_at"]
    assert body["after_entries"] == 6
    assert len(db.entries) == 6
    assert {e.week for e in db.entries if e.experiment == "项目A" and e.group == "1组"} == {1}

    st = client.get("/api/reschedule/parallel/land/status").json()
    assert st["landed"] is True and st["plan_rows"] == 4 and st["note"] == "测试落地"


def test_commit_is_diffed_against_live_state(env):
    """采纳时 diff 现算：预览之后表被改过，不能按旧 diff 写入（防偷换）。"""
    db, client, _ = env
    # 先预览（拿到"全部调整"的账），再把表改成与方案一致
    prev = client.post("/api/reschedule/parallel/land/preview",
                       json={"schedule": _plan_a()}).json()
    assert prev["has_changes"] is True
    plan_landing.apply_plan(db, _plan_a())
    r = client.post("/api/reschedule/parallel/land",
                    json={"schedule": _plan_a()}, headers=_admin())
    assert r.status_code == 200
    assert r.json()["has_changes"] is False, "表现已一致时必须报无差异，不能谎报调整"


def test_undo_restores_before_snapshot(env):
    db, client, _ = env
    client.post("/api/reschedule/parallel/land",
                json={"schedule": _plan_a()}, headers=_admin())
    r = client.post("/api/reschedule/parallel/land/undo", headers=_admin())
    assert r.status_code == 200
    assert r.json()["restored_entries"] == 7
    assert {(e.experiment, e.week) for e in db.entries} == \
           {(e.experiment, e.week) for e in _base_entries()}
    assert client.get("/api/reschedule/parallel/land/status").json()["landed"] is False
    # 再撤销一次：没有可撤销的 → 404，不假装成功
    assert client.post("/api/reschedule/parallel/land/undo", headers=_admin()).status_code == 404


def test_undo_counts_post_landing_moves(env):
    """落地之后生效的调课会随快照回滚 —— 必须计数报出，不静默吞。"""
    db, client, _ = env
    client.post("/api/reschedule/parallel/land",
                json={"schedule": _plan_a()}, headers=_admin())
    # 落地后批一条调课：把已落地的 项目B（W2 周二 567 A412）挪到第 5 周
    res = api._apply_reschedule({
        "teacher": "吴老师", "class_name": "甲班", "course_name": "",
        "original_time": "周二 13:30-15:45 · A412", "target_week": 5, "source_week": 2,
    }, persist=True)
    assert res["applied"] == 1
    r = client.post("/api/reschedule/parallel/land/undo", headers=_admin())
    assert r.json()["rolled_back_moves"] == 1


# ---------------------------------------------------------------- 5. 重启重放

def _cold_restart(monkeypatch, tmp_path):
    """模拟沙箱重启：内存清空，默认表指向临时 xlsx（内容＝夹具基线）。"""
    xlsx = tmp_path / "base.xlsx"
    _write_xlsx(str(xlsx), _xlsx_rows())
    monkeypatch.setattr(api, "DEFAULT_SCHEDULE_PATH", str(xlsx))
    monkeypatch.setattr(api._db, "loaded", False)
    monkeypatch.setattr(api._db, "entries", [])


def test_landed_plan_survives_simulated_restart(env, monkeypatch):
    db, client, tmp_path = env
    client.post("/api/reschedule/parallel/land",
                json={"schedule": _plan_a()}, headers=_admin())
    _cold_restart(monkeypatch, tmp_path)
    api._ensure_loaded()
    weeks = {e.week for e in db.entries if e.experiment == "项目A" and e.group == "1组"}
    assert weeks == {1}, f"重启后落地方案丢失：{weeks}"
    assert {(e.experiment, e.week) for e in db.entries} >= {
        ("绪论课（教室已安排）", 5), ("项目A考试", 6)}, "非可排行必须原样"
    assert not [t for t in db.pending if t.experiment == "项目B"], "待排剪裁须重放"


def test_replay_order_newer_event_wins(env, monkeypatch):
    """落地(T1)之后又批了调课(T2)：重启后必须是调课的结果（后发生者赢）。"""
    db, client, tmp_path = env
    client.post("/api/reschedule/parallel/land",
                json={"schedule": _plan_a()}, headers=_admin())
    res = api._apply_reschedule({
        "teacher": "吴老师", "class_name": "甲班", "course_name": "",
        "original_time": "周二 13:30-15:45 · A412", "target_week": 5, "source_week": 2,
    }, persist=True)
    assert res["applied"] == 1
    _cold_restart(monkeypatch, tmp_path)
    api._ensure_loaded()
    weeks = {e.week for e in db.entries if e.experiment == "项目B"}
    assert weeks == {5}, f"落地后的调课被静默吞掉：{weeks}"


def test_replay_order_landing_after_move_wins(env, monkeypatch):
    """反向：先批调课(T1)再落地(T2)，重启后落地赢（调课被替换是后发生者的权利）。"""
    db, client, tmp_path = env
    res = api._apply_reschedule({
        "teacher": "吴老师", "class_name": "甲班", "course_name": "",
        "original_time": "周二 13:30-15:45 · A412", "target_week": 5, "source_week": 3,
    }, persist=True)
    assert res["applied"] == 1
    client.post("/api/reschedule/parallel/land",
                json={"schedule": _plan_a()}, headers=_admin())
    _cold_restart(monkeypatch, tmp_path)
    api._ensure_loaded()
    weeks = {e.week for e in db.entries if e.experiment == "项目B"}
    assert weeks == {2}, f"落地应覆盖更早的调课：{weeks}"


def test_second_landing_diffs_against_first(env):
    """二次落地：diff 必须对照第一次落地后的状态，不是原始表。"""
    db, client, _ = env
    client.post("/api/reschedule/parallel/land",
                json={"schedule": _plan_a()}, headers=_admin())
    plan_b = [dict(r) for r in _plan_a()]
    plan_b[0]["week"] = 2   # 只把 项目A/1组 挪回第 2 周
    r = client.post("/api/reschedule/parallel/land",
                    json={"schedule": plan_b}, headers=_admin())
    body = r.json()
    assert len(body["changed"]) == 1 and "项目A" in body["changed"][0]["unit"]
    assert body["unchanged_units"] == 3
    assert {e.week for e in db.entries if e.experiment == "项目A" and e.group == "1组"} == {2}


# ---------------------------------------------------------------- 6. 换表作废

def test_upload_discards_landed_plan(env, monkeypatch, tmp_path):
    """换了安排表，旧表上的落地方案必须作废（方案里的单元在新表可能不存在）。"""
    db, client, _ = env
    client.post("/api/reschedule/parallel/land",
                json={"schedule": _plan_a()}, headers=_admin())
    monkeypatch.setattr(api, "_DATA_DIR", str(tmp_path / "uploads"))
    other = tmp_path / "other.xlsx"
    _write_xlsx(str(other), [(9, "周五", "567", "乙班", "1组", "新项目", "A501", "新老师")])
    with open(other, "rb") as f:
        r = client.post("/api/reschedule/upload",
                        files={"file": ("other.xlsx", f.read(),
                                        "application/vnd.ms-excel")},
                        headers=_admin())
    assert r.status_code == 200, r.text
    assert "落地方案 1 份" in r.json()["message"]
    assert client.get("/api/reschedule/parallel/land/status").json()["landed"] is False
