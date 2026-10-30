"""AI 候选方案：降级产物必须说清来源（R64）。

缺陷
----
`process_request` 里：

```python
try:
    plans_result = self.llm_parser.suggest_plans(...)
    ai_plans = plans_result.get("plans", [])
except Exception:
    ai_plans = []                    # ← LLM 异常

if not ai_plans:                     # ← 异常和"LLM 返回空"合并
    seen_days = set()
    for s in slots:
        if s["weekday"] not in seen_days:
            seen_days.add(s["weekday"])
            ai_plans.append({
                "weekday": ..., "time_slot": ..., "room": ...,
                "reason": "系统推荐：该时段教室空闲",
            })
```

实测（`LLM_DISABLED=1`，即 LLM 不可用）：

```
[候选数] 3
[有无来源标记] False
   reason='系统推荐：该时段教室空闲'
```

前端把它显示成「🤖 推荐候选方案」—— **教秘会以为这是 AI 分析过的结果**，
实际只是"按星期去重挑了前三个空档"。

### 为什么这算问题

PRD 与演示脚本承诺的是「AI 候选方案」。规则降级本身是好事（LLM 挂了
不该让沙盘没候选），但**降级的产物冒充 AI 产物**，就让这个承诺失真了。

这属于同一族「系统说的和真实情况不一致」，且**恰好是最容易被展示给
评审/领导看的那一块**。

### 修法

- 降级候选带 `source: "rule"`，LLM 候选带 `source: "llm"`
- 响应加 `ai_plans_source` 字段（`llm` / `rule` / `none`）
- 前端文案按来源区分：「🤖 AI 推荐」vs「📋 系统推荐（AI 暂不可用）」

注意：**不要改成"LLM 挂了就不给候选"** —— 那是另一种失真（少给东西）。
给，但说清是谁给的。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.agent.reschedule_agent import RescheduleAgent
from app.utils.excel_parser import ScheduleDatabase

REAL = os.path.join(os.path.dirname(__file__), "..", "..", "schedule.demo.xlsx")


@pytest.fixture(scope="module")
def agent():
    if not os.path.isfile(REAL):
        pytest.skip("缺少 schedule.demo.xlsx")
    db = ScheduleDatabase()
    db.load_from_excel(REAL)
    return RescheduleAgent(db)


def test_rule_fallback_plans_are_labelled(agent, monkeypatch):
    """LLM 不可用时，候选必须标明来源是 rule。"""
    # 确保走降级路径
    if getattr(agent, "llm_parser", None) is not None:
        monkeypatch.setattr(agent.llm_parser, "configured", False, raising=False)

    r = agent.process_request("我是吴琳，把声速测量调到第3周", with_ai=True)

    plans = r.get("ai_plans") or []
    assert plans, "降级后应当仍有候选（不能因为 LLM 挂了就不给）"

    assert r.get("ai_plans_source") == "rule", (
        f"候选来源没标出来（ai_plans_source={r.get('ai_plans_source')!r}）—— "
        "前端会把规则产物当 AI 产物显示"
    )
    for p in plans:
        assert p.get("source") == "rule", (
            f"候选没标 source：{p} —— 逐条也应有来源，界面可以逐条区分"
        )


def test_rule_plans_have_reason(agent, monkeypatch):
    """降级候选必须有可读的 reason（不能只有三个坐标）。"""
    if getattr(agent, "llm_parser", None) is not None:
        monkeypatch.setattr(agent.llm_parser, "configured", False, raising=False)

    r = agent.process_request("我是吴琳，把声速测量调到第3周", with_ai=True)
    for p in r.get("ai_plans") or []:
        assert (p.get("reason") or "").strip(), f"候选缺 reason：{p}"


def test_no_plans_reports_none_source(agent, monkeypatch):
    """一个候选都没有时，来源应是 none 而不是空字符串。"""
    # 造一个没有可用时段的周（用一个不存在的教师）
    r = agent.process_request("我是不存在的老师，把课调到第3周", with_ai=True)
    plans = r.get("ai_plans") or []
    if plans:
        pytest.skip("该场景仍有候选，跳过")
    assert r.get("ai_plans_source") == "none", (
        f"没有候选却没报 none：{r.get('ai_plans_source')!r}"
    )
