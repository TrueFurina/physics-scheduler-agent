// @vitest-environment node
/**
 * R100：`/experiment-tutor` 的知识源降级信号必须走得到前端 API 面。
 *
 * 后端 R95 只判「实验项目」一维，R100 补上「实验室 / 实验教师」两维的
 * 部分缺口；两轮的信号都放在 `data` 里。但 `client.ts` 的返回类型
 * 一直是 `{ success, message, reply }` —— 类型里没有 `data`，
 * 调用方拿不到，也不可能被接线（硬规则 34：每加字段必扫所有类型声明）。
 *
 * 这里一半测纯函数（信号 → 就地文案），一半测接线：helper 写了还得
 * 类型里真的声明、`msg.ts` 里真的导出（规则 27 三处）。
 *
 * 诚实边界：该端点在 UI 上尚未被调用（`client.ts` 注释写着「端点保留」，
 * 实验规范查询页走的是云端流式问答）。所以信号止于 API 面与可复用
 * helper，不谎报「用户已看到」；接入 UI 需先拆 App.tsx（现压 1499/70）。
 */
import { describe, expect, it } from 'vitest';
import fs from 'node:fs';
import path from 'node:path';
import * as msg from '../msg';

type TutorData = {
  degraded?: boolean;
  knowledge_context_empty?: boolean;
  knowledge_gaps?: string[];
  warnings?: string[];
};

// 用命名空间取值而不是命名导入：helper 还不存在时模块仍能加载，
// 于是红因只落在真正缺的功能上（防放宽用例在红阶段照常绿）。
const fn = (msg as Record<string, unknown>)['experimentTutorWarningOf'] as
  | ((data?: TutorData | undefined) => string)
  | undefined;

const SRC = path.resolve(__dirname, '../..');
const clientSource = () => fs.readFileSync(path.resolve(SRC, 'api', 'client.ts'), 'utf8');
const msgSource = () => fs.readFileSync(path.resolve(SRC, 'utils', 'msg.ts'), 'utf8');
const appSource = () => fs.readFileSync(path.resolve(SRC, 'App.tsx'), 'utf8');

describe('experimentTutorWarningOf：知识源降级信号 → 就地提示', () => {
  it('非 true 一律安静（老后端 / 正常响应都不许狼来了）', () => {
    expect(typeof fn).toBe('function');
    expect(fn!(undefined)).toBe('');
    expect(fn!({})).toBe('');
    expect(fn!({ degraded: false, warnings: ['不该显示'] })).toBe('');
  });

  it('判据不许放宽成 truthy（字符串 / 数字 / 对象都不算故障）', () => {
    expect(typeof fn).toBe('function');
    expect(fn!({ degraded: 'RuntimeError' as never })).toBe('');
    expect(fn!({ degraded: 1 as never })).toBe('');
    expect(fn!({ degraded: {} as never })).toBe('');
    expect(fn!({ degraded: null as never })).toBe('');
  });

  it('判据不许放宽成「有 warnings 就报」', () => {
    expect(typeof fn).toBe('function');
    expect(fn!({ warnings: ['只有告警没有降级标志'] })).toBe('');
    expect(fn!({ knowledge_gaps: ['实验室'], warnings: ['旧字段'] })).toBe('');
  });

  it('degraded 为真时 warnings 原文必须上屏且带 ⚠️ 语气', () => {
    expect(typeof fn).toBe('function');
    const text = fn!({
      degraded: true,
      warnings: ['本校实验安排表知识源缺少实验室信息，回答中相关部分未引用本校数据'],
    });
    expect(text.startsWith('⚠️')).toBe(true);
    expect(text).toContain('缺少实验室信息');
  });

  it('多条 warnings 要一起说清，不能只显示第一条', () => {
    expect(typeof fn).toBe('function');
    const text = fn!({ degraded: true, warnings: ['第一条告警', '第二条告警'] });
    expect(text).toContain('第一条告警');
    expect(text).toContain('第二条告警');
  });

  it('warnings 缺失但 gaps 有时兜底，不能什么都不显示', () => {
    expect(typeof fn).toBe('function');
    const text = fn!({ degraded: true, knowledge_gaps: ['实验室', '实验教师'] });
    expect(text.startsWith('⚠️')).toBe(true);
    expect(text).toContain('实验室');
    expect(text).toContain('实验教师');
  });

  it('warnings 为空 / gaps 也为空时仍兜底到「知识源为空」', () => {
    expect(typeof fn).toBe('function');
    expect(fn!({ degraded: true, warnings: [] })).toContain('⚠️');
    expect(fn!({ degraded: true, knowledge_context_empty: true })).toContain('⚠️');
  });

  it('脏数据不许把 helper 弄炸（warnings / gaps 不是数组时安静兜底）', () => {
    expect(typeof fn).toBe('function');
    expect(fn!({ degraded: true, warnings: '不该是字符串' as never })).toContain('⚠️');
    expect(fn!({ degraded: true, knowledge_gaps: null as never })).toContain('⚠️');
    expect(fn!({ degraded: true, knowledge_gaps: ['实验室', 42 as never] })).toContain('⚠️');
  });
});

describe('接线守卫：类型声明 + helper 都得真的存在（规则 27/34）', () => {
  it('client.ts 的 experimentTutor 必须声明 data 的四个字段', () => {
    const c = clientSource();
    const seg = c.slice(c.indexOf('experimentTutor:'), c.indexOf('autoSchedule:'));
    expect(seg).toContain('degraded?: boolean');
    expect(seg).toContain('knowledge_context_empty?: boolean');
    expect(seg).toContain('knowledge_gaps?: string[]');
    expect(seg).toContain('warnings?: string[]');
    expect(seg).toContain('data?:');
  });

  it('msg.ts 必须导出 helper 且判据严格 === true', () => {
    expect(typeof fn).toBe('function');
    const s = msgSource();
    expect(s).toContain('export function experimentTutorWarningOf');
    expect(s).toContain('data?.degraded !== true');
  });

  it('已知缺口显式记录：UI 尚未调用该端点，App.tsx 体量不许顺手改坏', () => {
    const s = appSource();
    expect(s).not.toContain('experimentTutor(');
    expect(s.split('\n').length).toBeLessThanOrEqual(1500);
    expect((s.match(/useState/g) ?? []).length).toBeLessThanOrEqual(70);
    expect((s.match(/as any/g) ?? []).length).toBeLessThanOrEqual(20);
  });
});
