"""R54 契约测试：`app/api/xlsx_helpers.py` 与 `app/api/reschedule.py` 的转发是否等价。

为什么要有这组：R54 把 7 个 XLSX 纯函数从 3456 行的 `reschedule.py` 搬到了新模块。
"搬家"这种改动最典型的翻车方式不是报错，而是**悄悄换了行为** ——
老位置改成转发之后，`from app.api.reschedule import _safe_sheet_name` 与
`from app.api.xlsx_helpers import _safe_sheet_name` 是不是**同一个对象**，
以及行为有没有在搬运过程中丢掉分支（截断、去重、非法字符），
光看测试全绿是看不出来的 —— 因为既有测试根本没引用这些函数（实测整词命中 0）。

所以这里从两头钉：**身份**（同一个对象，不是两份拷贝）+ **行为**（边界输入逐条比对）。
"""

import types

import pytest

from app.api import reschedule as _res
from app.api import xlsx_helpers as _xh


def _entry(**kw):
    """最小可用的排课记录替身：只带被这两个函数读到的字段。"""
    base = dict(week=1, weekday="周一", time_slot="567", group="", time_raw="")
    base.update(kw)
    return types.SimpleNamespace(**base)


# ---------------------------------------------------------------------------
# 1. 身份：老位置的 7 个名字必须就是新模块里的那 7 个对象
# ---------------------------------------------------------------------------

_FORWARDED = [
    "_safe_sheet_name",
    "_slot_text",
    "_entry_sort_key",
    "_workbook_bytes",
    "_xlsx_bytes_response",
    "_xlsx_response",
    "_write_sheet",
]


@pytest.mark.parametrize("name", _FORWARDED)
def test_forwarded_symbol_is_same_object(name):
    """不是"行为一样"，是 `is` —— 两份拷贝会各自漂移，同一个对象不会。"""
    assert hasattr(_res, name), f"reschedule 上已没有 {name}，老调用点会 ImportError"
    assert getattr(_res, name) is getattr(_xh, name)


def test_constants_moved_and_not_duplicated():
    """`_SLOT_ORDER` / `_XLSX_MEDIA` 只在新模块里定义一份，老文件不该有第二份。"""
    assert _xh._SLOT_ORDER == {"MORNING": 0, "567": 1, "678": 2, "91011": 3, "78": 4}
    assert _xh._XLSX_MEDIA.endswith("spreadsheetml.sheet")
    assert not hasattr(_res, "_SLOT_ORDER"), "老位置还留了一份常量 → 两边会漂移"
    assert not hasattr(_res, "_XLSX_MEDIA"), "老位置还留了一份常量 → 两边会漂移"


# ---------------------------------------------------------------------------
# 2. 表名净化：非法字符 / 长度 / 去重 / 截断后仍唯一
# ---------------------------------------------------------------------------


def test_safe_sheet_name_replaces_illegal_chars():
    used = set()
    name = _xh._safe_sheet_name("a:b\\c/d?e*f[g]h", used)
    for ch in ':\\/?*[]"':
        assert ch not in name, f"非法字符 {ch!r} 没被替换：{name!r}"
    assert name == "a_b_c_d_e_f_g_h"


def test_safe_sheet_name_truncates_to_31_and_falls_back():
    used = set()
    long_name = _xh._safe_sheet_name("周" * 40, used)
    assert len(long_name) == 31
    assert _xh._safe_sheet_name("", set()) == "未标注"
    assert _xh._safe_sheet_name(None, set()) == "未标注"


def test_safe_sheet_name_dedupes_with_suffix():
    used = set()
    a = _xh._safe_sheet_name("班级课表", used)
    b = _xh._safe_sheet_name("班级课表", used)
    c = _xh._safe_sheet_name("班级课表", used)
    assert (a, b, c) == ("班级课表", "班级课表~1", "班级课表~2")
    assert used == {"班级课表", "班级课表~1", "班级课表~2"}


def test_safe_sheet_name_truncated_names_stay_unique():
    """两个长名截断到同一个 31 字符前缀时，绝不能撞名（Excel 会直接报错）。

    前缀必须**长于 31 字**才真正触发碰撞：前 31 字完全相同时，第二个才会被加后缀。
    （第一版把前缀写成 21 字，差异落在第 31 字之内，两个名字本来就不一样 ——
    那是测试写错了，不是实现错了。）
    """
    used = set()
    prefix = "同名长前缀" * 7  # 35 字 > 31
    a = _xh._safe_sheet_name(prefix + "甲", used)
    b = _xh._safe_sheet_name(prefix + "乙", used)
    assert a == prefix[:31], "截断规则变了：应保留前 31 字"
    assert a != b, "截断后撞名了"
    assert len(b) <= 31, f"加后缀后超过 31 字符：{len(b)}"
    assert b.endswith("~1")


# ---------------------------------------------------------------------------
# 3. 节次文本：原表写了时间就照抄，没写就把节次码换成时钟
# ---------------------------------------------------------------------------


def test_slot_text_prefers_raw_clock_range():
    assert _xh._slot_text(_entry(time_raw="14:00-17:00")) == "14:00-17:00"
    # 全角冒号也算"写了时间"
    assert _xh._slot_text(_entry(time_raw="14：00-17：00")) == "14：00-17：00"


def test_slot_text_maps_slot_code_to_clock():
    assert _xh._slot_text(_entry(time_slot="567")) == "13:30-15:45"
    assert _xh._slot_text(_entry(time_slot="MORNING")) == "09:00-11:15"


# ---------------------------------------------------------------------------
# 4. 排序键：同周同节次下按班级稳定排序；未知星期排最后
# ---------------------------------------------------------------------------


def test_entry_sort_key_orders_by_week_weekday_slot_group():
    rows = [
        _entry(week=2, weekday="周一", time_slot="567", group="2班"),
        _entry(week=1, weekday="周一", time_slot="567", group="2班"),
        _entry(week=1, weekday="周一", time_slot="567", group="1班"),
        _entry(week=1, weekday="周二", time_slot="567", group="1班"),
        _entry(week=1, weekday="周一", time_slot="MORNING", group="1班"),
    ]
    got = [(_e.week, _e.weekday, _e.time_slot, _e.group) for _e in sorted(rows, key=_xh._entry_sort_key)]
    assert got == [
        (1, "周一", "MORNING", "1班"),
        (1, "周一", "567", "1班"),
        (1, "周一", "567", "2班"),
        (1, "周二", "567", "1班"),
        (2, "周一", "567", "2班"),
    ]


def test_entry_sort_key_puts_unknown_weekday_last():
    """星期不在 ALL_WEEKDAYS 里时给 99，不许抛 ValueError（否则一条脏数据能炸掉整个导出）。"""
    known = _xh._entry_sort_key(_entry(weekday="周一"))
    unknown = _xh._entry_sort_key(_entry(weekday="周八"))
    assert known[1] == 0
    assert unknown[1] == 99
    assert known < unknown


# ---------------------------------------------------------------------------
# 5. 序列化：产出的字节必须能被 openpyxl 真的重新打开（不是空壳）
# ---------------------------------------------------------------------------


def test_workbook_bytes_roundtrip():
    from io import BytesIO

    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["周次", "星期", "节次"])
    ws.append([1, "周一", "567"])

    data = _xh._workbook_bytes(wb)
    assert isinstance(data, bytes) and data[:2] == b"PK", "不是 zip 容器，openpyxl 打不开"

    back = openpyxl.load_workbook(BytesIO(data))
    assert back.active["A1"].value == "周次"
    assert back.active["C2"].value == "567"


def test_xlsx_bytes_response_sets_chinese_filename():
    resp = _xh._xlsx_bytes_response(b"PK\x03\x04", "班级课表_1班.xlsx")
    disposition = resp.headers["content-disposition"]
    assert disposition.startswith("attachment; filename*=UTF-8''")
    assert "xlsx" in disposition
    assert resp.media_type.endswith("spreadsheetml.sheet")
