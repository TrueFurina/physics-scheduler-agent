// @vitest-environment jsdom
/**
 * 500 的错误编号必须出现在用户眼前（R54）
 *
 * 背景
 * ----
 * R53 让后端在 500 时返回 `{detail, error_id}` 且所有响应都带 `x-request-id`。
 * 但前端 `describeApiError` 只读 `detail`，`error_id` 一个字都没用上 ——
 * 于是用户看到的仍是"服务器内部错误"，报给我们时也只能说"报错了"。
 * **后端给了编号 ≠ 用户看得到编号**（R51 同款：接口有了字段，界面没接）。
 *
 * 反过来也要守住：老后端（无编号、text/plain）的行为一个字节都不能变，
 * 否则又是一次"修一个场景、弄坏另一个场景"。
 */

import { describe, expect, it } from 'vitest';
import { describeApiError } from '../apiError';

const err = (
  response: { status: number; data?: unknown; headers?: Record<string, unknown> },
): unknown => ({
  isAxiosError: true,
  message: `Request failed with status code ${response.status}`,
  response: {
    status: response.status,
    data: response.data,
    headers: response.headers ?? {},
  },
});

describe('describeApiError · 500 的错误编号', () => {
  it('响应体带 error_id 时，消息里必须有那个编号', () => {
    const msg = describeApiError(
      err({
        status: 500,
        data: {
          detail: '服务器内部错误。请把错误编号（error_id）提供给管理员，便于定位。',
          error_id: 'a1b2c3d4',
        },
      }),
    );

    expect(msg).toContain('a1b2c3d4');
    // 不能把后端的占位文案原样透出来 —— 那句里的 "error_id" 是字段名，不是值
    expect(msg).not.toContain('（error_id）');
  });

  it('只有 x-request-id 头时也能取到编号', () => {
    const msg = describeApiError(
      err({ status: 503, data: 'upstream error', headers: { 'x-request-id': 'ffee0011' } }),
    );

    expect(msg).toContain('ffee0011');
  });

  it('5xx 带编号时不再劝人"稍后重试"（代码 bug 重试没有用）', () => {
    const msg = describeApiError(
      err({ status: 500, data: { detail: '服务端错误', error_id: 'deadbeef' } }),
    );

    expect(msg).not.toMatch(/重试|再试/);
  });

  it('老后端（无编号、text/plain）保持原行为', () => {
    const msg = describeApiError(err({ status: 500, data: 'Internal Server Error' }));

    expect(msg).toContain('HTTP 500');
    expect(msg).not.toContain('错误编号');
  });

  it('4xx 有 detail 时仍然优先用 detail，不被编号逻辑抢走', () => {
    const msg = describeApiError(
      err({ status: 400, data: { detail: '请先加载实验安排表，再试一次', error_id: 'ignored' } }),
    );

    expect(msg).toContain('请先加载实验安排表');
    expect(msg).not.toContain('ignored');
  });
});
