import type { AuditData, StatsData } from '../types';
import { timeSlotLabels } from '../constants/labels';

interface Props {
  auditLoading: boolean;
  auditData: AuditData | null;
  /** 体检失败原因。为空表示没失败过。 */
  auditError?: string;
  statsData: StatsData | null;
  statsError?: string;
  onRetry?: () => void;
  /** 导出教务两类标准课表（基于当前安排表：班级课表 / 场地课表） */
  onExportClass?: () => void;
  onExportRoom?: () => void;
  /** 导出基础数据台账（实验项目/教师/场地/班级 四张实体表） */
  onExportMaster?: () => void;
}

/** 📊 数据看板：安排表体检报告 + 利用率/工作量统计。 */
export default function DashboardView({
  auditLoading, auditData, auditError = '', statsData, statsError = '', onRetry,
  onExportClass, onExportRoom, onExportMaster,
}: Props) {
  // "没发现冲突"有两种：真干净，和"有一部分根本没检查"。后者不能标绿色。
  const noRoom = auditData?.missing_room_count ?? 0;
  const noSlot = auditData?.unparsed_slot_count ?? 0;
  const hasGap = noRoom > 0 || noSlot > 0;
  const gapText = '有 ' + [noRoom > 0 ? `${noRoom} 条记录没填场地` : '', noSlot > 0 ? `${noSlot} 条时段无法识别` : '']
    .filter(Boolean)
    .join('、');

  return (
    <main className="max-w-4xl mx-auto px-4 pb-10">
      <div className="bg-white rounded-xl border border-gray-200 p-5 mb-6">
        <h2 className="text-lg font-semibold text-gray-900 mb-1">📊 数据看板</h2>
        <p className="text-sm text-gray-500 mb-4">对现有实验安排表做一键体检：数据覆盖情况 + 教室/教师冲突扫描</p>

        {auditLoading && <div className="text-sm text-gray-400 py-4">体检扫描中...</div>}

        {/* 体检失败：必须给出明确的失败态。只留一张空白卡片会被读成"扫过了，没冲突" */}
        {!auditLoading && auditError && (
          <div className="rounded-xl p-4 border bg-red-50 border-red-200">
            <div className="text-sm font-medium text-red-700">⚠️ 体检没能完成，下面的数字都不是本次扫描的结果</div>
            <div className="text-xs text-red-500 mt-1 break-words">{auditError}</div>
            {onRetry && (
              <button
                onClick={onRetry}
                className="mt-3 px-3 py-1.5 bg-red-600 text-white rounded-lg hover:bg-red-700 text-xs font-medium"
              >
                重新体检
              </button>
            )}
          </div>
        )}

        {auditData && (
          <>
            {/* 健康总览 —— 三态：有冲突 / 无冲突但有数据缺口 / 全干净。
                "无冲突但有缺口"不能报成绿色「整体健康」：那 N 条根本没被检查过。 */}
            <div className={`rounded-xl p-4 mb-4 ${auditData.healthy && !hasGap
              ? 'bg-green-50 border border-green-200'
              : 'bg-amber-50 border border-amber-200'}`}>
              <div className="text-lg font-medium">
                {!auditData.healthy
                  ? '⚠️ 发现潜在冲突，建议核查'
                  : hasGap
                    ? `🟡 未发现资源冲突，但${gapText}`
                    : '✅ 安排表整体健康，未发现资源冲突'}
              </div>
              <div className="text-sm text-gray-600 mt-1">
                共 {auditData.total_entries} 条安排 ｜ {auditData.teacher_count} 位教师 ｜ {auditData.room_count} 间场地
                ｜ 覆盖第 {auditData.week_min}~{auditData.week_max} 周
                {(auditData.missing_room_count ?? 0) > 0 && `｜ ${auditData.missing_room_count} 条未填场地`}
                {(auditData.unparsed_slot_count ?? 0) > 0 && `｜ ${auditData.unparsed_slot_count} 条时段待确认`}
              </div>
            </div>

            {/* 数据缺口：时段填了看不懂的内容，不知道具体节次 ⇒ 该教室当天按整日占用处理。
                这条不能只写在后端日志里 —— 教秘得知道去哪张表改哪一格。 */}
            {(auditData.unparsed_slot_count ?? 0) > 0 && (
              <div className="mb-4 bg-amber-50 border border-amber-200 rounded-xl p-4">
                <div className="text-sm font-medium text-amber-800 mb-1">
                  ⚠️ {auditData.unparsed_slot_count} 条记录的「时段」无法识别，已按整日占用处理
                </div>
                <div className="text-xs text-amber-700">
                  {(auditData.unparsed_slot_samples || []).map((s, i) => (
                    <div key={i}>· {s}</div>
                  ))}
                </div>
                <div className="text-xs text-amber-600 mt-2">
                  这些教室当天不会再被推荐为可调时段（宁可少推荐，也不推荐一个已知有人在用的教室）。请回原表把时段一格改成「567/678/91011」或具体时间
                </div>
              </div>
            )}

            {/* 数据缺口：没场地的条目扫不了教室冲突，必须说出来 */}
            {(auditData.missing_room_count ?? 0) > 0 && (
              <div className="mb-4 bg-gray-50 border border-gray-200 rounded-xl p-4">
                <div className="text-sm font-medium text-gray-700 mb-1">
                  ⚪ {auditData.missing_room_count} 条记录未填场地，未纳入教室冲突检查
                </div>
                <div className="text-xs text-gray-500">
                  {(auditData.missing_room_samples || []).map((s, i) => (
                    <div key={i}>· {s}</div>
                  ))}
                  {(auditData.missing_room_count ?? 0) > (auditData.missing_room_samples || []).length && (
                    <div className="mt-1">…另有 {(auditData.missing_room_count ?? 0) - (auditData.missing_room_samples || []).length} 条</div>
                  )}
                </div>
                <div className="text-xs text-gray-400 mt-2">这类多为绪论课（安排表里写「教室已安排，请查系统」），请到教务系统核对场地</div>
              </div>
            )}

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
        {!statsData && statsError && (
          <div className="mt-6 pt-5 border-t border-gray-100 text-xs text-amber-600">
            ⚠️ 利用率与工作量统计加载失败：{statsError}
          </div>
        )}
        {statsData && (
          <div className="mt-6 pt-5 border-t border-gray-100">
            <h3 className="text-sm font-medium text-gray-800 mb-3">📈 利用率与工作量统计（{statsData.n_weeks} 周）</h3>

            {/* 关键指标 */}
            <div className="grid grid-cols-3 gap-3 mb-4">
              <div className="bg-teal-50 border border-teal-200 rounded-xl p-3">
                <div className="text-xl font-semibold text-teal-700">{statsData.avg_room_utilization}%</div>
                <div className="text-xs text-teal-700">平均教室利用率</div>
                {/* 分母口径必须一起给：只说 9.4% 而不说"按 448 格/间算"，
                    教秘无法判断这个数字对自己有没有意义（分母曾写死 5天×3时段，
                    把周末的课排除在外 ⇒ 利用率虚高近一倍） */}
                {statsData.capacity_basis && (
                  <div className="text-[10px] text-teal-600/80 mt-0.5">按 {statsData.capacity_basis}</div>
                )}
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

            {/* 教务两类标准课表导出
                在用系统（理论课排课工具）被认可的能力之一就是"排完能导出班级课表、
                场地课表"。此前本系统只能导出新排出来的并行方案，教秘还得自己整理
                一遍才能发到班级群/贴到实验室门口。这里导出的是**当前安排表**。 */}
            <div className="mt-5 pt-4 border-t border-gray-100">
              <div className="text-xs font-medium text-gray-500 mb-2">📤 导出标准课表（基于当前安排表）</div>
              <div className="flex flex-wrap gap-2">
                <button
                  onClick={onExportClass}
                  className="px-3 py-1.5 bg-emerald-600 text-white rounded-lg hover:bg-emerald-700 text-xs font-medium"
                >
                  ⬇️ 班级课表（每班一页）
                </button>
                <button
                  onClick={onExportRoom}
                  className="px-3 py-1.5 bg-emerald-600 text-white rounded-lg hover:bg-emerald-700 text-xs font-medium"
                >
                  ⬇️ 场地课表（每间一页）
                </button>
                <button
                  onClick={onExportMaster}
                  className="px-3 py-1.5 bg-white text-emerald-700 border border-emerald-300 rounded-lg hover:bg-emerald-50 text-xs font-medium"
                >
                  ⬇️ 基础数据台账（4 张实体表）
                </button>
              </div>
              <p className="text-[11px] text-gray-400 mt-1.5">
                班级课表发给班级，场地课表贴在实验室门口；没填场地的记录会进「（未填场地）」页，不会漏掉。
                台账把安排表反推成实验项目/教师/场地/班级四个实体，供核对与补全实验项目用。
              </p>
            </div>
          </div>
        )}
      </div>
    </main>
  );
}
