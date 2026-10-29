"""解析实验安排表 Excel，构建排课占用数据"""

import os

import openpyxl
from typing import List, Dict, Optional, Tuple

from app.utils import parse_cache
from app.utils.time_model import span_labels, primary_label, clock_text

# 时间段映射（节次标记 → 实际时间）
TIME_SLOTS = {
    "567": "13:30-15:45",
    "678": "15:45-18:00",
    "91011": "18:30-20:45",
    "78": "15:50-18:05",  # 仅历史缓存里可能残留，新解析一律归一为 678
}

# 反查：实际时间 → 标准节次标记（Excel 里时间格式不统一，需归一化）
#
# ⚠️ 这里的原则是"宁可合并，不可分裂"：所有落在大致同一时段的不同写法，
# 必须归到**同一个标签**上。冲突检查用的是标签相等（`e.week == w and
# e.time_slot == ts`），标签一分裂，两类记录就互相看不见 —— 教室已经被占，
# 沙盘却显示空闲，而且不报任何错。
#
# "78"/"15:50-18:05"（下午 7-8 节）与 "678"/"15:45-18:00"（下午 6-7-8 节）
# 真实时间重叠 2 小时 10 分，属于同一位老师排表时的两种写法。
# 原先它被单独留成 "78"，而沙盘/求解器/体检只枚举 567/678/91011 ——
# 真实表里 7 条这样写法的记录（第1/3/5/7/9/13/15 周 周四）因此**完全隐形**
# （实测：吴琼第7周周四 A502 有课，沙盘仍把该教室列为可用）。
TIME_SLOT_NORMALIZE = {
    "13:30-15:45": "567",
    "15:45-18:00": "678",
    "18:30-20:45": "91011",
    "15:50-18:05": "678",  # 原映射到 "78"，与 678 重叠却互不可见
    "18:00-21:00": "91011",
    "14:00-17:00": "567",  # ⚠️ 只用于决定"这行认得出来"；真实占用见 blocked
    "9:00-11:15": "MORNING",
    "9:00-12:00": "MORNING",
    "567": "567",
    "678": "678",
    "91011": "91011",
    "78": "678",  # 同上
}

# ⚠️ R-40：`14:00-17:00` 曾被上面这张表"吸附"成 567，真实结束时间 17:00 就丢了。
# 567 是 13:30-15:45，678 是 15:45-18:00 —— 这条记录**同时占住 567 和 678 的前 1 小时 15 分**。
# 只贴一个标签，该教室当天的 678 就成"空闲"，沙盘会把教秘推荐到正在上课的教室
# （实测第 12 周周四 A410：原表 14:00-17:00 有"设计性实验"，沙盘仍列 678 可用）。
# 所以解析时额外算 `blocked`（真实重叠的全部节次），下游一律按 blocked 判占用。


WEEKDAY_MAP = {
    "周一": "monday", "周二": "tuesday", "周三": "wednesday",
    "周四": "thursday", "周五": "friday", "周六": "saturday", "周日": "sunday",
}

# 上午时段不算实验冲突（实验课都排在下午/晚上），筛选时忽略
IGNORED_TIME_SLOTS = {"MORNING", ""}


# ---------------------------------------------------------------------------
# 工作簿级解析助手（接收已打开的 workbook，避免"同一个文件开三遍"）
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# 解析体积守卫
# ---------------------------------------------------------------------------
# 为什么需要（2026-10-29 实测）：
#   一个 271KB 的 xlsx（远低于上传接口 32MB 的上限）解压后是 5.7MB XML，
#   50,000 行；openpyxl 全量加载峰值内存 25.4MB（放大 96 倍），耗时 2.0 秒。
#   也就是说：**上传体积限制拦不住"小而密"的文件**，必须有行数上限。
#
# 两层防护：
#   1. MAX_SHEET_ROWS   —— 单表行数上限，超限直接拒绝解析（fail-fast）
#   2. read_only=True   —— 需要全表扫描时用只读流式模式，内存不随行数线性增长
#
# 默认 20,000 行：真实排课表通常在 100~500 行量级，留两个数量级余量。
# 可用环境变量 MAX_SHEET_ROWS 覆盖。

MAX_SHEET_ROWS = int(os.getenv("MAX_SHEET_ROWS", "20000"))


class SheetTooLargeError(ValueError):
    """工作表行数超过 MAX_SHEET_ROWS。

    携带 `limit` / `actual`，让 API 层能给出面向用户的文案并暴露限额头，
    而不必从异常字符串里正则抠数字。
    """

    def __init__(self, message: str, limit: int = 0, actual: int = 0):
        super().__init__(message)
        self.limit = limit
        self.actual = actual


def assert_sheet_size(sheet, what: str = "工作表") -> None:
    """解析前断言表规模在允许范围内。

    `sheet.max_row` 读的是 dimension 记录，不遍历全部单元格，代价可忽略。
    注意：篡改过的文件可能给出错误的 dimension，所以这只是第一道闸；
    真正的兜底是下面的 bounded_iter_rows()。
    """
    n = int(getattr(sheet, "max_row", 0) or 0)
    if n > MAX_SHEET_ROWS:
        # detail 里**不再**出现"设置环境变量"这类运维话术：
        # 教秘看到只会困惑，而且重试同一个文件永远不会成功。
        # API 层会补上"精简/拆分"这类可操作建议。
        raise SheetTooLargeError(
            f"{what}行数 {n} 超过上限 {MAX_SHEET_ROWS}",
            limit=MAX_SHEET_ROWS,
            actual=n,
        )


def bounded_iter_rows(sheet, *args, **kwargs):
    """iter_rows 的带边界版本：最多产出 MAX_SHEET_ROWS 行，超出即抛错。

    双重保险：即使 dimension 被伪造（assert_sheet_size 失效），
    这里也会在真正遍历到上限时中断，不会无限制跑下去。
    """
    count = 0
    for row in sheet.iter_rows(*args, **kwargs):
        count += 1
        if count > MAX_SHEET_ROWS:
            raise SheetTooLargeError(
                f"工作表行数超过上限 {MAX_SHEET_ROWS}（遍历时检测）",
                limit=MAX_SHEET_ROWS,
                actual=count,
            )
        yield row

def _parse_teacher_roles(wb) -> Dict[str, str]:
    """从"教师联系表"提取教师角色/备注（负责人、实验室管理员等）。"""
    roles: Dict[str, str] = {}
    try:
        if "教师联系表" in wb.sheetnames:
            ws = wb["教师联系表"]
            for row in ws.iter_rows(min_row=3, values_only=True):
                name, _, _, note = row[0], row[1], row[2], row[3]
                if name and note:
                    roles[str(name).strip()] = str(note).strip()
    except Exception:
        pass
    return roles


# 工作表候选名（按优先级）。
# 原先硬编码 `wb["大学物理实验安排表 (周) "]`（**还带一个尾随空格**），
# 换一所学校、或同一个文件另存时少个空格，就抛 KeyError → 400「文件解析失败：'大学物理实验安排表 (周) '」。
# 而 `docs/产品使用指南.md` 里写的是"上传 Excel 可替换（支持其他学校）"—— 那句话在当时是伪承诺。
SHEET_CANDIDATES = [
    "大学物理实验安排表 (周) ",
    "大学物理实验安排表 (周)",
    "大学物理实验安排表",
    "实验安排表",
    "安排表",
]


def find_schedule_sheet(wb):
    """容错定位安排表工作表：精确候选 → 名称含"安排表" → 名称含"周" → 第一个表。

    返回 (worksheet, 命中的名字)。找不到返回 (None, None)。
    """
    for name in SHEET_CANDIDATES:
        if name in wb.sheetnames:
            return wb[name], name
    for name in wb.sheetnames:
        if "安排表" in str(name):
            return wb[name], name
    for name in wb.sheetnames:
        if "周" in str(name):
            return wb[name], name
    if wb.sheetnames:
        return wb[wb.sheetnames[0]], wb.sheetnames[0]
    return None, None


# ---------------------------------------------------------------------------
# 教学大纲 / 主数据清单解析
#
# 为什么单独一套：排课的输入不该只有"上一次排完的课表行"。
# 导师的第 1–3 步（导入课程 → 新建实验项目 → 指定排课人）产出的是一份**清单**，
# 它天然没有周次/星期/时段。此前这些行会被主解析流程静默丢弃（excel_parser.py 里
# 的两个 `continue`），于是"排一个新增实验项目"在本系统里根本无从下手。
# ---------------------------------------------------------------------------

MASTER_SHEET_CANDIDATES = ["实验项目清单", "教学任务清单", "主数据", "实验项目", "课程清单"]

# 表头别名 → 字段（不同学校/不同人写的列名差很远，靠别名兜底，不靠列序）
MASTER_FIELDS = {
    "course": ("课程", "课程名称", "课程名", "所属课程"),
    "class_name": ("班级", "班级名称", "行政班级", "教学班", "班级/专业"),
    "experiment": ("实验项目", "项目名称", "实验名称", "项目", "实验"),
    "group": ("学生组", "组别", "分组", "组"),
    "teacher": ("教师", "授课教师", "指导教师", "任课教师", "老师"),
    "room": ("场地", "教室", "实验室", "候选场地", "上课地点"),
    "week": ("周次", "周", "上课周次"),
    "weekday": ("星期", "上课星期", "星期几"),
}


# 考试/考核类事项：真实表里有 3 行「大物实验B/C 考试」（有班级+项目，但没排时间）。
# 它们**不能被丢**（这正是本次要修的静默丢弃），但把"考试"当实验项目丢进求解器
# 是另一回事 —— 考试的时间与场地安排逻辑和实验课不同（真实表里它们连教师都没有）。
# 所以：照常记进 pending 并报出来，但不参与排课；这个判断是**显式且可见**的
# （load_report 里单列 pending_excluded），不是悄悄过滤。
EXAM_KEYWORDS = ("考试", "考核", "测验", "补考", "重修")


def looks_like_exam(name: str) -> bool:
    return any(k in (name or "") for k in EXAM_KEYWORDS)


def _norm_header(cell) -> str:
    return str(cell or "").strip().replace(" ", "").replace("：", "").replace(":", "")


def _detect_course_col(sheet) -> Optional[int]:
    """在安排表前两行表头里找「课程」列的索引；找不到返回 None。

    为什么需要（R-66 显式课程外键）：真实安排表是**固定列序**（周/星期/时段/班级/
    组/项目/场地/教师），没有课程列 —— 课程只能靠项目名查主数据反推，同名项目跨
    两门课时会歧义。若某校的安排表**自带**课程列，这里就把它认出来，逐行落进
    `ScheduleEntry.course`，成为权威归属（resolve_course 优先用它，不再反推）。

    ⚠️ **精确匹配别名，不做模糊子串**（与 parse_master_tasks 的宽松匹配刻意不同）：
    真实表第 9 列叫「课程类别」（值 C/B/普物3，是课程**分类码**而非课程名），
    模糊匹配 `课程 in 课程类别` 会把它误认成课程列 ⇒ 433 条全落密码值、锁定基线
    distinct_courses 从 1 变 3、导出课程列显示"C"。

    分类码列的采用是**独立的产品决策**（2026-10-24 用户拍板选 B，见
    docs/决策-课程类别归属-20261024.md）：走 `_detect_course_category_col` +
    `translate_course_code` 翻译，与这里的课程名列探测**刻意分开**（码翻译 ≠
    列探测，混在一起会把 R-66 的精确匹配教训搞糊）。真课程列永远优先。
    """
    aliases = MASTER_FIELDS["course"]
    for r in (1, 2):
        try:
            cells = next(sheet.iter_rows(min_row=r, max_row=r, values_only=True))
        except StopIteration:
            continue
        for i, c in enumerate(cells):
            if _norm_header(c) in aliases:
                return i
    return None


# ---------------------------------------------------------------------------
# 课程分类码 → 课程名（产品决策 B，2026-10-24 用户拍板）
#
# 真实安排表第 9 列「课程类别」携带 C/B/普物3 三门课的分班码。三轴分离证据
# （教师/班级/项目，见决策文档 §2.2）表明这是三门不同的课，不是一门课的两个
# 轨；学校自己的公文（「任课老师安排表」公共课程名称）与考试班名
# （「大物实验B/C班级」）给出映射值。映射待教秘终判 —— 若学校出带真「课程」
# 列的新表，本映射自然退场（_detect_course_col 优先，见上）。
# ---------------------------------------------------------------------------
COURSE_CODE_MAP = {
    "C": "大学物理实验C",
    "B": "大学物理实验B",
    "普物3": "普通物理实验3",
}

COURSE_CATEGORY_HEADER = "课程类别"


def _detect_course_category_col(sheet) -> Optional[int]:
    """在安排表前两行表头里找「课程类别」列（分类码列）的索引；找不到返回 None。

    与 `_detect_course_col`（课程名列）刻意分开：语义上这是"分类码翻译"不是
    "课程列探测"。精确等于表头别名，同样不做模糊子串。
    """
    for r in (1, 2):
        try:
            cells = next(sheet.iter_rows(min_row=r, max_row=r, values_only=True))
        except StopIteration:
            continue
        for i, c in enumerate(cells):
            if _norm_header(c) == COURSE_CATEGORY_HEADER:
                return i
    return None


def translate_course_code(raw) -> str:
    """分类码 → 课程名；未知码**原样返回**（不静默吞成默认课）。

    原样返回让异常值在 data-audit / 导出里可见（教秘能看见表里出现了没听说过的
    码），吞成默认课就是把问题藏起来（R-35「时段看不懂就静默丢弃」同族）。
    """
    code = str(raw or "").strip()
    if not code:
        return ""
    return COURSE_CODE_MAP.get(code, code)


def parse_master_tasks(filepath: str):
    """解析教学大纲/主数据清单 → (PendingTask 列表, 解析报告)。

    表头按别名匹配，匹配不到才退回列序；找不到就报清楚"缺哪一列"，
    而不是像主流程那样静默丢行。

    报告里的 `columns` / `unrecognized_columns` / `samples` 同时服务
    只读预览（`preview_master_tasks`，导入向导第 2 步）—— 预览与写库
    共用这一份匹配代码，"预览说 30 条、导入进 28 条"不可能发生。
    """
    wb = openpyxl.load_workbook(filepath)
    sheet, sheet_name = None, None
    for name in MASTER_SHEET_CANDIDATES:
        if name in wb.sheetnames:
            sheet, sheet_name = wb[name], name
            break
    if sheet is None:
        for name in wb.sheetnames:
            if any(k in str(name) for k in ("清单", "项目", "主数据")):
                sheet, sheet_name = wb[name], name
                break
    if sheet is None:
        sheet, sheet_name = (wb[wb.sheetnames[0]], wb.sheetnames[0]) if wb.sheetnames else (None, None)
    if sheet is None:
        raise ValueError("文件里没有任何工作表")
    assert_sheet_size(sheet, f"主数据「{sheet_name}」")

    # 找表头行：前 5 行里命中别名最多的一行
    header_row, mapping, header_cells = None, {}, []
    for r in range(1, min(6, sheet.max_row + 1)):
        cells = [_norm_header(c) for c in next(sheet.iter_rows(min_row=r, max_row=r, values_only=True))]
        hit = {}
        for field, aliases in MASTER_FIELDS.items():
            for i, c in enumerate(cells):
                if c and any(a in c or c in a for a in aliases):
                    hit[field] = i
                    break
        if len(hit) > len(mapping):
            header_row, mapping, header_cells = r, hit, cells
    if header_row is None or "class_name" not in mapping or "experiment" not in mapping:
        raise ValueError(
            "清单里必须至少能认出「班级」和「实验项目」两列"
            f"（当前识别到的列：{sorted(set(mapping)) or '无'}）"
        )

    # cell 定义在循环外、显式收 row 参数：不在循环内闭包循环变量
    # （B023 延迟绑定模式——虽然原写法是同步调用不会真踩坑，但这个模式
    # 一旦被后人改成延迟调用就是隐形 bug，趁 lint 门固化的机会修掉）
    def cell(row, field):
        i = mapping.get(field)
        return row[i] if i is not None and i < len(row) else None

    tasks, blanks = [], 0
    for row in sheet.iter_rows(min_row=header_row + 1, values_only=True):
        cls = str(cell(row, "class_name") or "").strip()
        exp = str(cell(row, "experiment") or "").strip()
        if not cls and not exp:
            blanks += 1
            continue
        if not cls or not exp:
            blanks += 1
            continue
        week = cell(row, "week")
        try:
            week = int(week) if week not in (None, "") else None
        except (TypeError, ValueError):
            week = None
        tasks.append(PendingTask(
            class_name=cls,
            experiment=exp,
            group=str(cell(row, "group") or "1组").strip(),
            teacher=str(cell(row, "teacher") or "").strip(),
            room=str(cell(row, "room") or "").strip(),
            course=str(cell(row, "course") or "").strip(),
            week=week,
            weekday=str(cell(row, "weekday") or "").strip(),
            source="master",
        ))
    claimed = set(mapping.values())
    report = {
        "sheet": str(sheet_name),
        "header_row": header_row,
        "recognized_columns": sorted(set(mapping)),
        "tasks": len(tasks),
        "blank_or_incomplete_rows": blanks,
        # ↓ 导入向导预览用：字段 ← 列名 / 没被认出的列 / 结构化样本行。
        # 与写库（/upload-master）共用同一份匹配代码，预览数字即导入数字。
        "columns": {f: (header_cells[i] if i < len(header_cells) else "")
                    for f, i in mapping.items()},
        "unrecognized_columns": [c for i, c in enumerate(header_cells)
                                 if c and i not in claimed],
        "samples": [{"class_name": t.class_name, "experiment": t.experiment,
                     "group": t.group, "teacher": t.teacher, "room": t.room,
                     "course": t.course, "week": t.week, "weekday": t.weekday}
                    for t in tasks[:5]],
    }
    return tasks, report


def preview_master_tasks(filepath) -> dict:
    """只读预览教学任务清单：**不写任何状态**（导入向导第 2 步，清单版）。

    就是 `parse_master_tasks` + 丢掉任务对象：`_master_tasks` / `db.pending`
    都不碰。认不出「班级/实验项目」两列时抛 ValueError（由端点转 400），
    与 `/upload-master` 同一条错误消息 —— 预览阶段就能看见，不用等导入。
    """
    _tasks, report = parse_master_tasks(filepath)
    return report


def _parse_semester_start(wb):
    """从安排表解析真实学期起始日（"第1周2月17日-2月23日"）。失败返回 None。"""
    import re
    from datetime import date
    try:
        ws, _ = find_schedule_sheet(wb)
        if ws is None:
            return None
        for row in ws.iter_rows(min_row=2, max_row=3, values_only=True):
            for cell in row:
                if cell and "第1周" in str(cell):
                    m = re.search(r"第1周(\d{1,2})月(\d{1,2})日", str(cell))
                    if m:
                        month, day = int(m.group(1)), int(m.group(2))
                        # 学年：24-25学年第二学期（春季），起始年在2月→2025
                        return date(2025, month, day)
    except Exception:
        pass
    return None


class ScheduleEntry:
    """一条排课记录

    `time_slot` 是**主标签**（展示/统计用，如 `567`）；
    `blocked`   是这条记录**真实占住**的全部节次（如 `["567","678"]`）。
    判占用必须用 `blocked`：跨节次的写法若只看主标签，另一个节次就漏了。
    """
    def __init__(self, week: int, weekday: str, time_slot: str, class_name: str,
                 group: str, experiment: str, room: str, teacher: str,
                 time_raw: str = "", blocked: Optional[List[str]] = None,
                 course: str = ""):
        self.week = week
        self.weekday = weekday
        self.time_slot = time_slot
        self.class_name = class_name
        self.group = group
        self.experiment = experiment
        self.room = room
        self.teacher = teacher
        # 原表里的时段写法。跨节次时展示要用它（`14:00-17:00`），
        # 否则教秘看到自己的课被写成 13:30-15:45，会以为系统读错了。
        self.time_raw = time_raw or ""
        self.blocked = list(blocked) if blocked else [time_slot]
        # 显式课程外键（R-66）：源安排表**有**课程列时直接落在这里，权威、无歧义；
        # 没有时为空串，由 course_bridge.resolve_course 回落主数据 experiment.course 反推。
        # 为什么不在解析时就用主数据物化：主数据可被 CRUD 改，物化会陈旧；
        # 只有"源表自带"的课程才配当条目的显式属性。
        self.course = course or ""

    def blocks(self, slot: str) -> bool:
        """这条记录是否占用 `slot` 这个节次。"""
        return slot in self.blocked

    def shares_slot_with(self, other: "ScheduleEntry") -> bool:
        """两条记录是否落在同一节次（真实重叠）。"""
        return not set(self.blocked).isdisjoint(set(other.blocked))

    def display_time(self) -> str:
        """给人看的时间：跨节次的原样显示原表写法，其余用标准时间。"""
        if len(self.blocked) > 1 and self.time_raw:
            return clock_text(self.time_raw)
        return clock_text(self.time_slot)

    def key(self) -> str:
        """唯一标识一次排课占用的资源"""
        return f"{self.week}_{self.weekday}_{self.time_slot}_{self.room}"

    def __repr__(self):
        return f"[W{self.week} {self.weekday} {self.time_slot}] {self.class_name}/{self.group} @ {self.room}"


class PendingTask:
    """一条**还没排时间**的教学任务（来自教学大纲/项目清单，而不是上一次排课结果）。

    为什么需要它
    ------------
    两个求解器（并行排课 / 一键自动排课）的教学任务清单，此前**只可能**来自
    `db.entries` —— 也就是上一次排课的行。于是系统只能"重排去年"，无法"从零排课"：
    教秘想排一个大纲里新增的实验项目，必须先伪造一行带周次和星期的记录塞进去。
    导师座谈里明确否定了"往年数据瞬移"，理由（培养方案/专业方向/实验顺序年年变）
    同样否定了"以去年课表行当输入"。

    PendingTask 让"课程 → 实验项目 → 学生组"这套主数据成为独立输入：
    没排过时间的行不再被丢弃，而是直接进求解器当待排单元。
    """

    def __init__(self, class_name: str, experiment: str, group: str,
                 teacher: str = "", room: str = "", course: str = "",
                 week: Optional[int] = None, weekday: str = "", source: str = "schedule"):
        self.course = course or ""
        self.class_name = class_name or ""
        self.experiment = experiment or ""
        self.group = group or ""
        self.teacher = (teacher or "").strip()
        self.room = (room or "").strip()
        self.week = week
        self.weekday = (weekday or "").strip()
        self.source = source          # 'schedule'（表里没排时间的行）/ 'master'（主数据清单）

    def unit_key(self) -> tuple:
        return (self.class_name, self.experiment, self.group)

    def __repr__(self):
        return f"⟨待排⟩ {self.class_name}/{self.group} {self.experiment}（{self.teacher or '未指派教师'}）"


class ScheduleDatabase:
    """排课数据内存数据库"""

    def __init__(self):
        self.entries: List[ScheduleEntry] = []
        # 索引：week + weekday + time_slot → 该时段所有排课
        self._by_time: Dict[str, List[ScheduleEntry]] = {}
        # 索引：room → 该房间的所有排课
        self._by_room: Dict[str, List[ScheduleEntry]] = {}
        # 索引：teacher → 该教师的所有排课
        self._by_teacher: Dict[str, List[ScheduleEntry]] = {}
        # 索引：class_name → 该班级的所有排课
        self._by_class: Dict[str, List[ScheduleEntry]] = {}
        # 时段无法识别而被剔除的行（原样保留，用于"整日按占用处理"和体检提示）
        self.skipped: List[Dict] = []
        # 待排任务：表里写了"课程/班级/实验项目/组"但**没排时间**的行。
        # ⚠️ 2026-10-22 之前这些行是静默 `continue` 掉的（不计数、不报告）——
        # 教秘拿今年的教学大纲清单来上传，会看到"成功加载 0 条"而不知为什么。
        # 现在它们单独成列，并可直接进入并行排课（这是"从零排课"的最小闭环）。
        self.pending: List["PendingTask"] = []
        # 本次加载的解析账本：进了多少条、多少条待排、多少条被丢、为什么丢
        self.load_report: Dict = {}
        self._short_rows: int = 0
        self.sheet_name: str = ""
        self.loaded = False

    def load_from_excel(self, filepath: str):
        """从 Excel 文件加载排课数据（带解析缓存，签名不符则重解析）。

        缓存只省"解析"这一步，索引（`_build_index`）永远现场重建 ——
        索引很便宜（433 行），没理由为它引入第二处可能不一致的状态。
        """
        self._filepath = filepath  # 保存路径，供 get_teacher_roles 等使用

        cached = parse_cache.load(filepath)
        if cached is not None:
            self.entries = [ScheduleEntry(*row) for row in cached["entries"]]
            self.skipped = list(cached.get("skipped") or [])
            self.pending = [PendingTask(*row) for row in (cached.get("pending") or [])]
            self.sheet_name = cached.get("sheet_name") or ""
            self._build_index()
            self._finalize_report()
            self.loaded = True
            print(f"✅ 已加载 {len(self.entries)} 条排课记录（解析缓存命中）"
                  + (f"，其中 {len(self.pending)} 条待排" if self.pending else ""))
            return

        wb = openpyxl.load_workbook(filepath)

        # 容错定位安排表：换学校/另存时表名对不上不该直接 400（见 find_schedule_sheet）
        sheet, sheet_name = find_schedule_sheet(wb)
        if sheet is None:
            raise ValueError(
                f"文件里找不到可用的工作表（现有：{'、'.join(map(str, wb.sheetnames)) or '无'}）；"
                "请把安排表放在名为「实验安排表」的工作表里"
            )
        # 解析前先卡规模：271KB 的恶意 xlsx 可解压成 5.7MB / 5 万行，
        # 全量解析峰值内存 25MB（放大 96 倍）。上传体积限制拦不住它。
        assert_sheet_size(sheet, f"安排表「{sheet_name}」")
        self.sheet_name = str(sheet_name)
        self.entries = []
        self.skipped = []
        self.pending = []

        # 课程列（R-66 显式外键）：源表自带就认出来逐行落 course，没有则全程留空回落反推。
        course_col = _detect_course_col(sheet)
        # 产品决策 B（2026-10-24）：没有真课程列时，采用「课程类别」分类码列并逐行
        # 翻译成课程名。两列探测刻意分开（码翻译 ≠ 课程列探测），课程列永远优先。
        category_col = None if course_col is not None else _detect_course_category_col(sheet)

        self._short_rows = 0
        for row in bounded_iter_rows(sheet, min_row=3, values_only=True):
            # 短行保护：合并单元格的表头会让后续数据行的 tuple 变短
            # （实测：A1:D1 合并后，数据行只剩 4 列 → IndexError:
            #   tuple index out of range → 被上层包成
            #   "文件解析失败：tuple index out of range"，把 Python 内部错误泄漏给教秘）。
            # 这里补齐到 8 列，交给下面的既有逻辑判空并计入 skipped。
            if len(row) < 8:
                self._short_rows += 1
                row = tuple(row) + (None,) * (8 - len(row))
            week, weekday, time_slot, class_name, group, experiment, room, teacher = \
                row[0], row[1], row[2], row[3], row[4], row[5], row[6], row[7]

            course_str = ""
            if course_col is not None and len(row) > course_col and row[course_col]:
                course_str = str(row[course_col]).strip()
            elif category_col is not None and len(row) > category_col and row[category_col]:
                course_str = translate_course_code(row[category_col])

            class_str = str(class_name).strip() if class_name else ""
            exp_str = str(experiment).strip() if experiment else ""
            group_str = str(group).strip() if group else ""
            room_str = str(room).strip() if room else ""
            teacher_str = str(teacher).strip() if teacher else ""

            # ⚠️ 2026-10-22 之前这里是两个静默 `continue`：没有周次/星期的行直接蒸发，
            # 不计数、不报告。教秘拿今年教学大纲的清单来上传，只会看到"成功加载 0 条"。
            # 现在：写了"班级 + 实验项目"却没排时间的行 ⇒ 记成**待排任务**（可直接进求解器）；
            # 既没班级也没项目的行 ⇒ 记为空白行，照样计数。
            if (not week or not isinstance(week, (int, float))) or not weekday:
                if class_str and exp_str:
                    self.pending.append(PendingTask(
                        class_name=class_str, experiment=exp_str, group=group_str,
                        teacher=teacher_str, room=room_str, course=course_str,
                        source="schedule",
                    ))
                continue

            # 清理教室字段
            room_str = str(room).strip() if room else ""
            teacher_str = str(teacher).strip() if teacher else ""
            time_raw = str(time_slot).strip() if time_slot else ""
            # 时段识别分两路（R-40）：
            #   ① 能解析成时间区间 —— 包括跨节次的 `14:00-17:00`。主标签取起始时刻
            #      所在的节次，`blocked` 取**所有真实重叠**的节次（可能两个）。
            #   ② 节次码 / 已知别名 —— 查表得主标签，`blocked` 只有它自己。
            blocked = span_labels(time_raw)
            if blocked:
                time_str = primary_label(time_raw, blocked[0])
            else:
                time_str = TIME_SLOT_NORMALIZE.get(time_raw, "")
                blocked = [time_str] if time_str else []

            if not time_str:
                # 时段看不懂就整行丢掉 —— 但**不能悄悄丢**。
                # 丢掉的是"这间教室这个人在这一天有课"这一事实，而沙盘照样会把
                # 这间教室推荐成空闲（实测：第3周周五 A504，翁铭华本人在上"扭摆法"，
                # 沙盘却把"A504 周五567"列为他自己的可用时段，因为是他就更荒唐）。
                # 所以原样记进 self.skipped，下游按"该教室/该教师当天整日占用"处理，
                # 并在数据体检里单列出来，让教秘回原表把这格改对。
                if room_str or teacher_str:
                    self.skipped.append({
                        "week": int(week),
                        "weekday": str(weekday).strip(),
                        "raw_ts": time_raw,
                        "class_name": str(class_name).strip(),
                        "experiment": str(experiment).strip() if experiment else "",
                        "room": room_str,
                        "teacher": teacher_str,
                    })
                continue  # 无效或无法识别的时间段

            entry = ScheduleEntry(
                week=int(week),
                weekday=weekday.strip(),
                time_slot=time_str,
                class_name=str(class_name).strip(),
                group=str(group).strip() if group else "",
                experiment=str(experiment).strip() if experiment else "",
                room=room_str,
                teacher=teacher_str,
                time_raw=time_raw,
                blocked=blocked,
                course=course_str,
            )
            self.entries.append(entry)

        self._build_index()
        self._finalize_report()
        self.loaded = True
        print(f"✅ 已加载 {len(self.entries)} 条排课记录"
              + (f"，其中 {len(self.pending)} 条待排" if self.pending else ""))

        # 工作簿已经打开了，顺手把"另开一次工作簿才能拿到"的两项一起算掉再落缓存：
        # 否则 /teachers 会在同一份文件上再 openpyxl.load_workbook 一遍（实测 +0.28s）。
        semester_start = _parse_semester_start(wb)
        parse_cache.save(filepath, {
            "entries": parse_cache.entries_to_rows(self.entries),
            "skipped": self.skipped,
            "pending": [[t.class_name, t.experiment, t.group, t.teacher, t.room, t.course,
                         t.week, t.weekday, t.source] for t in self.pending],
            "sheet_name": self.sheet_name,
            "roles": _parse_teacher_roles(wb),
            "semester_start": semester_start.isoformat() if semester_start else None,
        })

    def _finalize_report(self):
        """算出本次解析的账本：进了多少 / 多少待排 / 多少被丢、为什么丢。

        ⚠️ 静默丢弃是这个项目反复踩的坑（R-35 时段看不懂整行丢、这里缺周次整行丢）。
        凡是"没进 entries"的行，都必须能在界面上说出数量和原因。
        """
        schedulable = [t for t in self.pending if not looks_like_exam(t.experiment)]
        excluded = [t for t in self.pending if looks_like_exam(t.experiment)]
        self.load_report = {
            "entries": len(self.entries),
            "pending": len(schedulable),
            "pending_excluded": len(excluded),
            # 考试类不是被"过滤掉"，是**单独报出来**：不静默丢弃的原则同样适用于它们
            "pending_excluded_reason": "考试/考核类事项不参与实验排课（时间与场地逻辑不同）",
            "skipped": len(self.skipped),
            # 短行（列数不足 8）已被补齐为 None 继续处理，这里只报数量：
            # 合并单元格的表头常导致后续行变短，教秘应当知道自己的文件"结构不规整"。
            "short_rows": getattr(self, "_short_rows", 0),
            "sheet": self.sheet_name,
            "pending_samples": [repr(t) for t in schedulable[:5]],
            "pending_excluded_samples": [repr(t) for t in excluded[:5]],
            "skipped_samples": [
                f"第{s['week']}周 {s['weekday']} {s.get('class_name') or ''}"
                f"（时段原文「{s.get('raw_ts')}」）"
                for s in self.skipped[:5]
            ],
        }

    def _build_index(self):
        """构建索引加速查询"""
        self._by_time.clear()
        self._by_room.clear()
        self._by_teacher.clear()
        self._by_class.clear()

        for e in self.entries:
            time_key = f"{e.week}_{e.weekday}_{e.time_slot}"
            self._by_time.setdefault(time_key, []).append(e)
            if e.room:
                self._by_room.setdefault(e.room, []).append(e)
            if e.teacher:
                self._by_teacher.setdefault(e.teacher, []).append(e)
            if e.class_name:
                self._by_class.setdefault(e.class_name, []).append(e)

    def get_occupied_slots(self, week: int) -> List[Dict]:
        """获取某周所有已占用的时段"""
        occupied = []
        for e in self.entries:
            if e.week == week:
                occupied.append({
                    "week": e.week,
                    "weekday": e.weekday,
                    "time_slot": e.time_slot,
                    "room": e.room,
                    "class_name": e.class_name,
                    "teacher": e.teacher,
                    "experiment": e.experiment,
                })
        return occupied

    def find_free_slots(self, week: int, weekdays: List[str],
                        time_slots: List[str], rooms: List[str]) -> List[Dict]:
        """找到空闲时间段（按 blocked 判占用，跨节次写法两个节次都算忙）"""
        occupied = set()
        for e in self.entries:
            if e.week == week:
                for slot in e.blocked:
                    occupied.add(f"{e.weekday}_{slot}_{e.room}")

        free = []
        for wd in weekdays:
            for ts in time_slots:
                for rm in rooms:
                    if f"{wd}_{ts}_{rm}" not in occupied:
                        free.append({
                            "weekday": wd,
                            "time_slot": ts,
                            "room": rm,
                        })
        return free

    def get_all_rooms(self) -> List[str]:
        return sorted(set(e.room for e in self.entries if e.room))

    def get_all_teachers(self) -> List[str]:
        return sorted(set(e.teacher for e in self.entries if e.teacher))

    def get_teacher_roles(self) -> Dict[str, str]:
        """从"教师联系表"提取教师角色/备注（负责人、实验室管理员等）。

        优先用解析缓存里已有的结果：这个函数每次页面加载都会被调一次，
        而为了读十几个备注去 `load_workbook` 整个 400+ 行工作簿纯属浪费
        （实测 /teachers 因此稳定在 0.28s）。
        """
        path = self.schedule_path()
        cached = parse_cache.load(path) if path else None
        if cached and isinstance(cached.get("roles"), dict):
            return dict(cached["roles"])

        roles: Dict[str, str] = {}
        try:
            import openpyxl
            wb = openpyxl.load_workbook(path if path else self.schedule_path())
            roles = _parse_teacher_roles(wb)
        except Exception:
            pass
        if roles and path:
            parse_cache.merge(path, {"roles": roles})
        return roles

    def schedule_path(self) -> str:
        """返回数据源文件路径（兼容）"""
        return getattr(self, "_filepath", "")

    def get_semester_start_date(self):
        """从安排表解析真实学期起始日（"第1周2月17日-2月23日"）。

        同样优先吃缓存：这行日期在加载时就已经一起解析过了。
        """
        import openpyxl
        from datetime import date

        path = self.schedule_path()
        cached = parse_cache.load(path) if path else None
        if cached is not None and "semester_start" in cached:
            raw = cached["semester_start"]
            if not raw:
                return None
            try:
                return date.fromisoformat(raw)
            except Exception:
                pass  # 缓存里的日期坏了 → 老老实实重新解析

        try:
            wb = openpyxl.load_workbook(path)
        except Exception:
            return None

        parsed = _parse_semester_start(wb)
        if parse_cache.load(path) is not None:
            parse_cache.merge(path, {
                "semester_start": parsed.isoformat() if parsed else None,
            })
        return parsed

    def get_teacher_courses(self, teacher_name: str) -> List[Dict]:
        """
        按教师提取课程卡片（F2）
        将同一教师 + 同一课程名 + 同一班级的排课记录合并为一张课程卡片
        """
        # 时间段友好显示映射
        TIME_DISPLAY = {
            "567": "13:30-15:45", "678": "15:45-18:00",
            "91011": "18:30-20:45", "78": "15:50-18:05",
            "34": "10:00-11:40", "MORNING": "上午",
        }

        # 清理脏实验名（绪论课等提示性文本）
        def clean_experiment(name: str) -> str:
            if not name:
                return "未知实验"
            if "绪论" in name:
                return "绪论课"
            if "教室已安排" in name or "请查系统" in name:
                return "绪论课"
            return name

        # 按 (课程名, 班级) 分组
        groups: Dict[Tuple[str, str], Dict] = {}
        for e in self.entries:
            if not e.teacher or teacher_name not in e.teacher:
                continue
            key = (clean_experiment(e.experiment), e.class_name or "未知班级")
            if key not in groups:
                groups[key] = {
                    "course_name": key[0],
                    "class_name": key[1],
                    "slots": [],  # 所有上课时段
                    "weeks": set(),
                }
            groups[key]["slots"].append({
                "week": e.week,
                "weekday": e.weekday,
                "time_slot": e.time_slot,
                "room": e.room,
                # 跨节次写法（14:00-17:00）原样回显：教秘记得自己写的是几点，
                # 给他看"13:30-15:45"会以为系统读错了行。
                "time_text": e.display_time() if len(e.blocked) > 1 else "",
            })
            groups[key]["weeks"].add(e.week)

        # 转输出结构
        courses = []
        for info in groups.values():
            slots = info["slots"]
            # 排序取最早一周作为"原上课时间"代表
            slots_sorted = sorted(slots, key=lambda s: (s["week"], s["weekday"], s["time_slot"]))
            first = slots_sorted[0]
            weeks_sorted = sorted(info["weeks"])
            # 时间友好显示：周四 567 → 周四 13:30-15:45；
            # 跨节次记录按原表写法显示（如 14:00-17:00），见 slots[].time_text
            time_display = first.get("time_text") or TIME_DISPLAY.get(first["time_slot"], first["time_slot"])
            courses.append({
                "course_name": info["course_name"],
                "class_name": info["class_name"],
                "original_time": f"{first['weekday']} {time_display}",
                "room": first["room"],
                "week_range": f"{weeks_sorted[0]}-{weeks_sorted[-1]}周",
                "total_slots": len(slots),
                # 可调状态：实验轮次固定课程默认可调（由前端/后续规则细化）
                "adjustable": True,
                "adjustable_reason": "",
            })
        return courses

    def get_all_classes(self) -> List[str]:
        return sorted(set(e.class_name for e in self.entries if e.class_name))

    def get_week_range(self) -> tuple:
        if not self.entries:
            return (0, 0)
        weeks = [e.week for e in self.entries]
        return (min(weeks), max(weeks))
