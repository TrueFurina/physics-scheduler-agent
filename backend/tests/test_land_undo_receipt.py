"""撤销回执：撤掉的是哪一次、撤销之后还剩什么（R73）。

为什么要这一层
--------------
R60 给 `/parallel/land` 补了 `landing_receipt`，但 `/parallel/land/undo` 没有对称
的东西：成功时只回字段，界面自己拼一句"已撤销落地，安排表恢复到 N 条"。

实测（连续落地 A、B，再撤销一次）暴露三个缺口：

1. 不说撤的是哪一次 —— 响应里明明有 `landed_at` / `landed_actor`，界面一个没念；
2. 不说撤销后**还有更早一次落地在生效** —— 撤销只弹一条记录，实测撤销后
   `GET /parallel/land/status` 依旧 `landed=True`，面板下半部分照旧显示
   "当前安排表已是落地方案"。于是同一屏横幅说"已撤销"、状态栏说"已落地"，
   两句都真，读起来像 bug，教秘会以为已经回到原始状态；
3. 不说回滚掉的调课要重新审批 —— 那句是前端硬编码的，字段一改名就静默消失。

这里全部是纯函数用例（毫秒级，不碰库、不碰 HTTP）：接线由
`test_landing_endpoints.py` 的 R73 两条守。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import plan_landing


def _out(**kw):
    """撤销成功时 `plan_landing.undo` 的真实返回形态（缺省值照它来）。"""
    body = {
        "success": True,
        "revoked": True,
        "landed_at": "2026-11-02T20:22:52.758902",
        "landed_actor": "admin",
        "restored_entries": 2,
        "rolled_back_moves": 0,
        "revoked_id": 2,
        "revoked_at": "2026-11-02T20:22:53.001234",
        "revoked_by": "admin",
    }
    body.update(kw)
    return body


def _remaining(**kw):
    """库里**还留着**的更早那条落地（`LandingStore.latest()` 的返回）。"""
    body = {
        "id": 1,
        "landed_at": "2026-11-02T20:22:52.745900",
        "actor": "admin",
        "note": "",
        "after_entries": 2,
    }
    body.update(kw)
    return body


def test_receipt_names_which_landing_was_undone():
    """首句必须点名"撤的是哪一次、谁落的" —— 落地两次之后这是唯一能核对的信息。"""
    msg = plan_landing.undo_receipt(_out())

    assert msg.startswith("↩️ 已撤销 2026-11-02 20:22:52 那次落地"), msg
    assert "由 admin 落地" in msg, msg
    # 时间戳里的 T 必须换成人话里的空格，否则界面上一串 ISO 原文
    assert "T20:22" not in msg, msg


def test_receipt_says_table_is_back_to_pre_landing_when_nothing_left():
    """没有更早的落地时，"已回到落地前"才可以说 —— 这是最省事的假话来源。"""
    msg = plan_landing.undo_receipt(_out())

    assert "已回到落地前的状态（现 2 条）" in msg, msg
    assert "更早" not in msg, f"没有更早的落地就不该提它：{msg}"


def test_receipt_admits_earlier_landing_still_active():
    """还有更早一次落地在生效时必须明说，且**不许**说"已回到落地前"。

    实测：撤销第二次落地之后 `/land/status` 仍报 `landed=True`。
    回执若写"已回到落地前"，横幅与状态栏就在同一屏上互相打脸。
    """
    msg = plan_landing.undo_receipt(_out(), remaining=_remaining())

    assert "更早的一次落地" in msg, msg
    assert "2026-11-02 20:22:52" in msg, msg
    assert "仍然生效" in msg, msg
    assert "再撤一次" in msg, f"要告诉教秘怎么回到更早的状态：{msg}"
    assert "已回到落地前" not in msg, f"更早的落地还在生效，这句话是假的：{msg}"
    assert "不是原始状态" in msg, msg


def test_receipt_reports_rolled_back_moves():
    """落地后生效的调课被一并回滚 —— 这是撤销唯一的静默数据变更，必须点名。"""
    msg = plan_landing.undo_receipt(_out(rolled_back_moves=3))

    assert "一并回滚了落地后生效的 3 条调课" in msg, msg
    assert "重新审批" in msg, msg
    assert "不会自动重放" in msg, f"要写清「这些申请没有自动补回」：{msg}"


def test_receipt_stays_silent_when_no_move_rolled_back():
    """0 条时不许出现"回滚了 0 条调课"——那是噪声，还会让人以为出过事。"""
    msg = plan_landing.undo_receipt(_out(rolled_back_moves=0))

    assert "调课" not in msg, msg


def test_receipt_says_actor_missing_instead_of_leaving_a_hole():
    """老记录没有操作人（R55 之前落的）：要写"没有留操作人"，不能留个空档。"""
    msg = plan_landing.undo_receipt(_out(landed_actor=""))

    assert "该记录没有留操作人" in msg, msg
    assert "由  落地" not in msg and "由 落地" not in msg, msg


def test_receipt_degrades_when_landed_at_missing():
    """时间戳缺失时不写"已撤销  那次落地"，退成"最近一次落地"。"""
    msg = plan_landing.undo_receipt(_out(landed_at=""))

    assert "已撤销最近一次落地" in msg, msg
    assert "那次落地" not in msg, msg


def test_receipt_survives_sparse_remaining_record():
    """更早那条记录本身也缺字段时不能崩，且要如实说"未记录操作人"。"""
    msg = plan_landing.undo_receipt(_out(), remaining=_remaining(landed_at="", actor=""))

    assert "更早的一次落地" in msg, msg
    assert "未记录操作人" in msg, msg
    assert "时刻未记录" in msg, msg


def test_receipt_skips_clock_line_without_revoked_at():
    """没有撤销时间就不画时钟行 —— 空时间戳挂个 🕒 比不写更糟。"""
    msg = plan_landing.undo_receipt(_out(revoked_at="", revoked_by=""))

    assert "🕒" not in msg, msg
    # 落地那行的时间还在，别把两件事混起来
    assert "2026-11-02 20:22:52" in msg, msg
