/// <reference types="vite/client" />

/**
 * 给 `import.meta.env` 之外的项目级全局类型补丁。
 *
 * 当前只有一条：**测试里读文件/路径**要用到 `node:fs` / `node:path`。
 * 项目没装 `@types/node`（也不该为一条体积守卫把它拉进生产依赖图），
 * 所以这里只声明用到的那几个符号 —— 够用、且不会把整套 Node 全局塞进前端类型环境。
 *
 * 为什么非要走这道声明：`npm run build` 与 CI 都跑 `tsc -b`，缺类型就是硬红
 * （`TS2307: Cannot find module 'node:fs'`）。而 `vitest` 用 esbuild 转译**不做类型检查**，
 * 只跑 `npm test` 会看到全绿 —— 这个差异本轮实测踩到过一次。
 */
declare module 'node:fs' {
  const fs: {
    readFileSync(path: string, encoding: 'utf-8'): string;
  };
  export default fs;
}

declare module 'node:path' {
  const path: {
    resolve(...parts: string[]): string;
    dirname(p: string): string;
  };
  export default path;
}

declare const __dirname: string;
