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
# 默认管理员口令只在演示模式有效（生产未配 ADMIN_PASS 时拒绝，见 security.py）。
# 测试用演示口令登审批台，所以测试进程统一按演示模式跑。
os.environ.setdefault("DEMO_MODE", "1")

# 解析缓存写到系统临时目录：否则测试会把 schedule-*.json 落进仓库
# `backend/app/data/cache/`（而该目录会随发布包上传）。
os.environ.setdefault(
    "PARSE_CACHE_DIR", os.path.join(tempfile.gettempdir(), "physics-scheduler-test-cache")
)


import pytest

from app import master_store as _master_store


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
