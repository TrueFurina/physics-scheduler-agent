import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  build: {
    // 刻意不叫 dist/build：发布上传会排除「构建产物」（node_modules/.git/dist…），
    // 而本项目由 FastAPI 同源托管，产物必须随源码一起进沙箱，故改用 webui。
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
         */
        manualChunks(id: string) {
          if (!id.includes('node_modules')) return undefined;
          if (id.includes('workbuddy-cloud-sdk')) return 'cloud-sdk';
          if (/[\\/](react|react-dom|scheduler)[\\/]/.test(id)) return 'react-vendor';
          // 其余（markdown 解析链、lucide 等）全归 vendor：再细分会与 vendor
          // 互相引用生成「circular chunk」告警，收益仅剩缓存粒度，不值得。
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
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
    },
  },
})
