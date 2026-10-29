"""进程重启后数据接口自愈（回归测试）。

背景（线上真实故障，2026-10-21 用户报告）
------------------------------------------
线上页面报「加载课程总览失败：请先加载实验安排表（/load-default）」，而同一份
前端产物在本地一切正常、`/load-default` 手动调用也立刻成功。

根因不是版本差异（线上与本地 bundle hash 完全一致），而是**运行时状态差异**：
排课数据只存在进程内存里（`ScheduleDatabase.loaded` / `entries`），而线上由沙箱
托管、进程会被平台回收重启 ⇒ 内存态清零。原先 8 个数据接口在未加载时直接抛
`400 请先加载实验安排表`，而前端只在**页面挂载时**自动 load-default 一次、没有
重试：只要那一次失败、或进程在页面打开之后重启，用户就卡在报错上无法自救
（界面上没有任何"重新加载"入口，只能手动刷新）。

修法：改为 `_ensure_loaded()` —— 访问数据前若内存为空，自动把默认表读回来。
`/load-default` 仍然可用（幂等），但不再是使用前提。

本文件锁死的契约
----------------
1. 冷进程（`loaded=False`）下，数据接口应**自动恢复**并正常返回，而不是 400；
2. 自动恢复只解析一次（并发请求不重复解析 433 行 Excel）；
3. 默认表真的不存在时，仍要**如实报错**（404），不能假装恢复。
"""

import os
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.api import reschedule as rs_mod
from app.main import app


@pytest.fixture()
def client():
    return TestClient(app)


@pytest.fixture()
def cold_process(monkeypatch):
    """模拟沙箱重启：内存里的排课数据被清空，磁盘上的默认表还在。"""
    monkeypatch.setattr(rs_mod._db, "loaded", False)
    monkeypatch.setattr(rs_mod._db, "entries", [])
    assert rs_mod._db.loaded is False
    yield
    # 恢复全局状态，避免影响依赖"已加载"的其它用例
    monkeypatch.setattr(rs_mod._db, "loaded", True)


# 覆盖不同类型的数据接口：并行排课总览、教师列表、统计
DATA_ENDPOINTS = [
    "/api/reschedule/parallel/overview",
    "/api/reschedule/teachers",
    "/api/reschedule/stats",
]


@pytest.mark.parametrize("path", DATA_ENDPOINTS)
def test_data_endpoint_self_heals_when_process_restarted(client, cold_process, path):
    """冷进程下不应再返回 400「请先加载实验安排表」，而应自动加载后正常响应。"""
    r = client.get(path)
    assert r.status_code == 200, (
        f"{path} 未自愈：{r.status_code} {r.text[:200]} | "
        f"path={rs_mod.DEFAULT_SCHEDULE_PATH} exists={os.path.exists(rs_mod.DEFAULT_SCHEDULE_PATH)}"
    )
    assert "请先加载实验安排表" not in r.text
    assert rs_mod._db.loaded is True, "自愈后 loaded 应为 True"


def test_parallel_overview_returns_courses_after_self_heal(client, cold_process):
    """用户报错的具体接口：自愈后必须真的返回课程数据，而不是空壳 200。"""
    r = client.get("/api/reschedule/parallel/overview")
    assert r.status_code == 200, (
        f"{r.status_code} {r.text[:300]} | path={rs_mod.DEFAULT_SCHEDULE_PATH} "
        f"exists={os.path.exists(rs_mod.DEFAULT_SCHEDULE_PATH)}"
    )
    courses = r.json()["data"]["courses"]
    assert courses, "自愈后课程总览不应为空"


def test_ensure_loaded_is_idempotent(client, cold_process):
    """已加载时不应重复解析；冷启动连续调用也只加载一次。"""
    calls = []
    # 在**类**上打桩，而不是在 rs_mod._db 实例上：
    # R4 把加载改成了「构建新实例后原子替换」，实例级打桩会失效
    # （新实例上没有这个 monkeypatch），测试会假失败。
    # 打桩位置与实现细节解绑，才是稳定的断言方式。
    from app.utils.excel_parser import ScheduleDatabase

    original = ScheduleDatabase.load_from_excel

    def counting_load(self, path, *a, **kw):
        calls.append(path)
        return original(self, path, *a, **kw)

    monkeypatch_cls = ScheduleDatabase.load_from_excel
    ScheduleDatabase.load_from_excel = counting_load
    try:
        rs_mod._ensure_loaded()   # 冷 → 真正加载一次
        rs_mod._ensure_loaded()   # 热 → 直接返回
        rs_mod._ensure_loaded()
    finally:
        ScheduleDatabase.load_from_excel = monkeypatch_cls

    assert len(calls) == 1, f"应只解析一次，实际 {len(calls)} 次"


def test_missing_default_file_still_reports_error(client, monkeypatch, tmp_path):
    """默认表真的不存在时必须如实报错，不能静默返回空数据。"""
    monkeypatch.setattr(rs_mod._db, "loaded", False)
    monkeypatch.setattr(rs_mod._db, "entries", [])
    monkeypatch.setattr(rs_mod, "DEFAULT_SCHEDULE_PATH", str(tmp_path / "nope.xlsx"))

    r = client.get("/api/reschedule/parallel/overview")
    assert r.status_code == 404, r.text
    assert "不存在" in r.json()["detail"]

    # 收尾：恢复真实路径与已加载状态
    monkeypatch.undo()
    rs_mod._ensure_loaded()


# ---------------------------------------------------------------------------
# 路径解析回归（潜伏缺陷，2026-10-21 修）
#
# 本文件正是用 ``sys.path.insert(0, <tests>/..)`` 导入 app 的，所以本模块的
# ``__file__`` 会是 ``<repo>/backend/tests/../app/api/reschedule.py``。
# ``os.path.dirname`` 不消解 ``..``，四层连乘会把「仓库根」算成 ``backend/tests``，
# 于是在没有任何报错的情况下回退到脱敏表 —— 表现为教师名变成「教师A」。
# ---------------------------------------------------------------------------

def test_repo_root_survives_dotdot_in_module_path():
    """含 `..` 的导入路径下，仓库根仍须解析到真实仓库根，而不是 backend/tests。"""
    assert os.path.basename(os.path.normpath(rs_mod._REPO_ROOT)) != "tests", (
        f"仓库根被误算为 {rs_mod._REPO_ROOT}（dirname 未规范化 ..）"
    )
    assert os.path.isfile(os.path.join(rs_mod._REPO_ROOT, "backend", "app", "main.py")), (
        f"仓库根下找不到 backend/app/main.py：{rs_mod._REPO_ROOT}"
    )
    # 默认表必须落在仓库根，不能落在 backend/tests 之类的位置
    assert os.path.dirname(rs_mod.DEFAULT_SCHEDULE_PATH) == rs_mod._REPO_ROOT


def test_repo_root_probe_falls_back_without_marker(tmp_path):
    """marker（backend/app）不存在时要退到规范化后的四层向上，且不抛异常。"""
    fake = tmp_path / "a" / "b" / "c" / "m.py"
    fake.parent.mkdir(parents=True)
    fake.write_text("", encoding="utf-8")

    root = rs_mod._find_repo_root(str(fake).replace("/", os.sep))
    assert root == str(tmp_path)
    # 退化路径也必须已规范化：结果里不能残留 `..`
    assert ".." not in root

