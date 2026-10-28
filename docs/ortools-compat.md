# ortools 版本兼容性说明

## 结论先行

**`ortools==9.15.6755` 在 Python 3.12 上调用 `CpSolver.Solve()` 会直接段错误（SIGSEGV）。**

这不是项目代码的问题，是 ortools 原生库与该 Python 版本组合的兼容缺陷。本项目 `requirements.txt` 当前锁定的是 `9.15.6755`，在 Python 3.12 环境下会导致：

- 5 个测试文件整体崩溃（`test_apply_move` / `test_graybox` / `test_master_data` / `test_slot_alias` / `test_warmup`）
- 段错误发生在 C++ 层，Python 的 `try/except` 无法捕获，pytest 直接中断

---

## 最小复现（与项目代码无关）

```python
from ortools.sat.python import cp_model

m = cp_model.CpModel()
x = m.NewIntVar(0, 10, "x")
m.Add(x >= 5)
m.Maximize(x)

s = cp_model.CpSolver()
print(s.Solve(m))      # ← 9.15.6755 + Python 3.12：在这里段错误
```

实测结果：

| ortools | Python | 行为 |
|---|---|---|
| 9.15.6755 | 3.12.6 (win amd64) | `import` 正常，`Solve()` **段错误** |
| 9.11.4210 | 3.12.6 (win amd64) | 正常求解 |

> `import ortools` 和 `from ortools.sat.python import cp_model` 都能成功——崩溃只发生在实际求解时。所以"能导入"不能证明"能跑"。

---

## 处理方案（二选一）

### 方案 A · 锁到已验证可用的版本（推荐，改动最小）

```diff
- ortools==9.15.6755
+ ortools==9.11.4210
```

实测 `9.11.4210` + Python 3.12.6 下全量回归：

```
368 passed, 42 skipped, 0 failed in 76.89s
```

**无段错误，全部通过。**

### 方案 B · 保持 9.15.6755，改用 Python 3.10 / 3.11

官方为该版本发布了 cp310 ~ cp314 全套 Windows wheel，说明它**声明支持** 3.12。既然声明支持却仍崩溃，属于上游缺陷，不适合作为基线。

若必须用 9.15.6755，建议在 Python 3.10/3.11 环境验证后再锁。

---

## 为什么这值得改

当前状态的实际影响：

**1. 本地开发噪音**

跑一次全量测试，5 个文件直接段错误中断。新人第一次 `pytest` 就会遇到，会误以为项目坏了。

**2. CI 可能看不出来**

如果 CI 用 Linux，ortools 的 Linux wheel 通常不崩——本地崩、CI 绿，这种不一致最难排查。

**3. 掩盖真实回归**

我来做本次安全改动时就遇到了：全量回归出现 5 个段错误，第一反应是"是不是我改坏了排课引擎"。为此专门取回改动前的 `security.py` 做对照实验，才确认是既有问题。

**每次改动都要做一次这样的对照实验，成本很高。**

---

## 验证方式

改完 `requirements.txt` 后：

```bash
pip install -r backend/requirements.txt
cd backend
ADMIN_PASS=<your-value> python -m pytest tests/ -q
```

期望：

```
368 passed, 42 skipped
```

若仍出现 `Segmentation fault`，先单独验证最小复现，确认是 ortools 层还是项目层：

```bash
python -c "from ortools.sat.python import cp_model; m=cp_model.CpModel(); x=m.NewIntVar(0,10,'x'); m.Add(x>=5); print(cp_model.CpSolver().Solve(m))"
```

这条命令若崩，与项目无关，直接视为环境问题。

---

## 附：本次验证的完整数据

环境：Windows amd64 / Python 3.12.6 / ortools 9.11.4210 / fastapi 0.162.0 / pytest 9.2.0

| 项 | 结果 |
|---|---|
| 全量测试 | **368 passed, 42 skipped, 0 failed**（76.89s） |
| 原段错误的 5 个文件 | 全部恢复通过 |
| `test_apply_move` | 10 passed |
| `test_graybox` | 8 passed |
| `test_master_data` | 17 passed |
| `test_slot_alias` | 6 passed, 2 skipped |
| `test_warmup` | 3 passed |

42 个 skip 的来源：需要真实浏览器（`test_web_serving` 16）、需要特定数据文件（`test_demo_schedule` 10）、条件跳过（其余），均属设计内跳过。
