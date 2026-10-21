/**
 * 把「本地后端接口」的异常翻译成一句教秘看得懂的中文。
 *
 * 为什么需要它
 * ------------
 * 全站有一打 `catch (err) { setMsg(`…失败：${err.response?.data?.detail || err.message}`) }`。
 * 只有**后端主动抛 HTTPException** 时 `detail` 才存在；凡是下面这几种情况，`detail` 都是
 * `undefined`，于是 `err.message` 直接漏到界面上（实测，见 scripts/probe_faults.cjs）：
 *
 *   - 未捕获异常 → FastAPI 回 `text/plain: Internal Server Error`（没有 JSON，没有 detail）
 *     ⇒ 界面出现「加载失败：Request failed with status code 500」
 *   - 后端没起 / 网络断 → `Network Error`
 *   - 60s 超时 → `timeout of 60000ms exceeded`
 *
 * 公网演示时这几种都是**最可能被撞见**的（服务被回收、冷启动超时），偏偏文案最不像人话，
 * 而且不含任何"接下来该怎么办"。这里统一成中文 + 一句可操作指引。
 *
 * 与 `cloud.ts` 的 `describeCloudError` 分工：那个处理**云端 SDK** 的错误码
 * （gateway_/quota_/auth_…），这个处理**自家 FastAPI** 的 HTTP 状态。两者不重叠、别混用。
 */

/** axios 错误的松散结构（不 import axios 类型，避免把库类型泄漏进工具层）。 */
interface ApiErrorLike {
  message?: string;
  code?: string;
  response?: {
    status?: number;
    data?: unknown;
    headers?: Record<string, unknown>;
  };
}

/** 已经带了"下一步怎么办"的文案不再追加指引，避免「…请稍后重试（请稍后重试）」。 */
const HAS_HINT = /重试|再试|刷新|稍后|稍候|检查|联系/;

function ensureHint(text: string): string {
  return HAS_HINT.test(text) ? text : `${text}（请稍后重试或刷新页面）`;
}

function asErrorLike(err: unknown): ApiErrorLike {
  return (err && typeof err === 'object' ? err : {}) as ApiErrorLike;
}

/** 从响应头取 `Retry-After`（秒）。取不到返回 0。 */
function retryAfterSeconds(headers: Record<string, unknown> | undefined): number {
  if (!headers) return 0;
  // 头名大小写不固定（axios 保留服务端原样），两种都试
  const raw = headers['retry-after'] ?? headers['Retry-After'];
  const n = Number(raw);
  return Number.isFinite(n) && n > 0 ? Math.ceil(n) : 0;
}

/**
 * 从后端响应体里取一句话。
 * - `{ detail: "中文原因" }`        → 原样用（后端文案比前端猜得准）
 * - `{ detail: [{msg: "..."}] }`    → FastAPI 422 校验错误，拼成一句
 * - 其他（text/plain、HTML 网关页）  → null，交给状态码兜底
 */
function pickDetail(data: unknown): string | null {
  if (!data || typeof data !== 'object') return null;
  const detail = (data as { detail?: unknown }).detail;
  if (typeof detail === 'string' && detail.trim()) return detail.trim();
  if (Array.isArray(detail)) {
    const parts = detail
      .map((d) => (d && typeof d === 'object' ? String((d as { msg?: unknown }).msg ?? '') : ''))
      .filter(Boolean);
    if (parts.length) return `参数校验失败：${parts.join('；')}`;
  }
  return null;
}

/**
 * 把本地接口异常翻译成中文提示（**总是**以一句可操作的指引结尾）。
 *
 * @param err       catch 到的任意异常
 * @param fallback  识别不出时用的兜底文案
 */
export function describeApiError(err: unknown, fallback = '请求失败，请稍后重试或刷新页面。'): string {
  const e = asErrorLike(err);
  const res = e.response;

  if (res) {
    const status = res.status ?? 0;
    const detail = pickDetail(res.data);

    // 429 单独处理：必须告诉用户**等多久**。后端 `detail` 只说"稍候再试"，
    // 真正的时间在 `Retry-After` 头里（ratelimit.py 会填）。
    if (status === 429) {
      const wait = retryAfterSeconds(res.headers);
      return wait
        ? `请求过于频繁，请 ${wait} 秒后再试。`
        : detail || '请求过于频繁，请稍后再试。';
    }

    if (detail) return ensureHint(detail);

    if (status === 404) return '接口不存在（404），服务端版本可能不一致，请刷新页面后重试。';
    if (status === 401 || status === 403) return '登录状态已失效或没有权限，请重新登录后再试。';
    if (status >= 500) return `服务端出错（HTTP ${status}），请稍后重试或刷新页面。`;
    if (status >= 400) return `请求被拒绝（HTTP ${status}），请刷新页面后重试。`;
    return fallback;
  }

  // 没有 response ⇒ 请求根本没到服务端，或者没等到响应
  const msg = String(e.message || '');
  if (e.code === 'ECONNABORTED' || /timeout/i.test(msg)) {
    return '请求超时（服务可能正忙），请稍后重试。';
  }
  if (e.code === 'ERR_NETWORK' || /network error|failed to fetch|load failed/i.test(msg)) {
    return '无法连接服务（网络中断或服务未启动），请检查网络后刷新页面重试。';
  }
  return ensureHint(fallback);
}
