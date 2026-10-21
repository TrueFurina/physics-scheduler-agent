/**
 * 与 UI 无关的通用异步小工具。
 *
 * 单独成模块的目的只有一个：**能被单元测试直接 import**。
 * 这两个函数都承载着线上踩出来的约束（见各自注释），如果只能靠 Playwright
 * 端到端探针间接验证，改动它们就没有快速反馈。
 */

/**
 * 有界重试：线上由沙箱托管，进程被回收后首个请求可能因冷启动而失败。
 * 原先首屏初始化只请求一次、无重试，一旦那一次失败就永久空白、只能手动刷新页面。
 * 这里按给定退避序列重试（默认 3 次、最长约 2.3s）；全部失败则抛出**最后一个**错误。
 *
 * 注意：`delaysMs` 的第一个值如果是 0，表示"立刻来一次"，不做等待。
 */
export async function withRetry<T>(
  fn: () => Promise<T>,
  delaysMs: number[] = [0, 700, 1600],
): Promise<T> {
  let lastErr: unknown;
  for (const delay of delaysMs) {
    if (delay > 0) await new Promise((resolve) => setTimeout(resolve, delay));
    try {
      return await fn();
    } catch (err) {
      lastErr = err;
    }
  }
  throw lastErr;
}

/**
 * 把非首屏必需的工作推到浏览器空闲时段。
 *
 * 为什么不用 `setTimeout(fn, 0)`：那只是挪到下一轮任务，仍会和首屏渲染抢主线程；
 * `requestIdleCallback` 才是真正"等渲染停下来"的语义。Safari 长期不支持它，
 * 故保留定时器兜底（宁可晚一点，也不能不执行）。
 */
export function whenIdle(fn: () => void): void {
  const ric = (globalThis as any).requestIdleCallback as
    | ((cb: () => void, opts?: { timeout: number }) => void)
    | undefined;
  if (typeof ric === 'function') ric(fn, { timeout: 2500 });
  else globalThis.setTimeout(fn, 1200);
}
