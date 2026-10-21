// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import ErrorBoundary, { isChunkLoadError } from '../ErrorBoundary';

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe('isChunkLoadError', () => {
  it('认得各浏览器对动态 import 失败的措辞', () => {
    const cases = [
      'Failed to fetch dynamically imported module: https://x/assets/ParallelView-abc.js',
      'Importing a module script failed.',
      'error loading dynamically imported module',
      'ChunkLoadError: Loading chunk 3 failed',
      'Loading chunk vendor failed.',
    ];
    for (const message of cases) expect(isChunkLoadError(new Error(message))).toBe(true);
  });

  it('普通运行时错误不能被误判成资源更新（否则会把用户引向无意义的刷新）', () => {
    const cases = [
      new Error("Cannot read properties of undefined (reading 'class_name')"),
      new Error('fail to fetch'),          // 只是子串相近，但不是模块加载
      'some string error',
      null,
      undefined,
    ];
    for (const err of cases) expect(isChunkLoadError(err)).toBe(false);
  });
});

/** 渲染期抛错的视图，用来验证边界接管。 */
function Bomb({ shouldThrow }: { shouldThrow: boolean }) {
  if (shouldThrow) throw new Error('boom');
  return <div>视图内容</div>;
}

describe('ErrorBoundary', () => {
  it('不带错误时原样渲染子节点（不能影响正常路径）', () => {
    render(
      <ErrorBoundary label="courses">
        <Bomb shouldThrow={false} />
      </ErrorBoundary>,
    );
    expect(screen.getByText('视图内容')).toBeTruthy();
    expect(screen.queryByText('这个模块遇到了问题')).toBeNull();
  });

  it('渲染异常只显示提示卡，不把错误细节当正文抛给用户', () => {
    // React 会把边界捕获的错误再往 console.error 打一遍，测试里静音免得刷屏
    vi.spyOn(console, 'error').mockImplementation(() => {});
    render(
      <ErrorBoundary label="courses">
        <Bomb shouldThrow={true} />
      </ErrorBoundary>,
    );
    expect(screen.getByText('这个模块遇到了问题')).toBeTruthy();
    expect(screen.getByText('重试')).toBeTruthy();
    // 原始异常文案藏在 <details> 里，不是主文案
    expect(screen.getByText('技术详情')).toBeTruthy();
    expect(screen.queryByText('视图内容')).toBeNull();
  });

  it('重试会重新挂载子节点：错误是暂时的就不该逼用户刷新整页', () => {
    vi.spyOn(console, 'error').mockImplementation(() => {});
    let shouldThrow = true;
    function Flaky() {
      if (shouldThrow) throw new Error('temporary');
      return <div>恢复后的内容</div>;
    }
    render(
      <ErrorBoundary label="courses">
        <Flaky />
      </ErrorBoundary>,
    );
    expect(screen.getByText('这个模块遇到了问题')).toBeTruthy();

    shouldThrow = false;
    fireEvent.click(screen.getByText('重试'));

    expect(screen.getByText('恢复后的内容')).toBeTruthy();
    expect(screen.queryByText('这个模块遇到了问题')).toBeNull();
  });

  it('chunk 加载失败要提示重新加载，而不是误导性的"重试"', () => {
    vi.spyOn(console, 'error').mockImplementation(() => {});
    // 返回类型必须显式标注：只 throw 的函数会被推断成 void，tsc 报「不能作为 JSX 组件」
    function ChunkBomb(): JSX.Element {
      throw new Error('Failed to fetch dynamically imported module: /assets/ParallelView-x.js');
    }
    render(
      <ErrorBoundary label="parallel">
        <ChunkBomb />
      </ErrorBoundary>,
    );
    expect(screen.getByText('页面资源已更新，需要重新加载')).toBeTruthy();
    expect(screen.getByText('重新加载')).toBeTruthy();
    // 资源更新场景不给「重试」：同一个 URL 的模块缓存已是失败态，重试必然再失败
    expect(screen.queryByText('重试')).toBeNull();
  });
});
