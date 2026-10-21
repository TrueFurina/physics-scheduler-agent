import React from 'react'
import ReactDOM from 'react-dom/client'
import App from './App'
import './index.css'

// 刻意不用 react-router-dom：本应用是单页多视图（view 状态切换），
// 没有任何多路径/深链需求。挂一个 BrowserRouter 却不注册 Routes，
// 只会让每个访客多下一个用不上的依赖（App.tsx 里 0 处 Routes/useNavigate）。
ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
)
