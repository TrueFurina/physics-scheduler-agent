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
  | 'master'
  | 'grid';

/** `/schedule-grid` 的一个时段（行）：机器值 + 人话 + 真实时钟区间。 */
export interface GridSlotMeta {
  slot: string;
  label: string;
  clock: string;
}

/** `/schedule-grid` 单元格里的一条安排。 */
export interface GridCellEntry {
  class_name: string;
  group: string;
  experiment: string;
  teacher: string;
  /** 空串＝没填场地（前端标「未填场地」，不静默丢） */
  room: string;
  /** 主标签（展示用） */
  time_slot: string;
  /** 原表时段写法（跨节次时回显它，如 14:00-17:00） */
  time_raw: string;
  /** 真实占住的所有节次（R-40：一条记录可占多格） */
  blocked: string[];
  missing_room: boolean;
  /** 参与的核心冲突类型（来自 scan_table）：room/teacher/group */
  conflicts: string[];
}

/** `/schedule-grid` 的一周网格全景。 */
export interface ScheduleGrid {
  week: number;
  week_min: number;
  week_max: number;
  weekdays: string[];
  slots: GridSlotMeta[];
  /** key = `星期|时段`（如 `周一|567`），只有非空格 */
  cells: Record<string, GridCellEntry[]>;
  filters: { class_name: string; room: string };
  /** 筛选项从全表取：被筛选吃掉下拉选项是经典的"筛回去"陷阱 */
  class_options: string[];
  room_options: string[];
  totals: {
    entries: number;
    nonempty_cells: number;
    missing_room: number;
    conflict_entries: number;
    unplaced: number;
  };
  /** 全周全表口径，不受班级/场地筛选影响 */
  conflict_counts: { room: number; teacher: number; group: number };
  conflict_scope: string;
  notes: string[];
}

/** 导入向导：主数据台账预览的一张表（`/master/import-preview`，只读）。 */
export interface ImportPreviewTable {
  found: boolean;
  sheet?: string;
  header_row?: number;
  rows: number;
  blank_rows?: number;
  bad_values?: number;
  /** 表头认不出名称列 → 整页跳过（报告里说清缺什么） */
  skipped?: boolean;
  recognized?: string[];
  missing?: string[];
  /** 字段 ← 表头列名（预览与写库同一份匹配代码，前端不自己映射） */
  columns?: Record<string, string>;
  /** 表头里没被任何字段认出的列 —— 明示出来，否则教秘不知道它们被丢了 */
  unrecognized?: string[];
  /** 被判为统计列（数值）而忽略的列 */
  stat_dropped?: string[];
  /** 前 3 行样本（与写库同一段行转换代码） */
  samples?: Array<Record<string, unknown>>;
}

/** `/master/import-preview` 返回：五张表的只读解析报告。 */
export interface ImportWorkbookPreview {
  /** 文件带不带可导回标记（自己导出的台账带；外校格式不带） */
  tagged: boolean;
  sheets: string[];
  tables: Record<string, ImportPreviewTable>;
  warnings: string[];
  ignored_sheets: string[];
}

/** `/master/tasks-preview` 返回：教学任务清单的只读解析报告。 */
export interface ImportTasksPreview {
  sheet: string;
  header_row: number;
  /** 字段 ← 表头列名 */
  columns: Record<string, string>;
  /** 没被任何字段认出的列 */
  unrecognized_columns: string[];
  recognized_columns: string[];
  tasks: number;
  blank_or_incomplete_rows: number;
  samples: Array<Record<string, unknown>>;
}

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
  /** 全校一键（solve-all）的行自带班级名；单班求解不带，由请求的 class_name 补。
   *  落地接口按行取班级，缺省会报 400（不猜、不静默丢行）。 */
  class_name?: string;
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

// ---------------------------------------------------------------------------
// 方案落地（预览 → 采纳 → 撤销）
//
// 把求解结果写回**真实安排表**。三步的理由：这改的是真实排课数据，所以
// ① 先差异预览（不写入）② 教秘确认后才写 ③ 写完可撤销（恢复落地前快照）。
// 后端 `/parallel/land/preview|land|land/undo|land/status`。
// ---------------------------------------------------------------------------

/** 差异账里的一行安排（预览/采纳共用同一结构）。 */
export interface LandPlanRow {
  class_name?: string;
  project: string;
  group: string;
  week: number;
  weekday: string;
  time_slot: string;
  room: string;
  teacher: string;
}

/** `/parallel/land/preview` 与 `/parallel/land` 返回的差异账。 */
export interface LandPlanDiff {
  success?: boolean;
  plan_units: number;
  plan_rows: number;
  before_entries: number;
  after_entries: number;
  /** 方案里有、安排表里没有的单元 */
  added: Array<{ unit: string; rows: LandPlanRow[] }>;
  /** 时间/场地/教师被改写的单元（long_session=true 表示是跨时段长实验被截短） */
  changed: Array<{ unit: string; old: LandPlanRow[]; new: LandPlanRow[]; long_session?: boolean }>;
  unchanged_units: number;
  /** 方案没覆盖的可排单元：保留原行（删了就是静默丢课） */
  uncovered: Array<{ unit: string; rows: LandPlanRow[] }>;
  /** 原表里同周同天地占多个时段的长实验，方案每单元只排一个 ⇒ 会被截短 */
  long_sessions: Array<{ unit: string; old_slots: string[]; plan_slots: string[] }>;
  /** 绪论课/考试类：原样保留、不参与替换 */
  untouched_nonsched: number;
  warnings: string[];
  has_changes: boolean;
  /** 仅采纳响应带 */
  landed_at?: string;
}

/** `/parallel/land/status`：当前落地状态（供界面显示撤销入口）。 */
export interface LandStatus {
  landed: boolean;
  landed_at?: string;
  plan_rows?: number;
  after_entries?: number;
  note?: string;
}

/** `/parallel/land/undo`：撤销结果。 */
export interface LandUndoResult {
  success: boolean;
  landed_at: string;
  restored_entries: number;
  /** 落地之后又生效的调课条数 —— 会随快照一起回滚，必须如实报出 */
  rolled_back_moves: number;
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
