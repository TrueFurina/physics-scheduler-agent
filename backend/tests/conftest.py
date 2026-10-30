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

# ⚠️ ADMIN_PASS 必须在这里设，不能只放在 test_api_auth.py 里。
#
# `security.py` 在**模块导入时**就把 `ADMIN_PASS` 读进模块级常量
# （`_ADMIN_PASS_RAW = os.getenv("ADMIN_PASS")`）。谁先导入 `app.security`，
# 谁就决定了这个常量。
#
# 实测全量跑时的失败：pytest 按文件名排序，`test_admin_password_guard.py`
# 排在 `test_api_auth.py` 之前，它 `import app.security` →
# 此时 ADMIN_PASS 还没设 → 走 ephemeral 分支生成随机口令（22 字符）→
# 之后 `test_api_auth.py` 的 `setdefault` 已无意义，它自己读到的
# `os.environ["ADMIN_PASS"]` 是 admin123，而 security 里是随机串
# ⇒ 登录必然 401。
#
# 表现是"单独跑绿、全量跑红"，而且换个文件名排前/排后就会变 ——
# 典型的**导入顺序耦合**。conftest.py 是所有测试最早加载的文件，
# 把环境变量钉在这里，顺序就无关了。
os.environ.setdefault("ADMIN_PASS", "admin123")

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


def _real_data_dir():
    """`app.paths.DATA_DIR` 的原值（隔离前的仓库路径）。"""
    from app.paths import DATA_DIR
    return str(DATA_DIR)


@pytest.fixture(autouse=True)
def _isolate_data_dir(tmp_path, monkeypatch):
    """所有测试的上传目录 / SQLite 库一律重定向到临时目录（R55）。

    为什么需要：`_DATA_DIR` 既是**上传落脚目录**，也是 `_DB_PATH` 的父目录。

        _DATA_DIR = _RESOLVED_DATA_DIR       # → backend/app/data
        _DB_PATH  = _DATA_DIR / "requests.db"

    多数测试只 monkeypatch 了 `_DB_PATH`，没动 `_DATA_DIR` ——
    于是上传类用例（413 超限、合并表头、合法文件）把文件**真写进了仓库**。
    实测残留：a.xlsx / big.xlsx(1MB) / merged.xlsx / ok.xlsx / requests.db(224KB)。

    这是**发布阻塞项**：`preflight_release.py` 的「上传面·学生材料/演示库」
    会拦下它们。而 `.gitignore` 里的 `backend/app/data/` 救不了 ——
    预检脚本的注释写明：**上传器不读 .gitignore**。

    与 R46（ADMIN_PASS 注入时机）同一个道理：默认值决定大多数用例的行为。
    把"不污染"设成默认，比分头给每个用例加 monkeypatch 可靠得多。
    """
    import app.api.reschedule as _api

    fake_data = tmp_path / "data"
    fake_data.mkdir(exist_ok=True)
    monkeypatch.setattr(_api, "_DATA_DIR", str(fake_data))
    # `_DB_PATH` 由 `_DATA_DIR` 派生，必须一起改 —— 只改一个会出现
    # "库在仓库里、上传在临时目录"这种半隔离状态。
    monkeypatch.setattr(_api, "_DB_PATH", str(fake_data / "requests.db"))

    # ⚠️ 建表也要在**新路径**上重跑一遍。
    # `_init_db()` 在模块导入时用仓库路径跑过，切到临时目录后那里是个空库 ——
    # 不改这一步，任何碰工单表的用例都会 "no such table: requests"。
    _api._init_db()

    # `reschedule.py` 在**模块导入时**就调了一次 `_init_db()`（线上需要它），
    # 那一次会在仓库里建出空的 `requests.db`（24KB，低于预检 100KB 阈值）。
    # 空库无害，但"跑完测试仓库里多一个文件"仍是噪声 —— 跑完删掉。
    # 只删**空库**：真有内容的库删了会丢数据，而真有内容的库不该出现在测试里。
    _repo_db = os.path.join(str(_real_data_dir()), "requests.db")
    yield
    try:
        if os.path.exists(_repo_db) and os.path.getsize(_repo_db) <= 100_000:
            os.remove(_repo_db)
    except OSError:
        pass
