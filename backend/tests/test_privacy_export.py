"""联系方式脱敏锁死（2026-10-24 交付评审 P0-2）

背景
----
仓库里的真实 `schedule.xlsx` 的「教师联系表」携带 17 个教师手机号，而发布会
把整个仓库目录上传（上传器只排除 node_modules/.git/构建产物，**不读
.gitignore**）——手机号属于"已上膛未击发"：当前没有任何 HTTP 端点泄露它
（评审方线上实测 `/export/master-data`、`/teachers` 均 0 命中），但任何后续
新增的"原始表下载"能力、或沙箱内的路径遍历/日志误读，都会直接引爆。

本文件把"联系方式不进任何响应"锁死在三层：
  1. **解析层**：`_parse_teacher_roles` 只取「姓名 + 备注」两列，手机号/邮箱列
     结构上不被读取（合成带毒文件验证）；
  2. **API 层**：带毒文件经 `/upload` 真走一遍后，`/teachers` 与四个导出端点
     （class-schedule / room-schedule / master-data / master-workbook）
     响应里不得出现任何手机号/邮箱形态的字符串；
  3. **真实数据层**：仓库根的 `schedule.xlsx`（若在）原样驱动导出断言——
     R-55 铁律：合成数据全绿 ≠ 真实数据过。

⚠️ 本文件**绝不写入真实手机号**（那等于把 PII 提交进仓库）；一律用形态匹配
正则 `1[3-9]\\d{9}` 与 `13800138000` 这类公开测试号。
"""

import os
import re
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.api import reschedule as api
from app.main import app
from app.master_data import MasterData
from app.security import issue_admin_token
from app.utils.excel_parser import ScheduleDatabase

REPO_ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
REAL_XLSX = os.path.join(REPO_ROOT, "schedule.xlsx")

PHONE_RE = re.compile(r"1[3-9]\d{9}")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")

# 导出/读取端点：匿名可调的整表导出面（评审报告 P1-2 列名的六个里的五个读端点）
_EXPORT_PATHS = [
    "/api/reschedule/teachers",
    "/api/reschedule/export/class-schedule",
    "/api/reschedule/export/room-schedule",
    "/api/reschedule/export/master-data",
    "/api/reschedule/export/master-workbook",
]

# 公开测试号（形态合法、非真实号码）：评审报告样本同形态
TOXIC_PHONES = ["13800138000", "13900139000", "15000150000"]
TOXIC_EMAILS = ["wu@example.com", "lin@example.com"]


def _admin():
    return {"Authorization": f"Bearer {issue_admin_token('admin')['token']}"}


def _write_toxic_xlsx(path):
    """造一份"带毒"的安排表：联系表里手机号/邮箱齐全，另附一张系办电话表。"""
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "大学物理实验安排表 (周) "
    ws.append(["表头占位1"])
    ws.append(["表头占位2"])
    for r in [(2, "周一", "567", "甲班", "1组", "项目A", "A410", "吴老师"),
              (2, "周一", "678", "甲班", "2组", "项目A", "A410", "林老师"),
              (3, "周二", "567", "甲班", "1组", "项目B", "A412", "吴老师")]:
        ws.append(list(r))

    ct = wb.create_sheet("教师联系表")
    ct.append(["教师联系表（标题行占位）"])
    ct.append(["姓名", "手机号", "邮箱", "备注"])
    ct.append(["吴老师", TOXIC_PHONES[0], TOXIC_EMAILS[0], "负责人"])
    ct.append(["林老师", TOXIC_PHONES[1], TOXIC_EMAILS[1], "实验室管理员"])

    ph = wb.create_sheet("学生所在系系办电话")
    ph.append(["学生所在系系办电话（标题行占位）"])
    ph.append(["系别", "办公电话"])
    ph.append(["物理系", "010-12345678"])
    wb.save(path)


def _assert_no_pii(blob, where):
    """响应体里不得出现手机号/邮箱形态的字符串。

    ⚠️ 扫之前先把 xlsx **解压**（2026-10-29 修的一个假阳性）：
    原实现直接 `blob.decode("utf-8","ignore")` 然后正则扫 —— 但 xlsx 是 zip，
    压缩流里**随机字节**偶尔会拼出 `T@I.Z` 这种"邮箱形态"。
    实测一次全量跑就这么红过（`泄露邮箱形态字符串：['T@I.Z']`），
    而同一份导出单独跑 20 次都复现不到 —— 典型的 flaky 假阳性。

    它比"漏测"更难缠：报的是"泄露个人信息"，最容易被当成真问题去查；
    而实际上那段字节来自 deflate 压缩流，与单元格内容毫无关系。

    解压后只扫**文本部件**（xml / rels），既不会假阳性，也照样能抓到真泄漏
    （真泄漏必然出现在 sheet XML 的字符串里）。
    """
    if isinstance(blob, bytes) and blob[:2] == b"PK":
        import io
        import zipfile

        parts = []
        try:
            with zipfile.ZipFile(io.BytesIO(blob)) as z:
                for name in z.namelist():
                    if name.endswith((".xml", ".rels")):
                        parts.append(z.read(name).decode("utf-8", "ignore"))
        except zipfile.BadZipFile:
            parts.append("")            # 坏包由调用方各自断言，这里不吞错
        text = "\n".join(parts)
    else:
        text = blob.decode("utf-8", "ignore") if isinstance(blob, bytes) else str(blob)
    phones = PHONE_RE.findall(text)
    emails = EMAIL_RE.findall(text)
    assert not phones, f"{where} 泄露手机号形态字符串：{phones[:3]}"
    assert not emails, f"{where} 泄露邮箱形态字符串：{emails[:3]}"


# ---------------------------------------------------------------- 1. 解析层

def test_teacher_roles_never_contains_contact_info(tmp_path):
    """教师联系表只取姓名+备注；手机号/邮箱列结构上不进 roles，也不进 entries。"""
    f = tmp_path / "toxic.xlsx"
    _write_toxic_xlsx(str(f))

    db = ScheduleDatabase()
    db.load_from_excel(str(f))

    roles = db.get_teacher_roles()
    assert roles == {"吴老师": "负责人", "林老师": "实验室管理员"}, roles
    for name, note in roles.items():
        assert not PHONE_RE.search(note) and not EMAIL_RE.search(note), (name, note)
        assert name in ("吴老师", "林老师")

    # 全字段扫描：任何一条 entry 的任何文本字段都不得带联系方式
    for e in db.entries:
        for value in (e.weekday, e.time_slot, e.class_name, e.group,
                      e.experiment, e.room, e.teacher):
            assert not PHONE_RE.search(str(value or "")), value
            assert not EMAIL_RE.search(str(value or "")), value


def test_office_phone_sheet_is_not_parsed_at_all(tmp_path):
    """「学生所在系系办电话」整张表不在解析范围内（评审报告 R-09 残余暴露面①）。"""
    f = tmp_path / "toxic.xlsx"
    _write_toxic_xlsx(str(f))
    db = ScheduleDatabase()
    db.load_from_excel(str(f))
    dumped = repr(db.entries) + repr(db.pending) + repr(db.get_teacher_roles())
    assert "010-12345678" not in dumped
    assert not PHONE_RE.search(dumped)


# ---------------------------------------------------------------- 2. API 层（合成带毒文件）

@pytest.fixture
def toxic_env(tmp_path, monkeypatch):
    """隔离环境：/upload 会把文件写进 _DATA_DIR 并重置主数据/落地账本。"""
    db = ScheduleDatabase()           # 空库：upload 会整体重载它
    monkeypatch.setattr(api, "_db", db)
    monkeypatch.setattr(api, "_master_data", MasterData())
    monkeypatch.setattr(api, "_master_tasks", [])
    monkeypatch.setattr(api, "_DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setattr(api, "_landing_store", None)
    monkeypatch.setattr(api, "_DATA_DIR", str(tmp_path / "uploads"))
    return TestClient(app), tmp_path


def test_exports_after_upload_never_leak_contact_info(toxic_env):
    """带毒文件上传成功后，五个读/导出端点响应零联系方式（匿名即可调）。"""
    client, tmp_path = toxic_env
    f = tmp_path / "toxic.xlsx"
    _write_toxic_xlsx(str(f))

    r = client.post("/api/reschedule/upload",
                    files={"file": ("toxic.xlsx", f.read_bytes(),
                                    "application/vnd.ms-excel")},
                    headers=_admin())
    assert r.status_code == 200, r.text

    # 反假绿：带毒文件必须真的加载进来了（教师+条目都在），
    # 否则后面的"0 命中"是因为什么都没读到，vacuous green。
    body = r.json()
    assert body["total_entries"] == 3, body
    assert set(body["teachers"]) == {"吴老师", "林老师"}, body["teachers"]

    for path in _EXPORT_PATHS:
        resp = client.get(path)       # 全部匿名：导出面本就是免鉴权的
        assert resp.status_code == 200, f"{path} → {resp.status_code}"
        _assert_no_pii(resp.content, path)


# ---------------------------------------------------------------- 3. 真实数据层

@pytest.mark.skipif(not os.path.exists(REAL_XLSX), reason="仓库根没有 schedule.xlsx")
def test_real_schedule_exports_never_leak_contact_info(monkeypatch):
    """R-55：真实表（含 17 个手机号的联系表）原样驱动五个端点，零泄露。"""
    db = ScheduleDatabase()
    db.load_from_excel(REAL_XLSX)
    assert db.entries, "真实表应加载出条目"
    monkeypatch.setattr(api, "_db", db)
    monkeypatch.setattr(api, "_master_data", MasterData())

    client = TestClient(app)
    for path in _EXPORT_PATHS:
        resp = client.get(path)
        assert resp.status_code == 200, f"{path} → {resp.status_code}"
        _assert_no_pii(resp.content, path)
