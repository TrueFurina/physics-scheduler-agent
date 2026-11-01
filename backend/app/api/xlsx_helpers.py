"""XLSX 导出助手 —— 从 `app/api/reschedule.py` 拆出来的第一步。

为什么先拆这一组（2026-11-01 实测，不是"看起来像"）：

1. **纯函数**：入参 → 出参，没有共享状态、没有 DB、没有 `_DATA_DIR`。
   所以它们不可能踩 conftest 那个 `monkeypatch.setattr(_api, "_DATA_DIR", ...)`
   只 patch 模块属性的陷阱 —— 搬走之后补丁照样生效，因为压根没人读它。
2. **测试零引用**：`grep -rnw` 七次全 0（不是子串匹配；`_write_row`/`_sheet_name`
   这类子串曾经骗过一次，见 R50 记录的教训）。搬它们不需要改任何既有测试。
3. **零 patch 点**：`tests/` 下没有一处 `monkeypatch.setattr(reschedule, "_safe_sheet_name")`。

为什么**不**按 `docs/拆分方案-reschedule-api-20261024.md` 的第 1 步走（建 state.py）：
那一步要同时动 120 处 patch 点与 5 处 `global` 重指派，远超单轮"≤2 个源文件"的约束。
文档把它列为第 1 步，是按"逐步 commit、每步可回退"规划的顺序，不是按文件数规划的。

老位置（`reschedule.py`）保留 `from app.api.xlsx_helpers import (...)` 转发，
所以 `api._safe_sheet_name` 这类"模块属性直调"的写法仍然可用 ——
本轮不改变任何调用点，只搬家。
"""

import re

__all__ = [
    "_SLOT_ORDER",
    "_XLSX_MEDIA",
    "_entry_sort_key",
    "_safe_sheet_name",
    "_slot_text",
    "_workbook_bytes",
    "_write_sheet",
    "_xlsx_bytes_response",
    "_xlsx_response",
]

# 节次渲染顺序（与 time_model.ORDER 一致；unknown 排最后，绝不静默丢弃）
_SLOT_ORDER = {"MORNING": 0, "567": 1, "678": 2, "91011": 3, "78": 4}

_XLSX_MEDIA = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _safe_sheet_name(raw: str, used: set) -> str:
    """Excel 表名：最长 31 字符，且不允许 : \\ / ? * [ ]。重名加 ~N 后缀。"""
    name = re.sub(r"[:\\/?*\[\]]", "_", str(raw or "").strip())[:31] or "未标注"
    base, i = name, 1
    while name in used:
        suffix = f"~{i}"
        name = base[: 31 - len(suffix)] + suffix
        i += 1
    used.add(name)
    return name


def _slot_text(e) -> str:
    """给教秘看的上课时间：原表写的是时间区间就照抄（含 14:00-17:00 这类跨节次写法），
    否则用节次码换成时钟（567 → 13:30-15:45）。"""
    raw = (getattr(e, "time_raw", "") or "").strip()
    if ":" in raw or "：" in raw:
        return raw
    from app.utils.time_model import clock_text

    return clock_text(e.time_slot) or raw or e.time_slot


def _entry_sort_key(e):
    from app.engine.parallel_scheduler import ALL_WEEKDAYS

    wd = ALL_WEEKDAYS.index(e.weekday) if e.weekday in ALL_WEEKDAYS else 99
    return (e.week, wd, _SLOT_ORDER.get(e.time_slot, 99), str(e.group))


def _workbook_bytes(wb) -> bytes:
    """工作簿 → xlsx 字节。**纯 CPU，重活必须在工作线程里调用**（R-70）。"""
    from io import BytesIO

    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _xlsx_bytes_response(data: bytes, filename: str):
    """字节 → 带中文文件名的下载响应（与并行排课导出同一套口径）。"""
    from io import BytesIO
    from urllib.parse import quote

    from fastapi.responses import StreamingResponse

    return StreamingResponse(
        BytesIO(data),
        media_type=_XLSX_MEDIA,
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"},
    )


def _xlsx_response(wb, filename: str):
    """轻量路径：小工作簿在当前线程直接序列化（实测 ≤50ms 的导出才可走这里）。

    重导出（班级/场地/两份台账/自动排课初稿，实测 77–830ms）一律拆成
    `_build_*_bytes()` + `run_in_threadpool` —— 否则建表期间事件循环停摆，
    期间全站请求（含 /health）一起排队，见 R-70。
    """
    return _xlsx_bytes_response(_workbook_bytes(wb), filename)


def _write_sheet(ws, headers, widths):
    from openpyxl.styles import Alignment, Font

    ws.append(headers)
    for c in ws[1]:
        c.font = Font(bold=True)
        c.alignment = Alignment(horizontal="center")
    for col, w in zip("ABCDEFGH", widths):
        ws.column_dimensions[col].width = w
