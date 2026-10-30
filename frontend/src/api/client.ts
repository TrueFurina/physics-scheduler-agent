import axios from 'axios';
import type { AiPlan, LandHistory, LandPlanDiff, LandStatus, LandUndoResult, MasterOverview, NlSlotContext, ScheduleGrid, ImportWorkbookPreview, ImportTasksPreview } from '../types';

const API_BASE = '/api';

// 全局超时。**必须有值**：axios 默认 `timeout: 0` 表示"永不超时"，而
// `utils/apiError.ts` 里那条 `ECONNABORTED / timeout of …` 的中文分支正是按
// "60s 会超时"写的（文档与探针注释也这么写）。没有这一行时那个分支在真实浏览器里
// **基本不可达** —— 网关挂着时用户看到的是无限转圈，而不是"请求超时，请稍后重试"。
// 与 R-25 同族：一段看起来在防护的代码，实际从不生效。
//
// 取值 60s 的依据：本地/线上实测最慢的合法请求是全校园一键排课（满载 16.6s）与
// `/chat` 的 LLM 解析（7.6s）；60s 留出 3 倍余量，正常演示永远碰不到，
// 只兜住"服务被回收、连接挂死"这类真正该给用户一句话的情况。
export const API_TIMEOUT_MS = 60_000;

export const api = axios.create({
  baseURL: API_BASE,
  timeout: API_TIMEOUT_MS,
  headers: { 'Content-Type': 'application/json' },
});

// ---------------------------------------------------------------------------
// 管理员会话令牌
//
// 由服务端 `/admin-login` 签发（HMAC 自签，默认 8 小时）。
// 只放内存 + sessionStorage：刷新能续上，关标签页即丢，不长期驻留磁盘。
//
// 为什么要这一层：修复前管理员口令是**前端**比对的（明文写在 bundle 里），
// 且校验通过后不发任何凭据，后端写接口对匿名调用完全开放。
// 现在写接口要求 `Authorization: Bearer <token>`，由服务端校验。
// ---------------------------------------------------------------------------
const ADMIN_TOKEN_KEY = 'ps_admin_token';

let adminToken: string | null =
  typeof sessionStorage === 'undefined' ? null : sessionStorage.getItem(ADMIN_TOKEN_KEY);

export function setAdminToken(token: string | null) {
  adminToken = token;
  if (typeof sessionStorage === 'undefined') return;
  if (token) sessionStorage.setItem(ADMIN_TOKEN_KEY, token);
  else sessionStorage.removeItem(ADMIN_TOKEN_KEY);
}

export function getAdminToken(): string | null {
  return adminToken;
}

api.interceptors.request.use((cfg) => {
  if (!adminToken) return cfg;
  // 两个头都发，任一个验签通过即放行。
  //
  // 为什么不能只发 Authorization：实测发现部署环境的边缘代理会**注入**一个自己的
  // `Authorization` 头（本机 curl -v 确认自己没发，服务端却收到），从而把我们的令牌
  // 挤掉，表现为"刚登录成功，下一个请求就 401"。自定义头 `X-Admin-Token` 代理不碰，
  // 是可靠的那一路；Authorization 保留是为兼容标准的 API 调用方。
  cfg.headers.Authorization = `Bearer ${adminToken}`;
  cfg.headers['X-Admin-Token'] = adminToken;
  return cfg;
});

// 会话失效时清掉本地令牌，避免拿着废令牌反复重试。
api.interceptors.response.use(
  (res) => res,
  (err) => {
    if (err?.response?.status === 401) setAdminToken(null);
    return Promise.reject(err);
  },
);

// ---- 类型定义 ----
export interface ScheduleStatus {
  loaded: boolean;
  total_entries: number;
  rooms: string[];
  teachers: string[];
  week_range: number[];
}

export interface AvailableSlot {
  weekday: string;
  time_slot: string;
  room: string;
  date?: string;
}

export interface ChatResponse {
  success: boolean;
  message: string;
  data: {
    target_week?: number;
    available_slots?: AvailableSlot[];
    excluded_slots?: ExcludedSlot[];
    /** 仅 fast=false 时后端会填（LLM 生成；LLM 不可用时后端规则降级给 3 个） */
    ai_plans?: AiPlan[];
    total?: number;
    need_info?: boolean;
    followup_questions?: string[];
    /** 本轮已确认的槽位；下一轮请求原样带回，后端做无状态合并（多轮追问） */
    context?: NlSlotContext;
    /**
     * 本轮识别出的课程名；识别不出时为 null。
     *
     * R33：这两个字段从 R21 起后端就在返回（R31 才补上 API 层透传），
     * 但前端类型里一直没有 —— 于是"要读它们"这件事在类型层面就是不可能的，
     * 代码里自然也就没人读。`grep course_unrecognized frontend/src` 曾为 0。
     */
    course_name?: string | null;
    /** true = 没认出是哪门课/哪个实验，需要教秘在沙盘页补选 */
    course_unrecognized?: boolean;
  };
}

export interface UploadResponse {
  success: boolean;
  message: string;
  total_entries: number;
  rooms: string[];
  teachers: string[];
  week_range: number[];
}

export interface Teacher {
  name: string;
  title: string;
}

export interface TeacherCourse {
  course_name: string;
  class_name: string;
  original_time: string;
  room: string;
  week_range: string;
  total_slots: number;
  adjustable: boolean;
  adjustable_reason: string;
  // ---- R50：真实课程归属（后端 R50 起返回；旧后端没有这几个字段，故全部可选）----
  // `course_name` 一直是**实验项目名**（调课申请按它匹配），真实课程名在这里。
  // 同一项目跨两门课时，`course` 用「、」并列，`course_ambiguous` 为 true。
  course?: string;
  courses?: string[];
  course_ambiguous?: boolean;
  // 真实周次列表（`week_range` 是人读的文案，连续才写成区间）
  weeks?: number[];
}

export interface ExcludedSlot {
  weekday: string;
  time_slot: string;
  room: string;
  date?: string;
  reason_type: string;
  reason: string;
}

export interface RescheduleRequest {
  id: number;
  teacher: string;
  course_name: string;
  class_name: string;
  original_time: string;
  target_week: number;
  reason: string;
  status: string;  // pending / approved / rejected
  admin_comment: string;
  created_at: string;
}

// ---- API 函数 ----
export const rescheduleApi = {
  // 获取状态
  getStatus: () => api.get<ScheduleStatus>('/reschedule/status'),

  // 加载默认数据
  loadDefault: () => api.post<{ success: boolean; message: string; total_entries: number }>('/reschedule/load-default'),

  // 上传文件
  upload: (file: File) => {
    const formData = new FormData();
    formData.append('file', file);
    return api.post<UploadResponse>('/reschedule/upload', formData, {
      headers: { 'Content-Type': 'multipart/form-data' },
    });
  },

  // 教师列表（F1 身份选择）
  getTeachers: () => api.get<{ success: boolean; teachers: Teacher[]; total: number }>('/reschedule/teachers'),

  // 教师课程列表（F2）
  getTeacherCourses: (teacherName: string) =>
    api.get<{ success: boolean; teacher: string; courses: TeacherCourse[]; total: number }>(`/reschedule/teachers/${encodeURIComponent(teacherName)}/courses`),

  // 调课咨询（fast=true 时跳过 LLM 候选方案，秒回沙盘数据）
  // `context` 是上一轮响应回传的槽位（多轮追问），后端据此补全本轮没说全的信息。
  chat: (message: string, fast: boolean = false, context?: NlSlotContext | null) =>
    api.post<ChatResponse>('/reschedule/chat', context ? { message, fast, context } : { message, fast }),

  // 提交调课申请（F6）
  createRequest: (data: {
    teacher: string; course_name: string; class_name: string;
    original_time: string; target_week: number; reason: string;
  }) => api.post<{ success: boolean; message: string; request: RescheduleRequest }>('/reschedule/requests', data),

  // 申请列表（F6）
  listRequests: (status?: string) =>
    api.get<{ success: boolean; requests: RescheduleRequest[]; total: number }>('/reschedule/requests', { params: status ? { status } : {} }),

  // 管理员审批（F6）
  approveRequest: (id: number, approve: boolean, comment: string) =>
    api.post<{ success: boolean; message: string; request: RescheduleRequest }>(`/reschedule/requests/${id}/approve`, { approve, comment }),

  // 把一条已批准的调课落到排课数据上（审批台闭环）
  //
  // 审批台的主数据在云表，云表只存状态；排课数据在服务端进程内存里。
  // 不做这一步，教秘点完"同意"之后课表一个字节都不会变 —— 沙盘、我的课程、
  // 数据体检全都看不见这次调课，而这套能移记录、能检冲突、能持久化重放的逻辑
  // 原本只挂在 `/requests/{id}/approve` 上，前端一行都没调用。
  // `key` 用云端工单 id 做幂等键：重复点不会连着撬动更早的周次。
  applyMove: (data: {
    teacher: string; course_name: string; class_name: string;
    original_time: string; target_week: number; source_week?: number; key: string;
  }) => api.post<{
    success: boolean; message: string;
    data: { applied: number; skipped: number; details: string[] };
  }>('/reschedule/apply-move', data),

  // 实验规范查询（端点保留、面向排课人员/教师）
  experimentTutor: (question: string) =>
    api.post<{ success: boolean; message: string; reply: string }>('/reschedule/experiment-tutor', { question }),

  // 排课自动生成（P1）
  //
  // 方案条目现在带 `week`：不带周次的课表教秘不知道该用到哪一周，也无法跟
  // 已有安排表比对（R-37）。`capacity_checked` 是"没校验就别声称校验过"的显式
  // 交代，界面上直接由 message 呈现。
  autoSchedule: () =>
    api.post<{ success: boolean; message: string; data: {
      total_sections: number; total_sections_all: number; intro_excluded: number;
      solved: number; truncated?: boolean; coverage?: string; chunks?: any[];
      elapsed_s?: number | null;
      weeks: number[]; conflicts: any[]; schedule: any[];
      capacity_checked: boolean; real_entries: number;
    } }>('/reschedule/auto-schedule'),

  // 自动排课初稿导出（总表 + 每班一页）：教秘拿到 171 行文本无法落地，
  // 这一步把"排完了"变成"能发班级群"的 Excel。
  autoScheduleExport: (schedule: any[]) =>
    api.post('/reschedule/auto-schedule/export', { schedule }, { responseType: 'blob' }),

  // 管理员登录（审批台）：口令由服务端校验，成功返回会话令牌
  // 演示模式：审批台走本机工单（DEMO_MODE=1 时后端返回 true）
  demoMode: () =>
    api.get<{ success: boolean; demo_mode: boolean }>('/reschedule/demo-mode'),

  localRequests: (token: string) =>
    api.get<{ success: boolean; requests: any[] }>('/reschedule/requests', {
      headers: { Authorization: `Bearer ${token}` },
    }),

  approveLocalRequest: (id: number, approve: boolean, comment: string, token: string) =>
    api.post<{ success: boolean; message: string; request: any }>(
      `/reschedule/requests/${id}/approve`, { approve, comment },
      { headers: { Authorization: `Bearer ${token}` } },
    ),

  adminLogin: (username: string, password: string) =>
    api.post<{
      success: boolean; message: string; role: string;
      token: string; expires_in: number; expires_at: number;
    }>('/reschedule/admin-login', { username, password }),

  // 冲突影响分析（差异化功能）
  impactAnalysis: (message: string) =>
    api.post<{ success: boolean; message: string; data: {
      conflict_count: number;
      affected_classes: string[];
      affected_rooms: string[];
      affected_teachers: string[];
      conflict_slots: any[];
    } }>('/reschedule/impact-analysis', { message }),

  // 并行排课：课程总览（实验项目×分组）
  parallelOverview: () =>
    api.get<{ success: boolean; data: { courses: Array<{
      class_name: string;
      weekday: string;
      projects: Array<{ project: string; groups: string[] }>;
      total_units: number;
    }> } }>('/reschedule/parallel/overview'),

  // 并行排课：一键求解（CP-SAT）
  parallelSolve: (className: string, weekday?: string) =>
    api.post<{ success: boolean; error?: string; status?: string; weekday?: string;
      total_units?: number; scheduled?: number; weeks_used?: number;
      schedule?: Array<{ project: string; group: string; week: number; weekday: string;
        time_slot: string; room: string; teacher: string }>;
      by_week?: Record<string, number>; note?: string;
    }>('/reschedule/parallel/solve', { class_name: className, weekday: weekday ?? null }),

  // 并行排课：方案导出 Excel（blob 下载）
  // 全校一键：逐班求解（组级），班与班之间回填同星期的教室/教师占用
  parallelSolveAll: () =>
    api.post('/reschedule/parallel/solve-all'),

  // ---------------------------------------------------------------------------
  // 方案落地（预览 → 采纳 → 撤销）
  //
  // 把求解结果写回**真实安排表**（改的是真实排课数据，不是另存一份文件）：
  // ① preview 只读算差异，绝不写入；② commit 需管理员（与 /upload、/apply-move
  //    同一鉴权边界），且采纳时 diff 重新计算 —— 预览与采纳之间若有审批调课
  //    生效，按旧 diff 写入就是偷换；③ undo 恢复落地前快照，落地之后又生效的
  //    调课会一并回滚并在 rolled_back_moves 里如实计数。
  // `class_name` 只对单班方案需要（补班级名）；全校一键结果的行已自带。
  // ---------------------------------------------------------------------------
  landPreview: (schedule: unknown[], class_name?: string) =>
    api.post<LandPlanDiff>('/reschedule/parallel/land/preview',
      { schedule, class_name: class_name ?? null }),
  landCommit: (schedule: unknown[], class_name?: string, note?: string) =>
    api.post<LandPlanDiff>('/reschedule/parallel/land',
      { schedule, class_name: class_name ?? null, note: note ?? '' }),
  landUndo: () =>
    api.post<LandUndoResult>('/reschedule/parallel/land/undo'),
  landStatus: () =>
    api.get<LandStatus>('/reschedule/parallel/land/status'),

  // 落地历史（含已撤销，需管理员）。撤销改成"打标记不删行"之后，
  // "上周谁落过方案、后来又撤了没有"才答得上来（R55/R56）。
  landHistory: (limit = 20) =>
    api.get<LandHistory>('/reschedule/parallel/land/history', { params: { limit } }),

  // 周课表网格：把某一周的安排表重排成 星期×时段 全景（R-62）。
  // 读路径免鉴权（与 dataAudit/stats 同族）。班级/场地传空串＝不过滤。
  scheduleGrid: (week: number, class_name = '', room = '') =>
    api.get<{ success: boolean; data: ScheduleGrid }>(
      '/reschedule/schedule-grid',
      { params: { week, class_name, room } }),

  parallelExport: (className: string, schedule: Array<{ project: string; group: string; week: number; weekday: string; time_slot: string; room: string; teacher: string }>) =>
    api.post('/reschedule/parallel/export', { class_name: className, schedule }, { responseType: 'blob' }),

  // 调课申请记录导出 Excel（审批留档；status 为空 = 全部）
  exportRequests: (status = '') =>
    api.get('/reschedule/requests/export', { params: { status }, responseType: 'blob' }),

  // 教务两类标准课表（基于**现有安排表**，不是新排的方案）：
  // 班级课表 = 每班一页；场地课表 = 每间实验室一页。传空 = 全部。
  exportClassSchedule: (className?: string) =>
    api.get('/reschedule/export/class-schedule', {
      params: className ? { class_name: className } : {},
      responseType: 'blob',
    }),
  exportRoomSchedule: (room?: string) =>
    api.get('/reschedule/export/room-schedule', {
      params: room ? { room } : {},
      responseType: 'blob',
    }),
  // 基础数据台账：把安排表反推成 实验项目/教师/场地/班级 四张实体表
  // （教务第一号行动项"梳理实验课程属性、重构数据库"的起始材料）
  exportMasterData: () =>
    api.get('/reschedule/export/master-data', { responseType: 'blob' }),

  // 主数据五张实体表（课程/实验项目/教师/场地/班级）—— 导师一号行动项"重构数据库"。
  // 读免鉴权（纯实体清单，不含学生 PII）；写（增删改）挂 require_admin，凭 admin 会话令牌放行。
  masterOverview: () =>
    api.get<{ success: boolean; data: MasterOverview }>('/reschedule/master/overview'),
  masterInit: () =>
    api.post<{ success: boolean; data: MasterOverview }>('/reschedule/master/init'),
  masterUpsertCourse: (payload: { name: string }) =>
    api.post<{ success: boolean; data: MasterOverview }>('/reschedule/master/courses', payload),
  masterDeleteCourse: (name: string) =>
    api.delete<{ success: boolean; data: MasterOverview }>(`/reschedule/master/courses/${encodeURIComponent(name)}`),
  masterUpsertExperiment: (payload: { name: string; course?: string; teachers?: string[]; rooms?: string[]; classes?: string[] }) =>
    api.post<{ success: boolean; data: MasterOverview }>('/reschedule/master/experiments', payload),
  masterDeleteExperiment: (name: string) =>
    api.delete<{ success: boolean; data: MasterOverview }>(`/reschedule/master/experiments/${encodeURIComponent(name)}`),
  masterUpsertTeacher: (payload: { name: string; projects?: string[] }) =>
    api.post<{ success: boolean; data: MasterOverview }>('/reschedule/master/teachers', payload),
  masterDeleteTeacher: (name: string) =>
    api.delete<{ success: boolean; data: MasterOverview }>(`/reschedule/master/teachers/${encodeURIComponent(name)}`),
  masterUpsertRoom: (payload: { name: string; capacity?: number }) =>
    api.post<{ success: boolean; data: MasterOverview }>('/reschedule/master/rooms', payload),
  masterDeleteRoom: (name: string) =>
    api.delete<{ success: boolean; data: MasterOverview }>(`/reschedule/master/rooms/${encodeURIComponent(name)}`),
  masterUpsertClass: (payload: { name: string; groups?: string[]; weekday?: string; time_slots?: string[]; week_min?: number; week_max?: number }) =>
    api.post<{ success: boolean; data: MasterOverview }>('/reschedule/master/classes', payload),
  masterDeleteClass: (name: string) =>
    api.delete<{ success: boolean; data: MasterOverview }>(`/reschedule/master/classes/${encodeURIComponent(name)}`),

  // 可编辑主数据台账：导出后在 Excel 里批量维护，改完导回（导回只增补，不删实体）。
  // 与 exportMasterData 的区别：那份是统计台账（核对用），这份是实体台账（维护用）。
  exportMasterWorkbook: () =>
    api.get('/reschedule/export/master-workbook', { responseType: 'blob' }),
  masterImport: (file: File) => {
    const fd = new FormData();
    fd.append('file', file);
    return api.post<{
      success: boolean; message: string; data: MasterOverview;
      report: { warnings: string[] };
    }>('/reschedule/master/import', fd);
  },

  // ---------------------------------------------------------------------------
  // 导入向导（R-63）：预览（只读）→ 确认导入。
  //
  // 两个预览端点**不写任何状态**，且与 /master/import、/upload-master 共用
  // 同一份后端解析代码 —— 预览报的"将导入 N 行、没认出哪些列"就是点确认后
  // 真正发生的导入。前端因此不自己解析 Excel、不自己映射列名：
  // 教秘在界面上看到的每个字都来自服务端解析报告。
  // ---------------------------------------------------------------------------
  masterImportPreview: (file: File) => {
    const fd = new FormData();
    fd.append('file', file);
    return api.post<{ success: boolean; kind: 'workbook'; data: ImportWorkbookPreview }>(
      '/reschedule/master/import-preview', fd);
  },
  masterTasksPreview: (file: File) => {
    const fd = new FormData();
    fd.append('file', file);
    return api.post<{ success: boolean; kind: 'tasks'; data: ImportTasksPreview }>(
      '/reschedule/master/tasks-preview', fd);
  },
  // 教学任务清单的确认导入（写入 _master_tasks → 进并行排课）
  uploadMaster: (file: File) => {
    const fd = new FormData();
    fd.append('file', file);
    return api.post<{
      success: boolean; message: string; total_tasks: number;
      classes: string[]; report: { warnings: string[] };
    }>('/reschedule/upload-master', fd);
  },

  // 数据体检报告（扫描现有安排表冲突）
  dataAudit: () =>
    api.get<{ success: boolean; data: {
      healthy: boolean; total_entries: number; teacher_count: number; room_count: number;
      week_min: number; week_max: number;
      room_conflict_count: number; teacher_conflict_count: number;
      room_conflicts: Array<{ week: number; weekday: string; time_slot: string; place: string;
        involved: string[]; detail: string[] }>;
      teacher_conflicts: Array<{ week: number; weekday: string; time_slot: string; place: string;
        involved: string[]; detail: string[] }>;
    } }>('/reschedule/data-audit'),

  // 统计面板（教室利用率/教师工作量）
  stats: () =>
    api.get<{ success: boolean; data: {
      n_weeks: number; room_count: number; teacher_count: number;
      avg_room_utilization: number;
      busiest_room: { room: string; sessions: number; utilization: number } | null;
      avg_teacher_load: number; teacher_load_balance: number;
      top_rooms: Array<{ room: string; sessions: number; utilization: number }>;
      top_teachers: Array<{ teacher: string; sessions: number; experiments: number; active_weeks: number }>;
    } }>('/reschedule/stats'),
};
