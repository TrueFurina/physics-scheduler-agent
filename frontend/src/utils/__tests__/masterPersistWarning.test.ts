// @vitest-environment node
/**
 * R87：主数据「没存住」的警告必须真的到得了主数据管理页。
 *
 * 后端 master_store.save() 失败时只返回 False 不抛异常，14 个写入端点
 * 却一律回 `{"success": true, ...}` —— 教秘以为保存好了，重启/沙箱回收
 * 后修改全部消失。补的是结构化 `persisted: false` + `warning`，这里守两件事：
 *   1. 纯函数判据必须是 `=== false`（老后端没这字段时不许误报）；
 *   2. 光有 helper 没人调用等于没修（R86 教训）：client.ts 得声明字段，
 *      MasterDataView.tsx 得接住并显示，不能塞进红色错误框冒充「失败」。
 */
import { describe, expect, it } from 'vitest';
import fs from 'node:fs';
import path from 'node:path';
import * as msg from '../msg';

type PersistData = {
  persisted?: boolean;
  warning?: string;
};

// 用命名空间取值：helper 还不存在时模块仍能加载，防放宽用例红阶段照常绿。
const fn = (msg as Record<string, unknown>)['masterPersistWarningOf'] as
  | ((data?: PersistData | undefined) => string)
  | undefined;

const SRC = path.resolve(__dirname, '../..');
const viewSource = () => fs.readFileSync(path.resolve(SRC, 'views', 'MasterDataView.tsx'), 'utf8');
const clientSource = () => fs.readFileSync(path.resolve(SRC, 'api', 'client.ts'), 'utf8');
const appSource = () => fs.readFileSync(path.resolve(SRC, 'App.tsx'), 'utf8');

describe('masterPersistWarningOf：落盘失败 → 横幅文案', () => {
  it('persisted 不是 false 时一律安静（不许制造狼来了）', () => {
    expect(typeof fn).toBe('function');
    expect(fn!(undefined)).toBe('');
    expect(fn!({})).toBe('');
    expect(fn!({ persisted: true, warning: '不该显示' })).toBe('');
    // 老后端没有这个字段：不能因为 undefined 就报警
    expect(fn!({ persisted: undefined, warning: '不该显示' })).toBe('');
  });

  it('persisted=false 时返回后端给的警告，并带 ⚠️ 语气', () => {
    expect(typeof fn).toBe('function');
    const text = fn!({ persisted: false, warning: '⚠️ 未写入磁盘，重启会丢' });
    expect(text.startsWith('⚠️')).toBe(true);
    expect(text).toContain('重启会丢');
  });

  it('判据必须是 === false：不能用 !persisted 把缺字段当失败', () => {
    expect(typeof fn).toBe('function');
    const src = fs.readFileSync(path.resolve(SRC, 'utils', 'msg.ts'), 'utf8');
    const body = src.slice(src.indexOf('masterPersistWarningOf'));
    expect(body).toContain('=== false');
    expect(body).not.toMatch(/if \(!data\??\.persisted\)/);
  });

  it('persisted=false 但 warning 缺失时兜底，不能什么都不显示', () => {
    expect(typeof fn).toBe('function');
    const text = fn!({ persisted: false });
    expect(text.startsWith('⚠️')).toBe(true);
    expect(text.length).toBeGreaterThan(2);
  });

  it('warning 不带 ⚠️ 前缀时补上，避免被渲染成中性提示', () => {
    expect(typeof fn).toBe('function');
    const text = fn!({ persisted: false, warning: '磁盘不可写' });
    expect(text.startsWith('⚠️')).toBe(true);
    expect(text).toContain('磁盘不可写');
  });
});

describe('接线守卫：后端说的真话，前端得接住', () => {
  it('client.ts 的主数据写接口类型必须声明 persisted / warning', () => {
    const src = clientSource();
    expect(src).toContain('persisted?: boolean');
    expect(src).toContain('warning?: string');
    // `api.post<MasterWriteResult>(...)` 在声明的**下一行**，只查单行会假红。
    const lines = src.split('\n');
    const idx = lines.findIndex((l) => l.includes('masterUpsertTeacher:'));
    expect(idx).toBeGreaterThanOrEqual(0);
    expect(lines.slice(idx, idx + 3).join('\n')).toContain('MasterWriteResult');
  });

  it('MasterDataView.tsx 必须检查 persisted === false 并显示独立警告横幅', () => {
    const src = viewSource();
    expect(src).toContain('masterPersistWarningOf(');
    expect(src).toContain('setPersistWarning');
    expect(src).toContain('bg-amber-50');
  });

  it('不许把「没存住」塞进红色错误框当成操作失败', () => {
    const src = viewSource();
    expect(src).toMatch(/setPersistWarning\(/);
    expect(src).toMatch(/persistWarning &&/);
  });

  it('防放宽：本轮不碰 App.tsx，体量守卫与既有消息工具保持原样', () => {
    expect(appSource().split('\n').length).toBeLessThanOrEqual(1500);
    expect(appSource()).toContain('loadWarningOf');
    expect(clientSource()).toContain('masterOverview:');
    expect(clientSource()).toContain('masterDeleteCourse:');
  });
});
