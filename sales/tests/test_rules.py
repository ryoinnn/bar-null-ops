"""状態の決まり方（derive）、以前のデータとの互換、レビューで見つかった不具合の再発防止。"""

from __future__ import annotations

from datetime import datetime

import pytest
from helpers import JST, SQUARE_TXN, event, make_deps, pay, slip_msg, threads, with_staff

from barnull_sales import deposits, health, reconcile
from barnull_sales.deposits import payments_of
from barnull_sales.errors import JobFailed
from barnull_sales.extract import Entry, Parsed
from barnull_sales.http import ExternalAPIError
from barnull_sales.mf import BankTxn
from barnull_sales.process import handle_event
from barnull_sales.slips import Alert, Slip, Status, Unpaid, derive
from barnull_sales.tasks import task_name_for


def slip(**kw) -> Slip:
    base = dict(slip_id="S-1", event_id="E", entry_index=0, group_id="G", message_id="M", sender_line_user_id="U",
                sender_display_name="担当", sent_at="2026-10-10T13:00:00+00:00", raw_text="", business_date="2026-10-10",
                business_date_source="message", customer_name="顧客A", slip_amount=1000, note="",
                expected_square_amount=1000)
    return Slip(**{**base, **kw})


ALERT = [{"type": Alert.PARSE, "message": "", "at": "t", "resolved_at": None}]
PAID = {"payment_ids": ["p1"], "payments": [{"id": "p1", "amount": 1000, "source_type": "CARD"}]}


# ---------------------------------------------------------------- derive の表
@pytest.mark.parametrize("kw, status, closed", [
    ({}, Status.DRAFTED, False),
    ({"square": PAID}, Status.PAID, False),
    ({"square": PAID, "deposits": {"completed_at": "t"}}, Status.DEPOSITED, True),
    ({"square": PAID, "deposits": {"completed_at": "t"}, "alerts": ALERT}, Status.REVIEW, False),
    ({"square": PAID, "deposits": {"completed_at": "t"}, "unpaid_amount": 500, "unpaid_remaining": 500},
     Status.DEPOSITED, False),
    ({"expected_square_amount": 0, "unpaid_amount": 1000, "unpaid_remaining": 1000}, Status.DEPOSITED, False),
    ({"expected_square_amount": 0, "unpaid_amount": 1000, "unpaid_remaining": 0}, Status.DEPOSITED, True),
    ({"square": {"manual": True}}, Status.DEPOSITED, True),                 # 人が確認・Square の決済なし
    ({"square": {**PAID, "cancelled": True}}, Status.CANCELLED, True),
])
def test_derive_table(kw, status, closed):
    s = slip(**kw)
    derive(s)
    assert s.status == status and bool(s.closed_at) == closed


def test_derive_resolves_moot_alerts():
    s = slip(square=PAID, alerts=[{"type": Alert.PAYMENT_MISSING, "message": "", "at": "t", "resolved_at": None}])
    derive(s)
    assert not s.open_alerts() and s.status == Status.PAID


def test_unpaid_status():
    s = slip(unpaid_amount=500, unpaid_remaining=500)
    derive(s)
    assert s.unpaid_status == Unpaid.OPEN
    s.collection_reports.append({"type": "report", "amount": 500, "confirmed": False})
    derive(s)
    assert s.unpaid_status == Unpaid.REPORTED
    s.unpaid_remaining = 0
    derive(s)
    assert s.unpaid_status == Unpaid.COLLECTED


# ---------------------------------------------------------------- 以前のデータ
def test_old_documents_load_and_behave():
    """2026-10-19 より前のコードが書いた伝票（新しい項目が無い）も読める。"""
    old = slip().to_dict()
    for k in ("payer_checks", "sync_pending"):
        old.pop(k)
    old["updated_at"] = "2026-10-18T00:00:00+00:00"            # 伝票の項目に無いものは無視する
    old["square"] = {"payment_ids": ["p1"], "methods": ["CASH"], "paid_at": ["2026-10-10T14:00:00+00:00"],
                     "amount": 1000, "fee": 0}
    old["alerts"] = [{"type": "決済未確認", "message": "", "at": "t", "resolved_at": "t2"}]   # posted・important なし
    s = Slip.from_dict(old)
    assert s.payment_settled and s.awaiting_deposit
    assert payments_of(s) == [{"id": "p1", "amount": None, "fee": 0, "source_type": "CASH",
                               "paid_at": "2026-10-10T14:00:00+00:00"}]


# ---------------------------------------------------------------- 障害の通知
def test_health_names_failing_api_for_partial_failures():
    deps = make_deps()
    err = JobFailed("deposits", [("slip_id=S-1", ExternalAPIError("Asana", "PUT /tasks", 503, ""))])
    for _ in range(2):
        health.record(deps, "deposits", err)
    assert "【外部API障害】" in deps.notifier.posts[-1]["text"] and "Asana" in deps.notifier.posts[-1]["text"]
    deps = make_deps()
    mixed = JobFailed("deposits", [("a", ExternalAPIError("Asana", "x", 503, "")), ("b", KeyError("k"))])
    for _ in range(2):
        health.record(deps, "deposits", mixed)
    assert "【処理エラー】" in deps.notifier.posts[-1]["text"]


def test_unknown_groups_share_one_task_name():
    a = {"webhookEventId": "E1", "source": {"groupId": "Cx"}}
    b = {"webhookEventId": "E2", "source": {"groupId": "Cx"}}
    assert task_name_for(a, "unknown_group_message") == task_name_for(b, "unknown_group_message")
    assert task_name_for(a, "message") != task_name_for(b, "message")


# ---------------------------------------------------------------- 2026-10-19 のレビューで見つかった不具合
def test_unclear_transfer_alert_does_not_block_close():
    deps = make_deps([slip_msg(Entry(month=10, day=10, customer_name="山田", amount=50000, unpaid_amount=50000))],
                     txns=[BankTxn("tx", "2026-10-12", 50000, "振込 カ)ベツガイシャ"),
                           BankTxn("t4", "2026-10-16", 50000, "振込 ヤマダ タロウ")],
                     payers={("振込 ヤマダ タロウ", "山田"): (True, 0.95)})
    sid = handle_event(event("2026-10-11 02:30"), "message", deps)["slip_ids"][0]
    deposits.run(deps, now=datetime(2026, 10, 16, 12, 0, tzinfo=JST), force_mf=True)
    s = deps.store.get_slip(sid)
    assert s.unpaid_remaining == 0 and s.closed_at


def test_later_cash_collection_needs_its_own_deposit():
    deps = make_deps([slip_msg(Entry(month=10, day=10, customer_name="山田", amount=100000, unpaid_amount=40000))],
                     [pay("c1", "2026-10-11 02:14", 60000, source="CASH")], [],
                     [BankTxn("t1", "2026-10-12", 60000, "振込 スタッフ イチロウ")])
    with_staff(deps, "スタッフ イチロウ")
    sid = handle_event(event("2026-10-11 02:30"), "message", deps)["slip_ids"][0]
    deposits.run(deps, now=datetime(2026, 10, 12, 12, 0, tzinfo=JST), force_mf=True)
    deps.payments.payments.append(pay("c9", "2026-10-15 21:00", 40000, source="CASH"))
    reconcile.run(deps, now=datetime(2026, 10, 15, 21, 30, tzinfo=JST))
    deposits.run(deps, now=datetime(2026, 10, 15, 22, 0, tzinfo=JST))
    assert not deps.store.get_slip(sid).closed_at                       # 回収分の入金はまだ見ていない
    deps.bank.txns.append(BankTxn("t2", "2026-10-16", 40000, "振込 スタッフ イチロウ"))
    deposits.run(deps, now=datetime(2026, 10, 16, 12, 0, tzinfo=JST), force_mf=True)
    s = deps.store.get_slip(sid)
    assert s.closed_at and s.unpaid_remaining == 0


def test_collection_payment_is_not_taken_as_the_main_payment():
    deps = make_deps([slip_msg(Entry(month=10, day=10, customer_name="山田", amount=100000, unpaid_amount=40000))])
    sid = handle_event(event("2026-10-11 02:30"), "message", deps)["slip_ids"][0]
    deps.payments.payments.append(pay("p9", "2026-10-12 21:00", 40000))       # その日の決済より先に回収と同額の決済
    reconcile.run(deps, now=datetime(2026, 10, 12, 21, 30, tzinfo=JST))
    assert "p9" not in deps.store.get_slip(sid).payment_ids
    deps.payments.payments.append(pay("p1", "2026-10-11 03:00", 60000))
    reconcile.run(deps, now=datetime(2026, 10, 13, 1, 30, tzinfo=JST))
    s = deps.store.get_slip(sid)
    assert s.main_payment_ids == ["p1"] and "p9" in s.payment_ids             # その日の決済の後で、回収として割り当て


def test_every_correction_after_close_is_reported():
    corr = [Parsed(kind="correction", confidence=0.9, correction={
        "target_customer_name": None, "new_amount": a, "new_customer_name": None, "description": f"{a}円に訂正"})
        for a in (95000, 90000)]
    deps = make_deps([slip_msg(Entry(month=10, day=5, customer_name="顧客A", amount=100000)), *corr])
    s = deps.store.get_slip(handle_event(event("2026-10-06 05:40", "E1"), "message", deps)["slip_ids"][0])
    deps.store.update_slip(s, lambda x: setattr(x, "closed_at", "2026-10-08T00:00:00+00:00"))
    handle_event(event("2026-10-06 07:00", "E2"), "message", deps)
    handle_event(event("2026-10-06 08:00", "E3"), "message", deps)
    assert len([t for t in threads(deps, s) if "完了済みの伝票への訂正" in t]) == 2


def test_retried_name_correction_is_not_sent_to_review():
    deps = make_deps([slip_msg(Entry(month=10, day=5, customer_name="田中", amount=100000)),
                      Parsed(kind="correction", confidence=0.9, correction={
                          "target_customer_name": "田中", "new_amount": None, "new_customer_name": "佐藤",
                          "description": "名前の訂正"})])
    handle_event(event("2026-10-06 05:40", "E1"), "message", deps)
    ev = event("2026-10-06 06:10", "E2")
    handle_event(ev, "message", deps)
    deps.store.docs["line_events"]["E2"].pop("processed_at")
    assert handle_event(ev, "message", deps, attempt=2)["status"] == "corrected"
    assert not any("決められない" in p["text"] for p in deps.notifier.posts)


def test_correction_logs_do_not_contain_customer_data(caplog):
    deps = make_deps([slip_msg(Entry(month=10, day=5, customer_name="田中", amount=100000)),
                      Parsed(kind="correction", confidence=0.9, correction={
                          "target_customer_name": "田中", "new_amount": 95000, "new_customer_name": "佐藤",
                          "description": "訂正"})])
    handle_event(event("2026-10-06 05:40", "E1"), "message", deps)
    caplog.set_level("INFO")
    handle_event(event("2026-10-06 06:10", "E2"), "message", deps)
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "田中" not in text and "佐藤" not in text and "95,000" not in text


def test_square_txn_constant_is_used():
    assert "スクエア" in SQUARE_TXN
