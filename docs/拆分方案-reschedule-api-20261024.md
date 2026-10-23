# 拆分方案 · `backend/app/api/reschedule.py`（2489 行单文件）

> 日期：2026-10-24 ｜ 状态：**方案待决策**（交付评审 P2-2，明确延后项）
> 结论先行：**可拆，API 契约可保零变化**，但耦合面比初查深得多——**42 个测试文件
> 引用该模块，23 个文件共 120 处模块级属性 patch，38 个内部符号被测试直接调用**。
> 因此必须采用"**状态单例 + 门面模块**"模式，并用"旧 patch 点先红"完成迁移。
> 本文所有数字均为 2026-10-24 实测（勿凭印象修改，改前先 grep）。

## 1. 现状结构（文件自带的 17 个分节）

| # | 行范围 | 内容 | 自包含度 |
|---|---|---|---|
| 1 | 1–64 | imports + 鉴权边界文档 + `_ADMIN_ONLY` | 基础 |
| 2 | 65–102 | P1-2 免鉴权开关 `export_gate`/`_public_readonly_enabled` | 高 |
| 3 | 103–123 | `_landings()` 落地账本访问器 | 高 |
| 4 | 124–161 | 路径解析 `_find_repo_root`/`_resolve_default_schedule_path` | 高 |
| 5 | 162–192 | Pydantic 模型 ×5 | 高 |
| 6 | 193–334 | 上传：/upload、/upload-master、/master-status | 中 |
| 7 | 335–604 | 主数据 CRUD ×10 + 导入预览/导入 + master-workbook 导出 | 中 |
| 8 | 605–934 | 核心状态与加载：data_state、/status、_load_default_schedule、_ensure_loaded、任务派生 ×4 | **共享枢纽** |
| 9 | 935–1004 | LLM：/chat、/experiment-tutor、/admin-login | 中 |
| 10 | 1005–1157 | 并行求解：/parallel/overview\|solve\|solve-all\|export | 中 |
| 11 | 1158–1261 | 落地：/parallel/land/preview\|land\|undo\|status | 高 |
| 12 | 1262–1349 | XLSX 导出助手 ×6（纯函数） | 高 |
| 13 | 1350–1571 | 导出 ×3：class-schedule/room-schedule/master-data | 中 |
| 14 | 1572–1882 | 报表 ×4：data-audit/stats/schedule-grid/impact-analysis | 中 |
| 15 | 1883–2033 | 自动排课 ×2：/auto-schedule、/auto-schedule/export | 中 |
| 16 | 2034–2070 | 教师查询 ×2 | 高 |
| 17 | 2071–2489 | 调课工单（SQLite 全流程，自带 _DB_PATH） | **最独立** |

## 2. 共享状态清单（拆分的真正难点）

模块级单例/常量（§8 是枢纽，几乎每个端点都经它）。patch 次数为实测：

| 名字 | 类型 | patch 次数 |
|---|---|---|
| `_db` | ScheduleDatabase 单例 | 38 |
| `_master_data` | MasterData 单例 | 29 |
| `_master_tasks` | List | 22 |
| `_schedule_fp` | global 重指派 ×4（254/685/751/777 行） | 6 |
| `_master_derived_tasks` | §8 派生函数（被 `lambda: []` 桩替换） | 5 |
| `_DB_PATH` | SQLite 路径常量 | 6 |
| `_agent` | RescheduleAgent 单例 | 4 |
| `_DATA_DIR` | 上传目录常量 | 4 |
| `_landing_store` | global 单例（104 行） | 2 |
| `_upload_target` | 纯函数 | 1 |
| `_REPO_ROOT` | import 时计算的常量 | 1 |
| `_load_lock`/`_land_lock` | 锁 | 0 |
| `DEFAULT_SCHEDULE_PATH` | import 时计算的常量 | 2 |

`global` 重指派（`_schedule_fp`×4、`_landing_store`×1）搬到状态模块后改为属性赋值
`state._schedule_fp = ...`——全文件唯一需要改写法的地方。

## 3. 测试耦合清单（为什么不能纯搬运）

**42 个测试文件**以某种形式引用 `app.api.reschedule`（23 个顶层 import，三种形式：
`import app.api.reschedule as api` ×15、`from app.api import reschedule [as R]` ×7、
`from app.api.reschedule import (...)` ×1；其余为函数内 import）。按危险程度分三类：

1. **模块属性 patch —— 120 处 / 23 文件（假绿陷阱，本文核心）**
   分布见 §2 表。若门面模块仍 re-export `_db` 等名字，`monkeypatch.setattr(api, "_db", fake)`
   会"成功"但不生效——端点读新位置的真单例，测试跑在真实数据上还显示绿。
   本项目缺陷档案里这类事故反复出现（静默通过比报错危险）。
2. **符号直调 —— 38 个（5 个 Pydantic 模型 + 33 个函数）**
   模型：ChatRequest / ConsultRequest / ParallelSolveRequest / AutoScheduleRequest /
   AutoScheduleExportRequest。函数：`reschedule_chat`、`_apply_reschedule`、`_ensure_loaded`、
   `_ensure_master_data`、`_persist_master_state`、`_landings`、`_get_db`、`_init_db`、
   `_parse_original_time`、`_find_repo_root`、`_enrich_extra_tasks`、`_raw_schedulable_tasks`，
   以及全部端点处理函数（`parallel_solve`/`parallel_solve_all`/`auto_schedule`/
   `auto_schedule_export`/`export_class_schedule`/`export_room_schedule`/`export_master_data`/
   `data_audit`/`stats`/`impact_analysis`/`master_status`/`upload_master`/
   `experiment_tutor_consult`）。测试绕过 HTTP 直调它们（如 `asyncio.run(api.reschedule_chat(...))`）。
3. **对象属性 patch / 直接使用（只要还是同一个对象就安全）**
   `rs_mod._db.loaded = False`、`api._agent.process_request = fake`、
   `api._master_data.upsert_experiment(...)` 等。

⚠️ **两个双重身份特例**：`_upload_target` 和 `_master_derived_tasks` 既被 patch 又被直调。
它们的 patch/call 点必须一起迁移到真实所在模块，门面对它们**只迁移不 re-export**。

**门面 re-export 政策**（§4 的设计依据）：
- ✅ re-export：第 2、3 类中**零 patch 点**的符号（36 个）——函数/模型对象，re-export 后
  直调点原样可用；迁移期为这些点提供缓冲
- ❌ 绝不 re-export：§2 表里 12 个有 patch 记录的名字（**连同名都不给**）⇒ 120 处旧
  patch 点第一步全部 AttributeError 红 ⇒ 按"先红后绿"逐处迁移。这正是项目既有纪律
  （R-25 新判据先跑红）的镜像用法：这次先红的是旧探针

## 4. 目标布局

```
backend/app/api/
├── state.py            # 共享状态单例 + 路径常量 + 锁 + 加载/派生 + data_state()（~230 行，零路由）
├── gates.py            # _ADMIN_ONLY / export_gate / _public_readonly_enabled（~60 行）
├── upload_utils.py     # _safe_upload_name / _upload_target（~50 行，纯函数）
├── routes_upload.py    # §6 上传三端点（~140 行）
├── routes_master.py    # §7 主数据 CRUD + 导入 + workbook 导出（~270 行）
├── routes_chat.py      # §9 chat/tutor/admin-login + 知识上下文（~70 行）
├── routes_scheduling.py# §10+15+16 并行求解 + 自动排课 + 教师查询（~470 行）
├── routes_landing.py   # §11 落地四端点（~105 行，主体已在 plan_landing.py）
├── routes_exports.py   # §12+13 XLSX 助手 + 三个导出（~330 行）
├── routes_reports.py   # §14 audit/stats/grid/impact（~310 行）
├── routes_requests.py  # §17 SQLite 工单全流程（~420 行）
└── reschedule.py       # 门面：组合 router + re-export 36 个零 patch 符号（~60 行）
```

**关键设计：state.py 保留与原模块完全相同的属性名**（`state._db`、`state._master_data`、
`state._schedule_fp`……）。120 处迁移因此是纯别名替换（`api._db` → `state._db`），
不改名字、不碰语义，可 sed 批量完成再逐处实跑核验。端点侧 `from app.api import state`
后全部 `state._db` 访问。

API 契约零变化：各模块各自 `APIRouter()`，main.py 由 1 行 include 改为 11 行（或门面
组合后仍 1 行），**端点路径/方法/响应一律不动**。OpenAPI 路径数锁定 47 为每一步判据。

## 5. 执行计划（10 步，每步独立可回退）

每步完成标准：`pytest 399 passed` + `ruff check .` + `openapi paths == 47` + 至少一个
相关探针绿。建议**每步一个 commit**（回退粒度细；本仓 commit 文化支持）。

| 步 | 动作 | 要点 |
|---|---|---|
| 0 | 基线固化 | 记录当前 47 端点清单 + 399 passed + 42 文件耦合清单作对照 |
| 1 | 建 `state.py` | 单例/常量/锁/global 改写/加载与派生函数/`data_state` 搬入；reschedule 改为从 state import（此步端点行为不变） |
| 2 | 建 `gates.py` + `upload_utils.py` | 纯搬运；test_api_auth 的 4 个 import 改指新模块（先红后绿演练） |
| 3 | 拆 `routes_requests.py` | 最独立（自带 _DB_PATH）；`_DB_PATH` patch（6 处）先迁到 `state` |
| 4 | 拆 `routes_chat.py` | 小；`_agent` 对象属性 patch 天然安全，模块级 patch（4 处）迁移 |
| 5 | 拆 `routes_landing.py` | 薄壳；`_landing_store` patch（2 处）+ global 改写在此步兑现 |
| 6 | 拆 `routes_upload.py` | `_DATA_DIR`/`_upload_target` patch（5 处）迁移；`_upload_target` 双重身份特例在此了结 |
| 7 | 拆 `routes_master.py` | `_master_data`(29)/`_master_tasks`(22)/`_master_derived_tasks`(5) 迁移主战场 |
| 8 | 拆 `routes_exports.py` + `routes_reports.py` | XLSX 助手先纯搬；报表端点依赖 state |
| 9 | 拆 `routes_scheduling.py` + 门面收尾 | reschedule.py 瘦成门面；main.py 改 include；**门面删光 12 个状态名 re-export ⇒ 残留 patch 点全红 ⇒ 清干净后全绿**；36 个零 patch 符号 re-export 兜底直调点 |

## 6. 风险登记册

| 风险 | 等级 | 处置 |
|---|---|---|
| 旧 patch 点静默失效（120 处） | **高** | 门面不 re-export 状态名（同名也不给）；AttributeError 先红后迁 |
| `global` 重指派改写遗漏（5 处） | 中 | 逐一对照 grep；`state.X = ...` 属性赋值 |
| 路由注册顺序/重复路径 | 中 | 每步判 `openapi paths == 47` + 端点清单 diff |
| `_IncludedRouter` 懒加载表示变化 | 低 | preflight/CI 冒烟注释里"app.routes 只有 8 条"的描述需同步改 |
| 循环 import | 低 | state/gates/upload_utils 不 import 任何 routes_*；routes_* 只 import state |
| 120 处迁移机械出错 | 中 | 批量 sed 后必须逐文件实跑；禁"看起来对了" |
| 行为漂移（线程池/锁语义） | 中 | `run_in_threadpool`、`_load_lock`、`_land_lock` 原样搬迁，每步全量回归兜底 |

## 7. 明确不做

- 不改任何端点路径/方法/请求响应模型（API 契约冻结）
- 不改业务逻辑（拆分是纯代码搬家 + 状态访问方式改写）
- 不动 `plan_landing.py`/`schedule_grid.py` 等同级模块（它们已是拆好的范例）
- 不借拆分夹带其他重构（lint churn、类型现代化等另开批次）

## 8. 值不值与工作量

**买到的**：单文件 2489 行 → 最大模块 470 行；评审/找错从"滚动屏"变"按模块读"；
多人可并行改不同 routes_* 而不冲突；新增端点的落点有明确惯例。

**付出的**：42 文件耦合面一次性迁移（120 patch 点 + 38 直调符号），8–10 小时，
且必须是整块时间——**迁移一半的 patch 面是全仓最危险的状态**（部分测试跑在假单例上），
禁止切成碎片穿插功能开发。

**建议时机**：交付评审闭环已提交（`332cacf`）之后的独立批次；若近期有功能压力，
继续延后是正确的——本方案随时可启。
