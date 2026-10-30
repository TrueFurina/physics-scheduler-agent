# 物理学科智能排课选课智能体系统

> 面向高校物理实验教学的智能辅助系统：排课、并行批量排课、主数据管理、调课、实验规范查询。

[English README](README.md)

---

## 项目简介

面向大学物理实验教学场景的**三大模块智能体系统**：**排课、调课、实验规范查询**
（排课模块含并行批量排课、全校一键排课、主数据管理、方案落地、周课表网格可视化、数据体检、教务标准课表导出）：

| 模块 | 能力 |
|------|------|
| 📅 **排课** | 约束求解自动排课、冲突检测、候选方案推荐 |
| ⚡ **并行批量排课** | CP-SAT 一次求解一个班的「全部实验项目 × 学生组别」（导师座谈一号痛点）；缺教师/场地信息自动灰箱补全，且每个代做决定都说出口 |
| 🗂️ **主数据管理** | 课程/实验项目/教师/场地/班级五张实体表可增删改，从安排表反推、重启不丢（版本化落盘）、可导出 Excel 台账并导回 |
| 🔄 **调课** | 模板 + 自然语言双入口，冲突分级（场地/教师/整班同组），管理员审批真正落到课表 |
| 🔬 **实验规范查询** | 排课人员/教师查询实验项目的学时、场地、操作步骤、数据处理与报告规范 |

**设计原则**：规则引擎保证排课正确（冲突筛选），LLM 增强体验（意图解析、候选方案、咨询问答）。

## 为什么做这个项目

- 论文调研表明：实验课很少进入高校排课系统，排课依赖人工协调
- 一处调整可能引发多班、多教师、多实验室的连锁冲突
- 现有工具（Excel、问学委）无法高效解决跨班跨教师冲突

## 技术栈

| 层 | 技术 |
|----|------|
| 后端 | Python FastAPI + openpyxl + **OR-Tools CP-SAT**（`requirements.txt` 中为 `ortools`） |
| 前端 | React + Vite + TypeScript + Tailwind CSS |
| LLM | 律动基元（OpenAI 兼容，环境变量可替换） |
| 数据 | 实验安排表（433条）+ 五表主数据（不含任何学生数据） |

## 快速启动

### 环境要求

- Python 3.10+
- Node.js 18+
- **LLM 凭据（任选其一，按以下优先级自动探测）**：

| 优先级 | 环境变量 | 说明 |
|---:|---|---|
| 1 | `ZHIPU_API_KEY` | 永久免费不限量（主防线） |
| 2 | `MODELSCOPE_API_KEY` | 免费 2000 次/天，热门时段可能 429 |
| 3 | `TOKENHUB_API_KEY` | 一次性体验包，额度用尽报 402 |
| 4 | `DEEPSEEK_API_KEY` | 余额制（付费） |
| 5 | `AMD_API_KEY` / `XFYUN_API_KEY` | 免费档，有速率限制 |
| — | `JIYUAN_LVDONG_API_KEY` | ⚠️ **已欠费 402，仅历史兜底，勿用** |

> 只设其中一个即可；都未设时会自动按上表顺序探测（`app/agent/llm_intent_parser.py::_AUTO_ORDER`）。
> **不配 LLM 也能用**：规则引擎承担全部排课逻辑，LLM 只增强意图解析与咨询问答，
> 缺失时相关功能降级为规则解析（设 `LLM_DISABLED=1` 可显式关闭）。

### 后端

```bash
cd backend
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

### 前端

```bash
cd frontend
npm install
npm run dev
```

打开 **http://localhost:5173**

## 使用流程

1. **选择身份**——点击教师卡片（系统知道用户是谁）
2. **我的课程**——查看全部课程，标注可调/不可调
3. **模板调课**——选课程 + 目标周次，自动冲突筛选
4. **结果**——绿色可用时段 + 灰色被排除时段（附原因）+ AI 候选方案
5. **提交**——生成调课申请
6. **管理员审批**——同意/驳回（需管理员登录）
7. **实验规范查询**——面向排课人员/教师的实验规范问答

## 核心功能

- ✅ **冲突分级**：核心冲突（场地/教师/同班同组）说清「撞了什么、该怎么改」——整组判据按 (班级, 组) 粒度，真实表 47 处并行教学零误报
- ✅ **主数据一等公民**：五张实体表反推后可编辑，重启自愈（版本化落盘，换表自动失效）；绪论/考试类登记在册但**绝不进求解器**
- ✅ **并行批量排课**：整班 CP-SAT 一次求解；灰箱模式自动补全缺教师（负载最轻优先）/场地/周次，并逐条报告假设
- ✅ **台账往返**：五张表导出 Excel → 离线维护 → 导回，带统计列伪装与班名切碎两道防线
- ✅ **原因透传**：每个被排除时段说明原因
- ✅ **自然语言调课**："我是吴琳，第六周的课上不了，调到下周"
- ✅ **多轮追问**：信息缺失时系统主动补全
- ✅ **SQLite 持久化**：调课申请重启不丢
- ✅ **后端 734 个测试 + 前端  个测试**，另有独立审计器（`audit_conflicts`/`verify_graybox`/`verify_master_roundtrip`/`verify_intro_filter`/`probe_approval_chain`/`probe_land_plan`/`probe_schedule_grid`/`probe_import_wizard`/`probe_ui_newviews`，用独立解析器从原始 Excel 重算一切）
- ✅ **方案落地**：全校一键方案先看四类差异账，管理员采纳后写回安排表，可随时撤销恢复快照；重启自动重放（sqlite 账本）

## 项目结构

```
physics-scheduler-agent/
├── backend/
│   ├── app/
│   │   ├── main.py                 # FastAPI 入口
│   │   ├── api/reschedule.py       # API 端点
│   │   ├── master_data.py          # 五表主数据（反推 + CRUD）
│   │   ├── master_store.py         # 主数据版本化落盘
│   │   ├── master_workbook.py      # 台账导出/导回（往返）
│   │   ├── conflict_model.py       # 冲突分级（核心三类）
│   │   ├── course_bridge.py        # 课程归属 + 跨课程撞车扫描
│   │   ├── agent/                  # 调课智能体、LLM 意图解析
│   │   ├── engine/                 # 约束求解自动排课（CP-SAT）
│   │   └── utils/                  # Excel 解析器 / 时间模型
│   └── tests/                      # 734 个 pytest 测试
├── frontend/
│   └── src/                        # React 界面（看板/主数据/并行排课/沙盘等）
├── scripts/                        # 独立审计器 + 发布预检
├── docs/                           # PRD、架构、使用指南、总结反思
└── start.ps1 / start.bat           # 一键启动
```

## API 一览

| 方法 | 路径 | 模块 | 说明 |
|------|------|------|------|
| POST | `/api/reschedule/upload` | 排课 | 上传实验安排表 |
| GET | `/api/reschedule/teachers` | 排课 | 教师列表（身份选择） |
| GET | `/api/reschedule/teachers/{name}/courses` | 排课 | 教师课程 |
| POST | `/api/reschedule/auto-schedule` | 排课 | 自动排课 |
| POST | `/api/reschedule/chat` | 调课 | 调课查询（可用+被排除+AI方案） |
| POST | `/api/reschedule/requests` | 调课 | 提交调课申请 |
| POST | `/api/reschedule/requests/{id}/approve` | 调课 | 管理员审批（经 /apply-move 真正落到课表） |
| GET | `/api/reschedule/parallel/overview` | 并行排课 | 课程总览（项目 × 组别） |
| POST | `/api/reschedule/parallel/solve` | 并行排课 | 整班 CP-SAT 一键求解 |
| POST | `/api/reschedule/parallel/land/preview` | 方案落地 | 与当前安排表的差异预览（不写入） |
| POST | `/api/reschedule/parallel/land` | 方案落地 | 采纳方案写回安排表（需管理员，落盘可重放） |
| POST | `/api/reschedule/parallel/land/undo` | 方案落地 | 撤销最近一次落地（需管理员） |
| GET | `/api/reschedule/parallel/land/status` | 方案落地 | 当前落地状态（界面撤销入口据此显示） |
| GET | `/api/reschedule/schedule-grid` | 排课可视化 | 周课表网格：某一周的 星期×时段 全景（7 天 × 4 时段，跨节次记录在每个占用的时段都出现，冲突格带 ⚠ 标记） |
| POST | `/api/reschedule/upload-master` | 主数据 | 上传教学大纲/任务清单（从零排课输入） |
| POST | `/api/reschedule/master/import-preview` | 主数据 | 导入向导第 2 步：只读预览台账「页→列→字段」映射/未识别列/统计列/行数，不写任何状态 |
| POST | `/api/reschedule/master/tasks-preview` | 主数据 | 导入向导第 2 步（清单版）：只读预览任务清单列名映射，不写任何状态 |
| GET | `/api/reschedule/master/overview` | 主数据 | 五表主数据（只读） |
| GET | `/api/reschedule/export/master-workbook` | 主数据 | 导出可编辑台账（可导回） |
| GET | `/api/reschedule/data-audit` | 数据看板 | 分级冲突体检 + 并行教学口径说明 |
| GET | `/api/reschedule/export/class-schedule` | 导出 | 班级课表（课程列 + 跨课程撞车） |
| GET | `/api/reschedule/export/room-schedule` | 导出 | 场地课表 |
| POST | `/api/reschedule/admin-login` | 认证 | 管理员登录 |
| POST | `/api/reschedule/experiment-tutor` | 实验规范查询 | 实验规范查询（LLM，面向排课人员/教师） |
| GET | `/health` | - | 健康检查 |

## 测试

```bash
pip install -r requirements.txt -r requirements-dev.txt   # pytest 在 requirements-dev 里
cd backend
python -m pytest tests/ -v
```

Ruff 质量门（与 CI 同一把尺；`ruff.toml` 里每条 ignore 都附了理由）：

```bash
ruff check --config ruff.toml .
```

CI（`.github/workflows/ci.yml`）在每次 push/PR 跑两个 job：**后端**（py3.12 —— ruff → 导入冒烟 → pytest）和**前端**（node 22 —— `npm run verify` ＝ tsc + vitest）。干净检出（无 `schedule.xlsx`）时期望形态是"passed + skipped、零 failed"——真实表用例在设计上就skip。

后端测试不需要联网、不需要 LLM 密钥（`conftest.py` 默认 `LLM_DISABLED=1` /
`RATELIMIT_DISABLED=1`）。真实 `schedule.xlsx` 被 `.gitignore` 排除；没有它时
真实表用例**跳过**（干净检出保持绿）而不是报错。

## 后续规划

- [x] 冲突分级（教室/教师/整组三类核心冲突必须调整；**学生事务永久不做**——不再有"个别学生轻微冲突"这一类，force 强制通过永久不做）
- [x] 主数据增删改 + 持久化 + 台账 Excel 往返
- [x] 并行批量排课 + 灰箱补全
- [x] 班级课表跨课程合并（课程列 + 跨课程撞车标记）
- [x] 全校一键排课（分片 CP-SAT，171/171 覆盖，初稿导出）
- [x] 全校一键组级方案 + 方案落地为正式安排表（差异预览/采纳/撤销/重启重放）
- [x] 排课结果可视化（周课表网格）
- [x] 数据导入向导（只读字段映射预览 → 管理员提交，预览与写库同一份解析代码）
- [x] 课程→项目显式外键（源表带课程列时精确解析，解决同名项目跨课程歧义）
- [ ] 多校数据隔离
- [ ] 实验讲义 RAG 知识库

## 部署与发布

> ⚠️ **发布已永久冻结**（用户 2026-10-23 决定）：线上永远停在 D-9 构建（`index-B6jUBL3e.js`，
> 不含全校一键与方案落地）。本地可继续迭代，但**不再部署、勿再提议发布**；解冻须用户重新明确授权。

线上地址：<https://physics-scheduler.app.workbuddy.host/>

发布采用 **单端口同源** 形态：FastAPI 同时托管 API 与前端产物，前端 `API_BASE = '/api'` 相对路径无需改动。

```bash
# 1) 构建前端（产物必须落在 frontend/webui，不能叫 dist —— 见下方「两个坑」）
cd frontend && npm run build

# 2) 按发布态启动（进程自读 PORT，默认 0.0.0.0:8000）
python run.py
# 或指定端口： PORT=8080 python run.py
```

发布参数：

| 参数 | 值 |
|---|---|
| directory | 本仓库根 |
| language | `python` |
| installCmd | `python3 -m pip install -r backend/requirements.txt` |
| startCmd | `python3 run.py` |
| domainPrefix | `physics-scheduler` |

### 两个必须知道的坑（均为实测踩出）

1. **沙箱不展开 `$PORT`**：`--port $PORT` 会拿到空值，报 `Option '--port' requires an argument`，发布失败（`SITES_START_FAILED`）。端口必须由进程自己读 —— 这就是仓库根 `run.py` 存在的原因，请勿改回 `--port $PORT`。
2. **上传只排除 `node_modules` / `.git` / 构建产物，不排除 `.gitignore` 里的文件**：
   - 前端产物若留在 `dist/` 会被当构建产物**排除**，沙箱内就没有 `index.html`。故 `vite.config.ts` 设 `build.outDir: 'webui'`，`main.py` 的 `FRONTEND_DIST` 指向 `frontend/webui`。
   - `.gitignore` 中的 `schedule.xlsx` **仍会被上传**（其「教师联系表」携带 17 个真实手机号 —— 交付评审 P0-2；`scripts/preflight_release.py` 在它位于仓库根时会拦截发布）。它**就是**存在时的默认数据源 —— 产品决策（`docs/安全风险清单-公开链接数据暴露-20261015.md` R-09）：教秘演示必须呈现真实教师姓名。脱敏表 `schedule.demo.xlsx`（`教师A…`）只在真实表缺失时兜底。要换数据源用 `SCHEDULE_PATH=<路径>`。

### 数据与隐私

- 默认数据源是存在时的**真实表** `schedule.xlsx`（产品决策 R-09：真实教师姓名是教秘演示的业务主体数据），仅在真实表缺失时兜底脱敏表 `schedule.demo.xlsx`（由 `scripts/make_demo_schedule.py` 生成，教师为 `教师A…`，无手机号）。因此线上显示的是真实教师姓名（如吴琳）——**这是预期行为，不是泄露**。教师联系方式（「教师联系表」的手机号/邮箱列）从不进入任何 API 响应，由 `backend/tests/test_privacy_export.py` 锁死。
- 线上核验：`GET /api/reschedule/status` 返回的教师为真实姓名（R-09 产品决策）；若看到化名 `教师A…`，说明回退到了脱敏表（真实表缺失）。
- 云端能力（登录 / 数据 / 大模型）受**服务端 Origin 精确匹配**约束：只有发布域名被放行，其他来源 403。故本地 `localhost` 只能验证公网调用，微信登录须在线上域名下验证。

> 重新发布须由用户明确要求「发布 / 上线」（发布同意不跨轮次延续）。

## 文档

- [English README](README.md)
- [PRD V1.0](docs/PRD-V1.0.md)
- [技术架构设计文档 V1.0](docs/技术架构设计文档-V1.0.md)
- [产品使用指南](docs/产品使用指南.md)
- [项目总结与反思](docs/项目总结与反思.md)
