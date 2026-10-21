/**
 * 线上主流程 UI 点测（只读，不改任何数据）
 * 覆盖：首屏 → 我的课程(F1/F2) → 并行排课(总览+一键排课) → 数据看板 → 选课咨询(懒加载)
 *      → 懒加载 chunk 失败降级（故意 abort 一个按需 chunk，验证不白屏）
 */
const { chromium } = require('playwright-core');
const path = require('path');

const SITE = process.env.SITE || 'https://physics-scheduler.app.workbuddy.host/';
const CHROME = 'C:\\Users\\Lenovo\\AppData\\Local\\ms-playwright\\chromium-1243\\chrome-win64\\chrome.exe';
const SHOT_DIR = 'C:\\Users\\Lenovo\\AppData\\Local\\Temp\\psprobe';

const results = [];
function rec(ok, name, detail) {
  results.push({ ok, name, detail });
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}  — ${detail}`);
}

(async () => {
  const browser = await chromium.launch({
    executablePath: CHROME,
    headless: true,
    args: ['--no-proxy-server', '--disable-gpu'],
  });
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  const page = await ctx.newPage();

  const jsErrors = [];
  const failedReq = [];
  page.on('pageerror', (e) => jsErrors.push(String(e).slice(0, 160)));
  page.on('requestfailed', (r) => {
    const u = r.url();
    if (!/res\.wx\.qq\.com|open\.weixin|workbuddy\.cn/.test(u)) failedReq.push(`${u.slice(0, 90)} ${r.failure()?.errorText}`);
  });

  const t0 = Date.now();
  await page.goto(SITE, { waitUntil: 'domcontentloaded', timeout: 60000 });
  // 注意：document.title 是「物理学科智能排课选课系统」，但页面可见标题是「物理实验调课辅助」
  await page.waitForSelector('text=物理实验调课辅助', { timeout: 60000 });
  const loadMs = Date.now() - t0;

  // 等教师列表加载完成（首屏 init）
  let teacherCount = 0;
  try {
    await page.waitForFunction(() => {
      const names = ['吴琳','吴琼','张妹玉','林晨','林珠云','柯达','洪锦泉','翁铭华','肖刚','郑辉','鄢仁文','钟蔚','陈胜钰'];
      const btns = Array.from(document.querySelectorAll('button'));
      return btns.filter((b) => names.some((n) => (b.textContent || '').includes(n))).length >= 13;
    }, { timeout: 45000 });
    teacherCount = await page.evaluate(() => {
      const names = ['吴琳','吴琼','张妹玉','林晨','林珠云','柯达','洪锦泉','翁铭华','肖刚','郑辉','鄢仁文','钟蔚','陈胜钰'];
      return Array.from(document.querySelectorAll('button')).filter((b) => names.some((n) => (b.textContent || '').includes(n))).length;
    });
    rec(teacherCount >= 13, '首屏教师列表', `渲染 ${teacherCount}/13 位真实教师，首屏 ${loadMs}ms`);
  } catch (e) {
    rec(false, '首屏教师列表', `45s 内未渲染出教师列表：${String(e).slice(0, 80)}`);
  }

  // F2：点第一位教师 → 我的课程
  try {
    const t1 = Date.now();
    await page.locator('button', { hasText: '吴琳' }).first().click();
    await page.waitForFunction(() => /当前教师：/.test(document.body.innerText), { timeout: 30000 });
    await page.waitForTimeout(800);
    const bodyLen = await page.evaluate(() => document.body.innerText.length);
    rec(bodyLen > 800, '② 我的课程', `进入课程页 ${Date.now() - t1}ms，页面文本 ${bodyLen} 字`);
    await page.screenshot({ path: path.join(SHOT_DIR, 'flow-courses.png') });
  } catch (e) {
    rec(false, '② 我的课程', String(e).slice(0, 100));
  }

  // 并行排课：总览 + 一键排课
  try {
    await page.getByRole('button', { name: /并行排课/ }).first().click();
    await page.waitForFunction(() => document.body.innerText.includes('2024级'), { timeout: 40000 });
    const classCount = await page.evaluate(() => (document.body.innerText.match(/2024级/g) || []).length);
    rec(classCount > 5, '⚡ 并行排课 · 课程总览', `渲染出班级卡片（"2024级" 出现 ${classCount} 次）`);
    await page.screenshot({ path: path.join(SHOT_DIR, 'flow-parallel.png') });

    const t2 = Date.now();
    const btn = page.getByRole('button', { name: /^一键排课$/ }).first();
    await btn.click();
    // 结果区渲染的是「📋 排课方案（周五）· 状态 OPTIMAL」+「24/24 个单元已排定」
    // 注意：不能用"周次"之类的泛词做等待条件 —— 页面本来就有这些字，会立刻命中造成假通过。
    await page.waitForFunction(
      () => /个单元已排定/.test(document.body.innerText),
      { timeout: 150000 },
    );
    await page.waitForTimeout(600);
    const txt = await page.evaluate(() => document.body.innerText);
    const ok = /个单元已排定/.test(txt) && /状态\s*(OPTIMAL|FEASIBLE)/.test(txt);
    const m = txt.match(/(\d+)\/(\d+)\s*个单元已排定/);
    rec(ok, '⚡ 一键排课（CP-SAT）', `${Date.now() - t2}ms 返回，${m ? `${m[1]}/${m[2]} 单元已排定` : '未见结果摘要'}`);
    await page.screenshot({ path: path.join(SHOT_DIR, 'flow-solve.png') });
  } catch (e) {
    rec(false, '⚡ 并行排课链路', String(e).slice(0, 120));
  }

  // 数据看板
  try {
    await page.getByRole('button', { name: /数据看板/ }).first().click();
    await page.waitForFunction(
      () => /数据体检|冲突|统计/.test(document.body.innerText),
      { timeout: 40000 },
    );
    await page.waitForTimeout(1000);
    const hasAudit = await page.evaluate(() => /数据体检|健康|冲突/.test(document.body.innerText));
    rec(hasAudit, '📊 数据看板', '体检/统计内容已渲染');
    await page.screenshot({ path: path.join(SHOT_DIR, 'flow-dashboard.png') });
  } catch (e) {
    rec(false, '📊 数据看板', String(e).slice(0, 100));
  }

  // 选课咨询（懒加载视图）：验证按需 chunk 真的能到达且正常渲染。
  // 等待条件必须只在目标视图里出现 —— "选课咨询" 是导航按钮文案，页面一进来就有，
  // 用它会立刻命中造成假通过；"咨询选课资格" 只出现在 ConsultView 的说明文字里。
  try {
    await page.getByRole('button', { name: /选课咨询/ }).first().click();
    await page.waitForFunction(
      () => /咨询选课资格/.test(document.body.innerText),
      { timeout: 30000 },
    );
    const inputCount = await page.locator('input[placeholder*="为什么我选不了"]').count();
    rec(inputCount > 0, '🎓 选课咨询（懒加载）', `视图已渲染，提问框 ${inputCount} 个`);
    await page.screenshot({ path: path.join(SHOT_DIR, 'flow-consult.png') });
  } catch (e) {
    rec(false, '🎓 选课咨询（懒加载）', String(e).slice(0, 120));
  }

  // ⑨ 懒加载 chunk 取不到时的降级：这条是**故意制造故障**，因此单独开一个上下文，
  // 不污染上面的"JS 错误/失败请求"计数（那两个必须反映**正常路径**）。
  //
  // 要证明的是：`import()` 失败不会导致整页白屏。没有错误边界时，React 18 会把整棵树
  // 卸载 —— 连顶部导航一起消失，用户只能干瞪眼。有了边界，内容区出提示卡、导航还在，
  // 教秘可以立刻切到别的功能继续干活。
  try {
    const ctx2 = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
    const p2 = await ctx2.newPage();
    await p2.route('**/ParallelView-*.js', (route) => route.abort());
    await p2.goto(SITE, { waitUntil: 'domcontentloaded', timeout: 60000 });
    await p2.waitForSelector('text=物理实验调课辅助', { timeout: 60000 });
    await p2.getByRole('button', { name: /并行排课/ }).first().click();
    await p2.waitForFunction(
      () => /页面资源已更新|这个模块遇到了问题/.test(document.body.innerText),
      { timeout: 30000 },
    );
    const txt2 = await p2.evaluate(() => document.body.innerText);
    // 顶部导航是边界的兄弟节点，必须仍然可用 —— 这才是"降级"与"白屏"的区别
    const navAlive = /数据看板/.test(txt2) && /选课咨询/.test(txt2);
    const hasCard = /重新加载|重试/.test(txt2);
    rec(navAlive && hasCard, '🛟 懒加载失败降级', `chunk 中断后显示提示卡（${hasCard ? '有' : '无'}）且顶部导航仍可用（${navAlive ? '是' : '否'}=非白屏）`);
    await p2.screenshot({ path: path.join(SHOT_DIR, 'flow-chunk-fallback.png') });
    await ctx2.close();
  } catch (e) {
    rec(false, '🛟 懒加载失败降级', `chunk 被中断后未出现降级提示（疑似白屏）：${String(e).slice(0, 100)}`);
  }

  rec(jsErrors.length === 0, '全程 JS 错误', `${jsErrors.length} 条${jsErrors.length ? '：' + jsErrors[0] : ''}`);
  rec(failedReq.length === 0, '全程失败请求', `${failedReq.length} 条${failedReq.length ? '：' + failedReq[0] : ''}`);

  await browser.close();
  const pass = results.filter((r) => r.ok).length;
  console.log(`\n=== 汇总：${pass}/${results.length} 通过 ===`);
  process.exit(pass === results.length ? 0 : 1);
})().catch((e) => {
  console.error('探针异常：', e);
  process.exit(2);
});
