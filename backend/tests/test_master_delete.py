"""删除「默认课程」是个空操作（R39）。

缺陷
----
`master_data.py` 里：

    DEFAULT_COURSE = "大学物理实验"

    def delete_course(self, name):
        \"\"\"删课程：其下实验项目归回默认课程（不删项目，只改归属）。\"\"\"
        for ex in self.experiments.values():
            if ex.get("course") == name:
                ex["course"] = DEFAULT_COURSE        # ← 归属改到「大学物理实验」
        self.courses.pop(name, None)
        self._rebuild_courses()                      # ← 又从实验聚合回来

当前真实数据只有一门课程，而它**恰好就是** `DEFAULT_COURSE`：

    删除前：课程 1，实验 25
    删除：  success = True
    删除后：课程 1，实验 25          ← 一个都没少

调用方拿到 `success: True`，界面上课程仍在。**删除没有发生，却报了成功。**

这与项目反复踩的「静默不实」同族：接口说做了一件事，实际没做，
而且没有任何错误可看 —— 教秘只会觉得"点了没反应"。

更麻烦的是这不是"默认课程不能删"这种简单规则：
`delete_course` 的语义是"把实验归回默认课程再删课程本身"，
但当**要删的就是默认课程**时，这个语义自我抵消了。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.master_data import DEFAULT_COURSE, MasterData
from app.utils.excel_parser import ScheduleDatabase

REAL = os.path.join(os.path.dirname(__file__), "..", "..", "schedule.demo.xlsx")


@pytest.fixture(scope="module")
def baseline():
    if not os.path.isfile(REAL):
        pytest.skip("缺少 schedule.demo.xlsx")
    db = ScheduleDatabase()
    db.load_from_excel(REAL)
    md = MasterData()
    md.build_from_entries(db.entries)
    return md


def test_delete_default_course_does_not_silently_succeed(baseline):
    """删默认课程必须给出可理解的拒绝，而不是返回成功却什么都没变。"""
    md = MasterData()
    md.from_dict(baseline.to_dict())

    before_courses = set(md.courses)
    before_experiments = dict(md.experiments)
    assert DEFAULT_COURSE in before_courses, "测试前提：默认课程存在"

    try:
        md.delete_course(DEFAULT_COURSE)
    except ValueError:
        # 明确拒绝 —— 这是可接受的行为
        assert set(md.courses) == before_courses, "拒绝时不该改动数据"
        return

    # 没抛异常就必须真的删掉了
    assert DEFAULT_COURSE not in md.courses, (
        "delete_course 返回了（等于 success），但默认课程仍在 courses 里 —— "
        "删除是空操作，调用方会以为成功了"
    )
    del before_experiments


def test_delete_normal_course_still_works(baseline):
    """非默认课程：实验归回默认课程，课程本身消失（原语义必须保住）。"""
    md = MasterData()
    md.from_dict(baseline.to_dict())
    md.upsert_course("临时课程")
    md.upsert_experiment("仅用于测试的实验", course="临时课程")
    assert "临时课程" in md.courses

    md.delete_course("临时课程")
    assert "临时课程" not in md.courses
    assert md.experiments["仅用于测试的实验"]["course"] == DEFAULT_COURSE
    # 实验本身不能被跟着删掉
    assert "仅用于测试的实验" in md.experiments


def test_api_delete_default_course_returns_400_not_500(monkeypatch):
    """API 层必须把它转成 400（可照做的提示），而不是 500（看起来像系统坏了）。"""
    import app.api.reschedule as api
    from app.security import issue_admin_token

    monkeypatch.setattr(api._db, "loaded", True, raising=False)
    monkeypatch.setattr(api._master_data, "initialized", False, raising=False)
    api._ensure_master_data()

    from fastapi.testclient import TestClient
    from app.main import app

    c = TestClient(app)
    c.get("/api/reschedule/teachers")
    tok = issue_admin_token("admin")["token"]
    r = c.request("DELETE",
                  "/api/reschedule/master/courses/" + DEFAULT_COURSE,
                  headers={"Authorization": "Bearer " + tok})
    assert r.status_code == 400, (
        f"删默认课程返回 {r.status_code}，应为 400 —— "
        + ("500 会让教秘以为系统坏了" if r.status_code == 500 else "")
    )
    detail = r.json().get("detail", "")
    assert "默认课程" in detail or "不能删" in detail, f"提示不可照做：{detail}"
