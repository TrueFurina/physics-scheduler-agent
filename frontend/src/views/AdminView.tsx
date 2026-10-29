import { useState } from 'react';
import type { RescheduleRequest } from '../api/client';
import { rescheduleApi } from '../api/client';

interface Props {
  requests: RescheduleRequest[];
  loading: boolean;
  onBack: () => void;
  onApprove: (id: RescheduleRequest['id'], approved: boolean, comment: string) => void;
}

/** 页面4：管理员审批台（F6）。 */
export default function AdminView({ requests, loading, onBack, onApprove }: Props) {
  const [exporting, setExporting] = useState(false);

  // C：审批记录导出 Excel（留档/归档；admin token 由 axios 拦截器统一附加）
  const handleExport = async () => {
    setExporting(true);
    try {
      const r = await rescheduleApi.exportRequests();
      const url = window.URL.createObjectURL(new Blob([r.data]));
      const a = document.createElement('a');
      a.href = url;
      a.download = '调课申请记录.xlsx';
      document.body.appendChild(a);
      a.click();
      a.remove();
      window.URL.revokeObjectURL(url);
    } catch (err) {
      alert('导出失败：' + (err instanceof Error ? err.message : '未知错误'));
    } finally {
      setExporting(false);
    }
  };

  // 正在填写驳回理由的那条申请；null = 没有在填
  //
  // R34：原先驳回是**一键写死理由**：
  //     onClick={() => onApprove(r.id, false, '时间冲突，请重新选择')}
  // 后端 `comment` 支持任意文本、`App.handleApprove` 也传下去了 —— 只有 UI 写死。
  // 后果不是报错，而是**记录不实**：管理员因"教室维修"驳回，教师收到的
  // 却是"时间冲突，请重新选择"，于是去改时间，改了也没用。
  const [rejecting, setRejecting] = useState<RescheduleRequest | null>(null);
  const [reason, setReason] = useState('');
  const [hint, setHint] = useState('');

  const confirmReject = () => {
    const text = reason.trim();
    if (!text) {
      // 不退回写死的默认话术：那正是要修的问题。
      setHint('请填写驳回理由（会原样告知教师，写清楚他该怎么做）');
      return;
    }
    if (rejecting) onApprove(rejecting.id, false, text);
    setRejecting(null);
    setReason('');
    setHint('');
  };

  return (
    <div>
      <div className="flex items-center justify-between mb-6">
        <h2 className="text-lg font-semibold text-gray-900">管理员审批台</h2>
        <div className="flex gap-2">
          <button
            onClick={handleExport}
            disabled={exporting}
            className="text-sm px-3 py-1.5 bg-emerald-600 text-white rounded-lg hover:bg-emerald-700 disabled:opacity-50"
          >
            {exporting ? '⏳ 导出中…' : '⬇️ 导出记录'}
          </button>
          <button
            onClick={onBack}
            className="text-sm px-3 py-1.5 bg-gray-100 text-gray-600 rounded-lg hover:bg-gray-200"
          >
            ← 返回
          </button>
        </div>
      </div>

      <div className="mb-4 px-4 py-3 bg-gray-50 border border-gray-200 rounded-lg text-sm text-gray-600">
        待审批申请：{requests.length} 条
      </div>

      {requests.length === 0 && (
        <div className="text-center text-gray-400 py-16">
          <p className="text-4xl mb-3">📭</p>
          <p>暂无待审批的调课申请</p>
          <p className="text-sm mt-2 text-gray-300">教师提交申请后会显示在这里</p>
        </div>
      )}

      <div className="space-y-4">
        {requests.map((r) => (
          <div key={r.id} className="bg-white rounded-xl p-5 shadow-sm border border-gray-200">
            <div className="flex items-start justify-between">
              <div>
                <div className="font-semibold text-gray-900">
                  {r.teacher} · {r.course_name}
                </div>
                <div className="text-sm text-gray-500 mt-1">{r.class_name}</div>
                <div className="text-sm text-gray-500 mt-1">
                  {r.original_time} → <span className="font-medium text-blue-600">第{r.target_week}周</span>
                </div>
                <div className="text-xs text-gray-400 mt-2">理由：{r.reason}</div>
              </div>
              <div className="flex gap-2 shrink-0">
                <button
                  onClick={() => onApprove(r.id, true, '同意调课')}
                  disabled={loading}
                  className="px-4 py-2 bg-green-600 text-white rounded-lg hover:bg-green-700 disabled:opacity-50 text-sm font-medium"
                >
                  同意
                </button>
                <button
                  onClick={() => {
                    setRejecting(r);
                    setReason('');
                    setHint('');
                  }}
                  disabled={loading}
                  className="px-4 py-2 bg-red-500 text-white rounded-lg hover:bg-red-600 disabled:opacity-50 text-sm font-medium"
                >
                  驳回
                </button>
              </div>
            </div>

            {rejecting?.id === r.id && (
              <div className="mt-4 pt-4 border-t border-red-100">
                <label className="text-sm text-red-700 font-medium">
                  驳回理由（会原样告知教师）
                </label>
                <textarea
                  value={reason}
                  onChange={(e) => {
                    setReason(e.target.value);
                    if (e.target.value.trim()) setHint('');
                  }}
                  rows={2}
                  placeholder="例如：该教室周四起检修，请改约第4周"
                  className="mt-2 w-full px-3 py-2 border border-red-200 bg-red-50 rounded-lg text-sm focus:ring-2 focus:ring-red-400 outline-none"
                />
                {hint && <div className="text-xs text-red-600 mt-1">{hint}</div>}
                <div className="flex gap-2 mt-2">
                  <button
                    onClick={confirmReject}
                    disabled={loading}
                    className="px-4 py-1.5 bg-red-600 text-white rounded-lg hover:bg-red-700 disabled:opacity-50 text-sm font-medium"
                  >
                    确认驳回
                  </button>
                  <button
                    onClick={() => {
                      setRejecting(null);
                      setReason('');
                      setHint('');
                    }}
                    className="px-4 py-1.5 bg-gray-100 text-gray-600 rounded-lg hover:bg-gray-200 text-sm"
                  >
                    取消
                  </button>
                </div>
              </div>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}
