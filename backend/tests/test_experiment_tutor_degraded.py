"""R95：/experiment-tutor 知识源为空时必须结构化降级，不能"答得泛但不报"。

背景（只读探针实测，2026-11-10）：
  - 路径 A（自愈失败，_db.loaded=False）：_build_knowledge_context() 返回 ""，
    端点照样回 success=True / message="实验规范查询完成" / data={}。
  - 路径 B（更隐蔽）：_ensure_loaded_sync() 的 R27 兜底会把 loaded 置成 True，
    entries 为空时 context 是一段 48 字节的"实验项目（0个）"非空空壳 ——
    单判 `if not context` 会安静漏报。

判据必须看结构化事实（_db.loaded / 条目里有没有 experiment），
不能去猜 context 的中文文案，否则改个措辞就能把守卫洗白。
"""

import asyncio
from types import SimpleNamespace

import app.api.reschedule as api

REPLY = "（模型作答）"
QUESTION = "声速测量实验的数据处理规范是什么？"


def _entry(experiment="声速测量", room="物理楼201", teacher="吴琳"):
    return SimpleNamespace(experiment=experiment, room=room, teacher=teacher)


def _call(monkeypatch, *, loaded, entries, consult_result=None, fake_context=None):
    """在内存里装配场景并直接调 async 端点；全部经 monkeypatch 自动还原。"""
    monkeypatch.setattr(api._db, "loaded", loaded)
    monkeypatch.setattr(api._db, "entries", entries)
    # 不让自愈去读真实 Excel（沙箱里可能成功、可能失败，都会污染判据）
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


def test_empty_knowledge_source_is_structurally_degraded(monkeypatch):
    """路径 A：未加载 ⇒ 信号随响应回去，且 success 不因局部降级翻转（R86 约定）。"""
    resp, _ = _call(monkeypatch, loaded=False, entries=[])
    assert resp.success is True
    assert resp.reply == REPLY, "降级不能把已经拿到的回答丢掉"
    assert resp.data.get("degraded") is True
    assert resp.data.get("knowledge_context_empty") is True
    warnings = resp.data.get("warnings")
    assert isinstance(warnings, list) and warnings
    assert "知识源为空" in resp.message, "人话 message 里也要看得见（不只是 data）"


def test_zero_entry_shell_is_not_missed(monkeypatch):
    """路径 B：loaded=True 但 entries 空 ⇒ context 非空（48 字节"0个"空壳），仍须置位。

    这条专门防"只判 if not context"的假阴性：改回字符串判空就会红。
    """
    resp, seen = _call(monkeypatch, loaded=True, entries=[])
    assert seen["context"].strip(), "前置条件：context 确实是非空空壳"
    assert resp.data.get("degraded") is True
    assert resp.data.get("knowledge_context_empty") is True
    assert resp.success is True


def test_entries_without_experiment_are_also_empty(monkeypatch):
    """有条目但全都没有实验名 ⇒ 知识源依然是空的（不能按 len(entries) 判定）。"""
    resp, _ = _call(monkeypatch, loaded=True, entries=[_entry(experiment="")])
    assert resp.data.get("knowledge_context_empty") is True
    assert resp.data.get("degraded") is True


def test_healthy_knowledge_source_is_byte_identical(monkeypatch):
    """有本校数据时，响应必须与 R95 之前逐字节相同 —— 成功路径不许被改。"""
    resp, seen = _call(monkeypatch, loaded=True, entries=[_entry()])
    assert "声速测量" in seen["context"]
    assert resp.model_dump() == {
        "success": True,
        "message": "实验规范查询完成",
        "reply": REPLY,
        "data": {},
    }


def test_llm_error_branch_is_unchanged(monkeypatch):
    """LLM 失败走原有 error 分支，不因知识源为空被改写成 success=True 降级。"""
    resp, _ = _call(
        monkeypatch, loaded=False, entries=[],
        consult_result={"error": "LLM 未配置"},
    )
    assert resp.success is False
    assert resp.message == "LLM 未配置"
    assert resp.data == {}


def test_fake_context_text_cannot_whitewash(monkeypatch):
    """防放宽：把 context 伪装成一大段有实验名的文案，结构化事实仍为空时必须照样报。

    判据若偷懒去看 context 文案（或 `if not context`），这条就会红。
    """
    fake = "本系统实验安排表知识源：\n- 实验项目（3个）：声速测量、杨氏模量、密立根油滴\n"
    resp, _ = _call(monkeypatch, loaded=True, entries=[], fake_context=fake)
    assert resp.data.get("degraded") is True
    assert resp.data.get("knowledge_context_empty") is True
