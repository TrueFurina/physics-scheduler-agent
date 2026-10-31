// @vitest-environment jsdom
/**
 * 顶部横幅不能把成功渲染成错误（R67）。
 *
 * 现状
 * ----
 * `App.tsx` 的横幅：
 *
 *     <div className="bg-red-50 text-red-600 ... border-red-200">
 *       <span>{msg}</span>
 *
 * **样式写死是红色的**，而 `msg` 的取值来源里既有失败也有成功：
 *
 *     setMsg(`✅ 全校 17 个班全部排定，共 410 条安排`)   // 成功
 *     setMsg(`已驳回：大学物理实验`)                     // 正常操作
 *     setMsg(`审批失败：...`)                            // 失败
 *
 * 实测后果：教秘点「同意」之后弹出的 `✅ 已移动 3 条排课记录` 是**红底红字**，
 * 关闭按钮还叫「关闭提示」—— 看起来像出错了。
 *
 * 这和 R24/R66 是同一族问题的第三面：
 *   R24  灰字没区分失败
 *   R66  自动排课消息没区分 fail
 *   R67  **横幅只有一种颜色**，把成功画成了失败
 *
 * 修法
 * ----
 * 按文案前缀选样式：`✅` / `已` 开头用绿，`❌` / `⚠️` / 含「失败」用红，
 * 其余用中性灰（如「已驳回：xxx」不红不绿 —— 它是一个正常结果，但不是"好事"）。
 *
 * 本文件用源码契约守（横幅在 App 顶层，挂载需要云 SDK mock）。
 */
import { describe, expect, it } from 'vitest';
import appSrc from '../../../src/App.tsx?raw';

describe('顶部横幅的成败配色（R67）', () => {
  function banner(): string {
    const start = appSrc.indexOf('{msg && (');
    expect(start, '找不到横幅').toBeGreaterThan(-1);
    const end = appSrc.indexOf('{/* 云端登录弹窗', start);
    expect(end).toBeGreaterThan(start);
    return appSrc.slice(start, end);
  }

  it('横幅样式不能写死成一种颜色', () => {
    const b = banner();
    // 不能只有 bg-red-50 这一种
    // 横幅样式写死了的话，成功的 ✅ 消息也会是红底红字
    // （R67：教秘点「同意」后看到红条，会以为出错了）
    expect(Boolean(b.match(/msgTone|tone|variant|kind/i))).toBe(true);
  });

  it('成功/失败/中性三种色调都要有', () => {
    const b = banner();
    expect(Boolean(b.match(/emerald|green/))).toBe(true);
    expect(Boolean(b.match(/red/))).toBe(true);
    expect(Boolean(b.match(/amber|gray|slate/))).toBe(true);
  });

  it('关闭按钮的 aria-label 不能只写「提示」', () => {
    const b = banner();
    const m = b.match(/aria-label="([^"]+)"/);
    expect(m, '关闭按钮没有 aria-label').toBeTruthy();
    expect(m![1]).not.toBe('关闭提示');
  });
});
