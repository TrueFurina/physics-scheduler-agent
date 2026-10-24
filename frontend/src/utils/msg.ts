/**
 * 顶部提示横幅的清理判据。
 *
 * 为什么单独一个模块：这条判据承载一个线上踩出来的竞态 ——
 * 首屏后台加载（loadTeacherList）成功时会清横幅，而它的成功可能**晚于**
 * 用户的一次点击（冷启动沙箱上 /status 能拖数秒）。无条件清空会把用户
 * 刚看到的错误提示静默吞掉：提示出现又消失 = 没提示（R-30「点了没反应」
 * 同族，探针 probe_faults 的 solve 场景抓到过）。
 */

/** 首屏加载失败时横幅的前缀（App.tsx loadTeacherList 的 catch 分支）。 */
export const LOAD_FAILED_PREFIX = '加载失败';

/**
 * 首屏加载成功后，只清掉**自己留下的**「加载失败」横幅；
 * 其他任何文案（用户点击触发的错误、其他流程的失败）原样保留。
 */
export function clearStaleLoadBanner(prev: string): string {
  return prev.startsWith(LOAD_FAILED_PREFIX) ? '' : prev;
}
