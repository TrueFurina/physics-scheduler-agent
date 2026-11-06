"""R88：主数据落盘「没能用上」必须给结构化信号，不能只在服务端 print。

缺陷（探针 _r88_probe.py 实证 2026-11-07）
------------------------------------------
`_ensure_master_data_locked()` 有三种异常降级：

    1. 落盘 JSON 损坏（R50 的坏文件清理分支）
    2. `from_dict` 抛异常
    3. 落盘里是空主数据（历史 bug 产物，不能当权威）

三条都会 `master_store.clear()` **丢弃教秘已保存的主数据**，然后按安排表
反推基线。实测（demo 表 + 落盘里放了「教秘新建项目X」）：

    好落盘  → 1 个项目，含「教秘新建项目X」
    损坏    → 25 个反推项目，「教秘新建项目X」消失
    异常    → 同上
    空主数据 → 同上

而 `GET /master/overview` 在四种情况下的响应**逐字节同形**：
`{"success": true, "data": ...}`。教秘进主数据页看到的是反推基线，
却完全不知道自己上次保存的修改被丢了 —— 三条分支只 `print` 到服务端日志。
这正是 R87「落盘失败 → 重启会回退」那句警告的后半段：回退真发生时，
没有任何人告诉用户。

对照组（不许报警，R86「狼来了」同款口径）
------------------------------------------
- 首次启动，磁盘上没有落盘文件 → 正常反推，不是数据丢失；
- 安排表已更换（指纹不一致）→ 旧表上的主数据本就作废，是**设计**，不是故障；
- 好落盘被成功恢复 → 响应必须与改动前逐字节一致。

修法
----
三个异常分支记下原因；`/master/overview` 只在「确实丢了落盘」时附
`restored: false` + `warning`。成功路径响应形状一字不变。
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import app.api.reschedule as api
import app.master_store as ms
from app.main import app
from app.master_data import MasterData
from app.utils.excel_parser import ScheduleDatabase
from fastapi.testclient import TestClient

REAL = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "schedule.demo.xlsx"))
FP = "fp-r88"
NEW_PROJECT = "教秘新建项目X"
BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


@pytest.fixture()
def env(tmp_path, monkeypatch):
    if not os.path.isfile(REAL):
        pytest.skip("缺少 schedule.demo.xlsx")
    db = ScheduleDatabase()
    db.load_from_excel(REAL)
    store = tmp_path / "master_store.json"

    monkeypatch.setattr(ms, "STORE_PATH", str(store))
    monkeypatch.setattr(api, "_db", db)
    monkeypatch.setattr(api, "_schedule_fp", FP)
    monkeypatch.setattr(api, "_master_tasks", [])
    # R87 教训：teardown 不能手动写回全局，交给 monkeypatch 保存/还原原值。
    # raising=False：改动前这个全局还不存在，红测试不该死于 AttributeError。
    monkeypatch.setattr(api, "_master_restore_warning", "", raising=False)
    monkeypatch.setattr(api, "_landing_store", None)
    api._init_db()
    api._master_data.__init__()
    return store


def _good_master_dict():
    md = MasterData()
    md.upsert_experiment(NEW_PROJECT, course="测试课", classes=["24软工1"])
    return md.to_dict()


def _write(store, payload):
    with open(store, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)


def _good(store):
    _write(store, {"schema_version": ms.SCHEMA_VERSION,
                   "schedule_fingerprint": FP,
                   "master": _good_master_dict(), "tasks": []})


def _corrupt(store):
    with open(store, "w", encoding="utf-8") as f:
        f.write("{ 这不是 JSON")


def _empty_master(store):
    _write(store, {"schema_version": ms.SCHEMA_VERSION,
                   "schedule_fingerprint": FP, "master": {}, "tasks": []})


def _stale(store):
    _write(store, {"schema_version": ms.SCHEMA_VERSION,
                   "schedule_fingerprint": "fp-另一张表",
                   "master": _good_master_dict(), "tasks": []})


def _overview():
    with TestClient(app) as client:
        r = client.get("/api/reschedule/master/overview")
    assert r.status_code == 200, r.text
    return r.json()


def _prepare(store, setup):
    ms.consume_corrupt_flag()
    if setup:
        setup(store)


# ------------------------------------------------------------ 三种异常降级

@pytest.mark.parametrize("setup,label", [
    (_corrupt, "落盘损坏"),
    (_empty_master, "空主数据脏数据"),
])
def test_loss_branches_report_restored_false(env, setup, label):
    """丢了用户已保存的主数据，响应必须带结构化信号。"""
    _prepare(env, setup)
    body = _overview()
    assert body.get("restored") is False, f"{label}：丢了落盘却没标 restored=false"
    warning = body.get("warning") or ""
    assert warning.startswith("⚠️"), f"{label}：警告必须以 ⚠️ 开头，否则被渲染成中性提示"
    assert "重启" in warning and "回退" in warning, f"{label}：警告没说清后果：{warning!r}"
    assert "丢失" in warning or "核对" in warning, f"{label}：没给可执行的下一步：{warning!r}"


def test_from_dict_exception_reports_restored_false(env, monkeypatch):
    """恢复过程抛异常 → 同样不能静默回退。"""
    _prepare(env, _good)

    def boom(_d):
        raise RuntimeError("模拟 from_dict 抛异常")
    monkeypatch.setattr(api._master_data, "from_dict", boom)
    body = _overview()
    assert body.get("restored") is False, "恢复抛异常却没标 restored=false"
    assert "⚠️" in (body.get("warning") or "")


def test_loss_really_falls_back_to_baseline(env):
    """降级确实丢了用户数据（不是只挂了个假警告）。"""
    _prepare(env, _corrupt)
    body = _overview()
    exps = (body.get("data") or {}).get("experiments")
    assert NEW_PROJECT not in str(exps), "探针前提失效：损坏落盘竟然被恢复了"


# ------------------------------------------------------------ 对照组：不许狼来了

@pytest.mark.parametrize("setup,label", [
    (_good, "好落盘成功恢复"),
    (None, "首次启动无落盘"),
    (_stale, "安排表已更换"),
])
def test_normal_paths_keep_response_byte_shape(env, setup, label):
    """成功 / 正常降级路径的响应不许新增任何字段（R86 防放宽同款）。"""
    _prepare(env, setup)
    body = _overview()
    assert set(body.keys()) == {"success", "data"}, (
        f"{label}：响应多/少了字段 {sorted(body.keys())} —— 每次首屏弹警告比不报更糟")
    assert "restored" not in body and "warning" not in body


def test_good_store_still_restores_the_saved_project(env):
    """对照组前提：好落盘必须真的把教秘的项目恢复出来。"""
    _prepare(env, _good)
    exps = (_overview().get("data") or {}).get("experiments")
    assert NEW_PROJECT in str(exps), "好落盘没被恢复 —— 对照组本身失效"


def test_stale_fingerprint_really_discards_old_store(env):
    """对照组前提：换表确实丢了旧落盘，但那是设计，不该报警。"""
    _prepare(env, _stale)
    _overview()
    assert not env.exists(), "指纹不一致时旧落盘没被丢弃 —— 对照组前提失效"


# ------------------------------------------------------------ helper 契约

def test_helper_is_silent_and_does_not_copy_when_no_loss(monkeypatch):
    """没有降级时 helper 必须原样返回同一个对象（成功路径逐字节不变）。"""
    monkeypatch.setattr(api, "_master_restore_warning", "", raising=False)
    payload = {"success": True, "data": {"a": 1}}
    assert api._master_restore_result(payload) is payload


def test_helper_does_not_mutate_caller_payload(monkeypatch):
    """有降级时也不能就地改调用方的 dict。"""
    monkeypatch.setattr(api, "_master_restore_warning", "⚠️ 测试警告", raising=False)
    payload = {"success": True, "data": {"a": 1}}
    out = api._master_restore_result(payload)
    assert out is not payload and "restored" not in payload
    assert out.get("restored") is False


# ------------------------------------------------------------ 源码级守卫

def test_every_loss_branch_records_the_reason():
    """三条异常分支一个都不能漏（R87：批量改动必须有源码级守卫）。"""
    with open(os.path.join(BACKEND, "app", "api", "reschedule.py"), encoding="utf-8") as f:
        src = f.read()
    start = src.index("def _ensure_master_data_locked(")
    end = src.index("def _master_courses_snapshot(", start)
    body = src[start:end]
    assert body.count("_note_master_restore_loss(") >= 3, (
        "三条异常降级分支必须各记一次原因；只修探针打到的那一条等于没修")


def test_overview_is_wired_to_the_restore_helper():
    """后端说了前端才可能接住：overview 必须过 helper（R86 教训）。"""
    with open(os.path.join(BACKEND, "app", "api", "master.py"), encoding="utf-8") as f:
        src = f.read()
    start = src.index("async def master_overview(")
    end = src.index("@router", start)
    assert "_master_restore_result(" in src[start:end], (
        "overview 没接 helper —— 降级状态永远到不了界面")


def test_loss_note_is_not_attached_to_stale_fingerprint_branch():
    """指纹变更分支不许被记成降级（否则每次换表都弹假警告）。"""
    with open(os.path.join(BACKEND, "app", "api", "reschedule.py"), encoding="utf-8") as f:
        src = f.read()
    start = src.index("def _ensure_master_data_locked(")
    end = src.index("def _master_courses_snapshot(", start)
    body = src[start:end]
    elif_line = "安排表已更换，丢弃旧表上的主数据落盘"
    pos = body.index(elif_line)
    tail = body[pos:]
    assert "_note_master_restore_loss(" not in tail, (
        "指纹变更是设计内的正常降级，不该记成数据丢失")
