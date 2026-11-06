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

/** 主数据写入端点的落盘结果（R87）。字段可选：老后端没有也不炸。 */
export interface MasterPersistResult {
  persisted?: boolean;
  warning?: string;
}

/**
 * 落盘失败 → 主数据管理页的警告横幅文案；成功路径返回空串。
 *
 * 判据必须是 `=== false`：`undefined`（老后端 / 读接口 / mock）不是失败，
 * 否则每次正常保存都会弹「狼来了」。文案以 ⚠️ 开头，避免被渲染成中性提示。
 * 后端刻意不回滚内存（内存态仍是权威），所以这里也不能叫「保存失败」。
 */
export function masterPersistWarningOf(data?: MasterPersistResult): string {
  if (data?.persisted === false) {
    const raw = (data.warning || '').trim();
    if (!raw) return '⚠️ 本次修改未能写入磁盘，服务重启后会丢失，请尽快重试';
    return raw.startsWith('⚠️') ? raw : `⚠️ ${raw}`;
  }
  return '';
}

/**
 * 主数据总览的恢复结果（R88）。字段可选：老后端 / 正常响应没有也不炸。
 *
 * `restored: false` 说的是「磁盘上有、但没能用上」——落盘损坏 / 恢复抛异常 /
 * 空主数据脏数据。这三种都会丢掉教秘上次保存的修改并回退到反推基线，
 * 却只 print 到服务端日志。与 `persisted: false`（没存住）是两件事，
 * 所以单独一个纯函数、单独一条横幅，不许混成一个状态。
 */
export interface MasterRestoreResult {
  restored?: boolean;
  warning?: string;
}

/**
 * 恢复降级 → 主数据管理页的警告横幅文案；正常响应返回空串。
 *
 * 判据必须是 `=== false`：`undefined`（老后端 / 恢复成功 / 首次启动无落盘）
 * 不是故障，否则每次进页面都弹「狼来了」（R86/R87 同款口径）。
 */
export function masterRestoreWarningOf(data?: MasterRestoreResult): string {
  if (data?.restored === false) {
    const raw = (data.warning || '').trim();
    if (!raw) return '⚠️ 服务重启后主数据未能恢复，已回退到安排表基线，请核对是否完整';
    return raw.startsWith('⚠️') ? raw : `⚠️ ${raw}`;
  }
  return '';
}
