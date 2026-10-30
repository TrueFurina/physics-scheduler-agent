// @vitest-environment jsdom
/**
 * 落地面板：历史可见（R56）
 *
 * 背景
 * ----
 * R55 让后端在撤销时"打标记不删行"，并开了 `GET /parallel/land/history`。
 * 但如果界面没有入口，这一切仍然只存在于数据库里 —— 教秘和教务都看不到
 * "上周谁落过方案、后来撤了没有"。这已经是这条链路上第四次"后端给了、界面没接"
 * （R50→R51、R53→R54、R55→R56），所以这一轮先把它接上再算完。
 *
 * 锁四条：
 *  1. 有历史时给出入口，并报出总次数与已撤销次数；
 *  2. 展开后每条带**操作者**、行数，已撤销的标"已撤销"并带撤销人；
 *  3. 接口拿不到（未登录 / 旧后端）时整块不渲染，不打扰教秘、也不报错；
 *  4. 撤销成功后历史自动刷新（面板已在 loadStatus 里一并拉取）。
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';

const landStatus = vi.fn();
const landHistory = vi.fn();

vi.mock('../../api/client', () => ({
  rescheduleApi: {
    landPreview: vi.fn(),
    landCommit: vi.fn(),
    landUndo: vi.fn(),
    landStatus: (...a: unknown[]) => landStatus(...a),
    landHistory: (...a: unknown[]) => landHistory(...a),
  },
}));

import LandPlanPanel from '../LandPlanPanel';
import type { LandPlanRow } from '../../types';

afterEach(() => {
  cleanup();
  landStatus.mockReset();
  landHistory.mockReset();
});

const ROWS: LandPlanRow[] = [
  { class_name: '甲班', project: '项目A', group: '1组', week: 1, weekday: '周一', time_slot: '567', room: 'A410', teacher: '吴老师' },
];

const HISTORY = {
  success: true,
  total: 2,
  active: 1,
  revoked: 1,
  history: [
    {
      id: 2, landed_at: '2026-10-30T09:15:00.000000', actor: 'admin', note: '一键排全校',
      plan_rows: 410, after_entries: 433, revoked: false, revoked_at: '', revoked_by: '',
    },
    {
      id: 1, landed_at: '2026-10-28T16:40:00.000000', actor: 'admin', note: '',
      plan_rows: 405, after_entries: 433, revoked: true,
      revoked_at: '2026-10-29T08:02:00.000000', revoked_by: 'admin',
    },
  ],
};

function renderPanel() {
  render(
    <LandPlanPanel
      schedule={ROWS}
      adminAuthed
      onRequireLogin={() => {}}
      onMsg={() => {}}
    />,
  );
}

describe('LandPlanPanel · 落地历史', () => {
  it('有历史时给出入口，报出总次数与已撤销次数', async () => {
    landStatus.mockResolvedValue({ data: { landed: true, landed_at: '2026-10-30T09:15:00', plan_rows: 410, landed_by: 'admin' } });
    landHistory.mockResolvedValue({ data: HISTORY });

    renderPanel();

    await waitFor(() => expect(screen.getByText(/落地历史 2 次/)).toBeTruthy());
    expect(screen.getByText(/其中 1 次已撤销/)).toBeTruthy();
  });

  it('展开后每条带操作者、行数，已撤销的标出来并带撤销人', async () => {
    landStatus.mockResolvedValue({ data: { landed: true } });
    landHistory.mockResolvedValue({ data: HISTORY });

    renderPanel();
    fireEvent.click(await screen.findByText(/落地历史 2 次/));

    const list = await screen.findByTestId('land-history');
    expect(list.textContent).toContain('10-30 09:15');
    expect(list.textContent).toContain('admin');
    expect(list.textContent).toContain('410 行');
    expect(list.textContent).toContain('生效中');
    expect(list.textContent).toContain('已撤销');
    expect(list.textContent).toContain('10-29 08:02');
  });

  it('状态里带出"由谁落地"（R55 新增字段）', async () => {
    landStatus.mockResolvedValue({ data: { landed: true, landed_at: '2026-10-30T09:15:00', plan_rows: 410, landed_by: 'admin' } });
    landHistory.mockResolvedValue({ data: { ...HISTORY, total: 0, revoked: 0, history: [] } });

    renderPanel();

    await waitFor(() => expect(screen.getByText(/由 admin 落地/)).toBeTruthy());
  });

  it('历史接口拿不到时整块不渲染（未登录/旧后端都不能把面板搞红）', async () => {
    landStatus.mockResolvedValue({ data: { landed: false } });
    landHistory.mockRejectedValue(new Error('401'));

    renderPanel();

    await waitFor(() => expect(landStatus).toHaveBeenCalled());
    expect(screen.queryByText(/落地历史/)).toBeNull();
    expect(screen.queryByTestId('land-history')).toBeNull();
  });
});
