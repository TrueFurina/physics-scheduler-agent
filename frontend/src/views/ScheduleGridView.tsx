import { useEffect, useRef, useState } from 'react';
import type { GridCellEntry, ScheduleGrid } from '../types';

interface Props {
  week: number;
  data: ScheduleGrid | null;
  loading: boolean;
  error: string;
  classFilter: string;
  roomFilter: string;
  onWeekChange: (w: number) => void;
  onClassChange: (c: string) => void;
  onRoomChange: (r: string) => void;
  onRetry: () => void;
}

/** 冲突类型 → 人话（与 conflict_model 的三类核心冲突同口径）。 */
const CONFLICT_LABEL: Record<string, string> = {
  room: '教室冲突',
  teacher: '教师冲突',
  group: '整组冲突',
};

const conflictText = (kinds: string[]) =>
  kinds.map((k) => CONFLICT_LABEL[k] || k).join(' + ');

/** 📅 周课表：某一周的 星期×时段 排课全景（R-62，README 路线图头号未做项）。
 *
 *  与导出的分工：导出给教务系统/打印，这个给**当场看**——落地（R-61）之后，
 *  教秘要确认"方案真的写进安排表了"，看板只有统计数字，网格才是眼可见的那一面。
 *
 *  口径（后端 schedule_grid.py 是权威，这里只负责渲染）：
 *  - 7 天 × 4 时段全列：周末照常排课（真实表 71/433），上午时段真实存在；
 *  - 跨节次记录在每个占用的节次各出现一次，带「14:00-17:00」原表写法徽标（R-40）；
 *  - 没填场地的记录照常显示，标「未填场地」（R-35：不静默丢）；
 *  - 冲突徽标来自 scan_table（同班不同组同格是正常分批，不标——R-56）。
 */
export default function ScheduleGridView({
  week, data, loading, error, classFilter, roomFilter,
  onWeekChange, onClassChange, onRoomChange, onRetry,
}: Props) {
  const [selected, setSelected] = useState<{ entry: GridCellEntry; where: string } | null>(null);
  // 切周次/筛选后网格内容整体变化：滚回顾想区，避免用户停在页面中下部
  // 对着旧周的内容发呆（R-44 同族：结果出现在别处＝"点了没反应"）。
  const topRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (data) topRef.current?.scrollIntoView?.({ block: 'start' });
  }, [data, week, classFilter, roomFilter]);

  const weeks: number[] = [];
  if (data) {
    for (let w = data.week_min; w <= data.week_max; w++) weeks.push(w);
  }

  const cellEntries = (weekday: string, slot: string): GridCellEntry[] =>
    data?.cells?.[`${weekday}|${slot}`] || [];

  return (
    <main className="max-w-6xl mx-auto px-4 pb-10">
      <div ref={topRef} className="bg-white rounded-xl border border-gray-200 p-5 mb-6">
        <h2 className="text-lg font-semibold text-gray-900 mb-1">📅 周课表</h2>
        <p className="text-sm text-gray-500 mb-4">
          某一周的排课全景：星期 × 时段。与导出的分工——导出给教务系统/打印，这里给当场看：
          落地新方案后，这一面就是「安排表真的换了」的确认。
        </p>

        {/* 控件：周次 + 班级/场地筛选（选项来自全表，不受当前筛选影响） */}
        <div className="flex flex-wrap items-center gap-3 mb-4 text-sm">
          <label className="flex items-center gap-1.5 text-gray-600">
            周次
            <select
              value={week}
              onChange={(e) => onWeekChange(Number(e.target.value))}
              className="border border-gray-300 rounded-lg px-2 py-1.5 text-gray-800 bg-white"
            >
              {(weeks.length ? weeks : [week]).map((w) => (
                <option key={w} value={w}>第 {w} 周</option>
              ))}
            </select>
          </label>
          <label className="flex items-center gap-1.5 text-gray-600">
            班级
            <select
              value={classFilter}
              onChange={(e) => onClassChange(e.target.value)}
              className="border border-gray-300 rounded-lg px-2 py-1.5 text-gray-800 bg-white max-w-56"
            >
              <option value="">全部</option>
              {(data?.class_options || []).map((c) => (
                <option key={c} value={c}>{c}</option>
              ))}
            </select>
          </label>
          <label className="flex items-center gap-1.5 text-gray-600">
            场地
            <select
              value={roomFilter}
              onChange={(e) => onRoomChange(e.target.value)}
              className="border border-gray-300 rounded-lg px-2 py-1.5 text-gray-800 bg-white"
            >
              <option value="">全部</option>
              {(data?.room_options || []).map((r) => (
                <option key={r} value={r}>{r}</option>
              ))}
            </select>
          </label>
          <button
            onClick={onRetry}
            className="px-3 py-1.5 bg-gray-100 text-gray-600 rounded-lg hover:bg-gray-200 text-xs font-medium"
          >
            🔄 刷新
          </button>
        </div>

        {loading && <div className="text-sm text-gray-400 py-6">网格生成中...</div>}

        {/* 失败态：明确说失败，不清空旧数据蒙混（R-29） */}
        {!loading && error && (
          <div className="rounded-xl p-4 border bg-red-50 border-red-200 mb-4">
            <div className="text-sm font-medium text-red-700">⚠️ 网格没能加载，下面显示的不是本周数据</div>
            <div className="text-xs text-red-500 mt-1 break-words">{error}</div>
            <button
              onClick={onRetry}
              className="mt-3 px-3 py-1.5 bg-red-600 text-white rounded-lg hover:bg-red-700 text-xs font-medium"
            >
              重新加载
            </button>
          </div>
        )}

        {data && (
          <>
            {/* 汇总条：条数 + 冲突（说清是全表口径）+ 数据缺口 */}
            <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-gray-600 mb-3">
              <span>第 {data.week} 周 · <b className="text-gray-800">{data.totals.entries}</b> 条安排</span>
              <span>{data.totals.nonempty_cells} 个非空格</span>
              <span className={data.totals.conflict_entries > 0 ? 'text-red-600 font-medium' : ''}>
                ⚠ 冲突条目 {data.totals.conflict_entries}
                （教室 {data.conflict_counts.room} / 教师 {data.conflict_counts.teacher} / 整组 {data.conflict_counts.group}，{data.conflict_scope}）
              </span>
              {data.totals.missing_room > 0 && (
                <span className="text-amber-600">{data.totals.missing_room} 条未填场地</span>
              )}
              {data.totals.unplaced > 0 && (
                <span className="text-amber-600">{data.totals.unplaced} 条时段无法识别未入网格</span>
              )}
            </div>

            {/* 口径说明：不静默处理的每一类都必须说出口 */}
            {data.notes.length > 0 && (
              <div className="bg-amber-50 border border-amber-200 rounded-lg p-3 mb-4 space-y-1">
                {data.notes.map((n, i) => (
                  <div key={i} className="text-xs text-amber-800">⚠️ {n}</div>
                ))}
              </div>
            )}

            {/* 网格本体：行＝时段，列＝星期（7 天全列，周末不丢） */}
            <div className="overflow-x-auto border border-gray-200 rounded-lg">
              <table className="w-full text-xs border-collapse min-w-[880px]">
                <thead>
                  <tr className="bg-gray-50">
                    <th className="border border-gray-200 px-2 py-1.5 text-left text-gray-500 font-medium w-28">时段</th>
                    {data.weekdays.map((wd) => (
                      <th key={wd} className="border border-gray-200 px-2 py-1.5 text-left text-gray-700 font-medium">
                        {wd}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {data.slots.map((s) => (
                    <tr key={s.slot} className="align-top">
                      <td className="border border-gray-200 px-2 py-1.5 text-gray-500 bg-gray-50">
                        <div className="font-medium text-gray-700">{s.label}</div>
                        <div className="text-[10px] text-gray-400">{s.clock}</div>
                      </td>
                      {data.weekdays.map((wd) => {
                        const items = cellEntries(wd, s.slot);
                        return (
                          <td key={wd} className="border border-gray-200 px-1.5 py-1.5 align-top">
                            {items.length === 0 && (
                              <div className="text-gray-300 text-center py-2">—</div>
                            )}
                            {items.map((e, i) => {
                              const conflicted = e.conflicts.length > 0;
                              const where = `第 ${data.week} 周 ${wd} ${s.label}`;
                              return (
                                <button
                                  key={`${e.class_name}-${e.group}-${e.experiment}-${i}`}
                                  type="button"
                                  onClick={() => setSelected({ entry: e, where })}
                                  className={`w-full text-left rounded-lg px-2 py-1.5 mb-1 border transition-colors ${
                                    conflicted
                                      ? 'bg-red-50 border-red-300 hover:bg-red-100'
                                      : 'bg-white border-gray-200 hover:bg-gray-50'
                                  }`}
                                >
                                  <div className="flex items-start gap-1">
                                    {conflicted && (
                                      <span className="text-red-500 shrink-0" title={conflictText(e.conflicts)}>⚠️</span>
                                    )}
                                    <div className="font-medium text-gray-800 leading-tight line-clamp-2">
                                      {e.experiment}
                                    </div>
                                  </div>
                                  <div className="text-[10px] text-gray-500 mt-0.5">
                                    {e.class_name}/{e.group}
                                  </div>
                                  <div className="text-[10px] text-gray-500">
                                    {e.teacher || '未指派教师'}
                                  </div>
                                  <div className={`text-[10px] ${e.missing_room ? 'text-gray-400 italic' : 'text-gray-600'}`}>
                                    {e.missing_room ? '未填场地' : e.room}
                                  </div>
                                  {e.blocked.length > 1 && (
                                    <div className="text-[10px] text-cyan-600 mt-0.5" title="跨时段长实验：同一格在多个时段重复出现">
                                      ⏤ {e.time_raw || e.blocked.join('+')}
                                    </div>
                                  )}
                                </button>
                              );
                            })}
                          </td>
                        );
                      })}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            {/* 选中条目详情（点格子看全量字段） */}
            {selected && (
              <div className="mt-4 bg-gray-50 border border-gray-200 rounded-xl p-4 text-xs text-gray-700 space-y-1">
                <div className="flex items-center justify-between">
                  <div className="font-medium text-sm text-gray-900">
                    {selected.entry.experiment}
                    <span className="text-gray-400 font-normal ml-2">{selected.where}</span>
                  </div>
                  <button
                    onClick={() => setSelected(null)}
                    className="text-gray-400 hover:text-gray-600"
                    aria-label="关闭详情"
                  >
                    ✕
                  </button>
                </div>
                <div>班级/组：{selected.entry.class_name}/{selected.entry.group}</div>
                <div>教师：{selected.entry.teacher || '未指派教师'}</div>
                <div>场地：{selected.entry.missing_room ? '未填场地（数据缺口，不参与教室冲突判定）' : selected.entry.room}</div>
                <div>
                  时段：{selected.entry.time_raw || selected.entry.time_slot}
                  {selected.entry.blocked.length > 1 && `（占 ${selected.entry.blocked.join(' + ')}，跨时段）`}
                </div>
                {selected.entry.conflicts.length > 0 && (
                  <div className="text-red-600">
                    ⚠️ {conflictText(selected.entry.conflicts)}：与本周其它安排撞在同一格，处理建议见「📊 数据看板」
                  </div>
                )}
              </div>
            )}
          </>
        )}
      </div>
    </main>
  );
}
