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

# 解析缓存写到系统临时目录：否则测试会把 schedule-*.json 落进仓库
# `backend/app/data/cache/`（而该目录会随发布包上传）。
os.environ.setdefault(
    "PARSE_CACHE_DIR", os.path.join(tempfile.gettempdir(), "physics-scheduler-test-cache")
)
