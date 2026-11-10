// @vitest-environment node
/**
 * R98：学期起始日读失败 → 沙盘上的日期整整差 7 天，必须就地说明。
 *
 * 后端把 `date_degraded` 与 `date` 并列放进每条 slot（App.tsx 的
 * `setAvailable(r.data.data.available_slots || [])` 原样透传整条记录），
 * 所以这里一半测纯函数（信号 → 就地文案），一半测**接线**：
 * 光有 helper 却没人调用等于没修（R86 原话），按规则 27 把
 * 「类型别名 + 导入/调用 + 横幅渲染」三处都钉死。
 *
 * 本轮刻意不动 App.tsx（R96 后它压在 1500 行 / 70 个 useState 的体量守卫上）。
 */
import { describe, expect, it } from 'vitest';
import fs from 'node:fs';
import path from 'node:path';
import * as msg from '../msg';

type Slot = { date_degraded?: unknown };

// 用命名空间取值而不是命名导入：helper 还不存在时模块仍能加载，
// 于是红因只落在真正缺的功能上（防放宽用例在红阶段照常绿）。
const fn = (msg as Record<string, unknown>)['dateDegradedWarningOf'] as
  | ((slots?: Slot[] | undefined) => string)
  | undefined;

const SRC = path.resolve(__dirname, '../..');
const clientSource = () => fs.readFileSync(path.resolve(SRC, 'api', 'client.ts'), 'utf8');
const viewSource = () => fs.readFileSync(path.resolve(SRC, 'views', 'RescheduleView.tsx'), 'utf8');
const appSource = () => fs.readFileSync(path.resolve(SRC, 'App.tsx'), 'utf8');

describe('dateDegradedWarningOf：学期起始日降级信号 → 就地提示', () => {
  it('空数组 / undefined / 显式 false 一律安静', () => {
    expect(typeof fn).toBe('function');
    expect(fn!(undefined)).toBe('');
    expect(fn!([])).toBe('');
    expect(fn!([{}, { date_degraded: false }, {}])).toBe('');
  });

  it('任一条 === true 就必须上屏，且带 ⚠️ 语气', () => {
    expect(typeof fn).toBe('function');
    const text = fn!([{}, { date_degraded: true }]);
    expect(text.startsWith('⚠️')).toBe(true);
    expect(text).toContain('学期起始日');
    expect(text.length).toBeGreaterThan(8);
  });

  it('判据不许放宽成 truthy：字符串 / 数字 / 对象都不算故障', () => {
    expect(typeof fn).toBe('function');
    expect(fn!([{ date_degraded: 'RuntimeError' }])).toBe('');
    expect(fn!([{ date_degraded: 1 }])).toBe('');
    expect(fn!([{ date_degraded: {} }])).toBe('');
    expect(fn!([{ date_degraded: null }])).toBe('');
  });

  it('混合数组里只要有一条真故障就报（不被前面的正常条吞掉）', () => {
    expect(typeof fn).toBe('function');
    expect(fn!([{ date_degraded: false }, { date_degraded: false }, { date_degraded: true }]))
      .toContain('学期起始日');
  });
});

describe('接线守卫：helper 写了还得有人用（规则 27）', () => {
  it('client.ts 的 AvailableSlot / ExcludedSlot 都必须声明 date_degraded', () => {
    const c = clientSource();
    const available = c.slice(
      c.indexOf('export interface AvailableSlot'),
      c.indexOf('export interface ChatResponse'),
    );
    const excluded = c.slice(
      c.indexOf('export interface ExcludedSlot'),
      c.indexOf('export interface RescheduleRequest'),
    );
    expect(available).toContain('date_degraded?: boolean');
    expect(excluded).toContain('date_degraded?: boolean');
  });

  it('RescheduleView 必须导入、调用 helper 并真的渲染横幅', () => {
    const src = viewSource();
    expect(src).toContain('dateDegradedWarningOf');                       // 导入
    expect(src).toMatch(/dateDegradedWarningOf\(\[/);                     // 调用（合并两类 slot）
    expect(src).toMatch(/\{dateWarning && \(/);                          // 渲染条件
    expect(src).toContain('{dateWarning}');                              // 文案上屏
  });

  it('横幅必须放在沙盘内部，不能塞进 AI 候选卡片被显示条件吞掉', () => {
    const src = viewSource();
    const sandBox = src.indexOf('周课表沙盘（可视化网格视图）');
    const aiCard = src.indexOf('AI 候选方案（F9）');
    const banner = src.indexOf('{dateWarning && (');
    expect(sandBox).toBeGreaterThan(-1);
    expect(aiCard).toBeGreaterThan(sandBox);
    expect(banner).toBeGreaterThan(sandBox);
    expect(banner).toBeLessThan(aiCard);
  });

  it('本轮不许动 App.tsx：体量守卫保持压线，且信号不得绕经它', () => {
    const s = appSource();
    expect(s.split('\n').length).toBeLessThanOrEqual(1500);
    expect((s.match(/useState/g) ?? []).length).toBeLessThanOrEqual(70);
    expect((s.match(/as any/g) ?? []).length).toBeLessThanOrEqual(20);
    expect(s).not.toContain('dateDegradedWarningOf');
  });

  it('防放宽：既有降级 helper 与 R96 接线不许顺手改坏', () => {
    expect(appSource()).toContain('clearStaleLoadBanner');
    expect(appSource()).toContain('chatLlmWarningOf');
    expect(viewSource()).toContain('aiPlansWarning?: string');
    expect(viewSource()).toContain('{aiPlansWarning}');
  });
});

// ---------------------------------------------------------------------------
// R99：/impact-analysis 的 conflict_slots 是**重新构造**的 dict，R98 的
// date_degraded 在复制路上被丢掉了。后端补回后，前端必须真的接上：
// 类型别名 + 返回类型 + 横幅渲染，三处都钉死（规则 27）。
// ---------------------------------------------------------------------------
describe('接线守卫：影响分析页也要显示降级横幅（R99）', () => {
  const typesSource = () => fs.readFileSync(path.resolve(SRC, 'types.ts'), 'utf8');

  it('types.ts 的 ImpactResult 必须声明顶层与每条明细的 date_degraded', () => {
    const t = typesSource();
    const body = t.slice(t.indexOf('export interface ImpactResult'));
    expect(body).toContain('date_degraded?: boolean');
    expect(body).toContain('date_degraded_detail?: string | null');
    const slots = body.slice(body.indexOf('conflict_slots:'));
    expect(slots).toContain('date_degraded?: boolean');
  });

  it('client.ts 的 impactAnalysis 返回类型不许再是 any[]', () => {
    const c = clientSource();
    const seg = c.slice(c.indexOf('impactAnalysis:'), c.indexOf('parallelOverview:'));
    expect(seg).toContain('date_degraded?: boolean');
    expect(seg).not.toContain('conflict_slots: any[]');
  });

  it('RescheduleView 必须把 impact 明细交给 helper 并渲染横幅', () => {
    const src = viewSource();
    expect(src).toMatch(/dateDegradedWarningOf\(impact/);
    expect(src).toMatch(/\{impactDateWarning && \(/);
    expect(src).toContain('{impactDateWarning}');
  });

  it('横幅必须落在冲突明细块内，不能被沙盘横幅冒充', () => {
    const src = viewSource();
    const detail = src.indexOf('📋 冲突明细');
    const banner = src.indexOf('{impactDateWarning && (');
    expect(detail).toBeGreaterThan(-1);
    expect(banner).toBeGreaterThan(detail);
    expect(banner).toBeLessThan(src.indexOf('impact.conflict_slots!.map'));
  });

  it('不许动 App.tsx（体量守卫仍压线，信号不得绕经它）', () => {
    const s = appSource();
    expect(s.split('\n').length).toBeLessThanOrEqual(1500);
    expect((s.match(/useState/g) ?? []).length).toBeLessThanOrEqual(70);
    expect((s.match(/as any/g) ?? []).length).toBeLessThanOrEqual(20);
    expect(s).not.toContain('impactDateWarning');
  });
});
