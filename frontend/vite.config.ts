import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  build: {
    // 刻意不叫 dist/build：发布上传会排除「构建产物」（node_modules/.git/dist…），
    // 而本项目由 FastAPI 同源托管，产物必须随源码一起进沙箱，故改用 webui。
    // ⚠️ 改回 dist 的后果：本地 dev 一切正常（vite dev 不走 outDir），
    // 线上却因产物被当构建产物排除而白屏 —— 交付评审 P2-3 点名的隐式约定，
    // 改这里前先读 docs/安全风险清单 第二节根因第 2 条。
    outDir: 'webui',
    emptyOutDir: true,
    rollupOptions: {
      output: {
        /**
         * 把体积大且**很少变**的依赖切成独立 chunk：
         *  - 文件名带内容哈希 + `/assets/**` 一年 immutable ⇒ 只改业务代码时，
         *    用户不需要重下 React/解析器，这部分直接命中本地缓存。
         *  - 云 SDK 虽在本配置里点名，但它本身就是动态 import 的产物
         *    （`cloud.ts` 的 getCloud()），放在独立 chunk 里更利于定位体积。
         * 注意不要按"页面"切：本项目是单页多视图、视图共享同一份状态，
         * 按页面切只会让公共依赖重复出现在多个 chunk 里。
         *
         * ⚠️ axios 必须单独成块。它由 `api/client.ts` 静态引入（首屏必加载），
         * 而 markdown 解析链只被懒加载的 ConsultView 用到。两者若同处 vendor，
         * 首屏就会因为 axios 把整个 vendor（含 markdown ~160KB）一起拉下来——
         * 实测过这个坑：只做视图懒加载、不动分块策略时，首屏字节数几乎没变。
         */
        manualChunks(id: string) {
          if (!id.includes('node_modules')) return undefined;
          if (id.includes('workbuddy-cloud-sdk')) return 'cloud-sdk';
          if (/[\\/](react|react-dom|scheduler)[\\/]/.test(id)) return 'react-vendor';
          if (/[\\/]axios[\\/]/.test(id)) return 'http';
          // 其余（markdown 解析链等）全归 vendor：它只被懒加载视图引用，
          // 因此这个 chunk 不会出现在首屏关键路径上。
          return 'vendor';
        },
      },
    },
  },
  server: {
    host: '0.0.0.0',  // 监听所有地址，避免仅 IPv6 导致 localhost 拒绝连接
    port: 5173,
    proxy: {
      '/api': {
        // 2026-10-30：本机 7762-8612 大面积落进 Hyper-V 动态保留段无法 bind，
        // 临时改用 9000。重启电脑后保留段会重划，届时可改回 8000 并同步 start 脚本。
        target: 'http://localhost:9000',
        changeOrigin: true,
      },
    },
  },
})
