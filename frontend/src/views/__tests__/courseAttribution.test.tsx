// @vitest-environment jsdom
/**
 * 教师端课程卡片：真实课程归属的展示（R50 前端部分）
 *
 * 背景
 * ----
 * 后端 R50 起在 `/teachers/{name}/courses` 的每张卡片上返回 `course` /
 * `courses` / `course_ambiguous` / `weeks`。以前界面只显示**实验项目名**，
 * 教师看不出自己上的是哪门课；同一项目跨两门课时，卡片还会把两门课合成一张。
 *
 * 这里锁住三件事：
 *   1. 有归属 → 显示"课程：<名字>"；
 *   2. 横跨多门课 → 明确点出来，不让第一门课冒充整张卡片；
 *   3. 旧后端没这个字段 → 不显示空标签（向后兼容，别渲染出"课程："这种半截文案）。
 */
import { afterEach, describe, expect, it } from 'vitest';
import { cleanup, render, screen } from '@testing-library/react';
import type { TeacherCourse } from '../../api/client';
import CoursesView from '../CoursesView';

afterEach(cleanup);

const card = (over: Partial<TeacherCourse> = {}): TeacherCourse => ({
  course_name: '扭摆法测物体转动惯量', // 实验项目名，不是课程名
  class_name: '2024级1班',
  original_time: '周三 13:30-15:45',
  room: '物理楼101',
  week_range: '3、5、9、11周',
  total_slots: 4,
  adjustable: true,
  adjustable_reason: '',
  ...over,
});

const renderView = (courses: TeacherCourse[]) =>
  render(
    <CoursesView
      currentTeacher="吴琳"
      courses={courses}
      loading={false}
      nlInput=""
      nlMsg=""
      followupQ={[]}
      onNlInputChange={() => {}}
      onNlSubmit={() => {}}
      onReschedule={() => {}}
    />,
  );

describe('CoursesView · 课程归属', () => {
  it('单门课时显示准确课程名', () => {
    renderView([card({ course: '普通物理实验3', courses: ['普通物理实验3'], course_ambiguous: false })]);
    expect(screen.getByText(/课程：普通物理实验3/)).toBeTruthy();
    expect(screen.queryByText(/横跨多门课/)).toBeNull();
  });

  it('横跨两门课时必须点名，而不是只显示第一门', () => {
    renderView([
      card({
        course: '大学物理实验B、普通物理实验3',
        courses: ['大学物理实验B', '普通物理实验3'],
        course_ambiguous: true,
      }),
    ]);
    const line = screen.getByText(/课程：大学物理实验B、普通物理实验3/);
    expect(line.textContent).toContain('横跨多门课');
  });

  it('旧后端没有归属字段时不渲染空标签', () => {
    renderView([card()]);
    expect(screen.queryByText(/课程：/)).toBeNull();
  });

  it('不连续的周次原样显示，不被解释成区间', () => {
    renderView([card({ course: '普通物理实验3', week_range: '3、5、9、11周' })]);
    expect(screen.getByText(/3、5、9、11周/)).toBeTruthy();
  });
});
