// @vitest-environment node
/**
 * 依赖一致性守卫（R-125）。
 *
 * 为什么需要
 * ----------
 * 后端已经有一道 `test_requirements_installed.py`（R-124）—— 它抓到过
 * "本地装着 ortools 9.15、声明是 9.11"这种漂移。前端此前没有对应的东西。
 *
 * 前端比后端**安全一档**：有 `package-lock.json`（入库）+ CI 用 `npm ci`
 * （严格按 lock 装，不会像 `npm install` 那样偷偷升版本）。但仍有三种情况
 * 会让 CI 挂而本地看不出来：
 *
 *   ① `package.json` 改了范围但没重新生成 lock
 *      → `npm ci` 报 "lock file does not satisfy"，直接失败；
 *   ② lock 的 root 段与 `package.json` 不一致（本条实际抓到过：
 *      `playwright-core` 在 package.json 是精确 `1.64.1`、在 lock root 段
 *      却是 `^1.64.1`）—— 它能通过 `npm ci`（解析出的版本满足范围），
 *      但说明 lock 是手工改过/没同步的，下次真正不一致时不会被发现；
 *   ③ 依赖写在 `dependencies` 却在 `devDependencies` 里装（或反之）
 *      → 本地能跑、生产装不上（或反之）。
 *
 * 本文件锁 ① 和 ②。③ 需要判断"这个包是不是运行时必需"，机器判不准，不锁。
 *
 * 这些检查**不需要 node_modules**（只读两个 JSON），所以能在任何环境跑。
 */

import { describe, expect, it } from 'vitest';
import fs from 'node:fs';
import path from 'node:path';

const FRONTEND = path.resolve(__dirname, '..');
const PKG = path.join(FRONTEND, 'package.json');
const LOCK = path.join(FRONTEND, 'package-lock.json');

interface PkgJson {
  dependencies?: Record<string, string>;
  devDependencies?: Record<string, string>;
}

function readJson<T>(p: string): T {
  return JSON.parse(fs.readFileSync(p, 'utf-8')) as T;
}

describe('依赖一致性守卫', () => {
  it('package-lock.json 必须存在（CI 用 npm ci，没有它装不了）', () => {
    expect(fs.existsSync(LOCK)).toBe(true);
  });

  it('lock 的 root 段必须与 package.json 逐项一致', () => {
    // 这条就是实际抓到问题的那条：playwright-core 在 package.json 是
    // "1.64.1"、在 lock root 段是 "^1.64.1"。
    // 它当时**能通过 npm ci**（1.64.1 满足 ^1.64.1），所以 CI 不会红 ——
    // 正因如此才需要一条显式守卫：这种"能装但不同步"的状态会让 lock
    // 逐渐失真，等真出现不兼容时已经找不到是哪一次改歪的。
    const pkg = readJson<PkgJson>(PKG);
    const lock = readJson<{ packages: Record<string, PkgJson> }>(LOCK);
    const root = lock.packages[''] ?? {};

    const mismatches: string[] = [];
    for (const section of ['dependencies', 'devDependencies'] as const) {
      const want = pkg[section] ?? {};
      const got = root[section] ?? {};
      for (const [name, range] of Object.entries(want)) {
        if (!(name in got)) {
          mismatches.push(`${name}: package.json(${section}) 有 ${range}，lock root 段缺失`);
        } else if (got[name] !== range) {
          mismatches.push(`${name}: package.json=${range} vs lock=${got[name]}`);
        }
      }
      for (const name of Object.keys(got)) {
        if (!(name in want)) {
          mismatches.push(`${name}: lock root 段有 ${section} 记录，package.json 里没有`);
        }
      }
    }

    expect(mismatches).toEqual([]);
  });

  it('每个声明的依赖在 lock 里都有解析结果', () => {
    // `npm ci` 的第二类失败：lock 里缺某个包的解析（换了 package.json 但
    // 只跑了 `npm install --no-save`，或手工编辑过 lock）。
    const pkg = readJson<PkgJson>(PKG);
    const lock = readJson<{ packages: Record<string, { version?: string }> }>(LOCK);
    const declared = {
      ...(pkg.dependencies ?? {}),
      ...(pkg.devDependencies ?? {}),
    };

    const missing = Object.keys(declared).filter(
      (name) => !lock.packages[`node_modules/${name}`],
    );
    expect(missing).toEqual([]);
  });

  it('lock 里不得出现 file: / link: 这类本地路径依赖', () => {
    // 本地路径依赖在 CI 上必然装不上（那个路径只存在于作者机器）。
    // 本项目当前没有，这条是防止将来有人临时 `npm link` 后忘了还原。
    const lock = readJson<{ packages: Record<string, { resolved?: string }> }>(LOCK);
    const local: string[] = [];
    for (const [name, info] of Object.entries(lock.packages)) {
      if (!name.startsWith('node_modules/')) continue;
      const r = info.resolved ?? '';
      if (r.startsWith('file:') || r.startsWith('link:') || r.includes('..')) {
        local.push(`${name}: ${r}`);
      }
    }
    expect(local).toEqual([]);
  });
});
