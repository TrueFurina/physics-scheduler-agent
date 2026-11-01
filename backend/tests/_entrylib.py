"""测试专用 ScheduleEntry 构造器（**唯一实现**）。

为什么有这个模块（R55，2026-11-02）
--------------------------------
`backend/tests/` 里曾有 **16 个**同名 `_entry*` 辅助函数，AST 解析后发现它们实为 **11 种签名**
（参数顺序不同 4 种、默认值不同 1 种、多带参数 3 种、参数名不同 1 种、派生名 1 种）。
它们**不是 pytest 夹具** —— 16 个定义的装饰器数量都是 0，能工作只因"定义在同一个测试文件里"。

本模块只收编**逐字相同**的那 5 份（`test_graybox` / `test_import_wizard` / `test_master_data` /
`test_master_persist` / `test_master_workbook`，函数体 sha1 一致）。其余 11 份**保持原样**，因为
把签名统一到主流版本会让 **39 处位置传参静默错位**：`test_data_audit.py` 的 `_entry` 根本没有
`week` 参数（第一个是 `weekday`），整体错位一格；`test_auto_schedule.py` 那 17 处则是
`room→group`、`teacher→room` 互换。而这些用例多只断言数量与集合存在性，**错位后tests仍然全绿**
—— 正是本项目反复出现的形状：全绿 ≠ 没出事。

所以这里的边界是"**只合并逐字相同的**"，不是"统一所有 `_entry`"。要统一，前置条件是先把
142 处调用点全部关键字化，那是另一轮的事。
"""
import os
import sys

# 与各测试文件同款：让 `from app...` 在 pytest prepend 模式下可用。
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.utils.excel_parser import ScheduleEntry


def _entry(week, weekday, slot, cls, group, room, teacher="吴老师", experiment="扭摆法"):
    """位置参数顺序是**本模块的契约**：`(week, weekday, slot, cls, group, room, teacher, experiment)`。

    ⚠️ 注意构造 `ScheduleEntry` 时 `experiment` 与 `room` 的位置是反的（历史签名如此），
    改这一行等于改所有调用方的语义。
    """
    return ScheduleEntry(week, weekday, slot, cls, group, experiment, room, teacher)
