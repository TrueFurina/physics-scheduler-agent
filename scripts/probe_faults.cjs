/**
 * 故障注入点测（只读，不改任何数据）
 *
 * 存在的理由
 * ----------
 * 前面的探针都在验证「一切正常时界面是对的」。但教秘演示最丢脸的不是数字不好看，
 * 而是**出错时界面在说谎或什么都不说**：
 *   - R-24：解析成功却没画沙盘 → 一屏空白，0 JS 错误、0 失败请求；
 *   - R-27：体检失败只留一张空白卡 → 被读成"没冲突"。
 * 这两个都是「错误路径」，正常路径探针永远照不到。所以这里**主动制造故障**：
 * 把某一路接口拦成 500 / 429 / 断网，然后检查界面到底给了用户什么。
 *
 * 每个场景独立视图（errors 与正常路径互不污染），断言四件事：
 *   1. 有提示  —— 不能静默（空白/看着正常最糟）
 *   2. 说中文  —— `err.message` 直出会得到 "Request failed with status code 500"、
 *                "Network Error"、"timeout of 60000ms exceeded"，公网演示时非常刺眼
 *   3. 可操作  —— 提示里要有"重试/刷新/稍后"这类下一步
 *   4. 看得见  —— 提示条在**当前视口内**。教秘点了页面底部的「一键排课」，
 *                失败提示若出现在页面顶端而用户没滚动，等于没提示
 *
 * 用法：SITE=http://127.0.0.1:8023/ npm run probe:faults
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

// axios 直出的英文文案：出现在中文界面里就是缺陷
const EN_NOISE = /Request failed with status code|Network Error|timeout of \d+\s*ms exceeded|Internal Server Error|AxiosError|Failed to fetch/i;
const ACTIONABLE = /重试|刷新|稍后再试|稍候|检查|联系/;

/** 读顶部提示条的真实状态（存在性 / 文案 / 是否在当前视口内）。 */
async function readBanner(page) {
  return page.evaluate(() => {
    const btn = document.querySelector('button[aria-label="关闭提示"]');
    if (!btn) return null;
    const box = btn.closest('div.bg-red-50') || btn.parentElement;
    if (!box) return null;
    const r = box.getBoundingClientRect();
    return {
      text: (box.innerText || '').trim(),
      top: Math.round(r.top),
      bottom: Math.round(r.bottom),
      viewportH: window.innerHeight,
      inViewport: r.top >= 0 && r.bottom <= window.innerHeight + 1,
      scrollY: Math.round(window.scrollY),
    };
  });
}

function judge(label, banner) {
  if (!banner) return { ok: false, why: '界面没有给出任何提示（静默失败）', text: '' };
  const text = banner.text;
  if (EN_NOISE.test(text)) return { ok: false, why: `提示里是 axios 英文原文「${text.slice(0, 60)}」`, text };
  if (!/[\u4e00-\u9fa5]/.test(text)) return { ok: false, why: `提示不含中文：「${text.slice(0, 60)}」`, text };
  if (!ACTIONABLE.test(text)) return { ok: false, why: `提示没说下一步怎么办：「${text.slice(0, 60)}」`, text };
  if (!banner.inViewport) {
    return {
      ok: false,
      why: `提示在视口外（top=${banner.top}px / 视口 ${banner.viewportH}px，已滚到 ${banner.scrollY}px）——用户看不到`,
      text,
    };
  }
  return { ok: true, why: '', text };
}

/**
 * 读「就地提示」（不在顶部横幅里的那类）。
 * 自然语言调课把失败写在输入框正下方（`❌ 请求失败：…`），教秘眼睛就在那儿，
 * 所以这里同样要断言**它在视口内**，而不是页面某处存在这段文字。
 */
async function readInline(page, source) {
  return page.evaluate((src) => {
    const rx = new RegExp(src);
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    let n;
    while ((n = walker.nextNode())) {
      if (!rx.test(n.textContent || '')) continue;
      const el = n.parentElement;
      if (!el) continue;
      const r = el.getBoundingClientRect();
      return {
        text: (el.innerText || el.textContent || '').trim(),
        top: Math.round(r.top),
        bottom: Math.round(r.bottom),
        viewportH: window.innerHeight,
        inViewport: r.top >= 0 && r.bottom <= window.innerHeight + 1,
        scrollY: Math.round(window.scrollY),
      };
    }
    return null;
  }, source);
}

// ---------------------------------------------------------------------------
// 场景表
// ---------------------------------------------------------------------------
// `intercept` 用一个「只拦某一路」的路由谓词；`drive` 负责把界面开到会发该请求的位置。
// `mode` = 'banner' 用顶部提示条判定；'card' 由场景自定义断言（如看板的失败卡）。
const SCENARIOS = [
  {
    id: 'teachers',
    name: '首屏 · 教师列表 500',
    url: /\/api\/reschedule\/teachers(\?|$)/,
    body: 'Internal Server Error',
    contentType: 'text/plain',
    drive: async (page) => {
      await page.waitForTimeout(3200);   // withRetry 三连退避约 2.3s，等它走完
    },
  },
  {
    id: 'courses',
    name: '② 我的课程 500',
    url: /\/api\/reschedule\/teachers\/[^/]+\/courses$/,
    body: 'Internal Server Error',
    contentType: 'text/plain',
    drive: async (page) => {
      await page.locator('button', { hasText: TEACHER }).first().click();
      await page.waitForTimeout(1600);
    },
  },
  {
    id: 'overview',
    name: '⚡ 并行排课 · 课程总览 500',
    url: /\/api\/reschedule\/parallel\/overview$/,
    body: JSON.stringify({ detail: '模拟故障：总览不可用' }),
    contentType: 'application/json',
    drive: async (page) => {
      await page.getByRole('button', { name: /并行排课/ }).first().click();
      await page.waitForTimeout(3200);   // 这条也走 withRetry
    },
  },
  {
    id: 'solve',
    name: '⚡ 一键排课 500（页面已滚到按钮处）',
    url: /\/api\/reschedule\/parallel\/solve$/,
    body: 'Internal Server Error',
    contentType: 'text/plain',
    // 这条专治"点了没反应"：按钮在页面底部，提示出现在顶端而视口没回滚。
    drive: async (page, ctx) => {
      await page.getByRole('button', { name: /并行排课/ }).first().click();
      await page.waitForFunction(() => /2024级/.test(document.body.innerText), { timeout: 40000 });
      await ctx.arm();                     // 总览已到，现在才让 solve 开始失败
      const btn = page.getByRole('button', { name: /^一键排课$/ }).first();
      await btn.scrollIntoViewIfNeeded();
      await btn.click();
      await page.waitForTimeout(2500);
    },
  },
  {
    id: 'audit',
    name: '📊 数据体检 500',
    url: /\/api\/reschedule\/data-audit$/,
    body: JSON.stringify({ detail: '模拟故障：体检不可用' }),
    contentType: 'application/json',
    mode: 'card',
    drive: async (page) => {
      await page.getByRole('button', { name: /数据看板/ }).first().click();
      await page.waitForFunction(() => /体检没能完成/.test(document.body.innerText), { timeout: 40000 });
    },
    assert: async (page) => {
      const t = await page.evaluate(() => document.body.innerText);
      const hasRetry = /重新体检/.test(t);
      const fakeHealthy = /整体健康|未发现资源冲突/.test(t);
      return {
        ok: hasRetry && !fakeHealthy,
        why: fakeHealthy ? '失败时仍显示"健康"结论（会让教秘以为已检查过）' : '失败卡缺少「重新体检」入口',
      };
    },
  },
  {
    id: 'stats',
    name: '📊 统计面板 500',
    url: /\/api\/reschedule\/stats$/,
    body: 'Internal Server Error',
    contentType: 'text/plain',
    mode: 'inline',
    drive: async (page) => {
      await page.getByRole('button', { name: /数据看板/ }).first().click();
      await page.waitForFunction(() => /共 \d+ 条安排/.test(document.body.innerText), { timeout: 40000 });
      await page.waitForTimeout(1200);
    },
    assert: async (page) => {
      const t = await page.evaluate(() => document.body.innerText);
      const line = (t.match(/[^\n]*统计[^\n]*失败[^\n]*/) || t.match(/[^\n]*教室利用率[^\n]*不可用[^\n]*/) || [''])[0];
      if (EN_NOISE.test(t)) return { ok: false, why: '统计失败处出现 axios 英文原文' };
      return { ok: /失败|不可用/.test(t), why: `统计失败没有可见提示（截图文本片段：「${line.slice(0, 40)}」）` };
    },
  },
  {
    id: 'chat500',
    name: '💬 自然语言调课 500',
    url: /\/api\/reschedule\/chat$/,
    body: 'Internal Server Error',
    contentType: 'text/plain',
    mode: 'inline',
    inlineMatch: '请求失败',
    drive: async (page) => {
      await page.locator('button', { hasText: TEACHER }).first().click();
      await page.waitForFunction(() => /当前教师：/.test(document.body.innerText), { timeout: 30000 });
      await page.locator('input[placeholder="输入大白话调课需求..."]').fill(`我是${TEACHER}，把声速测量调到第3周`);
      await page.getByRole('button', { name: /^发送$/ }).click();
      await page.waitForTimeout(2500);
    },
  },
  {
    id: 'chat429',
    name: '💬 限流 429（Retry-After 15 秒）',
    url: /\/api\/reschedule\/chat$/,
    status: 429,
    headers: { 'Retry-After': '15' },
    body: JSON.stringify({ detail: '请求过于频繁，请稍候再试。' }),
    contentType: 'application/json',
    mode: 'inline',
    inlineMatch: '请求失败',
    drive: async (page) => {
      await page.locator('button', { hasText: TEACHER }).first().click();
      await page.waitForFunction(() => /当前教师：/.test(document.body.innerText), { timeout: 30000 });
      await page.locator('input[placeholder="输入大白话调课需求..."]').fill(`我是${TEACHER}，把声速测量调到第3周`);
      await page.getByRole('button', { name: /^发送$/ }).click();
      await page.waitForTimeout(2500);
    },
    assert: async (page) => {
      const b = await readInline(page, '请求失败');
      if (!b) return { ok: false, why: '限流时界面没有任何提示（静默失败）' };
      if (EN_NOISE.test(b.text)) return { ok: false, why: `限流提示是英文原文「${b.text.slice(0, 50)}」` };
      // 限流不是故障而是"稍后再试"，必须把等待时间告诉用户
      const hasWait = /1[0-9]\s*秒|稍候|稍后再试/.test(b.text);
      return {
        ok: hasWait && b.inViewport,
        why: b.inViewport ? `限流提示没说等多久（实际「${b.text.slice(0, 50)}」）` : '限流提示在视口外',
      };
    },
  },
];

(async () => {
  const browser = await launch();

  for (const sc of SCENARIOS) {
    const ctx2 = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
    const page = await ctx2.newPage();
    const jsErrors = [];
    page.on('pageerror', (e) => jsErrors.push(String(e).slice(0, 140)));

    // 求解场景需要"先放行总览、再拦截 solve"：用一个开关控制是否开始拦。
    const state = { armed: sc.id !== 'solve' };
    const handler = (route) =>
      state.armed
        ? route.fulfill({
            status: sc.status || 500,
            contentType: sc.contentType,
            headers: sc.headers,
            body: sc.body,
          })
        : route.continue();
    await page.route(sc.url, handler);
    page.ctxHelper = { arm: async () => { state.armed = true; } };

    let detail = '';
    let ok = false;
    try {
      await page.goto(SITE, { waitUntil: 'domcontentloaded', timeout: 60000 });
      await page.waitForSelector('text=物理实验调课辅助', { timeout: 60000 });
      await sc.drive(page, page.ctxHelper);

      if (sc.mode === 'inline' && !sc.assert) {
        const b = await readInline(page, sc.inlineMatch || '失败');
        const v = judge(sc.name, b);
        ok = v.ok;
        detail = v.ok ? `就地提示「${v.text.replace(/\s+/g, ' ').slice(0, 56)}」在视口内` : v.why;
      } else if (sc.mode === 'card' || sc.mode === 'inline') {
        const r = await sc.assert(page);
        ok = r.ok;
        detail = r.ok ? `${sc.name}：失败态可见且有出口` : r.why;
      } else {
        const b = await readBanner(page);
        const v = judge(sc.name, b);
        ok = v.ok;
        detail = v.ok ? `提示「${v.text.replace(/\s+/g, ' ').slice(0, 56)}」在视口内` : v.why;
      }
    } catch (e) {
      ok = false;
      const body = await page.evaluate(() => document.body.innerText).catch(() => '');
      detail = `场景未走完：${String(e).slice(0, 90)}｜当前可见：${String(body).replace(/\s+/g, ' ').slice(0, 90)}`;
    }
    if (jsErrors.length) {
      ok = false;
      detail += `｜崩溃：${jsErrors[0]}`;
    }
    rec(ok, sc.name, detail);
    await page.screenshot({ path: path.join(SHOT_DIR, `fault-${sc.id}.png`), fullPage: true }).catch(() => {});
    await ctx2.close();
  }

  await browser.close();
  const pass = results.filter((r) => r.ok).length;
  console.log(`\n=== 汇总：${pass}/${results.length} 通过 ===`);
  process.exit(pass === results.length ? 0 : 1);
})().catch((e) => {
  console.error('探针异常：', e);
  process.exit(2);
});
