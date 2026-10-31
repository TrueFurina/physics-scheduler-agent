"""撤销的两次写入不是原子的 —— `undo` 必须在标记写失败时如实交代（R45）。

缺陷
----
`plan_landing.undo()`（app/plan_landing.py:514）做的是两件独立的事：

    rec = store.latest()
    before = json.loads(rec["before_json"])
    restore(db, before)                        # ① 内存回到落地前
    mark = store.revoke(rec["id"], actor=...)  # ② sqlite 里标记"已撤销"

两步之间没有任何兜底。实测（2026-11-01 探针）三个方向的实际行为：

| 失败点 | 内存 | sqlite 记录 | 调用方看到 |
|---|---|---|---|
| ② revoke 抛异常 | **已回滚** | 仍是"已落地" | 裸 RuntimeError（500，无详情） |
| ④ restore 抛异常 | 没动 | 没标记 | 裸 RuntimeError |
| ③ revoke 返回 updated=0 | 已回滚 | 已标记 | success=True（本探针实测一致，见下） |

**② 是唯一的真缺口**，后果有两层，都不是"看起来坏了"而是"看起来好了"：

1. `/land/status` 报 `landed=True`，但课表其实已经是落地前的了 —— 界面上那个
   "撤销"按钮指向一个已经不在生效的方案。
2. 更严重：重放（`store.all()` 只取未撤销的）会在重启后**把这次撤销再撤销回来**，
   教秘上周撤掉的方案自己复活，而没有任何人点过按钮。R55 特意让 `all()` 排除
   已撤销记录，就是为了防这个；② 失败正好绕过了它。

**为什么 M43 / M44 打不到这个缺口**（本轮必须答的问题）：

- M43 守的是「落盘失败 ⇒ 内存要不要跟着退」，锚点在 `app/api/reschedule.py`
  的 `plan_landing.restore(_db, before)` —— 那是**落地**链路里的回滚，不是撤销链路。
- M44 守的是「损坏的落盘要不要丢弃」。
- 两者都属于「**写盘失败 ⇒ 单一存储要不要退**」这同一个方向。而 ② 是**内存与
  sqlite 两个存储之间的一致性**：① 和 ② 各自都成功了，崩在它们中间。
- 更根本的一条：**变异测试只能杀掉已存在的代码**。在补上兜底之前，这里压根
  没有可变异的那一行 —— 所以历史上任何一个变异体都不可能报到它。

**为什么不给 restore 也加对称的兜底**：restore 先于 revoke 是**有意为之的正确顺序**。
反过来（先标记再恢复）的话，restore 崩掉时记录已被标成"已撤销"而课表没动，
那才是真正无法挽回的一支。本文件用 `test_restore_failure_*` 把顺序钉住，
防止后人"顺手"把它调个儿。

**取舍**：② 写失败时判定为**整体失败**（`success=False`），而不是返回 `success=True`
加一个 warning。理由是审计标记本身就是撤销契约的一部分（R55 明确写过），
"课表退了、记录没退"报成功，正是这个仓库反复在打的那种"谎报成功"。
代价是路由会把它映射成 404（状态码语义不准，已记入 遗留）—— 但状态是**可重试**的，
错误信息里写明了这一点。

`revoke` 返回的 `updated` 字段本轮**故意不校验**：`latest()` 只返回未撤销的记录，
`revoke` 的 UPDATE 又带 `AND revoked_at=''`，因此 `updated=0` 只可能是"已被别人
撤销过"，而那时记录本就处于已撤销状态 —— 与内存一致。校验它是无效的防御。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import app.plan_landing as plan_landing  # noqa: E402


def _db_and_store(tmp_path):
    """最小内存库 + 独立 sqlite 落地记录（不依赖 app 全局状态）。"""
    from app.utils.excel_parser import ScheduleDatabase, ScheduleEntry

    db = ScheduleDatabase()
    db.entries = [
        ScheduleEntry(1, "周一", "567", "甲班", "1组", "项目A", "A101", "吴老师"),
        ScheduleEntry(1, "周二", "567", "甲班", "2组", "项目A", "A101", "吴老师"),
        ScheduleEntry(2, "周一", "567", "乙班", "1组", "项目B", "A102", "张老师"),
    ]
    db.pending = []
    db.loaded = True

    store = plan_landing.LandingStore(str(tmp_path / "land.db"))
    store.init_schema()
    return db, store


def _fingerprint(db):
    return sorted((e.week, e.weekday, e.time_slot, e.class_name, e.group,
                   e.experiment, e.room, e.teacher,
                   tuple(getattr(e, "blocked", None) or ()),
                   getattr(e, "course", "") or "")
                  for e in db.entries)


def _land_something(db, store):
    """落地一次：返回 (落地前指纹, 落地前条数)。内存随后被改成新课表。"""
    before_fp = _fingerprint(db)
    before_n = len(db.entries)
    snap = plan_landing.snapshot(db)
    db.entries = [
        type(db.entries[0])(3, "周三", "91011", "甲班", "1组", "项目A", "A201", "吴老师"),
        type(db.entries[0])(3, "周三", "91011", "甲班", "2组", "项目A", "A202", "吴老师"),
        type(db.entries[0])(2, "周一", "567", "乙班", "1组", "项目B", "A102", "张老师"),
    ]
    store.save([{"class_name": "甲班"}], snap, note="r45", actor="admin")
    return before_fp, before_n


def test_undo_revoke_failure_is_reported_not_raised_bare(tmp_path, monkeypatch):
    """② 写失败：不许裸抛，也不许报成功 —— 必须交代"课表已回滚、标记没写上"。"""
    db, store = _db_and_store(tmp_path)
    before_fp, before_n = _land_something(db, store)

    def boom(*a, **kw):
        raise RuntimeError("模拟 sqlite 写入失败（磁盘满）")

    monkeypatch.setattr(store, "revoke", boom)

    out = plan_landing.undo(db, store, actor="tester")

    assert isinstance(out, dict), (
        "撤销标记写失败时直接抛裸异常，调用方无从得知「内存其实已经回滚了」"
    )
    assert out.get("success") is False, (
        f"课表回滚了但撤销标记没写成，却报 success —— 重放会把这次撤销再撤销回来：{out}"
    )
    assert out.get("revoked") is False, f"没写成标记就不能宣称已撤销：{out}"
    assert not out.get("revoked_at"), f"没写成标记却返回了 revoked_at：{out}"

    # 必须说清楚内存这一半已经做了什么，否则排障的人猜不出自己在哪个状态
    text = str(out.get("error", "")) + str(out.get("warning", "")) + str(out)
    assert "回滚" in text, f"没交代内存已经回滚：{out}"

    # 实际状态确实处于"课表回退了、记录还活着"的分裂态，测试要把这个事实固定下来
    assert _fingerprint(db) == before_fp, "课表应当已经回到落地前"
    assert len(db.entries) == before_n
    assert store.latest() is not None, "标记没写成 ⇒ 记录仍应是有效的（可重试）"


def test_undo_revoke_transient_failure_is_retried(tmp_path, monkeypatch):
    """② 只是瞬时失败（sqlite 锁）时，应当重试一次就成功，而不是直接放弃。"""
    db, store = _db_and_store(tmp_path)
    _land_something(db, store)

    real = store.revoke
    calls = {"n": 0}

    def flaky(rec_id, actor=""):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("模拟瞬时锁")
        return real(rec_id, actor=actor)

    monkeypatch.setattr(store, "revoke", flaky)

    out = plan_landing.undo(db, store, actor="tester")

    assert out.get("success") is True, f"瞬时失败应重试后成功：{out}"
    assert calls["n"] == 2, f"应当恰好重试一次，实际调用 {calls['n']} 次"
    assert store.latest() is None, "重试成功后记录必须已标记撤销"


def test_restore_failure_leaves_record_unrevoked(tmp_path, monkeypatch):
    """顺序守卫：restore 崩掉时记录**不能**被标记成已撤销。

    这条锁的是"先恢复内存、再打标记"这个顺序。若有人把它调成先标记，
    就会造出「记录说已撤销、课表其实没变」这种最糟的一支 —— 撤销按钮消失了，
    而什么都没退。
    """
    db, store = _db_and_store(tmp_path)
    _land_something(db, store)

    monkeypatch.setattr(plan_landing, "restore",
                        lambda d, s: (_ for _ in ()).throw(RuntimeError("restore 崩了")))

    with pytest.raises(Exception):
        plan_landing.undo(db, store, actor="tester")

    assert store.latest() is not None, (
        "课表没恢复，记录却已被标成已撤销 —— 撤销入口消失而什么都没退"
    )


def test_undo_happy_path_unchanged(tmp_path):
    """对照组：不加故障时行为与 R44 之前完全一致（防止把正常路径改坏）。"""
    db, store = _db_and_store(tmp_path)
    before_fp, before_n = _land_something(db, store)

    out = plan_landing.undo(db, store, actor="tester")

    assert out["success"] is True
    assert out.get("revoked_at"), "正常路径必须给出撤销时间"
    assert _fingerprint(db) == before_fp
    assert len(db.entries) == before_n
    assert store.latest() is None
    hist = store.history(limit=5)
    assert len(hist) == 1 and hist[0]["revoked"] is True
    assert hist[0]["revoked_by"] == "tester", "撤销人要留痕"
