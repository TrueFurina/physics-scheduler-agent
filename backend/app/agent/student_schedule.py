"""学生课表服务 - 多学生课表管理，提供忙碌时段查询（并集）"""

import re
from typing import Dict, Set, List, Optional, Tuple
from app.utils.student_data import STUDENT_NAME, STUDENT_ID, SEMESTER, COURSES


class StudentSchedule:
    """学生课表（多学生支持，P1-2）

    students: List[Dict] = [{"name":..., "student_id":..., "courses":[(课程名,星期,节次,周次),...]}]
    """

    def __init__(self):
        # 默认：张敏杰真实课表 + 2 份模拟课表（同一班的同学，课程时间与张敏杰相近但略有差异）
        self.students = [
            {"name": STUDENT_NAME, "student_id": STUDENT_ID, "courses": list(COURSES)},
            self._mock_student("李雷", "3242705105", shift_days={"周四": "周四", "周五": "周五"}),
            self._mock_student("韩梅梅", "3242705106", shift_days={"周四": "周四", "周五": "周五"}),
        ]
        # 兼容旧接口：当前默认学生
        self.name = self.students[0]["name"]
        self.student_id = self.students[0]["student_id"]
        self.semester = SEMESTER
        self.courses = list(self.students[0]["courses"])

    def _mock_student(self, name: str, sid: str, shift_days: Dict[str, str]) -> Dict:
        """基于张敏杰课表生成模拟同学课表（调整部分课程时间，模拟不同选课）"""
        courses = []
        for c in COURSES:
            course_name, weekday, period, weeks = c
            # 模拟差异：部分课程换到其他星期
            if weekday == "周二" and "大学英语" in course_name:
                weekday = "周三"
            if weekday == "周三" and "数据库" in course_name:
                weekday = "周二"
            courses.append((course_name, weekday, period, weeks))
        return {"name": name, "student_id": sid, "courses": courses}

    def load_from_excel(self, filepath: str):
        """
        从 Excel 加载学生课表（替换默认学生课表）

        期望格式（第一行为表头）：
        | 课程名 | 星期 | 节次 | 周次 |
        | 高等数学A2 | 周一 | 1-2节 | 1-15周 |
        | 大学物理实验C | 周四 | 5-7节 | 4-10周 |
        """
        import openpyxl
        wb = openpyxl.load_workbook(filepath)
        ws = wb.active

        new_courses = []
        for row in ws.iter_rows(min_row=2, values_only=True):
            # 兼容多列：课程名、星期、节次、周次
            name, weekday, period, weeks = row[0], row[1], row[2], row[3]
            if not name or not weekday or not period:
                continue
            new_courses.append((str(name).strip(), str(weekday).strip(),
                                str(period).strip(), str(weeks).strip() if weeks else ""))

        if not new_courses:
            raise ValueError("未解析到有效课程数据，请检查文件格式（表头: 课程名, 星期, 节次, 周次）")

        # 替换第一个学生（默认学生）的课表
        self.students[0]["courses"] = new_courses
        self.courses = new_courses
        return len(new_courses)

    def parse_weeks(self, weeks_expr: str) -> Set[int]:
        """解析周次表达式，如 '1-15周' '4-6周(双),10-16周(双)' '1-6周,8-10周,12-17周'"""
        weeks = set()
        for part in weeks_expr.split(","):
            part = part.strip().replace("周", "")
            if not part:
                continue
            m = re.match(r'^(\d+)(?:-(\d+))?(?:\((\w+)\))?$', part)
            if not m:
                continue
            start = int(m.group(1))
            end = int(m.group(2)) if m.group(2) else start
            parity = m.group(3)  # 单/双

            for w in range(start, end + 1):
                if parity == "单" and w % 2 == 0:
                    continue
                if parity == "双" and w % 2 == 1:
                    continue
                weeks.add(w)
        return weeks

    def period_to_slot(self, period: str) -> Optional[str]:
        """节次 → 实验排课时间段标记"""
        m = re.match(r'^(\d+)-(\d+)节$', period.strip())
        if not m:
            return None
        start, end = int(m.group(1)), int(m.group(2))
        if start <= 2:
            return "MORNING"  # 上午有课（上午不排实验）
        if start >= 9:
            return "91011"
        if end <= 4:
            return "34"
        if start <= 6:
            return "567"
        return "78"

    def get_busy_slots(self, target_week: int) -> Dict[str, Set[str]]:
        """
        获取某周所有学生被占用的 (星期 → 时间段集合)——并集（P1-2 多学生）
        时间段标记: MORNING(上午), 34, 567, 78, 91011
        """
        busy: Dict[str, Set[str]] = {
            "周一": set(), "周二": set(), "周三": set(),
            "周四": set(), "周五": set(), "周六": set(), "周日": set(),
        }
        for student in self.students:
            for name, weekday, period, weeks_expr in student["courses"]:
                if target_week not in self.parse_weeks(weeks_expr):
                    continue
                slot = self.period_to_slot(period)
                if slot and weekday in busy:  # 忽略"晚上"等非星期键
                    busy[weekday].add(slot)
        return busy

    def get_busy_courses(self, target_week: int) -> Dict[str, Dict[str, List[str]]]:
        """某周被占用的 (星期 → 时间段 → 课程名列表)

        与 `get_busy_slots` 同源，区别是**保留课程名**：排除原因里写"学生该时段有课（高等数学A2）"
        比写"（学生课表课程）"有用得多 —— 教秘要拿这个去跟教师解释为什么这个时段不行。
        只返回课程名，不返回学生姓名/学号（那是越权项，这里没有暴露面）。
        """
        busy: Dict[str, Dict[str, List[str]]] = {}
        for student in self.students:
            for name, weekday, period, weeks_expr in student["courses"]:
                if target_week not in self.parse_weeks(weeks_expr):
                    continue
                slot = self.period_to_slot(period)
                if not slot or weekday not in ("周一", "周二", "周三", "周四", "周五", "周六", "周日"):
                    continue
                bucket = busy.setdefault(weekday, {}).setdefault(slot, [])
                if name not in bucket:
                    bucket.append(name)
        return busy

    def is_student_busy(self, target_week: int, weekday: str, time_slot: str) -> bool:
        """检查学生在 (周, 星期, 时段) 是否有课"""
        busy = self.get_busy_slots(target_week)
        if weekday not in busy:
            return False

        # 时间段映射：实验排课用 567/678/91011/78
        # 学生课表时间段：MORNING/34/567/78/91011
        if time_slot in ("567", "678") and "567" in busy[weekday]:
            return True
        if time_slot == "78" and "78" in busy[weekday]:
            return True
        if time_slot == "91011" and "91011" in busy[weekday]:
            return True
        if time_slot == "34" and "34" in busy[weekday]:
            return True
        return False

    def summary(self) -> str:
        """课表摘要"""
        return (
            f"{self.name}（学号{self.student_id}）{self.semester}学期\n"
            f"共 {len(self.courses)} 门课程"
        )
