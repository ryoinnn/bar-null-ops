"""引用して送り直した伝票・勤怠は、引用元の送信者を担当にする（2026-11-15 本人）。名前はすべて架空。"""

from __future__ import annotations

from helpers import approve_all, event, make_deps, slip_msg, staff_texts, threads

from barnull_sales.extract import Entry, Parsed
from barnull_sales.process import handle_event
from barnull_sales.staff import StaffIndex
from barnull_sales.testing import FakeLaborTracker, FakeLine


def deps_with(results):
    deps = make_deps(results)
    deps.line = FakeLine()
    deps.line.names = {"U1": "担当たろう", "U9": "管理じろう"}
    deps.labor = FakeLaborTracker()
    return deps


def quoting(ev, quoted_event):
    ev["message"]["quotedMessageId"] = quoted_event["message"]["id"]
    return ev


def test_resent_slip_is_attributed_to_quoted_sender():
    entry = Entry(month=11, day=12, customer_name="顧客A", amount=5500)
    deps = deps_with([None, slip_msg(entry)])                   # 1通目は読み取れなかった（期限切れのキーなど）
    original = event("2026-11-12 21:40", "E1", user="U1")
    handle_event(original, "message", deps)
    resend = quoting(event("2026-11-15 05:28", "E2", user="U9"), original)
    slip = deps.store.get_slip(handle_event(resend, "message", deps)["slip_ids"][0])
    assert slip.sender_line_user_id == "U1" and slip.sender_display_name == "担当たろう"
    assert any("管理じろうさんが、担当たろうさんのメッセージを引用して送り直した" in t for t in threads(deps, slip))


def test_quoting_own_message_or_unknown_message_keeps_sender():
    entry = Entry(month=11, day=12, customer_name="顧客A", amount=5500)
    deps = deps_with([slip_msg(entry)])
    ev = event("2026-11-15 05:28", "E2", user="U9")
    ev["message"]["quotedMessageId"] = "unknown-message"
    slip = deps.store.get_slip(handle_event(ev, "message", deps)["slip_ids"][0])
    assert slip.sender_line_user_id == "U9"


def test_resent_attendance_is_recorded_for_quoted_sender():
    att = Parsed(kind="attendance", confidence=0.9, attendance={"month": 11, "day": 14, "start": "21:00", "end": "03:00"})
    deps = deps_with([None, att])
    original = event("2026-11-15 03:30", "E1", user="U1")
    handle_event(original, "message", deps)
    handle_event(quoting(event("2026-11-15 05:20", "E2", user="U9"), original), "message", deps)
    person = StaffIndex.load(deps.store).by_line_user("U1")
    assert person and person.line_name == "担当たろう"
    assert any("管理じろうさんが引用して送り直し" in t for t in staff_texts(deps))     # 承認依頼に出す
    approve_all(deps)
    rec = deps.store.get_doc("attendance", f"W-20261114-{person.staff_id}")
    assert rec["hours"] == 6 and "管理じろうさんが引用して送り直し" in rec["history"][-1]["reason"]
