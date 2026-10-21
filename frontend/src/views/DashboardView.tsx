import type { AuditData, StatsData } from '../types';
import { timeSlotLabels } from '../constants/labels';

interface Props {
  auditLoading: boolean;
  auditData: AuditData | null;
  statsData: StatsData | null;
}

/** 📊 数据看板：安排表体检报告 + 利用率/工作量统计。 */
export default function DashboardView({ auditLoading, auditData, statsData }: Props) {
  return (
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
                <h3 className="text-sm font-medium text-gray-800 mb-2">🏫 教室冲突明细（前 {auditData.room_conflicts!.length} 条）</h3>
                <div className="space-y-2">
                  {auditData.room_conflicts!.map((c, i) => (
                    <div key={i} className="bg-red-50 border border-red-100 rounded-lg p-3 text-sm">
                      <div className="font-medium text-red-700">
                        第{c.week}周 {c.weekday} {timeSlotLabels[c.time_slot] || c.time_slot} · {c.place}
                      </div>
                      {c.detail.map((d, j) => (
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
                <h3 className="text-sm font-medium text-gray-800 mb-2">👨‍🏫 教师冲突明细（前 {auditData.teacher_conflicts!.length} 条）</h3>
                <div className="space-y-2">
                  {auditData.teacher_conflicts!.map((c, i) => (
                    <div key={i} className="bg-orange-50 border border-orange-100 rounded-lg p-3 text-sm">
                      <div className="font-medium text-orange-700">
                        第{c.week}周 {c.weekday} {timeSlotLabels[c.time_slot] || c.time_slot} · {c.place}
                      </div>
                      {c.detail.map((d, j) => (
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
                  {statsData.top_rooms.map((r) => (
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
                  {statsData.top_teachers.map((t) => (
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
  );
}
