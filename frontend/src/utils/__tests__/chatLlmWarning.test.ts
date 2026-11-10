// @vitest-environment node
/**
 * R96：`/chat` 的 LLM 失败信号必须真的到得了教秘眼前。
 *
 * 后端 R94 已把 `llm_error` / `llm_error_detail` 放进响应，但前端
 * `grep -rn llm_error frontend/src` 曾为空 —— 类型里没有、代码里没人读，
 * 信号只走到 HTTP 响应为止。这里一半测纯函数（信号 → 就地文案），
 * 一半测**接线**：光有 helper 却没人调用等于没修（R86 原话），
 * 所以按规则 27 把「类型别名 + 导入/调用/透传 + 卡片渲染」三处都钉死。
 */
import { describe, expect, it } from 'vitest';
import fs from 'node:fs';
import path from 'node:path';
import * as msg from '../msg';

type ChatData = {
  llm_error?: boolean;
  llm_error_detail?: string | null;
};

// 用命名空间取值而不是命名导入：helper 还不存在时模块仍能加载，
// 于是红因只落在真正缺的功能上（防放宽用例在红阶段照常绿）。
const fn = (msg as Record<string, unknown>)['chatLlmWarningOf'] as
  | ((data?: ChatData | undefined) => string)
  | undefined;

const SRC = path.resolve(__dirname, '../..');
const appSource = () => fs.readFileSync(path.resolve(SRC, 'App.tsx'), 'utf8');
const clientSource = () => fs.readFileSync(path.resolve(SRC, 'api', 'client.ts'), 'utf8');
const viewSource = () => fs.readFileSync(path.resolve(SRC, 'views', 'RescheduleView.tsx'), 'utf8');

describe('chatLlmWarningOf：LLM 失败信号 → 就地提示', () => {
  it('非 true 一律安静（fast / 离线未配置 / 老后端都不许狼来了）', () => {
    expect(typeof fn).toBe('function');
    expect(fn!({ llm_error: false, llm_error_detail: '不该显示' })).toBe('');
    expect(fn!(undefined)).toBe('');
    expect(fn!({})).toBe('');
    expect(fn!({ llm_error_detail: '旧字段' })).toBe('');
  });

  it('失败为真时，具体原因必须上屏且带 ⚠️ 语气', () => {
    expect(typeof fn).toBe('function');
    const text = fn!({ llm_error: true, llm_error_detail: 'LLM 调用异常：upstream timeout' });
    expect(text.startsWith('⚠️')).toBe(true);
    expect(text).toContain('upstream timeout');
    expect(text).toContain('已改用系统规则推荐');
  });

  it('失败为真但 detail 缺失时兜底，不能什么都不显示', () => {
    expect(typeof fn).toBe('function');
    const text = fn!({ llm_error: true });
    expect(text.startsWith('⚠️')).toBe(true);
    expect(text.length).toBeGreaterThan(2);
  });

  it('detail 为 null / 空白串同样兜底', () => {
    expect(typeof fn).toBe('function');
    expect(fn!({ llm_error: true, llm_error_detail: null })).toContain('⚠️');
    expect(fn!({ llm_error: true, llm_error_detail: '   ' })).toContain('⚠️');
  });

  it('判据不许放宽成「有 detail 就报」', () => {
    expect(typeof fn).toBe('function');
    expect(fn!({ llm_error_detail: '只有详情没有失败标志' })).toBe('');
    expect(fn!({ llm_error: false, llm_error_detail: '显式 False' })).toBe('');
  });
});

describe('接线守卫：helper 写了还得有人用（规则 27 三处）', () => {
  it('client.ts 的 ChatResponse 必须声明两个降级字段', () => {
    const c = clientSource();
    const start = c.indexOf('export interface ChatResponse');
    const end = c.indexOf('export interface UploadResponse', start);
    const body = c.slice(start, end > 0 ? end : undefined);
    expect(body).toContain('llm_error?: boolean');
    expect(body).toContain('llm_error_detail');
  });

  it('App.tsx 必须导入、调用 helper 并把结果透传给视图', () => {
    const src = appSource();
    expect(src).toContain('chatLlmWarningOf');                     // 导入
    expect(src).toMatch(/setAiPlansWarning\(chatLlmWarningOf\(/);  // 调用
    expect(src).toContain('aiPlansWarning={aiPlansWarning}');       // 透传
  });

  it('RescheduleView 必须声明 prop 并真的渲染警告', () => {
    const src = viewSource();
    expect(src).toContain('aiPlansWarning?: string');
    expect(src).toMatch(/\{aiPlansWarning && \(/);
    expect(src).toContain('{aiPlansWarning}');
  });

  it('降级信号不能被「恰好没有候选」的显示条件吞掉', () => {
    expect(viewSource()).toMatch(/\(planLoading \|\| aiPlans\.length > 0 \|\| aiPlansWarning\)/);
  });
});

describe('防放宽：既有契约与体量守卫不许顺手改坏', () => {
  it('既有的 rule 来源文案与契约字段仍在', () => {
    expect(viewSource()).toContain("aiPlansSource === 'rule' ? '📋 系统推荐候选（AI 暂不可用）'");
    const c = clientSource();
    const body = c.slice(c.indexOf('export interface ChatResponse'));
    expect(body).toContain('ai_plans?: AiPlan[]');
    expect(body).toContain('course_unrecognized?: boolean');
    expect(appSource()).toContain('clearStaleLoadBanner');
  });

  it('App.tsx 体量守卫不许被放宽（R96 接线后正好压线 1500 / 70）', () => {
    const s = appSource();
    expect(s.split('\n').length).toBeLessThanOrEqual(1500);
    expect((s.match(/useState/g) ?? []).length).toBeLessThanOrEqual(70);
    expect((s.match(/as any/g) ?? []).length).toBeLessThanOrEqual(20);
  });
});
