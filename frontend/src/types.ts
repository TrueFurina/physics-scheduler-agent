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
  affected_teachers: string[];
  affected_students: string[];
  conflict_slots: any[];
}

/** `/parallel/overview` 里的一门课。 */
export interface ParallelCourse {
  class_name: string;
  weekday: string;
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
  weeks_used: number;
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
  room_conflicts?: AuditConflict[];
  teacher_conflicts?: AuditConflict[];
}

/** `/stats` 返回：利用率与工作量统计。 */
export interface StatsData {
  n_weeks: number;
  avg_room_utilization: number;
  avg_teacher_load: number;
  teacher_load_balance: number;
  busiest_room?: { room: string; utilization: number };
  top_rooms: Array<{ room: string; sessions: number; utilization: number }>;
  top_teachers: Array<{ teacher: string; sessions: number; experiments: number }>;
}
