"""落地落盘失败时，内存必须回滚（R49）。

缺陷
----
`/parallel/land` 的写法是：

    before = plan_landing.snapshot(_db)
    applied = plan_landing.apply_plan(_db, plan)     # ← 内存已经改了
    try:
        rec = _landings().save(plan, before, ...)    # ← 落盘
    except Exception as e:
        plan_landing.restore(_db, before)            # ← 回滚
        raise HTTPException(500, "…已回滚内存改动…")

这条 **except 分支此前零测试覆盖**（`grep -c 落盘失败 backend/tests/*.py` = 0）。

它是整个落地链路里最危险的一条：
- 走正常路径时，`apply_plan` 和 `save` 都成功，回滚代码是死代码；
- 一旦 `save` 失败（磁盘满、sqlite 锁、权限），回滚就是**唯一**防止
  "内存已改、重启丢失"的东西 —— 而教秘下一次刷新会看到方案神秘消失。

回滚写错的两种形态：
1. 没回滚 → 内存留着新表，但没落盘；重启后被重放覆盖，中间态无人知道
2. 回滚不彻底 → 部分字段没还原（如 `blocked` / `course`），冲突判断从此偏移

本文件把这条分支钉住。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import app.api.reschedule as api
import app.plan_landing as plan_landing


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """与 test_plan_landing.env 同构的最小环境。"""
    from fastapi.testclient import TestClient
    from app.main import app
    from app.master_data import MasterData
    from app.utils.excel_parser import ScheduleDatabase, ScheduleEntry, PendingTask

    db = ScheduleDatabase()
    db.entries = [
        ScheduleEntry(1, "周一", "567", "甲班", "1组", "项目A", "A101", "吴老师"),
        ScheduleEntry(1, "周二", "567", "甲班", "2组", "项目A", "A101", "吴老师"),
        ScheduleEntry(2, "周一", "567", "乙班", "1组", "项目B", "A102", "张老师"),
    ]
    db.pending = []
    db.loaded = True

    md = MasterData()
    md.build_from_entries(db.entries)
    monkeypatch.setattr(api, "_db", db)
    monkeypatch.setattr(api, "_master_data", md)
    monkeypatch.setattr(api, "_master_tasks", [])
    monkeypatch.setattr(api, "_DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setattr(api, "_landing_store", None)
    api._init_db()

    client = TestClient(app)
    from app.security import issue_admin_token
    headers = {"Authorization": "Bearer " + issue_admin_token("admin")["token"]}
    return db, client, headers, PendingTask


def _plan():
    return {
        "schedule": [
            {"class_name": "甲班", "project": "项目A", "group": "1组",
             "week": 3, "weekday": "周三", "time_slot": "91011",
             "room": "A201", "teacher": "吴老师"},
            {"class_name": "甲班", "project": "项目A", "group": "2组",
             "week": 3, "weekday": "周三", "time_slot": "91011",
             "room": "A202", "teacher": "吴老师"},
        ]
    }


def _fingerprint(db):
    """表的完整指纹：任一字段变了都要能看出来。"""
    return sorted(
        (e.week, e.weekday, e.time_slot, e.class_name, e.group,
         e.experiment, e.room, e.teacher,
         tuple(getattr(e, "blocked", None) or ()),
         getattr(e, "course", "") or "")
        for e in db.entries
    )


def test_land_rolls_back_when_persist_fails(env, monkeypatch):
    """落盘抛异常 → 内存必须回到落地前，且返回 500。"""
    db, client, headers, _ = env
    before = _fingerprint(db)
    n_before = len(db.entries)

    def boom(*a, **kw):
        raise RuntimeError("模拟磁盘满")

    monkeypatch.setattr(api._landings(), "save", boom, raising=False)

    r = client.post("/api/reschedule/parallel/land",
                    json={**_plan(), "note": "应回滚"}, headers=headers)

    assert r.status_code == 500, f"落盘失败应返回 500，实际 {r.status_code}"
    assert "回滚" in r.json().get("detail", ""), "错误信息没说已回滚"

    assert len(db.entries) == n_before, (
        f"回滚后条数不对：{n_before} → {len(db.entries)}"
    )
    assert _fingerprint(db) == before, (
        "回滚不彻底 —— 有字段没还原（内存已改但没落盘，重启后方案会神秘消失）"
    )


def test_land_rollback_restores_blocked_and_course(env, monkeypatch):
    """回滚必须还原 `blocked` / `course` —— 这两个影响冲突判断的对错。"""
    db, client, headers, _ = env
    before_blocked = [list(getattr(e, "blocked", None) or []) for e in db.entries]
    before_course = [getattr(e, "course", "") or "" for e in db.entries]

    monkeypatch.setattr(api._landings(), "save",
                        lambda *a, **kw: (_ for _ in ()).throw(IOError("x")),
                        raising=False)
    client.post("/api/reschedule/parallel/land", json=_plan(), headers=headers)

    assert [list(getattr(e, "blocked", None) or []) for e in db.entries] == before_blocked
    assert [getattr(e, "course", "") or "" for e in db.entries] == before_course


def test_failed_land_leaves_no_landed_record(env, monkeypatch):
    """落盘失败后，land/status 必须仍报未落地（不能留下半条记录）。"""
    db, client, headers, _ = env

    monkeypatch.setattr(api._landings(), "save",
                        lambda *a, **kw: (_ for _ in ()).throw(IOError("x")),
                        raising=False)
    client.post("/api/reschedule/parallel/land", json=_plan(), headers=headers)

    st = client.get("/api/reschedule/parallel/land/status").json()
    assert st.get("landed") is False, f"落盘失败却留下了落地记录：{st}"


def test_land_success_still_works(env):
    """对照组：不注入故障时，落地照常成功（防止测试把正常路径也弄坏）。"""
    db, client, headers, _ = env
    r = client.post("/api/reschedule/parallel/land", json=_plan(), headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["success"] is True
    # 原表 3 条，其中甲班/项目A 的 2 条被方案替换成 2 条，乙班那 1 条保留
    assert len(db.entries) == 3, f"应仍是 3 条，实际 {len(db.entries)}"
