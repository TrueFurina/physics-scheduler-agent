"""Excel 解析缓存（`app/utils/parse_cache.py`）回归测试。

核心不变量：**缓存只能更快，不能更错**。
所以大部分用例都在验证"缓存坏了/过期了/关掉了"时行为仍然正确（fail-closed）。
"""

import json
import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.utils import parse_cache
from app.utils.excel_parser import ScheduleDatabase, ScheduleEntry, _LoadStage

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


# ---------------------------------------------------------------------------
# R-66：磁盘缓存"真命中"反假绿 + 行宽耦合 + 显式 course 往返
#
# 背景（实测坐实的真 bug）：`entries_to_rows` 自 v3 起产出 10 字段行（time_raw/blocked），
# 而 `_validate` 仍按 8 字段校验 ⇒ 磁盘缓存恒被判结构异常、永远 MISS、每次冷启动重解析。
# 旧用例 test_second_load_uses_cache_and_matches 即使磁盘 MISS 也靠 fail-closed 重解析
# 得到相同快照而变绿 —— 典型假绿（R-25）。下面第一条专治它：缺陷版上必红。
# ---------------------------------------------------------------------------

def test_disk_cache_actually_hits_after_reset_memo(cache_dir):
    """reset_memo 后走**磁盘**路径，必须真命中（而非静默重解析）。

    缺陷版（_validate 行长校验错位）下 parse_cache.load 返回 None ⇒ 本用例红。
    """
    path = _schedule_file()
    first = ScheduleDatabase()
    first.load_from_excel(path)
    assert list(cache_dir.glob("schedule-*.json")), "首次解析应写出磁盘缓存文件"

    parse_cache.reset_memo()                    # 清空进程内 memo，强制走磁盘
    cached = parse_cache.load(path)
    assert cached is not None, "磁盘缓存必须命中（_validate 行长校验曾错位导致恒 MISS）"
    assert len(cached["entries"]) == len(first.entries) > 100


def test_entries_row_width_matches_validate(cache_dir):
    """序列化行宽必须 == _validate 期望的 ENTRY_ROW_WIDTH（防止两者再次漂移成假绿）。"""
    db = ScheduleDatabase()
    db.load_from_excel(_schedule_file())
    rows = parse_cache.entries_to_rows(db.entries)
    assert rows, "真实表应有序列化行"
    assert len(rows[0]) == parse_cache.ENTRY_ROW_WIDTH
    # 且 _validate 认这份真实 payload（不是只认合成的小行）
    payload = {"sig": parse_cache.signature(db.schedule_path()),
               "data": {"entries": rows}}
    assert parse_cache._validate(payload, payload["sig"]) is True


def test_course_survives_cache_row_roundtrip():
    """显式 course 必须随缓存行往返（entries_to_rows → ScheduleEntry(*row)）。"""
    from app.utils.excel_parser import ScheduleEntry
    e = ScheduleEntry(1, "周一", "567", "C1", "g1", "扭摆法", "A504", "吴琳",
                      "", ["567"], "光学实验")
    row = parse_cache.entries_to_rows([e])[0]
    assert len(row) == parse_cache.ENTRY_ROW_WIDTH
    e2 = ScheduleEntry(*row)
    assert e2.course == "光学实验"
    assert e2.experiment == "扭摆法" and e2.blocked == ["567"]


# ---------------------------------------------------------------- 加载必须原子（R-87）
#
# 2026-10-29 实测的并发窗口：`load_from_excel` 原来 `self.entries = []` 之后逐行
# `append`，`_build_index()` 在最后才跑。433 行冷解析时，读者在 1263–1270ms 读到
# `entries=120`，而索引 1309ms 才建好 —— 约 46ms 里"课表只剩 120 条"。
# 这个函数在 `/load-default`、`/demo/reset` 里都跑在**线程池**上（R-70），
# 所以不是理论问题：教秘点"重新加载"时，别人的页面会刷出残缺课表。
#
# 修法是"构建到临时暂存器 → 一次性提交"。下面两条锁住它：
#   · 结构锁：`load_from_excel` 体内不许再出现 `self.entries = []` / `self.entries.append`；
#   · 行为锁：并发读的每一次采样，entries 要么是旧值、要么是满值。



def test_load_builds_into_stage_not_in_place():
    """结构锁：解析期间不许就地清空/追加 `self.entries`（否则读者能看到半张表）。

    2026-10-29 起实现体拆成两层：`load_from_excel`（持 `_LOAD_LOCK`，只做委派）
    + `_load_from_excel_locked`（真正的解析）。两者都要查 —— 只看外层会因为
    "真实逻辑搬走了"而假绿。
    """
    import inspect
    src = (inspect.getsource(ScheduleDatabase.load_from_excel)
           + inspect.getsource(ScheduleDatabase._load_from_excel_locked))
    code = "\n".join(line.split("#")[0] for line in src.split("\n"))
    for bad in ("self.entries = []", "self.entries.append"):
        assert bad not in code, (
            f"load_from_excel 里还有就地改写（{bad}）——"
            "并发读者会看到「表被清空、只填了一半」的中间态。应改为构建 _LoadStage 后 _commit_stage。"
        )
    assert "_commit_stage" in code, "没有走暂存器提交路径"


def test_commit_stage_swaps_entries_and_indexes_together():
    """提交必须**成对替换** entries 与四个索引 —— 否则会出现"新表 + 旧索引"。"""
    d = ScheduleDatabase()
    d.entries = [ScheduleEntry(1, "周一", "567", "旧班", "g", "旧项目", "A1", "旧师")]
    d._build_index()
    assert d._by_teacher  # 前提：旧索引非空

    stage = _LoadStage()
    stage.entries = [ScheduleEntry(2, "周二", "678", "新班", "g", "新项目", "A2", "新师")]
    stage.sheet_name = "新表"
    d._commit_stage(stage)

    assert [e.week for e in d.entries] == [2]
    assert set(d._by_teacher) == {"新师"}, f"索引没跟着换：{set(d._by_teacher)}"
    assert set(d._by_class) == {"新班"}
    assert d.sheet_name == "新表"
    assert d.loaded is True


def test_concurrent_reader_never_sees_a_partial_table(tmp_path, monkeypatch):
    """行为锁：并发读的每次采样，entries 要么是旧值(0 条)、要么是满值(3 条)。

    用小表 + 强制冷解析（不打缓存），把"清空 → 逐行 append"的窗口拉出来。
    修复前该窗口能让读者读到 1 或 2 条；修复后只可能读到 0 或 3。
    """
    import os as _os
    monkeypatch.setenv("PARSE_CACHE_DIR", str(tmp_path / "cache"))
    from app.utils import parse_cache
    monkeypatch.setattr(parse_cache, "load", lambda p: None)   # 强制真解析

    xlsx = _os.path.join(_os.path.dirname(__file__), "..", "..", "schedule.xlsx")
    if not _os.path.exists(xlsx):
        pytest.skip("真实排课表不在仓库里")

    d = ScheduleDatabase()
    d.load_from_excel(xlsx)                       # 先装一份，做"旧表"
    full = len(d.entries)
    assert full > 100, f"真实表应有几百条，实得 {full}"

    bad: list[int] = []
    stop = threading.Event()

    def reader():
        while not stop.is_set():
            n = len(d.entries)
            if 0 < n < full:                      # 半张表 = 中间态
                bad.append(n)

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    for _ in range(3):
        d.load_from_excel(xlsx)
    stop.set()
    t.join(timeout=5)

    assert not bad, f"并发读者看到了半张表（样本 {bad[:5]}，共 {len(bad)} 次）"


def test_concurrent_loads_do_not_overwrite_each_other(tmp_path, monkeypatch):
    """并发加载必须串行：不许"我加载完了，读到的却是别人的表"。

    2026-10-29 实测（无锁）：20 轮「两个线程同时加载不同规模的表」有 **2 次**
    某线程在 `load_from_excel` 返回后读到对方的表 —— 因为后提交的那个赢，
    而先提交的调用方以为自己成功了。

    危害比"半张表"更糟：教秘上传 A 表，界面上生效的可能是 B 表，且不报错。
    修法是给整表加载加模块级 `_LOAD_LOCK`（数据源是进程级单例，锁也跟着它）。
    """
    import os as _os
    import threading

    import openpyxl

    monkeypatch.setenv("PARSE_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(parse_cache, "load", lambda p: None)   # 强制真解析，拉长窗口

    real = _os.path.join(_os.path.dirname(__file__), "..", "..", "schedule.xlsx")
    if not _os.path.exists(real):
        pytest.skip("真实排课表不在仓库里")

    big = _os.path.join(str(tmp_path), "big.xlsx")
    import shutil
    shutil.copy2(real, big)

    wb = openpyxl.load_workbook(real)
    ws = wb[wb.sheetnames[1]]
    for r in range(ws.max_row, 6, -1):        # 只留 3 行数据
        ws.delete_rows(r)
    small = _os.path.join(str(tmp_path), "small.xlsx")
    wb.save(small)

    d = ScheduleDatabase()
    d.load_from_excel(big)
    big_n = len(d.entries)
    d.load_from_excel(small)
    small_n = len(d.entries)
    assert big_n > small_n > 0, f"前提：两张表规模不同（{big_n} vs {small_n}）"

    mismatches = []
    for _ in range(10):
        barrier = threading.Barrier(2)
        seen: dict[str, int] = {}

        def load(path, tag, expect, _barrier=barrier, _seen=seen):
            # 默认参数绑定当轮的 barrier/seen：否则闭包会捕获最后一次迭代的对象
            # （ruff B023）。这里每轮都是新对象，绑定后行为才确定。
            _barrier.wait()
            d.load_from_excel(path)
            _seen[tag] = len(d.entries)       # 加载返回后立刻读

        ts = [threading.Thread(target=load, args=(big, "big", big_n)),
              threading.Thread(target=load, args=(small, "small", small_n))]
        for t in ts:
            t.start()
        for t in ts:
            t.join(timeout=30)
        for tag, expect in (("big", big_n), ("small", small_n)):
            if seen.get(tag) != expect:
                mismatches.append((tag, expect, seen.get(tag)))

    assert not mismatches, (
        f"并发加载互相覆盖了 {len(mismatches)} 次（样本 {mismatches[:3]}）——"
        "load_from_excel 必须持 _LOAD_LOCK"
    )
