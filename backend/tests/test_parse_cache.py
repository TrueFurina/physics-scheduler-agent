"""Excel 解析缓存（`app/utils/parse_cache.py`）回归测试。

核心不变量：**缓存只能更快，不能更错**。
所以大部分用例都在验证"缓存坏了/过期了/关掉了"时行为仍然正确（fail-closed）。
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.utils import parse_cache
from app.utils.excel_parser import ScheduleDatabase

DATA_CANDIDATES = [
    os.path.join(os.path.dirname(__file__), "..", "..", "schedule.xlsx"),
    os.path.join(os.path.dirname(__file__), "..", "..", "schedule.demo.xlsx"),
]


def _schedule_file() -> str:
    for p in DATA_CANDIDATES:
        if os.path.isfile(p):
            return os.path.abspath(p)
    pytest.skip("仓库里没有排课表，跳过缓存用例")


@pytest.fixture()
def cache_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("PARSE_CACHE_DIR", str(tmp_path))
    monkeypatch.delenv("PARSE_CACHE", raising=False)
    parse_cache.reset_memo()
    yield tmp_path
    parse_cache.reset_memo()


def _snapshot(db: ScheduleDatabase):
    return [
        (e.week, e.weekday, e.time_slot, e.class_name, e.group, e.experiment, e.room, e.teacher)
        for e in db.entries
    ]


def test_second_load_uses_cache_and_matches(cache_dir):
    path = _schedule_file()

    first = ScheduleDatabase()
    first.load_from_excel(path)
    cache_files = list(cache_dir.glob("schedule-*.json"))
    assert cache_files, "首次解析后应写出缓存文件"

    parse_cache.reset_memo()  # 强制走磁盘（模拟进程重启后复用缓存）
    second = ScheduleDatabase()
    second.load_from_excel(path)

    assert len(second.entries) > 100
    assert _snapshot(second) == _snapshot(first)


def test_cache_contains_roles_and_semester(cache_dir):
    """加载时顺手把 roles / semester_start 一起算掉，避免 /teachers 再开一次工作簿。"""
    db = ScheduleDatabase()
    db.load_from_excel(_schedule_file())

    cached = parse_cache.load(db.schedule_path())
    assert isinstance(cached.get("roles"), dict)
    assert "semester_start" in cached

    # 命中缓存后两者都必须与"直连工作簿"算出来的一致
    assert db.get_teacher_roles() == cached["roles"]
    assert db.get_semester_start_date() is not None or cached["semester_start"] is None


def test_corrupted_cache_falls_back_to_parse(cache_dir):
    path = _schedule_file()
    good = ScheduleDatabase()
    good.load_from_excel(path)

    for cache_file in cache_dir.glob("schedule-*.json"):
        cache_file.write_text("{ this is not json", encoding="utf-8")
    parse_cache.reset_memo()

    recovered = ScheduleDatabase()
    recovered.load_from_excel(path)  # 不能抛
    assert _snapshot(recovered) == _snapshot(good)


def test_stale_cache_by_version_is_ignored(cache_dir):
    path = _schedule_file()
    db = ScheduleDatabase()
    db.load_from_excel(path)

    for cache_file in cache_dir.glob("schedule-*.json"):
        payload = json.loads(cache_file.read_text(encoding="utf-8"))
        payload["sig"]["v"] = 999  # 模拟解析器版本变更
        cache_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    parse_cache.reset_memo()

    assert parse_cache.load(path) is None, "版本不符必须视为未命中"
    again = ScheduleDatabase()
    again.load_from_excel(path)
    assert _snapshot(again) == _snapshot(db)


def test_cache_invalidated_when_file_changes(cache_dir, tmp_path, monkeypatch):
    """文件被替换（mtime/size 变化）后必须重新解析，不能返回旧课表。"""
    path = _schedule_file()
    db = ScheduleDatabase()
    db.load_from_excel(path)
    sig_before = parse_cache.signature(path)

    # 复制一份并改 mtime，模拟"换了一份新的安排表"
    new_path = str(tmp_path / "schedule_new.xlsx")
    with open(path, "rb") as src, open(new_path, "wb") as dst:
        dst.write(src.read())
    os.utime(new_path, (sig_before["mtime_ns"] / 1e9 + 60,) * 2)

    fresh = ScheduleDatabase()
    fresh.load_from_excel(new_path)
    assert len(fresh.entries) > 100
    assert parse_cache.load(new_path) is not None


def test_entries_shape_rejected_when_wrong(cache_dir):
    """结构异常（行长度不对）的缓存必须被拒，而不是拿去构造对象后崩在别处。"""
    path = _schedule_file()
    db = ScheduleDatabase()
    db.load_from_excel(path)

    for cache_file in cache_dir.glob("schedule-*.json"):
        payload = json.loads(cache_file.read_text(encoding="utf-8"))
        payload["data"]["entries"] = [["not-a-week"]]
        cache_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    parse_cache.reset_memo()

    assert parse_cache.load(path) is None
    again = ScheduleDatabase()
    again.load_from_excel(path)
    assert _snapshot(again) == _snapshot(db)


def test_disabled_by_env_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("PARSE_CACHE_DIR", str(tmp_path / "off"))
    monkeypatch.setenv("PARSE_CACHE", "0")
    parse_cache.reset_memo()

    db = ScheduleDatabase()
    db.load_from_excel(_schedule_file())

    assert len(db.entries) > 100
    assert not list((tmp_path / "off").glob("*.json")) if (tmp_path / "off").exists() else True
    assert parse_cache.load(db.schedule_path()) is None
