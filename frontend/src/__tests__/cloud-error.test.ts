import { describe, expect, it } from 'vitest';
import { describeCloudError } from '../cloud';

/**
 * 错误文案是"用户唯一能看到的东西"：云 SDK 的 code 前缀映射写错一个，
 * 用户看到的就是「云端请求失败，请稍后重试」这种等于没说的话。
 */
describe('describeCloudError', () => {
  it('按 error.code 前缀给出可行动的提示', () => {
    expect(describeCloudError({ error: { code: 'quota_exceeded' } })).toContain('额度');
    expect(describeCloudError({ error: { code: 'auth_origin' } })).toContain('访问来源');
    expect(describeCloudError({ error: { code: 'request_invalid' } })).toContain('参数');
    expect(describeCloudError({ error: { code: 'gateway_timeout' } })).toContain('暂时不可用');
    expect(describeCloudError({ error: { code: 'internal_error' } })).toContain('internal_error');
  });

  it('流中断单独给文案（已生成内容要保留，不能提示用户从头再来）', () => {
    const msg = describeCloudError({ error: { code: 'gateway_stream_interrupted' } });
    expect(msg).toContain('中断');
  });

  it('未知结构退化为 message，再退化为兜底文案', () => {
    expect(describeCloudError(new Error('boom'))).toBe('boom');
    expect(describeCloudError(undefined)).toBe('云端请求失败，请稍后重试。');
    expect(describeCloudError({})).toBe('云端请求失败，请稍后重试。');
  });
});
