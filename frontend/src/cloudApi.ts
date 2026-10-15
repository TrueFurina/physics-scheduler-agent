/**
 * 云服务能力封装：登录 / 调课申请数据 / AI 咨询（大模型）。
 * 所有调用都走 @tencent-ai/workbuddy-cloud-sdk，不手写 fetch、不拼 /.cloud/** 路径。
 */
import type { ChatCompletionCreateParamsStreaming } from '@tencent-ai/workbuddy-cloud-sdk';
import { cloud } from './cloud';
import type { RescheduleRequestRow, ConsultLogRow } from './cloud';

export type CloudSession = {
  user?: { id?: string; name?: string | null; avatarUrl?: string | null } | null;
} | null;

export type CloudRequest = RescheduleRequestRow;
export type CloudConsultLog = Pick<
  ConsultLogRow,
  'id' | 'module' | 'question' | 'answer' | 'owner_name' | 'created_at'
>;

export type ConsultModuleKey = 'course_select' | 'tutor';

// ---------------------------------------------------------------------------
// Auth：登录 / 会话 / 回调
// ---------------------------------------------------------------------------

export async function getCloudSession(): Promise<CloudSession> {
  const { data, error } = await cloud.auth.getSession();
  if (error) return null;
  return (data as CloudSession) ?? null;
}

export async function signOutCloud(): Promise<void> {
  await cloud.auth.signOut();
}

/**
 * OAuth 回调分发器：必须在任何路由/守卫之前、应用启动时调用。
 * 普通访问直接返回 handled=false 走会话恢复；只有携带 code/error 的回调才处理，
 * 且按 provider 只调用其中一个处理函数（两个都调会让微信回调被误判成错误）。
 */
export async function dispatchAuthCallback(): Promise<{
  handled: boolean;
  session: CloudSession;
  error?: string;
}> {
  const url = new URL(location.href);
  const hasCode = url.searchParams.has('code');
  const providerError = url.searchParams.get('error');
  if (!hasCode && !providerError) {
    return { handled: false, session: null };
  }

  const provider = url.searchParams.get('provider');
  const errorDescription = url.searchParams.get('error_description');

  /**
   * 回调参数是一次性的：只要接住了就必须清掉，**失败路径同样要清**。
   * 否则 URL 里的 code/state 会一直留着，用户每刷新一次就重放一次已作废的 code
   * （实测每次刷新都会打一发 401），看起来像后端坏了。
   */
  const stripCallbackParams = () => {
    for (const key of ['code', 'state', 'provider', 'error', 'error_description']) {
      url.searchParams.delete(key);
    }
    history.replaceState(null, '', `${url.pathname}${url.search}${url.hash}`);
  };

  if (provider !== 'workbuddy' && provider !== 'wechat') {
    stripCallbackParams();
    // 带 error 却没带 provider：这是上游拒绝/中断授权，要报真实原因，
    // 不能笼统说成「缺少登录方式」把用户引到错误方向。
    const detail = errorDescription || providerError;
    return {
      handled: true,
      session: null,
      error: detail ? `登录未完成：${detail}` : '授权回调缺少有效的登录方式，请重新登录。',
    };
  }

  const auth = cloud.auth;
  const handleCallback =
    provider === 'workbuddy' ? auth.handleWorkBuddyCallback : auth.handleWechatWebCallback;
  // 不能直接 .bind()：方法缺失时 .bind 会抛错，被外层 catch 吞掉后变成静默无响应。
  if (typeof handleCallback !== 'function') {
    stripCallbackParams();
    return { handled: true, session: null, error: '暂时无法使用此方式登录，请联系应用管理员。' };
  }

  try {
    const { data: session, error } = await handleCallback.call(auth);
    if (error) {
      stripCallbackParams();
      return { handled: true, session: null, error: error.message };
    }
    stripCallbackParams();
    return { handled: true, session: (session as CloudSession) ?? null };
  } catch (err: any) {
    stripCallbackParams();
    return { handled: true, session: null, error: err?.message || '登录回调处理失败' };
  }
}

export interface PendingPhoneOtp {
  phone: string;
  verificationId: string;
  isExistingUser: boolean;
}

/** 发送短信验证码（会真的发一条短信，只在「获取验证码」按钮里调用）。 */
export async function sendPhoneCode(phone: string): Promise<PendingPhoneOtp> {
  const { data, error } = await cloud.auth.sendOtp({ phone });
  if (error) throw new Error(error.message);
  return {
    phone,
    verificationId: data!.verificationId,
    isExistingUser: data!.isExistingUser,
  };
}

/** 只用已保存的验证码会话提交，绝不在这里重新发码。 */
export async function verifyPhoneCode(pending: PendingPhoneOtp, code: string): Promise<CloudSession> {
  const { data, error } = await cloud.auth.verifyOtp({
    phone: pending.phone,
    verificationId: pending.verificationId,
    isExistingUser: pending.isExistingUser,
    token: code,
  });
  if (error) throw new Error(error.message);
  return (data as CloudSession) ?? null;
}

/** 网页微信登录：拿到授权地址后在当前标签页跳转（PKCE 校验器在当前标签页，不能新开窗口）。 */
export async function signInWithWechat(): Promise<void> {
  const { data, error } = await cloud.auth.signInWithOAuth({
    provider: 'wechat',
    redirectTo: location.origin,
  });
  if (error) throw new Error(error.message);
  const url = data?.url;
  if (!url) throw new Error('未获取到微信登录地址');
  location.assign(url);
}

// ---------------------------------------------------------------------------
// Database：调课申请（云端持久化 + 跨端共享）
// ---------------------------------------------------------------------------

export async function listCloudRequests(status?: string): Promise<CloudRequest[]> {
  const base = cloud.database
    .from('reschedule_requests')
    .select('*')
    .order('created_at', { ascending: false })
    .limit(200);
  const { data, error } = status ? await base.eq('status', status) : await base;
  if (error) throw new Error(error.message);
  return (data ?? []) as CloudRequest[];
}

export async function createCloudRequest(
  payload: {
    teacher: string;
    course_name: string;
    class_name: string;
    original_time: string;
    target_week: number;
    reason: string;
  },
  ownerName: string | null,
): Promise<CloudRequest> {
  // 不传 owner_id：由表默认值 auth.uid() 在服务端填充，RLS 会拒绝伪造的 owner
  const { data, error } = await cloud.database
    .from('reschedule_requests')
    .insert({ ...payload, status: 'pending', owner_name: ownerName })
    .select();
  if (error) throw new Error(error.message);
  const rows = (data ?? []) as CloudRequest[];
  if (rows.length === 0) throw new Error('申请未写入云端（权限不足或被策略拦截）');
  return rows[0];
}

export async function approveCloudRequest(
  id: number,
  approve: boolean,
  comment: string,
  decidedBy: string | null,
): Promise<CloudRequest> {
  const { data, error } = await cloud.database
    .from('reschedule_requests')
    .update({
      status: approve ? 'approved' : 'rejected',
      admin_comment: comment,
      decided_by: decidedBy,
    })
    .eq('id', id)
    .select();
  if (error) throw new Error(error.message);
  const rows = (data ?? []) as CloudRequest[];
  // 返回空数组 = RLS 过滤掉了这行，而不是"更新成功"
  if (rows.length === 0) throw new Error('该申请不存在，或当前账号无权审批');
  return rows[0];
}

// ---------------------------------------------------------------------------
// Database：AI 咨询记录（按用户隔离）
// ---------------------------------------------------------------------------

export async function logConsult(
  moduleKey: ConsultModuleKey,
  question: string,
  answer: string,
  ownerName: string | null,
): Promise<void> {
  const { error } = await cloud.database.from('consult_logs').insert({
    module: moduleKey,
    question,
    answer: answer.slice(0, 6000),
    owner_name: ownerName,
  });
  if (error) throw new Error(error.message);
}

export async function listConsultLogs(moduleKey: ConsultModuleKey): Promise<CloudConsultLog[]> {
  const { data, error } = await cloud.database
    .from('consult_logs')
    .select('id, module, question, answer, owner_name, created_at')
    .eq('module', moduleKey)
    .order('created_at', { ascending: false })
    .limit(20);
  if (error) throw new Error(error.message);
  return (data ?? []) as CloudConsultLog[];
}

// ---------------------------------------------------------------------------
// LLM：免密钥大模型调用（选课咨询 / 实验辅导）
// ---------------------------------------------------------------------------

const SYSTEM_PROMPTS: Record<ConsultModuleKey, string> = {
  course_select:
    '你是某大学物理实验教学中心的选课咨询助手。学生咨询的是物理实验课程的选课资格、先修课程、学分要求、重修与时间冲突处理等问题。'
    + '请用简体中文、条理清晰地回答：先给结论，再给依据。不确定的信息不要编造，明确告知需要向教务或实验中心确认。',
  tutor:
    '你是某大学物理实验教学中心的实验辅导助手。学生咨询的是实验原理、操作步骤、数据处理、误差分析与实验报告规范。'
    + '请用简体中文回答：先给核心思路，再分点展开。涉及公式时用文字或 LaTeX 表示。不要编造特定实验的原始数据。',
};

let cachedModelId: string | null = null;

async function resolveModelId(): Promise<string> {
  if (cachedModelId) return cachedModelId;
  const models = await cloud.llm.models.list();
  if (!models || models.length === 0) throw new Error('云端当前没有可用的大模型');
  const pick = models.find((m) => m.disabled !== true);
  if (!pick) throw new Error('云端当前没有可用的大模型');
  cachedModelId = pick.id;
  return cachedModelId;
}

/**
 * 流式问答：onDelta 收到的是「到目前为止的完整文本」，便于直接渲染。
 * 注意 messages[0] 必须是应用自己的 system 消息，否则调用会被拒绝。
 * 只需要单次结果也走 streaming——该接口不支持非流式。
 */
export async function askCloudLlm(
  moduleKey: ConsultModuleKey,
  question: string,
  onDelta: (fullText: string) => void,
  signal?: AbortSignal,
): Promise<string> {
  const modelId = await resolveModelId();
  const params: ChatCompletionCreateParamsStreaming = {
    model: modelId,
    messages: [
      { role: 'system', content: SYSTEM_PROMPTS[moduleKey] },
      { role: 'user', content: question },
    ],
    stream: true,
    temperature: 0.6,
    ...(signal ? { signal } : {}),
  };

  let answer = '';
  for await (const chunk of cloud.llm.chat.completions.create(params)) {
    const text = chunk?.choices?.[0]?.delta?.content;
    if (text) {
      answer += text;
      onDelta(answer);
    }
  }
  return answer;
}
