"""审批闭环的单测：`_apply_reschedule` + `/apply-move`。

为什么单独立一个文件：
  这条写路径此前**零覆盖** —— 现有测试全在验"沙盘算得对不对"，
  没有任何一条验"教秘点完同意之后，课表到底有没有变"。
  而这正是最容易静默失败的地方：审批台显示"已同意"，课表一个字节没动，
  页面不会报错，用户不会怀疑。

本文件只用小的内存表构造场景（不读真实 xlsx），断言的是**数据本身**：
周次有没有变、变了哪几条、冲突时是不是如实跳过、重复调用是不是幂等。
"""

import pytest

from app import security as _security
from app.utils.excel_parser import ScheduleDatabase, ScheduleEntry

from app.api import reschedule as R


TEACHER = "吴琳"
COURSE = "扭摆法测物体转动惯量"
CLASS = "2024级应用化学1、2班"
ORIGINAL = "周二 13:30-15:45"


@pytest.fixture
def db(tmp_path, monkeypatch):
    """一张 3 条记录的小表 + 一个临时 SQLite（绝不碰真实的 requests.db）。"""
    d = ScheduleDatabase()
    d.entries = [
        # 吴琳 同一门课连排三周（第 3/4/5 周）
        ScheduleEntry(3, "周二", "567", CLASS, "24应化2_1", COURSE, "A504", TEACHER),
        ScheduleEntry(4, "周二", "567", CLASS, "24应化2_1", COURSE, "A504", TEACHER),
        ScheduleEntry(5, "周二", "567", CLASS, "24应化2_1", COURSE, "A504", TEACHER),
        # 第 9 周同一间教室被别人占了 —— 移到第 9 周必须被跳过而不是硬塞
        ScheduleEntry(9, "周二", "567", "别的班", "别班_1", "声速测量", "A504", "陈老师"),
    ]
    d.loaded = True
    monkeypatch.setattr(R, "_db", d)
    monkeypatch.setattr(R, "_DB_PATH", str(tmp_path / "t.db"))
    R._init_db()
    return d


def _record(target_week: int, **kw) -> dict:
    rec = {
        "teacher": TEACHER,
        "course_name": COURSE,
        "class_name": CLASS,
        "original_time": ORIGINAL,
        "target_week": target_week,
        "source_week": 0,
    }
    rec.update(kw)
    return rec


def _weeks(db, teacher=TEACHER):
    return sorted(e.week for e in db.entries if e.teacher == teacher)


# ---------- 1. 应用真的会改数据 ----------

def test_apply_moves_the_record_to_target_week(db):
    """核心承诺：审批通过 → 排课记录真的换周。"""
    before = _weeks(db)
    r = R._apply_reschedule(_record(7), persist=False)

    assert r["applied"] == 1, r
    # 源周 = 目标周之前最近的一周 = 第 5 周，所以是它被搬到第 7 周
    assert _weeks(db) == [3, 4, 7], f"期望第5周被移到第7周，实际 {_weeks(db)}（原来是 {before}）"


def test_apply_picks_source_week_before_target(db):
    """源周 = 目标周之前**最近**的匹配周，而不是最早的那条。

    否则"调到第7周"会顺手把第3周也搬走 —— 教秘只批了一次，却改了两周的课。
    """
    R._apply_reschedule(_record(7), persist=False)

    assert _weeks(db) == [3, 4, 7], f"只该动第5周（最近的），实际 {_weeks(db)} 全动了"


def test_apply_honours_explicit_source_week(db):
    """带 source_week 时按它移动，不再自动挑最近周。"""
    r = R._apply_reschedule(_record(7, source_week=3), persist=False)

    assert r["applied"] == 1, r
    assert _weeks(db) == [4, 5, 7], f"被移动的应当是第3周，实际 {_weeks(db)}"


# ---------- 2. 冲突必须如实跳过 ----------

def test_conflict_is_skipped_not_forced(db):
    """目标周该教室已被占 → 跳过并说明，绝不硬塞出双占。"""
    r = R._apply_reschedule(_record(9), persist=False)

    assert r["applied"] == 0, r
    assert r["skipped"] == 1, r
    # 2026-10-22 冲突分级后文案从笼统的「已有安排」升级为「撞上什么 + 怎么改」，
    # 这里断言的是**分级本身**（有 kind / 有处理建议），不再锚死旧措辞。
    assert any("核心冲突" in d for d in r["details"]), r
    assert r["conflicts"] and r["conflicts"][0]["kind"] == "room", r
    assert "换一间教室" in r["conflicts"][0]["fix_hint"], r
    # 第 5 周那条原地不动（源周是第5周，它没被移走）
    assert _weeks(db) == [3, 4, 5], f"冲突时不该动任何记录，实际 {_weeks(db)}"


def test_no_match_says_so_instead_of_pretending(db):
    """匹配不到记录时要明说，不能返回一个"成功但什么也没发生"。"""
    r = R._apply_reschedule(_record(7, teacher="查无此人"), persist=False)

    assert r["applied"] == 0 and r["skipped"] == 0, r
    assert any("未找到" in d for d in r["details"]), r


# ---------- 3. 幂等 ----------

def test_same_key_applies_only_once(db):
    """同一张工单重复调用：第二次必须明确说"已应用过"，且不再动数据。"""
    rec = _record(7)
    first = R._apply_reschedule(rec, persist=True, idem_key="cloud-42")
    assert first["applied"] == 1, first
    weeks_after_first = _weeks(db)

    second = R._apply_reschedule(rec, persist=True, idem_key="cloud-42")
    assert second["applied"] == 0, f"重复应用又移动了记录：{second}"
    assert any("应用过" in d for d in second["details"]), second
    assert _weeks(db) == weeks_after_first


def test_different_keys_are_independent(db):
    """幂等键是"每张工单一次"，不是"每门课一次"——两张不同工单各自生效。"""
    R._apply_reschedule(_record(7), persist=True, idem_key="cloud-1")
    R._apply_reschedule(_record(8), persist=True, idem_key="cloud-2")

    # 第一次移走第5周（→7），第二次移走第7周（→8），剩下 3、4
    assert _weeks(db) == [3, 4, 8], f"两张工单应各移一条，实际 {_weeks(db)}"


def test_idem_key_is_persisted(db):
    """键必须落库：只在内存里判重的话，进程一重启就失效了。"""
    R._apply_reschedule(_record(7), persist=True, idem_key="cloud-42")

    conn = R._get_db()
    try:
        rows = conn.execute("SELECT idem_key FROM applied_moves").fetchall()
    finally:
        conn.close()
    assert [r["idem_key"] for r in rows] == ["cloud-42"]


# ---------- 4. HTTP 层：写接口不能匿名可调 ----------

def test_apply_move_endpoint_requires_admin(db):
    """`/apply-move` 会改排课数据，必须挂管理员门禁。"""
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app) as client:
        resp = client.post("/api/reschedule/apply-move", json={
            "teacher": TEACHER, "course_name": COURSE,
            "original_time": ORIGINAL, "target_week": 7, "key": "cloud-x",
        })

    assert resp.status_code in (401, 403), f"匿名调用居然放行了：HTTP {resp.status_code}"


def test_apply_move_endpoint_moves_data_with_token(db):
    """带令牌的真实调用路径：能移动，且返回里说清楚移动了几条。"""
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app) as client:
        login = client.post("/api/reschedule/admin-login",
                            json={"username": _security.ADMIN_USER, "password": _security.ADMIN_PASS})
        assert login.status_code == 200, login.text
        token = login.json()["token"]

        resp = client.post("/api/reschedule/apply-move", headers={"Authorization": f"Bearer {token}"},
                           json={
                               "teacher": TEACHER, "course_name": COURSE, "class_name": CLASS,
                               "original_time": ORIGINAL, "target_week": 7, "key": "cloud-7",
                           })

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["data"]["applied"] == 1, body
    assert "已移动 1 条" in body["message"], body["message"]
    assert _weeks(db) == [3, 4, 7]


# ---------- 3b. 幂等的**并发**路径（R-115） ----------
#
# 为什么单独补这一组（2026-11-07）：
# `_init_db` 里已经记着一段实测 —— 查重与写入分在两个连接、两次 `execute` 之间
# 没有互斥，8 线程加 barrier 同时进，同一 idem_key **落库 8 行（期望 1）**；
# 放宽到 20 轮 × 6 线程仍有 1/20 轮重复。当时的修法是给 idem_key 加**部分唯一索引**
# （`WHERE idem_key != ''`），并把 IntegrityError 翻译成"已由并发请求应用过"。
#
# 但上面那三条幂等用例全是**串行**的：第一次成功、第二次被"先查"拦住。
# 也就是说，"先查"这条快速路径被锁住了，而**真正兜底的那道唯一索引没有任何测试**。
# 危险在于：下次有人调整 `_apply_reschedule` 里的锁边界、或把唯一索引改成普通索引
# （看起来更"兼容"），串行用例照样全绿，而并发重复会静默回来 ——
# 污染账本（审计轨迹、撤销依据、`/demo/reset` 计数），且只在真实并发下出现。
#
# 这组用例按创建索引前的那次实测**原样复现**：8 线程、barrier 对齐、同一 idem_key。

def test_concurrent_same_key_writes_one_row(db):
    """8 线程同时应用同一个 idem_key：账本必须只有 **1 行**。

    判据是**账本行数**，不是返回值。返回值可以撒谎（例如都返回成功），
    而账本是撤销与审计的唯一依据 —— 多一行就多一次"撤销"。

    与其他两条的分工（三条不是重复覆盖）：
      · 本条 —— 按**端点真实调用方式**（含 `_DB_WRITE_LOCK`）跑，验"整条链路"；
      · `test_unique_index_alone_blocks_duplicate_rows` —— **故意绕过锁**，
        单独验数据库那道兜底（把 UNIQUE 改成普通索引会让它红，而本条仍绿）；
      · `test_empty_key_is_not_deduplicated` —— 验部分索引的 `WHERE idem_key != ''`
        条件（写成全覆盖唯一会让"不带键的新调课"被误判为重复）。
    三条各守一层，缺哪条都会留下一个"改坏了也不红"的角落。
    """
    import threading

    rec = _record(7)
    n = 8
    barrier = threading.Barrier(n)
    results = []
    errors = []

    def work():
        try:
            barrier.wait(timeout=10)      # 尽量让 8 个线程同时进入临界区
            # ⚠️ 必须按**真实调用方式**加锁：`/apply-move` 端点是在
            # `with _DB_WRITE_LOCK` 里调它的（见 `_locked_apply`），
            # 审批路径同理（`_locked_apply_approve`）。直接裸调函数测出来的"竞态"
            # 在线上不可达 —— 第一版本测试就是这么写的，8 线程全裸调，
            # 结果看到"误报冲突"，那是测试绕开了锁，不是产品缺陷。
            with R._DB_WRITE_LOCK:
                results.append(R._apply_reschedule(rec, persist=True, idem_key="cloud-concurrent"))
        except Exception as exc:
            errors.append(repr(exc))

    threads = [threading.Thread(target=work) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors, f"并发调用抛了异常（不应把并发当故障报给上层）：{errors}"

    conn = R._get_db()
    try:
        rows = conn.execute(
            "SELECT COUNT(*) AS c FROM applied_moves WHERE idem_key = ?",
            ("cloud-concurrent",),
        ).fetchone()
    finally:
        conn.close()
    assert rows["c"] == 1, (
        f"同一 idem_key 落库 {rows['c']} 行（期望 1）—— 唯一索引没生效或已被削弱"
    )

    # 返回语义：恰好一个说"应用了"，其余说"已应用过"，且**没有一个**报错
    applied = sum(1 for r in results if r.get("applied"))
    assert applied <= 1, f"有 {applied} 个请求都声称应用成功：{results}"
    for r in results:
        if not r.get("applied"):
            assert any("应用过" in d for d in r.get("details") or []), (
                f"未应用的那次没说清原因，教秘会以为审批坏了：{r}"
            )


def test_concurrent_same_key_leaves_schedule_idempotent(db):
    """并发下**课表状态**也必须等价于"应用一次"。

    `_apply_reschedule` 的注释断言过："内存里这次移动已经做了（幂等地把该记录的
    week 设成目标周，与赢家相同）"。这条就是在验证那句注释 ——
    若它不成立（比如某次把 week 累加而非赋值），并发下会把课挪到错误的周。
    """
    import threading

    rec = _record(7)
    n = 6
    barrier = threading.Barrier(n)

    def work():
        barrier.wait(timeout=10)
        with R._DB_WRITE_LOCK:      # 与端点一致，见上一条用例的说明
            R._apply_reschedule(rec, persist=True, idem_key="cloud-concurrent-week")

    threads = [threading.Thread(target=work) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    # 原表 3/4/5 周，把第 5 周那条（源周自动取"目标周之前最近的匹配周"=5）移到第 7 周。
    # 无论并发多少次，结果都必须是 3/4/7 —— 不是 3/4/8、不是 3/4/9（累加），
    # 也不是两条都留下来。
    assert _weeks(db) == [3, 4, 7], (
        f"并发后课表状态不对：{_weeks(db)}（期望 [3,4,7]，多次应用必须等价于一次）"
    )


def test_empty_key_is_not_deduplicated(db):
    """空 idem_key 不参与去重（历史行与"本来就没给键"的调用都存 ''）。

    这条守的是部分唯一索引的 `WHERE idem_key != ''` 条件：
    若有人把它写成全覆盖唯一索引，第二次"不带键"的调用会撞约束、返回
    "已应用过"，而实际上它是一次**全新的**调课 —— 教秘会看到"没生效"。
    """
    R._apply_reschedule(_record(7), persist=True, idem_key="")
    # 换一条记录再应用一次，仍不带键：必须真的生效
    R._apply_reschedule(_record(8), persist=True, idem_key="")

    conn = R._get_db()
    try:
        rows = conn.execute("SELECT COUNT(*) AS c FROM applied_moves").fetchone()
    finally:
        conn.close()
    assert rows["c"] == 2, f"空键被错误去重，应有 2 行：{rows['c']}"
    assert _weeks(db) == [3, 4, 8], _weeks(db)


def test_unique_index_alone_blocks_duplicate_rows(db):
    """← 直接验**唯一索引本身**，不经业务锁。

    为什么要绕过业务锁单独测（这不是重复覆盖）：
    上面两条并发用例在 `with _DB_WRITE_LOCK` 里跑 —— 那测的是"锁生效"。
    而 `_init_db` 里的注释记着另一件事：**当时根本没有这把锁**，
    8 线程同时 `INSERT` 让同一 idem_key 落库 **8 行**，靠的正是后来加的
    **部分唯一索引**（`WHERE idem_key != ''`）才收敛到 1 行。

    如果哪天有人把索引从 UNIQUE 改成普通索引（"兼容性更好"），
    上面两条用例仍会全绿（锁还在，串行化了），而这道兜底已经消失 ——
    一旦有人绕过锁（或在锁外直接写库、或未来加了第二个写入口），
    账本就会重新出现重复行，而没有任何测试会红。

    所以这里**故意不用业务锁**，8 个线程直接 INSERT 同一个键，
    断言数据库自己把重复挡下来。
    """
    import sqlite3
    import threading

    n = 8
    barrier = threading.Barrier(n)
    ok = []
    violated = []

    def insert():
        conn = sqlite3.connect(R._DB_PATH, timeout=10)
        try:
            barrier.wait(timeout=10)
            conn.execute(
                "INSERT INTO applied_moves (request_data, applied_at, idem_key) "
                "VALUES (?, ?, ?)",
                ('{"probe": 1}', "2026-11-07T00:00:00.000000", "probe-dup"),
            )
            conn.commit()
            ok.append(1)
        except sqlite3.IntegrityError:
            violated.append(1)
            conn.rollback()
        finally:
            conn.close()

    threads = [threading.Thread(target=insert) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    conn = R._get_db()
    try:
        c = conn.execute(
            "SELECT COUNT(*) AS c FROM applied_moves WHERE idem_key = 'probe-dup'"
        ).fetchone()["c"]
    finally:
        conn.close()

    assert c == 1, (
        f"唯一索引没挡住重复：落库 {c} 行（期望 1）。"
        f"成功的 {len(ok)} 次 / 被唯一约束挡下 {len(violated)} 次"
    )
    assert len(violated) == n - 1, (
        f"应有 {n-1} 次被 IntegrityError 挡下，实际 {len(violated)} 次 —— "
        "说明索引可能不是 UNIQUE，或者被改成了普通索引"
    )
