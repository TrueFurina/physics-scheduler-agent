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

/** `/load-default` 的结构化降级信号（R86）。字段可选：老后端没有也不炸。 */
export interface LoadDefaultDegraded {
  degraded?: boolean;
  warnings?: string[];
}

/**
 * 降级信号 → 顶部横幅文案；无降级时返回空串。
 *
 * 为什么必须在这里转成 `⚠️` 开头：App 的 msgTone 按文案首字符分档，
 * 不以 ⚠️ 开头会被渲染成灰色中性/绿色成功，警告等于没说。
 *
 * 为什么单独成纯函数：App.tsx 顶着 1500 行体量守卫，且这段判据需要
 * 独立单测 —— 只写在组件里就没人能证明「degraded 缺失时保持安静」。
 */
export function loadWarningOf(data?: LoadDefaultDegraded): string {
  if (data?.degraded !== true) return '';
  const list = data.warnings ?? [];
  return list.length
    ? `⚠️ 排课表已加载，但存在异常：${list.join('；')}`
    : '⚠️ 排课表已加载，但部分已审批变更可能未生效，请核对课表';
}
