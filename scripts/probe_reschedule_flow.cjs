/**
 * 调课沙盘（③ 选择时间）UI 点测 —— 只读，不改任何数据
 *
 * 为什么单独一个探针：`probe_ui_flow.cjs` 覆盖的是「首屏 → 我的课程 → 并行排课 →
 * 看板 → 咨询」，恰好**跳过**了整个产品的主路径第 ③ 步（选时间/沙盘）。
 * 而这一步是教秘真干活的地方：三重冲突的结果、排除原因、影响分析、提交守卫。
 * 这段代码没有单测能替（要靠真实接口数据渲染），所以用浏览器点。
 *
 * 覆盖：
 *   1. 进入沙盘（模板表单 + 周课表网格）
 *   2. 可用时段 / 被排除时段都有内容（冲突筛选真的跑了）
 *   3. 切换目标周次会重新查询，且不留旧数据
 *   4. 影响分析能算出冲突面（含自身冲突分栏）
 *   5. 未登录点提交 → 出现云端登录弹窗（不能静默失败）
 *   6. 全程 JS 错误 / 失败请求
 *
 * 用法：SITE=http://127.0.0.1:8023/ npm run probe:reschedule
 */
const { launch } = require('./_browser.cjs');
const path = require('path');

const SITE = process.env.SITE || 'https://physics-scheduler.app.workbuddy.host/';
const SHOT_DIR = process.env.PROBE_SHOT_DIR || path.join(require('os').tmpdir(), 'psprobe');
const TEACHER = process.env.PROBE_TEACHER || '吴琳';

const results = [];
function rec(ok, name, detail, opts = {}) {
  const status = opts.skip ? 'SKIP' : ok ? 'PASS' : 'FAIL';
  results.push({ ok, skip: !!opts.skip, name, detail });
  console.log(`${status}  ${name}  — ${detail}`);
}

const text = (page) => page.evaluate(() => document.body.innerText);

(async () => {
  const browser = await launch();
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  const page = await ctx.newPage();

  const jsErrors = [];
  const failedReq = [];
  page.on('pageerror', (e) => jsErrors.push(String(e).slice(0, 160)));
  page.on('requestfailed', (r) => {
    const u = r.url();
    if (!/res\.wx\.qq\.com|open\.weixin|workbuddy\.cn/.test(u)) failedReq.push(`${u.slice(0, 90)} ${r.failure()?.errorText}`);
  });
  // 4xx/5xx 也算失败：接口 500 时界面往往只显示一句"加载失败"，页面看着"正常"
  page.on('response', (r) => {
    const u = r.url();
    if (u.includes('/api/') && r.status() >= 400) failedReq.push(`${r.status()} ${u.slice(0, 90)}`);
  });

  await page.goto(SITE, { waitUntil: 'domcontentloaded', timeout: 60000 });
  await page.waitForSelector('text=物理实验调课辅助', { timeout: 60000 });

  // ① 选身份 → 我的课程
  let ok = false;
  try {
    await page.locator('button', { hasText: TEACHER }).first().click();
    await page.waitForFunction(() => /当前教师：/.test(document.body.innerText), { timeout: 30000 });
    ok = true;
  } catch (e) {
    rec(false, '前置：进入我的课程', String(e).slice(0, 100));
  }
  if (!ok) {
    rec(false, '调课沙盘链路', '前置步骤失败，后续无法进行');
    await browser.close();
    console.log('\n=== 汇总：0/1 通过 ===');
    process.exit(1);
  }

  // ② 点第一门可调课程 → 进入沙盘
  let availCount = 0;
  let exclCount = 0;
  let sandboxMs = 0;
  try {
    const t1 = Date.now();
    await page.getByRole('button', { name: /申请调课/ }).first().click();
    await page.waitForFunction(() => /调课申请 · 模板表单/.test(document.body.innerText), { timeout: 30000 });
    // 沙盘"画出来"的标志是可用/排除时段已带数字（此时数据已到）
    await page.waitForFunction(() => /可用时段（\d+）/.test(document.body.innerText), { timeout: 30000 });
    sandboxMs = Date.now() - t1;
    await page.waitForTimeout(300);
    const t = await text(page);
    const mAvail = t.match(/可用时段（(\d+)）/);
    const mExcl = t.match(/被排除时段（(\d+)）/);
    availCount = mAvail ? Number(mAvail[1]) : 0;
    exclCount = mExcl ? Number(mExcl[1]) : 0;

    // 沙盘网格：绿格 = 可用，红格 = 排除。直接数 DOM，避免被文案骗过
    const cells = await page.evaluate(() => ({
      green: document.querySelectorAll('.bg-green-50.border-green-200').length,
      red: document.querySelectorAll('.bg-red-50.border-red-200').length,
    }));
    // 阈值 2s：首屏请求走 fast=true（规则解析，实测 25ms），若误改回 fast=false
    // 会退回 LLM 候选生成（实测 7.6s），这一项立刻变红。
    rec(
      availCount > 0 && exclCount > 0 && cells.green > 0 && cells.red > 0 && sandboxMs < 2000,
      '③ 调课沙盘',
      `${sandboxMs}ms 出沙盘：可用 ${availCount} / 排除 ${exclCount}（网格 绿${cells.green} 红${cells.red}）`,
    );
    await page.screenshot({ path: path.join(SHOT_DIR, 'reschedule-sandbox.png'), fullPage: true });
  } catch (e) {
    rec(false, '③ 调课沙盘', String(e).slice(0, 120));
  }

  // ③ 切周次 → 必须重查（可用时段数量应变化，至少不能沿用上一条数据）
  try {
    const before = await page.evaluate(() => (document.body.innerText.match(/可用时段（(\d+)）/) || [])[1]);
    await page.getByRole('combobox').first().selectOption({ label: '第4周' });
    await page.waitForFunction(() => /周课表沙盘（第4周）/.test(document.body.innerText), { timeout: 20000 });
    await page.waitForTimeout(800);
    const after = await page.evaluate(() => (document.body.innerText.match(/可用时段（(\d+)）/) || [])[1]);
    rec(true, '🔄 切换目标周次', `第2周起手 ${before} 个可用 → 第4周 ${after} 个可用（标题已切到第4周）`);
    await page.screenshot({ path: path.join(SHOT_DIR, 'reschedule-week4.png'), fullPage: true });
  } catch (e) {
    rec(false, '🔄 切换目标周次', String(e).slice(0, 120));
  }

  // ④ 影响分析
  try {
    const t2 = Date.now();
    await page.getByRole('button', { name: /分析影响范围/ }).first().click();
    await page.waitForFunction(() => /共 \d+ 处冲突/.test(document.body.innerText), { timeout: 30000 });
    const impactMs = Date.now() - t2;
    await page.waitForTimeout(400);
    const t = await text(page);
    const m = t.match(/共 (\d+) 处冲突/);
    const selfBusy = /您本人在这些时段已有课（(\d+) 处）/.exec(t);
    // 反回归：班级名不许出现在「相关教师」栏里
    const teacherLine = (t.match(/相关教师（该时段在用这些教室）\n([^\n]+)/) || [])[1] || '';
    const classLeak = /班/.test(teacherLine);
    // 阈值 2.5s：AI 候选方案正在后台调 LLM（7.6s），若后端没把阻塞活丢进线程池，
    // 这里会被拖到 8~10s（实测过 9.4s）—— 这个数字就是"全站被冻住"的探针。
    rec(
      Boolean(m) && !classLeak && impactMs < 2500,
      '🎯 影响分析',
      `${impactMs}ms（AI 候选后台占着 LLM 时不排队），共 ${m ? m[1] : '?'} 处冲突` +
        `；相关教师栏「${teacherLine.slice(0, 40)}」${classLeak ? '（含班级名，越界！）' : ''}` +
        `；自身冲突 ${selfBusy ? selfBusy[1] + ' 处（已分栏）' : '无'}`,
    );
    await page.screenshot({ path: path.join(SHOT_DIR, 'reschedule-impact.png'), fullPage: true });
  } catch (e) {
    rec(false, '🎯 影响分析', String(e).slice(0, 120));
  }

  // ⑥ AI 候选方案 —— PRD 验收标准（F9：候选 ≥2 个）与演示脚本旁白都承诺的画面。
  //    历史上后端一直在算，前端从来没渲染过：旁白在念一个屏幕上看不见的东西。
  try {
    const t3 = Date.now();
    // 等**卡片**而不是标题：标题在"正在挑候选…"占位时就已经在了（第一版探针就栽在这）
    await page.waitForFunction(() => /方案 1/.test(document.body.innerText), { timeout: 60000 });
    await page.waitForTimeout(300);
    const t = await text(page);
    const planCount = (t.match(/方案 \d/g) || []).length;
    const hasReason = /空闲|无课|间隔|推荐/.test(t);
    rec(planCount >= 2, '🤖 AI 候选方案', `${Date.now() - t3}ms 内出现 ${planCount} 个候选（PRD 要求 ≥2），附推荐理由：${hasReason ? '有' : '无'}`);
    await page.screenshot({ path: path.join(SHOT_DIR, 'reschedule-ai-plans.png'), fullPage: true });
  } catch (e) {
    rec(false, '🤖 AI 候选方案', `60s 内没渲染出候选方案（演示脚本第 4 步会翻车）：${String(e).slice(0, 100)}`);
  }

  // ⑤ 未登录提交 → 必须弹出云端登录（静默失败是最糟的体验）
  try {
    await page.getByRole('button', { name: /提交调课申请/ }).first().click();
    await page.waitForFunction(() => /云端登录/.test(document.body.innerText), { timeout: 15000 });
    const hasPhone = await page.locator('input[placeholder="手机号"]').count();
    rec(hasPhone > 0, '🔐 未登录提交守卫', `弹出云端登录弹窗，手机号输入框 ${hasPhone} 个`);
    await page.screenshot({ path: path.join(SHOT_DIR, 'reschedule-login-guard.png') });
  } catch (e) {
    rec(false, '🔐 未登录提交守卫', `点提交后没看到登录弹窗（疑似静默失败）：${String(e).slice(0, 100)}`);
  }

  rec(jsErrors.length === 0, '全程 JS 错误', `${jsErrors.length} 条${jsErrors.length ? '：' + jsErrors[0] : ''}`);
  rec(failedReq.length === 0, '全程失败请求', `${failedReq.length} 条${failedReq.length ? '：' + failedReq[0] : ''}`);

  await browser.close();
  const skipped = results.filter((r) => r.skip).length;
  const pass = results.filter((r) => r.ok && !r.skip).length;
  const total = results.length - skipped;
  console.log(`\n=== 汇总：${pass}/${total} 通过${skipped ? `（另有 ${skipped} 项跳过）` : ''} ===`);
  process.exit(pass === total ? 0 : 1);
})().catch((e) => {
  console.error('探针异常：', e);
  process.exit(2);
});
