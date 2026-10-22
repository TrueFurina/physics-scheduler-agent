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
  | 'dashboard'
  | 'master';

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
  /** 其中"还没排过时间"的单元数（大纲新增 / 清单里带来的）。>0 说明这门课有从零排的部分 */
  pending_units?: number;
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
  /** 灰箱补全：信息不全时系统替教秘做的默认决定（自动分配教师/默认星期/节次/周次） */
  assumptions?: string[];
}

/** 全校一键排课（逐班·按天，组级）的单班结果 */
export interface SchoolClassResult {
  class_name: string;
  success: boolean;
  status: string;
  weekdays: string[];
  scheduled: number;
  total_units: number;
  error?: string | null;
  days?: Array<{ day: string; units: number; scheduled: number; status: string }>;
}

export interface SchoolPlanResult {
  success: boolean;
  classes_total: number;
  classes_ok: number;
  classes_failed: Array<{ class_name: string; error?: string | null }>;
  scheduled: number;
  units: number;
  elapsed_s: number;
  per_class: SchoolClassResult[];
  schedule: ParallelSession[];
  note?: string;
}

/** `/data-audit` 的单条冲突。 */
export interface AuditConflict {
  week: number;
  weekday: string;
  time_slot: string;
  place: string;
  detail: string[];
  /** 冲突分级（2026-10-22）：core=必须调整，不可强制通过。 */
  severity?: 'core';
  /** 冲突类型：room 场地 / teacher 教师 / group 同一班级同一组。 */
  kind?: 'room' | 'teacher' | 'group';
  /** 人类可读的类型名，如「场地被占」。 */
  label?: string;
  /** 该怎么改，如「换一间教室即可解决」。 */
  fix_hint?: string;
  /** 该处整组冲突是否属**跨课程撞车**（同一班同一组在两门课程的安排撞车）。 */
  cross_course?: boolean;
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
  /** 同一班级同一组在同一时段被排了两次 —— 此前完全没查（2026-10-22 补上）。 */
  group_conflict_count?: number;
  /** 三类核心冲突合计。 */
  core_conflict_count?: number;
  /** 同班级**不同组**同一时段的条数：正常的分批教学，不是冲突，但要把口径说出来。 */
  parallel_group_count?: number;
  parallel_note?: string;
  /** 整组冲突中属**跨课程撞车**的处数（是整组的子集，不重复计入 core）。 */
  cross_course_conflict_count?: number;
  /** 跨课程口径说明：0 处时也要显示 —— 要说清是"没出现"还是"没查"（主数据未初始化）。 */
  cross_course_note?: string;
  /** 课程归属的可信度：课程由项目名反推，反推不出来会回落默认课。 */
  course_coverage?: {
    total_entries: number;
    /** 项目能在主数据里查到、课程是**查得**而非回落默认的条数。 */
    project_in_master: number;
    distinct_courses: number;
    explicit_course: number;
    fallback_default: number;
    master_ready: boolean;
  };
  /** 没填场地的记录数：这些条不参与教室冲突判定，属于数据缺口而非冲突。 */
  missing_room_count?: number;
  missing_room_samples?: string[];
  /** 时段列填了无法识别的内容而被剔除的行数：该教室当天按整日占用处理。 */
  unparsed_slot_count?: number;
  unparsed_slot_samples?: string[];
  room_conflicts?: AuditConflict[];
  teacher_conflicts?: AuditConflict[];
  group_conflicts?: AuditConflict[];
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

/** `/master/overview` 返回：主数据五张实体表（导师一号行动项"重构数据库"）。 */
export interface MasterOverview {
  initialized: boolean;
  courses: Array<{ name: string; experiments: string[] }>;
  experiments: Array<{
    name: string; course: string; teachers: string[]; rooms: string[]; classes: string[];
    /** 绪论课/考试类登记为实体但**不参与排课**（R-48 口径，项目名派生、不可覆盖）。 */
    schedulable?: boolean;
  }>;
  teachers: Array<{ name: string; projects: string[] }>;
  rooms: Array<{ name: string; capacity: number | null }>;
  classes: Array<{
    name: string; groups: string[];
    /** 教务给定的「课程总时间范围」：主要星期 / 节次 / 周次 */
    weekday: string; time_slots: string[]; week_min: number | null; week_max: number | null;
  }>;
}
