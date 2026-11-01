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
 *  7. 采纳横幅**用后端 message**，前端不再自拼数字（R60；无差异落地时尤其明显）。
 *  8. 撤销返回 500 + rolled_back=true（课表已退、标记没写进库）时，横幅必须说
 *     "只做了一半"并重拉状态 —— 否则界面还挂着"可撤销"而安排表已经变了（R61）。
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

  it('横幅用后端回执 message —— 无差异落地时不再自拼"N 行写入"', async () => {
    // R60：后端把回执说全了（四个账 / 长实验截短 / 能否撤销），前端只负责显示。
    // 这条守的是"前端不自己编数字"：旧代码在 has_changes=false 时仍说"2 行写入"，
    // 而那 2 行一个字都没改。
    const RECEIPT = '✅ 已记录一次落地（2 条方案行 / 2 个单元），但安排表**没有任何变化**';
    vi.mocked(rescheduleApi.landStatus).mockResolvedValue({ data: { landed: false } } as any);
    vi.mocked(rescheduleApi.landPreview).mockResolvedValue({ data: DIFF } as any);
    vi.mocked(rescheduleApi.landCommit).mockResolvedValue({
      data: { ...DIFF, has_changes: false, landed_at: '2026-11-02T04:35:12.000001', message: RECEIPT },
    } as any);
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true);
    // 前面用例已经点过一次采纳，mock 调用记录会累计 —— 不清就从"本次点了 1 次"变成 2 次
    vi.mocked(rescheduleApi.landCommit).mockClear();
    const { onMsg } = renderPanel(true);
    fireEvent.click(screen.getByRole('button', { name: /落地为正式安排表/ }));
    await waitFor(() => expect(screen.getByRole('button', { name: /确认采纳/ })).toBeTruthy());
    fireEvent.click(screen.getByRole('button', { name: /确认采纳/ }));
    await waitFor(() => expect(rescheduleApi.landCommit).toHaveBeenCalledTimes(1));
    expect(onMsg).toHaveBeenCalledWith(RECEIPT);
    expect(onMsg).not.toHaveBeenCalledWith(expect.stringContaining('行写入'));
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

  it('撤销返回 500 且 rolled_back=true：横幅说"只做了一半"，并重拉状态（R61）', async () => {
    // 上一个用例调过 landUndo，这里的调用计数必须从零开始数
    vi.mocked(rescheduleApi.landUndo).mockClear();
    vi.mocked(rescheduleApi.landStatus).mockClear();
    vi.mocked(rescheduleApi.landStatus).mockResolvedValue({
      data: { landed: true, landed_at: '2026-10-23T18:00:00', plan_rows: 2, note: '测试' },
    } as any);
    // 后端真实形态：课表已回滚，但撤销标记没写进库 → 500 + 完整结构 + detail 人话
    vi.mocked(rescheduleApi.landUndo).mockRejectedValue({
      response: {
        status: 500,
        data: {
          success: false,
          error_kind: 'revoke_failed',
          revoked: false,
          rolled_back: true,
          restored_entries: 7,
          detail: '课表已回滚到落地前，但撤销记录写入失败（首次：disk I/O error；重试：disk I/O error）。',
        },
      },
    });
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true);
    const { onMsg } = renderPanel(true);
    await waitFor(() => expect(screen.getByRole('button', { name: /撤销落地/ })).toBeTruthy());
    const statusCallsBefore = vi.mocked(rescheduleApi.landStatus).mock.calls.length;

    fireEvent.click(screen.getByRole('button', { name: /撤销落地/ }));
    await waitFor(() => expect(rescheduleApi.landUndo).toHaveBeenCalledTimes(1));

    // 横幅必须点明"只做了一半"——不能落成一句通用的"撤销失败"：
    // 教秘看到"失败"会去重新落地，而安排表其实已经退回去了。
    await waitFor(() => expect(onMsg).toHaveBeenCalledWith(expect.stringContaining('只做了一半')));
    expect(onMsg).toHaveBeenCalledWith(expect.stringContaining('已回滚到落地前'));

    // 状态必须重拉：否则面板还挂着"已落地 / 可撤销"，而安排表已经变了
    await waitFor(() =>
      expect(vi.mocked(rescheduleApi.landStatus).mock.calls.length).toBeGreaterThan(statusCallsBefore));

    // 后端的两次失败原因要透到界面上（别只剩"撤销失败"四个字）
    await waitFor(() => expect(document.body.textContent).toContain('disk I/O error'));
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
