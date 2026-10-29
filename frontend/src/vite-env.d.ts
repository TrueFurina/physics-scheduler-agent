/// <reference types="vite/client" />

/**
 * `?raw` 导入的兜底声明（R35）。
 *
 * vite/client 已经声明了 `*?raw`，但本项目没在 tsconfig 的 `types` 里引用它，
 * 于是 `import x from '....py?raw'` 在 tsc 下报「找不到模块」。
 * 这里补一条最小的 ambient 声明，让"从后端源码读常量做一致性比对"这类
 * 测试能在 tsc 下通过，而不是靠 `any` 绕过。
 */
declare module '*?raw' {
  const content: string;
  export default content;
}
