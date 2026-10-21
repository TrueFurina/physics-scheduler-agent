import type { Teacher } from '../api/client';

interface Props {
  teachers: Teacher[];
  /** 首屏加载失败的原因；非空表示"老师一个都没有"不是因为没数据，而是因为出错了 */
  loadError: string;
  autoScheduleLoading: boolean;
  autoScheduleMsg: string;
  onOpenAdmin: () => void;
  onAutoSchedule: () => void;
  onRetryLoad: () => void;
  onSelectTeacher: (name: string) => void;
}

/**
 * 页面1：身份选择（F1）。
 *
 * 纯展示：所有状态与请求都留在 App 里，这里只负责长相。
 * 教师姓名是业务主体（教秘按姓名发起调课），不做脱敏。
 */
export default function IdentityView({
  teachers, loadError, autoScheduleLoading, autoScheduleMsg, onOpenAdmin, onAutoSchedule, onRetryLoad, onSelectTeacher,
}: Props) {
  return (
    <div>
      <div className="flex items-center justify-between mb-4">
        <h2 className="text-lg font-semibold text-gray-900">请选择您的身份（教师）</h2>
        <button
          onClick={onOpenAdmin}
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
            <p className="text-sm text-gray-500 mt-1">基于约束求解器生成排课初稿：教室/教师/班级互斥，并逐周避开现有安排表已占用的时段</p>
          </div>
          <button
            onClick={onAutoSchedule}
            disabled={autoScheduleLoading}
            className="px-5 py-2.5 bg-blue-600 text-white rounded-lg hover:bg-blue-700 disabled:opacity-50 text-sm font-medium transition-colors shrink-0"
          >
            {autoScheduleLoading ? '⏳ 排课中...' : '一键自动排课'}
          </button>
        </div>
        {autoScheduleMsg && (
          <div className="mt-4 px-4 py-3 bg-gray-50 border border-gray-200 rounded-lg text-sm text-gray-700 whitespace-pre-wrap max-h-96 overflow-y-auto">
            {autoScheduleMsg}
          </div>
        )}
      </div>

      <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-4">
        {teachers.map((t) => (
          <button
            key={t.name}
            onClick={() => onSelectTeacher(t.name)}
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
        // 两种"没有老师"必须分开说：
        //  - 服务端压根没数据      → 提示去找安排表（现状，保留）
        //  - 接口报错了（loadError）→ 说清是加载失败，并给一个当场能点的重试。
        //    原先两种都写"请先加载实验安排表"，而界面上没有任何加载入口，
        //    教秘照做会卡死在一个不存在的按钮上（实测：把 /teachers 拦成 500 时就是这样）。
        loadError ? (
          <div className="text-center py-16">
            <div className="text-gray-500">教师列表没能加载出来</div>
            <div className="text-sm text-gray-400 mt-2 max-w-xl mx-auto break-words">{loadError}</div>
            <button
              type="button"
              onClick={onRetryLoad}
              className="mt-4 px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 text-sm"
            >
              🔄 重新加载
            </button>
          </div>
        ) : (
          <div className="text-center text-gray-400 py-16">暂无教师数据，请先加载实验安排表</div>
        )
      )}
    </div>
  );
}
