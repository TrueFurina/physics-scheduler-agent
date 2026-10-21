import { Fragment, useEffect, useRef, useState } from 'react';
import ReactMarkdown from 'react-markdown';
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

const weekdayLabels: Record<string, string> = {
  '周一': '周一', '周二': '周二', '周三': '周三',
  '周四': '周四', '周五': '周五', '周六': '周六', '周日': '周日',
};

const timeSlotLabels: Record<string, string> = {
  '567': '13:30-15:45',
  '678': '15:45-18:00',
  '91011': '18:30-20:45',
  '78': '15:50-18:05',
};

type View = 'identity' | 'courses' | 'reschedule' | 'admin' | 'course_select' | 'tutor' | 'parallel' | 'dashboard';

/**
 * 有界重试：线上由沙箱托管，进程被回收后首个请求可能因冷启动而失败。
 * 原先首屏初始化只请求一次、无重试，一旦那一次失败就永久空白、只能手动刷新页面。
 * 这里做 3 次、最多约 2.3s 的退避重试；仍失败则抛出最后一个错误交调用方展示。
 */
async function withRetry<T>(fn: () => Promise<T>, delaysMs: number[] = [0, 700, 1600]): Promise<T> {
  let lastErr: unknown;
  for (const delay of delaysMs) {
    if (delay > 0) await new Promise((resolve) => setTimeout(resolve, delay));
    try {
      return await fn();
    } catch (err) {
      lastErr = err;
    }
  }
  throw lastErr;
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
  const [impact, setImpact] = useState<{ conflict_count: number; affected_classes: string[]; affected_rooms: string[]; affected_teachers: string[]; affected_students: string[]; conflict_slots: any[] } | null>(null);  // 影响分析结果
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
          const restored = await getCloudSession();
          if (restored) setSession(restored);
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
        {!dataLoaded && loading && (
          <div className="flex flex-col items-center justify-center py-16">
            <div className="w-12 h-12 border-4 border-blue-200 border-t-blue-600 rounded-full animate-spin mb-4" />
            <div className="text-gray-400">⏳ 数据加载中...</div>
          </div>
        )}

        {/* 页面1：身份选择（F1） */}
        {view === 'identity' && (
          <div>
            <div className="flex items-center justify-between mb-4">
              <h2 className="text-lg font-semibold text-gray-900">请选择您的身份（教师）</h2>
              <button
                onClick={handleOpenAdmin}
                className="text-sm px-4 py-2 bg-indigo-600 text-white rounded-lg hover:bg-indigo-700 transition-colors"
              >
                ⚙️ 管理员审批台
              </button>
            </div>
            <p className="text-sm text-gray-500 mb-6">系统将展示您名下的全部课程与可调时段</p>

            {/* P1: 排课自动生成入口 */}
            <div className="bg-white rounded-xl p-5 shadow-sm border border-gray-200 mb-6">
              <div className="flex items-center justify-between">
                <div>
                  <h3 className="font-medium text-gray-900">🤖 排课自动生成</h3>
                  <p className="text-sm text-gray-500 mt-1">基于约束求解器，自动生成完整排课方案（教室/教师/学生三重不冲突）</p>
                </div>
                <button
                  onClick={handleAutoSchedule}
                  disabled={autoScheduleLoading}
                  className="px-5 py-2.5 bg-blue-600 text-white rounded-lg hover:bg-blue-700 disabled:opacity-50 text-sm font-medium transition-colors shrink-0"
                >
                  {autoScheduleLoading ? '⏳ 排课中...' : '一键自动排课'}
                </button>
              </div>
              {autoScheduleMsg && (
                <div className="mt-4 px-4 py-3 bg-gray-50 border border-gray-200 rounded-lg text-sm text-gray-700 whitespace-pre-wrap max-h-64 overflow-y-auto">
                  {autoScheduleMsg}
                </div>
              )}
            </div>

            <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-4">
              {teachers.map((t) => (
                <button
                  key={t.name}
                  onClick={() => handleSelectTeacher(t.name)}
                  className="bg-white rounded-xl p-5 shadow-sm border border-gray-200 hover:border-blue-400 hover:shadow-md transition-all text-left"
                >
                  <div className="w-12 h-12 bg-blue-100 rounded-full flex items-center justify-center mb-3">
                    <span className="text-blue-700 font-bold text-lg">{t.name[0]}</span>
                  </div>
                  <div className="font-semibold text-gray-900">{t.name}</div>
                  <div className="text-xs text-gray-500 mt-1">{t.title}</div>
                  <div className="text-xs text-blue-600 mt-3">进入我的课程 →</div>
                </button>
              ))}
            </div>
            {teachers.length === 0 && (
              <div className="text-center text-gray-400 py-16">暂无教师数据，请先加载实验安排表</div>
            )}
          </div>
        )}

        {/* 页面4：管理员审批台（F6） */}
        {view === 'admin' && (
          <div>
            <div className="flex items-center justify-between mb-6">
              <h2 className="text-lg font-semibold text-gray-900">管理员审批台</h2>
              <button
                onClick={handleBackToIdentity}
                className="text-sm px-3 py-1.5 bg-gray-100 text-gray-600 rounded-lg hover:bg-gray-200"
              >
                ← 返回
              </button>
            </div>

            <div className="mb-4 px-4 py-3 bg-gray-50 border border-gray-200 rounded-lg text-sm text-gray-600">
              待审批申请：{requests.length} 条
            </div>

            {requests.length === 0 && (
              <div className="text-center text-gray-400 py-16">
                <p className="text-4xl mb-3">📭</p>
                <p>暂无待审批的调课申请</p>
                <p className="text-sm mt-2 text-gray-300">教师提交申请后会显示在这里</p>
              </div>
            )}

            <div className="space-y-4">
              {requests.map((r) => (
                <div key={r.id} className="bg-white rounded-xl p-5 shadow-sm border border-gray-200">
                  <div className="flex items-start justify-between">
                    <div>
                      <div className="font-semibold text-gray-900">
                        {r.teacher} · {r.course_name}
                      </div>
                      <div className="text-sm text-gray-500 mt-1">{r.class_name}</div>
                      <div className="text-sm text-gray-500 mt-1">
                        {r.original_time} → <span className="font-medium text-blue-600">第{r.target_week}周</span>
                      </div>
                      <div className="text-xs text-gray-400 mt-2">理由：{r.reason}</div>
                    </div>
                    <div className="flex gap-2 shrink-0">
                      <button
                        onClick={() => handleApprove(r.id, true, '同意调课')}
                        disabled={loading}
                        className="px-4 py-2 bg-green-600 text-white rounded-lg hover:bg-green-700 disabled:opacity-50 text-sm font-medium"
                      >
                        同意
                      </button>
                      <button
                        onClick={() => handleApprove(r.id, false, '时间冲突，请重新选择')}
                        disabled={loading}
                        className="px-4 py-2 bg-red-500 text-white rounded-lg hover:bg-red-600 disabled:opacity-50 text-sm font-medium"
                      >
                        驳回
                      </button>
                    </div>
                  </div>
                </div>
              ))}
            </div>
          </div>
        )}

        {/* 页面2：我的课程（F2） */}
        {view === 'courses' && (
          <div>
            <div className="flex items-center gap-3 mb-6">
              <div className="w-10 h-10 bg-blue-100 rounded-full flex items-center justify-center">
                <span className="text-blue-700 font-bold">{currentTeacher[0]}</span>
              </div>
              <div>
                <h2 className="text-lg font-semibold text-gray-900">当前教师：{currentTeacher}</h2>
                <p className="text-sm text-gray-500">共 {courses.length} 门课程</p>
              </div>
            </div>

            {/* 可调课程 */}
            <h3 className="font-medium text-green-700 mb-3">✅ 可调课程</h3>
            <div className="space-y-3 mb-8">
              {courses.filter(c => c.adjustable).map((c, i) => (
                <div key={i} className="bg-white rounded-xl p-4 shadow-sm border border-gray-200 flex items-center justify-between">
                  <div className="flex-1 min-w-0">
                    <div className="font-semibold text-gray-900">{c.course_name}</div>
                    <div className="text-sm text-gray-500 mt-1">{c.class_name}</div>
                    <div className="text-xs text-gray-400 mt-1">
                      {c.original_time} · {c.room} · {c.week_range}
                    </div>
                  </div>
                  <button
                    onClick={() => handleReschedule(c)}
                    className="ml-4 px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 text-sm font-medium transition-colors shrink-0"
                  >
                    申请调课 →
                  </button>
                </div>
              ))}
              {courses.filter(c => c.adjustable).length === 0 && (
                <div className="text-gray-400 text-sm text-center py-6">没有可调课程</div>
              )}
            </div>

            {/* 不可调课程 */}
            <h3 className="font-medium text-red-600 mb-3">❌ 不可调课程</h3>
            <div className="space-y-3">
              {courses.filter(c => !c.adjustable).map((c, i) => (
                <div key={i} className="bg-gray-50 rounded-xl p-4 border border-gray-200 opacity-80">
                  <div className="font-semibold text-gray-500">{c.course_name}</div>
                  <div className="text-sm text-gray-400 mt-1">{c.class_name}</div>
                  <div className="text-xs text-red-500 mt-2">
                    ⚠️ {c.adjustable_reason || '该课程不可调'}
                  </div>
                </div>
              ))}
              {courses.filter(c => !c.adjustable).length === 0 && (
                <div className="text-gray-400 text-sm text-center py-6">暂无不可调课程</div>
              )}
            </div>

            {/* B1/B2: 自然语言调课（多轮追问） */}
            <div className="bg-white rounded-xl p-5 shadow-sm border border-gray-200 mt-8">
              <h3 className="font-medium text-gray-900 mb-1">💬 自然语言调课</h3>
              <p className="text-xs text-gray-400 mb-3">也可以直接说出需求，如"我是吴琳，下周三有事，把实验课往后挪一周"</p>
              <div className="flex gap-2">
                <input
                  value={nlInput}
                  onChange={(e) => setNlInput(e.target.value)}
                  onKeyDown={(e) => e.key === 'Enter' && handleNlReschedule()}
                  placeholder="输入大白话调课需求..."
                  className="flex-1 px-3 py-2 border border-gray-300 rounded-lg text-sm focus:ring-2 focus:ring-blue-500 outline-none"
                  disabled={loading}
                />
                <button
                  onClick={handleNlReschedule}
                  disabled={loading || !nlInput.trim()}
                  className="px-4 py-2 bg-gray-800 text-white rounded-lg hover:bg-gray-900 disabled:opacity-50 text-sm font-medium"
                >
                  {loading ? '解析中...' : '发送'}
                </button>
              </div>

              {/* 多轮追问（B2：信息缺失时系统追问） */}
              {followupQ.length > 0 && (
                <div className="mt-4 px-4 py-3 bg-amber-50 border border-amber-200 rounded-lg">
                  <div className="text-sm font-medium text-amber-800 mb-2">🤔 需要补充信息</div>
                  {followupQ.map((q, i) => (
                    <div key={i} className="text-sm text-amber-700 ml-2">• {q}</div>
                  ))}
                  <p className="text-xs text-amber-500 mt-2">请在上方输入框补充回答，我会继续为您处理</p>
                </div>
              )}

              {/* 自然语言反馈 */}
              {nlMsg && (
                <div className="mt-3 text-sm text-gray-600 whitespace-pre-wrap">{nlMsg}</div>
              )}
            </div>
          </div>
        )}

        {/* 页面3：调课结果（F3/F4） */}
        {view === 'reschedule' && selectedCourse && (
          <div>
            <button
              onClick={() => setView('courses')}
              className="text-sm px-3 py-1.5 bg-gray-100 text-gray-600 rounded-lg hover:bg-gray-200 mb-4"
            >
              ← 返回课程列表
            </button>

            <div className="bg-white rounded-xl p-5 shadow-sm border border-gray-200 mb-6">
              <h2 className="font-semibold text-gray-900">调课申请 · 模板表单</h2>
              <div className="text-sm text-gray-600 mt-2">
                课程：{selectedCourse.course_name}（{selectedCourse.class_name}）
              </div>
              <div className="text-sm text-gray-600 mt-1">
                原时间：{selectedCourse.original_time} · {selectedCourse.room} · {selectedCourse.week_range}
              </div>
              <div className="flex items-center gap-3 mt-4">
                <label className="text-sm text-gray-600">目标周次：</label>
                <select
                  value={targetWeek}
                  onChange={(e) => handleTargetWeekChange(Number(e.target.value))}
                  className="px-3 py-2 border border-gray-300 rounded-lg text-sm focus:ring-2 focus:ring-blue-500 outline-none"
                >
                  {Array.from({ length: 16 }, (_, i) => i + 1).map(w => (
                    <option key={w} value={w}>第{w}周</option>
                  ))}
                </select>
                <span className="text-xs text-gray-400">（选择后自动重新查询可用时段）</span>
              </div>
            </div>

            {/* 周课表沙盘（可视化网格视图） */}
            <div className="bg-white rounded-xl border border-gray-200 p-5 mb-6">
              <h3 className="font-medium text-gray-900 mb-1">🗓️ 周课表沙盘（第{targetWeek}周）</h3>
              <p className="text-xs text-gray-400 mb-4">绿=可用时段 ｜ 红=被排除时段（含原因）</p>
              <div className="grid" style={{ gridTemplateColumns: '70px repeat(5, 1fr)', gap: '6px' }}>
                <div />
                {(['周一', '周二', '周三', '周四', '周五'] as const).map(wd => (
                  <div key={wd} className="text-center text-xs font-medium text-gray-500 py-1">{weekdayLabels[wd]}</div>
                ))}
                {['567', '678', '91011'].map(ts => (
                  <>
                    <div key={`label-${ts}`} className="text-xs text-gray-500 flex items-center justify-center">
                      {timeSlotLabels[ts] || ts}
                    </div>
                    {(['周一', '周二', '周三', '周四', '周五'] as const).map(wd => {
                      const availSlot = available.find(s => s.weekday === wd && s.time_slot === ts);
                      const exclSlot = excluded.find(e => e.weekday === wd && e.time_slot === ts);
                      if (availSlot) {
                        return (
                          <div key={`${wd}-${ts}`} className="bg-green-50 border border-green-200 rounded-lg p-1.5 min-h-[52px]">
                            <div className="text-xs text-green-700 font-medium">{availSlot.room}</div>
                            {availSlot.date && <div className="text-[10px] text-green-500">{availSlot.date}</div>}
                          </div>
                        );
                      }
                      if (exclSlot) {
                        return (
                          <div key={`${wd}-${ts}`} className="bg-red-50 border border-red-200 rounded-lg p-1.5 min-h-[52px]">
                            <div className="text-[10px] text-red-500 line-clamp-2">{exclSlot.reason}</div>
                          </div>
                        );
                      }
                      return (
                        <div key={`${wd}-${ts}`} className="bg-gray-50 border border-gray-100 rounded-lg min-h-[52px]" />
                      );
                    })}
                  </>
                ))}
              </div>
            </div>

            {/* 可用时段（按星期分组展示，避免全是周一） */}
            <h3 className="font-medium text-green-700 mb-3">✅ 可用时段（{available.length}）</h3>
            <div className="bg-white rounded-xl border border-green-200 mb-6 overflow-hidden">
              {available.length === 0 && (
                <div className="text-gray-400 text-sm text-center py-6">无可用时段</div>
              )}
              {!showAllAvail ? (
                <div className="divide-y divide-gray-50">
                  {(['周一', '周二', '周三', '周四', '周五'] as const).map((wd) => {
                    const daySlots = available.filter(s => s.weekday === wd).slice(0, 4);
                    if (daySlots.length === 0) return null;
                    return (
                      <div key={wd} className="px-4 py-2">
                        <div className="text-xs font-medium text-gray-500 mb-1.5">{weekdayLabels[wd]}</div>
                        {daySlots.map((s, i) => (
                          <div key={i} className="flex items-center text-sm py-1">
                            <span className="text-green-600 mr-3">✅</span>
                            {s.date && <span className="text-gray-500 w-20">{s.date}</span>}
                            <span className="text-gray-500 w-28">{timeSlotLabels[s.time_slot] || s.time_slot}</span>
                            <span className="text-gray-400">{s.room}</span>
                          </div>
                        ))}
                      </div>
                    );
                  })}
                </div>
              ) : (
                <div className="divide-y divide-gray-50">
                  {available.map((s, i) => (
                    <div key={i} className="px-4 py-2.5 flex items-center text-sm">
                      <span className="text-green-600 mr-3">✅</span>
                      <span className="font-medium text-gray-800">{weekdayLabels[s.weekday]}</span>
                      {s.date && <span className="text-gray-500 ml-2">{s.date}</span>}
                      <span className="text-gray-500 ml-2">{timeSlotLabels[s.time_slot] || s.time_slot}</span>
                      <span className="text-gray-400 ml-3">{s.room}</span>
                    </div>
                  ))}
                </div>
              )}
              {available.length > 20 && (
                <button
                  onClick={() => setShowAllAvail(!showAllAvail)}
                  className="w-full py-2.5 text-sm text-blue-600 hover:bg-blue-50 transition-colors border-t border-gray-100"
                >
                  {showAllAvail ? '收起 ▲' : `展开全部 ${available.length} 个时段 ▼`}
                </button>
              )}
            </div>

            {/* 冲突影响分析（差异化功能） */}
            <div className="bg-white rounded-xl border border-orange-200 p-5 mb-6">
              <h3 className="font-medium text-orange-800 mb-2">🎯 冲突影响分析</h3>
              <p className="text-sm text-gray-600 mb-3">
                查看把「{selectedCourse.course_name}」调到第{targetWeek}周，会影响到哪些班级、教室和教师
              </p>
              <button
                onClick={handleImpactAnalysis}
                disabled={impactLoading}
                className="px-5 py-2.5 bg-orange-500 text-white rounded-lg hover:bg-orange-600 disabled:opacity-50 text-sm font-medium transition-colors"
              >
                {impactLoading ? '⏳ 分析中...' : '分析影响范围'}
              </button>

              {impact && (
                <div className="mt-4 space-y-3">
                  <div className="text-sm font-medium text-gray-800">
                    共 {impact.conflict_count} 处冲突
                  </div>
                  {impact.affected_classes.length > 0 && (
                    <div className="bg-red-50 border border-red-200 rounded-lg p-3">
                      <div className="text-xs font-medium text-red-700 mb-1">🏫 受影响班级（教室被占用）</div>
                      <div className="text-sm text-red-600">{impact.affected_classes.join('、')}</div>
                    </div>
                  )}
                  {impact.affected_rooms.length > 0 && (
                    <div className="bg-amber-50 border border-amber-200 rounded-lg p-3">
                      <div className="text-xs font-medium text-amber-700 mb-1">🏠 受影响教室</div>
                      <div className="text-sm text-amber-600">{impact.affected_rooms.join('、')}</div>
                    </div>
                  )}
                  {impact.affected_teachers.length > 0 && (
                    <div className="bg-purple-50 border border-purple-200 rounded-lg p-3">
                      <div className="text-xs font-medium text-purple-700 mb-1">👨‍🏫 受影响教师</div>
                      <div className="text-sm text-purple-600">{impact.affected_teachers.join('、')}</div>
                    </div>
                  )}
                  {impact.affected_students.length > 0 && (
                    <div className="bg-blue-50 border border-blue-200 rounded-lg p-3">
                      <div className="text-xs font-medium text-blue-700 mb-1">🎓 受影响学生课表</div>
                      <div className="text-sm text-blue-600">{impact.affected_students.join('、')}</div>
                    </div>
                  )}
                </div>
              )}
            </div>

            {/* 被排除时段（含原因，F4） */}
            <h3 className="font-medium text-gray-600 mb-3">🚫 被排除时段（{excluded.length}）</h3>
            <div className="bg-white rounded-xl border border-gray-200 mb-6 overflow-hidden">
              {excluded.length === 0 && (
                <div className="text-gray-400 text-sm text-center py-6">无被排除时段</div>
              )}
              <div className="divide-y divide-gray-50 max-h-80 overflow-y-auto">
                {excluded.map((s, i) => (
                  <div key={i} className="px-4 py-2.5 text-sm">
                    <div className="flex items-center">
                      <span className="text-gray-400 mr-3">✗</span>
                      <span className="text-gray-500">{weekdayLabels[s.weekday]}</span>
                      {s.date && <span className="text-gray-400 ml-2">{s.date}</span>}
                      <span className="text-gray-400 ml-2">{timeSlotLabels[s.time_slot] || s.time_slot}</span>
                      {s.room && <span className="text-gray-300 ml-3">{s.room}</span>}
                    </div>
                    <div className="text-xs text-red-400 mt-1 ml-7">{s.reason}</div>
                  </div>
                ))}
              </div>
            </div>

            {/* 提交申请（F6）——先看结果再提交 */}
            <div className="bg-white rounded-xl border border-blue-200 p-5 mb-6">
              <h3 className="font-medium text-blue-800 mb-2">📋 确认提交调课申请</h3>
              <p className="text-sm text-gray-600 mb-4">
                {currentTeacher} · {selectedCourse.course_name} → 第{targetWeek}周（提交后由管理员审批）
              </p>
              {requestStatus ? (
                <div className="text-sm px-4 py-3 bg-green-50 border border-green-200 text-green-700 rounded-lg">
                  {requestStatus}
                </div>
              ) : (
                <button
                  onClick={handleSubmitRequest}
                  disabled={loading}
                  className="px-6 py-2.5 bg-blue-600 text-white rounded-lg hover:bg-blue-700 disabled:opacity-50 text-sm font-medium transition-colors"
                >
                  {loading ? '提交中...' : '提交调课申请'}
                </button>
              )}
            </div>
          </div>
        )}

        {/* 页面5：选课咨询 / 实验辅导（LLM 咨询） */}
        {/* ⚡ 并行排课（访谈核心需求：所有实验项目一次列出，一键排定） */}
        {view === 'parallel' && (
          <main className="max-w-4xl mx-auto px-4 pb-10">
            <div className="bg-white rounded-xl border border-gray-200 p-5 mb-6">
              <h2 className="text-lg font-semibold text-gray-900 mb-1">⚡ 并行排课</h2>
              <p className="text-sm text-gray-500 mb-3">
                一门课的全部实验项目 × 学生组别一次列出，CP-SAT 约束求解一键统一分配周次/时段/场地（每组每周一个实验，场地/教师互斥）
              </p>

              {/* 上课星期选择（默认自动沿用历史最常用星期） */}
              <div className="flex items-center gap-2 mb-4 flex-wrap">
                <span className="text-xs text-gray-500">上课星期：</span>
                {['', '周一', '周二', '周三', '周四', '周五'].map((w) => (
                  <button
                    key={w || 'auto'}
                    onClick={() => handleParallelWeekdayChange(w)}
                    className={`px-2.5 py-1 rounded-lg text-xs transition-colors ${
                      parallelWeekday === w
                        ? 'bg-cyan-600 text-white'
                        : 'bg-gray-100 text-gray-600 hover:bg-gray-200'
                    }`}
                  >
                    {w || '自动'}
                  </button>
                ))}
              </div>

              {parallelLoading && <div className="text-sm text-gray-400 py-4">加载课程总览中...</div>}

              {/* 课程总览卡片 */}
              <div className="grid gap-3 md:grid-cols-2">
                {parallelCourses.map((c) => (
                  <div key={c.class_name}
                    className={`border rounded-xl p-4 transition-colors ${
                      parallelSelected === c.class_name ? 'border-cyan-400 bg-cyan-50' : 'border-gray-200 hover:border-cyan-300'
                    }`}>
                    <div className="flex items-start justify-between gap-2">
                      <div>
                        <div className="font-medium text-gray-900 text-sm">{c.class_name}</div>
                        <div className="text-xs text-gray-500 mt-1">
                          常上{c.weekday} · {c.projects.length} 个实验项目 · {c.total_units} 个教学单元
                        </div>
                      </div>
                      <button
                        onClick={() => handleParallelSolve(c.class_name)}
                        disabled={parallelSolving}
                        className="px-3 py-1.5 bg-cyan-600 text-white rounded-lg hover:bg-cyan-700 disabled:opacity-50 text-xs font-medium whitespace-nowrap"
                      >
                        {parallelSolving && parallelSelected === c.class_name ? '⏳ 求解中...' : '一键排课'}
                      </button>
                    </div>
                    <div className="mt-2 space-y-1">
                      {c.projects.slice(0, 4).map((p) => (
                        <div key={p.project} className="text-xs text-gray-600">
                          · {p.project} <span className="text-gray-400">（{p.groups.length} 组）</span>
                        </div>
                      ))}
                      {c.projects.length > 4 && (
                        <div className="text-xs text-gray-400">…共 {c.projects.length} 个项目</div>
                      )}
                    </div>
                  </div>
                ))}
              </div>
            </div>

            {/* 求解结果 */}
            {parallelResult && (
              <div className="bg-white rounded-xl border border-cyan-200 p-5">
                <h3 className="font-medium text-gray-900 mb-1">
                  📋 排课方案（{parallelResult.weekday}）· 状态 {parallelResult.status}
                </h3>
                <p className="text-xs text-gray-500 mb-4">
                  {parallelResult.scheduled}/{parallelResult.total_units} 个单元已排定，用 {parallelResult.weeks_used} 周
                  ｜ 按周分布：{Object.entries(parallelResult.by_week || {}).map(([w, n]) => `第${w}周${n}个`).join('、')}
                  <button
                    onClick={handleParallelExport}
                    className="ml-3 px-3 py-1 bg-emerald-600 text-white rounded-lg hover:bg-emerald-700 text-xs font-medium"
                  >
                    ⬇️ 导出 Excel
                  </button>
                </p>

                {/* 周沙盘：按周切换，行=时段 列=学生组 */}
                {(() => {
                  const wks = Object.keys(parallelResult.by_week || {}).map(Number).sort((a, b) => a - b);
                  const viewWeek = parallelViewWeek ?? wks[0];
                  if (!viewWeek) return null;
                  const weekSessions: any[] = (parallelResult.schedule || []).filter((s: any) => s.week === viewWeek);
                  const groups: string[] = Array.from(new Set<string>(weekSessions.map((s: any) => String(s.group))));
                  const slots = ['567', '678', '91011'];
                  return (
                    <div className="mb-5">
                      <div className="flex items-center gap-2 mb-2 flex-wrap">
                        <span className="text-xs font-medium text-gray-500">周沙盘（{parallelResult.weekday}）：</span>
                        {wks.map((w) => (
                          <button
                            key={w}
                            onClick={() => setParallelViewWeek(w)}
                            className={`px-2.5 py-1 rounded-lg text-xs transition-colors ${
                              viewWeek === w ? 'bg-cyan-600 text-white' : 'bg-gray-100 text-gray-600 hover:bg-gray-200'
                            }`}
                          >
                            第{w}周
                          </button>
                        ))}
                      </div>
                      <div className="grid gap-1.5" style={{ gridTemplateColumns: `90px repeat(${groups.length}, 1fr)` }}>
                        <div />
                        {groups.map((g) => (
                          <div key={g} className="text-center text-xs font-medium text-gray-500 truncate">{g}</div>
                        ))}
                        {slots.map((ts) => (
                          <Fragment key={ts}>
                            <div className="text-xs text-gray-500 flex items-center justify-center">
                              {timeSlotLabels[ts] || ts}
                            </div>
                            {groups.map((g) => {
                              const s = weekSessions.find((x: any) => x.group === g && x.time_slot === ts);
                              return s ? (
                                <div key={`${ts}-${g}`} className="bg-cyan-50 border border-cyan-200 rounded-lg p-1.5 min-h-[52px]">
                                  <div className="text-xs text-cyan-800 font-medium leading-tight line-clamp-2">{s.project}</div>
                                  <div className="text-[10px] text-cyan-600">{s.room} · {s.teacher}</div>
                                </div>
                              ) : (
                                <div key={`${ts}-${g}`} className="bg-gray-50 border border-gray-100 rounded-lg min-h-[52px]" />
                              );
                            })}
                          </Fragment>
                        ))}
                      </div>
                    </div>
                  );
                })()}

                <div className="overflow-x-auto">
                  <table className="w-full text-sm">
                    <thead>
                      <tr className="text-left text-xs text-gray-500 border-b border-gray-200">
                        <th className="py-2 pr-3">周次</th>
                        <th className="py-2 pr-3">时段</th>
                        <th className="py-2 pr-3">场地</th>
                        <th className="py-2 pr-3">学生组</th>
                        <th className="py-2 pr-3">实验项目</th>
                        <th className="py-2">教师</th>
                      </tr>
                    </thead>
                    <tbody>
                      {(parallelResult.schedule || []).slice(0, 40).map((s: any, i: number) => (
                        <tr key={i} className="border-b border-gray-50">
                          <td className="py-1.5 pr-3 text-cyan-700 font-medium">第{s.week}周</td>
                          <td className="py-1.5 pr-3">{timeSlotLabels[s.time_slot] || s.time_slot}</td>
                          <td className="py-1.5 pr-3">{s.room}</td>
                          <td className="py-1.5 pr-3">{s.group}</td>
                          <td className="py-1.5 pr-3">{s.project}</td>
                          <td className="py-1.5 text-gray-500">{s.teacher}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
                {(parallelResult.schedule || []).length > 40 && (
                  <p className="text-xs text-gray-400 mt-2">仅显示前 40 条，共 {parallelResult.schedule.length} 条</p>
                )}
              </div>
            )}
          </main>
        )}

        {/* 📊 数据看板（体检报告 + 统计） */}
        {view === 'dashboard' && (
          <main className="max-w-4xl mx-auto px-4 pb-10">
            <div className="bg-white rounded-xl border border-gray-200 p-5 mb-6">
              <h2 className="text-lg font-semibold text-gray-900 mb-1">📊 数据看板</h2>
              <p className="text-sm text-gray-500 mb-4">对现有实验安排表做一键体检：数据覆盖情况 + 教室/教师冲突扫描</p>

              {auditLoading && <div className="text-sm text-gray-400 py-4">体检扫描中...</div>}

              {auditData && (
                <>
                  {/* 健康总览 */}
                  <div className={`rounded-xl p-4 mb-4 ${auditData.healthy ? 'bg-green-50 border border-green-200' : 'bg-amber-50 border border-amber-200'}`}>
                    <div className="text-lg font-medium">
                      {auditData.healthy ? '✅ 安排表整体健康，未发现资源冲突' : '⚠️ 发现潜在冲突，建议核查'}
                    </div>
                    <div className="text-sm text-gray-600 mt-1">
                      共 {auditData.total_entries} 条安排 ｜ {auditData.teacher_count} 位教师 ｜ {auditData.room_count} 间场地
                      ｜ 覆盖第 {auditData.week_min}~{auditData.week_max} 周
                    </div>
                  </div>

                  {/* 冲突计数 */}
                  <div className="grid grid-cols-2 gap-3 mb-4">
                    <div className="bg-red-50 border border-red-200 rounded-xl p-4">
                      <div className="text-2xl font-semibold text-red-600">{auditData.room_conflict_count}</div>
                      <div className="text-sm text-red-700">教室冲突（同室同段多班）</div>
                    </div>
                    <div className="bg-orange-50 border border-orange-200 rounded-xl p-4">
                      <div className="text-2xl font-semibold text-orange-600">{auditData.teacher_conflict_count}</div>
                      <div className="text-sm text-orange-700">教师冲突（同人同段多班）</div>
                    </div>
                  </div>

                  {/* 教室冲突清单 */}
                  {(auditData.room_conflicts || []).length > 0 && (
                    <div className="mb-4">
                      <h3 className="text-sm font-medium text-gray-800 mb-2">🏫 教室冲突明细（前 {auditData.room_conflicts.length} 条）</h3>
                      <div className="space-y-2">
                        {auditData.room_conflicts.map((c: any, i: number) => (
                          <div key={i} className="bg-red-50 border border-red-100 rounded-lg p-3 text-sm">
                            <div className="font-medium text-red-700">
                              第{c.week}周 {c.weekday} {timeSlotLabels[c.time_slot] || c.time_slot} · {c.place}
                            </div>
                            {c.detail.map((d: string, j: number) => (
                              <div key={j} className="text-xs text-red-500 ml-4">· {d}</div>
                            ))}
                          </div>
                        ))}
                      </div>
                    </div>
                  )}

                  {/* 教师冲突清单 */}
                  {(auditData.teacher_conflicts || []).length > 0 && (
                    <div>
                      <h3 className="text-sm font-medium text-gray-800 mb-2">👨‍🏫 教师冲突明细（前 {auditData.teacher_conflicts.length} 条）</h3>
                      <div className="space-y-2">
                        {auditData.teacher_conflicts.map((c: any, i: number) => (
                          <div key={i} className="bg-orange-50 border border-orange-100 rounded-lg p-3 text-sm">
                            <div className="font-medium text-orange-700">
                              第{c.week}周 {c.weekday} {timeSlotLabels[c.time_slot] || c.time_slot} · {c.place}
                            </div>
                            {c.detail.map((d: string, j: number) => (
                              <div key={j} className="text-xs text-orange-500 ml-4">· {d}</div>
                            ))}
                          </div>
                        ))}
                      </div>
                    </div>
                  )}
                </>
              )}

              {/* 统计面板 */}
              {statsData && (
                <div className="mt-6 pt-5 border-t border-gray-100">
                  <h3 className="text-sm font-medium text-gray-800 mb-3">📈 利用率与工作量统计（{statsData.n_weeks} 周）</h3>

                  {/* 关键指标 */}
                  <div className="grid grid-cols-3 gap-3 mb-4">
                    <div className="bg-teal-50 border border-teal-200 rounded-xl p-3">
                      <div className="text-xl font-semibold text-teal-700">{statsData.avg_room_utilization}%</div>
                      <div className="text-xs text-teal-700">平均教室利用率</div>
                    </div>
                    <div className="bg-teal-50 border border-teal-200 rounded-xl p-3">
                      <div className="text-xl font-semibold text-teal-700">{statsData.avg_teacher_load}</div>
                      <div className="text-xs text-teal-700">教师平均课时（节）</div>
                    </div>
                    <div className="bg-teal-50 border border-teal-200 rounded-xl p-3">
                      <div className="text-xl font-semibold text-teal-700">{statsData.teacher_load_balance}%</div>
                      <div className="text-xs text-teal-700">教师工作量均衡度</div>
                    </div>
                  </div>

                  <div className="grid gap-4 md:grid-cols-2">
                    {/* 教室利用率 TOP */}
                    <div>
                      <div className="text-xs font-medium text-gray-500 mb-2">教室利用率 TOP（最忙：{statsData.busiest_room?.room} {statsData.busiest_room?.utilization}%）</div>
                      <div className="space-y-1.5">
                        {statsData.top_rooms.map((r: any) => (
                          <div key={r.room} className="flex items-center gap-2 text-xs">
                            <span className="w-14 text-gray-600 truncate">{r.room}</span>
                            <div className="flex-1 h-2.5 bg-gray-100 rounded-full overflow-hidden">
                              <div className="h-full bg-teal-500 rounded-full" style={{ width: `${Math.min(r.utilization, 100)}%` }} />
                            </div>
                            <span className="w-16 text-right text-gray-500">{r.sessions}节/{r.utilization}%</span>
                          </div>
                        ))}
                      </div>
                    </div>
                    {/* 教师工作量 TOP */}
                    <div>
                      <div className="text-xs font-medium text-gray-500 mb-2">教师工作量 TOP</div>
                      <div className="space-y-1.5">
                        {statsData.top_teachers.map((t: any) => (
                          <div key={t.teacher} className="flex items-center gap-2 text-xs">
                            <span className="w-14 text-gray-600 truncate">{t.teacher}</span>
                            <div className="flex-1 h-2.5 bg-gray-100 rounded-full overflow-hidden">
                              <div className="h-full bg-indigo-500 rounded-full"
                                style={{ width: `${Math.min((t.sessions / (statsData.top_teachers[0]?.sessions || 1)) * 100, 100)}%` }} />
                            </div>
                            <span className="w-24 text-right text-gray-500">{t.sessions}节·{t.experiments}实验</span>
                          </div>
                        ))}
                      </div>
                    </div>
                  </div>
                </div>
              )}
            </div>
          </main>
        )}

        {(view === 'course_select' || view === 'tutor') && (
          <div>
            <div className="flex items-center justify-between mb-2">
              <h2 className="text-lg font-semibold text-gray-900">
                {view === 'course_select' ? '🎓 选课咨询' : '🔬 实验辅导'}
              </h2>
              <span className="text-xs px-2 py-1 bg-emerald-50 text-emerald-700 rounded-full border border-emerald-200">
                ☁️ 云端大模型
              </span>
            </div>
            <p className="text-sm text-gray-500 mb-6">
              {view === 'course_select'
                ? '咨询选课资格、先修课程、学分要求等问题（免密钥云端大模型，回答流式生成）'
                : '咨询实验原理、数据处理、误差分析、报告规范等问题（免密钥云端大模型，回答流式生成）'}
            </p>

            <div className="bg-white rounded-xl p-5 shadow-sm border border-gray-200">
              <div className="flex gap-2">
                <input
                  value={consultQ}
                  onChange={(e) => setConsultQ(e.target.value)}
                  onKeyDown={(e) => e.key === 'Enter' && handleConsult()}
                  placeholder={view === 'course_select'
                    ? '如：为什么我选不了大学物理实验课？'
                    : '如：扭摆法测转动惯量的误差来源？'}
                  className="flex-1 min-w-0 px-3 py-2.5 border border-gray-300 rounded-lg text-sm focus:ring-2 focus:ring-blue-500 outline-none"
                  disabled={loading}
                />
                {loading ? (
                  <button
                    onClick={handleStopConsult}
                    className="px-5 py-2.5 bg-gray-800 text-white rounded-lg hover:bg-gray-900 text-sm font-medium whitespace-nowrap"
                  >
                    停止生成
                  </button>
                ) : (
                  <button
                    onClick={handleConsult}
                    disabled={!consultQ.trim()}
                    className="px-5 py-2.5 bg-blue-600 text-white rounded-lg hover:bg-blue-700 disabled:opacity-50 text-sm font-medium whitespace-nowrap"
                  >
                    提问
                  </button>
                )}
              </div>

              {consultMsg && (
                <div className="mt-4 px-4 py-3 bg-gray-50 border border-gray-200 rounded-lg text-sm text-gray-700 prose prose-sm max-w-none">
                  <ReactMarkdown>{consultMsg}</ReactMarkdown>
                </div>
              )}

              {/* 云端咨询历史（需登录，按用户隔离） */}
              <div className="mt-4 pt-4 border-t border-gray-100">
                <button
                  onClick={handleToggleLogs}
                  className="text-sm text-blue-600 hover:text-blue-700"
                >
                  {showLogs ? '收起云端历史 ▲' : '查看云端咨询历史 ▼'}
                </button>
                {showLogs && (
                  <div className="mt-3 space-y-2 max-h-80 overflow-y-auto">
                    {consultLogs.length === 0 && (
                      <div className="text-sm text-gray-400 py-3">还没有云端咨询记录</div>
                    )}
                    {consultLogs.map((log) => (
                      <div key={log.id} className="bg-gray-50 border border-gray-200 rounded-lg p-3">
                        <div className="text-xs text-gray-400 mb-1">
                          {new Date(log.created_at).toLocaleString('zh-CN')}
                        </div>
                        <div className="text-sm font-medium text-gray-800">Q：{log.question}</div>
                        <div className="text-sm text-gray-600 mt-1 whitespace-pre-wrap line-clamp-3">
                          A：{log.answer}
                        </div>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            </div>
          </div>
        )}
      </main>
    </div>
  );
}

export default App;
