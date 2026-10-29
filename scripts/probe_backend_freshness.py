"""部署新鲜度探测：线上跑的后端，到底是不是仓库里这份。

为什么需要（R31）
----------------
R30 发现：前端产物 hash 能比对，但**后端没有任何可比对的指纹**。
`preflight_release.py` 只比 `index-*.js`，于是「前端已更新、后端仍是旧版」
这种状态完全看不出来。我当时的实测是：

    线上 /chat → {"course_unrecognized": null, ...}

`course_unrecognized` 是 R21 才加的字段，线上返回 null = 字段不存在
= 后端仍是旧代码。这个事实**必须靠逐个接口试探**才能发现，
而每次都要人去想"哪个字段是新的"。

这里把"后端是否新鲜"变成一次可重复的调用：
选若干个**带版本标记的行为特征**（每个都对应某轮修复），
向线上打真实请求，逐条核对。任一不符 → 后端落后。

设计原则
--------
1. **只读**：全部用 GET / 或 fast=true 的查询，不写任何状态。
2. **特征必须可证伪**：断言的是"这个字段/这行为存在"，而不是"看起来正常"。
3. **失败要能定位**：报告指出是哪一轮的修复没上线，而不是笼统的"版本不符"。
4. **离线可用**：打不到线上时返回 unknown，不误判为"落后"。

用法
----
    python scripts/probe_backend_freshness.py
    python scripts/probe_backend_freshness.py --base https://other.host

退出码：0 = 全部新鲜 / 1 = 有特征不符（后端落后）/ 2 = 打不到线上（未知）
"""

import argparse
import json
import sys
import urllib.error
import urllib.request

DEFAULT_BASE = "https://physics-scheduler.app.workbuddy.host"
UA = {"User-Agent": "preflight-freshness", "Content-Type": "application/json"}


def _get(base, path, timeout=20):
    req = urllib.request.Request(base + path, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _post(base, path, body, timeout=40):
    req = urllib.request.Request(
        base + path, data=json.dumps(body).encode("utf-8"),
        headers=UA, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


# ---------------------------------------------------------------------------
# 特征清单：每一条 = (轮次, 说明, 检测函数)
#
# 检测函数返回 (ok, detail)。ok=False 表示"该轮修复不在线上"。
# 注意：这里的特征是**行为**，不是"接口存在"——接口存在不代表是新的。
# ---------------------------------------------------------------------------


def _feat_course_unrecognized(base):
    """R21：课程名未识别时，返回体带 course_unrecognized 字段。

    用一句**故意用简称**的话去问：全名是「PN结正向压降温度特性实验」，
    「PN结」匹配不到 → 应触发该字段。
    """
    d = _post(base, "/api/reschedule/chat",
              {"message": "我是教师H，把PN结调到第3周", "fast": True})
    data = d.get("data") or {}
    if "course_unrecognized" not in data:
        return False, "返回体没有 course_unrecognized 字段（R21 未上线）"
    return True, f"course_unrecognized={data.get('course_unrecognized')}"


def _feat_this_week_relative(base):
    """R23：「第6周的课上不了，这周调」应落到第 5 周，而不是原地第 6 周。

    这是纯规则解析，fast=true 就够（不走 LLM）。
    """
    d = _post(base, "/api/reschedule/chat",
              {"message": "我是吴琳，第6周的声速测量上不了，这周先调一下",
               "fast": True})
    data = d.get("data") or {}
    tw = data.get("target_week")
    ctx = data.get("context") or {}
    src = ctx.get("source_week")
    if src == 6 and tw == 5:
        return True, "第6周→第5周，正确"
    if src == 6 and tw == 6:
        return False, "目标周 = 源周 = 6（R23 未上线：原地打转）"
    # 源周识别到 6、却没算出目标周 —— 这在修复后不该发生：
    # 「这周」分支一定会给出 target = 5（或越界时明确拒绝）。
    # 早期把这里当"跳过"是错的：它会掩盖「R23 分支整个不存在」。
    if src == 6 and tw is None:
        return False, f"源周=6 但目标周为 None —— 「这周」分支未生效（R23 未上线）"
    return False, f"未按预期解析（target={tw}, source={src}）——与 R23 行为不符"


def _feat_class_longest_match(base):
    """R22：班名长名优先。线上数据里没有前缀对，无法从外部验证行为，
    只能验证接口仍可用（低置信度，标 known-gap）。
    """
    return None, "线上数据无前缀班级对，无法从外部验证（已由单测覆盖）"


FEATURES = [
    ("R21", "课程名未识别 → course_unrecognized 字段", _feat_course_unrecognized),
    ("R23", "「第6周…这周调」→ 第5周（非原地）", _feat_this_week_relative),
    ("R22", "班级名长名优先", _feat_class_longest_match),
]


def main(argv=None):
    ap = argparse.ArgumentParser(description="探测线上后端是否为仓库最新版")
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--json", action="store_true", help="输出机读结果")
    args = ap.parse_args(argv)
    base = args.base.rstrip("/")

    try:
        _get(base, "/health", timeout=10)
    except Exception as e:  # noqa: BLE001
        print(f"打不到线上（{base}）：{e}")
        return 2

    # 先触发懒加载：`/chat` 不会自己去加载排课表，冷启动后会回
    # 「排课数据尚未加载」—— 那样所有特征都会被误判成"未上线"。
    # `/teachers` 走 `_ensure_loaded_async()`，是官方推荐的自愈入口。
    try:
        _get(base, "/api/reschedule/teachers", timeout=120)
    except Exception as e:  # noqa: BLE001
        print(f"预热加载失败（{base}）：{e}")
        return 2

    rows, stale = [], False
    for round_name, desc, fn in FEATURES:
        try:
            ok, detail = fn(base)
        except urllib.error.HTTPError as e:
            ok, detail = False, f"HTTP {e.code}：{e.read()[:120].decode('utf-8', 'replace')}"
        except Exception as e:  # noqa: BLE001
            ok, detail = None, f"探测异常：{e}"
        if ok is False:
            stale = True
        rows.append({"round": round_name, "feature": desc, "ok": ok, "detail": detail})

    if args.json:
        print(json.dumps({"base": base, "stale": stale, "rows": rows},
                         ensure_ascii=False, indent=2))
    else:
        print(f"线上后端新鲜度：{base}\n")
        for r in rows:
            mark = {True: "✅", False: "❌", None: "·"}[r["ok"]]
            print(f" {mark} [{r['round']}] {r['feature']}")
            print(f"      {r['detail']}")
        print()
        print("落后 —— 有轮次的修复没上线" if stale else "新鲜 —— 仓库里的行为特征线上都有")

    return 1 if stale else 0


if __name__ == "__main__":
    sys.exit(main())
