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
  **2026-11-01 已解冻**：用户要求废弃旧链接并重发，现网状态见第 11 节。
- **解冻需用户拍板三件事**：
  1. ~~是否解除发布冻结~~ → **已拍板：解除，并已重发**（2026-11-01）；
  2. 是否开通 D-8 第二真实登录账号（A 提交 → 用户审批）；
  3. 轻微冲突强制通过的 PII 口径（`/upload-student` 维持 `require_admin` 且前端零调用，不顺手放开）。
- 上述任一项变动都属于产品决策，不在本工程收尾范围内。
- 注：教室容量不校验**已确认非痛点**（一个教室一个班级，容量结构性自保证），不做相关改造。

## 11. 云端发布态（sites 沙箱，2026-11-01 起）

现网：`https://physics-lab-scheduler.apps.workbuddy.host/?sharecode=FIEB2FPH6U`
应用 `wbapp_vrw4Hly0FOsTFNpB6gJRmz`；发布源 `E:/physics-scheduler-deploy`。

### 11.1 发布源必须从「工作树」导出，且不能直发仓库根

发布工具只排除 `node_modules/.git/build output`，**不认 `.gitignore`**。直发仓库根会把
`env.local`（真实 LLM 密钥）、`schedule.xlsx`（真实教师数据）、`scheduler.db` 一起传上云。

```bash
rm -rf /e/physics-scheduler-deploy && mkdir -p /e/physics-scheduler-deploy
tar -cf - --exclude='./.git' --exclude='./.tmp' --exclude='node_modules' \
  --exclude='./.ruff_cache' --exclude='./.pytest_cache' --exclude='./.box-agent*' \
  --exclude='./.workbuddy' --exclude='./.workbuddy-ai' --exclude='__pycache__' --exclude='*.pyc' \
  --exclude='./env.local' --exclude='*.db' --exclude='*.genie' --exclude='*.pdf' --exclude='./frontend/dist' \
  . | tar -xf - -C /e/physics-scheduler-deploy
```

为什么用 `tar` 而不是 `git archive HEAD`：后者只等于**提交态**。实测本地工作树曾有
20 文件 / 618 行未提交改动（含 `reschedule.py` 与重建的 `frontend/webui`），
按 HEAD 发布会得到「界面和代码都不是你本地那份」，且前端资产指纹能直接看出差异。

### 11.2 启动参数（含必须项）

```
APP_ENV=production ADMIN_PASS=<20 位强口令> APP_SECRET_KEY=<固定密钥> \
DEMO_MODE=1 OPENBLAS_NUM_THREADS=1 python run.py
```

- `APP_ENV=production` 只 gate 口令策略（缺失即启动失败、长度 <16 报错），不影响业务分支。
- **`APP_SECRET_KEY` 必须带**：不带则每次发布进程重启后所有管理员会话失效（实测）。
  带上之后实测「拿 token → 重新发布 → 同一 token 仍 200」。
- 口令与会话密钥**只存在于部署配置**，不写进仓库、不落盘、不入库。换口令＝改 `startCmd` 重发。
- `DEMO_MODE=1` 才有 `/demo/reset`（演示前一键复位），该端点需管理员会话。

### 11.3 四条实测约束（踩过的坑）

1. **鉴权只能走 `X-Admin-Token`**：边缘代理会覆盖 `Authorization`。同一 token 实测
   `X-Admin-Token` → 200、`Authorization: Bearer` → 401「会话已失效」。前端本来就用自定义头，
   浏览器不受影响；**脚本 / Postman / 第三方集成按标准 Bearer 会全部失败**。
2. **分享链接会种 cookie**：带 `?sharecode=` 首次访问返回 `Set-Cookie: __wb_rsc=…`（30 天），
   之后 SPA 的 `/api/*` 凭 cookie 通行；**不带 cookie 直连接口一律 403**（2988B，非应用故障）。
3. **上传的表是临时的**：`/upload` 只写进进程内存，重新发布立刻回落到镜像里的
   `schedule.xlsx`。实测「上传脱敏表→线上变教师H→重新发布→回落真实表」。
4. **`/health` 的 `data` 字段会假阴性**：刚回收重启时实测 `uptime_s=0`、
   `data.loaded=false / total_entries=0`，但同一时刻 `/api/reschedule/data-audit`
   已返回 433 条。**判断数据是否加载请以 `/data-audit` 为准**，别拿 `/health` 的
   `data` 子字段下结论（否则会误报「数据没加载」）。

### 11.4 数据在哪、什么会丢

| 数据 | 位置 | 重新发布 | 沙箱回收 |
|---|---|---|---|
| 安排表（权威） | 发布目录 `schedule.xlsx` | 被覆盖（＝发版） | **自动从发布目录重建** |
| 上传的表 | 进程内存 + data 目录 | **失效，回落镜像表** | 丢 |
| 工单 `requests.db` / 主数据 `master_store.json` | 沙箱磁盘 | **保留**（进程存活期内实测） | **丢** |

**回收后的实际行为（2026-11-04 实测）**：沙箱回收会杀掉进程并自动拉起新进程
（`/health` 的 `uptime_s` 归零）。真实表因为**来自发布目录文件、每次启动重新加载**，
所以无需人工干预即恢复为 433 条；强口令与管理员会话也因 `APP_SECRET_KEY` 在启动参数里而保留。
但**工单库与主数据活在沙箱磁盘，回收即归零**——此前实测的「重新发布不丢工单」只在
**进程存活期内**成立，扛不住回收。

结论（两条铁律）：

1. **改数据 = 改本地表 → 重新导出发布目录 → 再发一次**；线上直接传只对当次进程有效。
2. **线上工单不是长期存档**：需要留痕的审批结果，必须在回收前用
   `GET /api/reschedule/requests/export` 与 `/export/master-workbook` 导出到本地；
   别把审批台当唯一台账。

### 11.5 演示前检查清单（云端，4 步约 10 秒）

1. `POST /api/reschedule/demo/reset`（管理员）→ 期望「课表已重载」
2. `GET /api/reschedule/data-audit` → 期望 433 条 / 13 教师 / 1 处「林珠云」教师冲突
3. `GET /api/reschedule/export/class-schedule` → 期望合法 xlsx（约 41KB）
4. 首页 `<title>` = 物理学科智能排课选课系统

**全链路验收（可选，约 1 分钟，自带还原）**：两条写路径探针已支持打线上
（`--sharecode` 换 cookie、`PROBE_ADMIN_PASS` 传口令、鉴权头改 `X-Admin-Token`、
远端账本改按接口核账）：

```bash
PROBE_ADMIN_PASS=<管理员口令> python scripts/probe_land_plan.py \
  --site https://physics-lab-scheduler.apps.workbuddy.host --sharecode FIEB2FPH6U
PROBE_ADMIN_PASS=<管理员口令> python scripts/probe_approval_chain.py \
  --site https://physics-lab-scheduler.apps.workbuddy.host --sharecode FIEB2FPH6U
```

期望 **19/19** 与 **11/11**。前者测「方案 → 预览 → 采纳 → 落地 → 撤销」，
后者测「申请 → 驳回 → 应用 → 幂等」。两条都会自己还原：落地探针撤销，
审批探针走 `/demo/reset`（未开 DEMO_MODE 的站点会回 403，需人工清 `applied_moves`）。
跑完应回到基线：433 条 / 核心冲突 (0,1,0) / 同班分批 47 / `landed=false`。

### 11.6 性能基线（云端 vs 本地，2026-11-01 实测）

| 路径 | 云端 | 本地 |
|---|---|---|
| 全校一键 `/auto-schedule` | 17.4s（求解 14.7s），171/171 段 | 16.5s（求解 16.4s） |
| 并行 `/parallel/solve-all` | 14.4s（求解 11.8s），17/17 班、410/410 课时 | 同级 |
| 导出班级课表 | 2.6s / 41KB | — |

结论：**算力不是瓶颈**，云端与本地同级。

### 11.7 已知平台限制（非代码缺陷）

- **无外部数据库、无备份**：只有单端口 + 沙箱磁盘，沙箱回收即丢，需靠导出留档。
- **无 LLM 密钥**：`env.local` 里的 ZHIPU/DEEPSEEK 等未上云，`/chat` 静默降级为规则解析，
  `ai_plans` 恒空。要演示「AI 出候选方案」必须显式注入密钥（有额度成本）。
- **公开链接＝公开管理入口**：拿到链接的人都能看到登录页，口令是唯一门禁（已换 20 位强口令）。

---

_本手册随工程收尾（2026-11-01）新增；与 `backend/.env.example` 同批提交，仅涉及自有文件。_
