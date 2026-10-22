/**
 * 线上主流程 UI 点测（只读，不改任何数据）
 * 覆盖：首屏 → 一键自动排课 → 我的课程(F1/F2) → 并行排课(总览+一键排课) → 数据看板
 *      → 选课咨询(懒加载) → 懒加载 chunk 失败降级（故意 abort 一个按需 chunk，验证不白屏）
 */
const { launch } = require('./_browser.cjs');
const path = require('path');
const fs = require('fs');

const SITE = process.env.SITE || 'https://physics-scheduler.app.workbuddy.host/';
const SHOT_DIR = process.env.PROBE_SHOT_DIR || path.join(require('os').tmpdir(), 'psprobe');

const results = [];
/**
 * 三态记录：PASS / FAIL / SKIP。
 * SKIP 用来表达"当前产物不具备被测条件"，**绝不能**退化成 PASS ——
 * 把没测到的写成通过，是让整套探针失去可信度的最快方式。
 */
function rec(ok, name, detail, opts = {}) {
  const status = opts.skip ? 'SKIP' : ok ? 'PASS' : 'FAIL';
  results.push({ ok, skip: !!opts.skip, name, detail });
  console.log(`${status}  ${name}  — ${detail}`);
}

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

  // 🤖 一键自动排课 —— 首屏第一个按钮，也是全项目唯一没有正确性测试的引擎（R-37）。
  // 旧实现的返回值里**没有周次**：教秘拿到一份"不知道属于哪一周"的课表，
  // 而且它从不读已有安排表。这里断言三件事，缺一不可：
  //   ① 结果里出现「第N周：」——方案落地到具体周次；
  //   ② 明确写出未校验项——没校验就别声称三重不冲突；
  //   ③ 成功数 = 任务数（否则说明又被编造的约束判死了）。
  try {
    const tA = Date.now();
    await page.getByRole('button', { name: /一键自动排课/ }).first().click();
    // 等待条件必须是"只在结果出现后才有"的串：页面别处没有「个教学任务」
    await page.waitForFunction(() => /个教学任务/.test(document.body.innerText), { timeout: 120000 });
    await page.waitForTimeout(500);
    const txt = await page.evaluate(() => document.body.innerText);
    const m = txt.match(/成功\s*(\d+)\/(\d+)\s*个教学任务/);
    const hasWeek = /第\d+周：/.test(txt);
    const honest = /未校验/.test(txt);
    const ok = !!m && hasWeek && honest && m[1] === m[2];
    rec(ok, '🤖 一键自动排课（带周次）',
      `${Date.now() - tA}ms 返回，${m ? `${m[1]}/${m[2]} 个教学任务` : '未见结果摘要'}`
      + `，周次${hasWeek ? '有' : '缺'}，未校验声明${honest ? '有' : '缺'}`);
    await page.screenshot({ path: path.join(SHOT_DIR, 'flow-auto-schedule.png') });
  } catch (e) {
    rec(false, '🤖 一键自动排课（带周次）', String(e).slice(0, 100));
  }

  // ⬇️ 自动排课初稿导出（全校 171 条）：能排出来 ≠ 能交付。
  // 只给一段文本，教秘还得手抄；这里验"点了能拿到真 xlsx"（zip 魔数 + 体积）。
  try {
    const tE = Date.now();
    const [dl] = await Promise.all([
      page.waitForEvent('download', { timeout: 60000 }),
      page.getByRole('button', { name: /导出初稿 Excel/ }).first().click(),
    ]);
    const nm = dl.suggestedFilename();
    const pth = await dl.path();
    const buf = pth ? fs.readFileSync(pth) : Buffer.alloc(0);
    const isZip = buf.length > 4 && buf[0] === 0x50 && buf[1] === 0x4b;
    rec(
      isZip && /\.xlsx$/i.test(nm) && buf.length > 2000,
      '⬇️ 导出初稿 Excel',
      `${Date.now() - tE}ms，「${nm}」${(buf.length / 1024).toFixed(1)} KB，`
        + (isZip ? 'zip 魔数 PK 正常' : '不是 xlsx（可能下到了错误 JSON）'),
    );
  } catch (e) {
    rec(false, '⬇️ 导出初稿 Excel', `没拿到下载事件：${String(e).slice(0, 110)}`);
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

    // 🌐 一键排全校（逐班·按天，组级）：单班一键 ×17 次不是"一键全面排课"。
    // 断言三件事：① 结果标题出现；② 覆盖 17/17（真实表班级数）；③ 方案条数 > 300。
    try {
      const tS = Date.now();
      await page.getByRole('button', { name: /一键排全校/ }).first().click();
      await page.waitForFunction(
        () => /全校一键排课 · \d+\/\d+ 个班排定/.test(document.body.innerText),
        { timeout: 180000 },
      );
      await page.waitForTimeout(400);
      const stxt = await page.evaluate(() => document.body.innerText);
      const m = stxt.match(/全校一键排课 · (\d+)\/(\d+) 个班排定/);
      const rows = stxt.match(/共 (\d+) 条安排/);
      const n = rows ? Number(rows[1]) : 0;
      rec(
        !!m && m[1] === m[2] && n > 300,
        '🌐 一键排全校（组级）',
        `${Date.now() - tS}ms 返回，${m ? `${m[1]}/${m[2]} 班` : '未见解' }，${n} 条安排`,
      );
      await page.screenshot({ path: path.join(SHOT_DIR, 'flow-school-plan.png') });
    } catch (e) {
      rec(false, '🌐 一键排全校（组级）', String(e).slice(0, 120));
    }

    const t2 = Date.now();
    // 点**靠后的一张卡**：结果区渲染在全部课程卡之后，点第一张卡（本就在首屏）
    // 或点最后一张卡，结果都可能掉到折叠线以下 —— 教秘只看到按钮从"求解中"恢复，
    // 等于"点了没反应"（R-24 / R-30 同族）。这里连视口一起断言。
    const buttons = page.getByRole('button', { name: /^一键排课$/ });
    const nCards = await buttons.count();
    await buttons.nth(Math.min(8, nCards - 1)).click();
    // 结果区渲染的是「📋 排课方案（周五）· 状态 OPTIMAL」+「24/24 个单元已排定」
    // 注意：不能用"周次"之类的泛词做等待条件 —— 页面本来就有这些字，会立刻命中造成假通过。
    await page.waitForFunction(
      () => /个单元已排定/.test(document.body.innerText),
      { timeout: 150000 },
    );
    await page.waitForTimeout(600);
    const txt = await page.evaluate(() => document.body.innerText);
    const m = txt.match(/(\d+)\/(\d+)\s*个单元已排定/);
    const vis = await page.evaluate(() => {
      const h3 = [...document.querySelectorAll('h3')].find((h) => /排课方案/.test(h.textContent || ''));
      if (!h3) return { has: false, ok: false };
      const r = h3.getBoundingClientRect();
      const p = [...document.querySelectorAll('p')].find((x) => /个单元已排定/.test(x.textContent || ''));
      const pr = p ? p.getBoundingClientRect() : null;
      return {
        has: true,
        titleTop: Math.round(r.top),
        summaryTop: pr ? Math.round(pr.top) : null,
        vh: window.innerHeight,
        // 摘要行必须**完整**落在视口内：只断言"标题顶边在视口内"是假绿 ——
        // 修复前点最后一张卡，标题顶边 744px 也 < 800，照样判过，教秘其实只看到一条缝。
        ok: r.top >= 0 && r.top < window.innerHeight && !!pr && pr.top >= 0 && pr.bottom <= window.innerHeight + 1,
      };
    });
    const ok = /状态\s*(OPTIMAL|FEASIBLE)/.test(txt) && vis.ok;
    rec(
      ok,
      '⚡ 一键排课（CP-SAT）',
      `${Date.now() - t2}ms 返回，${m ? `${m[1]}/${m[2]} 单元已排定` : '未见结果摘要'}；` +
        (vis.ok
          ? '结果区在视口内'
          : `结果区掉到视口外（标题 top=${vis.titleTop}px / 摘要 top=${vis.summaryTop}px / 视口 ${vis.vh}px）`),
    );
    await page.screenshot({ path: path.join(SHOT_DIR, 'flow-solve.png') });

    // ⬇️ 导出 Excel —— 教秘真正带走成果的那一步，此前零覆盖。
    // 只断言"点了不报错"没有意义：即便后端返回 429/500 的 JSON，浏览器照样会下成一个
    // 后缀是 .xlsx 的坏文件。所以这里读下载回来的字节，验 zip 魔数 PK。
    try {
      const t4 = Date.now();
      const [download] = await Promise.all([
        page.waitForEvent('download', { timeout: 30000 }),
        page.getByRole('button', { name: /导出 Excel/ }).first().click(),
      ]);
      const name = download.suggestedFilename();
      const saved = await download.path();
      const buf = saved ? fs.readFileSync(saved) : Buffer.alloc(0);
      const isZip = buf.length > 4 && buf[0] === 0x50 && buf[1] === 0x4b;
      rec(
        isZip && /\.xlsx$/i.test(name) && buf.length > 1000,
        '⬇️ 导出 Excel',
        `${Date.now() - t4}ms，「${name}」${(buf.length / 1024).toFixed(1)} KB，` +
          (isZip ? 'zip 魔数 PK 正常' : `不是 xlsx（前 2 字节 ${buf.slice(0, 2).toString('hex') || '空'}，可能下到了错误 JSON）`),
      );
    } catch (e) {
      rec(false, '⬇️ 导出 Excel', `没拿到下载事件：${String(e).slice(0, 110)}`);
    }
  } catch (e) {
    rec(false, '⚡ 并行排课链路', String(e).slice(0, 120));
  }

  // 数据看板
  //
  // ⚠️ 这条断言曾经是假绿：等待条件写的 /数据体检|冲突|统计/ 在**页面一挂载**就命中，
  //    因为副标题「教室/教师冲突扫描」是静态文案；接口被 abort 掉照样 PASS（已实测证实）。
  //    现在只认**数据到达后才会出现**的字符串：「共 N 条安排」「N 间场地」「平均教室利用率」。
  try {
    const t3 = Date.now();
    await page.getByRole('button', { name: /数据看板/ }).first().click();
    await page.waitForFunction(
      () => /共 \d+ 条安排\s*｜\s*\d+ 位教师/.test(document.body.innerText),
      { timeout: 40000 },
    );
    await page.waitForFunction(
      () => /平均教室利用率/.test(document.body.innerText),
      { timeout: 20000 },
    );
    const txt = await page.evaluate(() => document.body.innerText);
    const mEntries = txt.match(/共 (\d+) 条安排\s*｜\s*\d+ 位教师\s*｜\s*(\d+) 间场地/);
    const mRooms = mEntries ? [null, mEntries[2]] : null;
    const mUtil = txt.match(/([\d.]+)%\s*\n\s*平均教室利用率/);
    const conclusion = /整体健康/.test(txt)
      ? '整体健康'
      : /发现潜在冲突/.test(txt)
        ? '发现潜在冲突'
        : /未发现资源冲突/.test(txt)
          ? '无冲突但有数据缺口'
          : '无结论';
    const mGap = txt.match(/(\d+) 条记录没填场地/);
    const failedCard = /体检没能完成/.test(txt);
    rec(
      Boolean(mEntries) && Boolean(mRooms) && Boolean(mUtil) && conclusion !== '无结论' && !failedCard,
      '📊 数据看板',
      `${Date.now() - t3}ms：${mEntries ? mEntries[1] : '?'} 条安排 / ${mRooms ? mRooms[1] : '?'} 间场地 / ` +
        `平均利用率 ${mUtil ? mUtil[1] + '%' : '?'}；结论「${conclusion}」` +
        (mGap ? `（${mGap[1]} 条未填场地）` : '') +
        (failedCard ? '（出现失败卡！）' : ''),
    );
    await page.screenshot({ path: path.join(SHOT_DIR, 'flow-dashboard.png'), fullPage: true });

    // 📤 教务两类标准课表（班级课表 / 场地课表）—— 基于**当前安排表**，不是新排的方案。
    // 只断言"点了不报错"没有意义：后端出错时返回的是 JSON，浏览器照样下成一个 .xlsx 坏文件。
    // 所以这里读回字节：验 zip 魔数 + 体积（433 条记录 ≈ 30–40KB，空表不可能这么大）。
    for (const [label, re, minKb] of [
      ['班级课表', /班级课表/, 15000],
      ['场地课表', /场地课表/, 15000],
      ['基础数据台账', /基础数据台账/, 6000],
    ]) {
      try {
        const t5 = Date.now();
        const [download] = await Promise.all([
          page.waitForEvent('download', { timeout: 30000 }),
          page.getByRole('button', { name: re }).first().click(),
        ]);
        const name = download.suggestedFilename();
        const saved = await download.path();
        const buf = saved ? fs.readFileSync(saved) : Buffer.alloc(0);
        const isZip = buf.length > 4 && buf[0] === 0x50 && buf[1] === 0x4b;
        rec(
          isZip && buf.length > minKb,
          `📤 导出${label}`,
          `${Date.now() - t5}ms，「${name}」${(buf.length / 1024).toFixed(1)} KB，` +
            (isZip ? 'zip 魔数 PK 正常' : `不是 xlsx（前 2 字节 ${buf.slice(0, 2).toString('hex') || '空'}）`),
        );
      } catch (e) {
        rec(false, `📤 导出${label}`, `没拿到下载事件：${String(e).slice(0, 110)}`);
      }
    }
  } catch (e) {
    const txt = await page.evaluate(() => document.body.innerText).catch(() => '');
    const why = /体检没能完成/.test(txt)
      ? '界面上是「体检没能完成」失败卡'
      : '40s 内没等到体检数字（共 N 条安排 / 平均教室利用率）';
    rec(false, '📊 数据看板', `${why}：${String(e).slice(0, 90)}`);
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
  let chunkAborted = 0;   // 是否真的掐到了按需 chunk：单包旧产物没有这个文件，掐不到
  try {
    const ctx2 = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
    const p2 = await ctx2.newPage();
    await p2.route('**/ParallelView-*.js', (route) => { chunkAborted += 1; return route.abort(); });
    await p2.goto(SITE, { waitUntil: 'domcontentloaded', timeout: 60000 });
    await p2.waitForSelector('text=物理实验调课辅助', { timeout: 60000 });
    // 点一次可能"没掐到"：动态 import 还没发起就被判定结束，会让本项退化成 SKIP。
    // SKIP 本身诚实，但**偶发的 SKIP 等于把这一项长期漏测**（实测三次里漏一次）。
    // 所以重载后允许再点一次，两次都没发起请求才认定为"单包产物，本项不适用"。
    for (let attempt = 0; attempt < 2 && !chunkAborted; attempt++) {
      await p2.getByRole('button', { name: /并行排课/ }).first().click();
      await p2.waitForTimeout(1200);
      if (chunkAborted) break;
      await p2.reload({ waitUntil: 'domcontentloaded' });
      await p2.waitForSelector('text=物理实验调课辅助', { timeout: 60000 });
    }
    await p2.waitForFunction(
      () => /页面资源已更新|这个模块遇到了问题/.test(document.body.innerText),
      { timeout: 30000 },
    );
    const txt2 = await p2.evaluate(() => document.body.innerText);
    // 顶部导航是边界的兄弟节点，必须仍然可用 —— 这才是"降级"与"白屏"的区别
    const navAlive = /数据看板/.test(txt2) && /选课咨询/.test(txt2);
    const hasCard = /重新加载|重试/.test(txt2);
    if (!chunkAborted) {
      rec(true, '🛟 懒加载失败降级', '当前产物是单包（没有 ParallelView-*.js），本项不适用', { skip: true });
    } else {
      rec(navAlive && hasCard, '🛟 懒加载失败降级', `chunk 中断 ${chunkAborted} 次，显示提示卡（${hasCard ? '有' : '无'}）且顶部导航仍可用（${navAlive ? '是' : '否'}=非白屏）`);
    }
    await p2.screenshot({ path: path.join(SHOT_DIR, 'flow-chunk-fallback.png') });
    await ctx2.close();
  } catch (e) {
    // 没掐到 chunk 说明当前产物根本没有这个按需文件（旧单包），本项无从测起 → SKIP。
    // 不能记 FAIL（会误导成"降级坏了"），更不能记 PASS（等于假绿）。
    if (!chunkAborted) {
      rec(true, '🛟 懒加载失败降级', '当前产物是单包（没有 ParallelView-*.js），本项不适用', { skip: true });
    } else {
      rec(false, '🛟 懒加载失败降级', `chunk 被中断 ${chunkAborted} 次后未出现降级提示（疑似白屏）：${String(e).slice(0, 100)}`);
    }
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
