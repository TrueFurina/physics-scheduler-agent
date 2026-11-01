"""给任何"跑 pytest"的脚本一个**干净、一次性**的 `PYTEST_DEBUG_TEMPROOT`。

为什么需要（R-100 / R-101）
--------------------------
pytest 在**会话结束**会清理 `%TEMP%/pytest-of-<user>/` 下旧的运行根目录，
一次删几百个文件。本沙箱的批量删除守卫是 `threshold=50 / scope=turn`，
撞上就 `SystemExit` —— 两种表现都很误导：

  * 子进程调用（run_acceptance / soak_tests）：用例全绿、**退出码非 0**；
  * in-process 调用（coverage_probe 的 `pytest.main`）：**报告根本没打印**，
    只看到一行 `[safe-delete][SAFE_DELETE_BULK_CONFIRM_REQUIRED]`。

解法不是"别删"，而是**让它没有东西可删**：每次给一个全新的 temproot，
会话结束时它自己刚建的那一份之外没有旧目录，清理量归零。

用法
----
    from _pytest_temproot import fresh_env, install_inprocess

    # 子进程
    env = fresh_env(dict(os.environ, OPENBLAS_NUM_THREADS="1"))
    subprocess.run([sys.executable, "-m", "pytest", ...], env=env)

    # in-process（pytest.main）
    install_inprocess()          # 改 os.environ，必须在 pytest.main 之前
    pytest.main([...])
"""
import os
import tempfile
import time

_PREFIX = "psa-pytest-"


def _new_root() -> str:
    # 毫秒级时间戳：同一秒内并发起两个也不撞
    root = os.path.join(tempfile.gettempdir(), "%s%d" % (_PREFIX, int(time.time() * 1000)))
    os.makedirs(root, exist_ok=True)
    return root


def fresh_env(base_env=None) -> dict:
    """返回一份带全新 temproot 的环境变量副本（给 subprocess 用）。"""
    env = dict(base_env if base_env is not None else os.environ)
    env["PYTEST_DEBUG_TEMPROOT"] = _new_root()
    return env


def install_inprocess() -> str:
    """改 `os.environ`，给 in-process 的 `pytest.main` 用；返回创建的根目录。"""
    root = _new_root()
    os.environ["PYTEST_DEBUG_TEMPROOT"] = root
    return root
