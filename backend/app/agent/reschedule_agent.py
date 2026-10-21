"""调课智能体 - 处理教师调课请求，筛选空闲时段"""

import json
from datetime import date, timedelta
from typing import List, Dict, Optional
from app.utils.excel_parser import ScheduleDatabase
from app.agent.student_schedule import StudentSchedule
from app.agent.llm_intent_parser import LLMIntentParser

# 学期起始日（第1周周一）默认值；实际从 schedule.xlsx 解析覆盖
SEMESTER_START = date(2025, 2, 24)
WEEKDAY_OFFSET = {"周一": 0, "周二": 1, "周三": 2, "周四": 3, "周五": 4, "周六": 5, "周日": 6}


def week_to_date(week: int, weekday: str, start: Optional[date] = None) -> str:
    """周次 + 星期 → 具体日期（MM月DD日），start 为学期起始日（第1周周一）"""
    try:
        base = start or SEMESTER_START
        d = base + timedelta(days=(week - 1) * 7 + WEEKDAY_OFFSET.get(weekday, 0))
        return d.strftime("%m月%d日")
    except Exception:
        return ""


class RescheduleAgent:
    """
    调课智能体

    核心逻辑：
    1. 教师说"调到下周" → 解析出目标周次（规则解析优先，失败时降级到 LLM）
    2. 从排课数据库中查出该周所有已占用时段
    3. 结合学生课表，筛掉学生有课的时段
    4. 剩余 = 教室空闲 + 学生没课 = 可用时段
    """

    def __init__(self, db: ScheduleDatabase, student: Optional[StudentSchedule] = None,
                 llm_parser: Optional[LLMIntentParser] = None):
        self.db = db
        self.student = student
        self.llm_parser = llm_parser or LLMIntentParser()
        # 学期起始日：缓存一次解析结果，避免每次筛选都重新打开 Excel（慢）
        self._semester_start_cache = None

    def _get_semester_start(self):
        """动态获取学期起始日（缓存解析结果，避免重复打开 Excel）"""
        if self._semester_start_cache is None:
            try:
                self._semester_start_cache = self.db.get_semester_start_date() or SEMESTER_START
            except Exception:
                self._semester_start_cache = SEMESTER_START
        return self._semester_start_cache

    def _cn_to_int(self, s: str) -> int:
        """中文数字转阿拉伯数字（一~二十）"""
        cn_map = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
                  "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
        if s == "十":
            return 10
        if len(s) == 1:
            return cn_map.get(s, 0)
        if s.startswith("十"):  # 十一、十二...
            return 10 + cn_map.get(s[1], 0)
        if s.endswith("十"):    # 二十、三十...
            return cn_map.get(s[0], 0) * 10
        if "十" in s:           # 二十三...
            parts = s.split("十")
            return cn_map.get(parts[0], 0) * 10 + cn_map.get(parts[1], 0)
        return 0

    def parse_request(self, text: str) -> dict:
        """解析自然语言请求，提取关键信息

        关键逻辑：
        - "第六周的课上不了，调到下周" → 源周=6，目标周=7
        - "调到第5周" → 目标周=5
        - "下周"（无上下文）→ 当前周+1
        """
        text = text.lower()
        import re

        week_range = self.db.get_week_range()
        current_week = week_range[0] if week_range else 1

        # 1. 解析源周次：文本中出现的"第N周"（含中文数字）
        source_week = None
        m = re.search(r'第([\d一二三四五六七八九十]+)周', text)
        if m:
            num_str = m.group(1)
            source_week = int(num_str) if num_str.isdigit() else self._cn_to_int(num_str)

        # 2. 确定目标周次
        # "下下周" 必须先判：它包含子串 "下周"，顺序反了会让 "下下周" 命中 +1 分支，
        # 于是 +2 那条分支永远不可达（原来的顺序就是这个 bug）。
        target_week = None
        if "下下周" in text:
            base = source_week if source_week else current_week
            target_week = base + 2
        elif "下周" in text:
            # 有源周 → 源周+1；无源周 → 当前周+1
            base = source_week if source_week else current_week
            target_week = base + 1
        elif "这周" in text or "本周" in text:
            target_week = source_week if source_week else current_week
        elif source_week is not None:
            # 直接指定"第N周"
            target_week = source_week

        # 上界夹取：`get_week_range()` 在数据未加载时返回 `(0, 0)`（见 excel_parser.py），
        # 而元组 `(0, 0)` 是**真值**——原来的 `min(target_week, week_range[1])`
        # 会把目标周次夹成 0。0 是假值，下游就当成"没解析出周次"，
        # 于是白白调用一次 LLM 并回一句"请补充信息"（实测每次约 5 秒）。
        # 因此只在拿到有效上界时才夹。
        if target_week and week_range and week_range[1]:
            target_week = min(target_week, week_range[1])

        # 提取教师/课程信息（简单规则）
        teacher_name = None
        class_name = None

        # "我是XXX" / "我是XXX老师"
        m_teacher = re.search(r'我(?:是|叫)([\u4e00-\u9fff]{2,4})(?:老师)?', text)
        if m_teacher:
            teacher_name = m_teacher.group(1)

        if not teacher_name:
            for t in self.db.get_all_teachers():
                if t in text:
                    teacher_name = t
                    break

        for c in self.db.get_all_classes():
            if c in text:
                class_name = c
                break

        return {
            "target_week": target_week,
            "source_week": source_week,
            "teacher_name": teacher_name,
            "class_name": class_name,
            "original_text": text,
        }

    def find_available_slots(self, target_week: int,
                             exclude_rooms: Optional[List[str]] = None,
                             teacher_name: Optional[str] = None) -> List[Dict]:
        """查找指定周的空闲时段（教室空闲 + 学生没课 + 教师自身时间空闲）"""
        if not self.db.loaded:
            return []

        weekdays = ["周一", "周二", "周三", "周四", "周五"]
        time_slots = ["567", "678", "91011"]  # 下午三个时段
        rooms = self.db.get_all_rooms()

        if exclude_rooms:
            rooms = [r for r in rooms if r not in exclude_rooms]

        # 已占用：{weekday_time_room} 集合（教室被占）
        occupied = set()
        # 教师已占用：{weekday_time} 集合（教师自身该时段有课）
        teacher_occupied = set()
        for e in self.db.entries:
            if e.week == target_week:
                occupied.add(f"{e.weekday}_{e.time_slot}_{e.room}")
                if teacher_name and e.teacher and teacher_name in e.teacher:
                    teacher_occupied.add(f"{e.weekday}_{e.time_slot}")

        # 学生忙碌时段：{weekday: {time_slot, ...}}
        student_busy = {}
        if self.student:
            student_busy = self.student.get_busy_slots(target_week)

        available = []
        for wd in weekdays:
            for ts in time_slots:
                # 检查0：教师自身该时段是否有课
                if teacher_name and f"{wd}_{ts}" in teacher_occupied:
                    continue
                for rm in rooms:
                    # 检查1：教室空闲
                    if f"{wd}_{ts}_{rm}" in occupied:
                        continue
                    # 检查2：学生没课（567/678 归入 567 检查）
                    if self.student and wd in student_busy:
                        check_ts = "567" if ts in ("567", "678") else ts
                        if check_ts in student_busy[wd]:
                            continue
                    available.append({
                        "weekday": wd,
                        "time_slot": ts,
                        "room": rm,
                    })

        return available

    def find_slots_with_reasons(self, target_week: int,
                                teacher_name: Optional[str] = None) -> Dict:
        """
        查找可用时段 + 被排除时段（含原因标注）—— F4 信息透传

        返回: {"available": [...], "excluded": [...]}
        excluded 每项含 reason_type / reason
        """
        if not self.db.loaded:
            return {"available": [], "excluded": []}

        weekdays = ["周一", "周二", "周三", "周四", "周五"]
        time_slots = ["567", "678", "91011"]
        rooms = self.db.get_all_rooms()

        # 教室占用：{weekday_time_room} → 占用课程信息
        occupied_info: Dict[str, Dict] = {}
        # 教师占用：{weekday_time} → 占用课程信息
        teacher_occupied: Dict[str, Dict] = {}
        for e in self.db.entries:
            if e.week == target_week:
                key = f"{e.weekday}_{e.time_slot}_{e.room}"
                if key not in occupied_info:
                    occupied_info[key] = {
                        "class_name": e.class_name,
                        "experiment": e.experiment,
                        "teacher": e.teacher or "",
                    }
                if teacher_name and e.teacher and teacher_name in e.teacher:
                    tk = f"{e.weekday}_{e.time_slot}"
                    if tk not in teacher_occupied:
                        teacher_occupied[tk] = {
                            "class_name": e.class_name,
                            "experiment": e.experiment,
                            "teacher": e.teacher or teacher_name,
                        }

        # 学生忙碌时段：{weekday: {time_slot: 课程名文案}}
        # 优先用"带课程名"的查询（教秘要拿课程名去跟教师解释）；取不到时退回占位文案，
        # 但**绝不因为取不到名字就放行**——冲突判定仍然以 get_busy_slots 为准。
        student_busy = {}
        student_busy_courses: Dict[str, Dict[str, list]] = {}
        if self.student:
            busy = self.student.get_busy_slots(target_week)
            get_names = getattr(self.student, "get_busy_courses", None)
            if callable(get_names):
                try:
                    student_busy_courses = get_names(target_week) or {}
                except Exception:
                    student_busy_courses = {}
            for wd, slots in busy.items():
                student_busy[wd] = {}
                for s in slots:
                    names = student_busy_courses.get(wd, {}).get(s) or []
                    student_busy[wd][s] = "、".join(names) if names else "学生课表课程"

        available = []
        excluded = []
        for wd in weekdays:
            for ts in time_slots:
                # 检查0：教师自身该时段是否有课
                if teacher_name and f"{wd}_{ts}" in teacher_occupied:
                    info = teacher_occupied[f"{wd}_{ts}"]
                    excluded.append({
                        "weekday": wd, "time_slot": ts, "room": "",
                        "date": week_to_date(target_week, wd, self._get_semester_start()),
                        "reason_type": "teacher_busy",
                        # 结构化字段与中文文案并列给出。**下游不要再用正则从 reason 里反解**：
                        # 文案是给人看的，改一个字（比如换个括号）就会静默解析失败。
                        "class_name": info["class_name"],
                        "experiment": info["experiment"],
                        "teacher": info["teacher"],
                        "reason": f"您（{info['teacher']}）该时段已有课："
                                  f"{info['class_name']} {info['experiment']}",
                    })
                    continue
                for rm in rooms:
                    # 检查1：教室空闲
                    if f"{wd}_{ts}_{rm}" in occupied_info:
                        info = occupied_info[f"{wd}_{ts}_{rm}"]
                        excluded.append({
                            "weekday": wd, "time_slot": ts, "room": rm,
                            "date": week_to_date(target_week, wd, self._get_semester_start()),
                            "reason_type": "classroom_busy",
                            "class_name": info["class_name"],
                            "experiment": info["experiment"],
                            "teacher": info["teacher"],
                            "reason": f"教室已被占用（{info['class_name']} {info['experiment']}）",
                        })
                        continue
                    # 检查2：学生没课（567/678 归入 567 检查）
                    if self.student and wd in student_busy:
                        check_ts = "567" if ts in ("567", "678") else ts
                        if check_ts in student_busy[wd]:
                            excluded.append({
                                "weekday": wd, "time_slot": ts, "room": rm,
                                "date": week_to_date(target_week, wd, self._get_semester_start()),
                                "reason_type": "student_busy",
                                "courses": student_busy_courses.get(wd, {}).get(check_ts) or [],
                                "reason": f"学生该时段有课（{student_busy[wd][check_ts]}）",
                            })
                            continue
                    available.append({
                        "weekday": wd,
                        "time_slot": ts,
                        "room": rm,
                        "date": week_to_date(target_week, wd, self._get_semester_start()),
                    })

        return {"available": available, "excluded": excluded}

    def process_request(self, text: str, with_ai: bool = True) -> dict:
        """处理调课请求，返回可用时段 + 被排除时段（含原因）

        解析策略：规则解析优先；规则失败时降级到 LLM 解析；
        LLM 判定信息不全时返回追问问题（B2 多轮追问）。
        with_ai=False 时跳过 LLM 候选方案生成（fast 模式，秒回）。
        """
        parsed = self.parse_request(text)

        # 规则解析失败 → 尝试 LLM 解析（B1）
        if not parsed["target_week"] and self.llm_parser:
            llm_result = self.llm_parser.parse(text)
            if not llm_result.get("error"):
                # LLM 判定信息不全 → 返回追问问题（B2）
                if llm_result.get("needs_followup"):
                    questions = llm_result.get("followup_questions", [])
                    msg = "需要补充信息才能为您调课：\n"
                    for q in questions:
                        msg += f"  • {q}\n"
                    return {
                        "success": False,
                        "need_info": True,
                        "message": msg,
                        "followup_questions": questions,
                        "available_slots": [],
                        "excluded_slots": [],
                    }
                # LLM 解析出目标周次 → 合并到 parsed
                if llm_result.get("target_week"):
                    parsed["target_week"] = llm_result["target_week"]
                    if not parsed.get("teacher_name") and llm_result.get("teacher"):
                        parsed["teacher_name"] = llm_result["teacher"]
                    if not parsed.get("source_week") and llm_result.get("source_week"):
                        parsed["source_week"] = llm_result["source_week"]

        if not parsed["target_week"]:
            return {
                "success": False,
                "need_info": True,
                "message": "无法确定您要调到哪一周，请明确说明（如：下周、第5周），或告诉我您是哪位老师、要调哪门课",
                "available_slots": [],
                "excluded_slots": [],
            }

        if not self.db.loaded:
            return {
                "success": False,
                "message": "排课数据尚未加载，请先上传实验安排表",
                "available_slots": [],
                "excluded_slots": [],
            }

        result = self.find_slots_with_reasons(parsed["target_week"], teacher_name=parsed.get("teacher_name"))
        slots = result["available"]
        excluded = result["excluded"]

        if not slots:
            return {
                "success": True,
                "message": f"第{parsed['target_week']}周没有空闲时段，请尝试其他周次",
                "available_slots": [],
                "excluded_slots": excluded,
            }

        # 按星期几分组展示
        by_weekday = {}
        for s in slots:
            by_weekday.setdefault(s["weekday"], []).append(s)

        summary_lines = [f"📋 第{parsed['target_week']}周可用时段："]
        for wd in ["周一", "周二", "周三", "周四", "周五"]:
            if wd in by_weekday:
                summary_lines.append(f"\n{wd}：")
                for s in by_weekday[wd][:5]:  # 每个星期几只展示5个
                    summary_lines.append(f"  {s['time_slot']} · {s['room']}")

        total = len(slots)
        summary_lines.append(f"\n共 {total} 个空闲时段")

        # 补充被排除时段摘要（原因标注，F4）
        if excluded:
            summary_lines.append(f"\n\n🚫 被排除时段（{len(excluded)} 个，原因见下）：")
            for ex in excluded[:6]:
                summary_lines.append(f"  ✗ {ex['weekday']} {ex['time_slot']} {ex['room']} — {ex['reason']}")

        # B3: 生成 AI 候选方案（with_ai=True 时；fast 模式完全跳过候选生成秒回）
        ai_plans = []
        if with_ai:
            if self.llm_parser and self.llm_parser.configured:
                try:
                    course_hint = parsed.get("course_name") or "课程"
                    plans_result = self.llm_parser.suggest_plans(
                        teacher=parsed.get("teacher_name") or "教师",
                        course=course_hint,
                        target_week=parsed["target_week"],
                        available_slots=slots,
                    )
                    ai_plans = plans_result.get("plans", [])
                except Exception:
                    ai_plans = []  # LLM 异常时降级

            # LLM 候选为空（服务不稳定）→ 规则降级：按星期去重挑 3 个候选
            if not ai_plans:
                seen_days = set()
                for s in slots:
                    if s["weekday"] not in seen_days and len(ai_plans) < 3:
                        seen_days.add(s["weekday"])
                        ai_plans.append({
                            "weekday": s["weekday"],
                            "time_slot": s["time_slot"],
                            "room": s["room"],
                            "reason": "系统推荐：该时段教室空闲且学生无课",
                        })

        if ai_plans:
            summary_lines.append(f"\n\n🤖 推荐候选方案：")
            for i, p in enumerate(ai_plans[:3], 1):
                summary_lines.append(
                    f"  {i}. {p['weekday']} {p['time_slot']} · {p['room']} — {p.get('reason', '')}"
                )

        return {
            "success": True,
            "message": "\n".join(summary_lines),
            "target_week": parsed["target_week"],
            "available_slots": slots,
            "excluded_slots": excluded,
            "ai_plans": ai_plans,
            "total": total,
        }
