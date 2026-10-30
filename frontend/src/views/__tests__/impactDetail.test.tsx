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
