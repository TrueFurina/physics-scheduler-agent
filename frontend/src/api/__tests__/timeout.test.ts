import { describe, expect, it } from 'vitest';
import { api, API_TIMEOUT_MS } from '../client';
import { describeApiError } from '../../utils/apiError';

/**
 * 为什么锁这条
 * ------------
 * `utils/apiError.ts` 有一条 `ECONNABORTED / timeout of …ms exceeded` 的中文分支
 * （「请求超时（服务可能正忙），请稍后重试。」），文档和探针注释也都按"60s 会超时"写。
 * 但 `axios.create()` 不带 `timeout` 时默认值是 **0 = 永不超时** —— 也就是说那个分支
 * 在真实浏览器里基本不可达，网关挂死时用户看到的是**无限转圈**而不是那句话。
 *
 * 这与 R-25（探针吃静态副标题、abort 掉接口照样 PASS）是同一类病：
 * **一段看起来在防护的代码，实际从不生效**。所以这里不只测"文案对不对"，
 * 还测"超时到底配没配"——两个断言必须同时成立，缺一个就是假防护。
 */

describe('HTTP 客户端超时配置', () => {
  it('axios 实例必须真的配了超时（0 = 永不超时，等于没有防护）', () => {
    expect(api.defaults.timeout).toBe(API_TIMEOUT_MS);
    expect(API_TIMEOUT_MS).toBeGreaterThan(0);
  });

  it('超时值要大于已知最慢的合法请求（满载全校一键排课 16.6s / LLM 解析 7.6s）', () => {
    // 60s 给了 3 倍余量：正常演示永远碰不到，只兜"连接挂死"。
    expect(API_TIMEOUT_MS).toBeGreaterThanOrEqual(30_000);
  });

  it('超时配置与文案口径一致：配上超时后，那段中文提示才是可达的', () => {
    // 模拟 axios 因超时抛出的错误（code=ECONNABORTED，message 无 response）
    const timeoutErr = {
      code: 'ECONNABORTED',
      message: `timeout of ${API_TIMEOUT_MS}ms exceeded`,
    };
    const text = describeApiError(timeoutErr);

    expect(text).toContain('超时');
    expect(text).not.toMatch(/timeout of/i);   // 不许把英文原文漏给教秘
    expect(text).toMatch(/重试|稍后/);          // 必须给出下一步
  });
});
