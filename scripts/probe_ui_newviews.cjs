/**
 * R-64：新视图 UI 探针 —— 周课表网格（只读）+ 方案落地面板（写路径，自还原）
 *
 * 为什么单独一个文件，而不是并进 probe_ui_flow.cjs
 * ------------------------------------------------
 * ① 主流程探针默认打**线上**（发布已冻结在旧构建，没有这两个视图），并进去
 *    只会永远 FAIL；本探针默认打本地同源托管（SITE 可覆盖）。
 * ② 本探针含**写路径**（落地→撤销）。主流程探针的立身之本是"只读，不改任何
 *    数据"；两类探针混在一个文件里，那条承诺就不再成立了。
 * ③ 落地链依赖「一键排全校」出方案（LandPlanPanel 的 schedule 来源），
 *    与主流程的场景顺序不同，混排会互相拖长。
 *
 * 覆盖（与 API 级 probe_land_plan.py / probe_schedule_grid.py 同族，但验的是
 * **界面接线**：按钮点了有没有反应、结果滚没滚进视口、筛选/周次状态机）：
 *   G1-G7 周课表网格：七天全列（R-41 哨兵）/ 四时段 / 点格详情 / 周次切换
 *         （切走再切回，签名必须复原——抓"状态被污染"）/ 班级筛选 /
 *         冲突标记数与汇总条一致（N=0 时一个 ⚠️ 都不许有）
 *   L1-L6 落地链：预览不写入（/status 前后一个字节不变）/ 预览区完整落视口
 *         （R-44）/ 采纳写入 / 撤销还原 / 收尾 safety net（中途崩了也 API 兜底撤销）
 *   L0 未登录守卫：无管理员会话时预览可看、点采纳弹云端登录、且**什么都没写**
 *
 * 三态 PASS/FAIL/SKIP，退出码 0/1/2，与其余探针一致。
 */
const { launch } = require('./_browser.cjs');
const path = require('path');

const SITE = process.env.SITE || 'http://127.0.0.1:8023/';
const SHOT_DIR = process.env.PROBE_SHOT_DIR || path.join(require('os').tmpdir(), 'psprobe');
// 管理员凭据只用于走**真实**的 /admin-login 端点换会话令牌（服务端校验），
// 默认值与后端 security.py 的演示默认一致，可用环境变量覆盖。
const ADMIN_USER = process.env.ADMIN_USER || 'admin';
const ADMIN_PASS = process.env.ADMIN_PASS || 'admin123';

const results = [];
function rec(ok, name, detail, opts = {}) {
  const status = opts.skip ? 'SKIP' : ok ? 'PASS' : 'FAIL';
  results.push({ ok, skip: !!opts.skip, name, detail });
  console.log(`${status}  ${name}  — ${detail}`);
}

/** 在页面上下文里发同源 API 请求（带管理员令牌头，与前端拦截器同口径） */
async function api(page, method, url, body) {
  return page.evaluate(async ({ method, url, body }) => {
    const token = sessionStorage.getItem('ps_admin_token') || '';
    const r = await fetch(url, {
      method,
      headers: { 'Content-Type': 'application/json', 'X-Admin-Token': token },
      body: body ? JSON.stringify(body) : undefined,
    });
    const j = await r.json().catch(() => null);
    return { status: r.status, body: j };
  }, { method, url, body });
}

const getStatus = (page) => api(page, 'GET', '/api/reschedule/status');
const getLandStatus = (page) => api(page, 'GET', '/api/reschedule/parallel/land/status');
const getGrid = (page, q) => api(page, 'GET', `/api/reschedule/schedule-grid${q}`);

/** 全周内容签名：逐周拉 /schedule-grid 的 cells 拼成指纹（FNV-1a 短哈希）。
 *
 *  为什么判据必须是内容而不是条数：真实表上「17 条绪论保留 + 416 行方案」
 *  恰好 = 433 = 基线总条数 —— "写入但条数没变"与"根本没写入"在计数上
 *  完全无法区分（本轮实测撞见这个巧合，第一版 L2 就是这么假绿的）。
 *  撤销同理：恢复的是"提交那一刻的快照"，若预览已经偷写，快照本身就是
 *  脏的，条数对得上、内容回不去 —— 只有全周签名能抓住。 */
async function fullSignature(page) {
  return page.evaluate(async () => {
    const st = await (await fetch('/api/reschedule/status')).json();
    const range = st.week_range && st.week_range.length === 2 ? st.week_range : [1, 1];
    const parts = [];
    for (let w = range[0]; w <= range[1]; w++) {
      const r = await fetch(`/api/reschedule/schedule-grid?week=${w}`);
      const j = await r.json().catch(() => null);
      const cells = j && j.data ? j.data.cells : null;
      parts.push(`w${w}:` + (cells ? JSON.stringify(cells) : 'ERR'));
    }
    const s = parts.join(';');
    let h = 0x811c9dc5;
    for (let i = 0; i < s.length; i++) {
      h ^= s.charCodeAt(i);
      h = (h * 0x01000193) >>> 0;
    }
    return { hash: h.toString(16), len: s.length };
  });
}

/** 网格当前全部格子条目的文本签名（周次/筛选状态机的"内容指纹"） */
const gridSignature = (page) =>
  page.$$eval('main table tbody button', (btns) => btns.map((b) => (b.textContent || '').replace(/\s+/g, ' ').trim()).join('|'));

const summaryText = (page) => page.evaluate(() => document.body.innerText);

/** 等一个"只在目标状态出现"的串。绝不用页面静态文案当等待条件（R-25 假绿教训）。 */
async function waitForText(page, re, timeout, what) {
  await page.waitForFunction(
    (src) => new RegExp(src).test(document.body.innerText),
    re.source, { timeout },
  ).catch(async (e) => {
    const txt = await page.evaluate(() => document.body.innerText).catch(() => '');
    throw new Error(`${what}：${String(e).slice(0, 80)}｜页面文本尾部：${txt.slice(-200)}`);
  });
}

/** 元素矩形是否**完整**落在视口内（只断言顶边在视口内是假绿，R-44 实测过） */
async function fullyInViewport(page, selector) {
  return page.evaluate((sel) => {
    const el = document.querySelector(sel);
    if (!el) return { found: false, ok: false };
    const r = el.getBoundingClientRect();
    return {
      found: true, ok: r.top >= 0 && r.bottom <= window.innerHeight + 1,
      top: Math.round(r.top), bottom: Math.round(r.bottom), vh: window.innerHeight,
    };
  }, selector);
}

(async () => {
  const browser = await launch();

  // =====================================================================
  // 上下文 1：网格只读场景 + 落地写链（自还原）
  // =====================================================================
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  const page = await ctx.newPage();
  const jsErrors = [];
  const failedReq = [];
  page.on('pageerror', (e) => jsErrors.push(String(e).slice(0, 160)));
  page.on('requestfailed', (r) => {
    const u = r.url();
    if (!/res\.wx\.qq\.com|open\.weixin|workbuddy\.cn/.test(u)) failedReq.push(`${u.slice(0, 90)} ${r.failure()?.errorText}`);
  });
  // 落地/撤销都有 window.confirm：统一接受。必须在点击前注册。
  page.on('dialog', (d) => d.accept());

  try {
    await page.goto(SITE, { waitUntil: 'domcontentloaded', timeout: 60000 });
    await page.waitForSelector('text=物理实验调课辅助', { timeout: 60000 });

    // 安排表必须已加载（探针前提，不是被测功能）
    let st = await getStatus(page);
    if (st.body && !st.body.loaded) {
      await api(page, 'POST', '/api/reschedule/load-default');
      st = await getStatus(page);
    }
    if (!st.body || !st.body.loaded) {
      rec(false, '探针前提：安排表已加载', `/status 未就绪：HTTP ${st.status}`);
      throw new Error('安排表未加载，后续场景无从测起');
    }

    // 管理员会话：走真实 /admin-login 换令牌写 sessionStorage，再 reload 让
    // App 从 sessionStorage 恢复 adminAuthed（与刷新页面同一路径）。
    // 不碰云端登录模态——那是审批链探针的辖区，与写接口鉴权无关。
    const login = await page.evaluate(async ({ u, p }) => {
      const r = await fetch('/api/reschedule/admin-login', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username: u, password: p }),
      });
      const j = await r.json().catch(() => null);
      if (j && j.token) sessionStorage.setItem('ps_admin_token', j.token);
      return { status: r.status, ok: !!(j && j.token) };
    }, { u: ADMIN_USER, p: ADMIN_PASS });
    if (!login.ok) {
      rec(false, '探针前提：管理员会话', `admin-login HTTP ${login.status}（默认 admin/admin123，可用 ADMIN_USER/ADMIN_PASS 覆盖）`);
      throw new Error('拿不到管理员令牌，落地写链无从测起');
    }
    await page.reload({ waitUntil: 'domcontentloaded', timeout: 60000 });
    await page.waitForSelector('text=物理实验调课辅助', { timeout: 60000 });

    // 前置清理：上次探针若崩在"已落地"状态，这里先撤销，保证基线干净。
    // 然后再 load-default 回文件基线 —— 若上次是"预览偷写"型崩溃（撤销
    // 恢复的是脏快照），只有整表重载能修好。load-default 会重放 landed_plans，
    // 所以顺序必须是先 undo 清账本、再重载。
    const pre = await getLandStatus(page);
    if (pre.body && pre.body.landed) {
      const un = await api(page, 'POST', '/api/reschedule/parallel/land/undo');
      rec(un.status === 200, '前置清理：撤销上次残留落地', `undo HTTP ${un.status}`);
    }
    await api(page, 'POST', '/api/reschedule/load-default');
    const baseline = (await getStatus(page)).body.total_entries;
    const baselineSig = await fullSignature(page);

    // ------------------------------------------------------------------
    // G1-G7：周课表网格（只读）
    // ------------------------------------------------------------------
    try {
      await page.getByRole('button', { name: /周课表/ }).first().click();
      // 等待条件：「第 N 周 · M 条安排」汇总条——接口回来后才有的串。
      // 不能用"网格生成中..."的消失或"周课表"标题（导航按钮文案，一进页面就有）。
      await waitForText(page, /第 \d+ 周 · \d+ 条安排/, 40000, 'G1 网格加载');
      const g1 = await summaryText(page);
      const mWeek = g1.match(/第 (\d+) 周 · (\d+) 条安排/);
      // 与 API 对账，而不是拍一个"每周应该有多少条"的常数——真实表周次分布
      // 极不均（第 1 周 6 条、第 9 周 45 条），写死下限必假红。
      const apiGrid = await getGrid(page, '?week=1');
      const apiN = apiGrid.body && apiGrid.body.data ? apiGrid.body.data.totals.entries : null;
      rec(!!mWeek && apiN !== null && Number(mWeek[2]) === apiN,
        'G1 周课表加载', `第 ${mWeek ? mWeek[1] : '?'} 周 UI 显示 ${mWeek ? mWeek[2] : '?'} 条，API 同参数 ${apiN} 条（对账一致）`);
      await page.screenshot({ path: path.join(SHOT_DIR, 'newviews-grid.png') });

      // G2：七天全列。前端旧常量 labels.ts 是 5 天口径（R-41：拿枚举当现实），
      // 真实表 71/433 在周末 —— 少一列就是静默丢课，这里是 UI 层哨兵。
      const head = await page.$$eval('main table thead th', (ths) => ths.map((t) => (t.textContent || '').trim()));
      rec(head.length === 8 && head.includes('周六') && head.includes('周日'),
        'G2 七天全列', `表头 ${head.length - 1} 天：${head.slice(1).join('/')}`);

      // G3：四时段全列（time_model.ORDER：上午/下午一/下午二/晚上）。
      // 旧沙盘口径只有 3 个时段，上午 4 条真实记录会被挤掉。
      const body = await page.$$eval('main table tbody tr', (trs) =>
        trs.map((tr) => (tr.querySelector('td') ? tr.querySelector('td').textContent || '' : '').replace(/\s+/g, ' ').trim()));
      rec(body.length === 4 && /上午/.test(body[0]) && /下午一/.test(body[1]) && /下午二/.test(body[2]) && /晚上/.test(body[3]),
        'G3 四时段全列', `${body.length} 行：${body.join('｜')}`);

      // G4：点格详情。条目是按钮，点开必须给出全量字段（不只格子里的摘要）。
      const cellCount = await page.locator('main table tbody button').count();
      if (cellCount > 0) {
        await page.locator('main table tbody button').first().click();
        await waitForText(page, /班级\/组：/, 10000, 'G4 点格详情');
        const det = await summaryText(page);
        rec(/教师：/.test(det) && /场地：/.test(det) && /时段：/.test(det),
          'G4 点格详情', `详情区含 班级/组+教师+场地+时段（首格 ${cellCount ? '已点开' : ''}，本周共 ${cellCount} 个条目按钮）`);
      } else {
        rec(false, 'G4 点格详情', '网格里一个条目按钮都没有（第 1 周真实表不可能为空）');
      }

      // G5：周次切换 + 切回复原。切周次后若还在显示旧周数据（或状态被污染），
      // 签名对比直接抓出来——"点了没反应"的机器判据。
      const sigW1 = await gridSignature(page);
      await page.locator('main select').first().selectOption({ label: '第 9 周' });
      await waitForText(page, /第 9 周 · \d+ 条安排/, 30000, 'G5 切到第 9 周');
      await page.waitForTimeout(300);
      const sigW9 = await gridSignature(page);
      await page.locator('main select').first().selectOption({ label: '第 1 周' });
      await waitForText(page, /第 1 周 · \d+ 条安排/, 30000, 'G5 切回第 1 周');
      await page.waitForTimeout(300);
      const sigBack = await gridSignature(page);
      rec(sigW1 !== sigW9 && sigBack === sigW1,
        'G5 周次切换与复原',
        `第 1 周 ${sigW1.split('|').length} 条 → 第 9 周 ${sigW9.split('|').length} 条（${sigW1 !== sigW9 ? '内容已变' : '内容没变！'}）`
        + ` → 切回第 1 周 ${sigBack === sigW1 ? '签名复原' : '签名被污染！'}`);

      // G6：班级筛选。判据不是"条数变少"——真实表周次分布不均，所选班级
      // 当周可能本来就 0 条（本轮实测：第 1 周共 6 条，任选一个班都可能是 0）。
      // 真正的机器判据是 **UI 与 API 同参数对账**：筛了等于没筛（UI 显示全表）
      // 或筛掉了不该筛的（UI 比 API 少），都会在对账里现形。
      const clsSel = page.locator('main select').nth(1);
      const clsOptions = await clsSel.locator('option').allTextContents();
      const beforeN = Number(((await summaryText(page)).match(/第 1 周 · (\d+) 条安排/) || [])[1] || 0);
      await clsSel.selectOption({ index: 1 });
      await page.waitForFunction(
        (prev) => {
          const m = document.body.innerText.match(/第 1 周 · (\d+) 条安排/);
          return m && Number(m[1]) !== prev;
        }, beforeN, { timeout: 30000 },
      ).catch(() => { /* 下面按实际文本判，超时记 FAIL 不猜 */ });
      await page.waitForTimeout(300);
      const filteredTxt = await summaryText(page);
      const afterN = Number((filteredTxt.match(/第 1 周 · (\d+) 条安排/) || [])[1] || 0);
      const picked = clsOptions[1];
      const apiFiltered = await getGrid(page, `?week=1&class_name=${encodeURIComponent(picked)}`);
      const apiFN = apiFiltered.body && apiFiltered.body.data ? apiFiltered.body.data.totals.entries : null;
      const cellTexts = await page.$$eval('main table tbody button', (bs) => bs.map((b) => (b.textContent || '').replace(/\s+/g, ' ')));
      const allMatch = cellTexts.every((t) => t.includes(picked));
      rec(apiFN !== null && afterN === apiFN && allMatch,
        'G6 班级筛选', `选「${picked}」：UI ${beforeN} → ${afterN} 条，API 同参数 ${apiFN} 条`
        + `，可见条目 ${cellTexts.length} 个${allMatch ? '全部属于该班' : '存在不属于该班的条目！'}`);
      // 复位筛选，别把状态留给 G7
      await clsSel.selectOption({ label: '全部' });
      await page.waitForFunction(
        (prev) => {
          const m = document.body.innerText.match(/第 1 周 · (\d+) 条安排/);
          return m && Number(m[1]) === prev;
        }, beforeN, { timeout: 30000 },
      );

      // G7：冲突标记与汇总条一致。汇总说 N 条冲突，网格里就必须有 N 个条目
      // 带 ⚠️；说 0 就一个都不许有——"计数与标记对不上"比没标记更坏
      // （教秘会以为自己数错了）。
      const g7txt = await summaryText(page);
      const nConf = Number((g7txt.match(/⚠ 冲突条目 (\d+)/) || [])[1] ?? -1);
      const marked = await page.$$eval('main table tbody button', (bs) => {
        const hit = bs.filter((b) => b.querySelector('span[title]'));
        return [...new Set(hit.map((b) => (b.textContent || '').replace(/\s+/g, ' ').trim()))];
      });
      rec(nConf >= 0 && marked.length === nConf,
        'G7 冲突标记一致', `汇总条 ⚠ 冲突条目 ${nConf}，网格实际标记 ${marked.length} 个条目`
        + (nConf === 0 ? '（本周无冲突，零标记正确）' : `（${marked.slice(0, 2).join('；')}${marked.length > 2 ? ' 等' : ''}）`));
    } catch (e) {
      rec(false, '📅 周课表网格', String(e).slice(0, 160));
    }

    // ------------------------------------------------------------------
    // L1-L6：落地写链（管理员会话已就绪；自还原 + 收尾 safety net）
    // ------------------------------------------------------------------
    let landedAtEnd = false;
    try {
      await page.getByRole('button', { name: /并行排课/ }).first().click();
      await page.waitForFunction(() => document.body.innerText.includes('2024级'), null, { timeout: 40000 });

      // L1：全校求解出方案（LandPlanPanel 的 schedule 来源；单班结果不进该面板）
      const tS = Date.now();
      await page.getByRole('button', { name: /一键排全校/ }).first().click();
      await waitForText(page, /全校一键排课 · \d+\/\d+ 个班排定/, 240000, 'L1 一键排全校');
      const planTxt = await summaryText(page);
      const mPlan = planTxt.match(/全校一键排课 · (\d+)\/(\d+) 个班排定/);
      rec(!!mPlan && mPlan[1] === mPlan[2], 'L1 一键排全校（方案来源）',
        `${Date.now() - tS}ms，${mPlan ? `${mPlan[1]}/${mPlan[2]} 班` : '未见解'}`);

      // L2：预览不写入。/status 前后必须一个字节不变，land/status 必须仍未落地。
      // 这条是"先看差异再写入"产品承诺的机器判据（预览偷写＝教秘被代表）。
      await page.getByRole('button', { name: /落地为正式安排表/ }).first().click();
      await waitForText(page, /🔍 落地预览（未写入）/, 60000, 'L2 落地预览');
      const previewTxt = await summaryText(page);
      const afterPreview = await getStatus(page);
      const landAfterPreview = await getLandStatus(page);
      const sigAfterPreview = await fullSignature(page);
      const contentIntact = sigAfterPreview.hash === baselineSig.hash;
      rec(afterPreview.body && afterPreview.body.total_entries === baseline
        && landAfterPreview.body && landAfterPreview.body.landed === false && contentIntact,
        'L2 预览不写入',
        `预览后 total_entries ${afterPreview.body ? afterPreview.body.total_entries : '?'}（基线 ${baseline}），`
        + `landed=${landAfterPreview.body ? landAfterPreview.body.landed : '?'}，`
        + `内容签名 ${sigAfterPreview.hash}${contentIntact ? '（与基线一致）' : ` ≠ 基线 ${baselineSig.hash}——预览写入了！`}`);
      // 预览账目：新增+调整 > 0 是"确认采纳"按钮出现的前提（has_changes），
      // 也是 L4 判断"内容真的换了"的依据——见下。
      const mAdd = (previewTxt.match(/新增 (\d+)/) || [])[1];
      const mChg = (previewTxt.match(/调整 (\d+)/) || [])[1];
      const nDiff = (Number(mAdd) || 0) + (Number(mChg) || 0);

      // L3：预览区必须完整滚进视口（R-44：入口在页面深处，差异区若在折叠线
      // 下面静静展开＝"点了没反应"）。
      const vis = await fullyInViewport(page, 'h4');
      const h4ok = await page.evaluate(() => {
        const h = [...document.querySelectorAll('h4')].find((x) => /落地预览/.test(x.textContent || ''));
        return !!h;
      });
      rec(h4ok && vis.ok, 'L3 预览区落视口',
        h4ok ? (vis.ok ? '差异区完整在视口内' : `差异区掉出视口（top=${vis.top} bottom=${vis.bottom} 视口 ${vis.vh}）`) : '找不到「落地预览」标题');
      await page.screenshot({ path: path.join(SHOT_DIR, 'newviews-land-preview.png') });

      // L4：采纳写入。
      // ⚠️ 判据**不能**是"总条数变了"：apply_plan = kept(非可排/未覆盖原样)
      // + 方案全量行，真实表上 17 条绪论保留 + 416 行方案恰好 = 433 = 基线，
      // 总条数一模一样但内容已换（本轮实测撞上这个巧合）。
      // 机器上可证的判据是三条合起来：① landed=true；② /status 与横幅报的
      // after_entries 一致；③ 预览账目 新增+调整 > 0（按钮只在 has_changes
      // 时出现，故内容必变——diff_plan 与 apply_plan 同一套 unit_key 口径）。
      await page.getByRole('button', { name: /确认采纳/ }).first().click();
      await waitForText(page, /✅ 落地结果/, 60000, 'L4 确认采纳');
      const bannerTxt = await summaryText(page);
      const afterCommit = await getStatus(page);
      const landAfterCommit = await getLandStatus(page);
      landedAtEnd = !!(landAfterCommit.body && landAfterCommit.body.landed);
      const mAfter = (bannerTxt.match(/安排表 \d+ → (\d+) 条/) || [])[1];
      const afterMatch = mAfter !== undefined && afterCommit.body
        && Number(mAfter) === afterCommit.body.total_entries;
      rec(landedAtEnd && afterMatch && nDiff > 0,
        'L4 采纳写入',
        `landed=${landedAtEnd}，${mAfter !== undefined ? `安排表 → ${mAfter} 条` : '横幅未见条数'}`
        + `（/status ${afterCommit.body ? afterCommit.body.total_entries : '?'}，${afterMatch ? '一致' : '不一致！'}），`
        + `预览账目 新增+调整 ${nDiff} 处${nDiff > 0 ? '（内容必变）' : '（无变化却有提交按钮？）'}`
        + (landAfterCommit.body && landAfterCommit.body.plan_rows ? `，${landAfterCommit.body.plan_rows} 行方案` : ''));

      // L5：撤销还原。必须**精确**回到基线：条数对 + 全周内容签名对。
      // 只断言条数是假绿——预览若偷写，提交快照本身就是脏的，撤销后条数
      // 照样对得上，内容却回不去（413 巧合的镜像坑）。
      await page.getByRole('button', { name: /撤销落地/ }).first().click();
      await waitForText(page, /已撤销落地/, 60000, 'L5 撤销落地');
      const afterUndo = await getStatus(page);
      const landAfterUndo = await getLandStatus(page);
      landedAtEnd = !!(landAfterUndo.body && landAfterUndo.body.landed);
      const sigAfterUndo = await fullSignature(page);
      const restored = afterUndo.body && afterUndo.body.total_entries === baseline
        && !landedAtEnd && sigAfterUndo.hash === baselineSig.hash;
      rec(restored, 'L5 撤销还原',
        `total_entries ${afterUndo.body ? afterUndo.body.total_entries : '?'}（基线 ${baseline}），landed=${landedAtEnd}，`
        + `内容签名 ${sigAfterUndo.hash}${sigAfterUndo.hash === baselineSig.hash ? '（与基线一致）' : ` ≠ 基线 ${baselineSig.hash}——内容没回去！`}`);
      await page.screenshot({ path: path.join(SHOT_DIR, 'newviews-land-undone.png') });
    } catch (e) {
      rec(false, '📥 方案落地链', String(e).slice(0, 160));
    } finally {
      // safety net：任何一步崩了，只要还是落地状态就 API 兜底撤销；
      // 撤销后再验一次全周签名——若内容仍与基线不符（预览偷写型崩溃，
      // 撤销恢复的是脏快照），load-default 整表重载修好它。
      // 绝不能把"改了没还原"留给下一个跑探针的人（假基线比假绿更毒）。
      try {
        const ls = await getLandStatus(page);
        if (ls.body && ls.body.landed) {
          const un = await api(page, 'POST', '/api/reschedule/parallel/land/undo');
          rec(un.status === 200, '收尾 safety net：兜底撤销',
            `探针中断时仍是落地状态，undo HTTP ${un.status}${un.status === 200 ? '' : '（请手动执行撤销！）'}`);
          landedAtEnd = false;
        }
        const sig = await fullSignature(page);
        if (sig.hash !== baselineSig.hash) {
          await api(page, 'POST', '/api/reschedule/load-default');
          const sig2 = await fullSignature(page);
          rec(sig2.hash === baselineSig.hash, '收尾 safety net：基线修复',
            `探针结束后内容与基线不符（${sig.hash} ≠ ${baselineSig.hash}），已 load-default 重载，`
            + `复验 ${sig2.hash === baselineSig.hash ? '与基线一致' : '仍不符（请手动 load-default！）'}`);
        }
      } catch { /* 页面可能已崩，忽略 */ }
    }

    rec(jsErrors.length === 0, '全程 JS 错误', `${jsErrors.length} 条${jsErrors.length ? '：' + jsErrors[0] : ''}`);
    rec(failedReq.length === 0, '全程失败请求', `${failedReq.length} 条${failedReq.length ? '：' + failedReq[0] : ''}`);
    await ctx.close();
  } catch (e) {
    rec(false, '探针前提', String(e).slice(0, 160));
    await ctx.close().catch(() => {});
  }

  // =====================================================================
  // 上下文 2：未登录守卫（全新上下文＝无 sessionStorage，天然未登录态）
  // 预览可看、点采纳弹云端登录、且什么都没写 —— 三步缺一不可：
  // 只验"弹了登录"不验"没写入"，就漏掉"前端拦了后端没拦"那类真 bug。
  // =====================================================================
  try {
    const ctx2 = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
    const p2 = await ctx2.newPage();
    p2.on('dialog', (d) => d.accept());
    await p2.goto(SITE, { waitUntil: 'domcontentloaded', timeout: 60000 });
    await p2.waitForSelector('text=物理实验调课辅助', { timeout: 60000 });
    const base2 = (await getStatus(p2)).body;
    // 开场内容签名：L0 的"什么都没写"以它为准，不能以 landed 查询为准——
    // land/status 与落地/撤销共用 parallel-land 桶（20 次/60s），探针一轮跑下来
    // 超限后这里会 429（body 无 landed 字段），"查询失败≠被写"。
    // 且签名判据比 landed 更强：413 巧合下"写入但条数不变"只有签名能抓住。
    const base2Sig = await fullSignature(p2);
    await p2.getByRole('button', { name: /并行排课/ }).first().click();
    await p2.waitForFunction(() => document.body.innerText.includes('2024级'), null, { timeout: 40000 });
    await p2.getByRole('button', { name: /一键排全校/ }).first().click();
    await waitForText(p2, /全校一键排课 · \d+\/\d+ 个班排定/, 240000, 'L0 求解');
    await p2.getByRole('button', { name: /落地为正式安排表/ }).first().click();
    await waitForText(p2, /🔍 落地预览（未写入）/, 60000, 'L0 预览');
    const guardTxt = await p2.evaluate(() => document.body.innerText);
    const hintShown = /采纳需管理员登录/.test(guardTxt);
    // 点采纳：必须弹云端登录（守卫接线），且安排表一个字节都没变
    await p2.getByRole('button', { name: /确认采纳/ }).first().click();
    let modalShown = false;
    try {
      await p2.waitForFunction(() => /☁️ 云端登录/.test(document.body.innerText), null, { timeout: 15000 });
      modalShown = true;
    } catch { /* 下面按 false 判 */ }
    const after2 = await getStatus(p2);
    const land2 = await getLandStatus(p2);
    const sigAfter2 = await fullSignature(p2);
    // landed 查询可用时才纳入判据；429（限流）时 body 没有 landed 字段，
    // 此时只凭内容判：条数 + 全周签名都与开场一致 ⇒ 没写。
    const land2Usable = land2.body && typeof land2.body.landed === 'boolean';
    const nothingWritten = after2.body && after2.body.total_entries === base2.total_entries
      && sigAfter2.hash === base2Sig.hash
      && (!land2Usable || land2.body.landed === false);
    rec(hintShown && modalShown && nothingWritten,
      'L0 未登录守卫',
      `提示「采纳需管理员登录」${hintShown ? '有' : '无'}，点采纳弹云端登录${modalShown ? '有' : '无'}，`
      + `安排表 ${after2.body ? after2.body.total_entries : '?'} 条（开场 ${base2.total_entries}），`
      + `内容签名 ${sigAfter2.hash}${sigAfter2.hash === base2Sig.hash ? '（与开场一致）' : ` ≠ 开场 ${base2Sig.hash}——内容被改了！`}，`
      + (land2Usable ? `landed=${land2.body.landed}` : `landed 查询不可用（HTTP ${land2.status || '?'}，限流不以它判冤）`));
    await p2.screenshot({ path: path.join(SHOT_DIR, 'newviews-land-guard.png') });
    await ctx2.close();
  } catch (e) {
    rec(false, 'L0 未登录守卫', String(e).slice(0, 160));
  }

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
