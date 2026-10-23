// @vitest-environment jsdom
/**
 * 数据导入向导（R-63）冒烟测试。
 *
 * 锁六条业务不变量（对应后端契约，前端不自己解析文件、不自己编映射）：
 *  1. 第 1 步只选类型+选文件，预览请求按类型走对应端点（workbook→import-preview，
 *     tasks→tasks-preview），且**预览阶段没有任何写调用**；
 *  2. 第 2 步的每个字都来自服务端报告：字段←列名映射、未识别列、统计列、
 *     样本行、将导入行数；
 *  3. 没有可导入的内容时「确认导入」禁用，并说清原因（不给能点下去的死按钮）；
 *  4. 未登录管理员时确认走 onRequireLogin，绝不调写接口（不能假装能写）；
 *  5. 确认成功后展示服务端回执（含告警），并回调 onImported 刷新主数据；
 *  6. 预览失败（400）停在第 1 步并原样呈现错误原因。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';

vi.mock('../../api/client', () => ({
  rescheduleApi: {
    masterImportPreview: vi.fn(),
    masterTasksPreview: vi.fn(),
    masterImport: vi.fn(),
    uploadMaster: vi.fn(),
  },
}));

import { rescheduleApi } from '../../api/client';
import ImportWizard from '../ImportWizard';
import type { ImportTasksPreview, ImportWorkbookPreview } from '../../types';

// vite.config 没有配 clearMocks：同一文件里 spy 的调用记录会跨用例累积，
// 第 2 个用例的"没被调用"断言会被第 1 个用例的调用判红（假红）。
beforeEach(() => { vi.clearAllMocks(); });
afterEach(cleanup);

const WB_PREVIEW: ImportWorkbookPreview = {
  tagged: false,
  sheets: ['实验项目', '场地'],
  tables: {
    courses: { found: false, rows: 0 },
    experiments: {
      found: true, sheet: '实验项目', header_row: 1, rows: 2, blank_rows: 0,
      recognized: ['name', 'teachers'], missing: ['rooms', 'classes'],
      columns: { name: '项目名称', teachers: '任课教师' },
      unrecognized: ['编号', '备注'],
      stat_dropped: ['班级数'],
      samples: [
        { name: '迈克耳孙干涉', teachers: ['鄢老师'] },
        { name: '密立根油滴', teachers: ['陈老师'] },
      ],
    },
    teachers: { found: true, sheet: '教师', header_row: 1, rows: 0 },
    rooms: { found: true, sheet: '场地', header_row: 1, rows: 1 },
    classes: { found: false, rows: 0 },
  },
  warnings: ['「实验项目」页的 班级数 是统计列（数值），已忽略'],
  ignored_sheets: [],
};

const TASK_PREVIEW: ImportTasksPreview = {
  sheet: '实验项目清单',
  header_row: 1,
  columns: { class_name: '行政班级', experiment: '项目名称', group: '组别' },
  unrecognized_columns: ['备注'],
  recognized_columns: ['class_name', 'experiment', 'group'],
  tasks: 2,
  blank_or_incomplete_rows: 1,
  samples: [
    { class_name: '25级新专业', experiment: '迈克耳孙干涉', group: '1组', teacher: '', room: '', course: '', week: null, weekday: '' },
  ],
};

/** axios 响应形状：{ data: { success, kind, data: 报告 } } */
function wbResponse() {
  return { data: { success: true, kind: 'workbook', data: WB_PREVIEW } } as any;
}
function taskResponse() {
  return { data: { success: true, kind: 'tasks', data: TASK_PREVIEW } } as any;
}

function renderWizard(kind: 'workbook' | 'tasks' = 'workbook', adminAuthed = true) {
  const onRequireLogin = vi.fn();
  const onImported = vi.fn();
  const onClose = vi.fn();
  render(
    <ImportWizard
      kind={kind}
      adminAuthed={adminAuthed}
      onRequireLogin={onRequireLogin}
      onImported={onImported}
      onClose={onClose}
    />,
  );
  return { onRequireLogin, onImported, onClose };
}

/** 造一个 File 并触发 input change */
function chooseFile() {
  const input = document.querySelector('input[type="file"]') as HTMLInputElement;
  const file = new File(['x'], '外校清单.xlsx', {
    type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
  });
  fireEvent.change(input, { target: { files: [file] } });
  return file;
}

async function parseWorkbook() {
  vi.mocked(rescheduleApi.masterImportPreview).mockResolvedValue(wbResponse());
  renderWizard('workbook');
  chooseFile();
  fireEvent.click(screen.getByRole('button', { name: '解析预览' }));
  await waitFor(() => expect(screen.getByRole('button', { name: '确认导入' })).toBeTruthy());
}

describe('ImportWizard', () => {
  it('台账预览：字段←列名/未识别列/统计列/行数全部来自服务端报告；预览阶段无写调用', async () => {
    await parseWorkbook();
    // 列映射：以列名为锚点（唯一），回溯它的映射行
    const mappingLine = screen.getByText('项目名称').closest('div');
    expect(mappingLine?.textContent).toContain('名称');
    expect(mappingLine?.textContent).toContain('任课教师');
    // 未识别列与统计列必须明示
    expect(screen.getByText(/未识别的列（不会导入）：编号、备注/)).toBeTruthy();
    expect(screen.getByText(/统计列已忽略（不当实体名）：班级数/)).toBeTruthy();
    // 行数汇总 + 告警原文
    expect(screen.getByText(/将导入/)).toBeTruthy();
    expect(screen.getByText('3')).toBeTruthy();
    expect(screen.getByText(/解析告警/)).toBeTruthy();
    expect(screen.getByText(/统计列（数值），已忽略/)).toBeTruthy();
    // 样本行
    expect(screen.getByText(/迈克耳孙干涉/)).toBeTruthy();
    // 铁律：预览阶段没有任何写调用
    expect(rescheduleApi.masterImport).not.toHaveBeenCalled();
    expect(rescheduleApi.uploadMaster).not.toHaveBeenCalled();
    expect(rescheduleApi.masterTasksPreview).not.toHaveBeenCalled();
  });

  it('清单预览走 tasks-preview，展示清单口径（条数/空行跳过/样本）', async () => {
    vi.mocked(rescheduleApi.masterTasksPreview).mockResolvedValue(taskResponse());
    renderWizard('tasks');
    fireEvent.click(screen.getByRole('button', { name: /教学任务清单/ }));
    chooseFile();
    fireEvent.click(screen.getByRole('button', { name: '解析预览' }));
    await waitFor(() => expect(rescheduleApi.masterTasksPreview).toHaveBeenCalledTimes(1));
    expect(rescheduleApi.masterImportPreview).not.toHaveBeenCalled();
    await waitFor(() => expect(screen.getByRole('button', { name: '确认导入' })).toBeTruthy());
    expect(screen.getByText(/2 条待排任务/)).toBeTruthy();
    expect(screen.getByText(/1 行班级或实验项目为空，已跳过/)).toBeTruthy();
    expect(screen.getByText(/未识别的列（不会导入）：备注/)).toBeTruthy();
    expect(screen.getByText(/班级：25级新专业/)).toBeTruthy();
  });

  it('没有可导入的内容：确认禁用并说清原因', async () => {
    vi.mocked(rescheduleApi.masterImportPreview).mockResolvedValue({
      data: { success: true, kind: 'workbook', data: { ...WB_PREVIEW, tables: {
        courses: { found: false, rows: 0 }, experiments: { found: false, rows: 0 },
        teachers: { found: false, rows: 0 }, rooms: { found: false, rows: 0 },
        classes: { found: false, rows: 0 },
      }, warnings: [] } },
    } as any);
    renderWizard('workbook');
    chooseFile();
    fireEvent.click(screen.getByRole('button', { name: '解析预览' }));
    await waitFor(() => expect(screen.getByText(/没有可导入的内容/)).toBeTruthy());
    expect((screen.getByRole('button', { name: '确认导入' }) as HTMLButtonElement).disabled).toBe(true);
    expect(rescheduleApi.masterImport).not.toHaveBeenCalled();
  });

  it('未登录管理员：确认走 onRequireLogin，不调写接口', async () => {
    vi.mocked(rescheduleApi.masterImportPreview).mockResolvedValue(wbResponse());
    const { onRequireLogin } = renderWizard('workbook', false);
    chooseFile();
    fireEvent.click(screen.getByRole('button', { name: '解析预览' }));
    await waitFor(() => expect(screen.getByRole('button', { name: '确认导入' })).toBeTruthy());
    fireEvent.click(screen.getByRole('button', { name: '确认导入' }));
    expect(onRequireLogin).toHaveBeenCalledTimes(1);
    expect(rescheduleApi.masterImport).not.toHaveBeenCalled();
  });

  it('确认导入：带原文件调 masterImport，展示回执与告警，回调 onImported', async () => {
    vi.mocked(rescheduleApi.masterImportPreview).mockResolvedValue(wbResponse());
    vi.mocked(rescheduleApi.masterImport).mockResolvedValue({
      data: { success: true, message: '✅ 已导入 3 行（新建实体 3 个）；导入只增补，不删除已有实体',
              report: { warnings: ['某条告警'] } },
    } as any);
    const { onImported, onClose } = renderWizard('workbook');
    const file = chooseFile();
    fireEvent.click(screen.getByRole('button', { name: '解析预览' }));
    await waitFor(() => expect(screen.getByRole('button', { name: '确认导入' })).toBeTruthy());
    fireEvent.click(screen.getByRole('button', { name: '确认导入' }));
    await waitFor(() => expect(rescheduleApi.masterImport).toHaveBeenCalledTimes(1));
    expect(vi.mocked(rescheduleApi.masterImport).mock.calls[0][0]).toBe(file);
    await waitFor(() => expect(screen.getByText(/已导入 3 行/)).toBeTruthy());
    expect(screen.getByText(/某条告警/)).toBeTruthy();
    expect(onImported).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole('button', { name: '完成' }));
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('清单确认走 uploadMaster', async () => {
    vi.mocked(rescheduleApi.masterTasksPreview).mockResolvedValue(taskResponse());
    vi.mocked(rescheduleApi.uploadMaster).mockResolvedValue({
      data: { success: true, message: '✅ 已读入 2 条主数据任务（可直接进并行排课）', report: { warnings: [] } },
    } as any);
    renderWizard('tasks');
    chooseFile();
    fireEvent.click(screen.getByRole('button', { name: '解析预览' }));
    await waitFor(() => expect(screen.getByRole('button', { name: '确认导入' })).toBeTruthy());
    fireEvent.click(screen.getByRole('button', { name: '确认导入' }));
    await waitFor(() => expect(rescheduleApi.uploadMaster).toHaveBeenCalledTimes(1));
    expect(screen.getByText(/已读入 2 条主数据任务/)).toBeTruthy();
  });

  it('预览失败（400）停在第 1 步，错误原因原样呈现', async () => {
    vi.mocked(rescheduleApi.masterTasksPreview).mockRejectedValue({
      response: { status: 400, data: { detail: '清单解析失败：清单里必须至少能认出「班级」和「实验项目」两列' } },
    });
    renderWizard('tasks');
    chooseFile();
    fireEvent.click(screen.getByRole('button', { name: '解析预览' }));
    await waitFor(() => expect(screen.getByText(/必须至少能认出「班级」和「实验项目」两列/)).toBeTruthy());
    // 没有进入预览步：确认按钮不可见（还在第 1 步）
    expect(screen.queryByRole('button', { name: '确认导入' })).toBeNull();
    expect(rescheduleApi.uploadMaster).not.toHaveBeenCalled();
  });
});
