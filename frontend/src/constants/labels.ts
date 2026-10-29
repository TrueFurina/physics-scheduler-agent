/** 全局展示用文案映射：星期与时间段的「机器值 → 人话」。 */

export const weekdayLabels: Record<string, string> = {
  '周一': '周一', '周二': '周二', '周三': '周三',
  '周四': '周四', '周五': '周五', '周六': '周六', '周日': '周日',
};

export const timeSlotLabels: Record<string, string> = {
  // R35：`MORNING` 一直缺着。后端 `time_model.CLOCK` 里它是标准节次之一
  // （09:00-11:15），真实数据 433 条里有 4 条上午课。
  //
  // 各视图都写 `timeSlotLabels[x] || x`，所以缺映射不会崩 ——
  // 界面直接显示 `MORNING` 这串英文。**兜底掩盖了映射缺失。**
  //
  // 这份映射必须与后端 CLOCK 的键保持一致，由
  // `constants/__tests__/labels.test.ts` 直接读后端源码比对。
  'MORNING': '09:00-11:15',
  '567': '13:30-15:45',
  '678': '15:45-18:00',
  '91011': '18:30-20:45',
  '78': '15:50-18:05',
};

/** 沙盘网格里按顺序渲染的工作日。 */
export const WEEKDAYS = ['周一', '周二', '周三', '周四', '周五'] as const;

/**
 * 调课沙盘要渲染的**全部七天**（R36）。
 *
 * 后端 `WEEKDAYS_ALL` 已改为覆盖周末：真实数据 433 条里 71 条在周末，
 * 而沙盘此前只画 5 天 —— 那些课"既不可用也不在排除列表"，从界面上消失了。
 *
 * `WEEKDAYS` 保留给并行排课沙盘（那里只排工作日三时段，属既定口径）。
 */
export const WEEKDAYS_ALL = [
  '周一', '周二', '周三', '周四', '周五', '周六', '周日',
] as const;

/** 沙盘网格里按顺序渲染的时段（与 timeSlotLabels 的键保持一致）。 */
export const GRID_TIME_SLOTS = ['567', '678', '91011'] as const;
