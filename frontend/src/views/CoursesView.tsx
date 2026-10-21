import type { TeacherCourse } from '../api/client';

interface Props {
  currentTeacher: string;
  courses: TeacherCourse[];
  loading: boolean;
  nlInput: string;
  nlMsg: string;
  followupQ: string[];
  onNlInputChange: (v: string) => void;
  onNlSubmit: () => void;
  onReschedule: (course: TeacherCourse) => void;
}

/** 页面2：我的课程（F2）—— 可调/不可调分组 + 自然语言调课入口。 */
export default function CoursesView({
  currentTeacher, courses, loading, nlInput, nlMsg, followupQ,
  onNlInputChange, onNlSubmit, onReschedule,
}: Props) {
  const adjustable = courses.filter((c) => c.adjustable);
  const notAdjustable = courses.filter((c) => !c.adjustable);

  return (
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
        {adjustable.map((c, i) => (
          <div key={i} className="bg-white rounded-xl p-4 shadow-sm border border-gray-200 flex items-center justify-between">
            <div className="flex-1 min-w-0">
              <div className="font-semibold text-gray-900">{c.course_name}</div>
              <div className="text-sm text-gray-500 mt-1">{c.class_name}</div>
              <div className="text-xs text-gray-400 mt-1">
                {c.original_time} · {c.room} · {c.week_range}
              </div>
            </div>
            <button
              onClick={() => onReschedule(c)}
              className="ml-4 px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 text-sm font-medium transition-colors shrink-0"
            >
              申请调课 →
            </button>
          </div>
        ))}
        {adjustable.length === 0 && (
          <div className="text-gray-400 text-sm text-center py-6">没有可调课程</div>
        )}
      </div>

      {/* 不可调课程 */}
      <h3 className="font-medium text-red-600 mb-3">❌ 不可调课程</h3>
      <div className="space-y-3">
        {notAdjustable.map((c, i) => (
          <div key={i} className="bg-gray-50 rounded-xl p-4 border border-gray-200 opacity-80">
            <div className="font-semibold text-gray-500">{c.course_name}</div>
            <div className="text-sm text-gray-400 mt-1">{c.class_name}</div>
            <div className="text-xs text-red-500 mt-2">
              ⚠️ {c.adjustable_reason || '该课程不可调'}
            </div>
          </div>
        ))}
        {notAdjustable.length === 0 && (
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
            onChange={(e) => onNlInputChange(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && onNlSubmit()}
            placeholder="输入大白话调课需求..."
            className="flex-1 px-3 py-2 border border-gray-300 rounded-lg text-sm focus:ring-2 focus:ring-blue-500 outline-none"
            disabled={loading}
          />
          <button
            onClick={onNlSubmit}
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
  );
}
