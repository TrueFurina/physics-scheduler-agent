"""调课智能体 - 处理教师调课请求，筛选空闲时段"""

from datetime import date, timedelta
from typing import List, Dict, Optional, Tuple
from app.utils.excel_parser import ScheduleDatabase, same_teacher
from app.utils.time_model import parse_clock_range
from app.agent.llm_intent_parser import LLMIntentParser

# 学期起始日（第1周周一）默认值；实际从 schedule.xlsx 解析覆盖
SEMESTER_START = date(2025, 2, 24)
WEEKDAY_OFFSET = {"周一": 0, "周二": 1, "周三": 2, "周四": 3, "周五": 4, "周六": 5, "周日": 6}

# 调课沙盘要覆盖的星期：**全部七天**，来自 WEEKDAY_OFFSET 这一个来源。
#
# R36：此前三处各写一份 `["周一"…"周五"]`，把周末整个漏掉。真实数据 433 条里
# 有 71 条在周末（周六 36 / 周日 35）—— 每 6 条课就有 1 条。
#
# 实测后果（教师H，第 2 周真实有周六课）：
#     find_slots_with_reasons(2, teacher_name='教师H')
#       周六可用：0    周六被排除：0
# 既不可用也不在排除列表 ⇒ **从沙盘上消失**。教秘看到一份完整的 5 天沙盘，
# 不会意识到周六还有课，更不会想到"这周六可能没空"。
#
# 排课引擎（auto_scheduler / parallel_scheduler）覆盖周末是既定口径，
# 调课沙盘却只看 5 天 —— 同一份数据在两条链路上口径不同。
WEEKDAYS_ALL = tuple(WEEKDAY_OFFSET)  # 保持 周一→周日 的顺序

# 多轮追问要填的槽位（B2）。后端不存会话，只把已确认的槽位随响应回传，
# 由前端在下一轮原样带回，服务端做无状态合并 —— 进程被回收也不丢上下文。
# `class_name` 也在里面：同一门实验（课程名相同）会挂在不同班级下，
# 只按课程名回找课程卡片会撞车（实测"扭摆法测物体转动惯量"就有两个班）。
SLOT_KEYS = ("teacher_name", "course_name", "class_name", "target_week", "source_week")


def _raw_slot_note(info: Dict) -> str:
    """占用源头若是**跨节次**写法，把原表时间补进文案。

    教秘看到"周四 678 不可用"时，会回表里找 678 那一格 —— 而原表那格写的是
    `14:00-17:00`。不点明这层对应关系，他会以为系统读错了。

    ⚠️ 只在**真的跨了节次**（blocked 多于一个）时才说。原表写 `13:30-15:45`
    的记录也是"时间区间"格式，但它只占 567 —— 对它说"跨到本时段"是假话，
    上一版就犯过（凡是区间格式都加注解）。
    """
    info = info or {}
    if len(info.get("blocked") or []) < 2:
        return ""
    raw = info.get("time_raw") or ""
    return f"（该课原表时段为 {raw}，跨到本时段）" if parse_clock_range(raw) else ""


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
    3. 剩余 = 教室空闲 + 教师自身时间空闲 = 可用时段
    """

    def __init__(self, db: ScheduleDatabase,
                 llm_parser: Optional[LLMIntentParser] = None):
        self.db = db
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

    def week_bounds(self) -> Optional[Tuple[int, int]]:
        """本次数据实际覆盖的周次区间；判定不了（未加载 / 解析不出周次）时返回 None。

        为什么需要单独一个方法：`get_week_range()` 在数据未加载时返回 `(0, 0)`
        ——而元组 `(0, 0)` 是**真值**，任何 `if week_range:` 都会误判为"有范围"。
        这里把"有范围"的定义收敛成一处：必须已加载且上界 > 0。
        """
        if not self.db.loaded:
            return None
        lo, hi = self.db.get_week_range()
        if not hi:
            return None
        return (lo or 1, hi)

    def parse_request(self, text: str) -> dict:
        """解析自然语言请求，提取关键信息

        关键逻辑：
        - "第六周的课上不了，调到下周" → 源周=6，目标周=7
        - "调到第5周" → 目标周=5
        - "下周"（无上下文）→ 当前周+1
        """
        text = text.lower()
        import re

        # R51：`current_week` 必须是"真的知道当前是第几周"，而不是"猜一个"。
        #
        # `get_week_range()` 空表时返回 `(0, 0)` —— 元组是**真值**，
        # 于是 `week_range[0] if week_range else 1` 取到 **0**。
        # 0 又是假值，下游 `if not parsed["target_week"]` 就把它当成
        # "用户没说周次"，追问"要把课调到第几周" —— 而用户明明说了「这周」。
        #
        # 更糟的是「下周」：0+1 = 1，得到一个**看起来合理、实则凭空猜的**第 1 周。
        #
        # 数据没加载时，"当前周"根本无从得知。相对表达（这周/下周/下下周）
        # 在没有基准时不该成立 —— 让 current_week 为 None，
        # 由下面的分支自然落到"没算出目标周"，交给统一的范围校验去追问。
        bounds_now = self.week_bounds()
        current_week = bounds_now[0] if bounds_now else None

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
        # 相对表达的基准：优先源周，其次"当前周"。两者都没有（空表）时为 None，
        # 此时相对表达**不成立** —— 不能凭空算出一个周次（见 current_week 处注释）。
        if "下下周" in text:
            base = source_week if source_week is not None else current_week
            target_week = (base + 2) if base is not None else None
        elif "下周" in text:
            # 有源周 → 源周+1；无源周 → 当前周+1
            base = source_week if source_week is not None else current_week
            target_week = (base + 1) if base is not None else None
        elif "这周" in text or "本周" in text:
            # 「这周」与「下周」对称：有源周就以源周为基准（−1），无源周才用当前周。
            #
            # 原来这里写的是 `target_week = source_week if source_week else current_week`，
            # 于是「第6周的课上不了，这周先调一下」把目标算成**第 6 周**，和源周一样，
            # 等于原地打转；教秘看到的却是"第6周可用时段"，很像成功了。
            # 而同一个句子把"这周"换成"下周"就正确（源周+1）—— 两个分支语义不一致。
            #
            # 与"下周"的差别只在方向：中文里"这周的课"指**已经发生/正在发生**的那次，
            # 说"要调"就是想往前挪。‑1 落在第 1 周 → 0 周，交给统一范围校验拒绝，
            # 不再静默绕回第 1 周（那同样等于替教秘改口）。
            base = source_week if source_week is not None else current_week
            if base is None:
                target_week = None          # 无基准 → 不猜
            else:
                target_week = base - 1 if source_week is not None else base
        elif source_week is not None:
            # 直接指定"第N周"
            target_week = source_week

        # 关于上界：这里**不再夹取**，改由 `process_request` 统一做范围校验并明确拒绝。
        #
        # 历史教训要留着：`get_week_range()` 在数据未加载时返回 `(0, 0)`，而元组
        # `(0, 0)` 是**真值**——原来的 `min(target_week, week_range[1])` 会把目标周次
        # 夹成 0，0 是假值，下游当成"没解析出周次"，于是白调一次 LLM 并回一句
        # "请补充信息"（实测每次 5 秒）。现在这个坑由 `week_bounds()` 兜（必须已加载
        # 且上界 > 0 才算有范围）。
        #
        # 为什么连"超上界的值"也不夹了：静默夹取等于**替教秘改口**——他说第 17 周，
        # 沙盘却画第 16 周，他很可能没注意到就提交了。而规则路径夹取、LLM 路径不夹，
        # 同一条输入还会因解析来源不同而行为不一致（R-32 的负数就是这么漏进来的）。
        # 现在两条路径汇合后统一判定：范围外 → 说清范围，不出结果。

        # 提取教师/课程信息（简单规则）
        teacher_name = None
        class_name = None

        # "我是XXX" / "我是XXX老师"
        #
        # ⚠️ 姓名必须允许**中英文混合**：真实教师名有「教师H」「教师K」这类。
        # 原先只写 `[\u4e00-\u9fff]{2,4}`，遇到 ASCII 字母就截断：
        #   "我是教师H，把…调到第2周" → 抓到 "教师"
        # 而 "教师" 是**所有**教师名的子串，于是 teacher_occupied 把别的教师
        # 的课也记成了"您自己的课"，沙盘显示「您（教师K）该时段已有课」。
        # 实测：教师H 调课，周一 567 被误标为 teacher_busy（教师K），
        # 而真实原因是教室被占（classroom_busy）。
        #
        # 用「中文或 ASCII 字母数字」的字符类，并排除标点/空格。
        m_teacher = re.search(
            r'我(?:是|叫)([\u4e00-\u9fffA-Za-z0-9]{2,8})(?:老师)?', text)
        if m_teacher:
            cand = m_teacher.group(1)
            # 再与真实教师名单核对：能精确命中就用名单里的名字，
            # 避免"教师"这种前缀被当成完整姓名。
            # 注意：函数开头已 `text = text.lower()`（第 112 行），
            # 所以 cand 是**小写**的（"教师h"）。这里与名单做**大小写不敏感**
            # 匹配，返回名单里的**原样名字**（"教师H"），否则后续
            # `teacher_name in e.teacher` 永远匹配不上。
            all_teachers = self.db.get_all_teachers()
            low = cand.lower()

            exact = [n for n in all_teachers if n.lower() == low]
            if exact:
                teacher_name = exact[0]
            else:
                # 前缀匹配（长名优先），避免"教师"被当成完整姓名
                prefixed = sorted(
                    (n for n in all_teachers if n.lower().startswith(low)),
                    key=len, reverse=True,
                )
                teacher_name = prefixed[0] if prefixed else cand

        if not teacher_name:
            for t in self.db.get_all_teachers():
                if t in text:
                    teacher_name = t
                    break

        # 班级名同样**长名优先**。`get_all_classes()` 返回的是 sorted()，
        # 按字典序排 —— 而字典序里 "2024级电子科学与技术" 排在
        # "2024级电子科学与技术1班" 前面，短名会先命中、把班名截断。
        # 当前 17 个真实班名里没有前缀对，但这是**数据巧合**而非代码保证，
        # 学校新增一个班就会静默出错。
        for c in sorted(self.db.get_all_classes(), key=len, reverse=True):
            if c in text:
                class_name = c
                break

        # 课程名（F2 里 course_name 就是实验名）——拿教师名下课程做匹配，
        # **长名优先**：否则"声速"会先吃掉"声速测量"、"分光计"会吃掉
        # "分光计测量光栅常数及光波波长"这类超集关系，匹配出一门错课。
        course_name = None
        if teacher_name:
            names = {c.get("course_name") for c in self.db.get_teacher_courses(teacher_name)}
        else:
            names = {e.experiment for e in self.db.entries}
        for n in sorted((x for x in names if x), key=len, reverse=True):
            if n.lower() in text:
                course_name = n
                break

        return {
            "target_week": target_week,
            "source_week": source_week,
            "teacher_name": teacher_name,
            "course_name": course_name,
            "class_name": class_name,
            "original_text": text,
        }

    def find_slots_with_reasons(self, target_week: int,
                                teacher_name: Optional[str] = None) -> Dict:
        """
        查找可用时段 + 被排除时段（含原因标注）—— F4 信息透传

        返回: {"available": [...], "excluded": [...]}
        excluded 每项含 reason_type / reason
        """
        if not self.db.loaded:
            return {"available": [], "excluded": []}

        weekdays = list(WEEKDAYS_ALL)
        time_slots = ["567", "678", "91011"]
        rooms = self.db.get_all_rooms()

        # 教室占用：{weekday_time_room} → 占用课程信息
        occupied_info: Dict[str, Dict] = {}
        # 教师占用：{weekday_time} → 占用课程信息
        teacher_occupied: Dict[str, Dict] = {}
        for e in self.db.entries:
            if e.week == target_week:
                # 逐 blocked 节次记占用，理由同 get_available_slots（R-40）
                for slot in getattr(e, "blocked", None) or [e.time_slot]:
                    key = f"{e.weekday}_{slot}_{e.room}"
                    if key not in occupied_info:
                        occupied_info[key] = {
                            "class_name": e.class_name,
                            "experiment": e.experiment,
                            "teacher": e.teacher or "",
                            "time_raw": e.time_raw or "",
                            "blocked": list(e.blocked),
                        }
                    # 精确匹配（不走子串）：「吴琳」不得把「吴琳琳」的课算成本人有课。
                    # 走同一个判据函数，与写路径/课程列表口径一致。
                    if teacher_name and e.teacher and same_teacher(teacher_name, e.teacher):
                        tk = f"{e.weekday}_{slot}"
                        if tk not in teacher_occupied:
                            teacher_occupied[tk] = {
                                "class_name": e.class_name,
                                "experiment": e.experiment,
                                "teacher": e.teacher or teacher_name,
                                "time_raw": e.time_raw or "",
                                "blocked": list(e.blocked),
                            }

        # 时段看不懂、已被剔除的行：只知道"这一天这间教室/这个人在上课"，
        # 不知道具体节次 ⇒ **整天按占用处理**。宁可少推荐三个时段，
        # 也不能把教秘推到一个已知有人在用的教室里。
        unparsed_here = [
            s for s in (getattr(self.db, "skipped", None) or [])
            if s.get("week") == target_week
        ]
        for s in unparsed_here:
            wd, rm, te = s.get("weekday"), s.get("room"), s.get("teacher")
            info = {
                "class_name": s.get("class_name") or "",
                "experiment": s.get("experiment") or "",
                "teacher": te or "",
                "unparsed_ts": s.get("raw_ts") or "",
            }
            for ts in time_slots:
                if rm:
                    occupied_info.setdefault(f"{wd}_{ts}_{rm}", {**info, "unparsed": True})
                if teacher_name and te and teacher_name in te:
                    teacher_occupied.setdefault(f"{wd}_{ts}", {**info, "unparsed": True})

        available = []
        excluded = []
        for wd in weekdays:
            for ts in time_slots:
                # 检查0：教师自身该时段是否有课
                if teacher_name and f"{wd}_{ts}" in teacher_occupied:
                    info = teacher_occupied[f"{wd}_{ts}"]
                    if info.get("unparsed"):
                        why = (f"您（{info['teacher']}）当天已有课（{info['class_name']} "
                               f"{info['experiment']}），该行时段填的是「{info['unparsed_ts']}」"
                               f"无法识别，已按整日占用处理")
                    else:
                        why = (f"您（{info['teacher']}）该时段已有课："
                               f"{info['class_name']} {info['experiment']}"
                               f"{_raw_slot_note(info)}")
                    excluded.append({
                        "weekday": wd, "time_slot": ts, "room": "",
                        "date": week_to_date(target_week, wd, self._get_semester_start()),
                        "reason_type": "teacher_busy",
                        # 结构化字段与中文文案并列给出。**下游不要再用正则从 reason 里反解**：
                        # 文案是给人看的，改一个字（比如换个括号）就会静默解析失败。
                        "class_name": info["class_name"],
                        "experiment": info["experiment"],
                        "teacher": info["teacher"],
                        "reason": why,
                    })
                    continue
                for rm in rooms:
                    # 检查1：教室空闲
                    if f"{wd}_{ts}_{rm}" in occupied_info:
                        info = occupied_info[f"{wd}_{ts}_{rm}"]
                        if info.get("unparsed"):
                            why = (f"教室已被占用（{info['class_name']} {info['experiment']}），"
                                   f"该行时段填的是「{info['unparsed_ts']}」无法识别，已按整日占用处理")
                        else:
                            why = (f"教室已被占用（{info['class_name']} {info['experiment']}）"
                                   f"{_raw_slot_note(info)}")
                        excluded.append({
                            "weekday": wd, "time_slot": ts, "room": rm,
                            "date": week_to_date(target_week, wd, self._get_semester_start()),
                            "reason_type": "classroom_busy",
                            "class_name": info["class_name"],
                            "experiment": info["experiment"],
                            "teacher": info["teacher"],
                            "reason": why,
                        })
                        continue
                    available.append({
                        "weekday": wd,
                        "time_slot": ts,
                        "room": rm,
                        "date": week_to_date(target_week, wd, self._get_semester_start()),
                    })

        return {"available": available, "excluded": excluded}

    def _slot_context(self, parsed: dict) -> Dict:
        """把本轮"已确认的槽位"抽出来回传给前端，供下一轮合并（B2 多轮追问）。

        后端**刻意不存会话状态**：上下文放在教秘的浏览器里，服务端只做无状态合并。
        这样进程被沙箱回收、或多实例轮转，多轮对话都不会凭空丢一半。
        """
        return {k: parsed.get(k) for k in SLOT_KEYS if parsed.get(k)}

    def _missing_slot_questions(self, parsed: dict) -> List[str]:
        """按"还缺哪个槽位"生成追问，而不复用 LLM 的自由文案。

        两个理由，都是实测出来的：
        ① 合并上下文之后不能再问已知的信息 —— 否则教秘答完"我是吴琳"，
           系统下一秒又问"您是哪位老师"，多轮对话就成了笑话；
        ② LLM 会同时给出"调哪周的课"和"调到哪一周"两条同义问题（实测），
           槽位驱动不会产生重复。
        """
        questions = []
        if not parsed.get("teacher_name"):
            questions.append("请问是哪位老师需要调课？")
        # ⚠️ 课程名也必须问。
        #
        # R21 实测：教秘说「我是教师H，把PN结调到第3周」——简称没写全
        # （全名是「PN结正向压降温度特性实验」），规则匹配不到 → course_name=None。
        # 此前这里不检查课程，于是**不追问、直接返回全周 80 个可用时段**，
        # 教秘以为系统懂了，实际它对"要调哪门课"一无所知。
        # 这比报错更危险：一份"看起来全可用"的沙盘会让人做出错误决定。
        if not parsed.get("target_week"):
            questions.append("请问要把课调到第几周？（例如：第5周、下周、下下周）")
        return questions

    def _need_info(self, parsed: dict, llm_questions: List[str]) -> dict:
        """信息不足 → 追问。槽位驱动优先；槽位都齐了才退回 LLM 的问题。"""
        questions = self._missing_slot_questions(parsed)
        if not questions:
            questions = [q for q in llm_questions if q][:2] or ["请补充调课信息"]
        msg = "需要补充信息才能为您调课：\n" + "".join(f"  • {q}\n" for q in questions)
        return {
            "success": False,
            "need_info": True,
            "message": msg,
            "followup_questions": questions,
            "context": self._slot_context(parsed),
            "target_week": parsed.get("target_week"),
            "available_slots": [],
            "excluded_slots": [],
            # R32：追问分支原先只有 8 键，比正常分支少 4 个。
            # 追问时课程名通常也还没确定，但**字段本身必须存在** ——
            # 下游靠"字段在不在"区分契约版本，靠"值是什么"区分当前状态。
            "course_name": parsed.get("course_name"),
            "course_unrecognized": not parsed.get("course_name"),
            "ai_plans": [],
            "ai_plans_source": "none",
            "total": 0,
        }

    def process_request(self, text: str, with_ai: bool = True,
                        context: Optional[Dict] = None) -> dict:
        """处理调课请求，返回可用时段 + 被排除时段（含原因）

        解析策略：规则解析优先；规则失败时降级到 LLM 解析；
        信息不足时返回追问问题（B2 多轮追问）。
        `context` 是上一轮响应回传的槽位，用于补全本轮没说的话。
        with_ai=False 时跳过 LLM 候选方案生成（fast 模式，秒回）。
        """
        parsed = self.parse_request(text)

        # 槽位合并（B2）：本轮没解析出来的，沿用上一轮已确认的。
        # 没有这一步，"多轮追问"是假的 —— 每一轮都从零开始解析，教秘按提示
        # 回答"我是吴琳"，系统会把"您是哪位老师"原样再问一遍（实测）。
        for k in SLOT_KEYS:
            if not parsed.get(k) and (context or {}).get(k):
                parsed[k] = context[k]

        # 规则解析失败 → 尝试 LLM 解析（B1）
        if not parsed["target_week"] and self.llm_parser:
            llm_result = self.llm_parser.parse(text)
            if not llm_result.get("error"):
                # 先把 LLM 解析到的槽位并进来，再判断要不要追问：
                # 原来是一看到 needs_followup 就立刻返回，于是 LLM 同时给出的
                # 教师/源周次被丢掉，追问里又把这些再问一遍。
                if not parsed["target_week"] and llm_result.get("target_week"):
                    parsed["target_week"] = llm_result["target_week"]
                for src, dst in (("teacher", "teacher_name"),
                                 ("source_week", "source_week"),
                                 ("course", "course_name")):
                    if not parsed.get(dst) and llm_result.get(src):
                        parsed[dst] = llm_result[src]
                if llm_result.get("needs_followup") and (
                        not parsed["target_week"] or not parsed.get("teacher_name")):
                    return self._need_info(parsed, llm_result.get("followup_questions") or [])

        # 周次范围校验（R-32）
        #
        # 实测（真实数据，第 1–16 周）：`我是吴琳，把声速测量调到第-1周` 返回
        # **150 个可用 / 0 个被排除**；而正常周次是 118 可用 / 32 排除（第2周）、
        # 78 / 36（第7周）。也就是说负数周次下**冲突检查一条都没命中**：
        # 教室/教师占用是按 `e.week == target_week` 过滤的，
        # 周次落在表外就全查不到 → 冲突检查静默空转 → 满屏绿格。
        #
        # 成因是两条解析路径此前待遇不同：规则路径有 `min(target_week, 上界)` 夹取，
        # **LLM 解析出来的 target_week 完全不受约束**（负数正是这样进来的）。
        # 现在夹取也去掉了，两条路径在这里汇合后统一判定：超出范围就明说，
        # 宁可不给结果，也不给一份"看起来全可用、其实没检查"的沙盘。
        bounds = self.week_bounds()
        tw = parsed["target_week"]
        if bounds and tw is not None:
            lo, hi = bounds
            if not (lo <= tw <= hi):
                ctx = self._slot_context(parsed)
                # 非法周次不能随 context 回传：否则下一轮会把它合并回来，
                # 教秘改口说"那调到第5周"也救不回来。
                ctx.pop("target_week", None)
                # 源周次同理：`第99周` 这种值既不是合法目标也不可能是合法原周，
                # 留着它只会让下一轮「下周」算成第 100 周，被拒第二次。
                if ctx.get("source_week") is not None and not (lo <= ctx["source_week"] <= hi):
                    ctx.pop("source_week", None)
                return {
                    "success": False,
                    "message": (
                        f"第{tw}周不在实验安排表范围内（本表覆盖第{lo}–{hi}周）。"
                        f"请改成第{lo}到第{hi}周之间的周次，例如「调到第{hi}周」。"
                    ),
                    "target_week": None,
                    "context": ctx,
                    "available_slots": [],
                    "excluded_slots": [],
                    "total": 0,
                    "need_info": False,
                    "followup_questions": [],
                    # R32：五个分支的键集必须一致（见 tests/test_response_contract.py）
                    "course_name": parsed.get("course_name"),
                    "course_unrecognized": not parsed.get("course_name"),
                    "ai_plans": [],
                    "ai_plans_source": "none",
                }

        # 教师是**必需槽位**，不是可选装饰：`find_slots_with_reasons` 的"检查0"
        # 靠它判断"教师本人在这个时段有没有课"。不给教师就直出时段，等于跳过
        # 这一重冲突检查，把可能撞课的时段当成可用来推荐（实测直接返回 94 个）。
        #
        # 课程名同理（R21 实测）：教秘说「我是教师H，把PN结调到第3周」，
        # 简称匹配不到全名「PN结正向压降温度特性实验」→ course_name=None。
        # 此前这里不查课程，于是**不追问、直接返回全周 80 个可用时段**，
        # 教秘以为系统懂了，实际它对"要调哪门课"一无所知。
        # 一份"看起来全可用"的沙盘比报错更危险。
        if not parsed["target_week"] or not parsed.get("teacher_name"):
            return self._need_info(parsed, [])
        # 课程名**不阻断**：`find_slots_with_reasons` 只用 (周次, 教师) 计算
        # "这一周这个人哪些时段空着"，与具体哪门课无关。硬性追问会让
        # "第2轮回答第3周就办完"这条多轮流程失效（实测 4 个测试变红）。
        # 改为在结果里**明确提示**没识别出课程 —— 见下方 course_unrecognized。

        if not self.db.loaded:
            return {
                "success": False,
                "message": "排课数据尚未加载，请先上传实验安排表",
                "target_week": parsed.get("target_week"),
                "context": self._slot_context(parsed),
                "available_slots": [],
                "excluded_slots": [],
                # R31：所有分支的字段集必须一致 —— 少一个键，下游拿到的
                # 就是 None，而 None 在不同分支上含义完全不同（未识别 / 未计算 / 失败）。
                "course_name": parsed.get("course_name"),
                "course_unrecognized": not parsed.get("course_name"),
                "ai_plans": [],
                "ai_plans_source": "none",
                "total": 0,
                # R32：补上另两个键 —— 五个分支键集必须一致
                "need_info": False,
                "followup_questions": [],
            }

        result = self.find_slots_with_reasons(parsed["target_week"], teacher_name=parsed.get("teacher_name"))
        slots = result["available"]
        excluded = result["excluded"]

        if not slots:
            # R31：这个早返回分支原先**没有** course_name / course_unrecognized。
            # 于是同一个字段在"有结果"和"零结果"两条路径上存在性不同 ——
            # 下游 `data.get("course_unrecognized")` 在零结果时拿到 None，
            # 被当成"不是未识别"，与真实语义相反。
            #
            # 又一次"白名单返回体 + 多分支"的接缝事故（同族：API 层没透传）。
            return {
                "success": True,
                "message": f"第{parsed['target_week']}周没有空闲时段，请尝试其他周次",
                "target_week": parsed["target_week"],
                "context": self._slot_context(parsed),
                "available_slots": [],
                "excluded_slots": excluded,
                "course_name": parsed.get("course_name"),
                "course_unrecognized": not parsed.get("course_name"),
                "ai_plans": [],
                "ai_plans_source": "none",
                "total": 0,
                "need_info": False,
                "followup_questions": [],
            }

        # 按星期几分组展示
        by_weekday = {}
        for s in slots:
            by_weekday.setdefault(s["weekday"], []).append(s)

        summary_lines = [f"📋 第{parsed['target_week']}周可用时段："]
        for wd in WEEKDAYS_ALL:
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
                    for __p in ai_plans:
                        __p.setdefault("source", "llm")
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
                            "reason": "系统推荐：该时段教室空闲",
                            # R64：**必须标出来源**。
                            # 规则降级本身是好事（LLM 挂了不该没候选），
                            # 但降级产物冒充 AI 产物会让 PRD 的承诺失真 ——
                            # 而这块恰好是最容易被拿给评审看的部分。
                            "source": "rule",
                        })

        if ai_plans:
            summary_lines.append("\n\n🤖 推荐候选方案：")
            for i, p in enumerate(ai_plans[:3], 1):
                summary_lines.append(
                    f"  {i}. {p['weekday']} {p['time_slot']} · {p['room']} — {p.get('reason', '')}"
                )

        # 课程名没识别出来时**明确提示**，而不是静默出一份全周沙盘。
        #
        # R21 实测：教秘说「我是教师H，把PN结调到第3周」——简称没写全
        # （全名「PN结正向压降温度特性实验」），course_name=None。
        # 时段计算本身不受影响（`find_slots_with_reasons` 只用 周次+教师），
        # 但教秘会以为系统知道"要调哪门课"，而后续提交申请时课程名是必填的。
        #
        # 所以不阻断（不破坏多轮流程），只把这件事说出口。
        course_unrecognized = not parsed.get("course_name")
        if course_unrecognized:
            summary_lines.append(
                "\n⚠️ 没能从您的话里认出具体是哪门课/哪个实验。"
                "上面的可用时段是「这一周您名下所有课」的空闲情况；"
                "如果要针对某一门课调课，请在提交申请时选定课程。"
            )

        return {
            "success": True,
            "message": "\n".join(summary_lines),
            "target_week": parsed["target_week"],
            "context": self._slot_context(parsed),
            "available_slots": slots,
            "excluded_slots": excluded,
            # 结构化字段与中文文案并列：下游不要从 message 里正则反解
            "course_name": parsed.get("course_name"),
            "course_unrecognized": course_unrecognized,
            "ai_plans": ai_plans,
            # "llm" / "rule" / "none" —— 谁给的候选，界面据此选文案
            "ai_plans_source": (
                "none" if not ai_plans
                else ("llm" if any(p.get("source") == "llm" for p in ai_plans)
                      else "rule")
            ),
            "total": total,
            # R32：正常分支原先缺这两个键（只有追问分支才有）。
            # 调用方无法用 `r.get("need_info")` 判断"这次是不是在追问"——
            # 正常结果里它不存在，和 False 不是一回事。
            "need_info": False,
            "followup_questions": [],
        }
