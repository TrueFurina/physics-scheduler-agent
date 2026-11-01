/**
 * blob → 浏览器下载。
 *
 * 为什么单独一个模块：App.tsx 是页面级 store，体量守卫
 * （src/App.tsx.size.test.ts）卡在 1500 行，而它已经 1510 行。
 * 这段逻辑零状态、零依赖，却被四个导出入口共用
 * （全校方案 / 并行方案 / 标准课表 / 自动排课初稿），
 * 留在组件里纯粹是在占页面 store 的额度。
 *
 * ⚠️ 后端出错时返回的是 JSON，浏览器照样会下成一个 .xlsx，
 * 所以校验走接口侧（导出接口只返回 xlsx），这里不额外猜内容。
 */
export function downloadBlob(data: any, filename: string): void {
  const url = window.URL.createObjectURL(new Blob([data]));
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  window.URL.revokeObjectURL(url);
}
