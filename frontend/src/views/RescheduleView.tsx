import { Fragment } from 'react';
import type { AvailableSlot, ExcludedSlot, TeacherCourse } from '../api/client';
import type { ImpactResult, AiPlan } from '../types';
import { GRID_TIME_SLOTS, WEEKDAYS, timeSlotLabels, weekdayLabels } from '../constants/labels';

interface Props {
  /**
   * 当前要调的课程。**允许为 null**：沙盘算的是"这一周还有哪些时段空着"，
   * 这件事只跟教师/周次有关，与具体哪门课无关（`find_slots_with_reasons` 根本不收课程）。
   * 自然语言路径未必能唯一确定课程（没提课程名，或同名实验挂在多个班下），
   * 过去是"没有课程就整页不渲染" ⇒ 解析成功却白屏。现在降级成可选项，由下拉补选。
   */
  selectedCourse: TeacherCourse | null;
  /** 可补选的课程（自然语言未唯一确定课程时用） */
  courseOptions?: TeacherCourse[];
  onSelectCourse?: (course: TeacherCourse) => void;
  currentTeacher: string;
  targetWeek: number;
  available: AvailableSlot[];
  excluded: ExcludedSlot[];
  showAllAvail: boolean;
  impact: ImpactResult | null;
  impactLoading: boolean;
  requestStatus: string;
  loading: boolean;
  /** AI 候选方案（后台加载）：到达前 planLoading 为真，显示"正在挑"占位 */
  aiPlans?: AiPlan[];
  planLoading?: boolean;
  onBack: () => void;
  onTargetWeekChange: (week: number) => void;
  onToggleShowAll: () => void;
  onImpactAnalysis: () => void;
  onSubmit: () => void;
}

/** 页面3：调课结果（F3/F4）—— 周沙盘 + 可用/排除时段 + 影响分析 + 提交。 */
export default function RescheduleView({
  selectedCourse, courseOptions = [], onSelectCourse,
  currentTeacher, targetWeek, available, excluded, showAllAvail,
  impact, impactLoading, requestStatus, loading, aiPlans = [], planLoading = false,
  onBack, onTargetWeekChange, onToggleShowAll, onImpactAnalysis, onSubmit,
}: Props) {
  const courseLabel = selectedCourse ? selectedCourse.course_name : '未指定课程';
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
        {selectedCourse ? (
          <>
            <div className="text-sm text-gray-600 mt-2">
              课程：{selectedCourse.course_name}（{selectedCourse.class_name}）
            </div>
            <div className="text-sm text-gray-600 mt-1">
              原时间：{selectedCourse.original_time} · {selectedCourse.room} · {selectedCourse.week_range}
            </div>
          </>
        ) : (
          <div className="mt-2">
            <div className="text-sm text-amber-700">
              自然语言没说清是哪门课（或同名课挂在多个班下），请确认要调整的课程：
            </div>
            <select
              value=""
              onChange={(e) => {
                const c = courseOptions.find((x) => `${x.course_name}|${x.class_name}` === e.target.value);
                if (c && onSelectCourse) onSelectCourse(c);
              }}
              className="mt-2 w-full max-w-md px-3 py-2 border border-amber-300 bg-amber-50 rounded-lg text-sm focus:ring-2 focus:ring-amber-400 outline-none"
            >
              <option value="">— 选择课程（共 {courseOptions.length} 个可选）—</option>
              {courseOptions.map((c) => (
                <option key={`${c.course_name}|${c.class_name}`} value={`${c.course_name}|${c.class_name}`}>
                  {c.course_name}（{c.class_name}）
                </option>
              ))}
            </select>
          </div>
        )}
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

      {/* AI 候选方案（F9）—— 后台加载，通常晚于沙盘 5 秒以上，故有独立占位 */}
      {(planLoading || aiPlans.length > 0) && (
        <div className="bg-white rounded-xl border border-indigo-200 p-5 mb-6">
          <h3 className="font-medium text-indigo-800 mb-1">🤖 AI 推荐候选方案</h3>
          <p className="text-xs text-gray-400 mb-3">
            从 {available.length} 个可用时段里挑几个更合适的 —— 只是建议，上面所有可用时段都能选
          </p>
          {planLoading && aiPlans.length === 0 ? (
            <div className="flex items-center gap-2 text-sm text-gray-400 py-2">
              <span className="w-4 h-4 border-2 border-indigo-200 border-t-indigo-500 rounded-full animate-spin" />
              正在从可用时段里挑候选…
            </div>
          ) : (
            <div className="grid gap-3 sm:grid-cols-3">
              {aiPlans.slice(0, 3).map((p, i) => (
                <div
                  key={`${p.weekday}-${p.time_slot}-${p.room}`}
                  className="bg-indigo-50 border border-indigo-100 rounded-lg p-3"
                >
                  <div className="text-xs text-indigo-500 mb-1">方案 {i + 1}</div>
                  <div className="text-sm font-medium text-gray-800">
                    {weekdayLabels[p.weekday] || p.weekday} {timeSlotLabels[p.time_slot] || p.time_slot}
                  </div>
                  <div className="text-xs text-gray-500 mt-0.5">{p.room}</div>
                  {p.reason && <div className="text-xs text-indigo-600 mt-1.5">{p.reason}</div>}
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {/* 冲突影响分析（差异化功能） */}
      <div className="bg-white rounded-xl border border-orange-200 p-5 mb-6">
        <h3 className="font-medium text-orange-800 mb-2">🎯 冲突影响分析</h3>
        <p className="text-sm text-gray-600 mb-3">
          查看把「{courseLabel}」调到第{targetWeek}周，会影响到哪些班级、教室和教师
        </p>
        <button
          onClick={onImpactAnalysis}
          // 影响分析的后端入参里带课程名；没有课程时 App 层直接 return，
          // 按钮可点却毫无反应是最难查的一类"故障"（不报错、不提示）。
          disabled={impactLoading || !selectedCourse}
          className="px-5 py-2.5 bg-orange-500 text-white rounded-lg hover:bg-orange-600 disabled:opacity-50 text-sm font-medium transition-colors"
        >
          {impactLoading ? '⏳ 分析中...' : '分析影响范围'}
        </button>
        {!selectedCourse && (
          <span className="ml-3 text-xs text-amber-600">请先选择要调整的课程</span>
        )}

        {impact && (
          <div className="mt-4 space-y-3">
            <div className="text-sm font-medium text-gray-800">
              共 {impact.conflict_count} 处冲突
            </div>
            {/*
              自身冲突必须单列：它和"受影响班级/教室/教师"是两件事。
              以前后端把 teacher_busy（本人该时段已有课）的班级名塞进了「受影响教师」，
              于是教秘看到"受影响教师：2024级应用化学1、2班"——一个班级出现在教师栏里。
              现在后端给结构化字段，这里按语义分开展示。
            */}
            {impact.self_busy_count ? (
              <div className="bg-orange-50 border border-orange-200 rounded-lg p-3">
                <div className="text-xs font-medium text-orange-700 mb-1">
                  🙋 您本人在这些时段已有课（{impact.self_busy_count} 处）
                </div>
                <div className="text-sm text-orange-600 space-y-0.5">
                  {impact.self_busy_slots?.map((s, i) => (
                    <div key={i}>
                      {weekdayLabels[s.weekday]} {(timeSlotLabels[s.time_slot] || s.time_slot)}
                      {s.class_name && <span className="text-orange-500"> · {s.class_name}</span>}
                      {s.experiment && <span className="text-orange-400"> {s.experiment}</span>}
                    </div>
                  ))}
                </div>
              </div>
            ) : null}
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
                <div className="text-xs font-medium text-purple-700 mb-1">👨‍🏫 相关教师（该时段在用这些教室）</div>
                <div className="text-sm text-purple-600">{impact.affected_teachers.join('、')}</div>
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
          {currentTeacher} · {courseLabel} → 第{targetWeek}周（提交后由管理员审批）
        </p>
        {requestStatus ? (
          <div className="text-sm px-4 py-3 bg-green-50 border border-green-200 text-green-700 rounded-lg">
            {requestStatus}
          </div>
        ) : (
          // 没确定课程就不许提交：提交会把 course_name/class_name/原时间写进云端工单，
          // 空字段进去就成了脏数据，审批台上也看不出要调哪门课。
          <div>
            <button
              onClick={onSubmit}
              disabled={loading || !selectedCourse}
              className="px-6 py-2.5 bg-blue-600 text-white rounded-lg hover:bg-blue-700 disabled:opacity-50 text-sm font-medium transition-colors"
            >
              {loading ? '提交中...' : '提交调课申请'}
            </button>
            {!selectedCourse && (
              <span className="ml-3 text-xs text-amber-600">请先在上方选择要调整的课程</span>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
