"""R100：/experiment-tutor 知识源「部分为空」也必须结构化降级。

动手前只读探针（真实 demo 表 433 条）：
  - 实验项目 433 / 实验室 417 / 实验教师 433 ⇒ 三维齐全，gaps=[]，
    成功路径逐字节不变（不会「狼来了」）。
  - R95 的 `_knowledge_source_is_empty()` 只看实验项目一维；
    `_build_knowledge_context()` 却把「实验室（0间）」「实验教师（0位）」
    一起塞进提示词。所以表里有实验名、但场地/教师整列为空时，
    模型拿到的是一份明写「0 间 / 0 位」的残知识源，回答会据此泛化，
    而响应仍是 success=True + data={} —— 与数据齐全时逐字节相同。
  - 与 R35「没填场地不计入冲突、但必须报出来」同族：
    「没查到」必须说出口，不能让模型替用户默认。

判据只看结构化事实（每条记录的属性），绝不猜 context 的中文文案。
"""

import asyncio
import inspect
from types import SimpleNamespace

import app.api.reschedule as api

REPLY = "（模型作答）"
QUESTION = "声速测量实验的数据处理规范是什么？"


def _entry(experiment="声速测量", room="物理楼201", teacher="吴琳"):
    return SimpleNamespace(experiment=experiment, room=room, teacher=teacher)


def _call(monkeypatch, *, loaded=True, entries, consult_result=None, fake_context=None):
    """装配场景并直接调 async 端点；全部经 monkeypatch 自动还原。"""
    monkeypatch.setattr(api._db, "loaded", loaded)
    monkeypatch.setattr(api._db, "entries", entries)
    # 不让自愈去读真实 Excel（沙箱里可能成功、可能失败，都会覆盖判据用的状态）
    monkeypatch.setattr(api, "_ensure_loaded_sync", lambda: None)
    seen = {}

    def fake_consult(question, context="", role="general"):
        seen["context"] = context
        seen["role"] = role
        return consult_result if consult_result is not None else {"reply": REPLY}

    monkeypatch.setattr(api._agent.llm_parser, "consult", fake_consult)
    if fake_context is not None:
        monkeypatch.setattr(api, "_build_knowledge_context", lambda: fake_context)
    resp = asyncio.run(api.experiment_tutor_consult(api.ConsultRequest(question=QUESTION)))
    return resp, seen


def test_partial_gaps_are_structurally_reported(monkeypatch):
    """有实验名但场地/教师整列为空 ⇒ degraded + gaps + warnings，success 不翻转。"""
    resp, seen = _call(monkeypatch, entries=[_entry(room="", teacher="")])
    assert "0间" in seen["context"], "前置条件：提示词里确实写着 0 间"
    assert "0位" in seen["context"], "前置条件：提示词里确实写着 0 位"
    assert resp.success is True
    assert resp.reply == REPLY, "降级不能把已经拿到的回答丢掉"
    assert resp.data.get("degraded") is True
    assert resp.data.get("knowledge_context_empty") is False
    assert resp.data.get("knowledge_gaps") == ["实验室", "实验教师"]
    warnings = resp.data.get("warnings")
    assert isinstance(warnings, list) and warnings
    assert any("实验室" in w for w in warnings)
    assert "缺少" in resp.message, "人话 message 里也要看得见（不只是 data）"


def test_single_gap_is_not_blown_up(monkeypatch):
    """只缺一维就只报一维，不许把另一维也算进去。"""
    resp, _ = _call(monkeypatch, entries=[_entry(room="")])
    assert resp.data.get("knowledge_gaps") == ["实验室"]
    assert resp.data.get("knowledge_context_empty") is False
    assert resp.data.get("degraded") is True


def test_healthy_source_is_byte_identical(monkeypatch):
    """三维齐全 ⇒ 响应与 R100 之前逐字节相同 —— 成功路径不许被改。"""
    resp, seen = _call(monkeypatch, entries=[_entry()])
    assert "声速测量" in seen["context"]
    assert resp.model_dump() == {
        "success": True,
        "message": "实验规范查询完成",
        "reply": REPLY,
        "data": {},
    }


def test_empty_source_keeps_the_R95_signal(monkeypatch):
    """未加载 ⇒ 仍走 R95 全空分支，gaps 不抢戏、不重复报。"""
    resp, _ = _call(monkeypatch, loaded=False, entries=[])
    assert resp.data.get("degraded") is True
    assert resp.data.get("knowledge_context_empty") is True
    assert resp.data.get("knowledge_gaps") == []
    assert "知识源为空" in resp.message


def test_zero_entry_shell_keeps_the_R95_signal(monkeypatch):
    """R27 兜底后的 0 条目空壳同样归 R95，不许被误认成「只缺两维」。"""
    resp, _ = _call(monkeypatch, loaded=True, entries=[])
    assert resp.data.get("knowledge_context_empty") is True
    assert resp.data.get("knowledge_gaps") == []
    assert resp.success is True


def test_fake_context_text_cannot_whitewash_gaps(monkeypatch):
    """防放宽：把 context 伪装成三维齐全的文案，结构化事实仍缺时照样报。"""
    fake = ("本系统实验安排表知识源：\n- 实验项目（1个）：声速测量\n"
            "- 实验室（3间）：A504、A516\n- 实验教师（2位）：吴琳、林珠云\n")
    resp, _ = _call(monkeypatch, entries=[_entry(room="", teacher="")], fake_context=fake)
    assert resp.data.get("knowledge_gaps") == ["实验室", "实验教师"]
    assert resp.data.get("degraded") is True


def test_llm_error_branch_is_unchanged(monkeypatch):
    """LLM 失败优先：不许把 error 改写成 success=True 的降级成功。"""
    resp, _ = _call(monkeypatch, entries=[_entry(room="", teacher="")],
                    consult_result={"error": "LLM 未配置"})
    assert resp.success is False
    assert resp.message == "LLM 未配置"
    assert resp.data == {}


def test_partial_gaps_http_exposes_data(monkeypatch):
    """端到端（规则 24）：POST /experiment-tutor 的 data 真带信号，没被 response_model 吃掉。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    monkeypatch.setattr(api._db, "loaded", True)
    monkeypatch.setattr(api._db, "entries", [_entry(room="", teacher="")])
    monkeypatch.setattr(api, "_ensure_loaded_sync", lambda: None)
    monkeypatch.setattr(api._agent.llm_parser, "consult",
                        lambda *a, **k: {"reply": REPLY})
    app = FastAPI()
    app.include_router(api.router)
    resp = TestClient(app).post("/experiment-tutor", json={"question": QUESTION})
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert body["reply"] == REPLY
    assert body["data"]["degraded"] is True
    assert body["data"]["knowledge_context_empty"] is False
    assert body["data"]["knowledge_gaps"] == ["实验室", "实验教师"]
    assert body["data"]["warnings"]


def test_gaps_criterion_reads_structured_facts(monkeypatch):
    """删型变异守卫：判据必须显式逐维度扫描结构化事实（规则 27）。"""
    src = inspect.getsource(api._knowledge_source_gaps)
    assert "any(" in src
    assert "room" in src and "teacher" in src
    assert "_db.entries" in src
