/**
 * 首屏加载态探针（R-117，R-121 改为自造窗口）
 *
 * 要验证的一件事：**界面在等待期间说"正在加载"，而不是"暂无教师数据"。**
 *
 * 为什么必须用真浏览器：这个缺陷只在"请求还没回来"的那段时间里可见。
 * 单测是把 `loading` 直接传进去的 —— 它证明"组件收到 loading=true 会说对话"，
 * 但没有证明"App 真的会在首屏设 loading=true"。后者只有端到端能看到。
 *
 * ## R-121：为什么不再依赖"服务刚启动"
 *
 * 第一版靠"服务里还没有表"来制造窗口（预检 `/health` 的 `data.loaded`）。
 * 结果是它**只能在服务刚启动时跑**：
 *   · 昨天跑真实验收，因为前面已加载过数据 → SKIP → exit 3 → 整条验收 15/16；
 *   · 而 SKIP 的判断本身是对的（确实观察不到），错的是"要求服务处于某个状态"
 *     这个前提 —— 一个探针不该有环境前置条件。
 *
 * 现在改为**自己造窗口**，与服务状态无关：
 *   ① 用 CDP 拦截 `/status`，把响应体改写成 `loaded:false`（页面会认为需要加载）；
 *   ② 拦截 `/load-default`，让它的响应**延迟 3 秒**（这就是"加载中"的窗口）；
 *   ③ 在 t≈800ms 读页面文字 —— 那一刻 load-default 还没回来。
 *
 * 这样无论服务里有没有表、是本地还是线上，窗口都一样，可以随时跑。
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
  const browser = await launch();
  const page = await browser.newPage();
  const results = [];
  const rec = (ok, name, detail) => {
    results.push({ ok, name, detail });
    console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? `  —— ${detail}` : ''}`);
  };

  // ---- 自造窗口：让页面以为"需要加载"，并让这次加载慢下来 --------------
  let releaseLoad;
  const loadGate = new Promise((r) => (releaseLoad = r));

  // ① /status 一律回 loaded:false —— 不管服务里实际有没有表。
  //    这一步把"服务状态"从前提条件里彻底去掉。
  await page.route('**/api/reschedule/status*', async (route) => {
    try {
      const resp = await route.fetch();
      const body = await resp.json().catch(() => ({}));
      const patched = { ...body, loaded: false, total_entries: 0 };
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(patched),
      });
    } catch {
      await route.abort().catch(() => {});
    }
  });

  // ② /load-default 卡住 DELAY_MS —— 这就是要观察的"加载中"窗口。
  await page.route('**/api/reschedule/load-default*', async (route) => {
    await loadGate;
    await route.continue().catch(() => {});
  });

  const nav = page.goto(SITE, { waitUntil: 'commit' }).catch(() => {});
  await page.waitForSelector('body', { timeout: 10000 }).catch(() => {});

  // 轮询取文本：SPA 首次渲染可能晚于固定时刻，硬等会撞上"上下文未就绪"。
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

  // ⚠️ 不能只断言"整页文本里有'加载中'"（第一版就是这么写，注入验证时发现抓不到缺陷）：
  // `App.tsx` 上层那个转圈块也含"数据加载中"，只要它在，整页断言就永远成立 ——
  // 于是即使 `IdentityView` 完全不看 loading（仍在说"暂无教师数据"），探针也会全绿。
  // 正确做法：**限定在 main 区域**，并接受两种加载语义之一（上层转圈 / 组件自己那句）。
  const mainText = await page.evaluate(() => {
    const m = document.querySelector('main');
    return (m && m.innerText) || '';
  });
  if (mainText) during = mainText;

  const hasLoading = /正在读取实验安排表|数据加载中/.test(mainText);
  const hasEmpty = /暂无教师数据/.test(mainText);

  rec(hasLoading, '加载中显示"正在读取/加载中"',
    hasLoading ? '' : `实际文本片段：${mainText.slice(0, 160) || during.slice(0, 160)}`);
  rec(!hasEmpty, '加载中不显示"暂无教师数据"',
    hasEmpty ? '出现了"暂无教师数据"——正是 R-117 的缺陷表现' : '');
  // 两种说法绝不能并存：一边"正在加载"、一边"没有数据"，自相矛盾。
  rec(!(hasLoading && hasEmpty), '加载中不出现自相矛盾的说法',
    hasLoading && hasEmpty ? `同时出现：「${mainText.slice(0, 120)}」` : '');

  // ---- 放行，验证加载完成后进入身份选择 ------------------------------
  releaseLoad();
  await nav;
  await page.waitForTimeout(2500);
  let after = '';
  for (let i = 0; i < 30; i++) {
    try {
      after = await page.evaluate(() => {
        const m = document.querySelector('main');
        return (m && m.innerText) || document.body.innerText || '';
      });
    } catch {
      /* 同上 */
    }
    if (after.trim()) break;
    await page.waitForTimeout(100);
  }
  const okAfter = /请选择您的身份/.test(after) && !/正在读取实验安排表/.test(after);
  rec(okAfter, '加载完成后进入身份选择且不再显示加载语',
    okAfter ? '' : `实际文本片段：${after.slice(0, 200)}`);

  // 先解路由拦截再关浏览器：若还有 page 挂在 gate 上，`close()` 会等它，
  // 表现为"全 PASS 了却不退出"（实测踩到，watchdog 打了一条误导性的 FAIL）。
  try { releaseLoad(); } catch { /* 已放行 */ }
  await page.unroute('**/api/reschedule/status*').catch(() => {});
  await page.unroute('**/api/reschedule/load-default*').catch(() => {});
  // 不 await `browser.close()`：此时导航 promise 可能仍在 settle，close 会一直等它。
  // 结果已经全部打印，用 process.exit 收尾是安全的 —— 进程退出会关掉浏览器。
  void browser.close().catch(() => {});
  clearTimeout(watchdog);

  const failed = results.filter((r) => !r.ok).length;
  console.log(`\n${results.length - failed}/${results.length} 通过`);
  process.exit(failed ? 1 : 0);
})().catch((e) => {
  console.error('探针异常：', e && e.message);
  process.exit(2);
});
