import { useEffect, useRef, useState } from 'react';
import { rescheduleApi } from '../api/client';
import { describeApiError } from '../utils/apiError';
import type { LandHistory, LandPlanDiff, LandPlanRow, LandStatus } from '../types';

/**
 * 方案落地三步：差异预览（不写入）→ 确认采纳（需管理员）→ 可撤销。
 *
 * 为什么是三步而不是一个「落地」按钮
 * ---------------------------------
 * 这改的是**真实安排表**（433 → 410 不是数字变化，是某些行的时间/教师/场地
 * 被正式改写），看板、班级课表、数据体检、教务系统导入全都以它为准。所以：
 *   ① 预览只读算差异，教秘先看清"会动什么"；
 *   ② 采纳才写入，且采纳时 diff **重新计算**（预览与采纳之间若有审批调课生效，
 *      按旧 diff 写入就是偷换 —— 后端保证，前端只管把结果摆出来）；
 *   ③ 写完可撤销：恢复落地前快照，落地之后又生效的调课会一并回滚并如实计数。
 *
 * 界面口径（与后端 diff 逐字对应，不自己编数字）
 * --------------------------------------------
 * - 新增 / 调整 / 未覆盖保留 / 原样保留 / 非可排原样 五本账齐全；
 * - 未覆盖单元**保留原行**（求解没覆盖就删＝静默丢课），必须单列；
 * - 跨时段长实验（原表同周同天地占多节次）被方案截短时，警告单独高亮 ——
 *   真实表里有 6 个这样的单元，埋在 400 条差异里等于静默删课。
 *
 * R-44 的教训在这个组件上直接适用：落地入口在页面深处（全校方案结果之后），
 * 而"成功/失败"提示若只出现在顶部横幅，用户此刻滚到哪儿根本不知道。
 * 所以差异区自己滚进视口，结果也写在面板里，不只依赖顶部 msg。
 */

interface Props {
  /** 方案行（全校一键结果自带 class_name；单班方案由父级补上） */
  schedule: LandPlanRow[];
  adminAuthed: boolean;
  /** 未登录管理员时引导去审批台登录（与 MasterDataView 同一模式） */
  onRequireLogin: () => void;
  /** 往顶部横幅送一句话（采纳/撤销的结果要让别处的人也看见） */
  onMsg: (m: string) => void;
}

type Busy = '' | 'preview' | 'commit' | 'undo';

/** 一行安排的紧凑描述：第2周 周一 567 A410 · 吴老师 */
function rowText(r: LandPlanRow): string {
  return `第${r.week}周 ${r.weekday || '—'} ${r.time_slot} ${r.room || '（无场地）'}${r.teacher ? ` · ${r.teacher}` : ''}`;
}

/** 401 要说"登录状态"，不能让用户以为是自己操作错了 */
function isAuthError(err: any): boolean {
  return err?.response?.status === 401 || err?.response?.status === 403;
}

/** `2026-10-31T01:30:12.123456` → `10-31 01:30`（历史列表只需要到分钟） */
function fmtTime(iso: string): string {
  const m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/.exec(iso || '');
  return m ? `${m[2]}-${m[3]} ${m[4]}:${m[5]}` : (iso || '');
}

export default function LandPlanPanel({ schedule, adminAuthed, onRequireLogin, onMsg }: Props) {
  const [status, setStatus] = useState<LandStatus | null>(null);
  const [history, setHistory] = useState<LandHistory | null>(null);
  const [historyNote, setHistoryNote] = useState('');
  const [showHistory, setShowHistory] = useState(false);
  const [diff, setDiff] = useState<LandPlanDiff | null>(null);
  const [busy, setBusy] = useState<Busy>('');
  const [error, setError] = useState('');
  const [showAll, setShowAll] = useState(false);
  const panelRef = useRef<HTMLDivElement>(null);

  const loadStatus = async () => {
    try {
      const r = await rescheduleApi.landStatus();
      setStatus(r.data);
    } catch {
      // 状态只是决定"要不要显示撤销入口"，取不到不打扰用户
    }
    // 落地历史（R56）：只读补充信息。未登录管理员 / 旧后端没有这个接口时
    // 一律静默 —— 不能因为"看不到历史"就让整个落地面板报错。
    //
    // R58 修正：**取消静默要分情况**。401/403 是预期内的（教秘本来就不是管理员），
    // 静默是对的；但 404 / 5xx 说明"接口不见了或服务端版本不一致"—— 那是事故，
    // 不能和"你不是管理员"长得一模一样。这次正是因为两者长得一样，
    // 一个端点被别的会话整段删掉，面板安静消失，没人发现。
    try {
      const h = await rescheduleApi.landHistory(8);
      setHistory(h.data);
      setHistoryNote('');
    } catch (err: any) {
      setHistory(null);
      const status = err?.response?.status;
      if (status && status !== 401 && status !== 403) {
        setHistoryNote(
          status === 404
            ? '落地历史不可用：服务端没有这个接口（版本可能不一致）'
            : `落地历史加载失败（HTTP ${status}）`,
        );
      } else {
        setHistoryNote('');
      }
    }
  };

  useEffect(() => { loadStatus(); }, []);

  // 差异区出现在页面深处：直接渲染的话用户刚点完按钮，视线还在按钮上，
  // 410 条差异在下面静静地展开 —— "点了没反应"就是这么来的（R-44）。
  // 可选链调用：jsdom/个别老旧环境没有 scrollIntoView，不能为一次滚动崩掉整个面板。
  useEffect(() => {
    if (diff) panelRef.current?.scrollIntoView?.({ block: 'start' });
  }, [diff]);

  const guard = (): boolean => {
    if (adminAuthed) return true;
    onRequireLogin();
    return false;
  };

  const doPreview = async () => {
    setBusy('preview');
    setError('');
    setShowAll(false);
    try {
      const r = await rescheduleApi.landPreview(schedule);
      setDiff(r.data);
      if (!r.data.has_changes) onMsg('方案与当前安排表完全一致，无需落地');
    } catch (err: any) {
      setError(describeApiError(err, '差异预览失败'));
    } finally {
      setBusy('');
    }
  };

  const doCommit = async () => {
    if (!guard()) return;
    const n = diff ? diff.plan_rows : schedule.length;
    if (!window.confirm(
      `确认把 ${n} 行方案写入正式安排表？\n\n`
      + '这会真实改写排课数据（看板/班级课表/数据体检随后以此为准）。\n'
      + '写入后可用「↩️ 撤销落地」恢复落地前的安排表。')) return;
    setBusy('commit');
    setError('');
    try {
      const r = await rescheduleApi.landCommit(schedule);
      setDiff(r.data);
      await loadStatus();
      onMsg(`✅ 已落地为正式安排表：${r.data.plan_rows} 行写入，安排表 ${r.data.before_entries} → ${r.data.after_entries} 条。`
        + '其他模块重新进入即可看到最新安排；如需回退用「↩️ 撤销落地」。');
    } catch (err: any) {
      if (isAuthError(err)) {
        onRequireLogin();
        setError('登录状态已失效，请重新登录管理员后再采纳');
      } else {
        setError(describeApiError(err, '采纳失败'));
      }
    } finally {
      setBusy('');
    }
  };

  const doUndo = async () => {
    if (!guard()) return;
    if (!window.confirm(
      '确认撤销上一次落地？\n\n'
      + '安排表将恢复到落地前的状态；落地之后又生效的调课也会被一并回滚'
      + '（具体条数撤销后会告知，那些申请需要重新审批）。')) return;
    setBusy('undo');
    setError('');
    try {
      const r = await rescheduleApi.landUndo();
      setDiff(null);
      await loadStatus();
      const extra = r.data.rolled_back_moves > 0
        ? `；一并回滚了落地后生效的 ${r.data.rolled_back_moves} 条调课（需重新审批）`
        : '';
      onMsg(`↩️ 已撤销落地，安排表恢复到 ${r.data.restored_entries} 条${extra}`);
    } catch (err: any) {
      if (isAuthError(err)) {
        onRequireLogin();
        setError('登录状态已失效，请重新登录管理员后再撤销');
      } else {
        setError(describeApiError(err, '撤销失败'));
      }
    } finally {
      setBusy('');
    }
  };

  if (!schedule.length) return null;

  const d = diff;
  const changedShown = d && (showAll ? d.changed : d.changed.slice(0, 15));
  const addedShown = d && (showAll ? d.added : d.added.slice(0, 15));
  const uncoveredShown = d && (showAll ? d.uncovered : d.uncovered.slice(0, 15));

  return (
    <div ref={panelRef} className="mt-4 border-t border-indigo-100 pt-4">
      {/* 入口 + 撤销入口 */}
      <div className="flex items-center gap-2 flex-wrap">
        <button
          onClick={doPreview}
          disabled={busy !== ''}
          className="px-3 py-1.5 bg-indigo-600 text-white rounded-lg hover:bg-indigo-700 disabled:opacity-50 text-xs font-medium"
          title="先看差异（不写入），确认后再写入正式安排表"
        >
          {busy === 'preview' ? '⏳ 算差异中...' : '📥 落地为正式安排表'}
        </button>
        {status?.landed && (
          <button
            onClick={doUndo}
            disabled={busy !== ''}
            className="px-3 py-1.5 bg-white border border-gray-300 text-gray-700 rounded-lg hover:bg-gray-50 disabled:opacity-50 text-xs font-medium"
            title="恢复落地前的安排表（落地后生效的调课会一并回滚并告知）"
          >
            {busy === 'undo' ? '⏳ 撤销中...' : '↩️ 撤销落地'}
          </button>
        )}
        <span className="text-xs text-gray-400">
          写回真实安排表：先预览差异 → 管理员确认 → 可随时撤销
        </span>
      </div>
      {status?.landed && !d && (
        <p className="mt-1.5 text-xs text-emerald-600">
          当前安排表已是落地方案（{status.landed_at}，{status.plan_rows} 行
          {status.landed_by ? `，由 ${status.landed_by} 落地` : ''}
          {status.note ? `，备注：${status.note}` : ''}）
        </p>
      )}

      {/* 落地历史（R56）：撤销不再删记录之后，"谁落过、后来撤没撤"才答得上来。
          管理员以外看不到（接口要管理员），401/403 就整块不渲染 —— 不打扰教秘。
          但 404/5xx 必须说出来（R58）：那说明接口没了或版本不一致，是事故不是权限。 */}
      {historyNote && (
        <p className="mt-2 text-xs text-amber-600">{historyNote}</p>
      )}
      {history && history.total > 0 && (
        <div className="mt-2">
          <button
            onClick={() => setShowHistory((v) => !v)}
            className="text-xs text-gray-500 hover:underline"
            title="每次落地与被撤销的记录都会留着，便于事后核对"
          >
            📜 落地历史 {history.total} 次
            {history.revoked > 0 ? `（其中 ${history.revoked} 次已撤销）` : ''}
            {showHistory ? ' ▴' : ' ▾'}
          </button>
          {showHistory && (
            <ul className="mt-1.5 space-y-1" data-testid="land-history">
              {history.history.map((h) => (
                <li key={h.id} className="text-xs text-gray-600 flex items-center gap-2 flex-wrap">
                  <span className="text-gray-400">{fmtTime(h.landed_at)}</span>
                  <span>{h.actor || '（未记录操作者）'}</span>
                  {/* R63：null = 记录损坏，不能显示成「0 行」（那会被读成"没改动"） */}
                  <span className={h.plan_rows === null ? 'text-red-600' : ''}>
                    {h.plan_rows === null ? '⚠️ 行数无法解析' : `${h.plan_rows} 行`}
                  </span>
                  {h.note && <span className="text-gray-400">备注：{h.note}</span>}
                  {h.revoked ? (
                    <span className="text-amber-600">
                      已撤销{fmtTime(h.revoked_at) ? `（${fmtTime(h.revoked_at)}${h.revoked_by ? ` · ${h.revoked_by}` : ''}）` : ''}
                    </span>
                  ) : (
                    <span className="text-emerald-600">生效中</span>
                  )}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}

      {error && (
        <div className="mt-3 px-3 py-2 bg-red-50 border border-red-200 text-red-700 text-sm rounded-lg">{error}</div>
      )}

      {/* 差异预览 / 采纳结果 */}
      {d && (
        <div className="mt-3 rounded-lg border border-indigo-200 bg-indigo-50/40 p-4">
          <div className="flex items-baseline justify-between gap-2 flex-wrap">
            <h4 className="text-sm font-semibold text-gray-900">
              {d.landed_at ? '✅ 落地结果' : '🔍 落地预览（未写入）'}
            </h4>
            <span className="text-xs text-gray-500">
              方案 {d.plan_units} 个单元 / {d.plan_rows} 行 ｜ 安排表 {d.before_entries} → {d.after_entries} 条
            </span>
          </div>

          {/* 五本账：新增/调整/未覆盖/原样/非可排，一个都不能少 */}
          <div className="mt-2 flex gap-2 flex-wrap text-xs">
            <span className="px-2 py-1 rounded bg-white border border-gray-200 text-gray-700">新增 {d.added.length}</span>
            <span className="px-2 py-1 rounded bg-white border border-gray-200 text-gray-700">调整 {d.changed.length}</span>
            <span className={`px-2 py-1 rounded border ${d.uncovered.length ? 'bg-amber-50 border-amber-300 text-amber-700' : 'bg-white border-gray-200 text-gray-700'}`}>
              未覆盖保留 {d.uncovered.length}（{d.uncovered.reduce((s, u) => s + u.rows.length, 0)} 行）
            </span>
            <span className="px-2 py-1 rounded bg-white border border-gray-200 text-gray-700">原样保留 {d.unchanged_units}</span>
            <span className="px-2 py-1 rounded bg-white border border-gray-200 text-gray-700">绪论/考试原样 {d.untouched_nonsched}</span>
          </div>

          {/* 警告：未覆盖 = 可能静默丢课；长实验截短 = 会丢半节课 */}
          {d.warnings.length > 0 && (
            <div className="mt-3 rounded-lg bg-amber-50 border border-amber-200 px-3 py-2 space-y-1">
              {d.warnings.map((w, i) => (
                <div key={i} className="text-xs text-amber-800">⚠️ {w}</div>
              ))}
            </div>
          )}
          {d.long_sessions.length > 0 && (
            <div className="mt-2 rounded-lg bg-amber-50 border border-amber-200 px-3 py-2">
              <div className="text-xs font-medium text-amber-800 mb-1">
                将被截短的长实验（{d.long_sessions.length} 个）：原表占多节次，方案每单元只排一个时段
              </div>
              <ul className="text-xs text-amber-700 list-disc list-inside">
                {d.long_sessions.slice(0, 8).map((ls) => (
                  <li key={ls.unit}>{ls.unit}：{ls.old_slots.join('+')} → {ls.plan_slots.join('+')}</li>
                ))}
                {d.long_sessions.length > 8 && <li>…共 {d.long_sessions.length} 个</li>}
              </ul>
            </div>
          )}

          {/* 明细：默认只看前 15 条，410 条全铺开等于没给人看 */}
          {(d.changed.length > 0 || d.added.length > 0 || d.uncovered.length > 0) && (
            <div className="mt-3 max-h-64 overflow-y-auto rounded bg-white border border-gray-100 px-3 py-2 text-xs space-y-2">
              {addedShown && addedShown.length > 0 && (
                <div>
                  <div className="font-medium text-emerald-700">新增（{d.added.length}）</div>
                  {addedShown.map((a) => (
                    <div key={a.unit} className="text-gray-600">· {a.unit} → {a.rows.map(rowText).join('；')}</div>
                  ))}
                </div>
              )}
              {changedShown && changedShown.length > 0 && (
                <div>
                  <div className="font-medium text-cyan-700">调整（{d.changed.length}）</div>
                  {changedShown.map((c) => (
                    <div key={c.unit} className="text-gray-600">
                      · {c.unit}
                      {c.long_session && <span className="text-amber-600">（长实验截短）</span>}
                      ：{c.old.map(rowText).join('；')} → {c.new.map(rowText).join('；')}
                    </div>
                  ))}
                </div>
              )}
              {uncoveredShown && uncoveredShown.length > 0 && (
                <div>
                  <div className="font-medium text-amber-700">未覆盖、保留原安排（{d.uncovered.length}）</div>
                  {uncoveredShown.map((u) => (
                    <div key={u.unit} className="text-gray-600">· {u.unit}：{u.rows.map(rowText).join('；')}</div>
                  ))}
                </div>
              )}
              {(d.changed.length > 15 || d.added.length > 15 || d.uncovered.length > 15) && (
                <button onClick={() => setShowAll(!showAll)}
                  className="text-cyan-600 hover:underline text-xs">
                  {showAll ? '收起，只看前 15 条' : `展开全部（${d.changed.length + d.added.length + d.uncovered.length} 条）`}
                </button>
              )}
            </div>
          )}

          {/* 操作区 */}
          <div className="mt-3 flex items-center gap-2 flex-wrap">
            {d.landed_at ? (
              <span className="text-xs text-emerald-700">
                已于 {d.landed_at} 写入，可用上方「↩️ 撤销落地」回退
              </span>
            ) : d.has_changes ? (
              <>
                <button
                  onClick={doCommit}
                  disabled={busy !== ''}
                  className="px-4 py-1.5 bg-emerald-600 text-white rounded-lg hover:bg-emerald-700 disabled:opacity-50 text-xs font-medium"
                >
                  {busy === 'commit' ? '⏳ 写入中...' : `✅ 确认采纳（写入 ${d.plan_rows} 行）`}
                </button>
                <button
                  onClick={() => setDiff(null)}
                  disabled={busy !== ''}
                  className="px-3 py-1.5 bg-white border border-gray-300 text-gray-600 rounded-lg hover:bg-gray-50 disabled:opacity-50 text-xs"
                >
                  取消
                </button>
                {!adminAuthed && (
                  <span className="text-xs text-amber-600">采纳需管理员登录（预览无需登录）</span>
                )}
              </>
            ) : (
              <span className="text-xs text-gray-500">方案与当前安排表一致，无需写入。</span>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
