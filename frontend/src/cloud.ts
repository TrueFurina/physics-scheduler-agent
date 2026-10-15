/**
 * 云服务（WorkBuddy Cloud Service）客户端初始化。
 *
 * publicConfig 由开通云服务时平台下发，只包含可安全放到前端代码里的公开字段：
 *   resourceId / endpoint / oauthRelayBaseUrl / publishableKey
 * 其中 publishableKey 只标识"是哪个应用"，本身不含任何权限；真正的鉴权由服务端
 * 的 Origin 精确匹配 + 登录态完成。底层环境 ID 与供应商密钥始终留在服务端。
 */
import { createWorkBuddyCloud } from '@tencent-ai/workbuddy-cloud-sdk';

export const publicConfig = {
  resourceId: 'wbcs_vX4fn006i2lQT7fpXOUUt7',
  endpoint: 'https://physics-scheduler.app.workbuddy.host',
  oauthRelayBaseUrl: 'https://www.workbuddy.cn/v2/as/genie-baas/oauth',
  publishableKey: 'wbpk_C5QC1ilCbEStyirXoqSAoS_eSki8lwS8oBprXSH4Lxx7y28QwSv6Bic',
};

// ---------------------------------------------------------------------------
// 云端表结构（public schema）
// ---------------------------------------------------------------------------

export interface RescheduleRequestRow {
  id: number;
  owner_id: string;
  owner_name: string | null;
  teacher: string;
  course_name: string;
  class_name: string;
  original_time: string;
  target_week: number;
  reason: string | null;
  status: string; // pending / approved / rejected
  admin_comment: string | null;
  decided_by: string | null;
  created_at: string;
}

export interface ConsultLogRow {
  id: number;
  owner_id: string;
  owner_name: string | null;
  module: string;
  question: string;
  answer: string | null;
  created_at: string;
}

/** id / owner_id / created_at 由数据库负责（IDENTITY / auth.uid() / now()），客户端不传。 */
type Auto = 'id' | 'owner_id' | 'created_at';
type InsertOf<T> = Omit<T, Auto> & { id?: number; created_at?: string; owner_id?: never };
type UpdateOf<T> = Partial<Omit<T, Auto>>;

interface Table<Row, Insert, Update> {
  Row: Row;
  Insert: Insert;
  Update: Update;
  Relationships: [];
}

export interface Database {
  public: {
    Tables: {
      reschedule_requests: Table<RescheduleRequestRow, InsertOf<RescheduleRequestRow>, UpdateOf<RescheduleRequestRow>>;
      consult_logs: Table<ConsultLogRow, InsertOf<ConsultLogRow>, UpdateOf<ConsultLogRow>>;
    };
    Views: Record<string, never>;
    Functions: Record<string, never>;
  };
}

/** 全应用共用同一个客户端实例：Auth / Database / Storage / LLM 均复用它。 */
export const cloud = createWorkBuddyCloud<Database>({
  endpoint: publicConfig.endpoint,
  oauthRelayBaseUrl: publicConfig.oauthRelayBaseUrl,
  publishableKey: publicConfig.publishableKey,
});

/** 把 SDK 抛出的错误翻译成用户能看懂的一句话。 */
export function describeCloudError(err: unknown): string {
  const e = err as any;
  const code: string = e?.error?.code ?? '';
  if (code.startsWith('quota_')) return '云端额度不足或触发限流，请稍后再试。';
  if (code.startsWith('auth_')) return '当前访问来源未被云端信任，请在应用正式发布域名下访问。';
  if (code.startsWith('request_')) return '请求参数或模型不匹配，请刷新后重试。';
  if (code.startsWith('gateway_') || code.startsWith('model_')) return '模型服务暂时不可用，请稍后重试。';
  if (code.startsWith('internal_')) return `云端服务异常（${code}），请稍后重试。`;
  if (code === 'gateway_stream_interrupted') return '云端返回中断，已保留已生成内容，可重新生成。';
  return e?.message || '云端请求失败，请稍后重试。';
}
