// @vitest-environment jsdom
/**
 * R87：落盘失败警告必须真的在页面上出现。
 *
 * 纯函数单测和源码守卫都只能证明「写过了」，这条走真实交互：
 * admin 点删除 → 后端回 persisted=false → 页面上出现黄色警告横幅。
 * 用删除路径是因为它不需要填表，一条 fireEvent 就能触发写请求。
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';

vi.mock('../../api/client', () => ({
  rescheduleApi: {
    masterOverview: vi.fn(),
    masterInit: vi.fn(),
    masterUpsertExperiment: vi.fn(),
    masterDeleteExperiment: vi.fn(),
    masterUpsertTeacher: vi.fn(),
    masterDeleteTeacher: vi.fn(),
    masterUpsertRoom: vi.fn(),
    masterDeleteRoom: vi.fn(),
    masterUpsertClass: vi.fn(),
    masterDeleteClass: vi.fn(),
    masterUpsertCourse: vi.fn(),
    masterDeleteCourse: vi.fn(),
    exportMasterWorkbook: vi.fn(),
  },
}));

import { rescheduleApi } from '../../api/client';
import MasterDataView from '../MasterDataView';

const OVERVIEW = {
  initialized: true,
  courses: [{ name: '大学物理实验', experiments: ['扭摆法'] }],
  experiments: [
    { name: '扭摆法', course: '大学物理实验', teachers: ['吴老师'], rooms: ['A410'], classes: ['一班'] },
  ],
  teachers: [{ name: '吴老师', projects: ['扭摆法'] }],
  rooms: [{ name: 'A410', capacity: null }],
  classes: [{ name: '一班', groups: ['1组'], weekday: '周四', time_slots: ['567'], week_min: 1, week_max: 16 }],
};

const WARNING = '⚠️ 本次修改已在当前会话生效，但未能写入磁盘；服务重启后会回退到安排表基线';

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe('主数据落盘失败警告', () => {
  it('删除返回 persisted=false 时，页面显示黄色警告而不是静默成功', async () => {
    vi.mocked(rescheduleApi.masterOverview).mockResolvedValue({ data: { data: OVERVIEW } } as any);
    vi.mocked(rescheduleApi.masterDeleteExperiment).mockResolvedValue({
      data: { success: true, data: OVERVIEW, persisted: false, warning: WARNING },
    } as any);
    vi.stubGlobal('confirm', vi.fn(() => true));

    render(<MasterDataView adminAuthed={true} onRequireLogin={vi.fn()} />);
    await waitFor(() => expect(screen.getByText('扭摆法')).toBeTruthy());

    fireEvent.click(screen.getAllByRole('button', { name: /删除/ })[0]);
    await waitFor(() => expect(screen.getByText(/未能写入磁盘/)).toBeTruthy());
    // 不是红色错误：清理主数据仍算成功（内存态已生效），只是没存住
    expect(screen.queryByText(/删除失败/)).toBeNull();
  });

  it('防放宽：落盘成功时页面不出现警告横幅', async () => {
    vi.mocked(rescheduleApi.masterOverview).mockResolvedValue({ data: { data: OVERVIEW } } as any);
    vi.mocked(rescheduleApi.masterDeleteExperiment).mockResolvedValue({
      data: { success: true, data: OVERVIEW },
    } as any);
    vi.stubGlobal('confirm', vi.fn(() => true));

    render(<MasterDataView adminAuthed={true} onRequireLogin={vi.fn()} />);
    await waitFor(() => expect(screen.getByText('扭摆法')).toBeTruthy());

    fireEvent.click(screen.getAllByRole('button', { name: /删除/ })[0]);
    await waitFor(() => expect(rescheduleApi.masterDeleteExperiment).toHaveBeenCalled());
    expect(screen.queryByText(/未能写入磁盘/)).toBeNull();
  });
});
