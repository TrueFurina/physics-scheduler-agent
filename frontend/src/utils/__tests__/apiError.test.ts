import { describe, expect, it } from 'vitest';
import { describeApiError } from '../apiError';

/**
 * 这些用例锁定的是**实测出来的缺陷**，不是假想的边界：
 * 把 /teachers 拦成 500（FastAPI 未捕获异常回的就是 text/plain）时，界面曾经显示
 * 「加载失败：Request failed with status code 500」——中文界面里的 axios 原文。
 */

const EN_NOISE = /Request failed with status code|Network Error|timeout of \d+\s*ms exceeded|Internal Server Error/i;
const ACTIONABLE = /重试|再试|刷新|稍后|稍候|检查/;

function axiosErr(opts: {
  status?: number;
  data?: unknown;
  headers?: Record<string, unknown>;
  code?: string;
  message?: string;
}) {
  return {
    message: opts.message ?? `Request failed with status code ${opts.status}`,
    code: opts.code,
    response:
      opts.status === undefined
        ? undefined
        : { status: opts.status, data: opts.data, headers: opts.headers ?? {} },
  };
}

describe('describeApiError', () => {
  it('无 detail 的 500（text/plain）翻译成中文，不泄漏 axios 原文', () => {
    const msg = describeApiError(axiosErr({ status: 500, data: 'Internal Server Error' }));
    expect(msg).toContain('服务端出错');
    expect(msg).toContain('HTTP 500');
    expect(msg).not.toMatch(EN_NOISE);
    expect(msg).toMatch(ACTIONABLE);
  });

  it('后端给了中文 detail 就原样用，且不重复追加指引', () => {
    const msg = describeApiError(axiosErr({ status: 400, data: { detail: '请先加载实验安排表，再试一次' } }));
    expect(msg).toBe('请先加载实验安排表，再试一次');
    expect(msg.match(/重试|再试/g)?.length).toBe(1);
  });

  it('detail 不带指引时补一句下一步', () => {
    const msg = describeApiError(axiosErr({ status: 400, data: { detail: '缺少参数 class_name' } }));
    expect(msg.startsWith('缺少参数 class_name')).toBe(true);
    expect(msg).toMatch(ACTIONABLE);
  });

  it('422 的数组 detail 拼成一句', () => {
    const msg = describeApiError(
      axiosErr({ status: 422, data: { detail: [{ msg: 'field required' }, { msg: 'bad week' }] } }),
    );
    expect(msg).toContain('参数校验失败');
    expect(msg).toContain('field required');
    expect(msg).toContain('bad week');
  });

  it('429 读 Retry-After，把等待秒数说出来', () => {
    const msg = describeApiError(
      axiosErr({ status: 429, data: { detail: '请求过于频繁，请稍候再试。' }, headers: { 'retry-after': '15' } }),
    );
    expect(msg).toContain('15 秒');
  });

  it('429 没有 Retry-After 时退回"稍后再试"', () => {
    const msg = describeApiError(axiosErr({ status: 429, data: { detail: '请求过于频繁，请稍候再试。' } }));
    expect(msg).toMatch(/稍后再试|稍候/);
    expect(msg).not.toMatch(EN_NOISE);
  });

  it('401/403 提示重新登录，不冒充"密码错"', () => {
    const msg = describeApiError(axiosErr({ status: 401 }));
    expect(msg).toContain('登录');
    expect(msg).toMatch(ACTIONABLE);
  });

  it('网络断开（ERR_NETWORK）不再显示 "Network Error"', () => {
    const msg = describeApiError(
      axiosErr({ code: 'ERR_NETWORK', message: 'Network Error' }),
    );
    expect(msg).toContain('无法连接服务');
    expect(msg).not.toMatch(EN_NOISE);
  });

  it('超时提示"服务可能正忙"', () => {
    const msg = describeApiError(
      axiosErr({ code: 'ECONNABORTED', message: 'timeout of 60000ms exceeded' }),
    );
    expect(msg).toContain('超时');
    expect(msg).not.toMatch(EN_NOISE);
  });

  it('完全认不出的异常也给中文兜底', () => {
    expect(describeApiError(undefined)).not.toMatch(EN_NOISE);
    expect(describeApiError(new Error('boom'))).not.toMatch(EN_NOISE);
    expect(describeApiError(new Error('boom'))).toMatch(ACTIONABLE);
  });

  it('所有分支的输出都不含英文原文（回归：这是本模块存在的唯一理由）', () => {
    const samples = [
      axiosErr({ status: 500, data: 'Internal Server Error' }),
      axiosErr({ status: 502 }),
      axiosErr({ status: 404 }),
      axiosErr({ status: 429, headers: { 'Retry-After': '7' } }),
      axiosErr({ code: 'ERR_NETWORK', message: 'Network Error' }),
      axiosErr({ code: 'ECONNABORTED', message: 'timeout of 60000ms exceeded' }),
      new Error('Request failed with status code 500'),
    ];
    for (const s of samples) {
      expect(describeApiError(s)).not.toMatch(EN_NOISE);
    }
  });
});
