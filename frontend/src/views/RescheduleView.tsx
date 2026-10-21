import { Fragment } from 'react';
import type { AvailableSlot, ExcludedSlot, TeacherCourse } from '../api/client';
import type { ImpactResult } from '../types';
import { GRID_TIME_SLOTS, WEEKDAYS, timeSlotLabels, weekdayLabels } from '../constants/labels';

interface Props {
  selectedCourse: TeacherCourse;
  currentTeacher: string;
  targetWeek: number;
  available: AvailableSlot[];
  excluded: ExcludedSlot[];
  showAllAvail: boolean;
  impact: ImpactResult | null;
  impactLoading: boolean;
  requestStatus: string;
  loading: boolean;
  onBack: () => void;
  onTargetWeekChange: (week: number) => void;
  onToggleShowAll: () => void;
  onImpactAnalysis: () => void;
  onSubmit: () => void;
}

/** 页面3：调课结果（F3/F4）—— 周沙盘 + 可用/排除时段 + 影响分析 + 提交。 */
export default function RescheduleView({
  selectedCourse, currentTeacher, targetWeek, available, excluded, showAllAvail,
  impact, impactLoading, requestStatus, loading,
  onBack, onTargetWeekChange, onToggleShowAll, onImpactAnalysis, onSubmit,
}: Props) {
  return (
    <div>
      <button
        onClick={onBack}
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
            onChange={(e) => onTargetWeekChange(Number(e.target.value))}
            className="px-3 py-2 border border-gray-300 rounded-lg text-sm focus:ring-2 focus:ring-blue-500 outline-none"
          >
            {Array.from({ length: 16 }, (_, i) => i + 1).map((w) => (
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
          {WEEKDAYS.map((wd) => (
            <div key={wd} className="text-center text-xs font-medium text-gray-500 py-1">{weekdayLabels[wd]}</div>
          ))}
          {GRID_TIME_SLOTS.map((ts) => (
            <Fragment key={ts}>
              <div className="text-xs text-gray-500 flex items-center justify-center">
                {timeSlotLabels[ts] || ts}
              </div>
              {WEEKDAYS.map((wd) => {
                const availSlot = available.find((s) => s.weekday === wd && s.time_slot === ts);
                const exclSlot = excluded.find((e) => e.weekday === wd && e.time_slot === ts);
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
            </Fragment>
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
            {WEEKDAYS.map((wd) => {
              const daySlots = available.filter((s) => s.weekday === wd).slice(0, 4);
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
            onClick={onToggleShowAll}
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
          onClick={onImpactAnalysis}
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
            onClick={onSubmit}
            disabled={loading}
            className="px-6 py-2.5 bg-blue-600 text-white rounded-lg hover:bg-blue-700 disabled:opacity-50 text-sm font-medium transition-colors"
          >
            {loading ? '提交中...' : '提交调课申请'}
          </button>
        )}
      </div>
    </div>
  );
}
