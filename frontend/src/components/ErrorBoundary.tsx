import { Component, type ErrorInfo, type ReactNode } from 'react';

/**
 * 视图级错误边界：把「一个视图崩了」限制在内容区，而不是整页白屏。
 *
 * 为什么必须有（不是"锦上添花"）：
 * React 18 里任何渲染期抛错，若没有边界接管，会把**整棵树**卸载 —— 用户看到纯白页面，
 * 连顶部导航一起消失，只能手动刷新。而本应用刚给三个视图加了 `React.lazy`，
 * 又多了一条必炸路径：`import()` 的 chunk 请求失败（网络抖动、部署切换、
 * 代理层 5xx、用户缓存被清理）会让 `lazy` 在渲染期 reject，同属"渲染期抛错"。
 *
 * 所以这里区分两类错误，给不同的人话：
 *  - 动态 chunk 加载失败 → "系统可能已更新，请刷新页面"。这类错误重试单次 import 无意义
 *    （同一个 URL 的模块缓存已被标记失败），刷新才能拿到新的 index.html 与哈希资源。
 *  - 其他渲染异常 → 显示简短原因 + 重试入口，便于教秘自己先脱身、我们再拿日志定位。
 *
 * 刻意不做：自动上报。当前没有前端日志后端，加了也只能写 console；等有渠道再接。
 */

/** 各浏览器对「动态 import 失败」的措辞各不相同，这里统一识别。 */
const CHUNK_ERROR_PATTERNS = [
  'failed to fetch dynamically imported module', // Chrome / Edge
  'importing a module script failed',            // Safari
  'error loading dynamically imported module',   // Firefox
  'chunkloaderror',                              // webpack 系（历史产物兜底）
  'loading chunk',                               // 同上
];

export function isChunkLoadError(err: unknown): boolean {
  const msg = String((err as { message?: string } | null)?.message ?? err ?? '').toLowerCase();
  return CHUNK_ERROR_PATTERNS.some((p) => msg.includes(p));
}

interface Props {
  children: ReactNode;
  /** 出错视图的名字，用于展示「哪个模块出错了」。 */
  label?: string;
}

interface State {
  error: Error | null;
}

class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    // 只写 console：现场排障靠它，且不会污染 UI。
    // 注意 probe 统计的是 pageerror，console.error 不计入"JS 错误"。
    console.error('[view-error]', this.props.label ?? '', error, info.componentStack);
  }

  private handleRetry = () => {
    const { error } = this.state;
    // chunk 失败：重挂载同一个边界没有用（模块缓存已是失败态），必须重新拉 index.html。
    if (isChunkLoadError(error)) {
      window.location.reload();
      return;
    }
    this.setState({ error: null });
  };

  render() {
    const { error } = this.state;
    if (!error) return this.props.children;

    const chunk = isChunkLoadError(error);
    return (
      <div className="flex flex-col items-center justify-center py-16 px-4">
        <div className="max-w-lg w-full bg-white border border-red-200 rounded-lg shadow-sm p-6 text-center">
          <div className="text-3xl mb-2">{chunk ? '🔄' : '⚠️'}</div>
          <h2 className="text-base font-semibold text-gray-800 mb-1">
            {chunk ? '页面资源已更新，需要重新加载' : '这个模块遇到了问题'}
          </h2>
          <p className="text-sm text-gray-500 mb-4 leading-relaxed">
            {chunk
              ? '系统刚刚更新过，或网络在加载过程中中断。点击下方按钮刷新即可继续，已提交的调课申请不受影响。'
              : '问题只影响当前这个页面，顶部的其他功能仍可正常使用。可以先重试，或刷新页面。'}
          </p>
          <div className="flex items-center justify-center gap-2">
            <button
              onClick={this.handleRetry}
              className="px-4 py-2 bg-blue-600 hover:bg-blue-700 text-white text-sm rounded-md"
            >
              {chunk ? '重新加载' : '重试'}
            </button>
            {!chunk && (
              <button
                onClick={() => window.location.reload()}
                className="px-4 py-2 bg-gray-100 hover:bg-gray-200 text-gray-700 text-sm rounded-md"
              >
                刷新页面
              </button>
            )}
          </div>
          {!chunk && (
            <details className="mt-4 text-left">
              <summary className="text-xs text-gray-400 cursor-pointer">技术详情</summary>
              <pre className="mt-2 text-xs text-gray-500 bg-gray-50 rounded p-2 overflow-auto max-h-40 whitespace-pre-wrap">
                {error.message}
              </pre>
            </details>
          )}
        </div>
      </div>
    );
  }
}

export default ErrorBoundary;
