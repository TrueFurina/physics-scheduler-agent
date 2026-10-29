// @vitest-environment jsdom
/**
 * 前端的时段映射必须覆盖后端 `CLOCK` 的全部键（R35）。
 *
 * 后端 `time_model.CLOCK` 是"全校排表的共同语言"，注释原话：
 *
 *     # 标准节次 → (起始分钟, 结束分钟)。这是全校排表的共同语言，改这里等于改口径。
 *     CLOCK = {
 *         "MORNING": (9*60, 11*60+15),       # 09:00-11:15
 *         "567":     (13*60+30, 15*60+45),
 *         "678":     (15*60+45, 18*60),
 *         "91011":   (18*60+30, 20*60+45),
 *         "78":      (...),                  # 历史写法
 *     }
 *
 * 而前端 `constants/labels.ts` 的 `timeSlotLabels` **少了 `MORNING`**。
 *
 * 各视图的写法都是 `timeSlotLabels[x] || x`，所以不会崩 ——
 * 界面上会直接显示 `MORNING` 这串英文。
 *
 * 真实数据（433 条）里有 **4 条 MORNING**（上午课），
 * 数据体检的冲突明细里就会出现「第N周 周X MORNING · 场地」。
 *
 * 这是"兜底掩盖了映射缺失"的典型：有 `|| x` 就不会报错，
 * 也不会有人注意到 —— 直到教秘真的看到那串英文。
 */
import { describe, expect, it } from 'vitest';
// 用 vite 的 ?raw 读后端源码：不依赖 @types/node（本仓库没装，
// views.test.tsx 里那几处 require/__dirname 已经因此让 tsc 报红）。
import timeModelSrc from '../../../../backend/app/utils/time_model.py?raw';
import rescheduleViewSrc from '../../views/RescheduleView.tsx?raw';

/** 直接读源文件文本（走 vite ?raw，不依赖 @types/node）。 */
function readSrc(rel: string): string {
  if (rel === 'views/RescheduleView.tsx') return rescheduleViewSrc;
  throw new Error('未注册的源文件：' + rel);
}
import { GRID_TIME_SLOTS, WEEKDAYS, WEEKDAYS_ALL, timeSlotLabels } from '../../constants/labels';

/** 从后端源码里读出 CLOCK 的键（避免前端硬编码一份"我以为的后端口径"）。 */
function backendClockKeys(): string[] {
  const src = timeModelSrc;
  const start = src.indexOf('CLOCK');
  const open = src.indexOf('{', start);
  const close = src.indexOf('}', open);
  const body = src.slice(open + 1, close);
  return Array.from(body.matchAll(/"([A-Z0-9]+)"\s*:/g)).map((m) => m[1]);
}

describe('时段/星期映射的前后端一致性（R35）', () => {
  it('timeSlotLabels 覆盖后端 CLOCK 的全部键', () => {
    const backendKeys = backendClockKeys();
    expect(backendKeys.length, '没解析出后端 CLOCK 键，测试前提失效').toBeGreaterThan(3);

    const missing = backendKeys.filter((k) => !(k in timeSlotLabels));
    expect(
      missing,
      `前端 timeSlotLabels 缺 ${missing.join(', ')} —— `
      + '界面会因为 `|| x` 兜底而直接显示机器值（如 MORNING）',
    ).toEqual([]);
  });

  it('映射值是人能读的时间区间，而不是机器码', () => {
    for (const [k, v] of Object.entries(timeSlotLabels)) {
      expect(v, `${k} 的文案仍是机器码`).not.toBe(k);
      expect(v).toMatch(/\d{1,2}:\d{2}\s*[-–~]\s*\d{1,2}:\d{2}/);
    }
  });
});


describe('GRID_TIME_SLOTS 是旧口径（R35 记录，不修）', () => {
  /**
   * 后端 `schedule_grid.py` 的注释已经写明：
   *
   *     # 刻意不复用前端 labels.ts 的 WEEKDAYS/GRID_TIME_SLOTS ——
   *     # 那两组是沙盘旧口径（5 天 × 3 时段），会把周末 71 条和上午 4 条排掉。
   *
   * 新视图 `ScheduleGridView` 从后端读 `data.slots`（后端定义 label/clock），
   * 已经不受它影响。仍在用它的只有 `ParallelView` / `RescheduleView` 的
   * **并行排课沙盘**——那里只排工作日三时段，属既定口径。
   *
   * 这条测试不是在说它是 bug，而是把这个已知限制钉住：
   * 将来谁把它当成"全时段"，下面的断言会提醒他先看后端注释。
   */
  it('GRID_TIME_SLOTS 只含工作日三时段，不含 MORNING/周末', () => {
    expect([...GRID_TIME_SLOTS]).toEqual(['567', '678', '91011']);
    expect((GRID_TIME_SLOTS as readonly string[]).includes('MORNING')).toBe(false);
    expect(WEEKDAYS.length).toBe(5);
  });
});


describe('WEEKDAYS_ALL 必须覆盖周末（R36）', () => {
  /**
   * 真实数据 433 条里 71 条在周末。调课沙盘此前只画 5 天，
   * 那些课既不在"可用"也不在"被排除"里 —— 从界面上消失了。
   *
   * 修复后后端 `WEEKDAYS_ALL` 覆盖七天，前端沙盘列数也要跟着 7。
   * 这条测试同时锁住"常量本身"和"沙盘用的就是这个常量"。
   */
  it('七天齐全且顺序为 周一→周日', () => {
    expect([...WEEKDAYS_ALL]).toEqual(
      ['周一', '周二', '周三', '周四', '周五', '周六', '周日'],
    );
  });

  it('调课沙盘（RescheduleView）按 WEEKDAYS_ALL 渲染，不是 5 天', () => {
    const src = readSrc('views/RescheduleView.tsx');
    expect(src).toContain('WEEKDAYS_ALL');
    expect(src).not.toContain('WEEKDAYS.map(');       // 旧 5 天常量不该再用
    expect(src).toContain('repeat(7, 1fr)');          // 列数必须跟着 7
  });
});
