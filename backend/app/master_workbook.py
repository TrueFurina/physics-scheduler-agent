"""主数据台账：可导出 → 可编辑 → 可导回（Excel 批量操作五张实体表）。

为什么单独一个模块
------------------
`/export/master-data` 导出的是**统计台账**（课次/覆盖率聚合列，给教务核对用），
它的 docstring 承诺"可以补列、再导回"，但 `/upload-master` 的 `parse_master_tasks`
认的是**清单型**任务表 —— 两种格式不对齐，导回会静默产出垃圾。本模块把这条
闭环真正接上：

    /export/master-workbook  五张实体表，每表一页，行=实体，列=可编辑字段
    /master/import-preview   先只读解析一遍：页→列→字段映射 + 没认出的列 +
                             统计列 + 样本行给教秘看，**不写任何状态**
                             （与 /master/import 共用 parse_workbook，数字一致）
    /master/import           看过预览再确认：逐表 upsert 进主数据

设计上刻意守的三条防线（前两条是本项目反复踩过的坑）
------------------------------------------------
1. **列靠别名、不靠列序**：不同人写的列名差很远，但同名列必须能认出来；
   别名匹配按**最长命中**优先，避免短别名抢走长别名（"课程"抢"所属课程"）。
2. **统计列不得当实体值**：老统计台账的「班级数」会被"班级"别名命中，
   值却全是数字 —— 直接导入就会把 `5` 当成一个班级名。所以对多值列做
   **值类型校验**：采样行里绝大多数是数值 → 判为统计列，剔除并显式告警，
   绝不静默半导入（R-34 的教训：静默比报错危险）。
3. **导入只做新增/补充，不做删除**：教秘在 Excel 里删一行，语义可能是"这行没变"
   而不是"删掉它"。删实体请用界面上的删除按钮 —— 导入端不猜删除意图。

导出的文件自带「填写说明」页（含版本标记 `MASTER_WORKBOOK_V1`），既是给教秘的
填写指引，也让导入端能确认"这就是那份可导回的台账"。没有标记也能导，
只是走通用解析（更宽松，仍需通过值类型校验）。
"""

import os
import re
from typing import Dict, List, Optional, Tuple

import openpyxl

MASTER_WORKBOOK_TAG = "MASTER_WORKBOOK_V1"
# 顶层多值分隔符。**不能**用顿号：真实表的班级名自带顿号
# （「2024级应用化学1、2班」「24级电子信息工程1、2班」，433 条里有 5 个），
# 用顿号导出再导回会把一个班切成「2024级应用化学1」+「2班」，实测 17→29 个班。
MULTI_SEP = ";"

# 顶层分隔符：出现它就只按它切，顿号退化为"名字内部字符"
_TOP_SEP = (";", "；", "|")
# 兼容分隔符：教秘手写时常用这些（无顶层分隔符时才启用）
_FALLBACK_SEP = ("、", "，", ",", "/")

# 统计列里常见的"数字后缀"，命中即判为统计列（配合值类型校验双保险）
_COUNT_SUFFIX = ("数", "格", "率", "%")


# --------------------------------------------------------------------------- 表规格
# required：缺了就没法认出实体本身，直接整表跳过并报清楚
# multi   ：该列是"多值"，按分隔符切分
TABLE_SPECS: List[dict] = [
    {
        "key": "experiments", "sheet": "实验项目",
        "fields": {
            "name": ("实验项目", "项目名称", "实验名称", "项目名", "实验"),
            "course": ("所属课程", "课程名称", "课程名", "课程"),
            "teachers": ("授课教师", "可授教师", "指导教师", "任课教师", "教师", "老师"),
            "rooms": ("使用场地", "可用场地", "候选场地", "实验室", "场地", "教室"),
            "classes": ("适用班级", "开课班级", "班级名称", "行政班级", "教学班", "班级"),
        },
        "multi": {"teachers", "rooms", "classes"}, "required": ("name",),
    },
    {
        "key": "teachers", "sheet": "教师",
        "fields": {
            "name": ("教师", "教师姓名", "老师", "指导教师", "任课教师"),
            "projects": ("可授项目", "授课项目", "实验项目", "项目"),
        },
        "multi": {"projects"}, "required": ("name",),
    },
    {
        "key": "rooms", "sheet": "场地",
        "fields": {
            "name": ("场地", "场地名称", "教室", "实验室", "房间"),
            "capacity": ("容量", "容纳人数", "可容纳", "工位"),
        },
        "multi": set(), "required": ("name",),
    },
    {
        "key": "classes", "sheet": "班级",
        "fields": {
            "name": ("班级", "班级名称", "行政班级", "教学班"),
            "groups": ("学生组", "组别", "分组", "组"),
            "weekday": ("上课星期", "星期", "星期几"),
            "weekdays": ("全部上课日", "上课日", "上课日期"),
            "time_slots": ("节次", "时段", "上课节次", "时间段"),
            "week_min": ("起始周", "开始周", "周次起", "起始周次"),
            "week_max": ("结束周", "截止周", "周次止", "结束周次"),
        },
        "multi": {"groups", "weekdays", "time_slots"}, "required": ("name",),
    },
    {
        "key": "courses", "sheet": "课程",
        "fields": {
            "name": ("课程", "课程名称", "课程名"),
            "experiments": ("实验项目", "包含项目", "项目"),
        },
        "multi": {"experiments"}, "required": ("name",),
    },
]


def _norm(cell) -> str:
    return str(cell or "").strip().replace(" ", "").replace("：", "").replace(":", "")


def _merge_orphans(parts: List[str]) -> List[str]:
    """把被顿号切碎的班级名拼回去：「2024级应用化学1」+「2班」→「2024级应用化学1、2班」。

    真实数据里班级名**自带顿号**（433 条表里有 5 个）。教秘手写多个班级时若用顿号
    分隔，纯切分就会拆出「2班」「3班」这种不存在的班 —— 于是导回一次，主数据里
    凭空多出十几个碎班级，后续排课把它们当成真班级去排。

    判据：残片是纯数字或「N班」，**且**它前一段的结尾也是数字。
    反过来，「1班、2班」这种合法列举（前段以「班」结尾）不会被误合并。
    """
    out: List[str] = []
    for p in parts:
        if out and re.fullmatch(r"\d{1,2}班?", p) and re.search(r"\d$", out[-1]):
            out[-1] = f"{out[-1]}、{p}"
        else:
            out.append(p)
    return out


def _split_multi(value) -> List[str]:
    """多值单元格 → 列表。空值返回 []，纯数值不当实体名。"""
    if value is None:
        return []
    s = str(value).strip()
    if not s or s in ("—", "-", "无"):
        return []
    seps = _TOP_SEP if any(c in s for c in _TOP_SEP) else _FALLBACK_SEP
    pattern = "[" + re.escape("".join(seps)) + "]"
    parts = [p.strip() for p in re.split(pattern, s) if p and p.strip()]
    return _merge_orphans(parts)


def _to_int(value) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _find_sheet(wb, spec: dict, used: set) -> Optional[str]:
    """定位 spec 对应的页：精确名优先，否则最短的包含名。

    `parse_workbook`（写库）与 `preview_workbook`（只读预览）共用 ——
    两处各写一份选页逻辑，就会出现"预览说这页没找到、导入却导进去了"
    的错版，而教秘没有任何手段发现它。
    """
    if spec["sheet"] in wb.sheetnames:
        return spec["sheet"]
    cands = [n for n in wb.sheetnames if n not in used and spec["sheet"] in str(n)]
    return sorted(cands, key=lambda n: len(str(n)))[0] if cands else None


def _row_to_rec(row: tuple, mapping: Dict[str, int], spec: dict) -> Tuple[Optional[dict], int]:
    """一行 → 字段 dict；名称为空返回 None（空行/续表）。

    多值切分、整数 coercion 都在这一处：**预览给教秘看的样本与导入真正
    写库的行，必须是同一段代码**（R-34 同族：校验器与被验对象必须同源，
    否则"预览说 12 行、导入进 9 行"这类错版无人能发现）。
    返回 (rec, bad)：bad = 这行里"填了却不是数字"的整数字段个数。
    """
    def cell(f):
        i = mapping.get(f)
        return row[i] if i is not None and i < len(row) else None

    name = str(cell("name") or "").strip()
    if not name:
        return None, 0
    rec: dict = {"name": name}
    bad = 0
    for f in spec["fields"]:
        if f == "name" or f not in mapping:
            continue
        v = cell(f)
        if f in spec["multi"]:
            rec[f] = _split_multi(v)
        elif f in ("capacity", "week_min", "week_max"):
            iv = _to_int(v)
            if v not in (None, "") and iv is None:
                bad += 1
            rec[f] = iv
        else:
            rec[f] = str(v).strip() if v is not None else ""
    return rec, bad


def _header_text(rows: List[tuple], header_row: int, col: int) -> str:
    return _norm(rows[header_row - 1][col]) if header_row and col < len(rows[header_row - 1]) else ""


# --------------------------------------------------------------------------- 导出
def build_workbook(md) -> "openpyxl.Workbook":
    """五张实体表 → 可编辑工作簿（行=实体，列=字段，多值用顿号连接）。"""
    from openpyxl.styles import Alignment, Font

    wb = openpyxl.Workbook()
    wb.remove(wb.active)

    def _sheet(title, headers, widths, rows):
        ws = wb.create_sheet(title)
        ws.append(headers)
        for c in ws[1]:
            c.font = Font(bold=True)
            c.alignment = Alignment(horizontal="center")
        for col, w in zip("ABCDEFGH", widths):
            ws.column_dimensions[col].width = w
        for r in rows:
            ws.append(r)
        return ws

    # 填写说明（首屏可见，同时带版本标记供导入端确认）
    ws = wb.create_sheet("填写说明")
    ws.column_dimensions["A"].width = 96
    for i, line in enumerate(_GUIDE_LINES, start=1):
        ws.cell(row=i, column=1, value=line)
        if i == 1:
            ws.cell(row=i, column=1).font = Font(bold=True)

    ov = md.overview()
    _sheet("课程", ["课程", "实验项目"], [24, 60],
           [[c["name"], MULTI_SEP.join(c["experiments"])] for c in ov["courses"]])
    _sheet("实验项目", ["实验项目", "所属课程", "授课教师", "使用场地", "适用班级"],
           [34, 18, 30, 26, 60],
           [[e["name"], e["course"], MULTI_SEP.join(e["teachers"]),
             MULTI_SEP.join(e["rooms"]), MULTI_SEP.join(e["classes"])]
            for e in ov["experiments"]])
    _sheet("教师", ["教师", "可授项目"], [14, 60],
           [[t["name"], MULTI_SEP.join(t["projects"])] for t in ov["teachers"]])
    _sheet("场地", ["场地", "容量"], [14, 10],
           [[r["name"], r["capacity"] if r["capacity"] is not None else ""]
            for r in ov["rooms"]])
    _sheet("班级", ["班级", "学生组", "上课星期", "全部上课日", "节次", "起始周", "结束周"],
           [30, 34, 12, 20, 16, 10, 10],
           [[c["name"], MULTI_SEP.join(c["groups"]), c["weekday"],
             MULTI_SEP.join(c.get("weekdays") or []),
             MULTI_SEP.join(c["time_slots"]),
             "" if c["week_min"] is None else c["week_min"],
             "" if c["week_max"] is None else c["week_max"]]
            for c in ov["classes"]])
    return wb


_GUIDE_LINES = [
    f"{MASTER_WORKBOOK_TAG} —— 物理实验排课 主数据台账（可编辑、可导回）",
    "",
    "怎么用：在下面五张表里直接改 → 保存 → 回到系统「主数据」页点「导入台账」。",
    "",
    "填写规则",
    "  1. 多个值用**分号**分隔，例如：授课教师 = 吴老师;林老师",
    "     （为什么不用顿号：班级名本身可能带顿号，如「2024级应用化学1、2班」）",
    "     只写顿号也能认，但一行里既有分号又有顿号时，只有分号当分隔符。",
    "  2. 「起始周 / 结束周 / 容量」填数字；留空表示不修改（不会清空已有值）",
    "  3. 第一行是表头，不要改列名；列的**顺序**可以随意调整",
    "  4. 想新增一行，直接在表末尾接着写即可",
    "",
    "⚠️ 导入只做「新增 / 补充」，不做删除：",
    "    在表里删掉一行，系统不会删掉对应实体（删请用界面上的删除按钮）。",
    "    同理，清空某个单元格 = 不修改该字段，而不是把它清空。",
    "",
    "本文件的口径：教师 / 场地 / 班级 / 实验项目均为**主数据实体**，",
    "不是课表行 —— 改这里不会动已排好的课表，只影响后续排课的可选资源。",
]


# --------------------------------------------------------------------------- 导入解析
def _match_columns(rows: List[tuple], spec: dict) -> Tuple[Optional[int], Dict[str, int]]:
    """在表头候选行里匹配字段 → 列下标。

    最长别名优先：避免短别名（"课程"）抢走长别名（"所属课程"）的列。
    """
    header_row, mapping = None, {}
    for r in range(1, min(6, len(rows) + 1)):
        cells = [_norm(c) for c in rows[r - 1]]
        best: Dict[int, Tuple[int, str]] = {}     # col -> (alias_len, field)
        for field, aliases in spec["fields"].items():
            for i, c in enumerate(cells):
                if not c:
                    continue
                for a in aliases:
                    if a == c or (len(a) >= 2 and (a in c or c in a)):
                        prev = best.get(i)
                        if prev is None or len(a) > prev[0]:
                            best[i] = (len(a), field)
                        break
        hit = {f: i for i, (_l, f) in best.items()}
        if len(hit) > len(mapping):
            header_row, mapping = r, hit
    return header_row, mapping


def _is_stat_column(rows: List[tuple], col: int, header_row: int) -> bool:
    """多值列的值几乎全是数字 → 这是统计列（如「班级数」），不能当实体名导入。"""
    vals = [r[col] for r in rows[header_row:] if col < len(r) and r[col] not in (None, "")]
    if len(vals) < 2:
        return False
    numeric = sum(1 for v in vals if isinstance(v, (int, float)) and not isinstance(v, bool))
    return numeric >= max(2, int(len(vals) * 0.8))


def parse_workbook(filepath: str) -> Tuple[Dict[str, List[dict]], dict]:
    """Excel → {表 key: 行 dict 列表} + 解析报告。

    报告里一定说清楚：认出了哪些表、每张表认出哪些列、**哪些列没认出来**、
    跳过了哪些行。宁可报"没认出来"，也不静默导入一半（R-34）。

    报告同时服务于**导入向导的只读预览**（`preview_workbook`）：每张表带
    `columns`（字段 ← 表头列名）、`unrecognized`（没被任何字段认出的列）、
    `stat_dropped`（判为统计列而忽略的列）、`samples`（前 3 行，与写库同一
    行转换代码）。预览与写库因此共用同一份解析，数字不可能对不上。
    """
    wb = openpyxl.load_workbook(filepath, data_only=True)
    tagged = any(MASTER_WORKBOOK_TAG in str(c[0] if c else "")
                 for c in wb["填写说明"].iter_rows(values_only=True)) \
        if "填写说明" in wb.sheetnames else False

    tables: Dict[str, List[dict]] = {s["key"]: [] for s in TABLE_SPECS}
    report: dict = {"tagged": tagged, "tables": {}, "warnings": [],
                    "ignored_sheets": [], "sheets": list(wb.sheetnames)}

    used_sheets = set()
    for spec in TABLE_SPECS:
        sheet_name = _find_sheet(wb, spec, used_sheets)
        if sheet_name is None:
            report["tables"][spec["key"]] = {"found": False, "rows": 0}
            continue
        used_sheets.add(sheet_name)
        sheet = wb[sheet_name]
        rows = list(sheet.iter_rows(values_only=True))

        header_row, mapping = _match_columns(rows, spec)
        if header_row is None or any(f not in mapping for f in spec["required"]):
            needed = "/".join(spec["fields"][spec["required"][0]][:3])
            report["warnings"].append(
                f"「{sheet_name}」页没认出「{spec['sheet']}」列名（需至少含{needed}），该页已跳过")
            report["tables"][spec["key"]] = {"found": True, "sheet": sheet_name,
                                             "rows": 0, "skipped": True}
            continue

        # 统计列剔除（双保险：列名带"数/格/率/%" 或 值几乎全是数字）
        # ⚠️ claimed 必须在剔除**之前**取：统计列是"认出来之后又主动忽略"的，
        # 剔除后再算未识别列，它就会同时出现在两个列表里（同一列报两次，
        # 教秘会以为有两处问题）。
        claimed = set(mapping.values())
        dropped = []
        for field, col in list(mapping.items()):
            if field not in spec["multi"]:
                continue
            header_text = _norm(rows[header_row - 1][col]) if col < len(rows[header_row - 1]) else ""
            if header_text.endswith(_COUNT_SUFFIX) or _is_stat_column(rows, col, header_row):
                dropped.append(header_text or field)
                mapping.pop(field)
        if dropped:
            report["warnings"].append(
                f"「{sheet_name}」页的 {'、'.join(dropped)} 是统计列（数值），已忽略 —— "
                f"它不是实体清单，导入不会把它当名字")

        recs, blank, bad = [], 0, 0
        for row in rows[header_row:]:
            rec, row_bad = _row_to_rec(row, mapping, spec)
            bad += row_bad
            if rec is None:
                if any(c not in (None, "") for c in row):
                    blank += 1
                continue
            recs.append(rec)

        tables[spec["key"]] = recs
        header_cells = [_norm(c) for c in rows[header_row - 1]]
        report["tables"][spec["key"]] = {
            "found": True, "sheet": sheet_name, "header_row": header_row,
            "rows": len(recs), "blank_rows": blank, "bad_values": bad,
            "recognized": sorted(mapping),
            "missing": sorted(f for f in spec["fields"] if f not in mapping),
            # ↓ 导入向导预览用：字段 ← 列名 / 没认出的列 / 统计列 / 样本行
            "columns": {f: _header_text(rows, header_row, col) for f, col in mapping.items()},
            "unrecognized": [c for i, c in enumerate(header_cells) if c and i not in claimed],
            "stat_dropped": dropped,
            "samples": recs[:3],
        }

    report["ignored_sheets"] = [n for n in wb.sheetnames if n not in used_sheets]
    return tables, report


def preview_workbook(filepath) -> dict:
    """只读预览：解析整份台账但**不写任何状态**（导入向导第 2 步）。

    就是 `parse_workbook` + 丢掉行数据：同一份选页/匹配/统计列/行转换
    代码，预览报的"将导入 N 行、没认出哪些列"与点确认后真正发生的导入
    逐字一致。单独写一套预览逻辑是 R-34 同族错误（校验器与被验对象
    不同源），本项目不这么干。
    """
    _tables, report = parse_workbook(filepath)
    return report


# --------------------------------------------------------------------------- 应用
def apply_tables(md, tables: Dict[str, List[dict]]) -> dict:
    """行 → MasterData（逐表 upsert，只增补不删除）。返回逐表计数。"""
    stats: Dict[str, dict] = {}
    before = {k: len(getattr(md, k)) for k in ("courses", "experiments", "teachers",
                                               "rooms", "classes")}

    stats["courses"] = _apply_courses(md, tables.get("courses") or [])
    stats["experiments"] = _apply_experiments(md, tables.get("experiments") or [])
    stats["teachers"] = _apply_teachers(md, tables.get("teachers") or [])
    stats["rooms"] = _apply_rooms(md, tables.get("rooms") or [])
    stats["classes"] = _apply_classes(md, tables.get("classes") or [])

    after = {k: len(getattr(md, k)) for k in before}
    for k in stats:
        stats[k]["before"] = before.get(k, 0)
        stats[k]["after"] = after.get(k, 0)
        stats[k]["added"] = max(0, after.get(k, 0) - before.get(k, 0))

    # ⚠️ 导入即"已初始化"：否则 `_ensure_master_data()` 看到 initialized=False
    # 会用安排表反推基线把刚导入的实体整个覆盖掉 —— 导入了个寂寞。
    if any(s.get("rows") for s in stats.values()):
        md.initialized = True
    return stats


def _apply_courses(md, rows) -> dict:
    n = 0
    for r in rows:
        md.upsert_course(r["name"])
        n += 1
    return {"rows": len(rows), "applied": n}


def _apply_experiments(md, rows) -> dict:
    n = 0
    for r in rows:
        md.upsert_experiment(
            r["name"], course=r.get("course") or "",
            teachers=r.get("teachers") or [], rooms=r.get("rooms") or [],
            classes=r.get("classes") or [])
        n += 1
    return {"rows": len(rows), "applied": n}


def _apply_teachers(md, rows) -> dict:
    n = 0
    for r in rows:
        md.upsert_teacher(r["name"], projects=r.get("projects") or [])
        n += 1
    return {"rows": len(rows), "applied": n}


def _apply_rooms(md, rows) -> dict:
    n = 0
    for r in rows:
        md.upsert_room(r["name"], capacity=r.get("capacity"))
        n += 1
    return {"rows": len(rows), "applied": n}


def _apply_classes(md, rows) -> dict:
    n = 0
    for r in rows:
        md.upsert_class(
            r["name"], groups=r.get("groups") or [], weekday=r.get("weekday") or "",
            time_slots=r.get("time_slots") or [],
            week_min=r.get("week_min"), week_max=r.get("week_max"),
            weekdays=r.get("weekdays") or None)
        n += 1
    return {"rows": len(rows), "applied": n}


def save_workbook(md, path: str) -> str:
    """导出到指定路径（测试/脚本用）；Web 端点走 `_xlsx_response`。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    wb = build_workbook(md)
    wb.save(path)
    return path
