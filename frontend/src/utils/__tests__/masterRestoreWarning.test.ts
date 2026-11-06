// @vitest-environment node
/**
 * R88：主数据「没从落盘恢复回来」的警告必须到得了主数据管理页。
 *
 * 后端 `_ensure_master_data_locked()` 有三种异常降级（落盘损坏 / 恢复抛异常 /
 * 空主数据脏数据），都会丢掉教秘已保存的修改、回退到安排表反推基线，
 * 却只 print 到服务端日志。`GET /master/overview` 从 R88 起附
 * `restored: false` + `warning`。这里守两件事：
 *   1. 纯函数判据必须是 `=== false`（老后端 / 正常响应不许误报）；
 *   2. 光有 helper 没人调用等于没修（R86 教训）：client.ts 得声明字段，
 *      MasterDataView.tsx 得接住并显示独立黄色横幅。
 */
import { describe, expect, it } from 'vitest';
import fs from 'node:fs';
import path from 'node:path';
import * as msg from '../msg';

type RestoreData = {
  restored?: boolean;
  warning?: string;
};

// 用命名空间取值：helper 还不存在时模块仍能加载，防放宽用例红阶段照常绿。
const fn = (msg as Record<string, unknown>)['masterRestoreWarningOf'] as
  | ((data?: RestoreData | undefined) => string)
  | undefined;

const SRC = path.resolve(__dirname, '../..');
const viewSource = () => fs.readFileSync(path.resolve(SRC, 'views', 'MasterDataView.tsx'), 'utf8');
const clientSource = () => fs.readFileSync(path.resolve(SRC, 'api', 'client.ts'), 'utf8');
const appSource = () => fs.readFileSync(path.resolve(SRC, 'App.tsx'), 'utf8');

describe('masterRestoreWarningOf：恢复降级 → 横幅文案', () => {
  it('restored 不是 false 时一律安静（不许制造狼来了）', () => {
    expect(typeof fn).toBe('function');
    expect(fn!(undefined)).toBe('');
    expect(fn!({})).toBe('');
    expect(fn!({ restored: true, warning: '不该显示' })).toBe('');
    // 老后端 / 正常响应没有这个字段：不能因为 undefined 就报警
    expect(fn!({ restored: undefined, warning: '不该显示' })).toBe('');
  });

  it('restored=false 时返回后端给的警告，并带 ⚠️ 语气', () => {
    expect(typeof fn).toBe('function');
    const text = fn!({ restored: false, warning: '⚠️ 重启后未能恢复，已回退基线' });
    expect(text.startsWith('⚠️')).toBe(true);
    expect(text).toContain('回退基线');
  });

  it('判据必须是 === false：不能用 !restored 把缺字段当失败', () => {
    expect(typeof fn).toBe('function');
    const src = fs.readFileSync(path.resolve(SRC, 'utils', 'msg.ts'), 'utf8');
    const body = src.slice(src.indexOf('masterRestoreWarningOf'));
    expect(body).toContain('=== false');
    expect(body).not.toMatch(/if \(!data\??\.restored\)/);
  });

  it('restored=false 但 warning 缺失时兜底，不能什么都不显示', () => {
    expect(typeof fn).toBe('function');
    const text = fn!({ restored: false });
    expect(text.startsWith('⚠️')).toBe(true);
    expect(text).toContain('重启');
    expect(text).toContain('回退');
  });

  it('warning 不带 ⚠️ 前缀时补上，避免被渲染成中性提示', () => {
    expect(typeof fn).toBe('function');
    const text = fn!({ restored: false, warning: '落盘损坏，已丢弃' });
    expect(text.startsWith('⚠️')).toBe(true);
    expect(text).toContain('落盘损坏');
  });
});

describe('接线守卫：后端说的真话，前端得接住', () => {
  it('client.ts 的主数据读接口类型必须声明 restored / warning', () => {
    const src = clientSource();
    expect(src).toContain('restored?: boolean');
    expect(src).toContain('warning?: string');
    // `masterOverview` 用的是同一份 MasterWriteResult 形状
    const lines = src.split('\n');
    const idx = lines.findIndex((l) => l.includes('masterOverview:'));
    expect(idx).toBeGreaterThanOrEqual(0);
    expect(lines.slice(idx, idx + 3).join('\n')).toContain('MasterWriteResult');
  });

  it('MasterDataView.tsx 必须在 load() 里接住并显示独立警告横幅', () => {
    const src = viewSource();
    expect(src).toContain('masterRestoreWarningOf(');
    expect(src).toContain('setRestoreWarning');
    // 恢复降级与「落盘失败」是两件事，不许只复用一条横幅的状态
    expect(src).toContain('restoreWarning &&');
    const loadStart = src.indexOf('const load = async ()');
    const loadEnd = src.indexOf('useEffect(', loadStart);
    expect(src.slice(loadStart, loadEnd)).toContain('setRestoreWarning(');
  });

  it('防放宽：本轮不碰 App.tsx，体量守卫与既有消息工具保持原样', () => {
    expect(appSource().split('\n').length).toBeLessThanOrEqual(1500);
    expect(appSource()).toContain('loadWarningOf');
    expect(clientSource()).toContain('masterOverview:');
    expect(viewSource()).toContain('masterPersistWarningOf(');
  });
});
