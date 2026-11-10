// @vitest-environment node
/**
 * R101：云端大模型返回「零内容流」时，不许让教秘对着一片空白发呆。
 *
 * 缺陷（动手前只读探针实测）
 * --------------------------
 * `cloudApi.ts::askCloudLlm()` 把流式 chunk 累加进 `answer`，循环结束直接
 * `return answer`。云端 HTTP 200 但一个有效 delta 都没给（模型拒答、
 * 内容审核静默拦截、网关吞流、白名单退化）时返回的是 `''`，**不抛错**。
 * App.tsx 的 `handleConsult` 因此：
 *   · `catch` 不触发（没有异常，`describeCloudError` 根本没机会说话）；
 *   · `if (session && answer)` 为假 → 问答不写云端历史；
 *   · `ConsultView` 的 `answer && (...)` 为假 → 页面什么都不渲染；
 *   · `finally` 里 `setConsultQ('')` 还把刚提的问题清空了。
 * 用户看到的就是「点完提问，转个圈，一片空白」，与 R35「没查到必须说出口」
 * 同族。后端 `llm_intent_parser.consult()` 早已把空内容转成 `{"error": ...}`，
 * 缺的是前端流式路径这一半。
 *
 * 修法口径：不动 App.tsx（R96 后它压在 1500 行 / 70 个 useState 的体量守卫上），
 * 只在 `cloudApi.ts` 里把空流变成结构化异常，交给**既有** catch 分支用
 * `describeCloudError` 显示。成功路径逐字节不变；用户主动停止是例外，不误报。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import fs from 'node:fs';
import path from 'node:path';

const modelList = vi.fn();
const create = vi.fn();

// 只替换装载点，保留 cloud.ts 其它导出（describeCloudError 要用真的那份）。
// SDK 本身是 getCloud() 里的动态 import，mock 掉之后测试不碰网络。
vi.mock('../cloud', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../cloud')>();
  return { ...actual, getCloud: vi.fn() };
});

import { getCloud, describeCloudError } from '../cloud';
import { askCloudLlm } from '../cloudApi';

type Delta = { choices?: Array<{ delta?: { content?: string } }> };

function stream(chunks: Delta[]) {
  return (async function* () {
    for (const c of chunks) yield c;
  })();
}

const SRC = path.resolve(__dirname, '..');
const appSource = () => fs.readFileSync(path.resolve(SRC, 'App.tsx'), 'utf8');
const cloudApiSource = () => fs.readFileSync(path.resolve(SRC, 'cloudApi.ts'), 'utf8');
const viewSource = () => fs.readFileSync(path.resolve(SRC, 'views', 'ConsultView.tsx'), 'utf8');

beforeEach(() => {
  vi.clearAllMocks();
  modelList.mockResolvedValue([{ id: 'model-r101', disabled: false }]);
  vi.mocked(getCloud).mockResolvedValue({
    llm: {
      models: { list: modelList },
      chat: { completions: { create } },
    },
  } as any);
});

describe('askCloudLlm：零内容流必须说出口', () => {
  it('探针命中守卫：create 真的被调到、onDelta 一次都没触发（否则整组在测空气）', async () => {
    create.mockReturnValue(stream([]));
    const onDelta = vi.fn();
    try {
      await askCloudLlm('tutor', '声速测量实验的数据处理规范是什么？', onDelta);
    } catch {
      // 修复后会抛；这条守卫在红/绿两阶段都要成立
    }
    expect(create).toHaveBeenCalledTimes(1);
    expect(onDelta).not.toHaveBeenCalled();
  });

  it('零 delta 的空流不许静默返回空串', async () => {
    create.mockReturnValue(stream([]));
    await expect(
      askCloudLlm('tutor', '扭摆法需要几学时？', vi.fn()),
    ).rejects.toThrow(/未返回|没有.*内容|空/);
  });

  it('全是空白 delta 的流同样算「没内容」（判据必须 trim）', async () => {
    create.mockReturnValue(stream([
      { choices: [{ delta: { content: '   ' } }] },
      { choices: [{ delta: { content: '\n' } }] },
    ]));
    await expect(askCloudLlm('tutor', 'q', vi.fn())).rejects.toThrow();
  });

  it('抛出的错误经 describeCloudError 后仍是能照着做的具体原因', async () => {
    create.mockReturnValue(stream([]));
    let caught: unknown;
    try {
      await askCloudLlm('tutor', 'q', vi.fn());
    } catch (e) {
      caught = e;
    }
    expect(caught).toBeInstanceOf(Error);
    const hint = describeCloudError(caught);
    expect(hint).toMatch(/未返回|没有.*内容|重试/);
    expect(hint).not.toBe('云端请求失败，请稍后重试。');
  });

  // ------------------------------------------------------------ 防放宽

  it('防放宽：正常流式回答逐字不变，onDelta 仍按累积文本回调', async () => {
    create.mockReturnValue(stream([
      { choices: [{ delta: { content: '甲方' } }] },
      { choices: [{ delta: { content: '、乙方' } }] },
    ]));
    const onDelta = vi.fn();
    const answer = await askCloudLlm('tutor', 'q', onDelta);
    expect(answer).toBe('甲方、乙方');
    expect(onDelta.mock.calls.map((c) => c[0])).toEqual(['甲方', '甲方、乙方']);
  });

  it('防放宽：已生成部分内容、后面只是空 chunk，不许误报失败', async () => {
    create.mockReturnValue(stream([
      { choices: [{ delta: { content: '已生成' } }] },
      { choices: [{}] },
    ]));
    await expect(askCloudLlm('tutor', 'q', vi.fn())).resolves.toBe('已生成');
  });

  it('防放宽：用户主动停止且零内容时保持安静，不把中止说成故障', async () => {
    const controller = new AbortController();
    controller.abort();
    create.mockReturnValue(stream([]));
    await expect(
      askCloudLlm('tutor', 'q', vi.fn(), controller.signal),
    ).resolves.toBe('');
  });

  // ------------------------------------------------------------ 接线守卫

  it('源码守卫：空流判据看 trim、放行 signal.aborted，并把原因抛出去', () => {
    const src = cloudApiSource();
    const body = src.slice(src.indexOf('export async function askCloudLlm'));
    expect(body).toContain('answer.trim()');
    expect(body).toContain('signal?.aborted');
    expect(body).toContain('throw new Error(');
  });

  it('接线守卫：既有 handleConsult catch 会把错误写回 consultMsg', () => {
    const src = appSource();
    const handler = src.slice(
      src.indexOf('const handleConsult = async () =>'),
      src.indexOf('const handleStopConsult'),
    );
    expect(handler).toContain('askCloudLlm(');
    expect(handler).toContain('describeCloudError(err)');
    expect(handler).toContain('setConsultMsg');
    // ConsultView 是纯展示组件，只有非空 answer 才渲染 —— 所以错误必须
    // 真的写进 consultMsg，光在控制台打一行等于没修（规则 24）。
    expect(viewSource()).toContain('answer && (');
    // 本轮刻意零改动 App.tsx
    expect(src.split('\n').length).toBeLessThanOrEqual(1500);
    expect((src.match(/useState/g) ?? []).length).toBeLessThanOrEqual(70);
  });
});
