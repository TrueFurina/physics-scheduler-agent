# 深刻质检与交付侧评审报告

**对象**：`E:\Program\physics-scheduler-agent`（物理实验排课 Agent 系统）
**评审日期**：2026-10-24
**评审方式**：静态代码审计 + 干净环境复现 + 线上黑盒实测（非只读 README）

---

## 一、结论先行

| 维度 | 评价 |
|---|---|
| 功能真实度 | ★★★★★ 排课/冲突/落地/导出链路真实可跑，线上可用 |
| 测试可信度 | ★★★★☆ 380 后端 + 85 前端实测全绿，且审计脚本确实独立 |
| 文档完备度 | ★★★★★ 22 轮修订、缺陷编号、复现与修复均留痕 |
| **可复现性（交付）** | **★★☆☆☆ 按 README 从零安装即启动失败** |
| 数据安全（交付） | ★★☆☆☆ 仓库携带 17 个真实手机号，依赖"发布器会带上去"的机制 |
| 可维护性 | ★★★☆☆ 2460 行单文件 + 48 端点 |

**一句话**：这是一个**工程质量远高于同类的真实项目**（独立审计脚本、缺陷编号化治理、线上数据体检与文档逐字吻合），但**"从零复现"这条最基本的交付路径是断的**——评审方第一件事 `pip install -r && python run.py` 会直接失败。这是交付前必须修的第一顺位否决项。

---

## 二、阻断级问题（P0，交付前必须修）

### P0-1　`requirements.txt` 缺 `python-multipart`，按文档安装后服务无法启动

**证据（两次独立复现）**

```
# 干净 venv，仅按 README 的 installCmd 安装
$ python -m venv cleanenv && ./cleanenv/bin/pip install -r backend/requirements.txt
$ python -c "from app.main import app"
IMPORT FAILED: RuntimeError Form data requires "python-multipart" to be installed.
```

- 代码在 8 个端点使用 `UploadFile = File(...)`（`/upload`、`/upload-master`、`/master/import-preview`、`/master/tasks-preview`、`/master/import` 等）。
- 首次跑 pytest 时，**23 个测试文件在收集阶段直接 ERROR**，症状就是这一个缺包。
- 补装 `python-multipart` 后：`IMPORT OK`，且 `380 passed`。

**影响**

1. 客户/评审方照 README 操作 = 100% 启动失败；
2. 发布参数 `installCmd` 依赖该文件，线上能跑只说明**沙箱基线环境里恰好预装了它**，不是依赖声明正确；
3. `scripts/preflight_release.py` 只检查 `requirements.txt` **文件是否存在**，不校验可导入，因此"预检全绿"也发现不了。

**修复**：`requirements.txt` 增加 `python-multipart==0.0.20`；preflight 增加一条 `python -c "import app.main"` 的导入检查。

---

### P0-2　真实数据文件携带 17 个教师手机号，且发布机制会把它上传

**证据**

```
schedule.xlsx  sheets: [任课老师安排表, 大学物理实验安排表(周), 大学物理实验安排表(班级), 教师联系表, 学生所在系系办电话]
  教师联系表  → 手机号数 = 17   （如 13338250337 / 18046092296 / 13905926839）
schedule.demo.xlsx（脱敏版）→ 手机号数 = 0，姓名已化为 教师H / 教师Q / 教师C
```

而 README 自己写明：

> 上传器只排除 `node_modules` / `.git` / 构建产物 —— **不读 `.gitignore`**
> The gitignored `schedule.xlsx` **is still uploaded**

**当前实际暴露面（已实测，未误报）**

- `GET /export/master-data`（匿名）→ 导出 xlsx 解析后 **手机号 0 命中** ✅
- `GET /teachers`（匿名）→ 只返回 `{name, title}`，无手机号 ✅
- `/.env`、`/README.md` → 返回 SPA 首页回退页（644B），**不是真实文件** ✅ 路径穿越防护有效

**结论**：手机号**目前未通过任何 HTTP 端点泄露**，但 17 个真实手机号以明文形式存在于会被上传的目录中，属于"已上膛未击发"的风险。任何后续新增的"原始表下载"能力、或沙箱内的路径遍历/日志误读，都会直接引爆。

**修复**：发布前把 `schedule.xlsx` 从上传目录物理移除（或移到沙箱外），并给 `教师联系表` 增加脱敏导出的单元测试锁死。

---

### P0-3　测试硬依赖未入库的真实数据，缺失时不是 skip 而是 ERROR

**证据**

```
$ mv schedule.xlsx /tmp && python -m pytest tests/test_reschedule.py
1 passed, 13 errors in 0.85s     # FileNotFoundError: .../schedule.xlsx
```

- 项目里已有正确写法（`test_conflict_grading.py` 用 `pytest.skip("仓库根没有 schedule.xlsx")`）。
- 但 `test_reschedule.py` 的 13 个用例是**在模块级读文件**，缺失即 `ERROR`。

**影响**：干净检出（CI / 新机器 / 评审方）跑测试 = **红**，与 README "371 backend tests" 的绿状态承诺不符。

**修复**：统一改用 conftest 的 `pytest.skip` 或 fixture 提供合成数据。

---

## 三、重要问题（P1）

### P1-1　文档数字与实测不符

| README 声称 | 实测 | 判定 |
|---|---|---|
| 371 backend tests | **380 passed** | 少报 9 条（无害但不严谨） |
| 85 frontend tests | 85 用例 / 9 文件 | ✅ 完全一致 |
| 默认数据集为脱敏 demo | demo 表确为 `教师H/Q/C`、0 手机号 | ✅ 属实 |
| "线上与本地数据一致" | 线上返回真实姓名 吴琳、吴琼… | ⚠️ 与"默认脱敏"并列阅读易生误解 |

### P1-2　25/48 端点免管理员鉴权，含全量导出

实测审计（装饰器级，含跨行参数）：

```
总端点 48；挂 _ADMIN_ONLY 23；无鉴权 25
无鉴权且能整表导出：
  GET /export/class-schedule   (41KB，17 班 433 行)
  GET /export/room-schedule
  GET /export/master-workbook  (13.6KB xlsx)
  GET /export/master-data      (11.2KB xlsx)
  POST /parallel/land/preview
  POST /auto-schedule/export
```

- 文档《安全风险清单》明确写"**刻意保留免鉴权**"，理由是"教秘演示主路径"，并已有单测锁定。
- 但**匿名批量下载 433 条真实排课 + 13 位真实教师姓名**已超出"演示主路径"的合理边界。

**交付建议**：不作为缺陷，但必须在验收文档里**显式列出此项为"客户已接受的风险"**，并给出开关（如 `PUBLIC_READONLY=0` 一键收紧导出）。当前形态下"客户以为有鉴权"的概率很高。

### P1-3　无用依赖与缺失的测试依赖

```
requirements.txt 声明：pypdf==6.19.0, PyMuPDF==1.28.2
全仓检索 pypdf / fitz / PdfReader → 0 命中          # 纯增安装体积与供应链面
pytest 未在任何 requirements 中声明                 # 测试步骤缺文档化依赖
```

### P1-4　无 lint / CI 配置

仓库无 `pyproject.toml` / `ruff.toml` / `eslint` / `.github`。以 ruff 默认规则扫描：**631 条告警**（463 条可自动修）。项目确实有 `.ruff_cache` 目录，说明作者本地跑过 ruff，但**规则集与阈值没入库**，等于质量门不可复现。

---

## 四、建议项（P2）

1. **`_same_teacher` 用子串包含判同一人**
   ```python
   return a == b or a in b or b in a
   ```
   当前 13 位教师姓名实测**互不为子串**，故不误判。但一旦出现"李娜 / 李娜娜"，会把两位教师判成同一人 → **假冲突**（或反向漏判）。建议归一化后精确匹配 + 显式别名表。

2. **`reschedule.py` 2460 行 / 48 端点单文件**：这是典型的"上帝文件"，任一端点改动都需通读上下文，回归成本高。建议按 `master / export / parallel / requests` 拆 router。

3. **构建产物隐式约定**：`vite.config.ts` 的 `outDir: 'webui'`（而非默认 `dist`）是为绕开"dist 被当构建产物排除"的坑。实测当前 `dist` 已不存在、`webui` 在用，状态正确，但这是**隐式约定**，新人极易改回 `dist` 后"本地正常、线上白屏"。

4. **`/master-status` 返回未排样本时带班级名**（如 `大物实验B班级`）：无学生 PII，属可接受，但建议在验收文档中注明读接口的数据边界。

---

## 五、扎实的地方（应保留、应写进交付说明）

1. **测试是"真测试"，不是数量堆砌**：388→380 用例中大量是反向断言与"假绿"防御。文档记载 R-25 —— 探针原本吃静态副标题，`abort` 掉接口照样 PASS，"前七批该票无效"，作者主动否定自己前七轮成果并改写断言。这种诚实度在同类项目中极少见。

2. **独立审计脚本确实独立**：`audit_conflicts.py` 不复用后端解析器，直接 `openpyxl` 读原始 xlsx + 真实时钟区间判重叠；`verify_graybox` 依赖它而非依赖 `app.*`。检索确认 `scripts/*.py` 中**仅 `preflight_release.py`** 引用了 `app.`（用于路径常量），符合"独立复核"的设计声明。

3. **线上行为与文档逐字吻合**（可作验收基线）：
   ```
   GET /api/reschedule/data-audit
   → healthy:false, total_entries:433, teacher_conflict_count:1,
     group_conflict_count:0, room_conflict_count:0, parallel_group_count:47
   ```
   README 与《优化路线》对"47 处同班不同组并行教学不得报假阳性"的承诺，线上**实测为 47**，完全一致。

4. **线上可用性**：`/health` 200，`solver.status=ok (504ms)`，`data.loaded=true, total_entries=433`；首页版本 `index-B6jUBL3e.js` 与 README 所述 D-9 一致，未偷偷发布新版本。

5. **安全设计的判断是清醒的**：`security.py` 用 HMAC 自签 + `hmac.compare_digest` 定时比较 + fail-closed + "密钥不落盘"（明确说明 `backend/app/data/` 会随包上传，所以不写那里）；`_extract_tokens` 还处理了"边缘代理注入 Authorization 头导致刚登录就 401"的真实坑。这说明作者理解威胁模型，不是抄模板。

---

## 六、交付前动作清单（建议按序执行）

| # | 动作 | 对应问题 | 工作量 |
|---|---|---|---|
| 1 | `requirements.txt` 补 `python-multipart` | P0-1 | 1 行 |
| 2 | preflight 增加 `import app.main` 冒烟检查 | P0-1 | 10 行 |
| 3 | 发布前移除上传目录中的 `schedule.xlsx`，并加脱敏导出单测 | P0-2 | 半天 |
| 4 | `test_reschedule.py` 缺数据改 `skip` | P0-3 | 半小时 |
| 5 | README 测试数改 380，补"线上为真实姓名数据"说明 | P1-1 | 10 分钟 |
| 6 | 验收文档显式列出"25 个免鉴权读/导出端点 —— 客户已接受" | P1-2 | 文档 |
| 7 | 删 `pypdf` / `PyMuPDF`；新增 `requirements-dev.txt`（pytest） | P1-3 | 10 分钟 |
| 8 | 入库 `ruff` 配置与 CI（先 `--fix` 收敛 463 条） | P1-4 | 1 天 |

---

## 七、可复现的验收命令（交给客户自证）

```bash
# 1) 干净环境
python -m venv venv && ./venv/bin/pip install -r backend/requirements.txt
python -c "import sys;sys.path.insert(0,'backend');from app.main import app;print('OK',len(app.routes))"
#   当前预期：RuntimeError python-multipart   ← P0-1

# 2) 后端测试
cd backend && python -m pytest tests/ -q
#   当前预期（有 schedule.xlsx）：380 passed
#   当前预期（无 schedule.xlsx）：13 errors   ← P0-3

# 3) 线上数据基线
curl -s https://physics-scheduler.app.workbuddy.host/api/reschedule/data-audit
#   预期：total_entries=433, teacher_conflict_count=1, parallel_group_count=47
```

---

## 八、总体评分

| 项 | 分 | 说明 |
|---|---|---|
| 能否直接交付给客户使用 | 6/10 | 线上可用，但本地从零起不来 |
| 能否通过技术评审（可复现性） | 4/10 | P0-1 + P0-3 会被当场打回 |
| 文档与实现一致性 | 8/10 | 数字微差、隐私表述需澄清 |
| 工程质量与诚实度 | 9/10 | 独立审计 + 自我否定前七批，罕见 |
| 安全设计 | 7/10 | 模型清醒，但仓库载体存真实手机号 |

**修完 P0-1 与 P0-3 两项共约 1 小时工作量，可复现性分数从 4 → 9。**
这是一个"只差最后一公里"的项目：内核扎实、留痕完整，但交付包装上有两个 1 小时就能补掉的漏斗。
