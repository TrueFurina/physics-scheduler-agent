// @vitest-environment jsdom
/**
 * 审批台驳回理由必须由管理员填写（R34）。
 *
 * 现状：`AdminView` 的驳回按钮写死了理由
 *
 *     onClick={() => onApprove(r.id, false, '时间冲突，请重新选择')}
 *
 * 链路是通的 —— 后端 `RescheduleRequestApprove.comment` 支持任意文本，
 * `App.handleApprove` 也把 comment 传下去了。**只有 UI 写死了。**
 *
 * 后果不是报错，而是**记录不实**：管理员因为"教室维修"驳回，教师收到的
 * 却是"时间冲突，请重新选择"，于是去改时间 —— 改了也没用。
 *
 * 这与项目里反复出现的「静默不实」同族：系统说的和真实情况不一致，
 * 而且不报错、测试也不红。
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import type { RescheduleRequest } from '../../api/client';
import AdminView from '../AdminView';

afterEach(cleanup);

const req = (over: Partial<RescheduleRequest> = {}): RescheduleRequest => ({
  id: 7,
  teacher: '吴琳',
  course_name: '声速测量',
  class_name: '2024级电子1班',
  original_time: '周三 13:30-15:45',
  target_week: 5,
  source_week: 3,
  reason: '实验室设备检修',
  status: 'pending',
  ...over,
} as RescheduleRequest);

const base = { loading: false, onBack: vi.fn() };

describe('AdminView · 驳回理由（R34）', () => {
  it('驳回时可以填写理由，且填的内容会传给 onApprove', () => {
    const onApprove = vi.fn();
    render(<AdminView {...base} requests={[req()]} onApprove={onApprove} />);

    // 点"驳回"应进入可填写理由的状态，而不是立刻提交一个写死的理由
    const rejectBtn = screen.getByRole('button', { name: /驳回/ });
    fireEvent.click(rejectBtn);

    // 写了一句话也不该已经提交（要先让管理员确认）
    expect(onApprove).not.toHaveBeenCalled();

    const box = screen.getByRole('textbox');
    fireEvent.change(box, { target: { value: '该教室周四起维修，请改约' } });
    fireEvent.click(screen.getByRole('button', { name: /确认驳回/ }));

    expect(onApprove).toHaveBeenCalledTimes(1);
    expect(onApprove.mock.calls[0][1]).toBe(false);
    expect(onApprove.mock.calls[0][2]).toBe('该教室周四起维修，请改约');
  });

  it('驳回理由留空时不应提交写死的话术，而是要求填写', () => {
    const onApprove = vi.fn();
    render(<AdminView {...base} requests={[req()]} onApprove={onApprove} />);

    fireEvent.click(screen.getByRole('button', { name: /驳回/ }));
    fireEvent.click(screen.getByRole('button', { name: /确认驳回/ }));

    expect(onApprove).not.toHaveBeenCalled();
    expect(document.body.textContent).toMatch(/请(填写|说明|写明).*理由/);
  });

  it('同意仍然一键完成，不强迫走弹窗', () => {
    const onApprove = vi.fn();
    render(<AdminView {...base} requests={[req()]} onApprove={onApprove} />);
    fireEvent.click(screen.getByRole('button', { name: /同意/ }));
    expect(onApprove).toHaveBeenCalledTimes(1);
    expect(onApprove.mock.calls[0][1]).toBe(true);
  });
});
