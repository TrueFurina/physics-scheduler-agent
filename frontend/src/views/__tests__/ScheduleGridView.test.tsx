// @vitest-environment jsdom
/**
 * 周课表网格视图测试（R-62）。
 *
 * 锁三条业务不变量（都与后端口径对应，缺一条就是假绿）：
 *  1. **7 天 × 4 时段全列**：周末条目（真实表 71/433）必须渲染出来——
 *     R-41「拿枚举当现实」在前端的回归样本（labels.ts 的 5 天 × 3 时段常量
 *     就是沙盘网格的旧口径，本视图刻意不复用）；
 *  2. **跨节次记录在每个占用的时段各出现一次**，带原表写法徽标（R-40）；
 *  3. **没填场地的记录照常显示**并标注（R-35：不静默丢）；冲突条目带 ⚠。
 *  另锁交互：切周次/筛选发起带参数的新请求；失败态明确可见（R-29）。
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';

vi.mock('../../api/client', () => ({
  rescheduleApi: { scheduleGrid: vi.fn() },
}));

import ScheduleGridView from '../ScheduleGridView';

afterEach(cleanup);

const GRID = {
  week: 2,
  week_min: 1,
  week_max: 5,
  weekdays: ['周一', '周二', '周三', '周四', '周五', '周六', '周日'],
  slots: [
    { slot: 'MORNING', label: '上午', clock: '09:00-11:15' },
    { slot: '567', label: '下午一', clock: '13:30-15:45' },
    { slot: '678', label: '下午二', clock: '15:45-18:00' },
    { slot: '91011', label: '晚上', clock: '18:30-20:45' },
  ],
  cells: {
    '周一|567': [
      { class_name: '甲班', group: '1组', experiment: '项目A', teacher: '吴老师', room: 'A410',
        time_slot: '567', time_raw: '567', blocked: ['567'], missing_room: false,
        conflicts: ['room', 'teacher'] },
      { class_name: '乙班', group: '1组', experiment: '项目B', teacher: '王老师', room: 'A410',
        time_slot: '567', time_raw: '567', blocked: ['567'], missing_room: false,
        conflicts: ['room'] },
    ],
    '周六|567': [
      { class_name: '甲班', group: '1组', experiment: '周末项目', teacher: '吴老师', room: 'A410',
        time_slot: '567', time_raw: '567', blocked: ['567'], missing_room: false, conflicts: [] },
    ],
    '周日|MORNING': [
      { class_name: '乙班', group: '1组', experiment: '周日绪论', teacher: '', room: '',
        time_slot: 'MORNING', time_raw: 'MORNING', blocked: ['MORNING'], missing_room: true,
        conflicts: [] },
    ],
    '周三|567': [
      { class_name: '甲班', group: '1组', experiment: '长实验', teacher: '吴老师', room: 'A410',
        time_slot: '567', time_raw: '14:00-17:00', blocked: ['567', '678'], missing_room: false,
        conflicts: [] },
    ],
    '周三|678': [
      { class_name: '甲班', group: '1组', experiment: '长实验', teacher: '吴老师', room: 'A410',
        time_slot: '567', time_raw: '14:00-17:00', blocked: ['567', '678'], missing_room: false,
        conflicts: [] },
    ],
  },
  filters: { class_name: '', room: '' },
  class_options: ['甲班', '乙班'],
  room_options: ['A410'],
  totals: { entries: 6, nonempty_cells: 4, missing_room: 1, conflict_entries: 2, unplaced: 0 },
  conflict_counts: { room: 1, teacher: 1, group: 0 },
  conflict_scope: '全周全表（不受班级/场地筛选影响）',
  notes: ['本周 1 条记录没填场地（灰色标注），不参与教室冲突判定——这是数据缺口，不是冲突'],
};

function renderView(overrides: Record<string, unknown> = {}) {
  const props = {
    week: 2,
    data: GRID as any,
    loading: false,
    error: '',
    classFilter: '',
    roomFilter: '',
    onWeekChange: vi.fn(),
    onClassChange: vi.fn(),
    onRoomChange: vi.fn(),
    onRetry: vi.fn(),
    ...overrides,
  };
  render(<ScheduleGridView {...(props as any)} />);
  return props;
}

describe('ScheduleGridView', () => {
  it('渲染 7 天 × 4 时段网格（R-41：周末/上午不丢）', () => {
    renderView();
    for (const wd of ['周一', '周二', '周三', '周四', '周五', '周六', '周日']) {
      expect(screen.getByRole('columnheader', { name: wd })).toBeTruthy();
    }
    for (const label of ['上午', '下午一', '下午二', '晚上']) {
      expect(screen.getByText(label)).toBeTruthy();
    }
    expect(screen.getByText('09:00-11:15')).toBeTruthy();   // 上午时段真实存在
  });

  it('周末格的条目渲染出来', () => {
    renderView();
    expect(screen.getByText('周末项目')).toBeTruthy();
    expect(screen.getByText('周日绪论')).toBeTruthy();
  });

  it('跨节次记录在两个时段各出现一次，带原表写法徽标（R-40）', () => {
    renderView();
    expect(screen.getAllByText('长实验')).toHaveLength(2);
    expect(screen.getAllByText('⏤ 14:00-17:00')).toHaveLength(2);
  });

  it('没填场地的记录照常显示并标注（R-35：不静默丢）', () => {
    renderView();
    expect(screen.getByText('未填场地')).toBeTruthy();
    expect(screen.getByText(/本周 1 条记录没填场地/)).toBeTruthy();
  });

  it('冲突条目带 ⚠ 徽标，汇总条说清冲突口径', () => {
    renderView();
    expect(screen.getByTitle('教室冲突 + 教师冲突')).toBeTruthy();
    expect(screen.getByText(/冲突条目 2/)).toBeTruthy();
    expect(screen.getByText(/全周全表（不受班级\/场地筛选影响）/)).toBeTruthy();
  });

  it('点条目展开详情，能关掉', async () => {
    renderView();
    fireEvent.click(screen.getByText('项目A'));
    await waitFor(() => expect(screen.getByText(/班级\/组：甲班\/1组/)).toBeTruthy());
    expect(screen.getByText(/与本周其它安排撞在同一格/)).toBeTruthy();
    fireEvent.click(screen.getByLabelText('关闭详情'));
    await waitFor(() => expect(screen.queryByText(/班级\/组：甲班\/1组/)).toBeNull());
  });

  it('切周次/筛选发起新请求（受控组件：回调上报意图）', () => {
    const props = renderView();
    const selects = screen.getAllByRole('combobox');
    fireEvent.change(selects[0], { target: { value: '3' } });       // 周次
    expect(props.onWeekChange).toHaveBeenCalledWith(3);
    fireEvent.change(selects[1], { target: { value: '甲班' } });    // 班级
    expect(props.onClassChange).toHaveBeenCalledWith('甲班');
    fireEvent.change(selects[2], { target: { value: 'A410' } });    // 场地
    expect(props.onRoomChange).toHaveBeenCalledWith('A410');
  });

  it('失败态明确可见并可重试（R-29：不留空白卡）', () => {
    const props = renderView({ data: null, error: '网络不可达，请检查网络连接' });
    expect(screen.getByText(/网格没能加载/)).toBeTruthy();
    expect(screen.getByText('网络不可达，请检查网络连接')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: '重新加载' }));
    expect(props.onRetry).toHaveBeenCalled();
  });

  it('加载中显示明确状态', () => {
    renderView({ data: null, loading: true });
    expect(screen.getByText('网格生成中...')).toBeTruthy();
  });
});
