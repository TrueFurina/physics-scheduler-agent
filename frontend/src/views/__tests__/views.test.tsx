// @vitest-environment jsdom
/**
 * 视图组件冒烟测试。
 *
 * 目的不是穷举渲染细节（那是 UI 探针的活，跑真实产物），而是锁住三条**业务不变量**：
 *  1. 分组/计数逻辑正确（可调 vs 不可调、审批台条数、沙盘格子）；
 *  2. 回调参数正确（审批的 approve 布尔、目标周次、提交动作）；
 *  3. 空态不崩（0 教师 / 0 工单 / 0 时段）。
 *
 * 背景：App.tsx 从 1711 行拆成 987 行 + 7 个视图后，这些视图之前**没有任何自动化保护**，
 * 只能靠跑一遍浏览器探针发现回归。探针慢且依赖环境，这里补最便宜的一层。
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import type { Teacher, TeacherCourse } from '../../api/client';
import IdentityView from '../IdentityView';
import AdminView from '../AdminView';
import CoursesView from '../CoursesView';
import RescheduleView from '../RescheduleView';
import ParallelView from '../ParallelView';
import ConsultView from '../ConsultView';

// vitest 未开 globals，RTL 的自动 cleanup 不会注册 —— 不显式清理的话，
// 每个用例都往同一份 document 里堆 DOM，getByRole 会命中上个用例的按钮。
afterEach(cleanup);

const teacher = (name: string): Teacher => ({ name, title: '副教授' });

const course = (over: Partial<TeacherCourse> = {}): TeacherCourse => ({
  course_name: '大学物理实验',
  class_name: '2024级1班',
  original_time: '周三 13:30-15:45',
  room: '物理楼101',
  week_range: '1-16周',
  total_slots: 16,
  adjustable: true,
  adjustable_reason: '',
  ...over,
});

describe('IdentityView', () => {
  it('渲染每位教师，点击后把姓名回传（教师姓名是业务主体，不能被吞）', () => {
    const onSelectTeacher = vi.fn();
    render(
      <IdentityView
        teachers={[teacher('吴琳'), teacher('张妹玉')]}
        autoScheduleLoading={false}
        autoScheduleMsg=""
        onOpenAdmin={() => {}}
        onAutoSchedule={() => {}}
        onSelectTeacher={onSelectTeacher}
      />,
    );
    fireEvent.click(screen.getByText('张妹玉'));
    expect(onSelectTeacher).toHaveBeenCalledWith('张妹玉');
  });

  it('0 教师显示空态而不是白屏', () => {
    render(
      <IdentityView
        teachers={[]}
        autoScheduleLoading={false}
        autoScheduleMsg=""
        onOpenAdmin={() => {}}
        onAutoSchedule={() => {}}
        onSelectTeacher={() => {}}
      />,
    );
    expect(screen.getByText(/暂无教师数据/)).toBeTruthy();
  });

  it('自动排课按钮在请求期间置灰并改文案（防重复提交）', () => {
    render(
      <IdentityView
        teachers={[]}
        autoScheduleLoading
        autoScheduleMsg="已排定 433 条"
        onOpenAdmin={() => {}}
        onAutoSchedule={() => {}}
        onSelectTeacher={() => {}}
      />,
    );
    const btn = screen.getByRole('button', { name: /排课中/ }) as HTMLButtonElement;
    expect(btn.disabled).toBe(true);
    expect(screen.getByText('已排定 433 条')).toBeTruthy();
  });
});

describe('AdminView', () => {
  it('同意/驳回分别回传 true/false，并带上工单 id', () => {
    const onApprove = vi.fn();
    render(
      <AdminView
        loading={false}
        onBack={() => {}}
        onApprove={onApprove}
        requests={[{
          id: 42,
          teacher: '吴琳',
          course_name: '大学物理实验',
          class_name: '2024级1班',
          original_time: '周三 13:30-15:45',
          target_week: 5,
          reason: '出差',
          status: 'pending',
          admin_comment: '',
          created_at: '2026-10-21',
        }]}
      />,
    );
    fireEvent.click(screen.getByRole('button', { name: '同意' }));
    expect(onApprove).toHaveBeenCalledWith(42, true, '同意调课');
    fireEvent.click(screen.getByRole('button', { name: '驳回' }));
    expect(onApprove).toHaveBeenCalledWith(42, false, '时间冲突，请重新选择');
  });

  it('0 工单显示空态（演示时不能是一块空白）', () => {
    render(<AdminView loading={false} requests={[]} onBack={() => {}} onApprove={() => {}} />);
    expect(screen.getByText(/暂无待审批的调课申请/)).toBeTruthy();
    expect(screen.getByText(/待审批申请：0 条/)).toBeTruthy();
  });
});

describe('CoursesView', () => {
  it('按 adjustable 分成两组，只有可调课程给「申请调课」入口', () => {
    const onReschedule = vi.fn();
    const ok = course();
    const no = course({ course_name: '近代物理实验', adjustable: false, adjustable_reason: '合班课' });
    render(
      <CoursesView
        currentTeacher="吴琳"
        courses={[ok, no]}
        loading={false}
        nlInput=""
        nlMsg=""
        followupQ={[]}
        onNlInputChange={() => {}}
        onNlSubmit={() => {}}
        onReschedule={onReschedule}
      />,
    );
    expect(screen.getByText(/共 2 门课程/)).toBeTruthy();
    expect(screen.getByText(/合班课/)).toBeTruthy();
    expect(screen.getAllByRole('button', { name: /申请调课/ })).toHaveLength(1);
    fireEvent.click(screen.getByRole('button', { name: /申请调课/ }));
    expect(onReschedule).toHaveBeenCalledWith(ok);
  });

  it('多轮追问逐条展示', () => {
    render(
      <CoursesView
        currentTeacher="吴琳"
        courses={[]}
        loading={false}
        nlInput=""
        nlMsg=""
        followupQ={['要调到第几周？', '哪个课程？']}
        onNlInputChange={() => {}}
        onNlSubmit={() => {}}
        onReschedule={() => {}}
      />,
    );
    expect(screen.getByText(/需要补充信息/)).toBeTruthy();
    expect(screen.getByText(/要调到第几周/)).toBeTruthy();
  });
});

describe('RescheduleView', () => {
  const baseProps = {
    selectedCourse: course(),
    currentTeacher: '吴琳',
    targetWeek: 5,
    available: [{ weekday: '周三', time_slot: '567', room: '物理楼103', date: '3月12日' }],
    excluded: [{ weekday: '周四', time_slot: '678', room: '物理楼101', reason_type: 'conflict', reason: '教师冲突' }],
    showAllAvail: false,
    impact: null,
    impactLoading: false,
    requestStatus: '',
    loading: false,
    onBack: () => {},
    onTargetWeekChange: () => {},
    onToggleShowAll: () => {},
    onImpactAnalysis: () => {},
    onSubmit: () => {},
  };

  it('沙盘同时渲染可用格与排除格（两者的判定不能互相覆盖）', () => {
    render(<RescheduleView {...baseProps} />);
    // 教室名与排除原因在沙盘格和下方列表里都会出现，故用 getAllByText 断言"存在且不止一处"
    expect(screen.getAllByText('物理楼103').length).toBeGreaterThan(0);
    expect(screen.getAllByText('教师冲突').length).toBeGreaterThan(0);
  });

  it('改目标周次回传数字而不是字符串（后端按 int 收）', () => {
    const onTargetWeekChange = vi.fn();
    render(<RescheduleView {...baseProps} onTargetWeekChange={onTargetWeekChange} />);
    fireEvent.change(screen.getByRole('combobox'), { target: { value: '9' } });
    expect(onTargetWeekChange).toHaveBeenCalledWith(9);
  });

  it('已提交后不再显示提交按钮（防重复提交）', () => {
    render(<RescheduleView {...baseProps} requestStatus="已提交，等待管理员审批" />);
    expect(screen.queryByRole('button', { name: /提交调课申请/ })).toBeNull();
    expect(screen.getByText(/等待管理员审批/)).toBeTruthy();
  });

  it('自身冲突单列，不混进「相关教师」（班级名不得出现在教师栏）', () => {
    render(
      <RescheduleView
        {...baseProps}
        impact={{
          conflict_count: 3,
          affected_classes: ['24软件1班'],
          affected_rooms: ['A101'],
          affected_teachers: ['陈老师'],
          affected_students: ['高等数学A2'],
          self_busy_count: 1,
          self_busy_slots: [
            { weekday: '周二', time_slot: '567', class_name: '24电子1班', experiment: '示波器的使用' },
          ],
          conflict_slots: [],
        }}
      />,
    );
    expect(screen.getByText(/您本人在这些时段已有课（1 处）/)).toBeTruthy();
    // 自身冲突的班级只出现在自身冲突卡里
    expect(screen.getAllByText(/24电子1班/).length).toBe(1);
    // 教师栏里是教师姓名，班级名不许越界
    expect(screen.getByText('陈老师')).toBeTruthy();
  });

  it('没有自身冲突时不渲染自身冲突卡（0 不能被当成"有"）', () => {
    render(
      <RescheduleView
        {...baseProps}
        impact={{
          conflict_count: 1, affected_classes: [], affected_rooms: [], affected_teachers: [],
          affected_students: [], self_busy_count: 0, self_busy_slots: [], conflict_slots: [],
        }}
      />,
    );
    expect(screen.queryByText(/您本人在这些时段已有课/)).toBeNull();
  });

  it('AI 候选方案：加载中给占位，到货后渲染方案与理由（PRD F9 的画面）', () => {
    const plans = [
      { weekday: '周一', time_slot: '567', room: 'A503', reason: '该时段教室空闲且学生无课' },
      { weekday: '周三', time_slot: '91011', room: 'A516', reason: '与教师其他课程间隔充足' },
    ];
    const { rerender } = render(<RescheduleView {...baseProps} planLoading />);
    expect(screen.getByText(/正在从可用时段里挑候选/)).toBeTruthy();
    expect(screen.queryByText(/方案 1/)).toBeNull();

    rerender(<RescheduleView {...baseProps} planLoading={false} aiPlans={plans} />);
    expect(screen.getByText(/AI 推荐候选方案/)).toBeTruthy();
    expect(screen.getByText(/方案 1/)).toBeTruthy();
    expect(screen.getByText(/方案 2/)).toBeTruthy();
    expect(screen.getByText('A503')).toBeTruthy();
    expect(screen.getByText(/该时段教室空闲且学生无课/)).toBeTruthy();
  });

  it('既没加载也没候选时不渲染候选区块（不占位、不留空标题）', () => {
    render(<RescheduleView {...baseProps} planLoading={false} aiPlans={[]} />);
    expect(screen.queryByText(/AI 推荐候选方案/)).toBeNull();
  });
});

describe('ParallelView', () => {
  const baseProps = {
    weekday: '',
    courses: [{
      class_name: '2024级',
      weekday: '周三',
      projects: [{ project: '扭摆法测转动惯量', groups: ['1组', '2组'] }],
      total_units: 24,
    }],
    selected: '',
    loading: false,
    solving: false,
    result: null,
    viewWeek: null,
    onWeekdayChange: () => {},
    onSolve: () => {},
    onExport: () => {},
    onViewWeekChange: () => {},
  };

  it('课程卡片回传班级名（求解按班级发请求）', () => {
    const onSolve = vi.fn();
    render(<ParallelView {...baseProps} onSolve={onSolve} />);
    expect(screen.getByText(/1 个实验项目/)).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: /一键排课/ }));
    expect(onSolve).toHaveBeenCalledWith('2024级');
  });

  it('有结果时显示排定数，且无结果时不渲染结果块', () => {
    const { rerender } = render(<ParallelView {...baseProps} />);
    expect(screen.queryByText(/个单元已排定/)).toBeNull();

    rerender(
      <ParallelView
        {...baseProps}
        result={{
          status: 'OPTIMAL',
          weekday: '周三',
          scheduled: 24,
          total_units: 24,
          weeks_used: 4,
          by_week: { '1': 6, '2': 6 },
          schedule: [{ week: 1, time_slot: '567', room: '物理楼101', group: '1组', project: '扭摆法测转动惯量', teacher: '吴琳' }],
        }}
      />,
    );
    expect(screen.getByText(/24\/24 个单元已排定/)).toBeTruthy();
    expect(screen.getAllByText('第1周').length).toBeGreaterThan(0);
  });

  it('求解中按钮置灰（同一时刻只允许一个求解）', () => {
    render(<ParallelView {...baseProps} selected="2024级" solving />);
    const btn = screen.getByRole('button', { name: /求解中/ }) as HTMLButtonElement;
    expect(btn.disabled).toBe(true);
  });
});

describe('ConsultView', () => {
  it('两个模块的标题与占位文案不同（选课 vs 实验辅导）', () => {
    const { rerender } = render(
      <ConsultView
        mode="course_select"
        question=""
        answer=""
        loading={false}
        logs={[]}
        showLogs={false}
        onQuestionChange={() => {}}
        onAsk={() => {}}
        onStop={() => {}}
        onToggleLogs={() => {}}
      />,
    );
    expect(screen.getByText('🎓 选课咨询')).toBeTruthy();

    rerender(
      <ConsultView
        mode="tutor"
        question=""
        answer=""
        loading={false}
        logs={[]}
        showLogs={false}
        onQuestionChange={() => {}}
        onAsk={() => {}}
        onStop={() => {}}
        onToggleLogs={() => {}}
      />,
    );
    expect(screen.getByText('🔬 实验辅导')).toBeTruthy();
  });

  it('空问题不能提问；生成中改显示「停止生成」并走 onStop', () => {
    const onAsk = vi.fn();
    const onStop = vi.fn();
    const props = {
      mode: 'tutor' as const,
      answer: '',
      logs: [],
      showLogs: false,
      onQuestionChange: () => {},
      onAsk,
      onStop,
      onToggleLogs: () => {},
    };
    const { rerender } = render(<ConsultView {...props} question="" loading={false} />);
    const ask = screen.getByRole('button', { name: '提问' }) as HTMLButtonElement;
    expect(ask.disabled).toBe(true);

    rerender(<ConsultView {...props} question="误差来源？" loading />);
    fireEvent.click(screen.getByRole('button', { name: /停止生成/ }));
    expect(onStop).toHaveBeenCalled();
    expect(onAsk).not.toHaveBeenCalled();
  });
});
