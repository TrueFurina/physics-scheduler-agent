// @vitest-environment jsdom
/**
 * 课程未识别时的提示（R33）。
 *
 * 背景链路：
 *   R21  agent 层加了 `course_unrecognized` + 一段"⚠️ 没能从您的话里认出
 *        具体是哪门课"的文案，注释写着"不阻断，只把这件事说出口"。
 *   R31  API 层漏透传（已修）。
 *   R32  五个分支键集统一（已修）。
 *   R33  前端**从来没读过这个字段**：`grep -c course_unrecognized frontend/src` = 0。
 *
 * 更糟的是文案这条路也是断的：前端只取首行 ——
 *
 *     setNlMsg(`✅ ${r.data.message.split('\n')[0]}`)
 *
 * 而那段 ⚠️ 提示在 message 的第 3 段。也就是说 R21 声称的"明确提示"，
 * **用户一个字都看不到**，只看到一句 ✅。
 *
 * 这份测试锁住的是"沙盘页必须说清楚为什么需要补选课程"，
 * 而不是某种具体实现。
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import type { TeacherCourse } from '../../api/client';
import RescheduleView from '../RescheduleView';

afterEach(cleanup);

const course = (over: Partial<TeacherCourse> = {}): TeacherCourse => ({
  course_name: '声速测量',
  class_name: '2024级1班',
  original_time: '周三 13:30-15:45',
  room: '物理楼101',
  week_range: '1-16周',
  total_slots: 16,
  adjustable: true,
  adjustable_reason: '',
  ...over,
});

const base = {
  currentTeacher: '吴琳',
  targetWeek: 3,
  available: [{ weekday: '周一', time_slot: '567', room: 'A101' }],
  excluded: [],
  showAllAvail: false,
  impact: null,
  impactLoading: false,
  requestStatus: '',
  loading: false,
  onBack: vi.fn(),
  onTargetWeekChange: vi.fn(),
  onToggleShowAll: vi.fn(),
  onImpactAnalysis: vi.fn(),
  onSubmit: vi.fn(),
};

describe('RescheduleView · 课程未识别提示（R33）', () => {
  it('未选课程但有候选时，页面要说明"需要补选"而不是只显示"未指定课程"', () => {
    render(
      <RescheduleView
        {...base}
        selectedCourse={null}
        courseOptions={[course(), course({ course_name: '等厚干涉' })]}
        onSelectCourse={vi.fn()}
      />,
    );

    // "未指定课程"只是标签，教秘看不出这是需要他动手的
    const text = document.body.textContent || '';
    expect(text).toContain('未指定课程');
    expect(
      /请(先)?(选择|补选)|没能(认出|识别)|识别不出|无法确定/.test(text),
      '页面上没有一句说明"为什么课程是空的、你要做什么" —— '
      + '教秘会以为系统已经知道是哪门课',
    ).toBe(true);
  });

  it('已选定课程时，不该再出现"补选"提示', () => {
    render(
      <RescheduleView
        {...base}
        selectedCourse={course()}
        courseOptions={[]}
        onSelectCourse={vi.fn()}
      />,
    );
    const text = document.body.textContent || '';
    expect(text).not.toMatch(/没能(认出|识别)|请先选择课程|请补选/);
  });

  it('候选下拉可切换课程（补选路径必须真的可用）', () => {
    const onSelectCourse = vi.fn();
    render(
      <RescheduleView
        {...base}
        selectedCourse={null}
        courseOptions={[course(), course({ course_name: '等厚干涉' })]}
        onSelectCourse={onSelectCourse}
      />,
    );

    // 页面上还有"目标周次"下拉 —— 课程那个是第一个（且在未选定课程时才渲染）。
    // 先确认取到的确实是课程下拉，避免"选错元素但恰好通过"。
    const courseSelect = screen.getAllByRole('combobox')[0];
    expect(courseSelect.textContent).toContain('选择课程');
    // value 的形式是 `课程名|班级`（同名课挂在多个班下时要能区分）
    fireEvent.change(courseSelect, { target: { value: '等厚干涉|2024级1班' } });
    expect(onSelectCourse).toHaveBeenCalledTimes(1);
    expect(onSelectCourse.mock.calls[0][0].course_name).toBe('等厚干涉');
  });
});


describe('App.tsx · 课程未识别警告必须到达用户（R33）', () => {
  /**
   * R21 在后端 message 末尾追加了「⚠️ 没能从您的话里认出具体是哪门课…」。
   * 实测：message 共 35 行，警告在**第 34 行**。
   * 而 App.tsx 的摘要只取首行：
   *
   *     setNlMsg(`✅ ${r.data.message.split('
')[0]}`)
   *
   * ⇒ 教秘只看到一句 ✅，对"要调哪门课"一无所知。
   *
   * 这条路径没有可挂载的浅层组件（跑在 App 的事件处理器里），
   * 用源码契约守卫：摘要必须按结构化字段补警告。
   */
  it('摘要拼接了 course_unrecognized 的警示', () => {
    const fs = require('fs');
    const path = require('path');
    const src = fs.readFileSync(
      path.resolve(__dirname, '../../../src/App.tsx'), 'utf8',
    );

    // 找到自然语言调课里设置摘要的那一行
    const idx = src.indexOf('let summary = ');
    expect(idx, '找不到摘要拼接处 —— App.tsx 结构变了，请重新核对').toBeGreaterThan(-1);
    const window = src.slice(idx, idx + 600);
    expect(window).toContain('course_unrecognized');
    expect(window).toContain('setNlMsg(summary)');
  });

  it('前端类型里声明了这两个字段（否则代码根本读不到）', () => {
    const fs = require('fs');
    const path = require('path');
    const src = fs.readFileSync(
      path.resolve(__dirname, '../../../src/api/client.ts'), 'utf8',
    );
    expect(src).toContain('course_name?');
    expect(src).toContain('course_unrecognized?');
  });
});
