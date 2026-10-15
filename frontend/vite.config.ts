import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  build: {
    // 刻意不叫 dist/build：发布上传会排除「构建产物」（node_modules/.git/dist…），
    // 而本项目由 FastAPI 同源托管，产物必须随源码一起进沙箱，故改用 webui。
    outDir: 'webui',
    emptyOutDir: true,
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
