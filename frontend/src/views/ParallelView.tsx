import { Fragment } from 'react';
import type { RefObject } from 'react';
import type { ParallelCourse, ParallelSolveResult } from '../types';
import { GRID_TIME_SLOTS, timeSlotLabels } from '../constants/labels';

// 周六/周日也是真实上课日（真实表 71/433 条在周末，"金工 5、6 周排周六"是常态）。
// 原先只给周中五个按钮，周末班连"选对日子"的入口都没有。
const WEEKDAY_OPTIONS = ['', '周一', '周二', '周三', '周四', '周五', '周六', '周日'];

interface Props {
  weekday: string;
  courses: ParallelCourse[];
  selected: string;
  loading: boolean;
  solving: boolean;
  result: ParallelSolveResult | null;
  viewWeek: number | null;
  resultRef: RefObject<HTMLDivElement>;
  onWeekdayChange: (w: string) => void;
  onSolve: (className: string) => void;
  onExport: () => void;
  onViewWeekChange: (w: number) => void;
}

/** ⚡ 并行排课：一门课的全部实验项目 × 学生组别一次列出，CP-SAT 一键统一分配。 */
export default function ParallelView({
  weekday, courses, selected, loading, solving, result, viewWeek, resultRef,
  onWeekdayChange, onSolve, onExport, onViewWeekChange,
}: Props) {
  // 周沙盘：只渲染被选中的那一周，行=时段 列=学生组
  const weeks = Object.keys(result?.by_week || {}).map(Number).sort((a, b) => a - b);
  const activeWeek = viewWeek ?? weeks[0];
  const weekSessions = (result?.schedule || []).filter((s) => s.week === activeWeek);
  const groups: string[] = Array.from(new Set<string>(weekSessions.map((s) => String(s.group))));

  return (
    <main className="max-w-4xl mx-auto px-4 pb-10">
      <div className="bg-white rounded-xl border border-gray-200 p-5 mb-6">
        <h2 className="text-lg font-semibold text-gray-900 mb-1">⚡ 并行排课</h2>
        <p className="text-sm text-gray-500 mb-3">
          一门课的全部实验项目 × 学生组别一次列出，CP-SAT 约束求解一键统一分配周次/时段/场地（每组每周一个实验，场地/教师互斥）
        </p>

        {/* 上课星期选择（默认自动沿用历史最常用星期） */}
        <div className="flex items-center gap-2 mb-4 flex-wrap">
          <span className="text-xs text-gray-500">上课星期：</span>
          {WEEKDAY_OPTIONS.map((w) => (
            <button
              key={w || 'auto'}
              onClick={() => onWeekdayChange(w)}
              className={`px-2.5 py-1 rounded-lg text-xs transition-colors ${
                weekday === w
                  ? 'bg-cyan-600 text-white'
                  : 'bg-gray-100 text-gray-600 hover:bg-gray-200'
              }`}
            >
              {w || '自动'}
            </button>
          ))}
        </div>

        {loading && <div className="text-sm text-gray-400 py-4">加载课程总览中...</div>}

        {/* 课程总览卡片 */}
        <div className="grid gap-3 md:grid-cols-2">
          {courses.map((c) => (
            <div key={c.class_name}
              className={`border rounded-xl p-4 transition-colors ${
                selected === c.class_name ? 'border-cyan-400 bg-cyan-50' : 'border-gray-200 hover:border-cyan-300'
              }`}>
              <div className="flex items-start justify-between gap-2">
                <div>
                  <div className="font-medium text-gray-900 text-sm">{c.class_name}</div>
                  <div className="text-xs text-gray-500 mt-1">
                    常上{c.weekday} · {c.projects.length} 个实验项目 · {c.total_units} 个教学单元
                  </div>
                  {!!c.weekend_sessions && (
                    <div className="text-xs text-amber-600 mt-0.5">
                      历史课次含周末 {c.weekend_sessions} 节（
                      {Object.entries(c.weekday_mix || {}).map(([d, n]) => `${d} ${n}`).join(' / ')}）
                    </div>
                  )}
                  {/* 待排单元：大纲里新增、此前从没排过时间的实验项目。
                      没有这个提示，教秘会以为系统只会"把去年的课重排一遍"。 */}
                  {!!c.pending_units && (
                    <div className="text-xs text-cyan-700 mt-0.5">
                      含 {c.pending_units} 个待排单元（大纲新增，此前没排过时间）
                    </div>
                  )}
                </div>
                <button
                  onClick={() => onSolve(c.class_name)}
                  disabled={solving}
                  className="px-3 py-1.5 bg-cyan-600 text-white rounded-lg hover:bg-cyan-700 disabled:opacity-50 text-xs font-medium whitespace-nowrap"
                >
                  {solving && selected === c.class_name ? '⏳ 求解中...' : '一键排课'}
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
      {result && (
        <div ref={resultRef} className="bg-white rounded-xl border border-cyan-200 p-5">
          <h3 className="font-medium text-gray-900 mb-1">
            📋 排课方案（{result.weekday}）· 状态 {result.status}
          </h3>
          <p className="text-xs text-gray-500 mb-4">
            {result.scheduled}/{result.total_units} 个单元已排定，用 {result.weeks_used} 周
            ｜ 按周分布：{Object.entries(result.by_week || {}).map(([w, n]) => `第${w}周${n}个`).join('、')}
            <button
              onClick={onExport}
              className="ml-3 px-3 py-1 bg-emerald-600 text-white rounded-lg hover:bg-emerald-700 text-xs font-medium"
            >
              ⬇️ 导出 Excel
            </button>
          </p>

          {/* 周沙盘：按周切换，行=时段 列=学生组 */}
          {activeWeek ? (
            <div className="mb-5">
              <div className="flex items-center gap-2 mb-2 flex-wrap">
                <span className="text-xs font-medium text-gray-500">周沙盘（{result.weekday}）：</span>
                {weeks.map((w) => (
                  <button
                    key={w}
                    onClick={() => onViewWeekChange(w)}
                    className={`px-2.5 py-1 rounded-lg text-xs transition-colors ${
                      activeWeek === w ? 'bg-cyan-600 text-white' : 'bg-gray-100 text-gray-600 hover:bg-gray-200'
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
                {GRID_TIME_SLOTS.map((ts) => (
                  <Fragment key={ts}>
                    <div className="text-xs text-gray-500 flex items-center justify-center">
                      {timeSlotLabels[ts] || ts}
                    </div>
                    {groups.map((g) => {
                      const s = weekSessions.find((x) => x.group === g && x.time_slot === ts);
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
          ) : null}

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
                {(result.schedule || []).slice(0, 40).map((s, i) => (
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
          {(result.schedule || []).length > 40 && (
            <p className="text-xs text-gray-400 mt-2">仅显示前 40 条，共 {result.schedule!.length} 条</p>
          )}
        </div>
      )}
    </main>
  );
}
