/**
 * 教务标准课表 + 基础数据台账的导出清单。
 *
 * 三个 kind 都基于**当前安排表**（不是新排的方案），文件名固定，
 * 且 label / file 成对出现，所以做成静态表而不是散在分支里。
 *
 * 为什么单独一个模块：App.tsx 顶到体量守卫 1500 行上限，
 * 静态常量表属于模块层，不该占页面 store 的额度。
 */
export type ExportKind = 'class' | 'room' | 'master';

/** key 用 ExportKind 而不是 string：越界 kind 在编译期就被 tsc 挡住。 */
export const EXPORT_KINDS: Record<ExportKind, { label: string; file: string }> = {
  class: { label: '班级课表', file: '班级课表_全部班级.xlsx' },
  room: { label: '场地课表', file: '场地课表_全部场地.xlsx' },
  master: { label: '基础数据台账', file: '实验排课基础数据台账.xlsx' },
};
