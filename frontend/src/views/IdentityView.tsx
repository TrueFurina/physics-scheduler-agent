import type { Teacher } from '../api/client';

interface Props {
  teachers: Teacher[];
  autoScheduleLoading: boolean;
  autoScheduleMsg: string;
  onOpenAdmin: () => void;
  onAutoSchedule: () => void;
  onSelectTeacher: (name: string) => void;
}

/**
 * 页面1：身份选择（F1）。
 *
 * 纯展示：所有状态与请求都留在 App 里，这里只负责长相。
 * 教师姓名是业务主体（教秘按姓名发起调课），不做脱敏。
 */
export default function IdentityView({
  teachers, autoScheduleLoading, autoScheduleMsg, onOpenAdmin, onAutoSchedule, onSelectTeacher,
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
            <p className="text-sm text-gray-500 mt-1">基于约束求解器，自动生成完整排课方案（教室/教师/学生三重不冲突）</p>
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
          <div className="mt-4 px-4 py-3 bg-gray-50 border border-gray-200 rounded-lg text-sm text-gray-700 whitespace-pre-wrap max-h-64 overflow-y-auto">
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
        <div className="text-center text-gray-400 py-16">暂无教师数据，请先加载实验安排表</div>
      )}
    </div>
  );
}
