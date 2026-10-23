"""调课意图解析器（LLM，OpenAI 兼容接口）

用 LLM 解析"大白话"调课请求（B1），与规则解析互补：
- 规则解析：处理标准话术（"第六周的课上不了，调到下周"）
- LLM 解析：处理口语化表达（"我下周三有点事，那个课往后挪一周行不行"）

渠道选择
--------
按 `E:\\Program\\免费Token总账.md` 的实测结论排序，**默认走智谱 `glm-4-flash`**
——它是总账里唯一的"永久免费、不限量"，适合长期挂在公网演示上；其余渠道
（ModelScope 2000 次/天、TokenHub 一次性体验包、DeepSeek 余额制…）按剩余额度
作为备选。可用 `LLM_PROVIDER` 强制指定，或用 `LLM_API_KEY` + `LLM_API_BASE`
完全自定义。

为什么改掉了原来的 `JIYUAN_LVDONG_*`
------------------------------------
原实现读 `JIYUAN_LVDONG_API_KEY`（tokenrhythm.studio 第三方中转），而总账
2026-10-15 实测该 key **余额不足 402**——即后端 LLM 当时根本调不通，只是
一直用"规则解析优先"的降级路径掩盖着。现按总账改为渠道表 + 自动探测，
并把已欠费的 jiyuan 降到最后一位兜底。

配置在**实例化时**解析（不是类定义时）
--------------------------------------
类属性在 import 时求值，测试与运行期都改不动。改为 `__init__` 里读环境变量，
类属性仅保留同名默认值以兼容历史引用。

环境变量
--------
    LLM_PROVIDER      渠道名（zhipu / modelscope / tokenhub / deepseek / amd / xfyun / jiyuan）
    LLM_API_KEY       显式密钥，优先级最高（配合 LLM_API_BASE / LLM_MODEL）
    LLM_API_BASE      显式端点
    LLM_MODEL         显式模型名
    <渠道>_API_KEY    各渠道密钥，见 `_CHANNELS`
"""

import json
import os
import re

import httpx

# ---------------------------------------------------------------------------
# 渠道表：渠道名 → {env: 候选密钥变量（有序）, base: 端点, model: 默认模型, note: 状态备注}
#
# 端点与状态全部来自 `E:\Program\免费Token总账.md`（2026-10-15 实测），
# 不是推测值。改动这里请同步更新总账的「项目接线位置」一节。
# ---------------------------------------------------------------------------
_CHANNELS = {
    "zhipu": {
        "env": ("ZHIPU_API_KEY", "ZHIPU_API_KEY_2"),
        "base": "https://open.bigmodel.cn/api/paas/v4",
        "model": "glm-4-flash",
        "note": "永久免费不限量（主防线）",
    },
    "modelscope": {
        "env": ("MODELSCOPE_API_KEY",),
        "base": "https://api-inference.modelscope.cn/v1",
        "model": "ZhipuAI/GLM-4.7-Flash",
        "note": "免费 2000 次/天，热门时段可能 429",
    },
    "tokenhub": {
        "env": ("TOKENHUB_API_KEY",),
        "base": "https://tokenhub.tencentmaas.com/v1",
        "model": "glm-5.3-flash",
        "note": "一次性体验包，额度用尽报 402",
    },
    "deepseek": {
        "env": ("DEEPSEEK_API_KEY",),
        "base": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
        "note": "余额制（要花钱），仅作后备",
    },
    "amd": {
        "env": ("AMD_API_KEY",),
        "base": "https://developer.amd.com.cn/radeon/api/v1",
        "model": "DeepSeek-V4-Flash",
        "note": "免费，限 30 RPM",
    },
    "xfyun": {
        "env": ("XFYUN_API_KEY",),
        "base": "https://spark-api-open.xf-yun.com/v1",
        "model": "lite",
        "note": "讯飞 spark lite 免费档",
    },
    "jiyuan": {
        "env": ("JIYUAN_LVDONG_API_KEY",),
        "base": "https://tokenrhythm.studio/v1",
        "model": "deepseek-v4-flash",
        "note": "⚠️ 已欠费 402（总账实测），仅历史兜底",
    },
}

# 自动探测顺序：免费/不限量优先，付费与已失效垫底
_AUTO_ORDER = ("zhipu", "modelscope", "tokenhub", "deepseek", "amd", "xfyun", "jiyuan")

# 默认端点（智谱），用于"只给了 LLM_API_KEY 没给 BASE"的场景
_DEFAULT_BASE = _CHANNELS["zhipu"]["base"]
_DEFAULT_MODEL = _CHANNELS["zhipu"]["model"]


def _endpoint_host(base_url: str) -> str:
    """从端点里取主机名，用于诊断输出（不含任何密钥）。"""
    m = re.match(r"^https?://([^/]+)", base_url or "")
    return m.group(1) if m else (base_url or "")


def resolve_llm_config() -> dict:
    """解析当前生效的 LLM 渠道配置。

    返回 dict：provider / base_url / api_key / model / configured / source / note。
    `source` 是取到密钥的环境变量名，便于线上核验"到底读的哪个渠道"——
    它**不含密钥值**，可以安全地暴露在 `/status` 里。

    `LLM_DISABLED=1` 是离线开关：整体判定为"未配置"，所有 LLM 调用走规则降级。
    给本地无网开发与 CI 用——避免测试真的打到外部 API（慢、贵、不稳定）。
    """
    if os.getenv("LLM_DISABLED", "").strip().lower() in {"1", "true", "yes", "on"}:
        return {
            "provider": None,
            "base_url": "",
            "api_key": "",
            "model": "",
            "configured": False,
            "source": None,
            "note": "LLM_DISABLED 已置位：全部走规则降级（离线开发 / CI 模式）",
        }

    forced = (os.getenv("LLM_PROVIDER") or "").strip().lower()

    # 1) 显式覆盖优先级最高
    explicit_key = (os.getenv("LLM_API_KEY") or "").strip()
    if explicit_key:
        return {
            "provider": "custom",
            "base_url": (os.getenv("LLM_API_BASE") or "").strip() or _DEFAULT_BASE,
            "api_key": explicit_key,
            "model": (os.getenv("LLM_MODEL") or "").strip() or _DEFAULT_MODEL,
            "configured": True,
            "source": "LLM_API_KEY",
            "note": "显式配置",
        }

    # 2) LLM_PROVIDER 指定的渠道优先，其后按自动顺序补齐
    order = ([forced] if forced in _CHANNELS else []) + [c for c in _AUTO_ORDER if c != forced]

    for name in order:
        spec = _CHANNELS[name]
        for env_name in spec["env"]:
            key = (os.getenv(env_name) or "").strip()
            if key:
                return {
                    "provider": name,
                    "base_url": (os.getenv("LLM_API_BASE") or "").strip() or spec["base"],
                    "api_key": key,
                    "model": (os.getenv("LLM_MODEL") or "").strip() or spec["model"],
                    "configured": True,
                    "source": env_name,
                    "note": spec["note"],
                }

    # 3) 一个可用的密钥都没有 → 明确报告，交由上游走规则解析降级
    return {
        "provider": forced or None,
        "base_url": "",
        "api_key": "",
        "model": "",
        "configured": False,
        "source": None,
        "note": "未找到任何可用的 LLM 密钥（见 E:\\Program\\免费Token总账.md）",
    }


class LLMIntentParser:
    """调课意图解析器（多渠道路由）"""

    # 历史默认值，实际值在 __init__ 里按环境变量覆盖
    BASE_URL = _DEFAULT_BASE
    API_KEY = ""
    MODEL = _DEFAULT_MODEL

    SYSTEM_PROMPT = """你是一个高校物理实验排课系统的意图解析器。
你的任务：把教师用大白话表达的调课请求，解析为结构化的 JSON。

必须提取的信息：
- teacher: 教师姓名（若有，如"张三"）
- source_week: 原上课周次（数字，若提到"第六周""这周"等；无则 null）
- target_week: 目标周次（数字，若提到"下周""第5周"等；无则 null）
- target_weekday: 目标星期（如"周一"；无则 null）
- course_hint: 提到的课程线索（如"物理实验""那个课"；无则 null）
- needs_followup: 布尔值，信息是否不完整（缺少教师/源周/目标周任一核心信息时为 true）
- followup_questions: needs_followup 为 true 时的追问问题列表（中文，1-3个）

规则：
- 只提取明确存在的信息，不猜测
- "下周" = 源周 + 1（如果有源周）；没有源周时 target_week=null，needs_followup=true
- 输出必须是合法 JSON，不要输出其他内容"""

    def __init__(self):
        self._config = resolve_llm_config()
        self.provider = self._config["provider"]
        self.BASE_URL = self._config["base_url"] or _DEFAULT_BASE
        self.API_KEY = self._config["api_key"]
        self.MODEL = self._config["model"] or _DEFAULT_MODEL
        self.source = self._config["source"]
        self.configured = bool(self.API_KEY)

    def describe(self) -> dict:
        """渠道自述。**不含密钥**，可安全暴露给 `/status` 等诊断接口。"""
        return {
            "configured": self.configured,
            "provider": self.provider or "none",
            "model": self.MODEL,
            "endpoint": _endpoint_host(self.BASE_URL),
            "key_source": self.source,
            "note": self._config.get("note", ""),
        }

    def reload_config(self) -> dict:
        """重新按环境变量解析配置（测试 / 运行期热切换渠道用）。"""
        self.__init__()
        return self.describe()

    def _build_payload(self, text: str) -> dict:
        return {
            "model": self.MODEL,
            "messages": [
                {"role": "system", "content": self.SYSTEM_PROMPT},
                {"role": "user", "content": f"调课请求：{text}"},
            ],
            "temperature": 0.2,
            "max_tokens": 2048,
        }

    def parse(self, text: str) -> dict:
        """调用 LLM 解析意图，返回结构化 JSON"""
        if not self.configured:
            return {"error": "LLM 未配置（未找到可用的 API 密钥）", "needs_followup": False}

        try:
            with httpx.Client(timeout=60) as client:
                resp = client.post(
                    f"{self.BASE_URL}/chat/completions",
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {self.API_KEY}",
                    },
                    json=self._build_payload(text),
                )
                if resp.status_code != 200:
                    return {"error": f"LLM 调用失败：HTTP {resp.status_code}", "needs_followup": False}

                content = resp.json().get("choices", [{}])[0].get("message", {}).get("content", "")
                return self._extract_json(content)
        except Exception as e:
            return {"error": f"LLM 调用异常：{e!s}", "needs_followup": False}

    def _extract_json(self, content: str) -> dict:
        """从 LLM 输出中提取 JSON"""
        # 尝试直接解析
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            pass
        # 尝试提取 ```json ... ``` 块
        m = re.search(r"```json\s*(\{.*?\})\s*```", content, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(1))
            except json.JSONDecodeError:
                pass
        # 尝试提取第一个 { ... }
        m = re.search(r"\{.*\}", content, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass
        return {"error": "LLM 输出无法解析为 JSON", "needs_followup": False, "raw": content[:200]}

    def consult(self, question: str, context: str = "", role: str = "general") -> dict:
        """通用咨询方法（实验规范查询 / 通用问答复用，B 批次）

        role: 'experiment_tutor' 实验规范查询（面向排课人员/教师） / 'general' 通用
        context: 系统上下文（实验安排表知识源等）
        """
        if not self.configured:
            return {"error": "LLM 未配置"}

        role_prompts = {
            "experiment_tutor": """你是一个面向排课人员与教师的大学物理实验规范查询助手。
你的职责是辅助排课核对，而不是给学生答疑：回答实验项目的学时、所需场地、
操作步骤、数据处理与误差分析规范、实验报告规范等问题。
回答要求：
- 结论准确、条理清晰，公式正确（用 LaTeX 公式）、引用实验规范
- 覆盖仪器误差、系统误差、操作误差、环境误差等维度
- 完整回答不要中途截断，宁可详细
- 不确定的内容不要编造，明确说明""",
            "general": """你是一个物理实验教学智能助手，请专业、清晰地回答用户问题。""",
        }
        system_prompt = role_prompts.get(role, role_prompts["general"])

        user_content = question
        if context:
            user_content = f"{context}\n\n问题：{question}"

        try:
            with httpx.Client(timeout=60) as client:
                resp = client.post(
                    f"{self.BASE_URL}/chat/completions",
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {self.API_KEY}",
                    },
                    json={
                        "model": self.MODEL,
                        "messages": [
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": user_content},
                        ],
                        "temperature": 0.5,
                        "max_tokens": 4096,
                    },
                )
                if resp.status_code != 200:
                    return {"error": f"LLM 调用失败：HTTP {resp.status_code}"}
                content = resp.json().get("choices", [{}])[0].get("message", {}).get("content", "")
                return {"reply": content}
        except Exception as e:
            return {"error": f"LLM 调用异常：{e!s}"}

    def suggest_plans(self, teacher: str, course: str, target_week: int,
                      available_slots: list) -> dict:
        """B3: 用 LLM 从可用时段中生成多个候选方案（呼应"AI 多可能性"评审意见）

        输入：教师、课程、目标周次、可用时段列表（可能很多条）
        输出：2-3 个精选候选方案，每个含时段、教室、推荐理由
        """
        if not self.configured:
            return {"error": "LLM 未配置"}

        # 压缩可用时段：每星期取前2个，避免 token 爆炸
        from collections import defaultdict
        by_day = defaultdict(list)
        for s in available_slots[:40]:
            by_day[s["weekday"]].append(f"{s['time_slot']} {s['room']}")
        slot_summary = "\n".join(
            f"{wd}: {', '.join(items[:2])}" for wd, items in by_day.items()
        )

        prompt = f"""你是高校物理实验排课专家。教师「{teacher}」想把「{course}」调到第{target_week}周。
以下是系统筛选出的可用时段（星期 时段 教室）：

{slot_summary}

请从这些时段中挑选 2-3 个最合理的候选方案，考虑：与原课时间接近、教室类型匹配、教师/学生便利性。
输出 JSON（不要其他内容）：
{{
  "plans": [
    {{
      "weekday": "周一", "time_slot": "567", "room": "A410",
      "reason": "与原周四下午课时段接近，教室类型匹配"
    }}
  ]
}}"""

        try:
            with httpx.Client(timeout=60) as client:
                resp = client.post(
                    f"{self.BASE_URL}/chat/completions",
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {self.API_KEY}",
                    },
                    json={
                        "model": self.MODEL,
                        "messages": [{"role": "user", "content": prompt}],
                        "temperature": 0.3,
                        "max_tokens": 2048,
                    },
                )
                if resp.status_code != 200:
                    return {"error": f"LLM 调用失败：HTTP {resp.status_code}"}

                content = resp.json().get("choices", [{}])[0].get("message", {}).get("content", "")
                result = self._extract_json(content)
                if "plans" in result:
                    return {"plans": result["plans"]}
                return {"error": "LLM 未返回候选方案", "raw": content[:150]}
        except Exception as e:
            return {"error": f"LLM 调用异常：{e!s}"}
