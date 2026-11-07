"""pytest 全局约定。

限流默认关闭
------------
本套件有大量请求走 `/admin-login`、`/chat` 等受限接口。若按真实阈值计数，
它们会在同一个 60 秒窗口、同一个 `testclient` 源 IP 上互相挤爆，
表现为"单独跑通过、全量跑随机 429"——这是最讨厌的一类假失败。

所以全局默认 `RATELIMIT_DISABLED=1`；限流本身的正确性由
`test_ratelimit.py` 显式打开后单独验证（见该文件里的 monkeypatch.delenv）。

LLM 默认关闭
------------
`backend/app/__init__.py` 会把仓库根 `.env` 灌进环境变量，所以测试进程里
**通常是有真实 LLM 密钥的**。而 `RescheduleAgent.process_request` 在"规则解析
拿不到周次"时会**降级调 LLM**，多个用例都会走到这条路径——于是整套测试变成
真的打外部 API：慢（单次约 5 秒）、有额度成本、且依赖网络（CI 上会随机失败）。

所以全局默认 `LLM_DISABLED=1`，让这些路径稳定走规则降级；
LLM 渠道解析本身的正确性由 `test_llm_config.py` 显式构造环境后单独验证。
"""

import os
import sys
import tempfile

# 让 `import app.*` 在任何执行目录下都能工作（与各测试文件里的 sys.path 插入等价）
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("RATELIMIT_DISABLED", "1")
os.environ.setdefault("LLM_DISABLED", "1")
# 预热（CP-SAT / 导出缓存）默认关闭：它们是**后台 daemon 线程**，会在测试中途
# 读写 `_db` / `_master_data` —— 而测试大量 monkeypatch 这两个全局。开着预热就等于
# 在用例背后跑一个看不见的线程，失败会表现为"随机红、单独跑又绿"（最难查的一类）。
os.environ.setdefault("SCHEDULE_WARMUP", "0")
# 默认管理员口令只在演示模式有效（生产未配 ADMIN_PASS 时拒绝，见 security.py）。
# 测试用演示口令登审批台，所以测试进程统一按演示模式跑。
os.environ.setdefault("DEMO_MODE", "1")
# ⚠️ R-123：ADMIN_PASS 必须在**模块级**设 —— 这是唯一先于所有测试模块执行的位置。
#
# `app/security.py` 在**导入时**读 ADMIN_PASS 并固化（未设则随机生成、且不打印）；
# 而几十个测试文件在模块级 `from app... import`，其中只有 2 个自己 setdefault。
# pytest 按文件名字母序收集，只要某个"不设口令"的文件先被导入，
# security.ADMIN_PASS 就固化成随机值，之后 test_api_auth 用 admin123 登录 → 401。
#
# 本地有 `.env` 提供 ADMIN_PASS，所以从不暴露；**CI 干净检出没有 `.env`，
# 这一条会随收集顺序随机挂**。实测干净树：2 failed / 810 passed / 31 skipped，
# 而同一份代码本地全绿 —— 典型的"本地过、CI 挂"。
os.environ.setdefault("ADMIN_PASS", "admin123")



# 解析缓存写到系统临时目录：否则测试会把 schedule-*.json 落进仓库
# `backend/app/data/cache/`（而该目录会随发布包上传）。
os.environ.setdefault(
    "PARSE_CACHE_DIR", os.path.join(tempfile.gettempdir(), "physics-scheduler-test-cache")
)


import pytest

# ---------------------------------------------------------------------------
# 临时文件落在**专用的空目录**里，不写系统 TEMP（R-122 / R-127）
# ---------------------------------------------------------------------------
# 本机 TEMP 实测 48805 个顶层条目 / 115032 个文件（别的程序的残留，遍历一遍 70.5s）。
# 而本机 agent 给 `os.remove` 挂了 audit 钩子，删除前做"保留扫描"（走 socket），
# 规模一大就超时 → `PermissionError: CHAT_REFERENCE_PROTECTED`。
#
# 谁受害：openpyxl 每写一个 worksheet 都建临时 xml 再删它（`_writer.py:389`），
# 于是任意"导出/工作簿/导入预览"测试都可能随机红，报的还是 PermissionError，
# 完全看不出根因。
#
# ⚠️ R-122 时这个修复只加在 `scripts/run_acceptance.py` 里 —— 那是**验收脚本的入口**，
# 但人们更常直接 `pytest backend/tests`。所以它在最常见的用法下**没生效**，
# 本轮又红了一次（`test_full_table_exports_stay_open_by_default[master-data]`）。
# conftest 是 pytest 的唯一入口，放这里才能覆盖所有跑法。
#
# 用**专用子目录**而非 `.tmp/` 本身：后者已积累 286 个条目 / 27MB 的历史残留，
# 扫描规模仍然偏大。（那 286 个条目不归本任务清理，不动。）
_TMP_FOR_TESTS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".tmp", "pytest-tmp"
)
os.makedirs(_TMP_FOR_TESTS, exist_ok=True)
tempfile.tempdir = _TMP_FOR_TESTS
for _k in ("TMPDIR", "TEMP", "TMP"):
    os.environ.setdefault(_k, _TMP_FOR_TESTS)

from app import master_store as _master_store  # noqa: E402 —— 必须在上面的 os.environ 设好之后导入，否则读到旧配置


# ⚠️ R-123：上面这些 `setdefault` **只在模块导入时生效一次**，不足以保证全程有效。
#
# 实测机制（2026-11-07，`_pytest.monkeypatch.MonkeyPatch().setenv(...).undo()`）：
# 若变量**原本不存在**，`setenv` 还原时执行的是 `delenv` —— 于是"设过又还原"等于删掉。
# 具体踩坑路径：
#
#   conftest 导入        setdefault("DEMO_MODE", "1")        → DEMO_MODE="1"
#   test_admin_password_guard::test_default_password_allowed_in_demo
#                        monkeypatch.setenv("DEMO_MODE","1")  → 记录"原本不存在"
#     ...用例结束        monkeypatch 还原                    → **DEMO_MODE 被删除**
#   之后的 test_api_auth::test_admin_login_issues_working_token
#                        is_demo_mode() → False → admin123 被拒 → 401
#
# 本地有 `.env` 提供 DEMO_MODE，所以 `setdefault` 不是"原本不存在"，永远不触发；
# **CI 的干净检出没有 `.env`，这一条必挂**。实测：干净树 `2 failed, 810 passed,
# 31 skipped`，本地全绿 —— 典型的"本地过、CI 挂"。
#
# 修法不是改那一行 `setdefault`，而是每个用例前都兜一次：环境类前置条件
# 应当 per-test 保证，不能依赖"导入时设过一次"。
_BASELINE_ENV = {
    # ⚠️ R-123：ADMIN_PASS 必须在这里设 —— 这是唯一"先于所有测试模块导入"的位置。
    #
    # `app/security.py` 在**导入时**读 ADMIN_PASS 并固化（未设则随机生成、且不打印）。
    # 而几十个测试文件在模块级 `from app... import`，其中只有 2 个自己 setdefault。
    # pytest 按文件名字母序收集 ⇒ 只要某个"不设口令"的文件先被导入，
    # security.ADMIN_PASS 就固化成随机值，之后 test_api_auth 用 admin123 登录 → 401。
    #
    # 本地有 `.env` 提供 ADMIN_PASS，所以从不暴露；**CI 干净检出没有 `.env`，
    # 这一条会随收集顺序随机挂**。实测干净树：2 failed / 810 passed / 31 skipped。
    "ADMIN_PASS": "admin123",
    "DEMO_MODE": "1",
    "RATELIMIT_DISABLED": "1",
    "LLM_DISABLED": "1",
    "SCHEDULE_WARMUP": "0",
    # 也纳入基线：它被 11 个用例 setenv/delenv 过。若某用例删掉它，
    # 解析缓存会回落到默认目录 —— 那正是「测试把 schedule-*.json 写进
    # backend/app/data/cache/ 并随发布包上传」的老问题。
    "PARSE_CACHE_DIR": os.path.join(tempfile.gettempdir(), "physics-scheduler-test-cache"),
}


@pytest.fixture(autouse=True)
def _baseline_env():
    """每个用例前恢复基线环境变量。

    只填**缺失的**，不覆盖用例显式设的值 —— 所以 `test_admin_password_guard`
    里 `delenv("DEMO_MODE")` 的用例仍然照常工作（它在用例体内删，fixture 只负责
    用例**开始**时是齐的）。
    """
    for k, v in _BASELINE_ENV.items():
        os.environ.setdefault(k, v)


@pytest.fixture(autouse=True)
def _isolate_master_store(tmp_path, monkeypatch):
    """所有测试的主数据落盘一律重定向到临时目录。

    不加这层时，个别触发 `_persist_master_state` 的用例会把 v2 落盘写进真实
    `backend/app/data/master_store.json`（实测发生过：全量跑完发现落盘文件
    mtime 刚好是 pytest 时间）—— 内容虽然等于当前基线所以无害，但测试绝不该
    有写真实数据目录的副作用。
    """
    monkeypatch.setattr(
        _master_store, "STORE_PATH", str(tmp_path / "master_store.json")
    )


# ---------------------------------------------------------------------------
# 关于「全量跑偶发 1 failed」—— 不是代码问题，不要为它改测试
# ---------------------------------------------------------------------------
# 2026-11-07 实测四次全量（同一份代码）：
#
#     832 passed
#     1 failed  test_api_auth::test_upload_keeps_old_data_when_parse_fails
#     1 failed  test_master_workbook::test_blank_cells_do_not_overwrite_existing_values
#     1 failed  test_api_auth::test_full_table_exports_stay_open_by_default[...]
#
# 三个不同的测试、同一个根因。traceback 落在：
#
#     openpyxl/worksheet/_writer.py:389  in cleanup
#         os.remove(self.out)          ← openpyxl 删自己写的临时 xml
#     <string>:56  in _audit           ← 本机 agent 运行时注入的 sys.addaudithook
#         PermissionError: CHAT_REFERENCE_PROTECTED
#
# openpyxl 每写一个 worksheet 都"建临时文件 → 写 → 删"。钩子在删除前做
# "引用保留扫描"（走 socket 与运行时通信），**长时间跑（13 分钟以上）后该通信超时**，
# 于是拒绝删除并把 PermissionError 抛进业务代码。
#
# 所以：**触发点是"任意一个会写 xlsx 的测试"，与具体用例无关**，也与
# `PYTEST_DEBUG_TEMPROOT` 无关（换过 temproot 仍复现）。CI 上不存在该钩子。
#
# 判定方法：重跑一次全量，若失败换了测试名且 traceback 在
# `openpyxl ... cleanup → os.remove`，即为环境问题。
#
# 已完成的反证：逐个跑所有写 xlsx 的测试文件（test_api_auth / test_master_workbook /
# test_export_empty / test_auto_schedule_all / test_course_bridge / test_import_wizard），
# **153 项全绿**——包括三次全量里各自失败的那几个。
#
# 不要为此加 `pytest.mark.flaky` 或 try/except：那会把真正的 openpyxl 写入错误
# 一起吞掉，而"导出写坏文件"是本项目最该被看见的故障之一。
