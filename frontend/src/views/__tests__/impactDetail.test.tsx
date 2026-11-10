// @vitest-environment jsdom
/**
 * 影响分析：40 处冲突不能只给一句"共 40 处"（R47）。
 *
 * 后端返回（实测，真实数据）：
 *
 *     conflict_count : 40
 *     conflict_slots : 10 条明细（weekday/time_slot/room/date/reason 齐全）
 *     affected_classes: 13 个班
 *     affected_rooms  : 4 间
 *     affected_teachers: 8 位
 *
 * 前端 `RescheduleView` 渲染了：
 *   ✓ 「共 40 处冲突」
 *   ✓ 受影响班级 / 教室 / 教师（聚合后的名单）
 *   ✗ **明细一条都没渲染** —— `grep -c conflict_slots RescheduleView.tsx` = 0
 *
 * 后果不是"少看一点"：教秘要判断"这次调课到底撞了谁的课"，
 * 聚合名单给不了答案。他知道"影响了 13 个班"，但不知道
 * "哪一周、星期几、第几节、哪间教室、撞的是哪门课"。
 *
 * 而且 `conflict_count` 是 40、明细只有 10 条 —— 这个截断
 * 在字段名上也看不出来。不展示明细的话，连"被截断了"都无从知道。
 *
 * 这与 R33（提示写进 message 但没人读）同族：
 * **后端给了数据，前端没展示 = 用户不知道。**
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render } from '@testing-library/react';
import type { TeacherCourse } from '../../api/client';
import RescheduleView from '../RescheduleView';

afterEach(cleanup);

const course = (): TeacherCourse => ({
  course_name: '声速测量',
  class_name: '2024级1班',
  original_time: '周三 13:30-15:45',
  room: '物理楼101',
  week_range: '1-16周',
  total_slots: 16,
  adjustable: true,
  adjustable_reason: '',
});

const impact = {
  conflict_count: 40,
  affected_classes: ['2024级计科3班和2024软工3班', '2024级制药工程'],
  affected_rooms: ['A412-2', 'A504'],
  affected_teachers: ['教师C', '教师H'],
  self_busy_count: 0,
  self_busy_slots: [],
  conflict_slots: [
    {
      weekday: '周一', time_slot: '567', room: 'A412-2', date: '03月10日',
      reason_type: 'classroom_busy',
      reason: '教室已被占用（2024级计科3班和2024软工3班 伏安法测二极管的特性曲线）',
    },
    {
      weekday: '周一', time_slot: '567', room: 'A504', date: '03月10日',
      reason_type: 'classroom_busy',
      reason: '教室已被占用（2024级数据科学与大数据技术1、2班 扭摆法测物体转动惯量）',
    },
  ],
};

const base = {
  selectedCourse: course(),
  currentTeacher: '吴琳',
  targetWeek: 4,
  available: [],
  excluded: [],
  showAllAvail: false,
  impactLoading: false,
  requestStatus: '',
  loading: false,
  onBack: vi.fn(),
  onSelectCourse: vi.fn(),
  onTargetWeekChange: vi.fn(),
  onToggleShowAll: vi.fn(),
  onImpactAnalysis: vi.fn(),
  onSubmit: vi.fn(),
};

describe('RescheduleView · 影响分析明细（R47）', () => {
  it('冲突明细的时段/教室/原因必须出现在页面上', () => {
    render(<RescheduleView {...base} impact={impact as never} />);
    const text = document.body.textContent || '';

    // 聚合视图本来就有
    expect(text).toContain('40');

    // 明细：至少能看到"哪间教室 / 什么原因"
    expect(text).toContain('A412-2');
    expect(text).toMatch(/伏安法测二极管的特性曲线/);
  });

  it('明细被截断时必须说明（count 40 / 明细 10）', () => {
    render(<RescheduleView {...base} impact={impact as never} />);
    const text = document.body.textContent || '';
    // 后端只回 10 条明细，而 conflict_count 是 40
    expect(
      /仅显示|前\s*\d+|另有\s*\d+|共\s*40/.test(text),
      '明细有截断但页面没说 —— 教秘会以为这 10 条就是全部',
    ).toBe(true);
  });

  it('没有冲突明细时不渲染空区块', () => {
    const empty = { ...impact, conflict_count: 0, conflict_slots: [] };
    render(<RescheduleView {...base} impact={empty as never} />);
    const text = document.body.textContent || '';
    expect(text).not.toMatch(/冲突明细/);
  });
});

// ---------------------------------------------------------------------------
// R64：规则降级候选不能显示成「🤖 AI 推荐」
//
// 后端在 LLM 不可用时会规则降级给 3 个候选（好事：不该没候选），
// 但 `ai_plans_source` 是 `rule` 时，标题必须是「系统推荐」而不是「AI 推荐」。
// 否则教秘以为这是分析过的结果 —— 而这块恰好最容易被拿给评审看。
// ---------------------------------------------------------------------------
describe('RescheduleView · 候选来源标注（R64）', () => {
  const plan = { weekday: '周一', time_slot: '567', room: 'A101', reason: '该时段教室空闲' };

  it('source=rule 时标题写明「AI 暂不可用」', () => {
    render(<RescheduleView {...base} impact={null} aiPlans={[plan]} aiPlansSource="rule" />);
    const text = document.body.textContent || '';
    expect(text).toContain('系统推荐');
    expect(text).toMatch(/AI 暂不可用|暂不可用/);
    expect(text).not.toContain('🤖 AI 推荐候选方案');
  });

  it('source=llm 时才是「🤖 AI 推荐候选方案」', () => {
    render(<RescheduleView {...base} impact={null} aiPlans={[plan]} aiPlansSource="llm" />);
    expect(document.body.textContent || '').toContain('AI 推荐候选方案');
  });
});

// ---------------------------------------------------------------------------
// R96：LLM 失败信号必须真的出现在 DOM 里，而不是只接在源码里。
//
// 源码守卫只能证明"调用了 helper / 传了 prop"，证明不了用户看得见。
// 这里专测最容易被吞的一种：候选方案恰好为空（planLoading=false），
// 卡片原本的显示条件是 (planLoading || aiPlans.length > 0) ——
// 若不把 warning 也放进条件，故障提示会静默消失。
// ---------------------------------------------------------------------------
describe('RescheduleView · LLM 失败就地提示（R96）', () => {
  const warning = '⚠️ AI 候选生成失败，已改用系统规则推荐：upstream timeout';

  it('无候选且未加载时，警告仍要上屏（不能被显示条件吞掉）', () => {
    render(<RescheduleView {...base} impact={null} aiPlans={[]} planLoading={false} aiPlansWarning={warning} />);
    const text = document.body.textContent || '';
    expect(text).toContain('AI 候选生成失败');
    expect(text).toContain('upstream timeout');
  });

  it('没有降级信号时保持安静（不许制造狼来了）', () => {
    render(<RescheduleView {...base} impact={null} aiPlans={[]} planLoading={false} />);
    expect(document.body.textContent || '').not.toContain('AI 候选生成失败');
  });
});

// ---------------------------------------------------------------------------
// R98：学期起始日读失败时，沙盘日期是按兜底常量算的（实测差 7 天）。
// 源码守卫证明不了用户看得见，这里专测 DOM：一条 slot 带 date_degraded
// 就必须在沙盘里出现警告；没有信号时保持安静。
// ---------------------------------------------------------------------------
describe('RescheduleView · 学期起始日降级就地提示（R98）', () => {
  const slot = { weekday: '周一', time_slot: '567', room: 'A410', date: '03月10日' };

  it('slot 带 date_degraded 时，沙盘上必须出现警告', () => {
    render(<RescheduleView {...base} impact={null} available={[{ ...slot, date_degraded: true }] as never} />);
    expect(document.body.textContent || '').toContain('学期起始日读取失败');
  });

  it('没有降级信号时保持安静（不许制造狼来了）', () => {
    render(<RescheduleView {...base} impact={null} available={[slot] as never} />);
    expect(document.body.textContent || '').not.toContain('学期起始日读取失败');
  });
});

// ---------------------------------------------------------------------------
// R99：影响分析页的冲突明细里 date 也是按兜底学期算的（实测差 7 天），
// 但 R98 只给沙盘接了横幅。这里专测 DOM：available/excluded 全空，
// 只有 conflict_slots 带信号 —— 警告必须仍出现在冲突明细块里。
// ---------------------------------------------------------------------------
describe('RescheduleView · 影响分析明细的日期降级提示（R99）', () => {
  it('conflict_slots 带 date_degraded 时，冲突明细块内必须出现警告', () => {
    const degraded = {
      ...impact,
      date_degraded: true,
      conflict_slots: impact.conflict_slots.map((c) => ({ ...c, date_degraded: true })),
    };
    render(<RescheduleView {...base} impact={degraded as never} />);
    const text = document.body.textContent || '';
    expect(text).toContain('📋 冲突明细');
    expect(text).toContain('学期起始日读取失败');
    expect(text.indexOf('学期起始日读取失败')).toBeGreaterThan(text.indexOf('📋 冲突明细'));
  });

  it('没有降级信号时保持安静（不许制造狼来了）', () => {
    render(<RescheduleView {...base} impact={impact as never} />);
    expect(document.body.textContent || '').not.toContain('学期起始日读取失败');
  });
});
