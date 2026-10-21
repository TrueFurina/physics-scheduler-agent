/**
 * 跨视图共享的领域类型。
 *
 * 后端这几个接口返回的是自由 dict（没有 pydantic 响应模型），所以这里按
 * 「前端实际读取到的字段」声明 —— 只写用得到的，避免出现"类型说有、实际没有"的假安全。
 * 可选字段一律标 `?`，视图里继续按原逻辑做防御。
 */

export type View =
  | 'identity'
  | 'courses'
  | 'reschedule'
  | 'admin'
  | 'course_select'
  | 'tutor'
  | 'parallel'
  | 'dashboard';

/** `/impact-analysis` 返回：调课后的冲突影响面。 */
export interface ImpactResult {
  conflict_count: number;
  affected_classes: string[];
  affected_rooms: string[];
  /** 占着那些教室的教师（真实姓名）。不是"被调课教师"——那个人在 self_busy_slots 里。 */
  affected_teachers: string[];
  /** 学生课表里挡路的课程名（后端能取到就填课程名，取不到才退回占位文案）。 */
  affected_students: string[];
  /** 调课教师**本人**在该时段已有课（自身冲突），语义上不是"别人受影响"，故单列。 */
  self_busy_count?: number;
  self_busy_slots?: Array<{
    weekday: string;
    time_slot: string;
    class_name: string;
    experiment: string;
  }>;
  conflict_slots: any[];
}

/** `/chat`（fast=false）返回的 AI 候选方案。沙盘与它并行加载，互不阻塞。 */
export interface AiPlan {
  weekday: string;
  time_slot: string;
  room: string;
  reason?: string;
}

/**
 * 多轮追问的槽位（B2）。
 *
 * 后端**不存会话**，只把"这一轮已确认的槽位"随响应回传；下一轮原样带回，
 * 服务端再做无状态合并。所以它必须留在前端 —— 进程被回收也不会丢上下文。
 */
export interface NlSlotContext {
  teacher_name?: string | null;
  course_name?: string | null;
  class_name?: string | null;
  target_week?: number | null;
  source_week?: number | null;
}

/** `/parallel/overview` 里的一门课。 */
export interface ParallelCourse {
  class_name: string;
  weekday: string;
  /** 星期分布（含周六/周日）——「常上周一」不能只看周中，真实表 16% 的课在周末 */
  weekday_mix?: Record<string, number>;
  weekend_sessions?: number;
  projects: Array<{ project: string; groups: string[] }>;
  total_units: number;
}

/** `/parallel/solve` 的一个排课单元。 */
export interface ParallelSession {
  week: number;
  weekday?: string;
  time_slot: string;
  room: string;
  group: string;
  project: string;
  teacher: string;
}

/** `/parallel/solve` 返回结果。 */
export interface ParallelSolveResult {
  status: string;
  weekday: string;
  scheduled: number;
  total_units: number;
  /** 实际用到几周（原先是"窗口宽度"，界面会写"用 10 周"而沙盘只有 6 周有格子） */
  weeks_used: number;
  /** 最多允许排到第几周（窗口宽度） */
  weeks_window?: number;
  by_week?: Record<string, number>;
  schedule?: ParallelSession[];
}

/** `/data-audit` 的单条冲突。 */
export interface AuditConflict {
  week: number;
  weekday: string;
  time_slot: string;
  place: string;
  detail: string[];
}

/** `/data-audit` 返回：安排表体检报告。 */
export interface AuditData {
  healthy: boolean;
  total_entries: number;
  teacher_count: number;
  room_count: number;
  week_min: number;
  week_max: number;
  room_conflict_count: number;
  teacher_conflict_count: number;
  /** 没填场地的记录数：这些条不参与教室冲突判定，属于数据缺口而非冲突。 */
  missing_room_count?: number;
  missing_room_samples?: string[];
  /** 时段列填了无法识别的内容而被剔除的行数：该教室当天按整日占用处理。 */
  unparsed_slot_count?: number;
  unparsed_slot_samples?: string[];
  room_conflicts?: AuditConflict[];
  teacher_conflicts?: AuditConflict[];
}

/** `/stats` 返回：利用率与工作量统计。 */
export interface StatsData {
  n_weeks: number;
  /** 利用率的分母口径（如"16 周 × 7 天 × 4 时段 = 448 格/间"）。
   *  没有它，教秘无从判断这个百分比对自己有没有意义。 */
  capacity_basis?: string;
  schedulable_days?: number;
  schedulable_slots?: number;
  avg_room_utilization: number;
  avg_teacher_load: number;
  teacher_load_balance: number;
  busiest_room?: { room: string; utilization: number };
  top_rooms: Array<{ room: string; sessions: number; utilization: number }>;
  top_teachers: Array<{ teacher: string; sessions: number; experiments: number }>;
}
