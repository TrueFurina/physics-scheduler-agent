import { useEffect, useRef, useState } from 'react';
import { rescheduleApi } from '../api/client';
import { describeApiError } from '../utils/apiError';
import type { ImportTasksPreview, ImportWorkbookPreview } from '../types';

/**
 * 数据导入向导（R-63）：选类型 → 映射预览（只读）→ 确认导入。
 *
 * 为什么是三步而不是一个「导入」按钮
 * ---------------------------------
 * 外校/外系的 Excel 列名千奇百怪（「项目名称」「任课教师」「实验室」「教学班」…
 * 都可能出现在同一份文件里）。此前的导入是**直写**：文件一传，实体就进库，
 * 警告只在事后出现 —— 教秘无法在写入前回答"这份文件会被解析成什么"。
 * 向导把「解析」变成可见的一步：
 *   ① 选导入类型 + 选文件；
 *   ② 预览：服务端只读解析，逐表展示「字段 ← 列名」映射、没认出的列、
 *      被判为统计列而忽略的列、样本行、将导入的行数（**不写任何状态**）；
 *   ③ 确认才写入，且导入**只增补不删除**（Excel 里的"空"可能是"没填"，
 *      后端不猜删除意图 —— 删实体用列表里的删除按钮）。
 *
 * 口径铁律：前端**不自己解析 Excel、不自己映射列名**。界面上每个映射、
 * 每个行数都来自服务端解析报告（预览与写入共用同一份后端代码，
 * "预览说 12 行、导入进 9 行"不可能发生）。
 */

export type ImportKind = 'workbook' | 'tasks';

interface Props {
  /** 初始类型：主数据页「导入台账」= workbook，「任务清单」= tasks */
  kind: ImportKind;
  adminAuthed: boolean;
  /** 未登录管理员时引导去审批台登录（与 MasterDataView 同一模式） */
  onRequireLogin: () => void;
  /** 导入成功后让主数据页重新拉取 */
  onImported: () => void;
  onClose: () => void;
}

type Step = 'choose' | 'preview' | 'done';

const KIND_META: Record<ImportKind, { icon: string; title: string; hint: string }> = {
  workbook: {
    icon: '🗂️', title: '主数据台账',
    hint: '课程 / 实验项目 / 教师 / 场地 / 班级 五张实体表。本系统导出的台账改完传回，或别校同格式清单。只增补，不删除。',
  },
  tasks: {
    icon: '📋', title: '教学任务清单',
    hint: '「班级 × 实验项目」清单：没有周次/时段的行直接进并行排课（从零排课的输入）。整份替换此前的清单。',
  },
};

const TABLE_LABELS: Record<string, string> = {
  courses: '课程', experiments: '实验项目', teachers: '教师', rooms: '场地', classes: '班级',
};
const TABLE_ORDER = ['courses', 'experiments', 'teachers', 'rooms', 'classes'];
const FIELD_LABELS: Record<string, string> = {
  name: '名称', course: '所属课程', teachers: '授课教师', rooms: '使用场地',
  classes: '适用班级', projects: '可授项目', capacity: '容量', groups: '学生组',
  weekday: '上课星期', weekdays: '全部上课日', time_slots: '节次',
  week_min: '起始周', week_max: '结束周', experiments: '实验项目',
};
const TASK_FIELD_LABELS: Record<string, string> = {
  class_name: '班级', experiment: '实验项目', group: '学生组', teacher: '授课教师',
  room: '场地', course: '课程', week: '周次', weekday: '星期',
};

/** 样本行 → 一行可读文本（多值用顿号连接，空值显式标出，不静默吞掉）。 */
function sampleText(rec: Record<string, unknown>, labels: Record<string, string>): string {
  return Object.entries(rec)
    .map(([k, v]) => {
      const val = Array.isArray(v)
        ? (v.length ? v.join('、') : '（空）')
        : (v === null || v === '' || v === undefined ? '（空）' : String(v));
      return `${labels[k] || k}：${val}`;
    })
    .join('　');
}

/** 401/403 要说"登录状态"，不能让用户以为是自己操作错了 */
function isAuthError(err: any): boolean {
  return err?.response?.status === 401 || err?.response?.status === 403;
}

export default function ImportWizard({ kind: initKind, adminAuthed, onRequireLogin, onImported, onClose }: Props) {
  const [step, setStep] = useState<Step>('choose');
  const [kind, setKind] = useState<ImportKind>(initKind);
  const [file, setFile] = useState<File | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [wbPreview, setWbPreview] = useState<ImportWorkbookPreview | null>(null);
  const [taskPreview, setTaskPreview] = useState<ImportTasksPreview | null>(null);
  const [result, setResult] = useState<{ message: string; warnings: string[] } | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const resultRef = useRef<HTMLDivElement>(null);

  // 导入结果出现在向导底部（映射预览可能很长）—— 滚进去，否则"导完了"
  // 用户看见的还是第 2 步（R-44：提示必须出现在用户此刻看的地方）。
  useEffect(() => {
    if (step === 'done') resultRef.current?.scrollIntoView?.({ block: 'start' });
  }, [step]);

  const doParse = async () => {
    if (!file) return;
    setBusy(true);
    setError('');
    try {
      if (kind === 'workbook') {
        const r = await rescheduleApi.masterImportPreview(file);
        setWbPreview(r.data.data);
        setTaskPreview(null);
      } else {
        const r = await rescheduleApi.masterTasksPreview(file);
        setTaskPreview(r.data.data);
        setWbPreview(null);
      }
      setStep('preview');
    } catch (err: any) {
      // 解析失败（含认不出必需列）停在第 1 步：文件得先能解析，才谈得上映射
      setError(describeApiError(err));
    } finally {
      setBusy(false);
    }
  };

  const doCommit = async () => {
    if (!adminAuthed) { onRequireLogin(); return; }
    if (!file) return;
    setBusy(true);
    setError('');
    try {
      const r = kind === 'workbook'
        ? await rescheduleApi.masterImport(file)
        : await rescheduleApi.uploadMaster(file);
      setResult({ message: r.data.message, warnings: r.data.report?.warnings || [] });
      setStep('done');
      onImported();
    } catch (err: any) {
      // 写入失败停在预览步：映射还在，用户可以重试或换文件
      setError(isAuthError(err) ? '登录状态已失效，请重新登录管理员后再确认导入' : describeApiError(err));
      if (isAuthError(err)) onRequireLogin();
    } finally {
      setBusy(false);
    }
  };

  const reset = () => {
    setStep('choose');
    setFile(null);
    setWbPreview(null);
    setTaskPreview(null);
    setResult(null);
    setError('');
    if (fileRef.current) fileRef.current.value = '';   // 允许连续选同一个文件
  };

  const totalRows = wbPreview
    ? Object.values(wbPreview.tables).reduce((s, t) => s + (t.rows || 0), 0)
    : 0;
  const taskRows = taskPreview?.tasks || 0;
  const nothingToImport = step === 'preview'
    && (kind === 'workbook' ? totalRows === 0 : taskRows === 0);

  const stepChip = (n: number, label: string, active: boolean) => (
    <span className={`px-2 py-1 rounded-full text-xs ${active ? 'bg-teal-100 text-teal-700 font-medium' : 'text-gray-400'}`}>
      {n} {label}
    </span>
  );

  return (
    <div className="fixed inset-0 bg-black/40 z-50 flex items-center justify-center p-4">
      <div className="bg-white rounded-2xl shadow-xl w-full max-w-3xl max-h-[90vh] overflow-y-auto p-6">
        {/* 头部：标题 + 步骤指示 + 关闭 */}
        <div className="flex items-start justify-between mb-4">
          <div>
            <h3 className="text-lg font-semibold text-gray-900">📥 数据导入向导</h3>
            <div className="flex items-center gap-1 mt-1">
              {stepChip(1, '选择文件', step === 'choose')}
              <span className="text-gray-300">›</span>
              {stepChip(2, '映射预览（只读）', step === 'preview')}
              <span className="text-gray-300">›</span>
              {stepChip(3, '确认导入', step === 'done')}
            </div>
          </div>
          <button onClick={onClose} aria-label="关闭向导"
            className="shrink-0 text-gray-400 hover:text-gray-600 leading-none text-lg">✕</button>
        </div>

        {error && (
          <div className="mb-3 px-3 py-2 bg-red-50 border border-red-200 text-red-700 text-sm rounded-lg break-words">
            {error}
          </div>
        )}

        {/* ── 第 1 步：选类型 + 选文件 ── */}
        {step === 'choose' && (
          <div className="space-y-4">
            <div className="grid grid-cols-2 gap-3">
              {(Object.keys(KIND_META) as ImportKind[]).map((k) => (
                <button
                  key={k}
                  onClick={() => { setKind(k); setError(''); }}
                  className={`text-left p-3 rounded-xl border transition-colors ${
                    kind === k ? 'border-teal-400 bg-teal-50' : 'border-gray-200 hover:bg-gray-50'
                  }`}
                >
                  <div className="text-sm font-medium text-gray-800">
                    {KIND_META[k].icon} {KIND_META[k].title}
                  </div>
                  <div className="text-xs text-gray-500 mt-1 leading-relaxed">{KIND_META[k].hint}</div>
                </button>
              ))}
            </div>

            <div>
              <label className="block text-sm text-gray-600 mb-1">
                选择 Excel 文件（.xlsx / .xls）
              </label>
              <input
                ref={fileRef}
                type="file"
                accept=".xlsx,.xls"
                onChange={(e) => { setFile(e.target.files?.[0] || null); setError(''); }}
                className="block w-full text-sm text-gray-600 file:mr-3 file:py-1.5 file:px-3
                           file:rounded-lg file:border-0 file:bg-gray-100 file:text-gray-700
                           hover:file:bg-gray-200"
              />
              {file && (
                <div className="text-xs text-gray-500 mt-1">已选择：{file.name}</div>
              )}
            </div>

            <div className="flex items-center gap-2">
              <button
                onClick={doParse}
                disabled={!file || busy}
                className="px-4 py-2 bg-teal-600 text-white rounded-lg hover:bg-teal-700 text-sm font-medium disabled:opacity-40"
              >
                {busy ? '解析中…' : '解析预览'}
              </button>
              <span className="text-xs text-gray-400">预览只读，不会写入任何数据</span>
            </div>
          </div>
        )}

        {/* ── 第 2 步：映射预览 ── */}
        {step === 'preview' && (
          <div className="space-y-4">
            <div className="text-sm text-gray-600">
              文件：<span className="font-medium text-gray-800">{file?.name}</span>
              {wbPreview && (
                <span className="ml-2 text-xs text-gray-400">
                  （{wbPreview.sheets.length} 个工作表：{wbPreview.sheets.join('、')}）
                </span>
              )}
              {taskPreview && (
                <span className="ml-2 text-xs text-gray-400">（工作表：{taskPreview.sheet}）</span>
              )}
            </div>

            {wbPreview && (
              <>
                <div className={`text-xs px-2.5 py-1.5 rounded-lg inline-block ${
                  wbPreview.tagged ? 'bg-emerald-50 text-emerald-700' : 'bg-amber-50 text-amber-700'
                }`}>
                  {wbPreview.tagged
                    ? '✅ 本系统导出的台账（带可导回标记）'
                    : '⚠️ 外校格式文件：按列名别名映射，没认出的列不会导入'}
                </div>
                <div className="text-sm text-gray-700">
                  将导入 <span className="font-semibold text-teal-700">{totalRows}</span> 行
                  （只增补，不删除已有实体）
                </div>
                {TABLE_ORDER.filter((k) => wbPreview.tables[k]).map((k) => {
                  const t = wbPreview.tables[k];
                  return (
                    <div key={k} className="border border-gray-200 rounded-xl p-3">
                      <div className="flex items-center gap-2 mb-2 flex-wrap">
                        <span className="text-sm font-medium text-gray-800">{TABLE_LABELS[k]}</span>
                        {t.skipped ? (
                          <span className="text-xs px-1.5 py-0.5 bg-amber-100 text-amber-700 rounded">⚠️ 该页已跳过</span>
                        ) : !t.found ? (
                          <span className="text-xs px-1.5 py-0.5 bg-gray-100 text-gray-500 rounded">未找到该页</span>
                        ) : t.rows > 0 ? (
                          <span className="text-xs px-1.5 py-0.5 bg-emerald-100 text-emerald-700 rounded">✅ {t.rows} 行</span>
                        ) : (
                          <span className="text-xs px-1.5 py-0.5 bg-gray-100 text-gray-500 rounded">该页没有数据行</span>
                        )}
                        {t.header_row ? <span className="text-xs text-gray-400">表头第 {t.header_row} 行</span> : null}
                      </div>
                      {t.columns && Object.keys(t.columns).length > 0 && (
                        <div className="text-xs text-gray-600 mb-1.5">
                          {Object.entries(t.columns).map(([f, col]) => (
                            <span key={f} className="inline-block mr-3 mb-1">
                              <span className="text-gray-400">{FIELD_LABELS[f] || f}</span>
                              {' ← '}
                              <span className="font-medium text-gray-700">{col}</span>
                            </span>
                          ))}
                        </div>
                      )}
                      {t.unrecognized && t.unrecognized.length > 0 && (
                        <div className="text-xs text-amber-700 mb-1">
                          未识别的列（不会导入）：{t.unrecognized.join('、')}
                        </div>
                      )}
                      {t.stat_dropped && t.stat_dropped.length > 0 && (
                        <div className="text-xs text-amber-700 mb-1">
                          统计列已忽略（不当实体名）：{t.stat_dropped.join('、')}
                        </div>
                      )}
                      {t.missing && t.missing.length > 0 && (
                        <div className="text-xs text-gray-400 mb-1">
                          未提供的字段（留空＝不修改）：{t.missing.map((f) => FIELD_LABELS[f] || f).join('、')}
                        </div>
                      )}
                      {t.samples && t.samples.length > 0 && (
                        <div className="mt-1.5 space-y-0.5">
                          {t.samples.map((s, i) => (
                            <div key={i} className="text-xs text-gray-500 bg-gray-50 rounded px-2 py-1 break-all">
                              {sampleText(s, FIELD_LABELS)}
                            </div>
                          ))}
                        </div>
                      )}
                    </div>
                  );
                })}
              </>
            )}

            {taskPreview && (
              <div className="border border-gray-200 rounded-xl p-3">
                <div className="flex items-center gap-2 mb-2 flex-wrap">
                  <span className="text-sm font-medium text-gray-800">清单解析</span>
                  {taskPreview.tasks > 0 ? (
                    <span className="text-xs px-1.5 py-0.5 bg-emerald-100 text-emerald-700 rounded">
                      ✅ {taskPreview.tasks} 条待排任务
                    </span>
                  ) : (
                    <span className="text-xs px-1.5 py-0.5 bg-gray-100 text-gray-500 rounded">没有可导入的任务</span>
                  )}
                  <span className="text-xs text-gray-400">表头第 {taskPreview.header_row} 行</span>
                </div>
                <div className="text-xs text-gray-600 mb-1.5">
                  {Object.entries(taskPreview.columns).map(([f, col]) => (
                    <span key={f} className="inline-block mr-3 mb-1">
                      <span className="text-gray-400">{TASK_FIELD_LABELS[f] || f}</span>
                      {' ← '}
                      <span className="font-medium text-gray-700">{col}</span>
                    </span>
                  ))}
                </div>
                {taskPreview.unrecognized_columns.length > 0 && (
                  <div className="text-xs text-amber-700 mb-1">
                    未识别的列（不会导入）：{taskPreview.unrecognized_columns.join('、')}
                  </div>
                )}
                {taskPreview.blank_or_incomplete_rows > 0 && (
                  <div className="text-xs text-amber-700 mb-1">
                    {taskPreview.blank_or_incomplete_rows} 行班级或实验项目为空，已跳过
                  </div>
                )}
                {taskPreview.samples.length > 0 && (
                  <div className="mt-1.5 space-y-0.5">
                    {taskPreview.samples.map((s, i) => (
                      <div key={i} className="text-xs text-gray-500 bg-gray-50 rounded px-2 py-1 break-all">
                        {sampleText(s, TASK_FIELD_LABELS)}
                      </div>
                    ))}
                  </div>
                )}
              </div>
            )}

            {((wbPreview?.warnings.length || 0) > 0) && (
              <div className="px-3 py-2 bg-amber-50 border border-amber-200 rounded-lg">
                <div className="text-xs font-medium text-amber-700 mb-1">解析告警</div>
                <ul className="list-disc pl-5 text-xs text-amber-700 space-y-0.5">
                  {wbPreview!.warnings.map((w, i) => <li key={i}>{w}</li>)}
                </ul>
              </div>
            )}

            {nothingToImport && (
              <div className="px-3 py-2 bg-red-50 border border-red-200 text-red-700 text-sm rounded-lg">
                没有可导入的内容 —— 请检查文件里是否有对应的工作表与表头（见上方告警），或换一份文件。
              </div>
            )}

            <div className="flex items-center gap-2 pt-1">
              <button onClick={reset} disabled={busy}
                className="px-4 py-2 bg-gray-100 text-gray-700 rounded-lg hover:bg-gray-200 text-sm disabled:opacity-40">
                ← 重新选择
              </button>
              <button
                onClick={doCommit}
                disabled={busy || nothingToImport}
                className="px-4 py-2 bg-teal-600 text-white rounded-lg hover:bg-teal-700 text-sm font-medium disabled:opacity-40"
                title={nothingToImport ? '没有可导入的内容' : (!adminAuthed ? '需要管理员登录' : '')}
              >
                {busy ? '导入中…' : '确认导入'}
              </button>
              {!adminAuthed && (
                <span className="text-xs text-amber-600">确认导入需管理员登录（点按钮会引导登录）</span>
              )}
            </div>
          </div>
        )}

        {/* ── 第 3 步：结果 ── */}
        {step === 'done' && result && (
          <div ref={resultRef} className="space-y-3">
            <div className="px-3 py-2.5 bg-emerald-50 border border-emerald-200 text-emerald-800 text-sm rounded-lg">
              {result.message}
            </div>
            {result.warnings.length > 0 && (
              <div className="px-3 py-2 bg-amber-50 border border-amber-200 rounded-lg">
                <div className="text-xs font-medium text-amber-700 mb-1">导入告警</div>
                <ul className="list-disc pl-5 text-xs text-amber-700 space-y-0.5">
                  {result.warnings.map((w, i) => <li key={i}>{w}</li>)}
                </ul>
              </div>
            )}
            <p className="text-xs text-gray-400">
              提醒：导入只增补、不删除。要删实体请在主数据列表里用「删除」按钮。
            </p>
            <button onClick={onClose}
              className="px-4 py-2 bg-teal-600 text-white rounded-lg hover:bg-teal-700 text-sm font-medium">
              完成
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
