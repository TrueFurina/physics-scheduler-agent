import { describe, expect, it, vi } from 'vitest';
import { whenIdle, withRetry } from '../async';

describe('withRetry', () => {
  it('第一次成功就不重试', async () => {
    const fn = vi.fn().mockResolvedValue('ok');
    await expect(withRetry(fn, [0, 1, 1])).resolves.toBe('ok');
    expect(fn).toHaveBeenCalledTimes(1);
  });

  it('失败后按退避序列重试，直到成功', async () => {
    const fn = vi
      .fn()
      .mockRejectedValueOnce(new Error('sandbox recycled'))
      .mockRejectedValueOnce(new Error('still cold'))
      .mockResolvedValue('warm');

    await expect(withRetry(fn, [0, 0, 0])).resolves.toBe('warm');
    expect(fn).toHaveBeenCalledTimes(3);
  });

  it('次数用尽后抛出**最后一个**错误（不是第一个）', async () => {
    const fn = vi
      .fn()
      .mockRejectedValueOnce(new Error('first'))
      .mockRejectedValueOnce(new Error('last'));

    await expect(withRetry(fn, [0, 0])).rejects.toThrow('last');
    expect(fn).toHaveBeenCalledTimes(2);
  });

  it('默认只尝试 3 次（首屏失败不能无限重试）', async () => {
    const fn = vi.fn().mockRejectedValue(new Error('down'));
    await expect(withRetry(fn)).rejects.toThrow('down');
    expect(fn).toHaveBeenCalledTimes(3);
  });
});

describe('whenIdle', () => {
  it('支持 requestIdleCallback 时走它，并带上超时兜底', () => {
    const ric = vi.fn();
    const fn = vi.fn();
    vi.stubGlobal('requestIdleCallback', ric);

    whenIdle(fn);

    expect(ric).toHaveBeenCalledTimes(1);
    expect(ric.mock.calls[0][1]).toEqual({ timeout: 2500 });
    vi.unstubAllGlobals();
  });

  it('不支持时（Safari）退回定时器，且仍会执行', () => {
    const fn = vi.fn();
    vi.stubGlobal('requestIdleCallback', undefined);
    vi.useFakeTimers();

    whenIdle(fn);
    expect(fn).not.toHaveBeenCalled();
    vi.advanceTimersByTime(1200);
    expect(fn).toHaveBeenCalledTimes(1);

    vi.useRealTimers();
    vi.unstubAllGlobals();
  });
});
