/**
 * 首屏加载态探针（R-117 现场验证）
 *
 * 要验证的一件事：**界面在等待期间说"正在加载"，而不是"暂无教师数据"。**
 *
 * 为什么必须用真浏览器：这个缺陷只在"请求还没回来"的那段时间里可见，
 * 而单测是把 `loading` 直接传进去的 —— 它证明"组件收到 loading=true 会说对话"，
 * 但没有证明"App 真的会在首屏设 loading=true"。后者只有端到端能看到。
 *
 * 做法：用 CDP 把 `/api/reschedule/status` 的响应**延迟 3 秒**，
 * 然后在 t≈500ms 时读页面文字 —— 那一刻请求还没回来，正是要观察的窗口。
 */
const { launch } = require('./_browser.cjs');

const SITE = process.env.SITE || 'http://127.0.0.1:8023/';
const DELAY_MS = Number(process.env.DELAY_MS || 3000);

// 硬超时兜底：路由被拦时 goto 可能一直挂着，没有这条会让整个探针卡死
// （实测：注入"组件不区分加载态"后，等待窗口里 evaluate 抛错 → 探针挂住 300s）。
const HARD_TIMEOUT_MS = Number(process.env.HARD_TIMEOUT_MS || 60000);
const watchdog = setTimeout(() => {
  console.error(`探针超过 ${HARD_TIMEOUT_MS}ms 未完成，强制退出（视为 FAIL）`);
  process.exit(2);
}, HARD_TIMEOUT_MS);
watchdog.unref?.();

(async () => {
  // 预检：这条探针要的是"数据还没加载"的窗口。如果服务里已有表，
  // 首屏会直接进身份选择，看不到加载态 —— 此时**必须 SKIP 而不是 PASS**。
  // （把没测到的写成通过，是让整套探针失去可信度的最快方式。）
  const st = await fetch(new URL('/health', SITE)).then((r) => r.json()).catch(() => null);
  if (st && st.data && st.data.loaded) {
    console.log('SKIP  服务里已有排课数据（loaded=true），观察不到首屏加载窗口。');
    console.log('      请在服务刚启动、尚未 load-default 时运行，或先重启服务。');
    // 退出码 3 = "跳过"（不是 0，避免被验收脚本当成 PASS）。
    // `run_acceptance.py` 里这条 step 未设 must，非 0 会判 FAIL ——
    // 这是刻意的：首屏加载态属于**每次交付都要看**的一项，
    // 静默跳过等于默认它没事。要跳过就显式 --skip-ui。
    process.exit(3);
  }

  const browser = await launch();
  const page = await browser.newPage();
  const results = [];
  const rec = (ok, name, detail) => {
    results.push({ ok, name, detail });
    console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? `  —— ${detail}` : ''}`);
  };

  // 拦 /status 并延迟回包：制造"首屏正在加载"的窗口
  let released;
  const gate = new Promise((r) => (released = r));
  await page.route('**/api/reschedule/status*', async (route) => {
    await gate;                      // 卡住，直到下面手动放行
    await route.continue().catch(() => {});
  });

  // 不 await 导航：我们要在"文档已就绪但数据还没回来"的窗口里取文本。
  // `commit` 比 `domcontentloaded` 更早，避免在 SPA 首次渲染前就 evaluate。
  const nav = page.goto(SITE, { waitUntil: 'commit' }).catch(() => {});
  await page.waitForSelector('body', { timeout: 10000 }).catch(() => {});

  // 轮询取文本：SPA 首次渲染可能晚于 800ms，硬等一个固定时刻会撞上"上下文未就绪"。
  let during = '';
  for (let i = 0; i < 40; i++) {
    try {
      during = await page.evaluate(() => document.body.innerText || '');
    } catch {
      /* 导航途中上下文会短暂不可用，重试即可 */
    }
    if (during.trim()) break;
    await page.waitForTimeout(100);
  }

  // ⚠️ 不能只断言"整页文本里有'加载中'"（第一版就是这么写，注入验证时发现它抓不到缺陷）：
  // `App.tsx` 在上层还有一个转圈块也含"数据加载中"，只要它在，整页断言就永远成立 ——
  // 于是即使 `IdentityView` 完全不看 loading（仍在说"暂无教师数据"），探针也会 3/3 绿。
  // 正确做法：**限定在身份选择区域**（main 内部），并且必须出现组件自己那句
  // 「正在读取实验安排表」，而不是上层的通用"数据加载中"。
  const mainText = await page.evaluate(() => {
    const m = document.querySelector('main');
    return (m && m.innerText) || '';
  });
  // 接受**任一种**加载语义，但必须有：
  //   · 上层转圈块（App.tsx 在 !dataLoaded && loading 时渲染）→「数据加载中」
  //   · 或组件自己那句（IdentityView 收到 loading 时渲染）→「正在读取实验安排表」
  // 两层的分工是：组件保证"就算被渲染也不会说错话"，App 保证"干脆不渲染它"。
  // 只要其中一层在，用户看到的就是"加载中"；两层都不在才是缺陷。
  const hasLoading = /正在读取实验安排表|数据加载中/.test(mainText);
  const hasEmpty = /暂无教师数据/.test(mainText);
  if (mainText) during = mainText;

  // 关键断言：绝不能同时出现"在加载"和"没有数据"两种说法（自相矛盾）。
  rec(
    !(hasLoading && hasEmpty),
    '加载中不出现自相矛盾的说法',
    hasLoading && hasEmpty ? `同时出现：「${mainText.slice(0, 120)}」` : '',
  );
  rec(hasLoading, '加载中显示"正在读取/加载中"', hasLoading ? '' : `实际文本片段：${during.slice(0, 160)}`);
  rec(!hasEmpty, '加载中不显示"暂无教师数据"', hasEmpty ? '出现了"暂无教师数据"——正是 R-117 的缺陷表现' : '');

  // 放行，等首屏完成
  released();
  await nav;
  await page.waitForTimeout(2500);
  let after = '';
  for (let i = 0; i < 30; i++) {
    try {
      after = await page.evaluate(() => document.body.innerText || '');
    } catch {
      /* 同上 */
    }
    if (after.trim()) break;
    await page.waitForTimeout(100);
  }
  const okAfter = /请选择您的身份/.test(after) && !/正在读取实验安排表/.test(after);
  rec(okAfter, '加载完成后进入身份选择且不再显示加载语', okAfter ? '' : `实际文本片段：${after.slice(0, 200)}`);

  // 先解路由拦截再关浏览器：若还有 page 挂在 `await gate` 上，`close()` 会等它，
  // 表现为"3 项都 PASS 了却不退出"（实测踩到，watchdog 打了一条误导性的 FAIL）。
  try { released(); } catch { /* 已放行过 */ }
  // 显式撤掉路由拦截：否则 playwright 内部还挂着那条 route 的 promise，
  // `browser.close()` 会一直等它 → 探针 4/4 PASS 了却不退出（实测 exit=124）。
  await page.unroute('**/api/reschedule/status*').catch(() => {});
  clearTimeout(watchdog);
  await page.close().catch(() => {});
  // 不 await `browser.close()`：此时导航 promise 可能仍在 settle，close 会一直等它，
  // 表现为"4/4 PASS 却不退出"（实测 exit=124 反复出现，加了 diag 才定位到是 close 挂住）。
  // 结果已经全部打印，用 process.exit 收尾是安全的 —— 进程退出会关掉浏览器。
  void browser.close().catch(() => {});
  const failed = results.filter((r) => !r.ok).length;
  console.log(`\n${results.length - failed}/${results.length} 通过`);
  process.exit(failed ? 1 : 0);
})().catch((e) => {
  console.error('探针异常：', e && e.message);
  process.exit(2);
});
