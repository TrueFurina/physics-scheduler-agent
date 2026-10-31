// @vitest-environment jsdom
/**
 * 自动排课失败时，消息必须和成功区分开（R66）。
 *
 * R65 修了后端：失败时不再拼「定位说明」，而是直接回「没有可排课的教学任务」。
 * 但前端这一步没有对应的失败态处理：
 *
 *     const r = await rescheduleApi.autoSchedule();
 *     setAutoScheduleMsg(r.data.message);          // ← 不区分 success
 *     setAutoScheduleRows(...);
 *
 * `IdentityView` 里消息是灰底灰字（`bg-gray-50 text-gray-700`），
 * 成功和失败长得**完全一样**。
 *
 * 这和 R24 修过的是同一个问题（自然语言调课「零空闲时段」落进灰字兜底）：
 * 这套界面里 ❌ / 红色是**唯一**能区分"这条路走不通"的标记，
 * 没有它就等于把"没有结果"和"结果就是这样"说成了同一件事。
 *
 * 注意：R65 之后后端 message 已经是"没有可排课的教学任务"这种**中性陈述**，
 * 不带任何失败标记 —— 它把"怎么呈现"完全交给了前端。
 */
import { describe, expect, it } from 'vitest';
// 用 vite 的 ?raw 读源码：不依赖 @types/node（本仓库没装，与 R33 同法）
import appSrc from '../../../src/App.tsx?raw';

describe('自动排课的失败态（R66）', () => {
  const src = appSrc;

  /** 取出 handleAutoSchedule 的函数体。 */
  function body(): string {
    const start = src.indexOf('const handleAutoSchedule');
    const end = src.indexOf('// 一键自动排课的初稿导出', start);
    expect(start, 'handleAutoSchedule 不见了').toBeGreaterThan(-1);
    expect(end).toBeGreaterThan(start);
    return src.slice(start, end)
      .split(/\r?\n/)
      .filter((l) => !l.trim().startsWith('//'))
      .join('\n');
  }

  it('必须判断 success，失败时加 ❌', () => {
    const b = body();
    expect(b).toMatch(/success\s*===\s*false|!\s*r\.data\.success|success\s*\)/), (
      'handleAutoSchedule 没有判断 success —— 失败时会把中性文案当成功显示'
    );
    expect(b).toMatch(/❌/), '失败时没有可区分的标记（❌）';
  });

  it('失败时不能把上一次的方案行留在屏幕上', () => {
    const b = body();
    // 失败分支要清空 rows，否则"❌ 没有可排课的任务"+ 171 行旧方案同时出现
    const idx = b.search(/success\s*===\s*false|!\s*r\.data\.success/);
    if (idx < 0) return; // 上一条会报
    const tail = b.slice(idx, idx + 400);
    expect(tail).toMatch(/setAutoScheduleRows\(\[\]\)/), (
      '失败时没清空上一条方案的明细 —— 会和错误提示同时出现在屏幕上'
    );
  });
});
