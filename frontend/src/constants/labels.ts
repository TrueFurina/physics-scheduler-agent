/** 全局展示用文案映射：星期与时间段的「机器值 → 人话」。 */

export const weekdayLabels: Record<string, string> = {
  '周一': '周一', '周二': '周二', '周三': '周三',
  '周四': '周四', '周五': '周五', '周六': '周六', '周日': '周日',
};

export const timeSlotLabels: Record<string, string> = {
  '567': '13:30-15:45',
  '678': '15:45-18:00',
  '91011': '18:30-20:45',
  '78': '15:50-18:05',
};

/** 沙盘网格里按顺序渲染的工作日。 */
export const WEEKDAYS = ['周一', '周二', '周三', '周四', '周五'] as const;

/** 沙盘网格里按顺序渲染的时段（与 timeSlotLabels 的键保持一致）。 */
export const GRID_TIME_SLOTS = ['567', '678', '91011'] as const;
