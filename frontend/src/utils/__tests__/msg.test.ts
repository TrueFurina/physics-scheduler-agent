import { describe, expect, it } from 'vitest';
import { clearStaleLoadBanner, LOAD_FAILED_PREFIX } from '../msg';

describe('clearStaleLoadBanner', () => {
  it('清掉首屏加载自己留下的「加载失败」横幅', () => {
    expect(clearStaleLoadBanner(`${LOAD_FAILED_PREFIX}：服务端出错（HTTP 500）`)).toBe('');
    expect(clearStaleLoadBanner(LOAD_FAILED_PREFIX)).toBe('');
  });

  it('空横幅原样空', () => {
    expect(clearStaleLoadBanner('')).toBe('');
  });

  it('不清用户点击触发的错误（竞态修复的核心判据）', () => {
    // 首屏后台加载的成功可能晚于用户点击：这些横幅必须活下来
    const userErrors = [
      '并行排课失败：服务端出错（HTTP 500），请稍后重试或刷新页面。',
      '全校排课失败：服务端出错（HTTP 500）',
      '导出失败：服务端出错（HTTP 500）',
      '提交失败：云端服务不可用',
      '审批失败：云端服务不可用',
    ];
    for (const msg of userErrors) {
      expect(clearStaleLoadBanner(msg)).toBe(msg);
    }
  });

  it('不清其他流程的失败横幅（前缀必须精确，不能误伤）', () => {
    // 「加载课程失败」「加载课程总览失败」是别的流程的文案，
    // 与 LOAD_FAILED_PREFIX 只差几个字，但一个都不能清
    for (const msg of ['加载课程失败：服务端出错', '加载课程总览失败：模拟故障']) {
      expect(clearStaleLoadBanner(msg)).toBe(msg);
    }
  });
});
