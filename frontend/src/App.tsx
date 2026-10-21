import { Suspense, lazy, useEffect, useRef, useState } from 'react';
import {
  rescheduleApi, getAdminToken, setAdminToken, Teacher, TeacherCourse, AvailableSlot, ExcludedSlot, RescheduleRequest,
} from './api/client';
import { describeCloudError } from './cloud';
import {
  dispatchAuthCallback, getCloudSession, signOutCloud, sendPhoneCode, verifyPhoneCode,
  signInWithWechat, askCloudLlm, logConsult, listConsultLogs,
  listCloudRequests, createCloudRequest, approveCloudRequest,
  type PendingPhoneOtp, type CloudConsultLog, type ConsultModuleKey,
} from './cloudApi';
import { withRetry, whenIdle } from './utils/async';
import ErrorBoundary from './components/ErrorBoundary';
import type { View, ImpactResult } from './types';
import IdentityView from './views/IdentityView';
import CoursesView from './views/CoursesView';
import RescheduleView from './views/RescheduleView';
import AdminView from './views/AdminView';
// 三个「附加模块」按需加载：教秘主路径（选身份 → 我的课程 → 调课 → 审批）不碰它们，
// 却各自带着独立依赖（并行排课的沙盘、看板的统计条、咨询的 react-markdown）。
// 静态 import 会把它们全部塞进首屏包；改成 import() 后各成一个 chunk，首屏只付主路径的钱。
const ParallelView = lazy(() => import('./views/ParallelView'));
const DashboardView = lazy(() => import('./views/DashboardView'));
const ConsultView = lazy(() => import('./views/ConsultView'));

/** 懒加载视图的占位：与视图等高的骨架，避免切换时页面高度塌陷导致跳动。 */
function ViewFallback() {
  return (
    <div className="flex flex-col items-center justify-center py-16">
      <div className="w-8 h-8 border-4 border-blue-200 border-t-blue-600 rounded-full animate-spin mb-3" />
      <div className="text-sm text-gray-400">模块加载中...</div>
    </div>
  );
}



function App() {
  const [view, setView] = useState<View>('identity');
  const [module, setModule] = useState('');  // 当前咨询模块: course_select / tutor
  const [teachers, setTeachers] = useState<Teacher[]>([]);
  const [currentTeacher, setCurrentTeacher] = useState<string>('');
  const [courses, setCourses] = useState<TeacherCourse[]>([]);
  const [selectedCourse, setSelectedCourse] = useState<TeacherCourse | null>(null);
  const [available, setAvailable] = useState<AvailableSlot[]>([]);
  const [excluded, setExcluded] = useState<ExcludedSlot[]>([]);
  const [showAllAvail, setShowAllAvail] = useState(false);  // 可用时段展开更多
  const [targetWeek, setTargetWeek] = useState(2);  // 模板表单：目标周次
  const [loading, setLoading] = useState(false);
  const [msg, setMsg] = useState('');
  const [dataLoaded, setDataLoaded] = useState(false);
  const [requestStatus, setRequestStatus] = useState('');  // 申请提交后的状态反馈
  const [requests, setRequests] = useState<RescheduleRequest[]>([]);  // 审批台申请列表
  const [nlInput, setNlInput] = useState('');  // 自然语言调课输入
  const [followupQ, setFollowupQ] = useState<string[]>([]);  // 多轮追问问题
  const [nlMsg, setNlMsg] = useState('');  // 自然语言反馈消息
  const [consultQ, setConsultQ] = useState('');  // 选课/辅导咨询输入
  const [consultMsg, setConsultMsg] = useState('');  // 咨询回复
  const [autoScheduleMsg, setAutoScheduleMsg] = useState('');  // 自动排课结果
  const [autoScheduleLoading, setAutoScheduleLoading] = useState(false);  // 自动排课中
  const [showAdminLogin, setShowAdminLogin] = useState(false);  // 管理员登录弹窗
  // 管理员登录态：以客户端持有的服务端令牌为准（刷新后从 sessionStorage 恢复）。
  // 不能写死 false——否则刷新页面会出现"令牌还在但 UI 认为没登录"的错位状态。
  const [adminAuthed, setAdminAuthed] = useState(() => Boolean(getAdminToken()));
  const [adminUser, setAdminUser] = useState('');  // 管理员账号
  const [adminPass, setAdminPass] = useState('');  // 管理员密码
  const [adminLoginErr, setAdminLoginErr] = useState('');  // 登录错误
  const [impact, setImpact] = useState<ImpactResult | null>(null);  // 影响分析结果
  const [impactLoading, setImpactLoading] = useState(false);  // 影响分析中
  // 并行排课（访谈核心需求：所有实验项目一次列出，一键排定）
  const [parallelCourses, setParallelCourses] = useState<Array<{ class_name: string; weekday: string; projects: Array<{ project: string; groups: string[] }>; total_units: number }>>([]);
  const [parallelLoading, setParallelLoading] = useState(false);  // 总览加载中
  const [parallelSolving, setParallelSolving] = useState(false);  // 求解中
  const [parallelResult, setParallelResult] = useState<any>(null);  // 求解结果
  const [parallelSelected, setParallelSelected] = useState('');  // 当前选中课程

  // ---- 云服务：登录态 ----
  const [session, setSession] = useState<any>(null);        // 云登录会话（null = 未登录）
  const [showLogin, setShowLogin] = useState(false);        // 云登录弹窗
  const [loginPhone, setLoginPhone] = useState('');
  const [loginCode, setLoginCode] = useState('');
  const [phoneErr, setPhoneErr] = useState('');             // 手机号校验错误
  const [loginErr, setLoginErr] = useState('');             // 登录整体错误
  const [loginBusy, setLoginBusy] = useState(false);
  const [codeSent, setCodeSent] = useState(false);          // 是否已发码
  const [loginIntent, setLoginIntent] = useState<'admin' | 'submit' | 'history' | null>(null);
  const pendingOtpRef = useRef<PendingPhoneOtp | null>(null);  // 发码结果，跨事件保留

  // ---- 云服务：AI 咨询（云大模型 + 咨询记录）----
  const consultAbortRef = useRef<AbortController | null>(null);  // 用于"停止生成"
  const [consultLogs, setConsultLogs] = useState<CloudConsultLog[]>([]);
  const [showLogs, setShowLogs] = useState(false);

  const consultKey: ConsultModuleKey = module === 'course_select' ? 'course_select' : 'tutor';
  const userName: string | null = session?.user?.name ?? null;

  // 云端调课申请 → 界面结构（与原后端返回字段保持一致）
  const toUiRequest = (r: any): RescheduleRequest => ({
    id: r.id,
    teacher: r.teacher,
    course_name: r.course_name,
    class_name: r.class_name,
    original_time: r.original_time,
    target_week: r.target_week,
    reason: r.reason ?? '',
    status: r.status,
    admin_comment: r.admin_comment ?? '',
    created_at: r.created_at,
  });

  // 应用启动：先处理第三方登录回调（必须在任何路由守卫之前），再恢复已有登录态
  useEffect(() => {
    const boot = async () => {
      try {
        const cb = await dispatchAuthCallback();
        if (cb.error) {
          // 授权失败/取消时用户已经回到站点，必须把原因摆到明面上：
          // 只写进 loginErr 是看不见的（弹窗默认是关的）→ 表现为「点了登录没任何反应」。
          setLoginErr(cb.error);
          setMsg(cb.error);
          setShowLogin(true);
        }
        if (cb.session) {
          setSession(cb.session);
        } else if (!cb.handled) {
          // 会话恢复推到**首屏空闲之后**：它会拉起云 SDK（独立 chunk ~70KB）。
          // 教秘主路径不碰云接口，没必要让它和主包抢首屏带宽；
          // 真需要云能力时（点登录/提交申请）getCloud() 仍走同一个共享 Promise，
          // 提前/延后都只下载一次，不会重复请求。
          whenIdle(async () => {
            try {
              const restored = await getCloudSession();
              if (restored) setSession(restored);
            } catch {
              /* 云端暂时不可达不影响本地功能 */
            }
          });
        }
      } catch {
        // 首屏不因云端暂时不可达而中断
      }
    };
    boot();
  }, []);

  // 并行排课：打开模块并加载课程总览
  const handleOpenParallel = async () => {
    setView('parallel');
    setParallelResult(null);
    setParallelLoading(true);
    try {
      const r = await withRetry(() => rescheduleApi.parallelOverview());
      setParallelCourses(r.data.data.courses || []);
    } catch (err: any) {
      setMsg(`加载课程总览失败：${err.response?.data?.detail || err.message}（可刷新页面重试）`);
    } finally {
      setParallelLoading(false);
    }
  };

  // 并行排课：一键求解（CP-SAT 统一分配 项目×组别 → 周次/时段/场地）
  const [parallelWeekday, setParallelWeekday] = useState('');  // 上课星期选择（''=自动沿用历史）
  const [parallelViewWeek, setParallelViewWeek] = useState<number | null>(null);  // 结果沙盘查看的周
  const handleParallelSolve = async (className: string, weekday?: string) => {
    setParallelSelected(className);
    setParallelSolving(true);
    setParallelResult(null);
    try {
      const r = await rescheduleApi.parallelSolve(className, weekday ?? (parallelWeekday || undefined));
      if (r.data.success) {
        setParallelResult(r.data);
        const wks = Object.keys(r.data.by_week || {}).map(Number).sort((a, b) => a - b);
        setParallelViewWeek(wks[0] ?? null);
      } else {
        setMsg(r.data.error || '并行排课失败');
      }
    } catch (err: any) {
      setMsg(`并行排课失败：${err.response?.data?.detail || err.message}`);
    } finally {
      setParallelSolving(false);
    }
  };

  // 切换上课星期（已选课程时立即用新星期重排）
  const handleParallelWeekdayChange = (w: string) => {
    setParallelWeekday(w);
    if (parallelSelected) handleParallelSolve(parallelSelected, w || undefined);
  };

  // 并行排课：方案导出 Excel（blob 触发浏览器下载）
  const handleParallelExport = async () => {
    if (!parallelResult?.schedule) return;
    try {
      const r = await rescheduleApi.parallelExport(parallelSelected, parallelResult.schedule);
      const url = window.URL.createObjectURL(new Blob([r.data]));
      const a = document.createElement('a');
      a.href = url;
      a.download = `并行排课方案_${parallelSelected}.xlsx`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      window.URL.revokeObjectURL(url);
    } catch (err: any) {
      setMsg(`导出失败：${err.response?.data?.detail || err.message}`);
    }
  };

  // 数据看板：体检报告（扫描现有安排表冲突）
  const [auditData, setAuditData] = useState<any>(null);
  const [auditLoading, setAuditLoading] = useState(false);
  const [statsData, setStatsData] = useState<any>(null);

  const handleOpenDashboard = async () => {
    setView('dashboard');
    setAuditLoading(true);
    try {
      const r = await rescheduleApi.dataAudit();
      setAuditData(r.data.data);
    } catch (err: any) {
      setMsg(`数据体检失败：${err.response?.data?.detail || err.message}`);
    } finally {
      setAuditLoading(false);
    }
    // 统计面板（失败不影响体检展示）
    try {
      const s = await rescheduleApi.stats();
      setStatsData(s.data.data);
    } catch {
      setStatsData(null);
    }
  };

  // 冲突影响分析（差异化功能）：查看调课影响范围
  const handleImpactAnalysis = async () => {
    if (!selectedCourse) return;
    setImpactLoading(true);
    setImpact(null);
    try {
      const r = await rescheduleApi.impactAnalysis(
        `我是${currentTeacher}，把${selectedCourse.course_name}调到第${targetWeek}周`
      );
      setImpact(r.data.data);
    } catch (err: any) {
      setMsg(`影响分析失败：${err.response?.data?.detail || err.message}`);
    } finally {
      setImpactLoading(false);
    }
  };

  // P1: 排课自动生成
  const handleAutoSchedule = async () => {
    setAutoScheduleLoading(true);
    setAutoScheduleMsg('');
    try {
      const r = await rescheduleApi.autoSchedule();
      setAutoScheduleMsg(r.data.message);
    } catch (err: any) {
      setAutoScheduleMsg(`❌ 自动排课失败：${err.response?.data?.detail || err.message}`);
    } finally {
      setAutoScheduleLoading(false);
    }
  };

  // ---- 云服务：登录相关 ----
  const openLogin = (intent: 'admin' | 'submit' | 'history' | null) => {
    setLoginIntent(intent);
    setLoginErr('');
    setShowLogin(true);
  };

  // 登录成功后按触发来源继续原动作
  const finishLogin = (s: any) => {
    setSession(s);
    setShowLogin(false);
    setLoginCode('');
    setCodeSent(false);
    pendingOtpRef.current = null;
    const intent = loginIntent;
    setLoginIntent(null);
    if (intent === 'admin') {
      setShowAdminLogin(true);          // 审批台还需要管理员口令
    } else if (intent === 'submit') {
      setMsg('已登录，请再次点击「提交调课申请」');
    } else if (intent === 'history') {
      setMsg('已登录，可查看云端咨询历史');
    }
  };

  // 发码：只在「获取验证码」按钮里调用
  const handleSendCode = async () => {
    if (!/^1\d{10}$/.test(loginPhone)) {
      setPhoneErr('请输入正确的 11 位手机号');
      return;
    }
    setPhoneErr('');
    setLoginErr('');
    setLoginBusy(true);
    try {
      pendingOtpRef.current = await sendPhoneCode(loginPhone);
      setCodeSent(true);
    } catch (err: any) {
      setLoginErr(err?.message || '验证码发送失败');
    } finally {
      setLoginBusy(false);
    }
  };

  // 验码：复用已保存的 challenge，绝不在这里重新发码
  const handlePhoneLogin = async () => {
    const pending = pendingOtpRef.current;
    if (!pending || pending.phone !== loginPhone) {
      setLoginErr('请先获取当前手机号的验证码');
      return;
    }
    if (!loginCode.trim()) {
      setLoginErr('请输入验证码');
      return;
    }
    setLoginBusy(true);
    setLoginErr('');
    try {
      const s = await verifyPhoneCode(pending, loginCode.trim());
      finishLogin(s);
    } catch (err: any) {
      setLoginErr(err?.message || '验证码校验失败');
    } finally {
      setLoginBusy(false);
    }
  };

  const handleWechatLogin = async () => {
    setLoginErr('');
    try {
      await signInWithWechat();   // 同标签页跳转，回调回到 location.origin
    } catch (err: any) {
      setLoginErr(err?.message || '微信登录发起失败');
    }
  };

  const handleCloudSignOut = async () => {
    try {
      await signOutCloud();
    } catch {
      // 退出失败不影响本地态清理
    }
    setSession(null);
    setAdminAuthed(false);
    setAdminToken(null);   // 一并作废管理员会话令牌，避免登出后残留可用的写权限
    setRequests([]);
    setConsultLogs([]);
    setShowLogs(false);
    if (view === 'admin') setView('identity');
  };

  // 从云端读取调课申请（status 为空表示全部）
  const loadCloudRequests = async (status?: string) => {
    const rows = await listCloudRequests(status);
    setRequests(rows.map(toUiRequest));
    return rows;
  };

  // 云服务：选课咨询 / 实验辅导（免密钥大模型，流式输出，可中途停止）
  const handleConsult = async () => {
    const q = consultQ.trim();
    if (!q) return;
    const controller = new AbortController();
    consultAbortRef.current = controller;
    setLoading(true);
    setConsultMsg('');
    try {
      const answer = await askCloudLlm(consultKey, q, (full) => setConsultMsg(full), controller.signal);
      // 已登录时把问答落到云端（按用户隔离）；记录失败不影响回答展示
      if (session && answer) {
        try {
          await logConsult(consultKey, q, answer, userName);
        } catch {
          // 忽略：记录失败不应打断咨询
        }
      }
    } catch (err: any) {
      if (err?.name === 'AbortError') {
        // 用户主动停止：保留已生成内容，不报错
      } else {
        const hint = describeCloudError(err);
        setConsultMsg((prev) => (prev ? `${prev}\n\n---\n⚠️ 生成中断：${hint}` : `❌ ${hint}`));
      }
    } finally {
      consultAbortRef.current = null;
      setLoading(false);
      setConsultQ('');
    }
  };

  const handleStopConsult = () => {
    consultAbortRef.current?.abort();
  };

  // 云端咨询历史（需登录，按用户隔离）
  const handleToggleLogs = async () => {
    if (!session) {
      openLogin('history');
      return;
    }
    const next = !showLogs;
    setShowLogs(next);
    if (!next) return;
    try {
      setConsultLogs(await listConsultLogs(consultKey));
    } catch (err: any) {
      setMsg(`加载云端咨询历史失败：${describeCloudError(err)}`);
      setConsultLogs([]);
    }
  };


  // 打开选课咨询 / 实验辅导模块
  const handleOpenModule = (m: string) => {
    setModule(m);
    setConsultMsg('');
    setConsultQ('');
    setView(m === 'course_select' ? 'course_select' : 'tutor');
  };

  // 进入时自动加载默认数据
  // 后端在数据未加载时也会自愈（_ensure_loaded），这里重试是为了覆盖"沙箱冷启动导致
  // 第一次请求直接超时/5xx"的情况 —— 那种情况下后端根本收不到请求，只能由前端重试。
  //
  // 先问 /status 再决定是否 load-default：排课数据在进程内存里，
  // 页面刷新（数据仍在）时无条件 load-default 会把 433 行 Excel **整表重解析一遍**
  // （本地实测 0.98s CPU / 冷沙箱 3.8s），纯属白烧 —— 而且它还是限流桶里的一员。
  useEffect(() => {
    const init = async () => {
      try {
        const t = await withRetry(async () => {
          const st = await rescheduleApi.getStatus();
          if (!st.data.loaded) await rescheduleApi.loadDefault();
          return rescheduleApi.getTeachers();
        });
        setTeachers(t.data.teachers);
        setDataLoaded(true);
      } catch (err: any) {
        setMsg(`加载失败：${err.response?.data?.detail || err.message}（可刷新页面重试）`);
      }
    };
    init();
  }, []);

  // F1: 选择教师身份 → 加载其课程列表（F2）
  const handleSelectTeacher = async (name: string) => {
    setLoading(true);
    try {
      const r = await rescheduleApi.getTeacherCourses(name);
      setCourses(r.data.courses);
      setCurrentTeacher(name);
      setSelectedCourse(null);
      setAvailable([]);
      setExcluded([]);
      setView('courses');
    } catch (err: any) {
      setMsg(`加载课程失败：${err.response?.data?.detail || err.message}`);
    } finally {
      setLoading(false);
    }
  };

  // F3/F4: 模板表单选择课程 + 目标周次 → 查询可用时段（含原因标注）
  // fast=true 时跳过 LLM 候选方案（沙盘秒回）；首次/手动查候选时 fast=false
  const handleReschedule = async (course: TeacherCourse, week?: number, fast: boolean = false) => {
    setSelectedCourse(course);
    setLoading(true);
    try {
      const target = week ?? targetWeek;
      const r = await rescheduleApi.chat(
        `我是${currentTeacher}，把${course.course_name}调到第${target}周`,
        fast
      );
      setAvailable(r.data.data.available_slots || []);
      setExcluded(r.data.data.excluded_slots || []);
      setView('reschedule');
    } catch (err: any) {
      setMsg(`查询失败：${err.response?.data?.detail || err.message}`);
    } finally {
      setLoading(false);
    }
  };

  // 模板表单：更改目标周次后重新查询（fast 模式——秒回沙盘，不等待 LLM 候选方案）
  const handleTargetWeekChange = (week: number) => {
    setTargetWeek(week);
    if (selectedCourse) {
      handleReschedule(selectedCourse, week, true);  // fast=true 跳过 LLM
    }
  };

  // F6: 提交调课申请（云端持久化，需登录）
  const handleSubmitRequest = async () => {
    if (!selectedCourse) return;
    if (!session) {
      openLogin('submit');
      return;
    }
    setLoading(true);
    try {
      await createCloudRequest({
        teacher: currentTeacher,
        course_name: selectedCourse.course_name,
        class_name: selectedCourse.class_name,
        original_time: `${selectedCourse.original_time} · ${selectedCourse.room}`,
        target_week: targetWeek,
        reason: '调课申请',
      }, userName);
      setRequestStatus('✅ 调课申请已提交到云端，等待管理员审批');
    } catch (err: any) {
      setMsg(`提交失败：${describeCloudError(err)}`);
    } finally {
      setLoading(false);
    }
  };

  // B1/B2: 自然语言调课（支持多轮追问——信息缺失时系统追问）
  const handleNlReschedule = async () => {
    const text = nlInput.trim();
    if (!text) return;
    setLoading(true);
    setNlMsg('');
    try {
      const r = await rescheduleApi.chat(text);
      const data = r.data.data;
      // 需要追问（信息缺失）
      if (data.need_info && data.followup_questions?.length) {
        setFollowupQ(data.followup_questions);
        setNlMsg(`🤔 ${r.data.message}`);
        return;
      }
      setFollowupQ([]);
      // 有可用时段 → 展示结果
      if (data.available_slots?.length) {
        setAvailable(data.available_slots);
        setExcluded(data.excluded_slots || []);
        setNlMsg(`✅ ${r.data.message.split('\n')[0]}`);
        setView('reschedule');
        return;
      }
      setNlMsg(r.data.message);
    } catch (err: any) {
      setNlMsg(`❌ 请求失败：${err.response?.data?.detail || err.message}`);
    } finally {
      setLoading(false);
      setNlInput('');
    }
  };

  // 返回身份选择
  const handleBackToIdentity = () => {
    setView('identity');
    setCurrentTeacher('');
    setCourses([]);
  };

  // F6: 管理员进入审批台（需云登录 + 管理员口令）
  const handleOpenAdmin = async () => {
    if (!session) {
      openLogin('admin');   // 第一步：云身份
      return;
    }
    if (!adminAuthed) {
      setShowAdminLogin(true);  // 第二步：管理员口令
      return;
    }
    setLoading(true);
    try {
      await loadCloudRequests('pending');
      setView('admin');
    } catch (err: any) {
      setMsg(`加载审批列表失败：${describeCloudError(err)}`);
    } finally {
      setLoading(false);
    }
  };

  // 管理员登录提交（云身份之后的审批授权）
  //
  // 口令比对已移到服务端（`/api/reschedule/admin-login`）：前端不再内置任何明文口令，
  // 成功时服务端签发会话令牌，后续写接口凭该令牌放行。
  const handleAdminLogin = async () => {
    setAdminLoginErr('');
    if (!adminUser.trim() || !adminPass) {
      setAdminLoginErr('请输入管理员账号和密码');
      return;
    }
    setLoading(true);
    try {
      const res = await rescheduleApi.adminLogin(adminUser.trim(), adminPass);
      setAdminToken(res.data.token);
      setAdminAuthed(true);
      setShowAdminLogin(false);
      await loadCloudRequests('pending');
      setView('admin');
    } catch (err: any) {
      // 401 才叫「口令错」；其余（网络/500）不能误导成密码问题。
      if (err?.response?.status === 401) {
        setAdminLoginErr(err?.response?.data?.detail || '管理员账号或密码错误');
      } else {
        setMsg(`管理员登录失败：${err?.response?.data?.detail || err?.message || '请稍后重试'}`);
      }
    } finally {
      setLoading(false);
    }
  };

  // F6: 管理员审批（云端更新状态）
  const handleApprove = async (id: number, approve: boolean, comment: string) => {
    setLoading(true);
    try {
      await approveCloudRequest(id, approve, comment, userName);
      await loadCloudRequests('pending');
    } catch (err: any) {
      setMsg(`审批失败：${describeCloudError(err)}`);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="min-h-screen bg-gray-50">
      <header className="bg-white border-b border-gray-200">
        <div className="max-w-4xl mx-auto px-4 py-4 flex items-center justify-between">
          <div>
            <h1 className="text-xl font-bold text-gray-900">物理实验调课辅助</h1>
            <p className="text-sm text-gray-500">先选身份 → 看我的课程 → 调课</p>
          </div>
          <div className="flex items-center gap-2">
            {view !== 'identity' && (
              <button
                onClick={handleBackToIdentity}
                className="text-sm px-4 py-2 bg-gray-100 text-gray-700 rounded-lg hover:bg-gray-200 transition-colors"
              >
                ← 返回身份选择
              </button>
            )}
            {session ? (
              <>
                <span className="text-sm px-3 py-2 bg-emerald-50 text-emerald-700 rounded-lg border border-emerald-200 whitespace-nowrap">
                  ☁️ {userName || '已登录'}
                </span>
                <button
                  onClick={handleCloudSignOut}
                  className="text-sm px-3 py-2 bg-gray-100 text-gray-600 rounded-lg hover:bg-gray-200 transition-colors"
                >
                  退出
                </button>
              </>
            ) : (
              <button
                onClick={() => openLogin(null)}
                className="text-sm px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 transition-colors whitespace-nowrap"
              >
                ☁️ 云端登录
              </button>
            )}
          </div>
        </div>

        {/* 步骤指示器（评审：缺少用户引导） */}
        <div className="max-w-4xl mx-auto px-4 pb-3">
          <div className="flex items-center gap-1 text-xs">
            {[
              { key: 'identity', label: '① 选择身份' },
              { key: 'courses', label: '② 我的课程' },
              { key: 'reschedule', label: '③ 选择时间' },
              { key: 'admin', label: '④ 管理员审批' },
            ].map((s, i) => (
              <div key={s.key} className="flex items-center gap-1">
                {i > 0 && <span className="text-gray-300">›</span>}
                <span
                  className={`px-2 py-1 rounded-full ${
                    (view === s.key) || (view === 'reschedule' && s.key === 'reschedule')
                      ? 'bg-blue-100 text-blue-700 font-medium'
                      : 'text-gray-400'
                  }`}
                >
                  {s.label}
                </span>
              </div>
            ))}
          </div>
        </div>

        {/* 四大模块导航（排课/选课/调课/辅导） */}
        <div className="max-w-4xl mx-auto px-4 pb-3">
          <div className="flex items-center gap-2 text-sm">
            <button
              onClick={() => setView('identity')}
              className={`px-4 py-2 rounded-lg transition-colors ${
                ['identity', 'courses', 'reschedule'].includes(view) && !['course_select', 'tutor', 'admin'].includes(view)
                  ? 'bg-blue-600 text-white' : 'bg-gray-100 text-gray-600 hover:bg-gray-200'
              }`}
            >
              📅 排课调课
            </button>
            <button
              onClick={handleOpenParallel}
              className={`px-4 py-2 rounded-lg transition-colors ${
                view === 'parallel' ? 'bg-cyan-600 text-white' : 'bg-gray-100 text-gray-600 hover:bg-gray-200'
              }`}
            >
              ⚡ 并行排课
            </button>
            <button
              onClick={handleOpenDashboard}
              className={`px-4 py-2 rounded-lg transition-colors ${
                view === 'dashboard' ? 'bg-teal-600 text-white' : 'bg-gray-100 text-gray-600 hover:bg-gray-200'
              }`}
            >
              📊 数据看板
            </button>
            <button
              onClick={() => handleOpenModule('course_select')}
              className={`px-4 py-2 rounded-lg transition-colors ${
                view === 'course_select' ? 'bg-green-600 text-white' : 'bg-gray-100 text-gray-600 hover:bg-gray-200'
              }`}
            >
              🎓 选课咨询
            </button>
            <button
              onClick={() => handleOpenModule('tutor')}
              className={`px-4 py-2 rounded-lg transition-colors ${
                view === 'tutor' ? 'bg-purple-600 text-white' : 'bg-gray-100 text-gray-600 hover:bg-gray-200'
              }`}
            >
              🔬 实验辅导
            </button>
            <button
              onClick={handleOpenAdmin}
              className={`px-4 py-2 rounded-lg transition-colors ${
                view === 'admin' ? 'bg-indigo-600 text-white' : 'bg-gray-100 text-gray-600 hover:bg-gray-200'
              }`}
            >
              ⚙️ 审批
            </button>
          </div>
        </div>
      </header>

      {msg && (
        <div className="max-w-4xl mx-auto px-4 pt-3">
          <div className="bg-red-50 text-red-600 text-sm px-4 py-2.5 rounded-lg border border-red-200 flex items-start gap-3">
            <span className="flex-1 break-words">{msg}</span>
            <button
              type="button"
              onClick={() => setMsg('')}
              aria-label="关闭提示"
              className="shrink-0 text-red-400 hover:text-red-600 leading-none"
            >
              ✕
            </button>
          </div>
        </div>
      )}

      {/* 云端登录弹窗（手机验证码 / 微信） */}
      {showLogin && (
        <div className="fixed inset-0 bg-black/40 z-50 flex items-center justify-center p-4">
          <div className="bg-white rounded-2xl shadow-xl p-6 w-full max-w-sm">
            <h3 className="text-lg font-semibold text-gray-900 mb-1">☁️ 云端登录</h3>
            <p className="text-sm text-gray-500 mb-4">
              登录后可提交调课申请、进入审批台、查看咨询历史
            </p>

            <div className="space-y-3">
              <div>
                <input
                  type="tel"
                  value={loginPhone}
                  onChange={(e) => { setLoginPhone(e.target.value); setPhoneErr(''); }}
                  placeholder="手机号"
                  className="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm focus:ring-2 focus:ring-blue-500 outline-none"
                />
                {phoneErr && <div className="text-xs text-red-500 mt-1">{phoneErr}</div>}
              </div>

              <div className="flex gap-2">
                <input
                  type="text"
                  value={loginCode}
                  onChange={(e) => setLoginCode(e.target.value)}
                  onKeyDown={(e) => e.key === 'Enter' && handlePhoneLogin()}
                  placeholder="短信验证码"
                  className="flex-1 min-w-0 px-3 py-2 border border-gray-300 rounded-lg text-sm focus:ring-2 focus:ring-blue-500 outline-none"
                />
                <button
                  type="button"
                  onClick={handleSendCode}
                  disabled={loginBusy}
                  className="px-3 py-2 bg-gray-100 text-gray-700 rounded-lg hover:bg-gray-200 disabled:opacity-50 text-sm whitespace-nowrap"
                >
                  {codeSent ? '重新获取' : '获取验证码'}
                </button>
              </div>

              {loginErr && (
                <div className="text-sm text-red-500 bg-red-50 px-3 py-2 rounded-lg">{loginErr}</div>
              )}

              <button
                onClick={handlePhoneLogin}
                disabled={loginBusy}
                className="w-full px-4 py-2.5 bg-blue-600 text-white rounded-lg hover:bg-blue-700 disabled:opacity-50 text-sm font-medium"
              >
                {loginBusy ? '处理中...' : '登录 / 注册'}
              </button>

              <div className="flex items-center gap-2 text-xs text-gray-400">
                <div className="flex-1 h-px bg-gray-200" />
                或
                <div className="flex-1 h-px bg-gray-200" />
              </div>

              <button
                onClick={handleWechatLogin}
                disabled={loginBusy}
                className="w-full px-4 py-2.5 bg-green-600 text-white rounded-lg hover:bg-green-700 disabled:opacity-50 text-sm font-medium"
              >
                微信扫码登录
              </button>

              <button
                onClick={() => { setShowLogin(false); setLoginErr(''); }}
                className="w-full px-4 py-2 bg-gray-100 text-gray-600 rounded-lg hover:bg-gray-200 text-sm"
              >
                取消
              </button>
            </div>
          </div>
        </div>
      )}

      {/* 管理员登录弹窗（F6：审批台需登录，普通用户免登录） */}
      {showAdminLogin && (
        <div className="fixed inset-0 bg-black/40 z-50 flex items-center justify-center p-4">
          <div className="bg-white rounded-2xl shadow-xl p-6 w-full max-w-sm">
            <h3 className="text-lg font-semibold text-gray-900 mb-1">🔐 管理员登录</h3>
            <p className="text-sm text-gray-500 mb-4">
              云端身份：{userName || '已登录'} ✓　请输入管理员口令进入审批台
            </p>
            <div className="space-y-3">
              <input
                type="text"
                value={adminUser}
                onChange={(e) => setAdminUser(e.target.value)}
                placeholder="账号"
                className="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm focus:ring-2 focus:ring-blue-500 outline-none"
              />
              <input
                type="password"
                value={adminPass}
                onChange={(e) => setAdminPass(e.target.value)}
                onKeyDown={(e) => e.key === 'Enter' && handleAdminLogin()}
                placeholder="密码"
                className="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm focus:ring-2 focus:ring-blue-500 outline-none"
              />
              {adminLoginErr && (
                <div className="text-sm text-red-500 bg-red-50 px-3 py-2 rounded-lg">{adminLoginErr}</div>
              )}
              <div className="flex gap-2 pt-1">
                <button
                  onClick={handleAdminLogin}
                  disabled={loading}
                  className="flex-1 px-4 py-2.5 bg-blue-600 text-white rounded-lg hover:bg-blue-700 disabled:opacity-50 text-sm font-medium"
                >
                  {loading ? '登录中...' : '登录'}
                </button>
                <button
                  onClick={() => setShowAdminLogin(false)}
                  className="px-4 py-2.5 bg-gray-100 text-gray-600 rounded-lg hover:bg-gray-200 text-sm"
                >
                  取消
                </button>
              </div>
              <p className="text-xs text-gray-400 text-center pt-1">
                管理员口令由系统管理员线下提供
              </p>
            </div>
          </div>
        </div>
      )}

      <main className="max-w-4xl mx-auto p-4">
        {/* 错误边界 key=view：某个视图崩了只影响内容区，切到别的视图自动重置（顶部导航仍可用） */}
        <ErrorBoundary key={view} label={view}>
          {!dataLoaded && loading && (
            <div className="flex flex-col items-center justify-center py-16">
              <div className="w-12 h-12 border-4 border-blue-200 border-t-blue-600 rounded-full animate-spin mb-4" />
              <div className="text-gray-400">⏳ 数据加载中...</div>
            </div>
          )}

          {/* 页面1：身份选择（F1） */}
          {view === 'identity' && (
            <IdentityView
              teachers={teachers}
              autoScheduleLoading={autoScheduleLoading}
              autoScheduleMsg={autoScheduleMsg}
              onOpenAdmin={handleOpenAdmin}
              onAutoSchedule={handleAutoSchedule}
              onSelectTeacher={handleSelectTeacher}
            />
          )}

          {/* 页面2：我的课程（F2） */}
          {view === 'courses' && (
            <CoursesView
              currentTeacher={currentTeacher}
              courses={courses}
              loading={loading}
              nlInput={nlInput}
              nlMsg={nlMsg}
              followupQ={followupQ}
              onNlInputChange={setNlInput}
              onNlSubmit={handleNlReschedule}
              onReschedule={handleReschedule}
            />
          )}

          {/* 页面3：调课结果（F3/F4） */}
          {view === 'reschedule' && selectedCourse && (
            <RescheduleView
              selectedCourse={selectedCourse}
              currentTeacher={currentTeacher}
              targetWeek={targetWeek}
              available={available}
              excluded={excluded}
              showAllAvail={showAllAvail}
              impact={impact}
              impactLoading={impactLoading}
              requestStatus={requestStatus}
              loading={loading}
              onBack={() => setView('courses')}
              onTargetWeekChange={handleTargetWeekChange}
              onToggleShowAll={() => setShowAllAvail(!showAllAvail)}
              onImpactAnalysis={handleImpactAnalysis}
              onSubmit={handleSubmitRequest}
            />
          )}

          {/* 页面4：管理员审批台（F6） */}
          {view === 'admin' && (
            <AdminView
              requests={requests}
              loading={loading}
              onBack={handleBackToIdentity}
              onApprove={handleApprove}
            />
          )}

          {/* 页面5：⚡ 并行排课（访谈核心需求：所有实验项目一次列出，一键排定） */}
          {view === 'parallel' && (
            <Suspense fallback={<ViewFallback />}>
              <ParallelView
                weekday={parallelWeekday}
                courses={parallelCourses}
                selected={parallelSelected}
                loading={parallelLoading}
                solving={parallelSolving}
                result={parallelResult}
                viewWeek={parallelViewWeek}
                onWeekdayChange={handleParallelWeekdayChange}
                onSolve={handleParallelSolve}
                onExport={handleParallelExport}
                onViewWeekChange={setParallelViewWeek}
              />
            </Suspense>
          )}

          {/* 页面6：📊 数据看板（体检报告 + 统计） */}
          {view === 'dashboard' && (
            <Suspense fallback={<ViewFallback />}>
              <DashboardView
                auditLoading={auditLoading}
                auditData={auditData}
                statsData={statsData}
              />
            </Suspense>
          )}

          {/* 页面7：选课咨询 / 实验辅导（云端大模型，流式问答） */}
          {(view === 'course_select' || view === 'tutor') && (
            <Suspense fallback={<ViewFallback />}>
              <ConsultView
                mode={view === 'course_select' ? 'course_select' : 'tutor'}
                question={consultQ}
                answer={consultMsg}
                loading={loading}
                logs={consultLogs}
                showLogs={showLogs}
                onQuestionChange={setConsultQ}
                onAsk={handleConsult}
                onStop={handleStopConsult}
                onToggleLogs={handleToggleLogs}
              />
            </Suspense>
          )}
        </ErrorBoundary>
      </main>
    </div>
  );
}

export default App;
