"""POS（福岡 Node）からの伝票: 受け付け・受け取りごとの突合・訂正・取り消し・未収の回収の通知・現金の営業日の合計・
同じ口座のほかの店舗の振込・Cloud Tasks での定期ジョブ。LINE の伝票の動きは既存のテストで守る。"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, time

import pytest
from helpers import JST, SQUARE_TXN, make_deps, pay, payout, threads, with_staff

from barnull_sales import deposits, pos_intake, pos_notify, reconcile, schedule, unpaid
from barnull_sales.mf import BankTxn
from barnull_sales.slips import Alert, Status
from barnull_sales.testing import FakePos


def jst(local: str) -> str:
    return datetime.strptime(local, "%Y-%m-%d %H:%M").replace(tzinfo=JST).isoformat()


def body(visit="V-20261017-001", rev=1, state="closed", total=12_000, closed="2026-10-18 01:40", receipts=None,
         name="山田様", staff="ハル", **extra) -> dict:
    receipts = receipts if receipts is not None else [
        {"id": "r1", "method": "cash", "amount": 5_000, "at": jst("2026-10-18 01:35")},
        {"id": "r2", "method": "square", "amount": 7_000, "at": jst("2026-10-18 01:38")},
    ]
    return {"visit_id": visit, "revision": rev, "state": state, "business_date": "2026-10-17", "closed_at": jst(closed),
            "total": total, "name": name, "staff": staff, "note": "", "party": 2, "seats": ["T1"],
            "receipts": receipts, "customers": [{"code": "FK-AB12CD", "visit_no": 1}],
            "referrals": [{"agent_id": "AG-0002", "agent_name": "春吉ライン", "tenant_agent_id": "AG-0001",
                           "tenant_name": "中洲ナイト", "member_id": "AM-0003", "member_name": "ユウ"}], **extra}


def node_deps(payments=(), payouts=(), txns=(), **kw):
    deps = make_deps((), payments, payouts, txns, **kw)
    deps.settings = replace(deps.settings, day_start=time(9, 0), store_name="Node", line_enabled=False,
                            pos_callback_url="https://pos.example/api/pos/sales-events", pos_callback_audience="aud")
    deps.pos = FakePos()
    return deps


def cash(pid, local, amount):
    return pay(pid, local, amount, source="CASH")


# ---------------------------------------------------------------- 受け付け
def test_pos_slip_is_created_without_line_and_matched_per_receipt():
    deps = node_deps([cash("c1", "2026-10-18 01:36", 5_000), pay("k1", "2026-10-18 01:39", 7_000, fee=175)])
    r = pos_intake.handle(deps, body())
    slip = deps.store.get_slip(r["slip_id"])
    assert r["status"] == "created" and slip.source == "pos" and slip.slip_id.startswith("S-20261017-")
    assert not slip.open_alerts(Alert.PARSE)                     # 送信者（LINE）が無くても「要確認」にしない
    assert slip.customer_name == "山田様" and slip.sender_display_name == "ハル" and slip.expected_square_amount == 12_000
    assert slip.status == Status.PAID and sorted(slip.payment_ids) == ["c1", "k1"]
    assert slip.square["match_method"] == "receipt" and slip.square["fee"] == 175
    parent = deps.notifier.posts[0]["text"]
    assert "POS V-20261017-001" in parent and "中洲ナイト › 春吉ライン" in parent and "（LINE）" not in parent
    assert any("POS の受け取り 2件と突合" in t for t in threads(deps, slip))


def test_same_revision_twice_makes_one_slip_and_older_revision_is_ignored():
    deps = node_deps()
    a = pos_intake.handle(deps, body(rev=2))
    b = pos_intake.handle(deps, body(rev=2))
    old = [{"id": "r1", "method": "cash", "amount": 99_000, "at": jst("2026-10-18 01:35")}]
    c = pos_intake.handle(deps, body(rev=1, total=99_000, receipts=old))
    assert a["slip_id"] == b["slip_id"] == c["slip_id"] and b["status"] == c["status"] == "stale"
    assert len(deps.store.docs["slips"]) == 1 and deps.store.get_slip(a["slip_id"]).slip_amount == 12_000


@pytest.mark.parametrize("bad", [
    {"receipts": [{"id": "r1", "method": "cash", "amount": 1_000, "at": jst("2026-10-18 01:00")}]},   # 合計と合わない
    {"state": "open"},
    {"revision": 0},
    {"receipts": [{"id": "r1", "method": "card", "amount": 12_000, "at": jst("2026-10-18 01:00")}]},
])
def test_invalid_bodies_are_rejected(bad):
    with pytest.raises(pos_intake.Invalid):
        pos_intake.handle(node_deps(), {**body(), **bad})


# ---------------------------------------------------------------- 受け取りごとの突合
def test_cash_receipt_does_not_take_a_card_payment():
    deps = node_deps([pay("k1", "2026-10-18 01:36", 5_000), pay("k2", "2026-10-18 01:39", 7_000)])
    slip = deps.store.get_slip(pos_intake.handle(deps, body())["slip_id"])
    assert slip.status == Status.DRAFTED and slip.square["receipt_matches"] == {"r2": "k2"}   # 一部だけ
    assert deps.store.payment_claims(["k1", "k2"]) == {"k2": slip.slip_id}


def test_partial_match_completes_later():
    deps = node_deps([pay("k1", "2026-10-18 01:39", 7_000)])
    sid = pos_intake.handle(deps, body())["slip_id"]
    deps.payments.payments.append(cash("c1", "2026-10-18 01:50", 5_000))   # 現金を後から Square に記録した
    reconcile.run(deps, now=datetime(2026, 10, 18, 1, 55, tzinfo=JST))
    slip = deps.store.get_slip(sid)
    assert slip.status == Status.PAID and sorted(slip.payment_ids) == ["c1", "k1"]


def test_split_receipts_with_the_same_amount():
    receipts = [{"id": f"r{i}", "method": "square", "amount": 10_000, "at": jst("2026-10-18 01:30")} for i in (1, 2)]
    deps = node_deps([pay("k1", "2026-10-18 01:31", 10_000), pay("k2", "2026-10-18 01:32", 10_000)])
    slip = deps.store.get_slip(pos_intake.handle(deps, body(total=20_000, receipts=receipts))["slip_id"])
    assert slip.status == Status.PAID and sorted(slip.payment_ids) == ["k1", "k2"]


def test_too_many_candidates_need_a_person():
    receipts = [{"id": "r1", "method": "square", "amount": 10_000, "at": jst("2026-10-18 01:30")}]
    deps = node_deps([pay("k1", "2026-10-18 01:31", 10_000), pay("k2", "2026-10-18 01:33", 10_000)])
    slip = deps.store.get_slip(pos_intake.handle(deps, body(total=10_000, receipts=receipts))["slip_id"])
    assert slip.status == Status.REVIEW and slip.open_alerts(Alert.AMBIGUOUS) and not slip.payment_ids


def test_clearly_nearest_candidate_is_taken():
    receipts = [{"id": "r1", "method": "square", "amount": 10_000, "at": jst("2026-10-18 01:30")}]
    deps = node_deps([pay("k1", "2026-10-18 01:31", 10_000), pay("k2", "2026-10-18 01:50", 10_000)])
    slip = deps.store.get_slip(pos_intake.handle(deps, body(total=10_000, receipts=receipts))["slip_id"])
    assert slip.payment_ids == ["k1"]


def test_missing_payment_alert_after_grace():
    deps = node_deps([cash("c1", "2026-10-18 01:36", 5_000)])
    sid = pos_intake.handle(deps, body())["slip_id"]
    reconcile.run(deps, now=datetime(2026, 10, 18, 2, 45, tzinfo=JST))
    slip = deps.store.get_slip(sid)
    alert = slip.open_alerts(Alert.PAYMENT_MISSING)
    assert alert and "Square ¥7,000" in alert[0]["message"] and "現金" not in alert[0]["message"].split("（")[1]


# ---------------------------------------------------------------- 訂正・取り消し
def test_correction_releases_and_rematches():
    deps = node_deps([cash("c1", "2026-10-18 01:36", 5_000), pay("k1", "2026-10-18 01:39", 7_000),
                      pay("k3", "2026-10-18 01:41", 9_000)])
    sid = pos_intake.handle(deps, body())["slip_id"]
    receipts = [{"id": "r1", "method": "cash", "amount": 5_000, "at": jst("2026-10-18 01:35")},
                {"id": "r3", "method": "square", "amount": 9_000, "at": jst("2026-10-18 01:40")}]
    r = pos_intake.handle(deps, body(rev=2, total=14_000, receipts=receipts))
    slip = deps.store.get_slip(sid)
    assert r["status"] == "corrected" and slip.slip_amount == 14_000 and sorted(slip.payment_ids) == ["c1", "k3"]
    assert deps.store.payment_claims(["k1"]) == {}            # 前の決済は外れた
    assert any("POS で訂正" in t for t in threads(deps, slip))


def test_void_cancels_and_frees_payments():
    deps = node_deps([cash("c1", "2026-10-18 01:36", 5_000), pay("k1", "2026-10-18 01:39", 7_000)])
    sid = pos_intake.handle(deps, body())["slip_id"]
    pos_intake.handle(deps, body(rev=2, state="void"))
    slip = deps.store.get_slip(sid)
    assert slip.status == Status.CANCELLED and deps.store.payment_claims(["c1", "k1"]) == {}


def test_voided_before_first_delivery_is_created_cancelled():
    deps = node_deps()
    slip = deps.store.get_slip(pos_intake.handle(deps, body(rev=3, state="void"))["slip_id"])
    assert slip.status == Status.CANCELLED


# ---------------------------------------------------------------- 未収の回収 → POS
def unpaid_body(**kw):
    receipts = [{"id": "r1", "method": "cash", "amount": 6_000, "at": jst("2026-10-18 01:35")},
                {"id": "r2", "method": "unpaid", "amount": 4_000, "at": jst("2026-10-18 01:36")}]
    return body(total=10_000, receipts=receipts, unpaid={"amount": 4_000, "due": "12/12"}, **kw)


def test_unpaid_collection_is_sent_to_pos():
    deps = node_deps([cash("c1", "2026-10-18 01:36", 6_000)])
    sid = pos_intake.handle(deps, unpaid_body())["slip_id"]
    slip = deps.store.get_slip(sid)
    assert slip.unpaid_amount == 4_000 and slip.expected_square_amount == 6_000 and slip.payment_ids == ["c1"]
    unpaid.record_collection(deps, slip, 4_000, {"type": "bank", "txn_id": "t9"}, "振込")
    assert deps.store.get_slip(sid).pos["outbox_pending"] is True
    deps.pos.fail = True
    with pytest.raises(RuntimeError):
        pos_notify.run(deps)
    assert deps.store.get_slip(sid).pos["outbox_pending"] is True   # 送れなければ次の回に
    deps.pos.fail = False
    assert pos_notify.run(deps) == 1
    (visit_id, events), = deps.pos.sent
    assert visit_id == "V-20261017-001" and events[0]["type"] == "unpaid_collected" and events[0]["remaining"] == 0
    assert deps.store.get_slip(sid).pos["outbox_pending"] is False and pos_notify.run(deps) == 0


def test_bar_null_has_no_pos_notifications():
    deps = node_deps()
    deps.settings = replace(deps.settings, pos_callback_url="")
    assert pos_notify.run(deps) == 0


# ---------------------------------------------------------------- 入金
def test_cash_is_confirmed_by_business_day_total():
    deps = node_deps([cash("c1", "2026-10-18 01:36", 5_000), pay("k1", "2026-10-18 01:39", 7_000),
                      cash("c2", "2026-10-18 03:10", 8_000)],
                     txns=[BankTxn("t1", "2026-10-19", 13_000, "振込 ハル")])
    with_staff(deps, "ハル")
    deps.settings = replace(deps.settings, mf=replace(deps.settings.mf, cash_by_business_day=True))
    a = pos_intake.handle(deps, body())["slip_id"]
    receipts = [{"id": "r9", "method": "cash", "amount": 8_000, "at": jst("2026-10-18 03:05")}]
    b = pos_intake.handle(deps, body(visit="V-20261017-002", total=8_000, receipts=receipts))["slip_id"]
    slips = deposits.awaiting_slips(deps)
    deposits.refresh_expected(deps, slips, deposits.ItemErrors("t"))
    early = datetime(2026, 10, 18, 12, 0, tzinfo=JST)              # 営業日の終わり（9:00）から6時間たっていない
    assert deposits.match_cash_days(deps, deposits.awaiting_slips(deps), deps.bank.txns, {}, ["ハル"], early,
                                    deposits.ItemErrors("t")) == 0
    later = datetime(2026, 10, 19, 12, 0, tzinfo=JST)
    # カードの振込がまだで、1件目の伝票の入金の予定ができていない → その日の現金の合計はまだ決まらない
    assert deposits.match_cash_days(deps, deposits.awaiting_slips(deps), deps.bank.txns, {}, ["ハル"], later,
                                    deposits.ItemErrors("t")) == 0
    deps.payouts.payouts.append(payout("po1", 6_825, "2026-10-20", ["k1"]))
    deposits.refresh_expected(deps, deposits.awaiting_slips(deps), deposits.ItemErrors("t"))
    assert deposits.match_cash_days(deps, deposits.awaiting_slips(deps), deps.bank.txns, {}, ["ハル"], later,
                                    deposits.ItemErrors("t")) == 1
    for sid in (a, b):
        cash_entry = next(e for e in deps.store.get_slip(sid).deposits["expected"] if e["kind"] == "cash")
        assert cash_entry["confirmed"]["txn_id"] == "t1"


def test_same_amount_payout_at_the_other_store_is_not_auto_confirmed():
    deps = node_deps([pay("k1", "2026-10-18 01:39", 7_000, fee=175)],
                     payouts=[payout("po1", 6_825, "2026-10-20", ["k1"])],
                     txns=[BankTxn("t1", "2026-10-20", 6_825, SQUARE_TXN)])
    deps.settings = replace(deps.settings, square=replace(deps.settings.square, shared_bank_locations=("BARNULL",)))
    deps.payouts.others = {"BARNULL": [{"id": "pb1", "amount_money": {"amount": 6_825}, "arrival_date": "2026-10-20",
                                        "status": "PAID"}]}
    receipts = [{"id": "r2", "method": "square", "amount": 7_000, "at": jst("2026-10-18 01:38")}]
    sid = pos_intake.handle(deps, body(total=7_000, receipts=receipts))["slip_id"]
    deposits.run(deps, now=datetime(2026, 10, 20, 12, 0, tzinfo=JST), force_mf=True)
    slip = deps.store.get_slip(sid)
    assert slip.open_alerts(Alert.DEPOSIT_AMBIGUOUS) and not slip.deposits.get("completed_at")
    # ほかの店舗に同じ金額が無ければ、いつもどおり確認する
    deps.payouts.others = {"BARNULL": []}
    deposits.run(deps, now=datetime(2026, 10, 20, 13, 0, tzinfo=JST), force_mf=True)
    assert deps.store.get_slip(sid).deposits.get("completed_at")


# ---------------------------------------------------------------- Cloud Tasks での定期ジョブ
def test_next_slot_and_due_jobs():
    assert schedule.next_slot(datetime(2026, 10, 19, 1, 10, tzinfo=UTC)) == datetime(2026, 10, 19, 1, 30, tzinfo=UTC)
    assert schedule.next_slot(datetime(2026, 10, 19, 1, 30, tzinfo=UTC)) == datetime(2026, 10, 19, 2, 0, tzinfo=UTC)
    deps = node_deps()
    monday_0915 = datetime(2026, 10, 19, 9, 15, tzinfo=JST)
    assert schedule.due(deps, monday_0915) == ["weekly"]
    deps.store.set_doc("meta", "schedule", {"weekly": "2026-10-19"})
    assert schedule.due(deps, datetime(2026, 10, 19, 10, 10, tzinfo=JST)) == ["daily"]


def test_ensure_only_in_tasks_mode():
    deps = node_deps()
    calls = []
    assert schedule.ensure(deps, enqueue=lambda d, slot: calls.append(slot) or "created") == "skipped"
    deps.settings = replace(deps.settings, schedule_mode="tasks")
    now = datetime(2026, 10, 19, 1, 10, tzinfo=UTC)
    assert schedule.ensure(deps, now, enqueue=lambda d, slot: calls.append(slot) or "created") == "created"
    assert calls == [datetime(2026, 10, 19, 1, 30, tzinfo=UTC)]
    assert schedule.ensure(deps, now, enqueue=lambda d, slot: (_ for _ in ()).throw(RuntimeError("x"))) == "failed"
