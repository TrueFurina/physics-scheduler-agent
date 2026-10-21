// @vitest-environment jsdom
/**
 * 主数据管理视图冒烟测试（导师一号行动项「重构数据库」）。
 *
 * 锁两条业务不变量：
 *  1. 五张实体表 Tab 都能切、数据能渲染出来；
 *  2. 未登录 admin 时，写操作（新增/删除/反推）被禁用并引导登录 ——
 *     写接口挂 require_admin，前端不能假装能写。
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
  },
}));

import { rescheduleApi } from '../../api/client';
import MasterDataView from '../MasterDataView';

afterEach(cleanup);

const OVERVIEW = {
  initialized: true,
  courses: [{ name: '大学物理实验', experiments: ['扭摆法', '声速测量'] }],
  experiments: [
    { name: '扭摆法', course: '大学物理实验', teachers: ['吴老师'], rooms: ['A410'], classes: ['一班'] },
  ],
  teachers: [{ name: '吴老师', projects: ['扭摆法'] }],
  rooms: [{ name: 'A410', capacity: null }],
  classes: [{ name: '一班', groups: ['1组', '2组'], weekday: '周四', time_slots: ['567', '678'], week_min: 1, week_max: 16 }],
};

function renderView(authed = false) {
  render(<MasterDataView adminAuthed={authed} onRequireLogin={vi.fn()} />);
}

describe('MasterDataView', () => {
  it('渲染实验项目数据，切 Tab 到教师列表', async () => {
    vi.mocked(rescheduleApi.masterOverview).mockResolvedValue({ data: { data: OVERVIEW } } as any);
    renderView();
    await waitFor(() => expect(screen.getByText('扭摆法')).toBeTruthy());
    // 教师 Tab
    fireEvent.click(screen.getByRole('button', { name: /教师/ }));
    expect(screen.getByText('可授 1 个项目：扭摆法')).toBeTruthy();
  });

  it('未登录时新增/删除被禁用，并提示只读', async () => {
    vi.mocked(rescheduleApi.masterOverview).mockResolvedValue({ data: { data: OVERVIEW } } as any);
    renderView(false);
    await waitFor(() => expect(screen.getByText('扭摆法')).toBeTruthy());

    // 新增按钮禁用：未登录不能写主数据
    const addBtn = screen.getByRole('button', { name: /新增/ }) as HTMLButtonElement;
    expect(addBtn.disabled).toBe(true);

    const delBtn = screen.getAllByRole('button', { name: /删除/ })[0] as HTMLButtonElement;
    expect(delBtn.disabled).toBe(true);

    // 视图提示"只读"
    expect(screen.getByText(/只读视图/)).toBeTruthy();
  });

  it('已登录时删除按钮可用', async () => {
    vi.mocked(rescheduleApi.masterOverview).mockResolvedValue({ data: { data: OVERVIEW } } as any);
    renderView(true);
    await waitFor(() => expect(screen.getByText('扭摆法')).toBeTruthy());
    const delBtn = screen.getAllByRole('button', { name: /删除/ })[0] as HTMLButtonElement;
    expect(delBtn.disabled).toBe(false);
  });
});
