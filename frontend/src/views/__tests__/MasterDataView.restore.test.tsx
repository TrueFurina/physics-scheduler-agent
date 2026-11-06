// @vitest-environment jsdom
/**
 * R88：恢复降级警告必须真的在页面上出现（不是只写了个 helper）。
 *
 * 走真实首屏路径：组件 mount → masterOverview() 返回 `restored: false` →
 * 页面上出现黄色警告横幅。对照组：正常响应不弹任何警告（狼来了）。
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen, waitFor } from '@testing-library/react';

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

const RESTORE_WARNING =
  '⚠️ 服务重启后未能恢复上次保存的主数据（落盘文件损坏，已丢弃）：已回退到按当前安排表反推的基线，请核对';

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe('主数据恢复降级警告', () => {
  it('overview 回 restored=false 时，首屏显示黄色警告横幅', async () => {
    vi.mocked(rescheduleApi.masterOverview).mockResolvedValue({
      data: { success: true, data: OVERVIEW, restored: false, warning: RESTORE_WARNING },
    } as any);

    render(<MasterDataView adminAuthed={true} onRequireLogin={vi.fn()} />);
    await waitFor(() => expect(screen.getByText(/未能恢复上次保存的主数据/)).toBeTruthy());
    // 不能冒充「加载失败」：主数据是可用的反推基线
    expect(screen.queryByText(/加载主数据失败/)).toBeNull();
  });

  it('防放宽：正常响应（老后端 / 恢复成功）不出现任何警告横幅', async () => {
    vi.mocked(rescheduleApi.masterOverview).mockResolvedValue({
      data: { success: true, data: OVERVIEW },
    } as any);

    render(<MasterDataView adminAuthed={true} onRequireLogin={vi.fn()} />);
    await waitFor(() => expect(screen.getByText('扭摆法')).toBeTruthy());
    expect(screen.queryByText(/未能恢复上次保存的主数据/)).toBeNull();
    expect(screen.queryByText(/未能写入磁盘/)).toBeNull();
  });

  it('防放宽：restored=true 但带着 warning 也不许弹（判据只看 === false）', async () => {
    vi.mocked(rescheduleApi.masterOverview).mockResolvedValue({
      data: { success: true, data: OVERVIEW, restored: true, warning: RESTORE_WARNING },
    } as any);

    render(<MasterDataView adminAuthed={true} onRequireLogin={vi.fn()} />);
    await waitFor(() => expect(screen.getByText('扭摆法')).toBeTruthy());
    expect(screen.queryByText(/未能恢复上次保存的主数据/)).toBeNull();
  });
});
