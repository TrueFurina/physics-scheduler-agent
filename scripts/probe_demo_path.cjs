/**
 * 线上演示路径探针（无凭据 · 只读观察）
 * ------------------------------------------------------------------
 * 用途：演示/评审前快速确认「页面能否跑、13 位真实教师姓名是否在、登录三法是否可用，
 *       微信扫码是否端到端接通」。不提交任何凭据、不发送短信、不消耗 LLM 额度。
 *
 * 运行（必须用受管 Node，并指向已安装 playwright-core 的 workspace）：
 *   cd C:/Users/Lenovo/.workbuddy/binaries/node/workspace
 *   NODE_PATH="C:/Users/Lenovo/.workbuddy/binaries/node/workspace/node_modules" \
 *     "C:/Users/Lenovo/.workbuddy/binaries/node/versions/22.22.2-6/node.exe" \
 *     E:/Program/physics-scheduler-agent/scripts/probe_demo_path.cjs
 *
 * 退出码：0 = 全部关键断言通过；1 = 通过但不完整（如微信未接通）；2 = 运行失败
 * ------------------------------------------------------------------
 */
const { chromium } = require('playwright-core');
const fs = require('fs');
const path = require('path');

const SITE = process.env.PROBE_URL || 'https://physics-scheduler.app.workbuddy.host/';
const EXE = process.env.PROBE_CHROME ||
  'C:/Users/Lenovo/AppData/Local/ms-playwright/chromium-1243/chrome-win64/chrome.exe';
const SHOT_DIR = process.env.PROBE_SHOT_DIR || path.join(require('os').tmpdir(), 'psprobe');
const TEACHERS = ['吴琳', '吴琼', '张妹玉', '林晨', '林珠云', '柯达', '洪锦泉', '翁铭华', '肖刚', '郑辉', '鄢仁文', '钟蔚', '陈胜钰'];

(async () => {
  const browser = await chromium.launch({
    executablePath: EXE, headless: true,
    args: ['--no-proxy-server', '--ignore-certificate-errors'],
  });
  const ctx = await browser.newContext({ locale: 'zh-CN', viewport: { width: 1440, height: 900 } });
  const page = await ctx.newPage();

  const jsErrors = [];
  const failedReq = [];
  const net = [];
  page.on('pageerror', (e) => jsErrors.push('pageerror: ' + e.message));
  page.on('console', (m) => { if (m.type() === 'error') jsErrors.push('console: ' + m.text().slice(0, 200)); });
  page.on('requestfailed', (r) => failedReq.push(r.url().slice(0, 140) + ' :: ' + ((r.failure() || {}).errorText || '')));
  page.on('response', (r) => {
    const u = r.url();
    if (/oauth|weixin|wx\.qq|genie-baas/i.test(u)) net.push(r.status() + ' ' + r.request().method() + ' ' + u.slice(0, 170));
  });

  const checks = [];
  const ok = (name, pass, detail) => { checks.push({ name, pass, detail }); console.log((pass ? 'PASS  ' : 'FAIL  ') + name + (detail ? '  — ' + detail : '')); };

  // 1) 页面加载
  const t0 = Date.now();
  await page.goto(SITE, { waitUntil: 'networkidle', timeout: 90000 });
  await page.waitForTimeout(3000);
  ok('页面加载', true, (Date.now() - t0) + 'ms, title=' + (await page.title()));

  const body = await page.locator('body').innerText();
  // 2) 真实教师姓名
  const hit = TEACHERS.filter((n) => body.includes(n));
  ok('13 位真实教师姓名', hit.length === TEACHERS.length, hit.length + '/' + TEACHERS.length);
  ok('无化名残留', !/教师[A-Z]/.test(body), '教师[A-Z]=' + /教师[A-Z]/.test(body));
  // 3) 加载期无错误
  ok('加载期 JS 错误', jsErrors.length === 0, jsErrors.length + ' 条');
  ok('加载期失败请求', failedReq.length === 0, failedReq.length + ' 条');

  // 4) 登录入口 + 三种方式
  await page.getByText('云端登录').first().click({ timeout: 8000 });
  await page.waitForTimeout(1500);
  const dlg = await page.locator('body').innerText();
  const methods = ['获取验证码', '登录', '微信扫码登录'].filter((m) => dlg.includes(m));
  ok('登录弹窗三法', methods.length >= 3, JSON.stringify(methods));

  // 5) 表单校验（不发送短信）
  try {
    await page.getByRole('button', { name: /登录\s*\/\s*注册/ }).first().click({ timeout: 5000 });
    await page.waitForTimeout(1200);
    const t = await page.locator('body').innerText();
    ok('空手机号校验', /请先获取|请输入|验证码/.test(t), '中文提示且无崩溃');
  } catch (e) { ok('空手机号校验', false, e.message.slice(0, 120)); }

  // 6) 微信扫码端到端
  try {
    await page.getByText('微信扫码登录', { exact: true }).first().click({ timeout: 8000 });
    await page.waitForTimeout(7000);
    const after = await page.locator('body').innerText();
    const authz = net.find((x) => /oauth\/authorize/.test(x));
    const qrconnect = net.find((x) => /qrconnect/.test(x));
    ok('微信 · oauth 授权跳转', !!authz, authz || '(未观察到)');
    ok('微信 · qrconnect+appid', !!qrconnect, qrconnect || '(未观察到)');
    ok('微信 · 扫码界面可见', /扫一扫|扫码登录/.test(after), '');
  } catch (e) { ok('微信扫码登录', false, e.message.slice(0, 120)); }

  fs.mkdirSync(SHOT_DIR, { recursive: true });
  await page.screenshot({ path: path.join(SHOT_DIR, 'demo-path.png'), fullPage: true });
  console.log('\n截图：' + path.join(SHOT_DIR, 'demo-path.png'));
  console.log('微信相关网络：\n' + (net.join('\n') || '(无)'));
  console.log('\nJS 错误 ' + jsErrors.length + ' 条：' + JSON.stringify(jsErrors.slice(0, 8)));

  await browser.close();
  const failed = checks.filter((c) => !c.pass);
  console.log('\n=== 汇总：' + (checks.length - failed.length) + '/' + checks.length + ' 通过 ===');
  process.exit(failed.length === 0 ? 0 : 1);
})().catch((e) => { console.error('PROBE FAILED:', e); process.exit(2); });
