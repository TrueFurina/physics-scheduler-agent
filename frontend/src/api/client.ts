import axios from 'axios';
import type { AiPlan } from '../types';

const API_BASE = '/api';

const api = axios.create({
  baseURL: API_BASE,
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

export interface StudentStatus {
  name: string;
  student_id: string;
  semester: string;
  total_courses: number;
  sample_courses: [string, string, string, string][];
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

  // 上传学生课表
  uploadStudent: (file: File) => {
    const formData = new FormData();
    formData.append('file', file);
    return api.post<{ success: boolean; message: string; total_courses: number }>('/reschedule/upload-student', formData, {
      headers: { 'Content-Type': 'multipart/form-data' },
    });
  },

  // 学生课表状态
  getStudentStatus: () => api.get<StudentStatus>('/reschedule/student-status'),

  // 教师列表（F1 身份选择）
  getTeachers: () => api.get<{ success: boolean; teachers: Teacher[]; total: number }>('/reschedule/teachers'),

  // 教师课程列表（F2）
  getTeacherCourses: (teacherName: string) =>
    api.get<{ success: boolean; teacher: string; courses: TeacherCourse[]; total: number }>(`/reschedule/teachers/${encodeURIComponent(teacherName)}/courses`),

  // 调课咨询（fast=true 时跳过 LLM 候选方案，秒回沙盘数据）
  chat: (message: string, fast: boolean = false) =>
    api.post<ChatResponse>('/reschedule/chat', { message, fast }),

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

  // 选课咨询（四大模块之一）
  courseSelect: (question: string) =>
    api.post<{ success: boolean; message: string; reply: string }>('/reschedule/course-select', { question }),

  // 实验辅导（四大模块之一）
  experimentTutor: (question: string) =>
    api.post<{ success: boolean; message: string; reply: string }>('/reschedule/experiment-tutor', { question }),

  // 排课自动生成（P1）
  autoSchedule: () =>
    api.post<{ success: boolean; message: string; data: {
      total_sections: number; solved: number; conflicts: any[]; schedule: any[];
    } }>('/reschedule/auto-schedule'),

  // 管理员登录（审批台）：口令由服务端校验，成功返回会话令牌
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
      affected_students: string[];
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
  parallelExport: (className: string, schedule: Array<{ project: string; group: string; week: number; weekday: string; time_slot: string; room: string; teacher: string }>) =>
    api.post('/reschedule/parallel/export', { class_name: className, schedule }, { responseType: 'blob' }),

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
