"""测试不该往真实数据目录写东西（R55）。

缺陷
----
`backend/app/data/` 是**上传落脚目录 + SQLite 库目录**：

    _DATA_DIR = _RESOLVED_DATA_DIR          # → backend/app/data
    _DB_PATH  = os.path.join(_DATA_DIR, "requests.db")

而多数测试只隔离了 `_DB_PATH`，没隔离 `_DATA_DIR`。于是上传类测试
（`test_api_auth.py` 里的 413 / 合并表头 / 合法文件等用例）把文件
真写进了仓库里的 `backend/app/data/`。

实测残留（全是我跑测试产生的）：

```
a.xlsx          1 B
big.xlsx    1.0 MB      超限测试用的 1MB 装填
merged.xlsx  5.0 KB     合并表头用例
ok.xlsx      1.0 KB     合法上传用例
master_m.xlsx 13 KB     台账导入用例
requests.db  224 KB     工单库
master_store.json 16 KB 主数据落盘
```

**这是发布阻塞项**：`preflight_release.py` 的「上传面·学生材料/演示库」检查
会拦下它们（`requests.db` 超过 100KB 就判为"装过真实工单"）。

`.gitignore` 里写了 `backend/app/data/`，但预检脚本的注释说得很清楚：
**上传器不读 .gitignore** —— 所以忽略规则救不了这个。

修法
----
在 `conftest.py` 里统一隔离 `_DATA_DIR`（以及由它派生的 `_DB_PATH`）。
个别需要真实路径的用例可以自己覆盖回去，但**默认必须是隔离的**。

与 R46 同一个道理：默认值决定大多数用例的行为，
把"不污染"设成默认，比分头给每个用例加 `monkeypatch` 可靠。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import app.api.reschedule as api


def _real_data_dir():
    from app.paths import DATA_DIR
    return os.path.abspath(str(DATA_DIR))


def test_data_dir_is_isolated_from_repo():
    """测试进程里的 `_DATA_DIR` 不能指向仓库的数据目录。"""
    real = _real_data_dir()
    cur = os.path.abspath(str(api._DATA_DIR))
    assert cur != real, (
        f"_DATA_DIR 仍指向仓库目录 {real} —— "
        "上传类测试会把文件真写进 backend/app/data/，"
        "成为发布阻塞项（上传器不读 .gitignore）"
    )


def test_db_path_follows_data_dir():
    """`_DB_PATH` 必须跟着 `_DATA_DIR` 走，不能仍指向仓库。"""
    real = os.path.join(_real_data_dir(), "requests.db")
    cur = os.path.abspath(str(api._DB_PATH))
    assert cur != real, f"_DB_PATH 仍指向仓库 {real}"


def test_conftest_isolates_data_dir():
    """源码契约：conftest 必须显式隔离 `_DATA_DIR`。"""
    path = os.path.join(os.path.dirname(__file__), "conftest.py")
    with open(path, encoding="utf-8") as f:
        src = f.read()
    assert "_DATA_DIR" in src, (
        "conftest.py 没有隔离 `_DATA_DIR` —— 上传测试会写进仓库数据目录"
    )
