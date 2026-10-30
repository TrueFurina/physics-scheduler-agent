"""测试环境的导入顺序耦合（R46）。

缺陷
----
`security.py` 在**模块导入时**就把管理员口令读进模块级常量：

    _ADMIN_PASS_RAW = (os.getenv("ADMIN_PASS") or "").strip()
    ADMIN_PASS = _ADMIN_PASS_RAW or <随机生成>

谁先导入 `app.security`，谁就决定了这个常量。

而 `ADMIN_PASS` 原来只设在 `test_api_auth.py` 里（模块级 `setdefault`）。
pytest 按文件名排序，`test_admin_password_guard.py` 排在它**前面**，
并且会 `import app.security` —— 此时 `ADMIN_PASS` 还没设：

```
security.ADMIN_PASS = 'O0UMVsXgZwaLKNszCFSHkQ'   ← 随机生成（ephemeral）
env  ADMIN_PASS     = 'admin123'                  ← 后来才设的
```

两者不一致 ⇒ `admin-login` 必然 401。

表现：**单独跑该用例绿，全量跑红**；而且换个文件名排序就会变。
典型的导入顺序耦合。

修复
----
把 `ADMIN_PASS` 钉在 `conftest.py` —— 它是所有测试最早加载的文件，
`pytest` 在导入任何 test 模块之前先导入它。这样顺序就无关了。

下面两条测试守住这个不变量。
"""
import os

import pytest


def test_admin_pass_is_set_before_anything_imports_security():
    """`ADMIN_PASS` 环境变量必须在测试开始前就存在，且不是随机值。"""
    import app.security as security

    env_pass = os.environ.get("ADMIN_PASS")
    assert env_pass, (
        "conftest 没设 ADMIN_PASS —— security.py 会随机生成口令，"
        "而 test_api_auth 读到的是环境变量，两者不一致 ⇒ 登录 401"
    )
    assert security.ADMIN_PASS == env_pass, (
        f"security.ADMIN_PASS({security.ADMIN_PASS!r}) 与 "
        f"环境变量({env_pass!r}) 不一致 —— 有模块在 ADMIN_PASS 设置之前"
        "就 import 了 app.security"
    )


def test_security_password_not_ephemeral_in_tests():
    """测试进程不该用随机口令 —— 那意味着环境变量没生效。"""
    import app.security as security

    assert getattr(security, "ADMIN_PASS_IS_EPHEMERAL", False) is False, (
        "测试进程跑成了 ephemeral（随机口令）—— 环境变量注入时机错了"
    )


def test_conftest_sets_admin_pass():
    """源码契约：conftest 必须显式 setdefault ADMIN_PASS。"""
    path = os.path.join(os.path.dirname(__file__), "conftest.py")
    with open(path, encoding="utf-8") as f:
        src = f.read()
    assert 'os.environ.setdefault("ADMIN_PASS"' in src, (
        "conftest.py 里没有 setdefault ADMIN_PASS —— "
        "只放在 test_api_auth.py 里会因文件名排序而失效"
    )
