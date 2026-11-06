"""主数据落盘失败时，端点不许再静默报「成功」（R87）。

缺陷（只读探针实测）
--------------------
`master_store.save()` 的契约写得很清楚：原子写失败**不抛异常，返回 False**
（落盘是尽力而为的自愈层，内存态才是权威）。但 `_persist_master_state()`
把这个返回值整个丢掉，`_persist_master_state_async()` 也不透传，于是 14 个
写入端点（master.py 12 + reschedule.py 2）一律返回：

    {"success": True, "data": {...}}     # 盘上根本没有文件

教秘新建教师/上传清单后看到「保存成功」，沙箱回收或重启进程后修改全部消失，
回到安排表反推基线 —— 而**没有任何一处**告诉他这件事发生过。

对照 R49 的 `land_rollback`：那边落盘失败会回滚内存并返回 500。
主数据这边刻意不回滚（内存态仍是权威、失败不阻塞排课），所以更不能
把「没存住」伪装成「存好了」；正确做法是给结构化降级信号 + 明说后果。

修复口径
--------
* 不改库层、不回滚内存、不改 HTTP 状态码（内存里的修改确实生效了，
  谎称失败同样是骗人）；
* 落盘成功时响应**逐字节不变**（防放宽用例锁死）；
* 只在 `persisted is False` 时附带 `persisted` + `warning`，
  `None`/`MagicMock` 等异常返回不得误报。
"""
import ast
import asyncio
import os
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import master_store
from app.main import app
from app.master_data import MasterData
from app.security import issue_admin_token
from app.utils.excel_parser import ScheduleDatabase

import app.api.reschedule as api

client = TestClient(app)
HEADERS = {"Authorization": "Bearer " + issue_admin_token("admin")["token"]}


def _fresh_state(initialized=True):
    """把模块状态钉到临时对象，避开 demo 表整表解析。"""
    db = ScheduleDatabase()
    db.entries = []
    db.loaded = True
    api._db = db
    md = MasterData()
    if initialized:
        md.upsert_teacher("已有教师")
        md.upsert_experiment("已有项目")
        md.upsert_room("A410")
        md.upsert_class("一班")
        md.initialized = True
    api._master_data = md
    api._master_tasks = []
    api._schedule_fp = None


def _save_false(monkeypatch):
    """模拟真实契约：写失败但不抛，返回 False。"""
    monkeypatch.setattr(master_store, "save", lambda payload, store_path=None: False)


@pytest.fixture(autouse=True)
def _clean_state():
    # 这四个是 reschedule 的模块全局状态：必须**存原值再还原**，不能在
    # teardown 里换成另一个假对象 —— 那会污染同进程里排在后面的测试文件。
    saved = (api._db, api._master_data, api._master_tasks, api._schedule_fp)
    _fresh_state()
    yield
    api._db, api._master_data, api._master_tasks, api._schedule_fp = saved


# ------------------------------------------------------------ 返回值透传

def test_sync_persist_returns_false_when_save_fails(monkeypatch):
    """同步落盘函数必须把 save 的 False 交出来，否则上层无从知道。"""
    _save_false(monkeypatch)
    assert api._persist_master_state() is False, (
        "_persist_master_state() 吞掉了 save() 的 False —— 上层只会看到成功"
    )


def test_sync_persist_returns_true_when_nothing_to_save(monkeypatch):
    """防放宽：未初始化且无清单时本来就不写盘，那是「无事可做」不是「失败」。"""
    _fresh_state(initialized=False)
    _save_false(monkeypatch)
    assert api._persist_master_state() is True


def test_async_persist_propagates_false(monkeypatch):
    """线程池版本同样要透传（R26 把落盘下沉，不能在这里断链）。"""
    _save_false(monkeypatch)
    assert asyncio.run(api._persist_master_state_async()) is False


# ------------------------------------------------------------ 端点信号

def test_upsert_teacher_reports_persist_failure(monkeypatch):
    _save_false(monkeypatch)
    r = client.post("/api/reschedule/master/teachers",
                    json={"name": "新教师A"}, headers=HEADERS)
    assert r.status_code == 200
    body = r.json()
    assert body.get("persisted") is False, (
        "落盘失败却没有任何结构化信号；响应 keys = " + repr(sorted(body))
    )
    warning = body.get("warning") or ""
    # 只查「磁盘」二字太松：一句「已安全保存，磁盘正常」也能混过去（M102
    # 存活就是这个原因）。必须同时说清**重启**这个触发条件与**回退/丢失**后果。
    assert "重启" in warning, f"警告没说重启后会发生什么，实际：{warning!r}"
    assert any(w in warning for w in ("回退", "丢失", "会丢")), (
        f"警告没说明修改会丢，实际：{warning!r}"
    )
    assert body.get("success") is True, "内存里的修改确实生效了，不该反过来谎称失败"


def test_delete_experiment_reports_persist_failure(monkeypatch):
    _save_false(monkeypatch)
    r = client.delete("/api/reschedule/master/experiments/已有项目", headers=HEADERS)
    assert r.status_code == 200
    assert r.json().get("persisted") is False, "删除路径同样落盘，不能漏掉信号"


def test_master_init_reports_persist_failure(monkeypatch):
    _save_false(monkeypatch)
    r = client.post("/api/reschedule/master/init", headers=HEADERS)
    assert r.status_code == 200
    assert r.json().get("persisted") is False, "一键反推也会落盘"


def test_clear_tasks_reports_persist_failure(monkeypatch):
    """reschedule.py 里那两处调用点（/master/tasks 清空）也要覆盖。"""
    _save_false(monkeypatch)
    r = client.delete("/api/reschedule/master/tasks", headers=HEADERS)
    assert r.status_code == 200
    assert r.json().get("persisted") is False, (
        "清空清单落盘失败仍报 success，重启后清单会回来"
    )


def test_disk_really_has_no_file_after_failure(monkeypatch):
    """把「信号」和「事实」对齐：报 persisted=False 时盘上确实没有落盘文件。"""
    _save_false(monkeypatch)
    r = client.post("/api/reschedule/master/teachers",
                    json={"name": "新教师B"}, headers=HEADERS)
    assert r.json().get("persisted") is False
    assert not os.path.exists(master_store.STORE_PATH), (
        "探针前提失效：盘上居然有文件，这个用例测不到真实失败"
    )


# ------------------------------------------------------------ 防放宽

def test_healthy_write_has_no_warning():
    """防放宽：落盘成功时响应必须逐字节保持原样，不许无故多字段。"""
    r = client.post("/api/reschedule/master/teachers",
                    json={"name": "新教师C"}, headers=HEADERS)
    assert r.status_code == 200
    body = r.json()
    assert sorted(body) == ["data", "success"], (
        f"成功路径响应被改了：{sorted(body)}"
    )
    assert body["success"] is True
    assert os.path.exists(master_store.STORE_PATH), "对照组前提：盘上应有文件"


def test_memory_change_still_applied_when_persist_fails(monkeypatch):
    """防放宽：不回滚内存 —— 落盘是尽力而为，内存态仍是权威。"""
    _save_false(monkeypatch)
    client.post("/api/reschedule/master/teachers",
                json={"name": "新教师D"}, headers=HEADERS)
    names = [t["name"] if isinstance(t, dict) else t
             for t in api._master_data.overview()["teachers"]]
    assert "新教师D" in names, "落盘失败不该把已生效的内存修改也撤掉"


def test_no_false_alarm_when_persist_result_is_none(monkeypatch):
    """防放宽：判据必须是 `is False`。旧夹具/mock 返回 None 不许满屏报警告。"""
    async def fake_async():
        return None
    monkeypatch.setattr(api, "_persist_master_state_async", fake_async)
    r = client.post("/api/reschedule/master/teachers",
                    json={"name": "新教师E"}, headers=HEADERS)
    body = r.json()
    assert body.get("persisted") is not False
    assert not body.get("warning")


# ------------------------------------------------------------ 源码守卫

def test_every_persist_call_consumes_its_result():
    """14 个写入端点一个都不能漏：每个落盘调用的返回值都必须被接住并交给 helper。

    这条守卫防的是「只修了探针打到的那一个端点」—— 主数据有 12 个 CRUD +
    init/import，外加 reschedule.py 的上传/清空清单，漏一个就是同一个谎报。
    """
    api_dir = os.path.join(os.path.dirname(__file__), "..", "app", "api")
    total_calls = 0
    for fname in ("master.py", "reschedule.py"):
        path = os.path.join(api_dir, fname)
        with open(path, encoding="utf-8") as f:
            src = f.read()
        ast.parse(src, filename=path)  # 源码本身必须合法
        # 只认「真的在 await 调用」的行：模块头部 docstring 里也提到了这个
        # 函数名，按名字扫会把它算成一处漏接（守卫自身假红）。
        lines = [ln for ln in src.splitlines()
                 if "_persist_master_state_async()" in ln and "await" in ln
                 and not ln.lstrip().startswith("#")]
        for ln in lines:
            assert " = await " in ln, (
                f"{fname}: 落盘返回值被丢弃 → {ln.strip()!r}"
            )
        total_calls += len(lines)
        assert src.count("_master_persist_result(") >= len(lines), (
            f"{fname}: {len(lines)} 处落盘调用，但 helper 使用次数不足"
        )
    assert total_calls >= 14, f"守卫只看到 {total_calls} 处落盘调用，扫描范围可能失明"
