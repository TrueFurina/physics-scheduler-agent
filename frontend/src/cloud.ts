/**
 * 云服务（WorkBuddy Cloud Service）客户端初始化。
 *
 * publicConfig 由开通云服务时平台下发，只包含可安全放到前端代码里的公开字段：
 *   resourceId / endpoint / oauthRelayBaseUrl / publishableKey
 * 其中 publishableKey 只标识"是哪个应用"，本身不含任何权限；真正的鉴权由服务端
 * 的 Origin 精确匹配 + 登录态完成。底层环境 ID 与供应商密钥始终留在服务端。
 */
import type { WorkBuddyCloudClient } from '@tencent-ai/workbuddy-cloud-sdk';

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

/**
 * 全应用共用同一个客户端实例：Auth / Database / Storage / LLM 均复用它。
 *
 * **为什么是动态 import**：SDK 只在"登录 / 云表 / 云端 LLM"这条链路上需要，
 * 而教秘的主路径（教师选择、沙盘、冲突分析、并行排课、数据看板）完全走本地
 * API、一个云接口都不碰。静态 import 会把它塞进首屏主包，让所有访客为一个
 * 多数人用不到的能力买单。
 *
 * 改成 `import()` 后 Vite 会把它切成独立 chunk，只有真正调用云能力的交互
 * （点「登录」/「提交申请」/「咨询历史」）才下载；首屏主包因此变小。
 * 实例本身仍**只创建一次**（模块级缓存 + 共享 Promise 防并发重复初始化）。
 */
type CloudClient = WorkBuddyCloudClient<Database>;

let _cloud: CloudClient | null = null;
let _loading: Promise<CloudClient> | null = null;

export function getCloud(): Promise<CloudClient> {
  if (_cloud) return Promise.resolve(_cloud);
  if (!_loading) {
    _loading = import('@tencent-ai/workbuddy-cloud-sdk')
      .then(({ createWorkBuddyCloud }) => {
        _cloud = createWorkBuddyCloud<Database>({
          endpoint: publicConfig.endpoint,
          oauthRelayBaseUrl: publicConfig.oauthRelayBaseUrl,
          publishableKey: publicConfig.publishableKey,
        });
        return _cloud;
      })
      .catch((err) => {
        // 加载失败必须允许下次重试，否则一次网络抖动会把云能力永久锁死
        _loading = null;
        throw err;
      });
  }
  return _loading;
}

/** 把 SDK 抛出的错误翻译成用户能看懂的一句话。 */
export function describeCloudError(err: unknown): string {
  const e = err as any;
  const code: string = e?.error?.code ?? '';
  // 精确匹配必须放在前缀匹配之前：`gateway_stream_interrupted` 同时满足
  // `startsWith('gateway_')`，写反了这条分支永远不可达（用户会看到
  // "模型服务暂时不可用"，而真实原因是流被中断、已生成内容其实还在）。
  if (code === 'gateway_stream_interrupted') return '云端返回中断，已保留已生成内容，可重新生成。';
  if (code.startsWith('quota_')) return '云端额度不足或触发限流，请稍后再试。';
  if (code.startsWith('auth_')) return '当前访问来源未被云端信任，请在应用正式发布域名下访问。';
  if (code.startsWith('request_')) return '请求参数或模型不匹配，请刷新后重试。';
  if (code.startsWith('gateway_') || code.startsWith('model_')) return '模型服务暂时不可用，请稍后重试。';
  if (code.startsWith('internal_')) return `云端服务异常（${code}），请稍后重试。`;
  return e?.message || '云端请求失败，请稍后重试。';
}
