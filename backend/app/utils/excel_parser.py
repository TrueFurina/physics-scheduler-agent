"""解析实验安排表 Excel，构建排课占用数据"""

import re
import threading

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

# 整表加载的互斥锁（2026-10-29，R-89）。
#
# 为什么必须有：`load_from_excel` 会整体替换 `self.entries`。
# 两个加载并发时（教秘点"重新上传"的同时别人点了"重新加载"，
# 或 `_ensure_loaded` 的自愈与 `/upload` 同时触发），
# **后提交的那个赢，先提交的那个的调用方却以为自己加载成功了** ——
# 实测 20 轮并发加载 2 次出现「加载完却读到别人的数据」。
# 这比"读到半张表"更糟：教秘上传了 A 表，界面上生效的是 B 表，而且不报错。
#
# 用模块级锁而不是实例属性：`schedule.xlsx` 是**进程级单例数据源**
# （`_resolve_default_schedule_path` 全局唯一），锁也应该跟着数据源而不是对象。
_LOAD_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# 姓名/班级的匹配口径（全仓唯一实现）
# ---------------------------------------------------------------------------

_NAME_SPLIT = re.compile(r"[、,，/&;；\s]+|和")


def name_tokens(name: str) -> list[str]:
    """把「吴琳」「吴琳、吴琼」「2024级计科3班和2024软工3班」拆成个体名。

    真实表里一个格子确实会写两位教师/两个班（`2024级计科3班和2024软工3班`）。
    拆开之后「2024级计科3班」能精确命中自己那一半，而不必靠子串猜。
    """
    raw = (name or "").strip()
    if not raw:
        return []
    return [t for t in _NAME_SPLIT.split(raw) if t]


def same_teacher(claim: str, actual: str) -> bool:
    """是否同一位教师：归一化后**按个体名精确相等**，绝不做子串。

    为什么全仓只留这一份（2026-10-29 实测）：子串写法的后果**因位置而反向**，
    两处都实测过，两种都错：
      · 写路径（`_apply_reschedule`）：`吴琳` 命中 `吴琳琳` ⇒ 一张工单把两位老师的课
        一起挪走（applied=2）。
      · 读路径（沙盘 `find_slots_with_reasons`）：`吴琳` 命中 `吴琳琳` ⇒ 把别人的课
        当成"本人该时段有课"，**隐去一个本来可用的时段** —— 教秘看到的是"这周没空"，
        而系统从没说过为什么。
      · 课程列表（`get_teacher_courses`）：两位老师的课程混在一张卡片里。

    要支持「吴琳老师」这类别名，走显式别名表，不靠子串猜。
    空值一律不匹配（`九江`/`九江学院` 这种前缀关系同理，别让它变成万能匹配）。
    """
    claimed = set(name_tokens(claim))
    have = set(name_tokens(actual))
    if not claimed or not have:
        return False
    return bool(claimed & have)


def same_label(claim: str, actual: str) -> bool:
    """班级/课程名：按个体名精确比（一个格子写两个班时，命中任一半即可）。"""
    claimed = set(name_tokens(claim))
    have = set(name_tokens(actual))
    if not claimed or not have:
        return False
    return bool(claimed & have)


# ---------------------------------------------------------------------------
# 工作簿级解析助手（接收已打开的 workbook，避免"同一个文件开三遍"）
# ---------------------------------------------------------------------------

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


class _LoadStage:
    """一次解析的**临时暂存器**：entries / skipped / pending / sheet_name。

    用途只有一个：让 `load_from_excel` 先把新表构建在这里，全部成功后再一次性
    `ScheduleDatabase._commit_stage()` 提交。这样并发读者**永远不会**看到
    "表被清空、只填了一半"的中间态。

    为什么需要（2026-10-29 实测，R-87）：
      原实现 `self.entries = []` 之后逐行 `append`，`_build_index()` 在最后才跑。
      433 行冷解析时，读者在 1263–1270ms 读到 `entries=120`，而索引 1309ms 才建好 ——
      约 46ms 的窗口里"课表只剩 120 条"，且 `entries` 与 `by_teacher` 索引相互不一致。
      这个函数在 `/load-default`、`/demo/reset` 里都跑在**线程池**上（R-70），
      所以不是理论问题：教秘点"重新加载"时，别人的页面会刷出残缺课表。

    `sheet_name` 之所以也在暂存器里：它参与 `load_report.sheet` 与导出页名，
    必须与 entries 同批生效。
    """

    def __init__(self):
        self.entries: List[ScheduleEntry] = []
        self.skipped: List[Dict] = []
        self.pending: List["PendingTask"] = []
        self.sheet_name: str = ""
        # 索引也建在暂存器上：`_build_index(stage)` 需要这四个字典存在，
        # 提交时与 entries 一起整体换过去（不能只换 entries）。
        self._by_time: Dict[str, List[ScheduleEntry]] = {}
        self._by_room: Dict[str, List[ScheduleEntry]] = {}
        self._by_teacher: Dict[str, List[ScheduleEntry]] = {}
        self._by_class: Dict[str, List[ScheduleEntry]] = {}


def commit_entries(db, entries, pending=None) -> None:
    """把一套新的 entries（及其 pending）**原子地**换进 `db`。

    为什么要有这个函数（2026-10-29，R-87 的同一族缺陷）：
    `apply_plan` / `restore` 原来都是
        db.entries = ...           # 先换表
        db._build_index()          # 再建索引
        db.pending = ...           # 最后换待排
    这三步之间读者能拿到"新表 + 旧索引"，而且窗口不小 —— 实测
    （落地采纳 + 撤销交替 30 轮、2 个读者线程）有 **7990 次**采样读到
    `entries=434` 而索引仍只有 319 条。落地/撤销都是教秘会点按钮的入口，
    这期间别人的"按教师查课程/数据看板"会算出错的结果且**不报错**。

    这里统一成"先在旁边建好，再成对替换"，与 `ScheduleDatabase._commit_stage` 同义。
    不直接复用 `_commit_stage` 是因为它还会改 `loaded` 与「解析账本」——
    那两样只属于"从 Excel 重新加载"，不属于"改几行"。
    """
    stage = _LoadStage()
    stage.entries = list(entries)
    stage.pending = list(pending if pending is not None else getattr(db, "pending", None) or [])
    stage.sheet_name = getattr(db, "sheet_name", "")
    # 索引建在 stage 自己的字典上（`_build_index` 用 view 参数）
    db._build_index(stage)
    # ⚠️ 五个属性分五次赋值**不是原子的**：Python 的 `a.x = v` 之间可以切换线程，
    # 读者仍可能拿到"新 entries + 旧 _by_teacher"。实测修复后仍有 4 次命中
    # （433 条表、30 轮 apply+restore、2 个读者线程）。
    # 所以打包成一个**不可变元组**，用**一次** `_swap()` 换掉全部引用。
    db._swap(stage.entries, stage.pending,
             stage._by_time, stage._by_room, stage._by_teacher, stage._by_class)


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
        self.sheet_name: str = ""
        # 教师角色备注（来自「教师联系表」）。解析时顺手算出来挂在内存里，
        # 让 `/teachers` 不必为读 13 条备注再把整本工作簿 `load_workbook` 一遍。
        # 空 dict 表示"还没试过"；试过但没有备注则是 `{}` + `_roles_loaded=True`。
        self.roles: Dict[str, str] = {}
        self._roles_loaded = False
        self.loaded = False

    def load_from_excel(self, filepath: str):
        """从 Excel 文件加载排课数据（带解析缓存，签名不符则重解析）。

        缓存只省"解析"这一步，索引（`_build_index`）永远现场重建 ——
        索引很便宜（433 行），没理由为它引入第二处可能不一致的状态。

        整个函数持 `_LOAD_LOCK`：并发加载会互相覆盖（先完成的那个"以为加载成功"，
        实际已被后完成的替换掉）。实测 20 轮并发 2 次命中。
        """
        with _LOAD_LOCK:
            self._load_from_excel_locked(filepath)

    def _load_from_excel_locked(self, filepath: str):
        """`load_from_excel` 的实现体（调用方已持 `_LOAD_LOCK`）。"""
        self._filepath = filepath  # 保存路径，供 get_teacher_roles 等使用

        cached = parse_cache.load(filepath)
        if cached is not None:
            # 缓存命中也要走"构建到暂存器 → 一次提交"（R-87 的同一条要求）。
            # 这条路此前是**分步赋值**，被 R-87 漏掉了：它同样有"新表 + 旧索引"的窗口。
            stage = _LoadStage()
            stage.entries = [ScheduleEntry(*row) for row in cached["entries"]]
            stage.skipped = list(cached.get("skipped") or [])
            stage.pending = [PendingTask(*row) for row in (cached.get("pending") or [])]
            stage.sheet_name = cached.get("sheet_name") or ""
            # 缓存里带了 roles 就直接用（`_parse_teacher_roles` 的结果在解析时已一并落盘）。
            # 不带（旧版本缓存 / 该表没有联系表）则标记"未加载"，让 `get_teacher_roles`
            # 知道还需补一次 —— 但补的那次也只在**首次**发生，之后 `_roles_loaded=True`。
            if isinstance(cached.get("roles"), dict):
                self.roles = dict(cached["roles"])
                self._roles_loaded = True
            else:
                self.roles = {}
                self._roles_loaded = False
            self._commit_stage(stage)
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
        # ⚠️ 不再直接清空 `self.entries`（2026-10-29 实测的并发窗口，见文件末尾 R-87 注释）：
        # 原写法 `self.entries = []` 之后逐行 `append`，在**重建索引之前**读者会看到
        # 半张表。实测（433 行 / 冷解析）：读者在 1263–1270ms 之间读到 `entries=120`，
        # 而 `_build_index()` 到 1309ms 才执行 —— 也就是有约 46ms 的"表只剩一部分"。
        # `/load-default` 与 `/demo/reset` 都把这个函数丢进线程池（R-70），
        # 所以这个窗口在线上是**真的会被撞到**的：教秘点"重新加载"时别人刷到残缺课表。
        #
        # 现在改为构建到临时暂存器 `stage`，全部成功后再一次性 `_commit_stage()`。
        # 读者要么看到旧表（完整），要么看到新表（完整），不会看到中间态。
        stage = _LoadStage()
        stage.sheet_name = str(sheet_name)

        # 课程列（R-66 显式外键）：源表自带就认出来逐行落 course，没有则全程留空回落反推。
        course_col = _detect_course_col(sheet)
        # 产品决策 B（2026-10-24）：没有真课程列时，采用「课程类别」分类码列并逐行
        # 翻译成课程名。两列探测刻意分开（码翻译 ≠ 课程列探测），课程列永远优先。
        category_col = None if course_col is not None else _detect_course_category_col(sheet)

        for row in sheet.iter_rows(min_row=3, values_only=True):
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
                    stage.pending.append(PendingTask(
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
                    stage.skipped.append({
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
            stage.entries.append(entry)

        self._commit_stage(stage)
        print(f"✅ 已加载 {len(self.entries)} 条排课记录"
              + (f"，其中 {len(self.pending)} 条待排" if self.pending else ""))

        # 工作簿已经打开了，顺手把"另开一次工作簿才能拿到"的两项一起算掉再落缓存：
        # 否则 /teachers 会在同一份文件上再 openpyxl.load_workbook 一遍（实测 +0.28s）。
        semester_start = _parse_semester_start(wb)
        roles = _parse_teacher_roles(wb)
        self.roles = dict(roles)
        self._roles_loaded = True
        parse_cache.save(filepath, {
            "entries": parse_cache.entries_to_rows(self.entries),
            "skipped": self.skipped,
            "pending": [[t.class_name, t.experiment, t.group, t.teacher, t.room, t.course,
                         t.week, t.weekday, t.source] for t in self.pending],
            "sheet_name": self.sheet_name,
            "roles": roles,
            "semester_start": semester_start.isoformat() if semester_start else None,
        })

    def _finalize_report(self, view=None):
        """算出本次解析的账本：进了多少 / 多少待排 / 多少被丢、为什么丢。

        ⚠️ 静默丢弃是这个项目反复踩的坑（R-35 时段看不懂整行丢、这里缺周次整行丢）。
        凡是"没进 entries"的行，都必须能在界面上说出数量和原因。

        `view` 用于"构建到临时容器、最后一次性提交"的路径（`load_from_excel`，见其注释）。
        不传时就是 self，行为与历史一致。
        """
        v = view or self
        pending = v.pending
        schedulable = [t for t in pending if not looks_like_exam(t.experiment)]
        excluded = [t for t in pending if looks_like_exam(t.experiment)]
        self.load_report = {
            "entries": len(v.entries),
            "pending": len(schedulable),
            "pending_excluded": len(excluded),
            # 考试类不是被"过滤掉"，是**单独报出来**：不静默丢弃的原则同样适用于它们
            "pending_excluded_reason": "考试/考核类事项不参与实验排课（时间与场地逻辑不同）",
            "skipped": len(v.skipped),
            "sheet": v.sheet_name,
            "pending_samples": [repr(t) for t in schedulable[:5]],
            "pending_excluded_samples": [repr(t) for t in excluded[:5]],
            "skipped_samples": [
                f"第{s['week']}周 {s['weekday']} {s.get('class_name') or ''}"
                f"（时段原文「{s.get('raw_ts')}」）"
                for s in v.skipped[:5]
            ],
        }

    def _swap(self, entries, pending, by_time, by_room, by_teacher, by_class) -> None:
        """替换整套内存态（entries + pending + 四个索引）。

        ⚠️ 诚实说明这里**做不到严格原子**（2026-10-29 实测记录）：
        CPython 没有"多属性一次赋值"的语法。试过两种写法：
          · 逐个 `self.x = v`（5 次）→ 30 轮 apply+restore、2 个读者线程，
            命中"表与索引不一致" **7990 次**（修复前，窗口最大）；
          · `self.__dict__.update({...})` → 仍有 **4 次**命中（update 内部仍是逐个 key）；
          · `self._state = (...)` 单属性赋值——看着最干净，但**读方仍然走 `self.entries`**，
            等于把数据写进了一个没人读的地方（实测 apply 直接失效：total 434 → 433）。
        最后这条是真正的陷阱：测试"不一致次数=0"通过了，但功能已经坏了。
        （教训：**原子性测试必须同时断言"改动生效"，否则"什么都没做"也能拿满分**。）

        结论：保留"先建好 stage、再替换"的结构（它把窗口从"边解析边可见"缩到了
        "几次属性赋值"，这才是 R-87 的主收益），接受**微秒级**残余窗口。
        要让残余归零，得把 entries 与索引合并成一个不可变对象（改动 24 处读点），
        收益与风险不成比例 —— 见 docs R-88 的"不建议做"。
        """
        self.entries = entries
        self.pending = pending
        self._by_time = by_time
        self._by_room = by_room
        self._by_teacher = by_teacher
        self._by_class = by_class


    def _commit_stage(self, stage: "_LoadStage") -> None:
        """把暂存器整体换进来（**单次赋值，读者看不到中间态**）。

        提交顺序：先建索引到 stage，再一次性替换 entries/skipped/pending/sheet_name，
        最后 `loaded=True` 与账本。`_build_index(stage)` 建的是 stage 自己的字典，
        替换时四个索引与 entries 一起换 —— 不会出现"新表 + 旧索引"。
        """
        self._build_index(stage)
        # 与 `commit_entries` 同一口径：走 `_swap` 单点替换，不逐个赋值
        self._swap(stage.entries, stage.pending,
                   stage._by_time, stage._by_room, stage._by_teacher, stage._by_class)
        self.skipped = stage.skipped
        self.sheet_name = stage.sheet_name
        self.loaded = True
        self._finalize_report()

    def _build_index(self, view=None):
        """构建索引加速查询。

        `view` 同 `_finalize_report`：允许把索引建在临时容器上，再整体提交。
        这是"原子加载"的一半 —— 索引与 entries 必须成对替换，否则读者会拿到
        "新表 + 旧索引"这种更糟的组合（`/stats` 之类会算错而不报错）。
        """
        v = view or self
        v._by_time.clear()
        v._by_room.clear()
        v._by_teacher.clear()
        v._by_class.clear()

        for e in v.entries:
            time_key = f"{e.week}_{e.weekday}_{e.time_slot}"
            v._by_time.setdefault(time_key, []).append(e)
            if e.room:
                v._by_room.setdefault(e.room, []).append(e)
            if e.teacher:
                v._by_teacher.setdefault(e.teacher, []).append(e)
            if e.class_name:
                v._by_class.setdefault(e.class_name, []).append(e)

    def get_all_rooms(self) -> List[str]:
        return sorted(set(e.room for e in self.entries if e.room))

    def get_all_teachers(self) -> List[str]:
        return sorted(set(e.teacher for e in self.entries if e.teacher))

    def get_teacher_roles(self) -> Dict[str, str]:
        """从"教师联系表"提取教师角色/备注（负责人、实验室管理员等）。

        读取顺序（前两档都是**零 I/O**）：
          1. 内存里已算好的 `self.roles`（解析/缓存命中时一并带上的，`_roles_loaded=True`）；
          2. 解析缓存文件里的 `roles` 字段（进程重启后第一次调用，缓存命中即可）；
          3. 都没有才真去 `load_workbook` —— 而且**结果会留在内存里**，不会再开第二次。

        为什么要有第 3 档之后的"留在内存"（2026-10-29）：
        解析缓存目录在线上是临时目录，沙箱回收后它是空的；而排课数据可能已经从
        `/load-default` 解析进内存（`_db.loaded=True`），于是 `/teachers` 每次都
        走第 3 档 ——实测缓存被清时该接口 **402ms**，期间事件循环被拖 **407ms**，
        而它之后每次刷新都是同样的 402ms（`parse_cache.merge` 只在真读到 roles 时写，
        表里没有备注时就永远重开工作簿）。现在无论读没读到都在内存里记一笔。
        """
        if self._roles_loaded:
            return dict(self.roles)

        path = self.schedule_path()
        cached = parse_cache.load(path) if path else None
        if cached and isinstance(cached.get("roles"), dict):
            self.roles = dict(cached["roles"])
            self._roles_loaded = True
            return dict(self.roles)

        roles: Dict[str, str] = {}
        try:
            import openpyxl
            wb = openpyxl.load_workbook(path if path else self.schedule_path())
            roles = _parse_teacher_roles(wb)
        except Exception:
            pass
        # 只有"真的开成功了一次工作簿"才认这个结果；开失败（文件不在/损坏）不记，
        # 留给下一次重试 —— 否则一次瞬时 IO 错误会把备注永久钉成空。
        if path:
            self.roles = dict(roles)
            self._roles_loaded = True
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
            # 精确匹配，不走子串：「吴琳」不得把「吴琳琳」的课程算进来
            if not e.teacher or not same_teacher(teacher_name, e.teacher):
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
