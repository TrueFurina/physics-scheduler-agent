"""主数据落盘持久化（JSON 文件）。

为什么需要
----------
主数据（五张实体表 + 上传的灰箱清单）此前是**纯内存态**：教秘在灰箱场景补录的
教师/场地、上传的大纲清单，进程重启（线上沙箱回收/本地重启）后全部丢失，
回退到安排表反推基线 —— "灰箱闭环上唯一的实质缺口"。

设计（与项目哲学一致：不引入第二套持久化引擎）
----------------------------------------------
- 单个 JSON 文件 `backend/app/data/master_store.json`（该目录整目录 gitignore，
  与 requests.db / parse_cache 同级）；
- 文件头带 **schedule_fingerprint**（安排表路径+mtime）：安排表换了 → 落盘的
  主数据作废，回退到新表的反推基线 —— 安排表永远是主数据的权威来源，落盘只
  保存"教秘在基线之上做的会话内修改"；
- 原子写（临时文件 + os.replace），坏文件 fail-closed（回退反推基线，不崩）。
"""

import json
import os
import tempfile
from typing import List, Optional

STORE_PATH = os.path.join(os.path.dirname(__file__), "data", "master_store.json")


def schedule_fingerprint(schedule_path: Optional[str]) -> Optional[str]:
    """安排表的指纹（路径+mtime）。上传新表 / 表文件被替换 → 指纹变化。"""
    if not schedule_path or not os.path.exists(schedule_path):
        return None
    try:
        st = os.stat(schedule_path)
        return f"{os.path.abspath(schedule_path)}::{st.st_mtime_ns}"
    except OSError:
        return None


def save(payload: dict, store_path: Optional[str] = None) -> bool:
    """原子写。失败不抛（落盘是尽力而为的自愈层，内存态才是权威）。"""
    store_path = store_path or STORE_PATH
    try:
        os.makedirs(os.path.dirname(store_path), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(store_path), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
            os.replace(tmp, store_path)
            return True
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
    except (OSError, TypeError, ValueError) as e:
        print(f"⚠️ 主数据落盘失败（不阻塞排课，重启后回退反推基线）：{e}")
        return False


def load(store_path: Optional[str] = None) -> Optional[dict]:
    """读落盘文件。不存在/损坏返回 None（调用方回退反推基线）。"""
    store_path = store_path or STORE_PATH
    if not os.path.exists(store_path):
        return None
    try:
        with open(store_path, encoding="utf-8") as f:
            d = json.load(f)
        if not isinstance(d, dict) or "master" not in d:
            return None
        return d
    except (OSError, ValueError) as e:
        print(f"⚠️ 主数据落盘文件读取失败（回退反推基线）：{e}")
        return None


def clear(store_path: Optional[str] = None) -> None:
    """删除落盘文件（测试用 / 换新表时主动清理）。"""
    store_path = store_path or STORE_PATH
    try:
        if os.path.exists(store_path):
            os.remove(store_path)
    except OSError:
        pass


def tasks_to_rows(tasks: List) -> List[dict]:
    """PendingTask 列表 → 可序列化 rows（含补全来源标记）。"""
    rows = []
    for t in tasks:
        rows.append({
            "class_name": t.class_name, "experiment": t.experiment,
            "group": t.group, "teacher": t.teacher, "room": t.room,
            "course": getattr(t, "course", "") or "",
            "week": getattr(t, "week", None),
            "weekday": getattr(t, "weekday", "") or "",
            "source": getattr(t, "source", "master") or "master",
            "teacher_enriched": bool(getattr(t, "_teacher_enriched", False)),
            "room_enriched": bool(getattr(t, "_room_enriched", False)),
        })
    return rows


def rows_to_tasks(rows: List[dict]) -> List:
    """rows → PendingTask 列表（恢复补全来源标记，假设报告不丢）。"""
    from app.utils.excel_parser import PendingTask

    tasks = []
    for r in rows or []:
        t = PendingTask(
            class_name=r.get("class_name") or "",
            experiment=r.get("experiment") or "",
            group=r.get("group") or "",
            teacher=r.get("teacher") or "",
            room=r.get("room") or "",
            course=r.get("course") or "",
            week=r.get("week"),
            weekday=r.get("weekday") or "",
            source=r.get("source") or "master",
        )
        if r.get("teacher_enriched"):
            t._teacher_enriched = True
        if r.get("room_enriched"):
            t._room_enriched = True
        tasks.append(t)
    return tasks
