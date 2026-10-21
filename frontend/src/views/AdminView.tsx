import type { RescheduleRequest } from '../api/client';

interface Props {
  requests: RescheduleRequest[];
  loading: boolean;
  onBack: () => void;
  onApprove: (id: RescheduleRequest['id'], approved: boolean, comment: string) => void;
}

/** 页面4：管理员审批台（F6）。 */
export default function AdminView({ requests, loading, onBack, onApprove }: Props) {
  return (
    <div>
      <div className="flex items-center justify-between mb-6">
        <h2 className="text-lg font-semibold text-gray-900">管理员审批台</h2>
        <button
          onClick={onBack}
          className="text-sm px-3 py-1.5 bg-gray-100 text-gray-600 rounded-lg hover:bg-gray-200"
        >
          ← 返回
        </button>
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
                  onClick={() => onApprove(r.id, false, '时间冲突，请重新选择')}
                  disabled={loading}
                  className="px-4 py-2 bg-red-500 text-white rounded-lg hover:bg-red-600 disabled:opacity-50 text-sm font-medium"
                >
                  驳回
                </button>
              </div>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
