/**
 * 交付彩排：按《现场演示脚本》逐步走一遍，每步截图 + 计时。
 *
 * 与 probe_ui_flow 的分工：那个是**回归**（断言功能对不对），这个是**彩排**
 * （断言"台上这一屏能不能看、要等多久"）。现场最怕的不是报错，而是：
 *   - 点下去 20 秒没反应，不知道是在算还是卡死；
 *   - 屏幕上是一屏报错/空态，讲稿还在往下讲。
 * 所以每步都记录耗时、截图，最后给一张时间表，讲解节奏照着排。
 *
 * 前置：后端以 DEMO_MODE=1 启动（审批台走本机工单），且已跑 prepare_demo.py。
 * 用法：
 *   SITE=http://127.0.0.1:8023/ node ../scripts/probe_demo_rehearsal.cjs
 *   SITE=... node ../scripts/probe_demo_rehearsal.cjs --with-llm   # 含 ~27 秒的规范查询
 * 输出：默认 .tmp/demo_rehearsal/（可用 SHOT_DIR 覆盖）
 */
const { launch } = require('./_browser.cjs');
const path = require('path');
const fs = require('fs');

const SITE = process.env.SITE || 'http://127.0.0.1:8023/';
const SHOT_DIR = process.env.SHOT_DIR || path.join(process.cwd(), '..', '.tmp', 'demo_rehearsal');
const WITH_LLM = process.argv.includes('--with-llm');

const steps = [];

function rec(name, ok, ms, shot, detail) {
  steps.push({ name, ok, ms, shot, detail });
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}  —  ${(ms / 1000).toFixed(1)}s` +
    (detail ? `，${detail}` : '') + (shot ? `  [${path.basename(shot)}]` : ''));
}

async function shoot(page, name) {
  fs.mkdirSync(SHOT_DIR, { recursive: true });
  const p = path.join(SHOT_DIR, name);
  await page.screenshot({ path: p, fullPage: false });
  return p;
}

(async () => {
  fs.mkdirSync(SHOT_DIR, { recursive: true });
  const browser = await launch();
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  page.setDefaultTimeout(60000);

  async function timed(name, fn, shotName) {
    const t0 = Date.now();
    try {
      const detail = await fn();
      const shot = shotName ? await shoot(page, shotName) : null;
      rec(name, true, Date.now() - t0, shot, detail || '');
    } catch (e) {
      const shot = shotName ? await shoot(page, shotName.replace('.png', '-FAIL.png')) : null;
      rec(name, false, Date.now() - t0, shot, String(e).slice(0, 100));
    }
  }

  const nav = (re) => page.locator('header').getByRole('button', { name: re }).first();

  // ① 首屏
  await timed('① 首屏（身份选择）', async () => {
    await page.goto(SITE, { waitUntil: 'domcontentloaded' });
    // 教师卡片是异步加载的：只等"请选择身份"会数到 0 张（踩过）
    await page.waitForFunction(
      () => (document.body.innerText.match(/进入我的课程/g) || []).length >= 13,
      null, { timeout: 40000 });
    const txt = await page.evaluate(() => document.body.innerText);
    const teachers = (txt.match(/进入我的课程/g) || []).length;
    return '13 位真实教师';
  }, '01-identity.png');

  // ② 一键自动排课（全校 171）：现场 35–45 秒，讲稿要留话
  await timed('② 一键自动排课（全校 171）', async () => {
    await page.getByRole('button', { name: /一键自动排课/ }).first().click();
    // 等"成功 X/Y 个教学任务"出现，再解析数字；不假设一定是 171/171（如实读）
    await page.waitForFunction(() => /成功\s*\d+\/\d+\s*个教学任务/.test(document.body.innerText),
      null, { timeout: 180000 });
    const txt = await page.evaluate(() => document.body.innerText);
    const m = txt.match(/成功\s*(\d+)\/(\d+)\s*个教学任务/);
    if (!m) throw new Error('未见结果摘要');
    if (m[1] !== m[2]) throw new Error(`只排定 ${m[1]}/${m[2]}`);
    if (!/未校验/.test(txt)) throw new Error('缺"未校验"声明');
    return `${m[1]}/${m[2]} 排定（含未校验声明）`;
  }, '02-auto-schedule.png');

  // ③ 我的课程（点真实教师）
  await timed('③ 我的课程（吴琳）', async () => {
    const respPromise = page.waitForResponse(
      (r) => r.url().includes('/courses') && r.status() === 200, { timeout: 40000 });
    await page.getByRole('button', { name: /吴琳/ }).first().click();
    await respPromise;
    await page.waitForFunction(() => /可调课程/.test(document.body.innerText), null, { timeout: 30000 });
    return '可调/不可调分组已渲染';
  }, '03-courses.png');

  // ④ 排课调课入口
  await timed('④ 排课调课入口', async () => {
    await nav(/排课调课/).click();
    await page.waitForFunction(() => /选择时间|候选时段|调课/.test(document.body.innerText), null, { timeout: 30000 });
    return '入口页已打开';
  }, '04-reschedule.png');

  // ⑤ 审批台（演示模式：本地口令，无需云登录）
  await timed('⑤ 审批台（3 条待办）', async () => {
    await nav(/审批/).click();
    await page.waitForFunction(() => !!document.querySelector('input[placeholder="账号"]'), null, { timeout: 30000 });
    await page.fill('input[placeholder="账号"]', 'admin');
    await page.fill('input[placeholder="密码"]', 'admin123');
    await page.getByRole('button', { name: /^登录$/ }).first().click();
    await page.waitForFunction(() => /待审批申请：\d+ 条/.test(document.body.innerText), null, { timeout: 30000 });
    const txt = await page.evaluate(() => document.body.innerText);
    const m = txt.match(/待审批申请：(\d+) 条/);
    if (!m || Number(m[1]) < 1) throw new Error('审批台没有待办，演示会空场');
    return `待审批 ${m[1]} 条`;
  }, '05-admin.png');

  // ⑥ 审批通过 → 课表真的变了（演示模式走本机 /requests/{id}/approve）
  await timed('⑥ 同意一条 → 课表落地', async () => {
    const before = await page.evaluate(() => document.body.innerText.length);
    const btn = page.getByRole('button', { name: /^同意$/ }).first();
    if (!(await btn.count())) throw new Error('没有可点的「同意」按钮');
    await btn.click();
    // 只认"已自动应用调课"这句：页面到处都是"调课"二字，用宽松正则会立刻命中（假通过）
    // 真实文案是「已同意并自动应用调课：N 条记录」——正则要容下"同意并"
    await page.waitForFunction(() => /已(同意并)?自动应用调课/.test(document.body.innerText), null, { timeout: 90000 });
    const txt = await page.evaluate(() => document.body.innerText);
    const m = txt.match(/已(?:同意并)?自动应用调课：(\d+) 条记录/);
    if (!m) throw new Error('未看到"已自动应用调课：N 条"');
    return `已自动应用 ${m[1]} 条（课表已被改写）`;
  }, '06-approved.png');

  // ⑦ 并行排课 → 一键排全校（17 班）
  await timed('⑦ 一键排全校（17 班）', async () => {
    await nav(/并行排课/).click();
    await page.waitForFunction(() => document.body.innerText.includes('2024级'), null, { timeout: 40000 });
    await page.getByRole('button', { name: /一键排全校/ }).first().click();
    await page.waitForFunction(() => /全校一键排课 · 17\/17 个班排定/.test(document.body.innerText),
      null, { timeout: 180000 });
    return '17/17 班，逐班结果表';
  }, '07-school-plan.png');

  // ⑧ 数据看板
  await timed('⑧ 数据看板', async () => {
    await nav(/数据看板/).click();
    await page.waitForFunction(() => /共 \d+ 条安排\s*｜\s*\d+ 位教师/.test(document.body.innerText),
      null, { timeout: 60000 });
    const txt = await page.evaluate(() => document.body.innerText);
    const m = txt.match(/共 (\d+) 条安排\s*｜\s*\d+ 位教师/);
    return `${m ? m[1] : '?'} 条安排`;
  }, '08-dashboard.png');

  // ⑨ 周课表
  await timed('⑨ 周课表（网格）', async () => {
    await nav(/周课表/).click();
    await page.waitForFunction(() => /周课表/.test(document.body.innerText) && /第\s*\d+\s*周|时段/.test(document.body.innerText),
      null, { timeout: 40000 });
    return '周网格已渲染';
  }, '09-grid.png');

  // ⑩ 主数据
  await timed('⑩ 主数据（五张表）', async () => {
    await nav(/主数据/).click();
    await page.waitForFunction(() => /实验项目/.test(document.body.innerText) && /学生组|班级/.test(document.body.innerText),
      null, { timeout: 40000 });
    return '五张表可见';
  }, '10-master.png');

  // ⑪ 实验规范查询（LLM，可选）
  if (WITH_LLM) {
    await timed('⑪ 实验规范查询（LLM ~27s）', async () => {
      await nav(/实验规范查询/).click();
      await page.waitForFunction(() => !!document.querySelector('textarea'), null, { timeout: 40000 });
      await page.fill('textarea', '示波器使用前要注意什么？');
      await page.getByRole('button', { name: /提问|发送|开始/ }).first().click();
      await page.waitForFunction(() => {
        const t = document.body.innerText;
        return /示波器/.test(t) && t.length > 600;
      }, null, { timeout: 120000 });
      return '回复已渲染（等 25–30 秒属正常）';
    }, '11-tutor.png');
  }

  await browser.close();

  const total = steps.reduce((a, s) => a + s.ms, 0);
  console.log('\n' + '='.repeat(58));
  console.log('彩排时间表（不含讲解）');
  console.log('='.repeat(58));
  for (const s of steps) {
    console.log(`${s.ok ? '✓' : '✗'} ${s.name.padEnd(28, ' ')}${(s.ms / 1000).toFixed(1).padStart(6)}s`);
  }
  console.log('-'.repeat(58));
  console.log(`合计 ${(total / 1000).toFixed(1)} 秒（${steps.filter(s => s.ok).length}/${steps.length} 步通过）`);
  const bad = steps.filter(s => !s.ok);
  if (bad.length) {
    console.log('失败步骤：' + bad.map(s => s.name).join('、'));
    process.exitCode = 1;
  } else {
    console.log('截图目录：' + SHOT_DIR);
  }
})();
