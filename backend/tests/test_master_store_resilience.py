"""主数据落盘的**失败路径**契约（R-101）。

为什么单列
----------
`master_store` 的职责只有一句："别丢教秘补录的主数据"。它的正常路径被
`test_master_*` 覆盖得很好，但覆盖率实测有 14 行从未执行 —— 全是**IO 失败分支**：

    文件不存在 / 坏 JSON / 结构不对 / 旧版本 / 写盘失败 / 临时文件清理失败

R-68-A 的教训正是这一类：落盘一旦"静默失效"，内存态一切正常，只有重启后才发现
主数据被一份脏落盘覆盖。所以这些分支必须按契约锁死：

  * 读侧：任何异常 → `None`（调用方回退反推基线），**不许抛**；
  * 写侧：任何失败 → `False` + 提示，**不许抛**，且**不许破坏已有文件**（原子写）。
"""

import json
import os

from app import master_store as ms


def _valid_payload():
    return {"master": {"teachers": ["吴琳"]}, "fingerprint": "x::1"}


# ---------------------------------------------------------------- 读侧

def test_load_missing_file_returns_none(tmp_path):
    assert ms.load(str(tmp_path / "nope.json")) is None


def test_load_corrupt_json_returns_none_and_warns(tmp_path, capsys):
    p = tmp_path / "store.json"
    p.write_text("{not json", encoding="utf-8")
    assert ms.load(str(p)) is None
    assert "落盘文件读取失败" in capsys.readouterr().out


def test_load_non_dict_returns_none(tmp_path):
    p = tmp_path / "store.json"
    p.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    assert ms.load(str(p)) is None


def test_load_dict_without_master_key_returns_none(tmp_path):
    p = tmp_path / "store.json"
    p.write_text(json.dumps({"schema_version": ms.SCHEMA_VERSION}), encoding="utf-8")
    assert ms.load(str(p)) is None


def test_load_old_schema_version_is_discarded(tmp_path, capsys):
    """旧版本落盘必须整体作废 —— 否则它会遮蔽新的反推基线（R-58 的版本门）。"""
    p = tmp_path / "store.json"
    p.write_text(json.dumps({"master": {"teachers": []},
                             "schema_version": ms.SCHEMA_VERSION - 1}), encoding="utf-8")
    assert ms.load(str(p)) is None
    assert "旧版本格式" in capsys.readouterr().out


def test_save_then_load_roundtrip(tmp_path):
    p = tmp_path / "store.json"
    assert ms.save(_valid_payload(), str(p)) is True
    got = ms.load(str(p))
    assert got is not None
    assert got["master"] == {"teachers": ["吴琳"]}
    assert got["schema_version"] == ms.SCHEMA_VERSION


# ---------------------------------------------------------------- 写侧

def test_save_failure_returns_false_without_raising(tmp_path):
    """父路径是个**文件**时 makedirs 必然失败：落盘是尽力而为，不能把排课拖崩。"""
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    assert ms.save(_valid_payload(), str(blocker / "store.json")) is False


def test_save_is_atomic_and_keeps_old_file_on_failure(tmp_path, monkeypatch):
    """`os.replace` 失败（磁盘满/权限）时，**旧文件必须原样还在**、临时文件要清掉。

    这条是"原子写"的真正含义：读者永远看不到半截 JSON。
    """
    p = tmp_path / "store.json"
    assert ms.save({"master": {"teachers": ["旧值"]}}, str(p)) is True
    before = p.read_text(encoding="utf-8")

    def boom(*_a, **_k):
        raise OSError("磁盘满了")

    monkeypatch.setattr(ms.os, "replace", boom)
    assert ms.save({"master": {"teachers": ["新值"]}}, str(p)) is False

    assert p.read_text(encoding="utf-8") == before, "失败时旧文件被改坏了"
    assert not list(tmp_path.glob("*.tmp")), "失败后留下了临时文件"


def test_save_overwrites_previous_content(tmp_path):
    p = tmp_path / "store.json"
    ms.save({"master": {"teachers": ["A"]}}, str(p))
    ms.save({"master": {"teachers": ["B"]}}, str(p))
    assert ms.load(str(p))["master"] == {"teachers": ["B"]}


def test_clear_removes_file_and_is_idempotent(tmp_path):
    p = tmp_path / "store.json"
    ms.save(_valid_payload(), str(p))
    ms.clear(str(p))
    assert not p.exists()
    ms.clear(str(p))  # 再删一次不许抛
    assert ms.load(str(p)) is None


def test_save_stamps_schema_version_even_if_caller_sets_wrong_one(tmp_path):
    """调用方传什么版本都不算数 —— 写侧统一盖章，读侧才敢信这个字段。"""
    p = tmp_path / "store.json"
    ms.save({"master": {}, "schema_version": 0}, str(p))
    assert ms.load(str(p))["schema_version"] == ms.SCHEMA_VERSION


def test_fingerprint_changes_when_schedule_file_changes(tmp_path):
    """安排表指纹 = 路径+mtime：表换了，落盘主数据就该作废（回退新基线）。"""
    f = tmp_path / "schedule.xlsx"
    f.write_text("v1", encoding="utf-8")
    fp1 = ms.schedule_fingerprint(str(f))
    assert fp1 and str(f.resolve()) in fp1

    os.utime(f, (1, 1))  # 改 mtime
    fp2 = ms.schedule_fingerprint(str(f))
    assert fp2 != fp1, "表变了但指纹没变 —— 旧主数据会继续遮蔽新基线"

    assert ms.schedule_fingerprint(None) is None
    assert ms.schedule_fingerprint(str(tmp_path / "missing.xlsx")) is None
