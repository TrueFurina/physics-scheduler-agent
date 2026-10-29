"""时段标签 ↔ 真实时钟区间的唯一权威。

为什么需要单独一个模块
----------------------
"推荐的时段不会撞课"是本产品对教秘的**唯一硬承诺**，而这条承诺的判据不是
"标签相等"，是"两个时间区间是否真的重叠"。此前每个模块各写一份映射表
（`excel_parser.TIME_SLOTS` / `auto_scheduler._SLOT_SPAN` / 前端 labels），
同一件事有三份实现，其中两份还在按"标签相等"做判定 —— R-34 的标签分裂
（`15:50-18:05` 留成 `78`，谁都不认）就是这么混过去的。

关键概念：**一条记录可能同时占住两个节次**
------------------------------------------
原表里的时段有五种写法：节次码（`567`）、标准时间（`13:30-15:45`）、
以及**跨节次的时间段**（`14:00-17:00`）。最后一种最容易被误处理：

    14:00-17:00  ⊃  567(13:30-15:45) 的前 1 小时 45 分
                 ⊃  678(15:45-18:00) 的前 1 小时 15 分

若把它"吸附"成 `567`，那么该教室这一天的 `678` 会被判为空闲 —— 沙盘会把
教秘推荐到一间正在上课的教室（实测第 12 周周四 A410 就是这种情形）。
所以解析阶段就要算出 `blocked`（真实重叠的所有标准节次），下游一律按
`blocked` 判占用，而不是按 `time_slot` 单个标签。

刻意**不做**的事
----------------
- 不把相邻当成重叠：`567` 结束 15:45、`678` 开始 15:45，首尾相接 ⇒ 不重叠。
- 不认识的时间一律不猜：解析不出区间就返回空列表，由调用方决定"整日占用"
  还是"记进数据缺口"（R-35 的口径：宁可少推荐，不可静默丢弃）。
"""

from __future__ import annotations

import re
from typing import List, Optional, Tuple

# 标准节次 → (起始分钟, 结束分钟)。这是全校排表的共同语言，改这里等于改口径。
CLOCK: dict = {
    "MORNING": (9 * 60, 11 * 60 + 15),          # 09:00-11:15
    "567": (13 * 60 + 30, 15 * 60 + 45),        # 13:30-15:45
    "678": (15 * 60 + 45, 18 * 60),             # 15:45-18:00
    "91011": (18 * 60 + 30, 20 * 60 + 45),      # 18:30-20:45
    # 历史写法：下午 7-8 节。新解析一律归到 678，这里留着只为读得懂旧缓存/旧导出。
    "78": (15 * 60 + 50, 18 * 60 + 5),
}

# 渲染/比较顺序（也用于"主标签"取第一个命中项）
ORDER = ("MORNING", "567", "678", "91011")

_RANGE_RE = re.compile(
    r"^\s*(\d{1,2})\s*[:：]\s*(\d{2})\s*[-~—－]\s*(\d{1,2})\s*[:：]\s*(\d{2})\s*$"
)


def parse_clock_range(raw: Optional[str]) -> Optional[Tuple[int, int]]:
    """把 `14:00-17:00` 解析成 (840, 1020)。不是时间区间就返回 None。"""
    if not raw:
        return None
    m = _RANGE_RE.match(str(raw))
    if not m:
        return None
    h1, m1, h2, m2 = (int(x) for x in m.groups())
    start, end = h1 * 60 + m1, h2 * 60 + m2
    if end <= start:  # 17:00-14:00 这种写反了的，别硬猜
        return None
    return start, end


def intervals_overlap(a: Optional[Tuple[int, int]], b: Optional[Tuple[int, int]]) -> bool:
    """两个区间是否**真的**有交。首尾相接（a.end == b.start）不算。"""
    if not a or not b:
        return False
    return max(a[0], b[0]) < min(a[1], b[1])




def span_labels(raw: Optional[str], fallback: str = "") -> List[str]:
    """一个时段写法**真实占住**的所有标准节次。

    - 时间区间（`14:00-17:00`）⇒ 所有与它真重叠的节次，可能不止一个；
    - 节次码（`567`）⇒ 它自己；
    - 解析不出来 ⇒ `[fallback]`（fallback 为空则是空列表，表示"不认识"）。
    """
    iv = parse_clock_range(raw)
    if iv:
        hit = [label for label in ORDER if intervals_overlap(iv, CLOCK[label])]
        if hit:
            return hit
        # 落在课表节次之外（例如 07:00-08:00）：不猜，交给调用方按数据缺口处理
        return []
    if fallback and fallback in CLOCK:
        return [fallback]
    return [fallback] if fallback else []


def primary_label(raw: Optional[str], fallback: str = "") -> str:
    """主标签：用于展示与统计的那一个。跨节次时取**起始时刻所在**的节次。"""
    iv = parse_clock_range(raw)
    if not iv:
        return fallback
    for label in ORDER:
        c = CLOCK[label]
        if c[0] <= iv[0] < c[1]:
            return label
    hit = span_labels(raw, fallback)
    return hit[0] if hit else fallback


def clock_text(label_or_raw: str) -> str:
    """给人看的时间文案：能解析成区间就原样返回，否则查标签表。"""
    if parse_clock_range(label_or_raw):
        return str(label_or_raw).replace("：", ":")
    c = CLOCK.get(label_or_raw)
    if not c:
        return label_or_raw
    return "%02d:%02d-%02d:%02d" % (c[0] // 60, c[0] % 60, c[1] // 60, c[1] % 60)
