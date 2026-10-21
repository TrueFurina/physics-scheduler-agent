/**
 * 探针共用的浏览器装配：找 playwright-core、找 Chromium、统一启动参数。
 *
 * 为什么要单独一层：原先每个探针各自 `require('playwright-core')` 并**硬编码**
 * `chromium-1243\chrome-win64\chrome.exe`，于是产生两个隐形门槛：
 *  1. 必须手动设 `NODE_PATH` 指向受管 node workspace，否则 require 直接失败 ——
 *     这就是"探针一直只能手动跑、进不了 CI"的真实原因；
 *  2. 浏览器一升级（1243 → 下一个版本号），两个探针同时失效，且报错是
 *     "file not found"，看不出是版本漂移。
 *
 * 现在：模块解析按候选列表兜底，浏览器按已安装版本**自动发现**（取最新一个）。
 * 覆盖方式：`PW_CORE_PATH` 指定 playwright-core 目录、`CHROME` 指定浏览器可执行文件。
 */
const fs = require('fs');
const path = require('path');

function firstExisting(paths) {
  for (const p of paths) {
    try {
      if (p && fs.existsSync(p)) return p;
    } catch {
      /* 忽略探测失败 */
    }
  }
  return null;
}

/** 按优先级找 playwright-core：项目依赖 → 受管 workspace → 显式覆盖。 */
function loadPlaywrightCore() {
  const candidates = [
    'playwright-core',
    path.join(__dirname, '..', 'frontend', 'node_modules', 'playwright-core'),
    process.env.PW_CORE_PATH,
    path.join(
      process.env.USERPROFILE || '',
      '.workbuddy/binaries/node/workspace/node_modules/playwright-core',
    ),
  ].filter(Boolean);

  const tried = [];
  for (const id of candidates) {
    try {
      return require(id);
    } catch {
      tried.push(id);
    }
  }
  throw new Error(
    `找不到 playwright-core。已尝试：\n  ${tried.join('\n  ')}\n` +
      '请执行 `cd frontend && npm install`，或用 PW_CORE_PATH 指定目录。',
  );
}

/** 找浏览器可执行文件：CHROME 环境变量 → Playwright 缓存里最新的一份 → 系统 Chrome/Edge。 */
function resolveChrome() {
  if (process.env.CHROME) return process.env.CHROME;

  const cacheRoot = path.join(process.env.LOCALAPPDATA || '', 'ms-playwright');
  const fromCache = [];
  try {
    for (const dir of fs.readdirSync(cacheRoot)) {
      if (!dir.startsWith('chromium-')) continue;
      for (const tail of ['chrome-win64/chrome.exe', 'chrome-win/chrome.exe']) {
        const p = path.join(cacheRoot, dir, tail);
        if (fs.existsSync(p)) fromCache.push({ dir, p });
      }
    }
  } catch {
    /* 没有 Playwright 缓存就往下走 */
  }
  if (fromCache.length) {
    // 目录名形如 chromium-1243，取修订号最大的那个（字符串排序对 4 位数够用）
    fromCache.sort((a, b) => a.dir.localeCompare(b.dir, 'en', { numeric: true }));
    return fromCache[fromCache.length - 1].p;
  }

  const system = firstExisting([
    'C:/Program Files/Google/Chrome/Application/chrome.exe',
    'C:/Program Files (x86)/Google/Chrome/Application/chrome.exe',
    'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
    '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
    '/usr/bin/google-chrome',
    '/usr/bin/chromium',
  ]);
  if (system) return system;

  throw new Error('找不到可用的 Chromium/Chrome，请用 CHROME 环境变量指定可执行文件。');
}

/**
 * 启动浏览器。
 * `--no-proxy-server` 必加：本机代理会拦掉到 127.0.0.1 的请求，表现为"页面打不开"。
 */
async function launch(extraArgs = []) {
  const { chromium } = loadPlaywrightCore();
  const executablePath = resolveChrome();
  return chromium.launch({
    executablePath,
    headless: true,
    args: ['--no-proxy-server', '--disable-gpu', ...extraArgs],
  });
}

module.exports = { launch, loadPlaywrightCore, resolveChrome };
