"""勤怠・スタッフ台帳・給与（docs/profit/labor_design.md）。名前・時給・金額はすべて架空。"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime

import pytest
from helpers import JST, event, make_deps, slip_msg

from barnull_sales import attendance, jobs, labor_hooks, payroll
from barnull_sales.extract import Entry, Parsed
from barnull_sales.process import handle_event
from barnull_sales.staff import StaffIndex
from barnull_sales.testing import FakeLaborTracker, FakeLine

APPROVER = "UALERT"      # tests/sales.test.toml の alert_mention


def att(month, day, start, end) -> Parsed:
    return Parsed(kind="attendance", confidence=0.95, attendance={"month": month, "day": day, "start": start, "end": end})


def labor_deps(results=(), **kw):
    deps = make_deps(results, **kw)
    deps.labor = FakeLaborTracker()
    deps.line = FakeLine()
    deps.line.names = {"U1": "たろう", "U2": "はなこ", "U9": "紹介じろう"}
    return deps


def staff_posts(deps) -> list[str]:
    return [p["text"] for p in deps.notifier.posts if p["channel"] == "CSTAFF"]


def ledger(deps, **values) -> str:
    """管理者が Asana の台帳にタスクを作った（と同じ状態にする）。"""
    gid, _ = deps.labor.upsert_staff(None, values.get("real_name", ""), values)
    jobs.run_steps(deps, [("staff_sync", labor_hooks.staff_sync)])
    return gid


def rec_of(deps, day="20261123", user="U1"):
    person = StaffIndex.load(deps.store).by_line_user(user)
    return deps.store.get_doc("attendance", f"W-{day}-{person.staff_id}")


# ---------------------------------------------------------------- 受け付け
def test_attendance_across_6am_is_recorded_once():
    deps = labor_deps([att(11, 23, "21:00", "03:00")])
    ev = event("2026-11-24 03:30", "E1", user="U1")
    r = handle_event(ev, "message", deps)
    assert r["status"] == "attendance"
    rec = rec_of(deps)
    assert rec["business_date"] == "2026-11-23" and rec["hours"] == 6
    assert rec["start"].startswith("2026-11-23T21:00") and rec["end"].startswith("2026-11-24T03:00")
    texts = staff_posts(deps)
    assert any("【台帳に無い人】" in t and "たろう" in t for t in texts)
    assert any("11/23（月）　21:00〜03:00　6h" in t and "11月の稼働：1日・6h" in t for t in texts)
    tasks = [t for t in deps.labor.tasks.values() if t["project"] == "attendance"]
    assert len(tasks) == 1 and tasks[0]["hours"] == 6 and tasks[0]["att_status"] == "記録済み"
    assert [t for t in deps.labor.tasks.values() if t["project"] == "staff"][0]["line_name"] == "たろう"
    assert handle_event(ev, "message", deps)["status"] == "duplicate"


def test_hours_notation_over_24_and_bad_spans():
    deps = labor_deps([att(11, 23, "20:30", "26:15"), att(11, 23, "", "03:00"), att(11, 23, "09:00", "09:00")])
    handle_event(event("2026-11-24 03:30", "E1"), "message", deps)
    assert rec_of(deps)["hours"] == 5.75
    assert handle_event(event("2026-11-24 03:31", "E2"), "message", deps)["status"] == "attendance_unparsed"
    assert handle_event(event("2026-11-24 03:32", "E3"), "message", deps)["status"] == "attendance_unparsed"
    assert any("読み取れない" in t for t in staff_posts(deps))


def test_second_message_for_same_day_overwrites():
    deps = labor_deps([att(11, 23, "21:00", "03:00"), att(11, 23, "21:30", "03:00")])
    handle_event(event("2026-11-24 03:30", "E1"), "message", deps)
    handle_event(event("2026-11-24 12:00", "E2"), "message", deps)
    rec = rec_of(deps)
    assert rec["hours"] == 5.5 and len(rec["event_ids"]) == 2
    assert any(p["thread_ts"] and "訂正：21:00〜03:00 6h → 21:30〜03:00 5.5h" in p["text"] for p in deps.notifier.posts)
    assert len([t for t in deps.labor.tasks.values() if t["project"] == "attendance"]) == 1


# ---------------------------------------------------------------- 期限外の承認
def late(deps, eid="E1", when="2026-11-25 10:00"):
    ev = event(when, eid, user="U1")
    ev["replyToken"], ev["message"]["quoteToken"] = "R1", "Q1"
    return handle_event(ev, "message", deps)


def test_late_input_waits_for_approval_then_is_recorded_and_announced():
    deps = labor_deps([att(11, 23, "21:00", "03:00")], reply_ack=True)
    assert late(deps)["status"] == "attendance_pending"
    person = StaffIndex.load(deps.store).by_line_user("U1")
    assert deps.store.get_doc("attendance", f"W-20261123-{person.staff_id}") is None
    ask = next(p for p in deps.notifier.posts if "承認依頼（期限切れ）" in p["text"])
    assert ask["channel"] == "CSTAFF" and "<@UALERT>" in ask["text"]
    assert deps.line.replies[-1][2].startswith("11/23 の勤怠は入力の期限を過ぎている")
    deps.notifier.reacted = {ask["ts"]: {"white_check_mark": ["USOMEONE"]}}      # 桝本以外は数えない
    jobs.run_steps(deps, [("attendance_approvals", attendance.approvals)])
    assert rec_of(deps) is None
    deps.notifier.reacted = {ask["ts"]: {"white_check_mark": [APPROVER]}}
    jobs.run_steps(deps, [("attendance_approvals", attendance.approvals)])
    assert rec_of(deps)["hours"] == 6
    to, text, quote, key = deps.line.pushes[-1]
    assert to == "Cgroup" and quote == "Q1" and "承認され" in text and key
    jobs.run_steps(deps, [("attendance_approvals", attendance.approvals)])        # 二度は反映しない
    assert len(deps.line.pushes) == 1


def test_rejected_late_input_is_not_recorded():
    deps = labor_deps([att(11, 23, "21:00", "03:00")])
    late(deps)
    ask = next(p for p in deps.notifier.posts if "承認依頼" in p["text"])
    deps.notifier.reacted = {ask["ts"]: {"x": [APPROVER]}}
    jobs.run_steps(deps, [("attendance_approvals", attendance.approvals)])
    assert rec_of(deps) is None
    assert any(p["thread_ts"] == ask["ts"] and "却下" in p["text"] for p in deps.notifier.posts)


def test_deadline_is_day_plus_two_at_9am_and_future_needs_approval():
    deps = labor_deps([att(11, 23, "21:00", "03:00"), att(11, 30, "21:00", "03:00")])
    assert late(deps, "E1", "2026-11-25 08:59")["status"] == "attendance"
    assert late(deps, "E2", "2026-11-25 08:59")["status"] == "attendance_pending"     # 11/30 はまだ先


# ---------------------------------------------------------------- 台帳と売上
def test_ledger_links_line_name_and_shows_real_name_and_own_sales():
    deps = labor_deps([slip_msg(Entry(month=11, day=23, customer_name="顧客A", amount=30000)),
                       att(11, 23, "21:00", "03:00"),
                       slip_msg(Entry(month=11, day=23, customer_name="顧客B", amount=20000))])
    ledger(deps, real_name="山田花子", line_name="たろう", staff_kind="スタッフ", wage=1500, back_rate=10)
    handle_event(event("2026-11-23 23:00", "S1", user="U1"), "message", deps)        # 伝票（本人）
    handle_event(event("2026-11-24 03:30", "E1", user="U1"), "message", deps)        # 勤怠
    rec = rec_of(deps)
    assert rec["own_sales"] == 30000 and rec["wage_amount"] == 9000
    assert not any("【台帳に無い人】" in t for t in staff_posts(deps))
    assert any(t.startswith("山田花子｜11/23") for t in staff_posts(deps))
    handle_event(event("2026-11-24 03:40", "S2", user="U1"), "message", deps)        # 後から伝票が入った
    assert rec_of(deps)["own_sales"] == 50000
    task = deps.labor.tasks[rec["asana"]["task_gid"]]
    assert task["own_sales"] == 50000 and task["staff_name"] == "山田花子"


def test_promoter_flag_moves_sales_to_promoter():
    deps = labor_deps([slip_msg(Entry(month=11, day=23, customer_name="顧客A", amount=40000, promoter_name="紹介じろう"))])
    ledger(deps, real_name="山田花子", line_name="たろう", staff_kind="スタッフ", wage=1500, back_rate=10)
    ledger(deps, real_name="佐藤次郎", line_name="紹介じろう", staff_kind="プロモーター", back_rate=20)
    handle_event(event("2026-11-23 23:00", "S1", user="U1"), "message", deps)
    index = StaffIndex.load(deps.store)
    slip = deps.store.find_slips("event_id", "==", "S1")[0]
    assert index.owner_of(slip).real_name == "佐藤次郎"


def test_wage_change_applies_from_its_start_date():
    deps = labor_deps()
    gid = ledger(deps, real_name="山田花子", line_name="たろう", staff_kind="スタッフ", wage=1500)
    deps.labor.tasks[gid].update(wage=1800, wage_from="2026-11-20")
    jobs.run_steps(deps, [("staff_sync", labor_hooks.staff_sync)])
    p = StaffIndex.load(deps.store).by_name("たろう")
    assert p.wage_on("2026-11-19") == 1500 and p.wage_on("2026-11-20") == 1800
    jobs.run_steps(deps, [("staff_sync", labor_hooks.staff_sync)])                 # 同じ値なら増えない
    assert len(StaffIndex.load(deps.store).by_name("たろう").wages) == 2


def _sync(deps):
    jobs.run_steps(deps, [("staff_sync", labor_hooks.staff_sync)])
    return StaffIndex.load(deps.store).by_name("たろう")


def test_wage_change_without_new_start_date_does_not_rewrite_past():
    """適用開始日を変えずに時給だけ変えても、過去の勤務の時給は変わらない（今日から当て、台帳の日付を今日に直す）。"""
    deps = labor_deps()
    today = attendance.cycle_date(datetime.now(deps.settings.tz), deps).isoformat()
    gid = ledger(deps, real_name="山田花子", line_name="たろう", staff_kind="スタッフ", wage=1500, wage_from="2026-01-01")
    deps.labor.tasks[gid].update(wage=1800)
    p = _sync(deps)
    assert p.wage_on("2026-01-02") == 1500 and p.wage_on(today) == 1800
    assert deps.labor.tasks[gid]["wage_from"] == today
    history = list(p.wages)
    assert _sync(deps).wages == history      # 書き戻した日付を読んでも、履歴は変わらない


def test_wage_correction_with_reentered_or_future_start_date():
    deps = labor_deps()
    gid = ledger(deps, real_name="山田花子", line_name="たろう", staff_kind="スタッフ", wage=1500, wage_from="2026-01-01")
    # 適用開始日を入れ直した → その日から（過去でもよい）
    deps.labor.tasks[gid].update(wage=1600, wage_from="2026-01-10")
    p = _sync(deps)
    assert p.wage_on("2026-01-09") == 1500 and p.wage_on("2026-01-10") == 1600
    # まだ始まっていない額の訂正 → 同じ日のまま上書き（履歴は増えない）
    deps.labor.tasks[gid].update(wage=1800, wage_from="2099-12-01")
    _sync(deps)
    deps.labor.tasks[gid].update(wage=1700)
    p = _sync(deps)
    assert [w["wage"] for w in p.wages] == [1500, 1600, 1700] and p.wage_on("2099-12-01") == 1700


# ---------------------------------------------------------------- 給与
@pytest.fixture
def clock(monkeypatch):
    now = {"t": datetime(2026, 12, 2, 17, 10, tzinfo=JST)}
    monkeypatch.setattr(payroll, "_now", lambda deps: now["t"])
    return now


def payroll_docs(deps) -> dict:
    return {d["staff_name"]: d for d in deps.store.find_docs("payroll", "month", "==", "2026-11").values()}


def test_payroll_calculates_recalculates_confirms_and_alerts(clock):
    deps = labor_deps([slip_msg(Entry(month=11, day=23, customer_name="顧客A", amount=30000)),
                       att(11, 23, "21:00", "03:00"),
                       slip_msg(Entry(month=11, day=30, customer_name="顧客B", amount=10000)),
                       slip_msg(Entry(month=11, day=29, customer_name="顧客C", amount=5000))])
    ledger(deps, real_name="山田花子", line_name="たろう", staff_kind="スタッフ", wage=1500, back_rate=10)
    ledger(deps, real_name="管理者", line_name="かんりしゃ", staff_kind="管理者")
    handle_event(event("2026-11-23 23:00", "S1", user="U1"), "message", deps)
    handle_event(event("2026-11-24 03:30", "E1", user="U1"), "message", deps)

    est = payroll_docs(deps)["山田花子"]                                             # 計算日の前は「見込み」
    assert (est["status"], est["pay"]) == ("見込み", 12000)
    assert not any("計算し直しました" in t for t in staff_posts(deps))
    clock["t"] = datetime(2026, 12, 2, 16, 50, tzinfo=JST)                         # 第1水曜 17:00 の前
    assert "calculated" not in payroll.step(deps)
    clock["t"] = datetime(2026, 12, 2, 17, 10, tzinfo=JST)
    assert payroll.step(deps)["calculated"] == 1
    doc = payroll_docs(deps)["山田花子"]
    assert (doc["hours"], doc["wage_part"], doc["own_sales"], doc["back"], doc["pay"]) == (6, 9000, 30000, 3000, 12000)
    assert doc["status"] == "計算済み" and deps.labor.tasks[doc["asana"]["task_gid"]]["pay_status"] == "計算済み"
    assert "管理者" not in payroll_docs(deps)
    assert any("11月分の給与を計算しました（1人）" in t for t in staff_posts(deps))
    assert not any("12000" in t or "12,000" in t for t in staff_posts(deps))      # 額は Slack に出さない
    assert payroll.step(deps) == {"month": "2026-11"}                              # 二度は計算しない

    handle_event(event("2026-12-03 12:00", "S2", user="U1"), "message", deps)        # 月末の伝票が遅れて入った
    doc = payroll_docs(deps)["山田花子"]
    assert doc["own_sales"] == 40000 and doc["pay"] == 13000 and doc["status"] == "計算済み"
    assert any("給与を計算し直しました（売上の変化）" in t for t in staff_posts(deps))

    clock["t"] = datetime(2026, 12, 10, 9, 5, tzinfo=JST)
    assert payroll.step(deps)["confirmed"] == 1
    assert payroll_docs(deps)["山田花子"]["status"] == "確定"

    handle_event(event("2026-12-11 12:00", "S3", user="U1"), "message", deps)        # 確定後の変化
    doc = payroll_docs(deps)["山田花子"]
    assert doc["pay"] == 13000
    assert any("【確定後の変更】山田花子さんの11月の売上" in t for t in staff_posts(deps))

    task = deps.labor.tasks[doc["asana"]["task_gid"]]                               # 管理者の調整
    task.update(adjustment=-500, adjust_reason="立替の精算")
    jobs.run_steps(deps, [("payroll_adjustments", payroll.adjustments)])
    doc = payroll_docs(deps)["山田花子"]
    assert doc["pay"] == 12500 and deps.labor.tasks[doc["asana"]["task_gid"]]["pay"] == 12500


def test_attendance_for_confirmed_month_is_refused(clock):
    deps = labor_deps([att(11, 23, "21:00", "03:00")])
    deps.store.set_doc("payroll_runs", "2026-11", {"calculated_at": "x", "confirmed_at": "y"})
    assert late(deps, when="2026-12-11 10:00")["status"] == "attendance_month_closed"


def test_payroll_skips_months_before_payroll_since(clock):
    deps = labor_deps()
    deps.settings = replace(deps.settings, labor=replace(deps.settings.labor, payroll_since="2026-12"))
    assert payroll.step(deps)["skipped"]


def test_first_wednesday():
    assert payroll.first_weekday(2026, 12, 2).isoformat() == "2026-12-02"
    assert payroll.first_weekday(2027, 9, 2).isoformat() == "2027-09-01"      # 1日が水曜


def test_asana_override_changes_hours_and_void():
    deps = labor_deps([att(11, 23, "21:00", "03:00")])
    handle_event(event("2026-11-24 03:30", "E1"), "message", deps)
    gid = rec_of(deps)["asana"]["task_gid"]
    deps.labor.tasks[gid].update(att_manual="時間を反映", start="22:00", end="03:00")
    jobs.run_steps(deps, [("attendance_overrides", attendance.overrides)])
    assert rec_of(deps)["hours"] == 5 and deps.labor.tasks[gid]["att_manual"] == "なし"
    deps.labor.tasks[gid].update(att_manual="対象外")
    jobs.run_steps(deps, [("attendance_overrides", attendance.overrides)])
    assert rec_of(deps)["status"] == "対象外" and deps.labor.tasks[gid]["att_status"] == "対象外"


def test_asana_failure_is_retried_by_resync():
    deps = labor_deps([att(11, 23, "21:00", "03:00")])
    deps.labor.fail = {"attendance"}
    handle_event(event("2026-11-24 03:30", "E1"), "message", deps)
    assert rec_of(deps)["sync_pending"]
    deps.labor.fail = set()
    assert jobs.run_steps(deps, [("attendance_resync", attendance.resync)])
    assert not rec_of(deps)["sync_pending"] and rec_of(deps)["asana"]["task_gid"]
