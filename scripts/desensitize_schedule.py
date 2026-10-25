"""排课表脱敏（发布前用）——把手机号打码，别让真实联系方式随包上公网

为什么需要
----------
`sites` 发布器把**整个目录**上传，且**不读 .gitignore**。仓库根的
`schedule.xlsx` 除了 433 条排课记录，还带一张「教师联系表」（17 个手机号）。
这些号码是真实教师的，不该出现在公开链接背后的源码包里。

用法（发布前）
--------------
    python scripts/desensitize_schedule.py            # 只检查，报告会命中几个号码
    python scripts/desensitize_schedule.py --write    # 打码写入（先自行备份！）

打码规则：11 位手机号 → 前 3 + **** + 后 4（如 138****5678）。
只改数字串，不动表结构 —— 解析器按列名取值，打码后仍能正常加载。
"""

import argparse
import os
import re
import shutil
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
XLSX = os.path.join(REPO, "schedule.xlsx")
PHONE = re.compile(r"(?<!\d)(1[3-9]\d)\d{4}(\d{4})(?!\d)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true", help="写入打码结果（默认只检查）")
    ap.add_argument("--file", default=XLSX)
    args = ap.parse_args()

    import openpyxl

    if not os.path.exists(args.file):
        print(f"❌ 找不到 {args.file}")
        return 1

    wb = openpyxl.load_workbook(args.file)
    hits, cells = 0, []
    for ws in wb.worksheets:
        for row in ws.iter_rows():
            for c in row:
                if isinstance(c.value, str) and PHONE.search(c.value):
                    hits += len(PHONE.findall(c.value))
                    cells.append((ws.title, c.coordinate, c.value))
                elif isinstance(c.value, int) and PHONE.match(str(c.value)):
                    hits += 1
                    cells.append((ws.title, c.coordinate, str(c.value)))

    print(f"扫描 {len(wb.worksheets)} 个工作表：命中 {hits} 个手机号，分布在 {len(cells)} 个单元格")
    for sheet, coord, val in cells[:5]:
        print(f"  · {sheet}!{coord} = {str(val)[:14]}…")

    if not args.write:
        print("\n（只检查模式；加 --write 打码写入）")
        return 0 if hits == 0 else 2

    backup = args.file + ".bak"
    shutil.copy2(args.file, backup)
    for sheet, coord, _ in cells:
        ws = wb[sheet]
        orig = ws[coord].value
        ws[coord] = PHONE.sub(r"\1****\2", str(orig))
    wb.save(args.file)
    print(f"\n✅ 已打码写入 {args.file}（原件备份 {os.path.basename(backup)}）")

    # 复核：重新读一遍，确认没有漏网
    wb2 = openpyxl.load_workbook(args.file)
    left = 0
    for ws in wb2.worksheets:
        for row in ws.iter_rows():
            for c in row:
                if isinstance(c.value, str) and PHONE.search(c.value):
                    left += len(PHONE.findall(c.value))
    print(f"复核：剩余明文手机号 {left} 个")
    return 0 if left == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
