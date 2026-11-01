# 部署与回滚手册（Runbook）

> 适用范围：物理实验调课辅助系统（FastAPI 后端 + React/Vite 前端）
> 状态：**发布冻结**（用户 2026-10-23 决策，线上永停）。本手册用于「冻结态也能干净交接 / 复现」，
> 不是上线步骤。解除冻结见第 10 节「解冻三决策」。
> 维护者交接红线见第 9 节「并发会话纪律」。

---

## 1. 架构与端口

- 后端 FastAPI：`uvicorn app.main:app`。默认 `:8000`；**一键启动脚本 `start.ps1` 与前端代理实际使用 `:9000`**。
- 前端：
  - 开发态 `npm run dev` 起在 `:5173`，`/api` 代理到 `:9000`（见 `frontend/vite.config.ts`）。
  - 生产态 `npm run build` 产物在 `frontend/webui`（`outDir` 刻意非 `dist`，原因见 `vite.config.ts` 注释：
    发布上传会排除「构建产物」目录，而本系统由 FastAPI 同源托管，产物必须随源码进沙箱）。
    构建后由 FastAPI 同源托管（`FRONTEND_DIST` 默认 `frontend/webui`），不再需要独立前端服务。
- 三个端口（8000 / 9000 / 8023）并存是历史约定：
  - `8000` = uvicorn 默认；
  - `9000` = 当前启动脚本（`start.ps1`）与 Vite 代理目标（Hyper-V 动态保留段 workaround）；
  - `8023` = 测试脚本与 `prepare_demo.py` 默认。
  - 改端口前先读 `vite.config.ts` 第二节与 `start.ps1` 注释，避免「白屏 / 连接被拒」。

## 2. 一键启动（开发 / 演示）

- Windows：双击 `启动项目.bat` 或 `start.bat`（两者都调用 `start.ps1`）。
- `start.ps1` 会依次：设 `OPENBLAS_NUM_THREADS=1` → 注入 `DEMO_MODE=1 ADMIN_PASS=admin123`
  → 起后端（`:9000`）→ 起前端（`:5173`）→ `curl` 健康检查。
- 健康检查：`GET http://localhost:9000/health` → `200`。
- 浏览器打开：`http://localhost:5173`。

## 3. 生产启动（冻结态下仅作交接说明，非上线步骤）

1. 前端构建：`cd frontend && npm run build`（输出 `frontend/webui`）。
2. 配置：复制 `backend/.env.example` → `backend/.env`，至少填：
   `ADMIN_PASS`（>=16 位）、`APP_ENV=production`、`DEMO_MODE=0`、以及 `LLM_*`（若启用 AI 功能）。
3. 起服务：`cd backend && uvicorn app.main:app --host 0.0.0.0 --port 9000`
   （或设 `HOST` / `PORT` 环境变量）。
4. 验证：`curl http://localhost:9000/health`；`curl http://localhost:9000/docs`（若未 `DISABLE_DOCS=1`）。

## 4. 配置来源与优先级

- 优先级：**真实环境变量 > .env > env.local**（见 `backend/app/__init__.py`）。
- 模板：`backend/.env.example`（本仓提供，准确反映代码实际读取的变量）。
- 真实运行配置：`env.local`（由 `scripts/write_local_config.py` 生成，含明文密钥，**已 gitignore，勿提交**）。
- `APP_NO_ENV_FILE=1` 可跳过所有 `.env` 文件，仅用真实环境变量。

## 5. 关键路径兜底（故障不「假失败」）

- **CP-SAT 求解超时 ≠ 排课失败**：顶层路径有确定性贪心兜底，返回体带 `fallback: "greedy"`。
  机器繁忙时教秘看到的是「已用兜底排课」，不是「失败」。
- **后台预热线程** `SCHEDULE_WARMUP`：测试 / CI 必须设 `0`（`conftest.py` 已做），否则线程预热会与测试互相干扰。
- **`SOLVER_WORKERS`** 控制 CP-SAT 并行度；满载验证按 **4–5× 余量**留超时（非 1.5×，见优化路线 R-77）。
- **解析缓存键**覆盖全部输入（R-71 起改用数据自身指纹 `vars(e)` + 主数据 `to_dict`），新增写路径须注意缓存失效。
- **`busy` 与 `kept` 永不合并**（R-74）：任何会 `discard`/`add` 的函数，只读约束必须单独传参。

## 6. 演示准备

- `python scripts/prepare_demo.py`：起后端（`:8023`）→ 加载默认表（433 条）→ 造 3 条可落地工单。
- ⚠️ 它默认指向 `:8023`，与启动脚本 `:9000` 不一致；需先以 `8023` 启动或设置 `DEMO_BASE_URL`。
- 演示前置条件：`DEMO_MODE=1` + `ADMIN_PASS=admin123`，否则审批台 `401`、工单造不出来（详见脚本内 `_admin_token`）。

## 7. 回滚

- 代码回滚：`git revert <commit>` 或 `git checkout <commit> -- <paths>`（**不要 `reset --hard` 混他人在途改动**）。
- 数据：持久化目录 `PSA_DATA_DIR`（默认代码内定目录），回滚代码后旧数据仍可读；
  如需还原数据，请单独备份该目录后再操作。
- 不 `git add -A`：当前树混有并发会话在途改动，收口只提交自己的文件（见第 9 节）。

## 8. 验收 / 测试（冻结态复现闭环用）

- 后端单测：
  `OPENBLAS_NUM_THREADS=1 python -m pytest backend/tests -q -p no:cacheprovider` → **678 passed**。
- 前端：`cd frontend && npm run verify`（tsc -b + vitest）→ **126 passed**
  （`vite.config.ts` 已关文件级并行 `fileParallelism:false`，消除沙箱 brokered-fs 并发写 TEMP 的 EPERM flake）。
- 活体完整验收（含独立审计 + UI 探针；**勿常态 `--skip-audit` / `--skip-ui`**）：
  `python scripts/run_acceptance.py --skip-tests`（先起 `DEMO_MODE=1` 服务并跑 `prepare_demo.py`）→ **15/15 PASS**。
- 满载验收：`python scripts/acceptance_under_load.py 28`（28 个忙等压满核）→ **10/10 PASS**。
- 独立 oracle：`python scripts/verify_r72.py` → **410/410**（绪论/考试冲突 0）。

## 9. 并发会话纪律（收口红线）

- 当前工作树混有**并发会话的未提交改动**（`R-94` 原子改名、`R-95` scheduler 角色、`reschedule.py`、
  `run_acceptance.py`、`frontend/webui` 构建产物等），这些**不是本会话产物**。
- 收口原则：**不动、不 `git add -A`、不裹进自己的提交**。
- 只提交自己新增 / 修改的文件（如本手册与 `backend/.env.example`），按显式路径 `git add <path>`。

## 10. 冻结状态与解冻三决策（用户拍板，非工程缺陷）

- **冻结**：2026-10-23 起发布永久冻结，线上永停（D-9 态）。
- **解冻需用户拍板三件事**：
  1. 是否解除发布冻结；
  2. 是否开通 D-8 第二真实登录账号（A 提交 → 用户审批）；
  3. 轻微冲突强制通过的 PII 口径（`/upload-student` 维持 `require_admin` 且前端零调用，不顺手放开）。
- 上述任一项变动都属于产品决策，不在本工程收尾范围内。
- 注：教室容量不校验**已确认非痛点**（一个教室一个班级，容量结构性自保证），不做相关改造。

---

_本手册随工程收尾（2026-11-01）新增；与 `backend/.env.example` 同批提交，仅涉及自有文件。_
