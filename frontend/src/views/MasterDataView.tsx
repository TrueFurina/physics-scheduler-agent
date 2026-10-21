import { useEffect, useState } from 'react';
import { rescheduleApi } from '../api/client';
import type { MasterOverview } from '../types';

/**
 * 主数据管理（导师座谈一号行动项「基于教学大纲梳理实验课程属性、重构数据库」）。
 *
 * 五张实体表：课程 / 实验项目 / 教师 / 场地 / 班级。读免鉴权；增删改走 admin
 * 会话令牌（写接口挂 require_admin），未登录时写按钮禁用并引导登录。
 *
 * 反推基线：初次进入从安排表自动反推（后端 `_ensure_master_data`），"一键反推"
 * 按钮用于教秘手动重置到安排表基线（撤销本会话内的增删改）。
 */

type TabKey = 'experiments' | 'teachers' | 'rooms' | 'classes' | 'courses';

const TABS: { key: TabKey; label: string; icon: string; hint: string }[] = [
  { key: 'experiments', label: '实验项目', icon: '🧪', hint: '名称 / 所属课程 / 可授课教师 / 可用场地 / 适用班级' },
  { key: 'teachers', label: '教师', icon: '👩‍🏫', hint: '姓名 / 可授实验项目' },
  { key: 'rooms', label: '场地', icon: '🏫', hint: '名称 / 容量' },
  { key: 'classes', label: '班级', icon: '📚', hint: '名称 / 学生组' },
  { key: 'courses', label: '课程', icon: '📖', hint: '课程名称（含哪些实验项目）' },
];

interface Props {
  adminAuthed: boolean;
  onRequireLogin: () => void;
}

/** 逗号分隔的文本 → 去空去重的数组。 */
function splitList(s: string): string[] {
  return [...new Set(s.split(/[,，、\n]/).map((x) => x.trim()).filter(Boolean))];
}

export default function MasterDataView({ adminAuthed, onRequireLogin }: Props) {
  const [data, setData] = useState<MasterOverview | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [tab, setTab] = useState<TabKey>('experiments');
  const [busy, setBusy] = useState(false);
  const [showForm, setShowForm] = useState(false);
  const [form, setForm] = useState<Record<string, string>>({});

  const load = async () => {
    setLoading(true);
    setError('');
    try {
      const r = await rescheduleApi.masterOverview();
      setData(r.data.data);
    } catch (err: any) {
      setError(err?.response?.data?.detail || '加载主数据失败');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { load(); }, []);

  const guard = (): boolean => {
    if (adminAuthed) return true;
    onRequireLogin();
    return false;
  };

  const refresh = (next: MasterOverview) => setData(next);

  const doInit = async () => {
    if (!guard()) return;
    setBusy(true);
    try {
      const r = await rescheduleApi.masterInit();
      refresh(r.data.data);
      setShowForm(false);
    } catch (err: any) {
      setError(err?.response?.data?.detail || '反推失败');
    } finally {
      setBusy(false);
    }
  };

  const doDelete = async (kind: TabKey, name: string) => {
    if (!guard()) return;
    if (!window.confirm(`确认删除「${name}」？此操作会同步摘除相关引用。`)) return;
    setBusy(true);
    try {
      const r = await (kind === 'experiments' ? rescheduleApi.masterDeleteExperiment(name)
        : kind === 'teachers' ? rescheduleApi.masterDeleteTeacher(name)
        : kind === 'rooms' ? rescheduleApi.masterDeleteRoom(name)
        : kind === 'classes' ? rescheduleApi.masterDeleteClass(name)
        : rescheduleApi.masterDeleteCourse(name));
      refresh(r.data.data);
    } catch (err: any) {
      setError(err?.response?.data?.detail || '删除失败');
    } finally {
      setBusy(false);
    }
  };

  const doSubmit = async () => {
    if (!guard()) return;
    setBusy(true);
    try {
      let r;
      if (tab === 'experiments') {
        r = await rescheduleApi.masterUpsertExperiment({
          name: form.name || '',
          course: form.course || undefined,
          teachers: form.teachers ? splitList(form.teachers) : undefined,
          rooms: form.rooms ? splitList(form.rooms) : undefined,
          classes: form.classes ? splitList(form.classes) : undefined,
        });
      } else if (tab === 'teachers') {
        r = await rescheduleApi.masterUpsertTeacher({
          name: form.name || '',
          projects: form.projects ? splitList(form.projects) : undefined,
        });
      } else if (tab === 'rooms') {
        const cap = form.capacity?.trim();
        r = await rescheduleApi.masterUpsertRoom({
          name: form.name || '',
          capacity: cap ? Number(cap) : undefined,
        });
      } else if (tab === 'classes') {
        r = await rescheduleApi.masterUpsertClass({
          name: form.name || '',
          groups: form.groups ? splitList(form.groups) : undefined,
          weekday: form.weekday?.trim() || undefined,
          time_slots: form.time_slots ? splitList(form.time_slots) : undefined,
          week_min: form.week_min?.trim() ? Number(form.week_min) : undefined,
          week_max: form.week_max?.trim() ? Number(form.week_max) : undefined,
        });
      } else {
        r = await rescheduleApi.masterUpsertCourse({ name: form.name || '' });
      }
      refresh(r.data.data);
      setShowForm(false);
      setForm({});
    } catch (err: any) {
      setError(err?.response?.data?.detail || '保存失败');
    } finally {
      setBusy(false);
    }
  };

  const field = (key: string, label: string, placeholder: string, wide = false) => (
    <label className={`block ${wide ? 'col-span-2' : ''}`}>
      <span className="text-xs text-gray-500">{label}</span>
      <input
        value={form[key] || ''}
        onChange={(e) => setForm({ ...form, [key]: e.target.value })}
        placeholder={placeholder}
        className="mt-0.5 w-full px-3 py-1.5 border border-gray-200 rounded-lg text-sm focus:outline-none focus:border-teal-400"
      />
    </label>
  );

  const counts = data
    ? { experiments: data.experiments.length, teachers: data.teachers.length,
        rooms: data.rooms.length, classes: data.classes.length, courses: data.courses.length }
    : { experiments: 0, teachers: 0, rooms: 0, classes: 0, courses: 0 };

  return (
    <main className="max-w-4xl mx-auto px-4 py-6">
      <div className="flex items-start justify-between flex-wrap gap-3 mb-4">
        <div>
          <h1 className="text-xl font-semibold text-gray-800">🗂️ 主数据管理</h1>
          <p className="text-sm text-gray-500 mt-1">
            导师一号行动项：把「课程 → 实验项目 → 教师/场地」与「班级 → 学生组」建成可增删改的实体库，
            排课时随时灵活调取、新增项目不必伪造旧课表行。
          </p>
        </div>
        <div className="flex gap-2">
          <button
            onClick={doInit}
            disabled={busy || !adminAuthed}
            className="px-3 py-2 bg-gray-100 text-gray-700 rounded-lg hover:bg-gray-200 text-sm font-medium disabled:opacity-40"
            title="清空本会话内的增删改，回到安排表反推基线"
          >
            ↺ 从安排表反推
          </button>
          <button
            onClick={() => { if (guard()) { setShowForm((v) => !v); setForm({}); } }}
            disabled={busy || !adminAuthed}
            className="px-3 py-2 bg-teal-600 text-white rounded-lg hover:bg-teal-700 text-sm font-medium disabled:opacity-40"
          >
            ＋ 新增
          </button>
        </div>
      </div>

      {!adminAuthed && (
        <div className="mb-3 px-3 py-2 bg-amber-50 border border-amber-200 text-amber-700 text-sm rounded-lg">
          当前为只读视图。增删改需管理员登录 —— 点「⚙️ 审批」进入并输入管理员口令后回来即可。
        </div>
      )}
      {error && (
        <div className="mb-3 px-3 py-2 bg-red-50 border border-red-200 text-red-700 text-sm rounded-lg">{error}</div>
      )}

      {/* Tab */}
      <div className="flex gap-2 mb-4 flex-wrap">
        {TABS.map((t) => (
          <button
            key={t.key}
            onClick={() => { setTab(t.key); setShowForm(false); setForm({}); }}
            className={`px-3 py-1.5 rounded-lg text-sm font-medium transition-colors ${
              tab === t.key ? 'bg-teal-600 text-white' : 'bg-gray-100 text-gray-600 hover:bg-gray-200'
            }`}
          >
            {t.icon} {t.label} <span className="opacity-70">({counts[t.key]})</span>
          </button>
        ))}
      </div>

      {showForm && (
        <div className="mb-4 p-4 bg-teal-50 border border-teal-200 rounded-xl">
          <div className="text-sm font-medium text-teal-800 mb-2">
            新增{TABS.find((t) => t.key === tab)?.label} <span className="text-xs text-teal-600 font-normal">（逗号分隔多项）</span>
          </div>
          <div className="grid grid-cols-2 gap-3">
            {field('name', '名称 *', '必填')}
            {tab === 'experiments' && field('course', '所属课程', '大学物理实验')}
            {tab === 'experiments' && field('teachers', '可授课教师', '吴老师, 林老师')}
            {tab === 'experiments' && field('rooms', '可用场地', 'A410, A502')}
            {tab === 'experiments' && field('classes', '适用班级', '23物本, 2024级纺织工程1、2班', true)}
            {tab === 'teachers' && field('projects', '可授实验项目', '扭摆法, 声速测量', true)}
            {tab === 'rooms' && field('capacity', '容量（可选）', '如 40')}
            {tab === 'classes' && field('groups', '学生组', '1组, 2组', true)}
            {tab === 'classes' && field('weekday', '上课星期', '如 周四')}
            {tab === 'classes' && field('time_slots', '节次', '如 567, 678')}
            {tab === 'classes' && field('week_min', '起始周', '如 1')}
            {tab === 'classes' && field('week_max', '结束周', '如 16')}
          </div>
          <div className="flex gap-2 mt-3">
            <button onClick={doSubmit} disabled={busy} className="px-3 py-1.5 bg-teal-600 text-white rounded-lg hover:bg-teal-700 text-sm font-medium disabled:opacity-40">
              保存
            </button>
            <button onClick={() => { setShowForm(false); setForm({}); }} className="px-3 py-1.5 bg-gray-200 text-gray-700 rounded-lg hover:bg-gray-300 text-sm">
              取消
            </button>
          </div>
        </div>
      )}

      {loading ? (
        <div className="text-center text-gray-400 py-10">加载中…</div>
      ) : (
        <div className="bg-white rounded-xl border border-gray-200 overflow-hidden">
          <table className="w-full text-sm">
            <thead>
              <tr className="bg-gray-50 text-left text-xs text-gray-500">
                <th className="px-4 py-2.5 font-medium">名称</th>
                <th className="px-4 py-2.5 font-medium">关联</th>
                <th className="px-4 py-2.5 font-medium text-right w-24">操作</th>
              </tr>
            </thead>
            <tbody>
              {tab === 'experiments' && data?.experiments.map((e) => (
                <tr key={e.name} className="border-t border-gray-100 hover:bg-gray-50">
                  <td className="px-4 py-2.5 font-medium text-gray-800">{e.name}</td>
                  <td className="px-4 py-2.5 text-gray-500">
                    <div>课程：{e.course}</div>
                    <div className="text-xs">教师 {e.teachers.length} · 场地 {e.rooms.length} · 班级 {e.classes.length}</div>
                  </td>
                  <td className="px-4 py-2.5 text-right">
                    <button onClick={() => doDelete('experiments', e.name)} disabled={busy || !adminAuthed}
                      className="text-red-500 hover:text-red-700 text-xs disabled:opacity-30">删除</button>
                  </td>
                </tr>
              ))}
              {tab === 'teachers' && data?.teachers.map((t) => (
                <tr key={t.name} className="border-t border-gray-100 hover:bg-gray-50">
                  <td className="px-4 py-2.5 font-medium text-gray-800">{t.name}</td>
                  <td className="px-4 py-2.5 text-gray-500 text-xs">可授 {t.projects.length} 个项目：{t.projects.slice(0, 4).join('、')}{t.projects.length > 4 ? '…' : ''}</td>
                  <td className="px-4 py-2.5 text-right">
                    <button onClick={() => doDelete('teachers', t.name)} disabled={busy || !adminAuthed}
                      className="text-red-500 hover:text-red-700 text-xs disabled:opacity-30">删除</button>
                  </td>
                </tr>
              ))}
              {tab === 'rooms' && data?.rooms.map((r) => (
                <tr key={r.name} className="border-t border-gray-100 hover:bg-gray-50">
                  <td className="px-4 py-2.5 font-medium text-gray-800">{r.name}</td>
                  <td className="px-4 py-2.5 text-gray-500 text-xs">容量：{r.capacity ?? '未标注'}</td>
                  <td className="px-4 py-2.5 text-right">
                    <button onClick={() => doDelete('rooms', r.name)} disabled={busy || !adminAuthed}
                      className="text-red-500 hover:text-red-700 text-xs disabled:opacity-30">删除</button>
                  </td>
                </tr>
              ))}
              {tab === 'classes' && data?.classes.map((c) => (
                <tr key={c.name} className="border-t border-gray-100 hover:bg-gray-50">
                  <td className="px-4 py-2.5 font-medium text-gray-800">{c.name}</td>
                  <td className="px-4 py-2.5 text-gray-500 text-xs">
                    <div>学生组：{c.groups.length ? c.groups.join('、') : '未标注'}</div>
                    <div>时间范围：{c.weekday || '未定星期'}{c.time_slots.length ? ` ${c.time_slots.join('/')}` : ''} · 第{c.week_min ?? '?'}-{c.week_max ?? '?'}周</div>
                  </td>
                  <td className="px-4 py-2.5 text-right">
                    <button onClick={() => doDelete('classes', c.name)} disabled={busy || !adminAuthed}
                      className="text-red-500 hover:text-red-700 text-xs disabled:opacity-30">删除</button>
                  </td>
                </tr>
              ))}
              {tab === 'courses' && data?.courses.map((c) => (
                <tr key={c.name} className="border-t border-gray-100 hover:bg-gray-50">
                  <td className="px-4 py-2.5 font-medium text-gray-800">{c.name}</td>
                  <td className="px-4 py-2.5 text-gray-500 text-xs">含 {c.experiments.length} 个实验项目：{c.experiments.slice(0, 4).join('、')}{c.experiments.length > 4 ? '…' : ''}</td>
                  <td className="px-4 py-2.5 text-right">
                    <button onClick={() => doDelete('courses', c.name)} disabled={busy || !adminAuthed}
                      className="text-red-500 hover:text-red-700 text-xs disabled:opacity-30">删除</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {data && TABS.find((t) => t.key === tab) && data[tab].length === 0 && (
            <div className="text-center text-gray-400 py-8">暂无数据，点「＋ 新增」录入</div>
          )}
        </div>
      )}
    </main>
  );
}
