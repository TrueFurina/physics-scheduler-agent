#!/usr/bin/env node
/**
 * 前端测试的"数量守卫"（R61）
 *
 * 为什么需要它
 * ------------
 * 2026-10-31 实测：同一份代码，`vitest run` 连跑多次报出
 *
 *     17 files / 137 tests     ← 全了
 *     16 files / 128 tests
 *     16 files / 132 tests
 *     16 files / 134 tests     ← 少跑了文件，却依然 "all passed"，退出码 0
 *
 * 也就是说 **`npm test` 通过，不代表测试都跑了**。这一晚我据此写过两次"全绿"，
 * 事后核对才发现其中一次有 36 条用例根本没跑。
 *
 * 根因在宿主环境侧：本机的 vitest 偶尔有 worker 中途消失，掉队文件既不报错也不
 * 计入失败（日志里伴随一条 `node-brokered-fs-shim` 的内部错误，属运行环境）。
 * 换 `--pool=forks` 更糟（掉到 14 files / 85 tests），所以不从 pool 侧修。
 *
 * 这个守卫不修根因，只做两件事：
 *   1. **让"少跑了"变成红色**——判据是"不少于已知基线"，新增测试永远不会误报；
 *   2. **偶发掉队自动重试一次**——只有在连续两次都少时才算失败，避免把环境抖动
 *      变成日常红灯（红灯见多了，真问题也会被忽略）。
 *
 * 用法：`npm test`（已接到这个脚本上）
 *       原始行为：`npm run test:raw`
 */
import { spawnSync } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

// 已知基线：2026-10-31 在本机测到的最大值。新增测试不用改这里（判据是 >=）。
const FLOOR_FILES = 17;
const FLOOR_TESTS = 137;
const MAX_ATTEMPTS = 2;

const frontendDir = path.resolve(import.meta.dirname, '..', 'frontend');

function runOnce(attempt) {
  const reportFile = path.join(os.tmpdir(), `vitest-report-${process.pid}-${attempt}.json`);
  const run = spawnSync(
    'npx',
    ['vitest', 'run', '--reporter=default', '--reporter=json', `--outputFile=${reportFile}`],
    { cwd: frontendDir, stdio: 'inherit', shell: true },
  );

  let report = null;
  try {
    report = JSON.parse(fs.readFileSync(reportFile, 'utf8'));
  } catch {
    report = null;
  }
  try { fs.unlinkSync(reportFile); } catch { /* 忽略 */ }

  return {
    exit: run.status ?? 1,
    // 注意：`numTotalTestSuites` 数的是 describe 块（本仓库是 48），
    // 文件数是 `testResults.length`（17）。这两个数字差得很远，别拿错。
    files: report?.testResults?.length ?? 0,
    tests: report?.numTotalTests ?? 0,
    failed: report?.numFailedTests ?? 0,
    hasReport: report !== null,
  };
}

const problems = [];
for (let attempt = 1; attempt <= MAX_ATTEMPTS; attempt++) {
  const r = runOnce(attempt);
  const short = r.files < FLOOR_FILES || r.tests < FLOOR_TESTS;

  if (r.failed > 0) {
    console.error(`\n[vitest-guarded] 有失败用例：${r.failed} 条（vitest 退出码 ${r.exit}）`);
    process.exit(r.exit || 1);
  }
  if (!r.hasReport) {
    problems.push('读不到 JSON 报告（无法确认跑全）');
  } else if (short) {
    problems.push(`只跑了 ${r.files} files / ${r.tests} tests`);
  } else if (r.exit !== 0) {
    // 0 条失败但退出码非 0：本机实测过（宿主 fs shim 报 "Errors 1 error"）。
    // 不能当通过 —— 报告不完整的一轮，绿也说明不了什么。
    problems.push(`0 条失败但 vitest 自身报错退出（码 ${r.exit}）`);
  } else {
    console.log(
      `\n[vitest-guarded] ✅ ${r.files} files / ${r.tests} tests`
      + `（基线 ${FLOOR_FILES}/${FLOOR_TESTS}，第 ${attempt} 次尝试）`,
    );
    process.exit(0);
  }

  if (attempt < MAX_ATTEMPTS) {
    console.warn(`\n[vitest-guarded] 第 ${attempt} 次不完整：${problems.at(-1)}，重试一次…`);
  }
}

console.error(
  '\n[vitest-guarded] ❌ 连续 ' + MAX_ATTEMPTS + ' 次都没跑全：\n'
  + problems.map((p) => `    - ${p}`).join('\n') + '\n'
  + `    基线：${FLOOR_FILES} files / ${FLOOR_TESTS} tests\n`
  + '    这不是用例失败，是**运行环境**掉了 worker（见脚本顶部注释）。\n'
  + '    处理：重开一个终端再跑；若稳定复现，说明某个测试文件把 worker 弄崩了。\n',
);
process.exit(1);
