"""导出预热状态（R91）：与 solver 预热（engine/warmup.py）对称，失败绝不能只 print。

此前 `_warm_export_cache` 在 except 里只 `print(...)` —— 和 R88 之前的主数据恢复一样，
"首调慢了一点"到底是"本来就这样"还是"预热失败过"，运维/前端完全看不到。
本文件验证：状态被结构化记录，且通过 /health、/status 两处读得到。
"""

import pytest
from fastapi.testclient import TestClient

from app.api import export
from app.main import app

client = TestClient(app)


@pytest.fixture
def reset_state():
    export._EXPORT_WARM_STATE["status"] = "pending"
    export._EXPORT_WARM_STATE["detail"] = None
    yield
    export._EXPORT_WARM_STATE["status"] = "pending"
    export._EXPORT_WARM_STATE["detail"] = None


def test_export_warmup_records_ok(monkeypatch, reset_state):
    # 数据已加载、主数据就绪，四份建表都成功 → 状态必须记 ok
    monkeypatch.setattr(export._api._db, "loaded", True, raising=False)
    monkeypatch.setattr(export._api, "_ensure_master_data", lambda: None)
    monkeypatch.setattr(export, "_class_schedule_bytes", lambda *a, **k: b"x")
    monkeypatch.setattr(export, "_room_schedule_bytes", lambda *a, **k: b"x")
    monkeypatch.setattr(export, "_master_data_bytes", lambda *a, **k: b"x")
    monkeypatch.setattr(export, "_master_workbook_bytes", lambda *a, **k: b"x")
    export._warm_export_cache()
    state = export.export_warmup_state()
    assert state["status"] == "ok"
    assert state["detail"] is None


def test_export_warmup_records_failure(monkeypatch, reset_state):
    # 建表抛异常 → 必须记 failed + 详情，且异常不能漏给调用方
    monkeypatch.setattr(export._api._db, "loaded", True, raising=False)
    monkeypatch.setattr(export._api, "_ensure_master_data", lambda: None)

    def _boom(*a, **k):
        raise RuntimeError("boom-123")

    monkeypatch.setattr(export, "_class_schedule_bytes", _boom)
    export._warm_export_cache()  # 不能抛
    state = export.export_warmup_state()
    assert state["status"] == "failed"
    assert "boom-123" in (state["detail"] or "")


def test_export_warmup_state_is_resettable(reset_state):
    # 初始必须是 pending（未跑过）
    assert export.export_warmup_state()["status"] == "pending"


def test_health_exposes_export_warmup():
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert "export" in body
    assert body["export"]["status"] in {"pending", "running", "ok", "failed"}


def test_status_exposes_export_warmup():
    r = client.get("/api/reschedule/status")
    assert r.status_code == 200
    assert "export_warmup" in r.json()
