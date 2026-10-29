import { describe, expect, it } from 'vitest';
import { describeApiError } from '../apiError';

/**
 * 这些用例锁定的是**实测出来的缺陷**，不是假想的边界：
 * 把 /teachers 拦成 500（FastAPI 未捕获异常回的就是 text/plain）时，界面曾经显示
 * 「加载失败：Request failed with status code 500」——中文界面里的 axios 原文。
 */

const EN_NOISE = /Request failed with status code|Network Error|timeout of \d+\s*ms exceeded|Internal Server Error/i;
const ACTIONABLE = /重试|再试|刷新|稍后|稍候|检查/;

function axiosErr(opts: {
  status?: number;
  data?: unknown;
  headers?: Record<string, unknown>;
  code?: string;
  message?: string;
}) {
  return {
    message: opts.message ?? `Request failed with status code ${opts.status}`,
    code: opts.code,
    response:
      opts.status === undefined
        ? undefined
        : { status: opts.status, data: opts.data, headers: opts.headers ?? {} },
  };
}

describe('describeApiError', () => {
  it('无 detail 的 500（text/plain）翻译成中文，不泄漏 axios 原文', () => {
    const msg = describeApiError(axiosErr({ status: 500, data: 'Internal Server Error' }));
    expect(msg).toContain('服务端出错');
    expect(msg).toContain('HTTP 500');
    expect(msg).not.toMatch(EN_NOISE);
    expect(msg).toMatch(ACTIONABLE);
  });

  it('后端给了中文 detail 就原样用，且不重复追加指引', () => {
    const msg = describeApiError(axiosErr({ status: 400, data: { detail: '请先加载实验安排表，再试一次' } }));
    expect(msg).toBe('请先加载实验安排表，再试一次');
    expect(msg.match(/重试|再试/g)?.length).toBe(1);
  });

  it('detail 不带指引时补一句下一步', () => {
    const msg = describeApiError(axiosErr({ status: 400, data: { detail: '缺少参数 class_name' } }));
    expect(msg.startsWith('缺少参数 class_name')).toBe(true);
    expect(msg).toMatch(ACTIONABLE);
  });

  it('422 的数组 detail 拼成一句', () => {
    const msg = describeApiError(
      axiosErr({ status: 422, data: { detail: [{ msg: 'field required' }, { msg: 'bad week' }] } }),
    );
    expect(msg).toContain('参数校验失败');
    expect(msg).toContain('field required');
    expect(msg).toContain('bad week');
  });

  it('429 读 Retry-After，把等待秒数说出来', () => {
    const msg = describeApiError(
      axiosErr({ status: 429, data: { detail: '请求过于频繁，请稍候再试。' }, headers: { 'retry-after': '15' } }),
    );
    expect(msg).toContain('15 秒');
  });

  it('429 没有 Retry-After 时退回"稍后再试"', () => {
    const msg = describeApiError(axiosErr({ status: 429, data: { detail: '请求过于频繁，请稍候再试。' } }));
    expect(msg).toMatch(/稍后再试|稍候/);
    expect(msg).not.toMatch(EN_NOISE);
  });

  it('401/403 提示重新登录，不冒充"密码错"', () => {
    const msg = describeApiError(axiosErr({ status: 401 }));
    expect(msg).toContain('登录');
    expect(msg).toMatch(ACTIONABLE);
  });

  it('网络断开（ERR_NETWORK）不再显示 "Network Error"', () => {
    const msg = describeApiError(
      axiosErr({ code: 'ERR_NETWORK', message: 'Network Error' }),
    );
    expect(msg).toContain('无法连接服务');
    expect(msg).not.toMatch(EN_NOISE);
  });

  it('超时提示"服务可能正忙"', () => {
    const msg = describeApiError(
      axiosErr({ code: 'ECONNABORTED', message: 'timeout of 60000ms exceeded' }),
    );
    expect(msg).toContain('超时');
    expect(msg).not.toMatch(EN_NOISE);
  });

  it('完全认不出的异常也给中文兜底', () => {
    expect(describeApiError(undefined)).not.toMatch(EN_NOISE);
    expect(describeApiError(new Error('boom'))).not.toMatch(EN_NOISE);
    expect(describeApiError(new Error('boom'))).toMatch(ACTIONABLE);
  });

  it('所有分支的输出都不含英文原文（回归：这是本模块存在的唯一理由）', () => {
    const samples = [
      axiosErr({ status: 500, data: 'Internal Server Error' }),
      axiosErr({ status: 502 }),
      axiosErr({ status: 404 }),
      axiosErr({ status: 429, headers: { 'Retry-After': '7' } }),
      axiosErr({ code: 'ERR_NETWORK', message: 'Network Error' }),
      axiosErr({ code: 'ECONNABORTED', message: 'timeout of 60000ms exceeded' }),
      new Error('Request failed with status code 500'),
    ];
    for (const s of samples) {
      expect(describeApiError(s)).not.toMatch(EN_NOISE);
    }
  });
});

/**
 * 413 上传超限（2026-10-29 补）。
 *
 * 背景：后端给上传接口加了 32MB 上限，超限返回 413 + `X-Max-Upload-Bytes` 头，
 * 但 `detail` 里写的是给运维看的话（"设置环境变量 MAX_UPLOAD_MB 后重启服务"）。
 * 前端若不专门处理，教秘会看到：
 *   "文件超过上限（32 MB）。如确需上传更大的表格，请设置环境变量 MAX_UPLOAD_MB
 *    后重启服务。（请稍后重试或刷新页面）"
 * ——既让他去动环境变量，又让他重试同一个注定失败的文件。
 */
describe('describeApiError · 413 上传超限', () => {
  it('提示换小文件，而不是让用户重试或改环境变量', () => {
    const msg = describeApiError(
      axiosErr({
        status: 413,
        data: {
          detail:
            '文件超过上限（32 MB）。如确需上传更大的表格，请设置环境变量 MAX_UPLOAD_MB 后重启服务。',
        },
        headers: { 'x-max-upload-bytes': '33554432' },
      }),
    );

    // 必须说清"怎么办"
    expect(msg).toMatch(/压缩|拆分|更小/);
    // 不能把运维话术漏给用户
    expect(msg).not.toMatch(/环境变量|MAX_UPLOAD_MB|重启服务/);
    // 不能是"重试"这种无效建议作为主指引
    expect(msg).not.toMatch(/刷新页面/);
  });

  it('从 X-Max-Upload-Bytes 头读上限，前端不硬编码', () => {
    const msg = describeApiError(
      axiosErr({
        status: 413,
        data: { detail: '太大' },
        headers: { 'x-max-upload-bytes': String(8 * 1024 * 1024) },
      }),
    );
    expect(msg).toContain('8 MB');
  });

  it('头缺失时也能给出可用文案（不出现"上限 服务端上限"这类病句）', () => {
    const msg = describeApiError(
      axiosErr({ status: 413, data: { detail: '太大' }, headers: {} }),
    );
    expect(msg).toContain('文件太大');
    expect(msg).toMatch(/压缩|拆分/);
    expect(msg).not.toMatch(/上限\s*服务端上限/);
  });
});

/**
 * R8：`MasterDataView` 曾用 `err?.response?.data?.detail || 'xxx失败'`，
 * 与 R0 修掉的是同一个反模式（模块 docstring 里点名的那个）。
 *
 * 后果不是"报错"，而是**错误原因丢失**：500 / 网络断 / 超时 / 503
 * 全部显示成同一句"加载主数据失败"，教秘无法判断该重试、该查网络、还是该找管理员。
 *
 * 这里锁定 describeApiError 对这几类的区分能力——它们是 MasterDataView
 * 现在依赖的行为。
 */
describe('describeApiError · 错误原因必须可区分（R8）', () => {
  const cases: Array<[string, unknown, RegExp]> = [
    ['500 未捕获异常', axiosErr({ status: 500, data: 'Internal Server Error' }), /服务端出错/],
    ['503 网关', axiosErr({ status: 503, data: '' }), /服务端出错/],
    ['网络断开', { message: 'Network Error' }, /无法连接服务/],
    ['超时', { message: 'timeout of 60000ms exceeded' }, /请求超时/],
  ];

  it.each(cases)('%s 给出专属文案（而不是统一一句"失败"）', (_name, err, re) => {
    const msg = describeApiError(err, '加载主数据失败');
    expect(msg).toMatch(re);
    // 关键：不能只是原样返回 fallback
    expect(msg).not.toBe('加载主数据失败');
  });

  it('四类错误的文案互不相同——否则"区分"没有意义', () => {
    const msgs = cases.map(([, err]) => describeApiError(err, '加载主数据失败'));
    expect(new Set(msgs).size).toBe(cases.length);
  });

  it('fallback 仍作为兜底出现在可用位置', () => {
    // 完全认不出的异常：应包含 fallback 或等价中文，且不含英文原文
    const msg = describeApiError({}, '加载主数据失败');
    expect(msg).not.toMatch(EN_NOISE);
    expect(msg.length).toBeGreaterThan(0);
  });
});

describe('全站 catch 必须走统一错误处理（R8 守卫）', () => {
  /**
   * 为什么需要静态守卫：
   * `err?.response?.data?.detail || 'xxx失败'` 这个反模式会让 500/网络断/超时
   * 全部塌缩成一句"失败"，而且**不会报任何错、不会有任何测试失败**。
   * 只能靠扫描源码拦住。
   */
  const fs = require('fs');
  const path = require('path');

  function walk(dir: string, out: string[] = []): string[] {
    for (const name of fs.readdirSync(dir)) {
      const full = path.join(dir, name);
      const st = fs.statSync(full);
      if (st.isDirectory()) {
        if (name === '__tests__' || name === 'node_modules') continue;
        walk(full, out);
      } else if (/\.(tsx?|jsx?)$/.test(name)) {
        out.push(full);
      }
    }
    return out;
  }

  // 匹配 "detail || " / "detail || '" —— 即直取 detail 后用固定文案兜底
  const COLLAPSE = /\.detail\s*\|\|\s*['"`]/;
  const STATUS_401 = /status\s*===\s*401/;

  it('没有文件再用 `detail || 固定文案` 这种塌缩写法', () => {
    // 例外：位于 `status === 401` 分支内的直取是合法的 ——
    // 后端 401 的 detail 就是可操作文案（R3 已补齐），比前端兜底更准确。
    // App.tsx 的管理员登录分支属于这一类，它的 else 分支已走 describeApiError。
    const root = path.resolve(__dirname, '../..');
    const offenders: string[] = [];
    for (const f of walk(root)) {
      if (/apiError\.ts$/.test(f)) continue;   // 它本身就是干这个的
      const lines = fs.readFileSync(f, 'utf8').split(/\r?\n/);
      for (let i = 0; i < lines.length; i++) {
        if (!COLLAPSE.test(lines[i])) continue;
        const ctx = lines.slice(Math.max(0, i - 3), i + 1).join('\n');
        if (STATUS_401.test(ctx)) continue;
        offenders.push(path.relative(root, f) + ':' + (i + 1));
      }
    }
    expect(offenders).toEqual([]);
  });

  it('登录相关 catch 使用 describeCloudError（不能漏英文原文）', () => {
    const app = fs.readFileSync(path.resolve(__dirname, '../../App.tsx'), 'utf8');
    const bad = app.match(/setLoginErr\(err\?\.message/g) || [];
    expect(bad).toEqual([]);
  });

  it('MasterDataView 的 5 个 catch 全部走 describeApiError（R8 修复点）', () => {
    const src = fs.readFileSync(
      path.resolve(__dirname, '../../views/MasterDataView.tsx'), 'utf8'
    );
    const descCount = (src.match(/describeApiError/g) || []).length;
    // 1 处 import + 5 处 setError
    expect(descCount).toBeGreaterThanOrEqual(6);
  });
});
