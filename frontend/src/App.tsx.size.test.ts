// @vitest-environment node
/**
 * `App.tsx` 体量的守卫 —— 让"又长回去了"变成一次红灯，而不是一年后的考古。
 *
 * 背景：拆视图那轮把 1711 行拆到 987 行，随后 5 个功能轮次净增 437 行回到 1424 行
 * （R-62 周网格 / R-61 落地 / 全校一键 / R-69 演示 / R-76 口径修正）。
 * 这不是"谁写错了"，而是**新状态默认写回 App** 的自然结果 —— 没有阈值时，
 * 每次改动单独看都合理，合起来就是单调增长。
 *
 * 为什么用阈值而不是继续拆：
 * 实测这一簇（并行排课 + 落地 + 周网格 + 看板）只占 210 行 / 14.7%，
 * 而它们要读 `_db` 之外 20 多个状态（selectedCourse / currentTeacher / adminAuthed…）。
 * 拆出去 = 20 多个 props 的搬运 + 一层回调上提，减行数为零、耦合反而更碎。
 * 所以本轮的选择是：**承认它是本项目的页面级 store**，用阈值守住不再劣化。
 * （这也正是上一轮"别做第三种（拆一半）"的具体落实：要么继续拆清，要么承认现状 +
 *   立个尺子。这里选后者，理由如上。）
 *
 * 想放宽阈值之前先回答一句：这次新增的状态，真的属于"页面级"吗？
 */
import { describe, expect, it } from 'vitest';
import fs from 'node:fs';
import path from 'node:path';

const APP = path.resolve(__dirname, 'App.tsx');

function appSource(): string {
  return fs.readFileSync(APP, 'utf-8');
}

describe('App.tsx 体量守卫', () => {
  it('行数不得超过 1500（拆视图后净增 437 行的教训）', () => {
    const lines = appSource().split('\n').length;
    expect(lines).toBeLessThanOrEqual(1500);
  });

  it('useState 数量不得超过 70（每个都是一份要手工同步的状态）', () => {
    const count = (appSource().match(/useState/g) ?? []).length;
    expect(count).toBeLessThanOrEqual(70);
  });

  it('不得再引入 any 泛滥：`as any` 不超过 20 处', () => {
    // 视图层的 any 已在拆视图那轮清零；App 层仍有历史遗留（导出结果、云 SDK 返回）。
    // 这里只锁"不再变多"，不要求一次清完 —— 一次清完会碰 20 处请求体，风险大于收益。
    const count = (appSource().match(/as any/g) ?? []).length;
    expect(count).toBeLessThanOrEqual(20);
  });

  it('视图组件必须保持懒加载（首屏关键路径 81.8 kB 的基线）', () => {
    const src = appSource();
    for (const view of ['ParallelView', 'DashboardView', 'ConsultView']) {
      expect(src).toContain(`lazy(() => import('./views/${view}'))`);
    }
  });
});
