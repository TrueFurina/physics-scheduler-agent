import { Suspense, lazy, useCallback, useEffect, useRef, useState } from 'react';
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
import { describeApiError } from './utils/apiError';
import { clearStaleLoadBanner } from './utils/msg';
import ErrorBoundary from './components/ErrorBoundary';
import type { View, ImpactResult, AiPlan, NlSlotContext, ScheduleGrid } from './types';
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
const MasterDataView = lazy(() => import('./views/MasterDataView'));
const ScheduleGridView = lazy(() => import('./views/ScheduleGridView'));

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
  /**
   * 横幅色调：按文案前缀判断。
   * 为什么用前缀而不是多存一个 state：msg 有 20+ 处 setMsg 调用，
   * 逐处加 tone 参数既容易漏、又多一个可能不同步的状态。
   * 前缀本来就是这些文案的共同约定（✅/❌/⚠️/已…）。
   */
  const msgTone: 'ok' | 'err' | 'neutral' =
    msg.startsWith('✅') || msg.startsWith('已同意') || msg.startsWith('已驳回')
      ? 'ok'
      : msg.startsWith('❌') || msg.startsWith('⚠️') || msg.includes('失败')
        ? 'err'
        : 'neutral';
  const [dataLoaded, setDataLoaded] = useState(false);
  // 首屏加载失败的原因。必须单独记：教师列表为空有两种完全不同的原因 ——
  // 「服务端没读到安排表」和「服务端报错了」，空态文案不能都写成"请先加载实验安排表"，
  // 后者会让教秘去找一个界面上根本不存在的加载入口（实测见 probe_faults 首屏场景）。
  const [loadError, setLoadError] = useState('');
  const [requestStatus, setRequestStatus] = useState('');  // 申请提交后的状态反馈
  const [requests, setRequests] = useState<RescheduleRequest[]>([]);  // 审批台申请列表
  // 演示模式（DEMO_MODE=1）：审批台走本机工单 + 本地口令，不依赖云端登录。
  // 现场网络/手机验证码不可控，而"审批通过→课表真的变"是最有说服力的一段。
  const [demoMode, setDemoMode] = useState(false);
  const [nlInput, setNlInput] = useState('');  // 自然语言调课输入
  const [followupQ, setFollowupQ] = useState<string[]>([]);  // 多轮追问问题
  // 多轮追问的槽位（B2）：后端不存会话，由前端每轮带回，服务端无状态合并。
  // 自然语言说到的课程名：同名跨班时用它把下拉从"13 门里挑"收窄成"这几个班挑"
  const [courseHint, setCourseHint] = useState('');
  // 多轮追问的槽位（B2）：后端不存会话，由前端每轮带回，服务端无状态合并。
  const [nlContext, setNlContext] = useState<NlSlotContext | null>(null);
  const [nlMsg, setNlMsg] = useState('');  // 自然语言反馈消息
  const [consultQ, setConsultQ] = useState('');  // 选课/辅导咨询输入
  const [consultMsg, setConsultMsg] = useState('');  // 咨询回复
  const [autoScheduleMsg, setAutoScheduleMsg] = useState('');  // 自动排课结果
  const [autoScheduleRows, setAutoScheduleRows] = useState<any[]>([]);  // 初稿明细（供导出）
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
  const [schoolPlan, setSchoolPlan] = useState<any>(null);  // 全校一键结果（逐班·按天）
  const [schoolSolving, setSchoolSolving] = useState(false);
  const [parallelSelected, setParallelSelected] = useState('');  // 当前选中课程
  // 周课表网格（R-62）：读路径免鉴权，与数据看板同族
  const [gridWeek, setGridWeek] = useState(1);
  const [gridData, setGridData] = useState<ScheduleGrid | null>(null);
  const [gridLoading, setGridLoading] = useState(false);
  const [gridError, setGridError] = useState('');
  const [gridClass, setGridClass] = useState('');
  const [gridRoom, setGridRoom] = useState('');
  // 失败提示条渲染在页面顶部（header 正下方），而教秘的点按发生在页面中部/底部
  // （如并行排课点某张课程卡的「一键排课」）—— 不滚进视口就是"点了没反应"（R-30 同族）。
  const msgBannerRef = useRef<HTMLDivElement>(null);
  // 并行排课结果渲染在全部课程卡之后：点靠后的卡时，结果顶部只露 56px，沙盘/明细全在折叠线下。
  const parallelResultRef = useRef<HTMLDivElement>(null);
  // AI 候选方案（B3/F9）：与沙盘分开加载——沙盘要立刻出现，候选方案可以后到。
  // PRD 验收标准与演示脚本都承诺"显示 AI 候选方案"，但历史上前端从未渲染过它。
  const [aiPlans, setAiPlans] = useState<AiPlan[]>([]);
  // R64：候选来源。规则降级产物不能冒充 AI 产物。
  const [aiPlansSource, setAiPlansSource] = useState<'llm' | 'rule' | 'none'>('none');
  const [plansLoading, setPlansLoading] = useState(false);
  const plansSeqRef = useRef(0);      // 请求序号：旧响应晚到就丢弃，避免切周次后串台
  const plansTimerRef = useRef<number | null>(null);  // 切周次的防抖（连点只发最后一次）

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

  const consultKey: ConsultModuleKey = 'tutor';
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

  // 演示模式开关（只读探测；失败按关闭处理，不影响正常流程）
  useEffect(() => {
    rescheduleApi.demoMode()
      .then((r) => setDemoMode(Boolean(r.data?.demo_mode)))
      .catch(() => setDemoMode(false));
  }, []);

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
      setMsg(`加载课程总览失败：${describeApiError(err)}`);
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
      setMsg(`并行排课失败：${describeApiError(err)}`);
    } finally {
      setParallelSolving(false);
    }
  };

  // 全校一键：逐班求解（组级），班与班之间互斥教室/教师。
  // 与首屏「一键自动排课」的分工：那个是教师×课程×班级的初稿，这个是班级×项目×组，
  // 与安排表同口径，可直接对照/导出班级课表。
  const handleSolveSchool = async () => {
    setSchoolSolving(true);
    setMsg('');
    try {
      const r = await rescheduleApi.parallelSolveAll();
      const d = r.data as any;
      setSchoolPlan(d);
      if (!d?.success) {
        setMsg(`全校排课未完成：${d?.classes_failed?.[0]?.error || '没有可排的班级'}`);
      } else if (d.classes_failed?.length) {
        setMsg(`⚠️ ${d.classes_ok}/${d.classes_total} 个班排定，${d.classes_failed.length} 个班未排定（原因见结果表）`);
      } else if ((d.units_missing ?? 0) > 0) {
        // `classes_ok` 的口径是"有产出"（2/31 也算），所以"没有失败班级"不等于"排满了"。
        // 满载实测曾出现 358/410 却报"全部排定"——缺课时必须说清楚（R-76）。
        setMsg(`⚠️ 全校 ${d.classes_complete ?? d.classes_total}/${d.classes_total} 个班排满，`
               + `共 ${d.scheduled} 条安排，仍缺 ${d.units_missing} 个组课时（${d.elapsed_s}s）`);
      } else {
        setMsg(`✅ 全校 ${d.classes_total} 个班全部排定，共 ${d.scheduled} 条安排（${d.elapsed_s}s）`);
      }
    } catch (err: any) {
      setMsg(`全校排课失败：${describeApiError(err)}`);
    } finally {
      setSchoolSolving(false);
    }
  };

  // 全校方案导出（总表 + 每班一页，与自动排课初稿共用导出端点）
  const handleExportSchool = async () => {
    const rows = schoolPlan?.schedule || [];
    if (!rows.length) return;
    try {
      const r = await rescheduleApi.autoScheduleExport(rows);
      downloadBlob(r.data, `全校排课方案_${schoolPlan.classes_ok}班_${rows.length}条.xlsx`);
    } catch (err: any) {
      setMsg(`全校方案导出失败：${describeApiError(err)}`);
    }
  };

  // 切换上课星期（已选课程时立即用新星期重排）
  const handleParallelWeekdayChange = (w: string) => {
    setParallelWeekday(w);
    if (parallelSelected) handleParallelSolve(parallelSelected, w || undefined);
  };

  // 失败提示出现在页面顶部：用户此刻往往已滚到别处，把提示带进视口中央。
  // （首屏加载失败时页面本就在顶部，scrollIntoView 是空操作，不会乱滚。）
  useEffect(() => {
    if (msg) msgBannerRef.current?.scrollIntoView({ block: "center" });
  }, [msg]);

  // 求解成功：结果区在 17 张课程卡之后，直接滚动会让教秘"点了没反应"。
  // 只在结果出现时滚一次（切周沙盘不重复滚）。
  useEffect(() => {
    if (parallelResult) parallelResultRef.current?.scrollIntoView({ block: "start" });
  }, [parallelResult]);

  // blob → 浏览器下载。⚠️ 后端出错时返回的是 JSON，浏览器照样会下成一个 .xlsx，
  // 所以校验走接口侧（导出接口只返回 xlsx），这里不额外猜内容。
  const downloadBlob = (data: any, filename: string) => {
    const url = window.URL.createObjectURL(new Blob([data]));
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    window.URL.revokeObjectURL(url);
  };

  // 并行排课：方案导出 Excel（blob 触发浏览器下载）
  const handleParallelExport = async () => {
    if (!parallelResult?.schedule) return;
    try {
      const r = await rescheduleApi.parallelExport(parallelSelected, parallelResult.schedule);
      downloadBlob(r.data, `并行排课方案_${parallelSelected}.xlsx`);
    } catch (err: any) {
      setMsg(`导出失败：${describeApiError(err)}`);
    }
  };

  // 教务标准课表 + 基础数据台账（都基于当前安排表，不是新排的方案）
  const EXPORT_KINDS: Record<string, { label: string; file: string }> = {
    class: { label: '班级课表', file: '班级课表_全部班级.xlsx' },
    room: { label: '场地课表', file: '场地课表_全部场地.xlsx' },
    master: { label: '基础数据台账', file: '实验排课基础数据台账.xlsx' },
  };
  // 导出中状态：班级课表要 17 个 sheet，实测约 750ms（openpyxl 每 sheet 固定开销），
  // 场地课表约 500ms。没有反馈时用户会重复点击，导致重复下载。
  // 用 kind 而不是布尔值：可区分"正在导班级课表"与"正在导台账"。
  const [exportingKind, setExportingKind] = useState<'class' | 'room' | 'master' | null>(null);

  const handleExportStandard = async (kind: 'class' | 'room' | 'master') => {
    const meta = EXPORT_KINDS[kind];
    if (exportingKind) return;          // 防重复点击
    setExportingKind(kind);
    try {
      const r = kind === 'class'
        ? await rescheduleApi.exportClassSchedule()
        : kind === 'room'
          ? await rescheduleApi.exportRoomSchedule()
          : await rescheduleApi.exportMasterData();
      downloadBlob(r.data, meta.file);
    } catch (err: any) {
      // 失败要说清是哪一类 —— "导出失败"四个字教秘不知道该重试哪个按钮
      setMsg(`${meta.label}导出失败：${describeApiError(err)}`);
    } finally {
      setExportingKind(null);
    }
  };

  // 数据看板：体检报告（扫描现有安排表冲突）
  const [auditData, setAuditData] = useState<any>(null);
  const [auditLoading, setAuditLoading] = useState(false);
  const [auditError, setAuditError] = useState('');   // 体检失败原因：空白卡片会被误读成"没冲突"
  const [statsData, setStatsData] = useState<any>(null);
  const [statsError, setStatsError] = useState('');

  const handleOpenDashboard = async () => {
    setView('dashboard');
    setAuditLoading(true);
    setAuditError('');
    setStatsError('');
    try {
      const r = await rescheduleApi.dataAudit();
      setAuditData(r.data.data);
    } catch (err: any) {
      // 失败时清掉旧数据：陈旧数字配上"这是刚扫出来的"比空白更误导
      setAuditData(null);
      setAuditError(describeApiError(err));
    } finally {
      setAuditLoading(false);
    }
    // 统计面板（失败不影响体检展示，但必须让用户看见它失败了）
    try {
      const s = await rescheduleApi.stats();
      setStatsData(s.data.data);
    } catch (err: any) {
      setStatsData(null);
      setStatsError(describeApiError(err));
    }
  };

  // 周课表网格（R-62）：打开即加载；切周次/筛选只是重新取数。
  const fetchGrid = async (w: number, cls: string, room: string) => {
    setGridLoading(true);
    setGridError('');
    try {
      const r = await rescheduleApi.scheduleGrid(w, cls, room);
      setGridData(r.data.data);
    } catch (err: any) {
      // 失败时清掉旧数据：旧周的格子配上"这是第 N 周的"比空白更误导
      setGridData(null);
      setGridError(describeApiError(err));
    } finally {
      setGridLoading(false);
    }
  };

  const handleOpenGrid = () => {
    setView('grid');
    void fetchGrid(gridWeek, gridClass, gridRoom);
  };

  const handleGridWeekChange = (w: number) => {
    setGridWeek(w);
    void fetchGrid(w, gridClass, gridRoom);
  };

  const handleGridClassChange = (c: string) => {
    setGridClass(c);
    void fetchGrid(gridWeek, c, gridRoom);
  };

  const handleGridRoomChange = (r: string) => {
    setGridRoom(r);
    void fetchGrid(gridWeek, gridClass, r);
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
      setMsg(`影响分析失败：${describeApiError(err)}`);
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
      // R66：**必须判断 success**。
      //
      // R65 之后后端失败时回的是「没有可排课的教学任务」这种中性陈述，
      // 不带任何失败标记 —— 它把"怎么呈现"交给了前端。
      // 而这里原来直接 setAutoScheduleMsg(message)，消息区是灰底灰字，
      // 成功和失败长得完全一样。
      //
      // 这套界面里 ❌ 是唯一能区分"这条路走不通"的标记（R24 修过同一处）。
      if (r.data.success === false) {
        setAutoScheduleRows([]);   // 旧方案不能和错误提示同时留在屏幕上
        setAutoScheduleMsg(`❌ ${r.data.message}`);
        return;
      }
      setAutoScheduleMsg(r.data.message);
      setAutoScheduleRows((r.data as any)?.data?.schedule || []);
    } catch (err: any) {
      setAutoScheduleRows([]);
      setAutoScheduleMsg(`❌ 自动排课失败：${describeApiError(err)}`);
    } finally {
      setAutoScheduleLoading(false);
    }
  };

  // 一键自动排课的初稿导出：总表 + 每班一页（171 行文本没法直接交给班级）
  const handleAutoScheduleExport = async () => {
    if (!autoScheduleRows.length) return;
    try {
      const r = await rescheduleApi.autoScheduleExport(autoScheduleRows);
      downloadBlob(r.data, `自动排课初稿_${autoScheduleRows.length}条.xlsx`);
    } catch (err: any) {
      setMsg(`初稿导出失败：${describeApiError(err)}`);
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
      setLoginErr(describeCloudError(err) || '验证码发送失败');
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
      setLoginErr(describeCloudError(err) || '验证码校验失败');
    } finally {
      setLoginBusy(false);
    }
  };

  const handleWechatLogin = async () => {
    setLoginErr('');
    try {
      await signInWithWechat();   // 同标签页跳转，回调回到 location.origin
    } catch (err: any) {
      setLoginErr(describeCloudError(err) || '微信登录发起失败');
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

  // 云服务：实验规范查询（免密钥大模型，流式输出，可中途停止）
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


  // 打开实验规范查询模块
  const handleOpenModule = () => {
    setConsultMsg('');
    setConsultQ('');
    setView('tutor');
  };

  // 进入时自动加载默认数据
  // 后端在数据未加载时也会自愈（_ensure_loaded），这里重试是为了覆盖"沙箱冷启动导致
  // 第一次请求直接超时/5xx"的情况 —— 那种情况下后端根本收不到请求，只能由前端重试。
  //
  // 先问 /status 再决定是否 load-default：排课数据在进程内存里，
  // 页面刷新（数据仍在）时无条件 load-default 会把 433 行 Excel **整表重解析一遍**
  // （本地实测 0.98s CPU / 冷沙箱 3.8s），纯属白烧 —— 而且它还是限流桶里的一员。
  // 首屏加载（也是空态「重试」按钮的入口）：/status → 需要才 load-default → /teachers。
  // 抽成一个函数是为了一处定义、两处调用 —— 重试与首刷必须走**完全相同的路径**，
  // 否则"刷新能好、点重试不好"这类差异会在演示现场才被发现。
  const loadTeacherList = useCallback(async () => {
    setLoadError('');
    try {
      const t = await withRetry(async () => {
        const st = await rescheduleApi.getStatus();
        if (!st.data.loaded) await rescheduleApi.loadDefault();
        return rescheduleApi.getTeachers();
      });
      setTeachers(t.data.teachers);
      setDataLoaded(true);
      // 只清"自己留下的"加载失败横幅：首屏后台加载的成功可能晚于用户的一次点击
      // （冷启动沙箱上 /status 可拖数秒），无条件清空会把用户刚看到的错误提示静默
      // 吞掉 —— 提示出现又消失等于没提示（R-30 同族，probe_faults solve 场景抓到）。
      setMsg((prev) => clearStaleLoadBanner(prev));
    } catch (err: any) {
      const why = describeApiError(err);
      setLoadError(why);
      setMsg(`加载失败：${why}`);
    }
  }, []);

  useEffect(() => {
    void loadTeacherList();
  }, [loadTeacherList]);

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
      // 换教师 = 换会话：上一轮的追问槽位（含"我是吴琳"）必须清掉，否则会把
      // 上一个老师的身份合并进新老师的调课请求里。
      setNlContext(null);
      setFollowupQ([]);
      setNlMsg('');
      setCourseHint('');
      // 换教师必须作废上一轮的候选方案：在途请求靠序号丢弃，已到的一并清掉
      plansSeqRef.current += 1;
      setAiPlans([]);
      setPlansLoading(false);
      setView('courses');
    } catch (err: any) {
      setMsg(`加载课程失败：${describeApiError(err)}`);
    } finally {
      setLoading(false);
    }
  };

  // AI 候选方案（后台加载，不阻塞沙盘）
  //
  // 为什么单独拉一次而不是首屏直接要：`fast=false` 会走 LLM 生成候选（实测 7.6s），
  // 而沙盘本身只需要规则解析的结果（25ms）。把这两件事绑在一个请求里，等于让教秘
  // 为一张加分卡片等 7.6 秒；拆开之后首屏 25ms，候选方案晚到就晚到。
  //
  // 失败一律静默：候选方案是加分项，LLM 不可用时后端还会规则降级给 3 个候选；
  // 真的什么都没有也不该在教秘面前弹红字（沙盘和"提交"仍然可用）。
  const fetchAiPlans = async (course: TeacherCourse, week: number, seq: number) => {
    try {
      const r = await rescheduleApi.chat(
        `我是${currentTeacher}，把${course.course_name}调到第${week}周`,
        false,
      );
      if (seq !== plansSeqRef.current) return;  // 已被更晚的选择取代
      setAiPlans(r.data.data.ai_plans || []);
      setAiPlansSource((r.data.data as any).ai_plans_source || 'none');
    } catch {
      if (seq === plansSeqRef.current) setAiPlans([]);
    } finally {
      if (seq === plansSeqRef.current) setPlansLoading(false);
    }
  };

  const scheduleAiPlans = (course: TeacherCourse, week: number) => {
    if (plansTimerRef.current !== null) window.clearTimeout(plansTimerRef.current);
    const seq = ++plansSeqRef.current;
    // 切周次时先清空：上一周的候选留在屏幕上，比没有更糟（会被当成这一周的答案）
    setAiPlans([]);
    setPlansLoading(true);
    plansTimerRef.current = window.setTimeout(() => {
      plansTimerRef.current = null;
      void fetchAiPlans(course, week, seq);
    }, 350);
  };

  // 定时器随组件卸载清理（否则关页面后仍可能 setState）
  useEffect(() => () => {
    if (plansTimerRef.current !== null) window.clearTimeout(plansTimerRef.current);
  }, []);

  // F3/F4: 模板表单选择课程 + 目标周次 → 查询可用时段（含原因标注）
  //
  // ⚠️ 首屏**恒用 fast=true**（规则解析，实测 25ms）。历史实现首屏传 fast=false，
  // 于是教秘点「申请调课 →」后要白等一次 LLM 调用（本地实测 7.63s，305 倍差距），
  // 而换来的 `ai_plans` **界面上根本没渲染** —— 白等且白烧额度。
  // 现在拆成两段：沙盘立刻出来（fast=true），AI 候选方案后台补（见 scheduleAiPlans）。
  const handleReschedule = async (course: TeacherCourse, week?: number) => {
    setSelectedCourse(course);
    setLoading(true);
    const target = week ?? targetWeek;
    try {
      const r = await rescheduleApi.chat(
        `我是${currentTeacher}，把${course.course_name}调到第${target}周`,
        true,
      );
      setAvailable(r.data.data.available_slots || []);
      setExcluded(r.data.data.excluded_slots || []);
      setView('reschedule');
    } catch (err: any) {
      setMsg(`查询失败：${describeApiError(err)}`);
    } finally {
      setLoading(false);
    }
    scheduleAiPlans(course, target);
  };

  // 模板表单：更改目标周次后重新查询（fast 模式——秒回沙盘，不等待 LLM 候选方案）
  const handleTargetWeekChange = (week: number) => {
    setTargetWeek(week);
    if (selectedCourse) {
      handleReschedule(selectedCourse, week);
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

  // 把后端识别出的课程名/班级回找成课程卡片。
  // 同一门实验会挂在多个班级下（实测"扭摆法测物体转动惯量"就有两个班），
  // 这时**不猜** —— 返回 null，由沙盘里的课程下拉交给教秘确认：
  // 猜错的代价是把 A 班的课按 B 班提交上去。
  const pickCourseFromContext = (ctx: NlSlotContext): TeacherCourse | null => {
    const adjustable = courses.filter((c) => c.adjustable);
    if (ctx.course_name) {
      let hits = adjustable.filter((c) => c.course_name === ctx.course_name);
      if (hits.length > 1 && ctx.class_name) {
        hits = hits.filter((c) => c.class_name === ctx.class_name);
      }
      return hits.length === 1 ? hits[0] : null;
    }
    return adjustable.length === 1 ? adjustable[0] : null;
  };

  // 沙盘里补选课程（自然语言没提课程，或同名跨班无法唯一确定时）
  const handlePickCourse = (c: TeacherCourse) => {
    setSelectedCourse(c);
    setCourseHint('');
    setAiPlans([]);
    setPlansLoading(false);
    if (targetWeek) scheduleAiPlans(c, targetWeek);
  };

  // B1/B2: 自然语言调课（支持多轮追问——信息缺失时系统追问）
  //
  // 三个坑，都在这一条链路上：
  // ① 多轮是假的：后端每轮从零解析，教秘照着提示答"我是吴琳"，系统下一句
  //    又问"您是哪位老师"。修法是**前端持有槽位**、每轮带回，后端无状态合并。
  // ② 解析成功却白屏：这里只 `setView('reschedule')`，而沙盘的渲染条件是
  //    `view==='reschedule' && selectedCourse`，自然语言路径从来没设过课程 ⇒
  //    后端算出 94 个可用时段，屏幕上只剩导航条（0 JS 错误、0 失败请求，纯静默）。
  // ③ 白等一次 LLM：原来没传 `fast`，默认 `fast=false` 会内联生成一份
  //    这条路径根本不渲染的候选方案。改 `fast=true`，候选方案等沙盘打开后由
  //    `scheduleAiPlans` 后台补（与模板表单路径一致）。
  const handleNlReschedule = async () => {
    const text = nlInput.trim();
    if (!text) return;
    setLoading(true);
    setNlMsg('');
    try {
      // 教师身份直接取"当前选中的身份"：教秘已经点了吴琳，不必再打一遍"我是吴琳"。
      // 文本里显式说了别的老师时，后端本轮解析到的值会覆盖它（覆盖逻辑在服务端）。
      const base: NlSlotContext = { teacher_name: currentTeacher, ...(nlContext || {}) };
      const r = await rescheduleApi.chat(text, true, base);
      const data = r.data.data;
      const ctx: NlSlotContext = { ...base, ...(data.context || {}) };
      // 需要追问（信息缺失）→ 记住已确认的槽位，下一轮带回
      if (data.need_info && data.followup_questions?.length) {
        setNlContext(ctx);
        setFollowupQ(data.followup_questions);
        // 追问文案由上方琥珀色卡片承载（问题 + 已记住的槽位），这里不再复述一遍
        setNlMsg('');
        return;
      }
      setFollowupQ([]);
      setNlContext(null);
      // 有可用时段 → 展示结果
      if (data.available_slots?.length) {
        const course = pickCourseFromContext(ctx);
        setSelectedCourse(course);
        // 课程名认出来了、但同名跨班无法唯一确定 → 用课程名收窄下拉（2 个班里挑，
        // 而不是 13 门里挑）；其余情况清空，免得把旧提示带到下一轮
        setCourseHint(!course && ctx.course_name ? ctx.course_name : '');
        if (ctx.target_week) setTargetWeek(ctx.target_week);
        setAvailable(data.available_slots);
        setExcluded(data.excluded_slots || []);
        setImpact(null);
        setRequestStatus('');
        setAiPlans([]);
        setPlansLoading(false);
        // 只取首行，是给这条灰字摘要用的（明细在沙盘页里）。
        //
        // ⚠️ R33：但「课程没认出来」的警告**也**在 message 里，位置在末尾
        // （实测 35 行，警告在第 34 行）。只取首行 ⇒ 教秘一个字都看不到，
        // 屏幕上只有一句 ✅，而他对“要调哪门课”其实一无所知。
        // R21 写的是“只把这件事说出口”，但出口在 message 尾部，
        // 前端从不展示 message 全文 —— 提示从来没有真正到达用户。
        let summary = '✅ ' + r.data.message.split(String.fromCharCode(10))[0];
        if (data.course_unrecognized) {
          summary += '｜⚠️ 没认出是哪门课，请在下方「选择课程」里确认';
        }
        setNlMsg(summary);
        setView('reschedule');
        // 课程识别不出来（同名跨班/没提课程）时沙盘照常打开，由教秘在下拉里补选
        if (course && ctx.target_week) scheduleAiPlans(course, ctx.target_week);
        return;
      }
      // 后端明确拒绝（如周次不在实验安排表范围内）：必须标成失败态。
      // 这类消息原来是灰字，和"✅ 第7周可用时段"看起来是同一种反馈，
      // 教秘读不出"这条路走不通"。
      if (r.data.success === false) {
        // 拒绝时同样要清空：否则屏幕上留着上一次的沙盘，而请输入框已清空，
        // 教秘看到的是「❌ 范围外」+ 一片绿格子，两者互相矛盾。
        setAvailable([]);
        setExcluded(data.excluded_slots || []);
        setAiPlans([]);
        setPlansLoading(false);
        setNlMsg(`❌ ${r.data.message}`);
        return;
      }
      // success=true 但一个时段都没有 —— 通常是「第N周该教师名下所有课时段
      // 都已被占满」。这条也必须标成失败态，且**同样要清掉上一轮的沙盘**：
      // 原来它走的是最后那行灰字，既不报错也不打开沙盘，教秘输入框清空后
      // 屏幕上仍是上一次查询的绿格子 —— 看着像这次也成功了。
      setAvailable([]);
      setExcluded(data.excluded_slots || []);
      setAiPlans([]);
      setPlansLoading(false);
      setNlMsg(`❌ ${r.data.message}`);
      return;
    } catch (err: any) {
      setNlMsg(`❌ 请求失败：${describeApiError(err)}`);
    } finally {
      setLoading(false);
      setNlInput('');
    }
  };

  // 返回身份选择
  const handleBackToIdentity = () => {
    plansSeqRef.current += 1;
    setAiPlans([]);
    setPlansLoading(false);
    setNlContext(null);
    setFollowupQ([]);
    setNlMsg('');
    setCourseHint('');
    setView('identity');
    setCurrentTeacher('');
    setCourses([]);
  };

  // F6: 管理员进入审批台（需云登录 + 管理员口令）
  const handleOpenAdmin = async () => {
    if (!session && !demoMode) {
      openLogin('admin');   // 第一步：云身份（演示模式下跳过）
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
      if (demoMode) {
        const lr = await rescheduleApi.localRequests(res.data.token);
        setRequests((lr.data.requests || []).map(toUiRequest));
      } else {
        await loadCloudRequests('pending');
      }
      setView('admin');
    } catch (err: any) {
      // 401 才叫「口令错」；其余（网络/500）不能误导成密码问题。
      if (err?.response?.status === 401) {
        setAdminLoginErr(err?.response?.data?.detail || '管理员账号或密码错误');
      } else {
        setMsg(`管理员登录失败：${describeApiError(err)}`);
      }
    } finally {
      setLoading(false);
    }
  };

  // F6: 管理员审批（云端更新状态 → 再落地到排课数据）
  //
  // 云端只存工单状态；"调课真的生效"是另一件事，必须由服务端把排课记录移过去。
  // 这两步没法合成一个请求（前端写云表、后端改内存，两条通道），所以第一步成功后
  // 立刻做第二步，并把第二步的结果原样告诉教秘。
  // "批了但没生效"是最坏的一种静默失败：审批台显示已同意，课表却没动，谁都不会再查。
  const handleApprove = async (id: number, approve: boolean, comment: string) => {
    setLoading(true);
    if (demoMode) {
      // 演示模式：/requests/{id}/approve 在服务端直接把课表改掉（并写 applied_moves），
      // 前端只需刷新列表 + 提示结果，不用再走云的 approveCloudRequest。
      try {
        const token = getAdminToken() || '';
        const r = await rescheduleApi.approveLocalRequest(id, approve, comment, token);
        const lr = await rescheduleApi.localRequests(token);
        setRequests((lr.data.requests || []).map(toUiRequest));
        setMsg(r.data.message || (approve ? '已同意' : '已驳回'));
      } catch (err: any) {
        setMsg(`审批失败：${describeApiError(err)}`);
      } finally {
        setLoading(false);
      }
      return;
    }
    try {
      const row = await approveCloudRequest(id, approve, comment, userName);
      await loadCloudRequests('pending');
      if (!approve) {
        setMsg(`已驳回：${row.course_name || '该申请'}`);
        return;
      }
      if (!getAdminToken()) {
        setMsg('⚠️ 已同意，但本机没有管理员会话，排课表没有自动更新。请重新登录审批台后再处理。');
        return;
      }
      try {
        const r = await rescheduleApi.applyMove({
          teacher: row.teacher || '',
          course_name: row.course_name || '',
          class_name: row.class_name || '',
          original_time: row.original_time || '',
          target_week: row.target_week,
          key: `cloud-${row.id}`,
        });
        setMsg(r.data.message);
      } catch (err: any) {
        // 审批已经写进云表了，这一步失败只影响"自动落地"，必须说清楚哪个成了哪个没成
        setMsg(`⚠️ 审批已记录，但排课表未自动更新：${describeApiError(err)}（需人工调整）`);
      }
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

        {/* 主导航（排课调课 / 并行排课 / 数据看板 / 周课表 / 主数据 / 实验规范查询 / 审批） */}
        <div className="max-w-4xl mx-auto px-4 pb-3">
          <div className="flex items-center gap-2 text-sm">
            <button
              onClick={() => setView('identity')}
              className={`px-4 py-2 rounded-lg transition-colors ${
                ['identity', 'courses', 'reschedule'].includes(view) && !['tutor', 'admin'].includes(view)
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
              onClick={handleOpenGrid}
              className={`px-4 py-2 rounded-lg transition-colors ${
                view === 'grid' ? 'bg-teal-600 text-white' : 'bg-gray-100 text-gray-600 hover:bg-gray-200'
              }`}
            >
              📅 周课表
            </button>
            <button
              onClick={() => setView('master')}
              className={`px-4 py-2 rounded-lg transition-colors ${
                view === 'master' ? 'bg-teal-600 text-white' : 'bg-gray-100 text-gray-600 hover:bg-gray-200'
              }`}
            >
              🗂️ 主数据
            </button>
            <button
              onClick={() => handleOpenModule()}
              className={`px-4 py-2 rounded-lg transition-colors ${
                view === 'tutor' ? 'bg-purple-600 text-white' : 'bg-gray-100 text-gray-600 hover:bg-gray-200'
              }`}
            >
              🔬 实验规范查询
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

      {/*
        R67：横幅样式原本**写死红色**，而 msg 的来源里既有失败也有成功：
          setMsg(`✅ 全校 17 个班全部排定…`)   ← 成功
          setMsg(`已驳回：大学物理实验`)        ← 正常操作
          setMsg(`审批失败：…`)                ← 失败
        于是教秘点「同意」后弹出 `✅ 已移动 3 条` 是**红底红字**，看起来像出错了。

        按文案前缀分三档：
          ✅ / 已…        → 绿（成功）
          ❌ / ⚠️ / 失败  → 红（失败）
          其余            → 中性（正常结果，如「已驳回」）
      */}
      {msg && (
        <div ref={msgBannerRef} className="max-w-4xl mx-auto px-4 pt-3">
          <div
            className={`text-sm px-4 py-2.5 rounded-lg border flex items-start gap-3 ${
              msgTone === 'ok'
                ? 'bg-emerald-50 text-emerald-700 border-emerald-200'
                : msgTone === 'err'
                  ? 'bg-red-50 text-red-600 border-red-200'
                  : 'bg-gray-50 text-gray-700 border-gray-200'
            }`}
          >
            <span className="flex-1 break-words">{msg}</span>
            <button
              type="button"
              onClick={() => setMsg('')}
              aria-label="关闭消息"
              className="shrink-0 opacity-60 hover:opacity-100 leading-none"
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
              loadError={loadError}
              autoScheduleLoading={autoScheduleLoading}
              autoScheduleMsg={autoScheduleMsg}
              autoScheduleCount={autoScheduleRows.length}
              onOpenAdmin={handleOpenAdmin}
              onAutoSchedule={handleAutoSchedule}
              onExportAutoSchedule={handleAutoScheduleExport}
              onRetryLoad={loadTeacherList}
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
              nlContext={nlContext}
              onNlInputChange={setNlInput}
              onNlSubmit={handleNlReschedule}
              onReschedule={handleReschedule}
            />
          )}

          {/* 页面3：调课结果（F3/F4） */}
          {view === 'reschedule' && (
            <RescheduleView
              selectedCourse={selectedCourse}
              courseOptions={courses.filter((c) => c.adjustable && (!courseHint || c.course_name === courseHint))}
              onSelectCourse={handlePickCourse}
              currentTeacher={currentTeacher}
              targetWeek={targetWeek}
              available={available}
              excluded={excluded}
              showAllAvail={showAllAvail}
              impact={impact}
              impactLoading={impactLoading}
              requestStatus={requestStatus}
              loading={loading}
              aiPlans={aiPlans}
              aiPlansSource={aiPlansSource}
              planLoading={plansLoading}
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
                schoolPlan={schoolPlan}
                schoolSolving={schoolSolving}
                adminAuthed={adminAuthed}
                onRequireLogin={handleOpenAdmin}
                onMsg={setMsg}
                onSolveSchool={handleSolveSchool}
                onExportSchool={handleExportSchool}
                resultRef={parallelResultRef}
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
                auditError={auditError}
                statsData={statsData}
                statsError={statsError}
                onRetry={handleOpenDashboard}
                onExportClass={() => handleExportStandard('class')}
                onExportRoom={() => handleExportStandard('room')}
                onExportMaster={() => handleExportStandard('master')}
                exportingKind={exportingKind}
              />
            </Suspense>
          )}

          {/* 页面6.6：📅 周课表网格（R-62：排课结果可视化，落地后的一眼确认面） */}
          {view === 'grid' && (
            <Suspense fallback={<ViewFallback />}>
              <ScheduleGridView
                week={gridWeek}
                data={gridData}
                loading={gridLoading}
                error={gridError}
                classFilter={gridClass}
                roomFilter={gridRoom}
                onWeekChange={handleGridWeekChange}
                onClassChange={handleGridClassChange}
                onRoomChange={handleGridRoomChange}
                onRetry={handleOpenGrid}
              />
            </Suspense>
          )}

          {/* 页面6.5：🗂️ 主数据管理（导师一号行动项：可增删改的实体库） */}
          {view === 'master' && (
            <Suspense fallback={<ViewFallback />}>
              <MasterDataView adminAuthed={adminAuthed} onRequireLogin={handleOpenAdmin} />
            </Suspense>
          )}

          {/* 页面7：实验规范查询（云端大模型，流式问答） */}
          {view === 'tutor' && (
            <Suspense fallback={<ViewFallback />}>
              <ConsultView
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
