// @vitest-environment jsdom
/**
 * 方案落地面板（预览 → 采纳 → 撤销）冒烟测试。
 *
 * 锁六条业务不变量（对应后端契约，前端不许自己编数字）：
 *  1. 预览按钮把方案行原样带给 landPreview；diff 未写入前不出现"确认采纳"以外的写动作；
 *  2. 差异五本账 + before→after 条数按后端返回渲染（新增/调整/未覆盖/原样/非可排）；
 *  3. 长实验截短警告必须出现在界面上（埋在 400 条差异里＝静默删课）；
 *  4. has_changes=false 时说"无需写入"，不给确认按钮；
 *  5. 未登录管理员时采纳/撤销走 onRequireLogin，绝不调写接口（不能假装能写）；
 *  6. 撤销成功后 rolled_back_moves>0 时，顶部横幅必须带出"一并回滚 N 条调课"。
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';

vi.mock('../../api/client', () => ({
  rescheduleApi: {
    landPreview: vi.fn(),
    landCommit: vi.fn(),
    landUndo: vi.fn(),
    landStatus: vi.fn(),
  },
}));

import { rescheduleApi } from '../../api/client';
import LandPlanPanel from '../LandPlanPanel';
import type { LandPlanDiff, LandPlanRow } from '../../types';

afterEach(cleanup);

const ROWS: LandPlanRow[] = [
  { class_name: '甲班', project: '项目A', group: '1组', week: 1, weekday: '周一', time_slot: '567', room: 'A410', teacher: '吴老师' },
  { class_name: '甲班', project: '长实验', group: '1组', week: 4, weekday: '周三', time_slot: '567', room: 'A504', teacher: '林晨' },
];

const DIFF: LandPlanDiff = {
  success: true,
  plan_units: 2,
  plan_rows: 2,
  before_entries: 7,
  after_entries: 6,
  added: [],
  changed: [
    { unit: '甲班 / 项目A / 1组', old: [{ ...ROWS[0], week: 2 }], new: [ROWS[0]] },
    { unit: '甲班 / 长实验 / 1组', long_session: true, old: [{ ...ROWS[1] }], new: [ROWS[1]] },
  ],
  unchanged_units: 0,
  uncovered: [{ unit: '甲班 / 项目B / 1组', rows: [{ ...ROWS[0], project: '项目B', week: 3 }] }],
  long_sessions: [{ unit: '甲班 / 长实验 / 1组', old_slots: ['567', '678'], plan_slots: ['567'] }],
  untouched_nonsched: 2,
  warnings: ['1 个可排单元不在方案里（求解未覆盖），共 1 行，已保留原安排'],
  has_changes: true,
};

function renderPanel(adminAuthed = true) {
  const onRequireLogin = vi.fn();
  const onMsg = vi.fn();
  render(
    <LandPlanPanel
      schedule={ROWS}
      adminAuthed={adminAuthed}
      onRequireLogin={onRequireLogin}
      onMsg={onMsg}
    />,
  );
  return { onRequireLogin, onMsg };
}

/** 点「落地」并等差异渲染出来 */
async function openDiff() {
  vi.mocked(rescheduleApi.landStatus).mockResolvedValue({ data: { landed: false } } as any);
  vi.mocked(rescheduleApi.landPreview).mockResolvedValue({ data: DIFF } as any);
  renderPanel();
  fireEvent.click(screen.getByRole('button', { name: /落地为正式安排表/ }));
  await waitFor(() => expect(screen.getByText(/落地预览/)).toBeTruthy());
}

describe('LandPlanPanel', () => {
  it('点「落地」→ 调 landPreview 带原方案行；预览不写入（无 landCommit 调用）', async () => {
    vi.mocked(rescheduleApi.landStatus).mockResolvedValue({ data: { landed: false } } as any);
    vi.mocked(rescheduleApi.landPreview).mockResolvedValue({ data: DIFF } as any);
    renderPanel();
    fireEvent.click(screen.getByRole('button', { name: /落地为正式安排表/ }));
    await waitFor(() => expect(rescheduleApi.landPreview).toHaveBeenCalledTimes(1));
    expect(vi.mocked(rescheduleApi.landPreview).mock.calls[0][0]).toEqual(ROWS);
    expect(rescheduleApi.landCommit).not.toHaveBeenCalled();
  });

  it('差异五本账与 before→after 条数按后端返回渲染', async () => {
    await openDiff();
    expect(screen.getByText('新增 0')).toBeTruthy();
    expect(screen.getByText('调整 2')).toBeTruthy();
    expect(screen.getByText(/未覆盖保留 1（1 行）/)).toBeTruthy();
    expect(screen.getByText('原样保留 0')).toBeTruthy();
    expect(screen.getByText('绪论/考试原样 2')).toBeTruthy();
    expect(screen.getByText(/安排表 7 → 6 条/)).toBeTruthy();
  });

  it('长实验截短与未覆盖警告必须在界面上出现', async () => {
    await openDiff();
    expect(screen.getByText(/将被截短的长实验（1 个）/)).toBeTruthy();
    expect(screen.getByText(/567\+678 → 567/)).toBeTruthy();
    expect(screen.getByText(/长实验截短/)).toBeTruthy();
    expect(screen.getByText(/未覆盖、保留原安排（1）/)).toBeTruthy();
    expect(screen.getByText(/⚠️ 1 个可排单元不在方案里/)).toBeTruthy();
  });

  it('has_changes=false 时说无需写入，不给确认按钮', async () => {
    vi.mocked(rescheduleApi.landStatus).mockResolvedValue({ data: { landed: false } } as any);
    vi.mocked(rescheduleApi.landPreview).mockResolvedValue({
      data: { ...DIFF, has_changes: false, changed: [], added: [], uncovered: [], warnings: [] },
    } as any);
    renderPanel();
    fireEvent.click(screen.getByRole('button', { name: /落地为正式安排表/ }));
    await waitFor(() => expect(screen.getByText(/方案与当前安排表一致，无需写入/)).toBeTruthy());
    expect(screen.queryByRole('button', { name: /确认采纳/ })).toBeNull();
  });

  it('未登录管理员：确认采纳走 onRequireLogin，不调写接口', async () => {
    vi.mocked(rescheduleApi.landStatus).mockResolvedValue({ data: { landed: false } } as any);
    vi.mocked(rescheduleApi.landPreview).mockResolvedValue({ data: DIFF } as any);
    const { onRequireLogin } = renderPanel(false);
    fireEvent.click(screen.getByRole('button', { name: /落地为正式安排表/ }));
    await waitFor(() => expect(screen.getByRole('button', { name: /确认采纳/ })).toBeTruthy());
    fireEvent.click(screen.getByRole('button', { name: /确认采纳/ }));
    expect(onRequireLogin).toHaveBeenCalledTimes(1);
    expect(rescheduleApi.landCommit).not.toHaveBeenCalled();
  });

  it('已登录：确认后调 landCommit，成功横幅带"已落地"并刷新状态', async () => {
    vi.mocked(rescheduleApi.landStatus).mockResolvedValue({ data: { landed: false } } as any);
    vi.mocked(rescheduleApi.landPreview).mockResolvedValue({ data: DIFF } as any);
    vi.mocked(rescheduleApi.landCommit).mockResolvedValue({
      data: { ...DIFF, landed_at: '2026-10-23T18:00:00.123456' },
    } as any);
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true);
    const { onMsg } = renderPanel(true);
    fireEvent.click(screen.getByRole('button', { name: /落地为正式安排表/ }));
    await waitFor(() => expect(screen.getByRole('button', { name: /确认采纳/ })).toBeTruthy());
    fireEvent.click(screen.getByRole('button', { name: /确认采纳/ }));
    await waitFor(() => expect(rescheduleApi.landCommit).toHaveBeenCalledTimes(1));
    expect(onMsg).toHaveBeenCalledWith(expect.stringContaining('已落地为正式安排表'));
    expect(onMsg).toHaveBeenCalledWith(expect.stringContaining('7 → 6 条'));
    // 状态刷新：撤销入口应出现（ landed: true 后第二次 landStatus）
    expect(vi.mocked(rescheduleApi.landStatus).mock.calls.length).toBeGreaterThanOrEqual(2);
    confirmSpy.mockRestore();
  });

  it('有落地记录时显示撤销入口；撤销成功且 rolled_back_moves>0 时横幅带出条数', async () => {
    vi.mocked(rescheduleApi.landStatus).mockResolvedValue({
      data: { landed: true, landed_at: '2026-10-23T18:00:00', plan_rows: 2, note: '测试' },
    } as any);
    vi.mocked(rescheduleApi.landUndo).mockResolvedValue({
      data: { success: true, landed_at: '2026-10-23T18:00:00', restored_entries: 7, rolled_back_moves: 1 },
    } as any);
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true);
    const { onMsg } = renderPanel(true);
    await waitFor(() => expect(screen.getByRole('button', { name: /撤销落地/ })).toBeTruthy());
    fireEvent.click(screen.getByRole('button', { name: /撤销落地/ }));
    await waitFor(() => expect(rescheduleApi.landUndo).toHaveBeenCalledTimes(1));
    expect(onMsg).toHaveBeenCalledWith(expect.stringContaining('已撤销落地'));
    expect(onMsg).toHaveBeenCalledWith(expect.stringContaining('1 条调课'));
    confirmSpy.mockRestore();
  });

  it('采纳返回 401：引导重新登录，面板内给中文错误', async () => {
    vi.mocked(rescheduleApi.landStatus).mockResolvedValue({ data: { landed: false } } as any);
    vi.mocked(rescheduleApi.landPreview).mockResolvedValue({ data: DIFF } as any);
    vi.mocked(rescheduleApi.landCommit).mockRejectedValue({ response: { status: 401, data: { detail: '未授权' } } });
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true);
    const { onRequireLogin } = renderPanel(true);
    fireEvent.click(screen.getByRole('button', { name: /落地为正式安排表/ }));
    await waitFor(() => expect(screen.getByRole('button', { name: /确认采纳/ })).toBeTruthy());
    fireEvent.click(screen.getByRole('button', { name: /确认采纳/ }));
    await waitFor(() => expect(screen.getByText(/登录状态已失效/)).toBeTruthy());
    expect(onRequireLogin).toHaveBeenCalled();
    confirmSpy.mockRestore();
  });

  it('schedule 为空时整个面板不渲染', () => {
    vi.mocked(rescheduleApi.landStatus).mockResolvedValue({ data: { landed: false } } as any);
    render(
      <LandPlanPanel schedule={[]} adminAuthed onRequireLogin={vi.fn()} onMsg={vi.fn()} />,
    );
    expect(screen.queryByRole('button', { name: /落地为正式安排表/ })).toBeNull();
  });
});
