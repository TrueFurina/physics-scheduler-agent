// @vitest-environment node
/**
 * R86：/load-default 的降级警告必须真的到得了首屏。
 *
 * 后端 R62/R51 已经把「重放失败 / 重放中断」写进 message，但前端首屏是
 * `await rescheduleApi.loadDefault();` —— 返回值整个丢弃。/status.loaded
 * 转 true 后前端再也不会调这个端点，所以这段警告只在冷启动那一次响应里
 * 出现，丢了就永久消失。
 *
 * 这里一半测纯函数（降级数据 → 横幅文案），一半测**接线**：光有 helper
 * 却没人调用等于没修，所以用源码守卫把「App.tsx 接住返回值」钉死。
 */
import { describe, expect, it } from 'vitest';
import fs from 'node:fs';
import path from 'node:path';
import * as msg from '../msg';

type LoadData = {
  degraded?: boolean;
  warnings?: string[];
};

// 用命名空间取值而不是命名导入：helper 还不存在时模块仍能加载，
// 于是「防放宽」用例在红阶段可以照常绿，红因只落在真正缺的功能上。
const fn = (msg as Record<string, unknown>)['loadWarningOf'] as
  | ((data?: LoadData | undefined) => string)
  | undefined;

const SRC = path.resolve(__dirname, '../..');
const appSource = () => fs.readFileSync(path.resolve(SRC, 'App.tsx'), 'utf8');
const clientSource = () => fs.readFileSync(path.resolve(SRC, 'api', 'client.ts'), 'utf8');

describe('loadWarningOf：降级数据 → 横幅文案', () => {
  it('没有降级时返回空串（不许制造狼来了）', () => {
    expect(typeof fn).toBe('function');
    expect(fn!({ degraded: false, warnings: [] })).toBe('');
    expect(fn!({ degraded: false, warnings: ['不该显示'] })).toBe('');
    expect(fn!(undefined)).toBe('');
  });

  it('降级为真时，每条 warning 都要上屏且带 ⚠️ 语气', () => {
    expect(typeof fn).toBe('function');
    const text = fn!({ degraded: true, warnings: ['2 条重放失败', '重放中断：库锁'] });
    expect(text.startsWith('⚠️')).toBe(true);
    expect(text).toContain('2 条重放失败');
    expect(text).toContain('重放中断：库锁');
  });

  it('降级为真但 warnings 缺失时也要兜底，不能什么都不显示', () => {
    expect(typeof fn).toBe('function');
    const text = fn!({ degraded: true });
    expect(text).toContain('⚠️');
    expect(text.length).toBeGreaterThan(2);
  });

  it('warnings 为空数组时同样兜底', () => {
    expect(typeof fn).toBe('function');
    expect(fn!({ degraded: true, warnings: [] })).toContain('⚠️');
  });

  it('degraded 缺失（老后端）时保持安静，向前兼容', () => {
    expect(typeof fn).toBe('function');
    expect(fn!({ warnings: ['旧字段'] })).toBe('');
    expect(fn!({})).toBe('');
  });
});

describe('接线守卫：helper 写了还得有人用', () => {
  it('App.tsx 必须接住 loadDefault() 的返回值并调用 loadWarningOf', () => {
    const src = appSource();
    expect(src).toContain('loadWarningOf(');
    expect(src).not.toMatch(/await rescheduleApi\.loadDefault\(\);/);
    expect(src).toMatch(/loadWarningOf\(.*loadDefault\(\)/s);
  });

  it('显示警告时不许覆盖用户刚看到的消息', () => {
    const src = appSource();
    expect(src).toContain('clearStaleLoadBanner(prev)');
    expect(src).toMatch(/setMsg\(\(prev\) => prev \|\| loadWarning\)/);
  });

  it('client.ts 的 loadDefault 返回类型必须声明降级字段', () => {
    const src = clientSource();
    const line = src.split('\n').find((l) => l.includes('loadDefault:')) ?? '';
    expect(line).toContain('degraded');
    expect(line).toContain('warnings');
  });

  it('防放宽：既有契约字段与体量守卫不能被顺手改坏', () => {
    const c = clientSource();
    const line = c.split('\n').find((l) => l.includes('loadDefault:')) ?? '';
    expect(line).toContain('message');
    expect(line).toContain('total_entries');
    expect(appSource().split('\n').length).toBeLessThanOrEqual(1500);
    expect(appSource()).toContain('clearStaleLoadBanner');
  });
});
