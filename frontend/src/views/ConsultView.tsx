import ReactMarkdown from 'react-markdown';
import type { CloudConsultLog } from '../cloudApi';

interface Props {
  question: string;
  answer: string;
  loading: boolean;
  logs: CloudConsultLog[];
  showLogs: boolean;
  onQuestionChange: (v: string) => void;
  onAsk: () => void;
  onStop: () => void;
  onToggleLogs: () => void;
}

/** 实验规范查询（云大模型流式问答 + 云端咨询历史）。面向排课人员/教师。 */
export default function ConsultView({
  question, answer, loading, logs, showLogs,
  onQuestionChange, onAsk, onStop, onToggleLogs,
}: Props) {
  return (
    <div>
      <div className="flex items-center justify-between mb-2">
        <h2 className="text-lg font-semibold text-gray-900">
          🔬 实验规范查询
        </h2>
        <span className="text-xs px-2 py-1 bg-emerald-50 text-emerald-700 rounded-full border border-emerald-200">
          ☁️ 云端大模型
        </span>
      </div>
      <p className="text-sm text-gray-500 mb-6">
        面向排课人员/教师，查询实验项目的学时、所需场地、操作步骤、数据处理与误差分析规范、报告规范等问题（免密钥云端大模型，回答流式生成）
      </p>

      <div className="bg-white rounded-xl p-5 shadow-sm border border-gray-200">
        <div className="flex gap-2">
          <input
            value={question}
            onChange={(e) => onQuestionChange(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && onAsk()}
            placeholder="如：扭摆法测转动惯量需要几学时、用哪间实验室、数据处理与报告规范是什么？"
            className="flex-1 min-w-0 px-3 py-2.5 border border-gray-300 rounded-lg text-sm focus:ring-2 focus:ring-blue-500 outline-none"
            disabled={loading}
          />
          {loading ? (
            <button
              onClick={onStop}
              className="px-5 py-2.5 bg-gray-800 text-white rounded-lg hover:bg-gray-900 text-sm font-medium whitespace-nowrap"
            >
              停止生成
            </button>
          ) : (
            <button
              onClick={onAsk}
              disabled={!question.trim()}
              className="px-5 py-2.5 bg-blue-600 text-white rounded-lg hover:bg-blue-700 disabled:opacity-50 text-sm font-medium whitespace-nowrap"
            >
              提问
            </button>
          )}
        </div>

        {answer && (
          <div className="mt-4 px-4 py-3 bg-gray-50 border border-gray-200 rounded-lg text-sm text-gray-700 prose prose-sm max-w-none">
            <ReactMarkdown>{answer}</ReactMarkdown>
          </div>
        )}

        {/* 云端咨询历史（需登录，按用户隔离） */}
        <div className="mt-4 pt-4 border-t border-gray-100">
          <button
            onClick={onToggleLogs}
            className="text-sm text-blue-600 hover:text-blue-700"
          >
            {showLogs ? '收起云端历史 ▲' : '查看云端咨询历史 ▼'}
          </button>
          {showLogs && (
            <div className="mt-3 space-y-2 max-h-80 overflow-y-auto">
              {logs.length === 0 && (
                <div className="text-sm text-gray-400 py-3">还没有云端咨询记录</div>
              )}
              {logs.map((log) => (
                <div key={log.id} className="bg-gray-50 border border-gray-200 rounded-lg p-3">
                  <div className="text-xs text-gray-400 mb-1">
                    {new Date(log.created_at).toLocaleString('zh-CN')}
                  </div>
                  <div className="text-sm font-medium text-gray-800">Q：{log.question}</div>
                  <div className="text-sm text-gray-600 mt-1 whitespace-pre-wrap line-clamp-3">
                    A：{log.answer}
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
