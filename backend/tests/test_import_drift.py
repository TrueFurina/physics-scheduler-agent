"""导入向导：预览与提交之间数据被改，结果必须说得清（R54）。

场景
----
导入是**两阶段**（预览 → 确认导入），中间可能隔几分钟。这期间：

- 另一位管理员通过 CRUD 建了同名实体
- 教秘自己开了两个标签页各点一次
- 别人换了安排表

预览报告说"将新增 1 个"，提交后却是"新增 0 个" —— **两个数字都对，
但它们描述的不是同一件事**。教秘看到的是"导入了，但什么都没变"，
会以为导入失败、再点一次。

实测（预览后手工建出同名教师）：

    预览         teachers.rows=13，样本里含「教师C新」
    中间改动     upsert_teacher("教师C新")
    提交         rows=13, added=0, before=14, after=14
    ⇒ 预览说会新增，实际新增 0

### 现状与应有的行为

`apply_tables` 的语义是"只增补不删除"，所以 `added=0` 本身是**对的** ——
那个实体已经存在，不该再建一次。真正缺的是**说明**：

    教秘需要知道"你看到的是预览时的账面，实际数据在此期间变了 1 个"

否则他会把 `added=0` 读成"我的导入没生效"。

本文件锁住这条：**提交时必须报出"预览之后变了多少"**，
而不是让两个数字各说各话。
"""
import io
import os
import sys

import openpyxl
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import app.api.reschedule as api
import app.master_store as ms
from app.utils.excel_parser import ScheduleDatabase

REAL = os.path.join(os.path.dirname(__file__), "..", "..", "schedule.demo.xlsx")
MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.security import issue_admin_token

    if not os.path.isfile(REAL):
        pytest.skip("缺少 schedule.demo.xlsx")
    db = ScheduleDatabase()
    db.load_from_excel(REAL)

    monkeypatch.setattr(api, "_DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setattr(ms, "STORE_PATH", str(tmp_path / "s.json"))
    monkeypatch.setattr(api, "_db", db)
    monkeypatch.setattr(api, "_schedule_fp", "fp-test")
    api._master_data.__init__()
    api._master_tasks.clear()
    api._ensure_master_data()

    c = TestClient(app)
    c.get("/api/reschedule/teachers")
    c.headers.update({"Authorization": "Bearer " + issue_admin_token("admin")["token"]})
    return c


def _rename_first_teacher(client, new_name):
    raw = client.get("/api/reschedule/export/master-workbook").content
    wb = openpyxl.load_workbook(io.BytesIO(raw))
    ws = wb["教师"]
    old = ws.cell(2, 1).value
    ws.cell(2, 1).value = new_name
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue(), old


def test_import_reports_drift_since_preview(client):
    """预览之后部分数据被改 → 提交结果必须说明"与预览不符"。

    ⚠️ 场景要构造成**既有新增、又有已存在**：
    - 全是已存在（added=0）→ 这是"重复导入同一份文件"，提示 66 个名字只会刷屏
    - 全是新增 → 没有歧义
    只有两者都有时，教秘才会遇到"预览说新增 3 个、实际新增 2 个"的困惑。
    """
    # 文件里：第 2 行改成「审校用教师X」（新名字），第 3 行保持原样（已存在）
    raw = client.get("/api/reschedule/export/master-workbook").content
    wb = openpyxl.load_workbook(io.BytesIO(raw))
    ws = wb["教师"]
    ws.cell(2, 1).value = "审校用教师X"
    buf = io.BytesIO()
    wb.save(buf)
    payload = buf.getvalue()

    pv = client.post("/api/reschedule/master/import-preview",
                     files={"file": ("m.xlsx", payload, MIME)})
    assert pv.status_code == 200, pv.text[:300]

    # 中间：另一个入口先把新名字建出来 → 预览的那条"新增"落空
    api._master_data.upsert_teacher("审校用教师X")
    api._persist_master_state()

    r = client.post("/api/reschedule/master/import",
                    files={"file": ("m.xlsx", payload, MIME)})
    assert r.status_code == 200, r.text[:300]
    d = r.json()

    st = d["stats"]["teachers"]
    assert st["added"] == 0, "前提：新名字已被建出，本次不该再新增"
    msg = d.get("message") or ""
    assert any(k in msg for k in ("预览", "此期间", "期间", "已被")), (
        "提交结果没说「预览之后数据变了」—— 教秘看到 added=0 会以为导入失败。"
        f"实际 message：{msg!r}"
    )


def test_import_reports_stale_when_same_run_twice(client):
    """同一份文件连提两次：第二次新增为 0，且要说明这是重复导入。"""
    renamed, _ = _rename_first_teacher(client, "审校用教师Y")

    r1 = client.post("/api/reschedule/master/import",
                     files={"file": ("m.xlsx", renamed, MIME)}).json()
    assert r1["stats"]["teachers"]["added"] == 1

    r2 = client.post("/api/reschedule/master/import",
                     files={"file": ("m.xlsx", renamed, MIME)}).json()
    assert r2["stats"]["teachers"]["added"] == 0
    msg = r2.get("message") or ""
    assert "已导入" in msg, "第二次导入连基本回执都没有"
    # 允许两条路：明说"无新增"，或明说"已存在"
    assert ("新建实体 0 个" in msg) or ("已存在" in msg) or ("预览" in msg), (
        f"第二次导入没说清为什么没有新增：{msg!r}"
    )
