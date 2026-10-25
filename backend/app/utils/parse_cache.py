"""Excel 解析结果缓存（按文件签名失效，fail-closed）。

为什么需要
----------
排课表是一个 400+ 行的 xlsx，每次「加载默认表」都要 openpyxl 完整解析一遍
（本地实测 ~1.0s，冷沙箱 3.8s）；而 `get_teacher_roles()` / `get_semester_start_date()`
为了读十几个备注、一行日期，又各自把**整个工作簿**重新打开一次
（`/teachers` 因此每次 ~0.28s，而它每次页面加载都会被调用）。

这份数据是只读的、且只在文件变化时才需要重新解析 —— 典型的一次算、多次用。

失效策略（唯一正确性来源）
------------------------
以 **(绝对路径, mtime_ns, size, 解析器版本)** 作为签名，任一不符即视为未命中并重解析。
这里**刻意不做"猜测式"失效**（比如只看路径、或按天过期）：缓存脏了会静默返回
旧课表，比慢 1 秒危险得多。所以原则是 fail-closed —— 任何可疑情况（读失败、
字段缺失、版本不符、结构异常）一律当作未命中，走真实解析。

环境变量
--------
    PARSE_CACHE=0        关闭缓存（排查"数据看着不对"时第一件事）
    PARSE_CACHE_DIR=...  自定义缓存目录（测试用临时目录）
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

# 解析逻辑变更时 +1：旧缓存自动失效，避免"代码改了但读到旧结构"
# v2：时段别名统一（"78"/15:50-18:05 → 678）+ 新增 skipped（时段无法识别的行）
# v3：新增 time_raw / blocked —— 跨节次写法（14:00-17:00）要同时占住 567 与 678。
#     ⚠️ 结构变了必须失效：否则旧缓存里的条目没有 blocked，判占用会退回单标签，
#     于是"沙盘把 678 判为空闲"这个缺陷会在命中缓存时重现（且难以察觉）。
# v4：新增 pending（没排时间的行 → 待排任务）与 sheet_name。
#     旧缓存里没有 pending ⇒ 命中缓存时"待排任务"会凭空消失（正好是静默丢弃的另一种形态）。
# v5：ScheduleEntry 新增 course（显式课程外键，R-66）。
#     ⚠️ 顺带修一个自 v3 起就潜伏的真 bug：`entries_to_rows` 自 v3 起产出 **10 字段**行
#     （time_raw/blocked），而 `_validate` 仍按 **8 字段**校验 ⇒ 磁盘缓存恒被判为结构异常、
#     永远 MISS、每次冷启动都重解析整表（进程内 memo 绕过校验，故不易察觉；优化路线里
#     "冷启动 0.025s/13.6×" 是 memo 的数字，不是真冷启动）。现在行宽收敛成一处常量
#     `ENTRY_ROW_WIDTH`，序列化与校验共用它，杜绝再次漂移。
# v6：**解析行为变更**（不是结构变更）也必须 bump —— 这是本次踩坑的根因：
#     R-66 期间 `_detect_course_col` 先从"模糊子串"改成"精确别名匹配"（防「课程类别」
#     分类码被误当课程名），但当时没 bump 版本 ⇒ 模糊时代写下的 v5 磁盘缓存
#     （433 条 course=C/B/普物3）签名（path/mtime/size/v）全部仍"有效"而被命中，
#     表现为 test_real_table_baseline 莫名红（distinct_courses==3），
#     隔离实验（PARSE_CACHE=0）却全绿。v5 从未发布、只是本地中间态产物，
#     统一作废即可。教训：签名里的 v 必须跟随**任何**解析逻辑变化，不止结构变化。
# v7：产品决策 B（2026-10-24）—— 没有真课程列时采用「课程类别」分类码列，
#     逐行翻译成课程名（COURSE_CODE_MAP）。解析**行为**又变了（course 从空串
#     变三门课名），同 v6 的教训：不 bump 则 v6 磁盘缓存（course 全空）签名仍
#     "有效"被命中，映射静默不生效。
CACHE_VERSION = 7

# ScheduleEntry 序列化后的字段数（entries_to_rows 产出 / _validate 校验 / ScheduleEntry(*row) 反序列化
# 三处必须一致）。字段顺序即 ScheduleEntry.__init__ 的位置参数顺序：
#   week, weekday, time_slot, class_name, group, experiment, room, teacher, time_raw, blocked, course
ENTRY_ROW_WIDTH = 11

_APP_DIR = Path(__file__).resolve().parents[1]  # backend/app
from app.paths import DATA_DIR as _DATA_DIR

_DEFAULT_DIR = __import__("pathlib").Path(_DATA_DIR) / "cache"

# 进程内记忆：(绝对路径) → {"sig": {...}, "data": {...}}
_MEMO: Dict[str, Dict[str, Any]] = {}
_LOCK = threading.Lock()


def _enabled() -> bool:
    return os.getenv("PARSE_CACHE", "1").strip().lower() not in {"0", "false", "no", "off"}


def cache_dir() -> Path:
    raw = (os.getenv("PARSE_CACHE_DIR") or "").strip()
    return Path(raw) if raw else _DEFAULT_DIR


def signature(filepath: str) -> Optional[Dict[str, Any]]:
    """当前文件的签名；取不到（文件不存在/无权限）返回 None。"""
    try:
        st = os.stat(filepath)
    except OSError:
        return None
    return {
        "v": CACHE_VERSION,
        "path": os.path.abspath(filepath),
        "mtime_ns": st.st_mtime_ns,
        "size": st.st_size,
    }


def _cache_file(filepath: str) -> Path:
    digest = hashlib.sha1(os.path.abspath(filepath).encode("utf-8")).hexdigest()[:12]
    return cache_dir() / f"schedule-{digest}.json"


def _validate(payload: Any, sig: Dict[str, Any]) -> bool:
    """严格校验：宁可多解析一次，也不返回结构可疑的数据。"""
    if not isinstance(payload, dict):
        return False
    cached_sig = payload.get("sig")
    if not isinstance(cached_sig, dict):
        return False
    for key in ("v", "path", "mtime_ns", "size"):
        if cached_sig.get(key) != sig.get(key):
            return False
    data = payload.get("data")
    if not isinstance(data, dict):
        return False
    entries = data.get("entries")
    if not isinstance(entries, list):
        return False
    for row in entries:
        if not isinstance(row, list) or len(row) != ENTRY_ROW_WIDTH:
            return False
        if not isinstance(row[0], int):
            return False
    return True


def load(filepath: str) -> Optional[Dict[str, Any]]:
    """命中则返回缓存的 data 字典，否则 None（调用方必须自己解析）。

    任何异常都被吞掉并返回 None —— 缓存只是加速手段，**不能成为新的故障源**。
    """
    if not _enabled():
        return None
    sig = signature(filepath)
    if sig is None:
        return None

    with _LOCK:
        memo = _MEMO.get(sig["path"])
        if memo and memo["sig"] == sig:
            return memo["data"]

    try:
        raw = _cache_file(filepath).read_text(encoding="utf-8")
        payload = json.loads(raw)
    except Exception:
        return None

    if not _validate(payload, sig):
        return None

    with _LOCK:
        _MEMO[sig["path"]] = {"sig": sig, "data": payload["data"]}
    return payload["data"]


def save(filepath: str, data: Dict[str, Any]) -> None:
    """写入（尽力而为）：失败只意味着下次重新解析，绝不向上抛。"""
    if not _enabled():
        return
    sig = signature(filepath)
    if sig is None:
        return

    payload = {"sig": sig, "data": data}
    try:
        target = _cache_file(filepath)
        target.parent.mkdir(parents=True, exist_ok=True)
        # 原子写：先写临时文件再替换，避免进程在写一半时被回收留下半截 JSON
        fd, tmp = tempfile.mkstemp(dir=str(target.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, separators=(",", ":"))
            os.replace(tmp, target)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
    except Exception:
        return

    with _LOCK:
        _MEMO[sig["path"]] = {"sig": sig, "data": data}


def merge(filepath: str, extra: Dict[str, Any]) -> None:
    """在已有缓存上补字段（例如 roles / semester_start 单独算出来后合并）。"""
    if not _enabled():
        return
    current = load(filepath)
    if current is None:
        return
    merged = dict(current)
    merged.update(extra)
    save(filepath, merged)


def reset_memo() -> None:
    """清空进程内记忆（测试用；磁盘缓存不动）。"""
    with _LOCK:
        _MEMO.clear()


def entries_to_rows(entries: List[Any]) -> List[List[Any]]:
    """ScheduleEntry 列表 → 可 JSON 化的行列表（字段顺序即 ScheduleEntry 构造顺序）。

    末尾三项 `time_raw` / `blocked`（v3）/ `course`（v5）：跨节次记录的"原表写法"和
    "真实占住哪几个节次"、以及"源表自带的显式课程"都必须一起落盘，否则缓存命中时
    判占用会退回单标签、课程归属会丢失显式外键。
    ⚠️ 字段数必须 == ENTRY_ROW_WIDTH，且顺序与 ScheduleEntry.__init__ 位置参数一致
    （反序列化是 `ScheduleEntry(*row)`）—— 二者由 test_parse_cache 的行宽用例锁死。
    """
    return [
        [e.week, e.weekday, e.time_slot, e.class_name, e.group, e.experiment, e.room, e.teacher,
         getattr(e, "time_raw", "") or "", list(getattr(e, "blocked", None) or [e.time_slot]),
         getattr(e, "course", "") or ""]
        for e in entries
    ]
