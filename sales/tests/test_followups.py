"""訂正、未収の回収、手動上書き、現金のまとめ入金、復元。"""

from __future__ import annotations

from datetime import datetime, timedelta

from helpers import JST, SQUARE_TXN, event, make_deps, pay, payout, slip_msg, threads, with_staff

from barnull_sales import deposits, overrides, reconcile, unpaid
from barnull_sales.extract import Entry, Parsed
from barnull_sales.mf import BankTxn
from barnull_sales.process import handle_event
from barnull_sales.slips import Manual, Status


def correction(amount=None, target=None, name=None):
    return Parsed(kind="correction", confidence=0.9, correction={
        "target_customer_name": target, "new_amount": amount, "new_customer_name": name, "description": "訂正"})


# ---------------------------------------------------------------- 訂正
def test_correction_changes_amount_and_rematches():
    deps = make_deps([slip_msg(Entry(month=10, day=5, customer_name="顧客A", amount=100000)),
                      correction(95000, target="顧客A")],
                     [pay("p1", "2026-10-06 05:28", 100000), pay("p2", "2026-10-06 05:29", 95000)])
    sid = handle_event(event("2026-10-06 05:40", "E1"), "message", deps)["slip_ids"][0]
    assert deps.store.get_slip(sid).payment_ids == ["p1"]
    r2 = handle_event(event("2026-10-06 06:10", "E2"), "message", deps)
    slip = deps.store.get_slip(sid)
    assert r2["status"] == "corrected"
    assert slip.slip_amount == 95000 and slip.payment_ids == ["p2"]          # 割り当てを外して照合し直した
    assert "p1" not in deps.store.payment_claims(["p1"])
    assert any(t.startswith("訂正：金額 ¥100,000 → ¥95,000") for t in threads(deps, slip))


def test_correction_is_applied_once_even_if_retried():
    deps = make_deps([slip_msg(Entry(month=10, day=5, customer_name="顧客A", amount=100000)), correction(95000)])
    sid = handle_event(event("2026-10-06 05:40", "E1"), "message", deps)["slip_ids"][0]
    ev = event("2026-10-06 06:10", "E2")
    handle_event(ev, "message", deps)
    deps.store.docs["line_events"]["E2"].pop("processed_at")     # 処理済みの記録の前に止まった状態
    assert handle_event(ev, "message", deps, attempt=2)["status"] == "corrected"
    slip = deps.store.get_slip(sid)
    assert len([t for t in threads(deps, slip) if t.startswith("訂正")]) == 1


def test_correction_after_close_is_not_reopened():
    deps = make_deps([slip_msg(Entry(month=10, day=5, customer_name="顧客A", amount=100000)), correction(95000)])
    slip = deps.store.get_slip(handle_event(event("2026-10-06 05:40", "E1"), "message", deps)["slip_ids"][0])
    deps.store.update_slip(slip, lambda s: setattr(s, "closed_at", "2026-10-08T00:00:00+00:00"))
    assert handle_event(event("2026-10-06 07:00", "E2"), "message", deps)["status"] == "correction_after_close"
    assert deps.store.get_slip(slip.slip_id).slip_amount == 100000
    assert any("完了後の訂正" in t for t in threads(deps, slip))


# ---------------------------------------------------------------- 未収
def unpaid_deps(results=(), **kw):
    first = slip_msg(Entry(month=10, day=10, customer_name="山田", amount=100000, unpaid_amount=40000))
    return make_deps([first, *results], **kw)


def test_collection_report_then_bank_transfer_closes_slip():
    deps = unpaid_deps([Parsed(kind="unpaid_collection_report", confidence=0.9,
                               collection_report={"customer_name": "山田", "amount": 40000, "method": "bank_transfer"})],
                       payments=[pay("p1", "2026-10-11 02:14", 60000, fee=1500)],
                       payouts=[payout("po2", 58500, "2026-10-13", ["p1"])],
                       txns=[BankTxn("t3", "2026-10-12", 58500, SQUARE_TXN),
                             BankTxn("t4", "2026-10-16", 40000, "振込 ヤマダ タロウ")],
                       payers={("振込 ヤマダ タロウ", "山田"): (True, 0.93)})
    sid = handle_event(event("2026-10-11 02:30", "E1"), "message", deps)["slip_ids"][0]
    ev = event("2026-10-15 20:00", "E2")
    handle_event(ev, "message", deps)
    deps.store.docs["line_events"]["E2"].pop("processed_at")
    handle_event(ev, "message", deps, attempt=2)                      # 再試行しても報告は1件
    slip = deps.store.get_slip(sid)
    assert slip.unpaid_status == "回収報告済み" and len(slip.collection_reports) == 1
    deposits.run(deps, now=datetime(2026, 10, 16, 12, 0, tzinfo=JST), force_mf=True)
    slip = deps.store.get_slip(sid)
    assert slip.unpaid_status == "回収入金確認済み" and slip.unpaid_remaining == 0
    assert slip.status == "入金確認完了" and slip.closed_at
    assert deps.tracker.tasks[slip.asana["task_gid"]]["completed"]


def test_fully_unpaid_slip_closes_after_collection():
    """全額未収（その日の決済なし）の伝票も、回収が確認できれば完了する。"""
    deps = make_deps([slip_msg(Entry(month=10, day=10, customer_name="山田", amount=50000, unpaid_amount=50000))],
                     txns=[BankTxn("t4", "2026-10-16", 50000, "振込 ヤマダ タロウ")],
                     payers={("振込 ヤマダ タロウ", "山田"): (True, 0.95)})
    sid = handle_event(event("2026-10-11 02:30"), "message", deps)["slip_ids"][0]
    slip = deps.store.get_slip(sid)
    assert slip.status == "入金確認完了" and slip.unpaid_status == "未収入金有" and not slip.closed_at
    reconcile.run(deps, now=datetime(2026, 10, 11, 9, 0, tzinfo=JST))
    assert not [p for p in deps.notifier.posts if "決済未確認" in p["text"]]
    deposits.run(deps, now=datetime(2026, 10, 16, 12, 0, tzinfo=JST), force_mf=True)
    assert deps.store.get_slip(sid).closed_at


def test_transfer_with_unclear_name_asks_once_and_is_judged_once():
    deps = unpaid_deps(txns=[BankTxn("t4", "2026-10-16", 40000, "振込 カ)テストショウジ")])
    sid = handle_event(event("2026-10-11 02:30", "E1"), "message", deps)["slip_ids"][0]
    for h in (12, 15, 18):
        deposits.run(deps, now=datetime(2026, 10, 16, h, 0, tzinfo=JST), force_mf=True)
    slip = deps.store.get_slip(sid)
    assert slip.unpaid_remaining == 40000
    assert len([t for t in threads(deps, slip) if "回収の振込か不明" in t]) == 1
    assert deps.extractor.payer_calls == 1                         # 判定の結果は保存して使い回す


def test_unpaid_paid_later_by_square_keeps_confirmed_deposits():
    deps = unpaid_deps(payments=[pay("p1", "2026-10-11 02:14", 60000, fee=1500)],
                       payouts=[payout("poA", 58500, "2026-10-13", ["p1"])],
                       txns=[BankTxn("tA", "2026-10-12", 58500, SQUARE_TXN)])
    sid = handle_event(event("2026-10-11 02:30", "E1"), "message", deps)["slip_ids"][0]
    deposits.run(deps, now=datetime(2026, 10, 13, 12, 0, tzinfo=JST), force_mf=True)
    assert deps.store.get_slip(sid).deposits["completed_at"]
    deps.payments.payments.append(pay("p9", "2026-10-15 21:00", 40000, fee=1000))
    reconcile.run(deps, now=datetime(2026, 10, 15, 21, 30, tzinfo=JST))
    slip = deps.store.get_slip(sid)
    assert slip.unpaid_status == "回収報告済み" and "p9" in slip.payment_ids and slip.status == "決済確認済み"
    # 回収分の振込が着金すれば完了。当日分の確認済みの入金は引き継ぐ（消して照合し直さない）
    deps.payouts.payouts.append(payout("poB", 39000, "2026-10-16", ["p9"]))
    deps.bank.txns.append(BankTxn("tB", "2026-10-15", 39000, SQUARE_TXN))
    deposits.run(deps, now=datetime(2026, 10, 16, 12, 0, tzinfo=JST), force_mf=True)
    slip = deps.store.get_slip(sid)
    assert slip.unpaid_status == "回収入金確認済み" and slip.closed_at
    assert {e["confirmed"]["txn_id"] for e in slip.deposits["expected"]} == {"tA", "tB"}


def test_unpaid_reminder_every_interval():
    deps = unpaid_deps()
    slip = deps.store.get_slip(handle_event(event("2026-10-11 02:30", "E1"), "message", deps)["slip_ids"][0])
    created = datetime.fromisoformat(slip.created_at)
    assert unpaid.reminders(deps, now=created + timedelta(days=6)) == 0
    assert unpaid.reminders(deps, now=created + timedelta(days=7, hours=1)) == 1
    assert unpaid.reminders(deps, now=created + timedelta(days=8)) == 0               # 同じ周期では1回だけ
    assert unpaid.reminders(deps, now=created + timedelta(days=14, hours=1)) == 1
    texts = [t for t in threads(deps, slip) if t.startswith("<@UALERT> 未収リマインド")]
    assert len(texts) == 2 and "未収残高 ¥40,000" in texts[0]


# ---------------------------------------------------------------- 手動上書き
def test_manual_paid_without_square_payment_completes():
    """Square の決済が無いまま人が「決済確認済み」にした伝票は、自動で確かめる入金が無いので完了する（止まらない）。"""
    deps = make_deps([slip_msg(Entry(month=10, day=16, customer_name="顧客A", amount=12345))])
    slip = deps.store.get_slip(handle_event(event("2026-10-17 01:00"), "message", deps)["slip_ids"][0])
    task = deps.tracker.tasks[slip.asana["task_gid"]]
    task["override"] = Manual.PAID
    assert overrides.apply(deps) == 1
    slip = deps.store.get_slip(slip.slip_id)
    assert slip.status == Status.DEPOSITED and slip.closed_at and task["completed"]
    assert any("手動上書き：テスト担当 が「決済確認済み」" in t for t in threads(deps, slip))
    assert task["override"] is None                                  # 「なし」に戻す


def test_manual_deposited_closes_slip_waiting_for_deposit():
    deps = make_deps([slip_msg(Entry(month=10, day=5, customer_name="顧客A", amount=1000))],
                     [pay("p1", "2026-10-06 05:28", 1000)])
    slip = deps.store.get_slip(handle_event(event("2026-10-06 05:40"), "message", deps)["slip_ids"][0])
    assert slip.status == Status.PAID
    deps.tracker.tasks[slip.asana["task_gid"]]["override"] = Manual.DEPOSITED
    overrides.apply(deps)
    slip = deps.store.get_slip(slip.slip_id)
    assert slip.status == Status.DEPOSITED and slip.closed_at


def test_unknown_override_is_reported_and_reset():
    deps = make_deps([slip_msg(Entry(month=10, day=16, customer_name="顧客A", amount=1))])
    slip = deps.store.get_slip(handle_event(event("2026-10-17 01:00"), "message", deps)["slip_ids"][0])
    task = deps.tracker.tasks[slip.asana["task_gid"]]
    task["override"] = "手動：よく分からない値"
    overrides.apply(deps)
    assert task["override"] is None
    assert any("反映できませんでした" in p["text"] for p in deps.notifier.posts)


def test_manual_paid_keeps_square_payments_and_checks_deposit():
    deps = make_deps([slip_msg(Entry(month=10, day=5, customer_name="顧客A", amount=100000))],
                     [pay("p1", "2026-10-06 05:28", 100000, fee=2500)],
                     [payout("po1", 97500, "2026-10-08", ["p1"])], [BankTxn("t1", "2026-10-07", 97500, SQUARE_TXN)])
    slip = deps.store.get_slip(handle_event(event("2026-10-06 05:40"), "message", deps)["slip_ids"][0])
    deps.tracker.tasks[slip.asana["task_gid"]]["override"] = "手動：決済確認済み"
    overrides.apply(deps)
    assert deps.store.get_slip(slip.slip_id).payment_ids == ["p1"]
    deposits.run(deps, now=datetime(2026, 10, 8, 12, 0, tzinfo=JST), force_mf=True)
    assert deps.store.get_slip(slip.slip_id).closed_at


def test_cancel_releases_payment_for_other_slips():
    deps = make_deps([slip_msg(Entry(month=10, day=5, customer_name="顧客A", amount=1000))],
                     [pay("p1", "2026-10-06 05:28", 1000)])
    slip = deps.store.get_slip(handle_event(event("2026-10-06 05:40"), "message", deps)["slip_ids"][0])
    deps.tracker.tasks[slip.asana["task_gid"]]["override"] = "手動：対象外（取消）"
    overrides.apply(deps)
    slip = deps.store.get_slip(slip.slip_id)
    assert slip.status == "対象外" and slip.closed_at and not deps.store.payment_claims(["p1"])


def test_override_position_is_kept_when_it_fails():
    deps = make_deps([slip_msg(Entry(month=10, day=16, customer_name="顧客A", amount=1))])
    slip = deps.store.get_slip(handle_event(event("2026-10-17 01:00"), "message", deps)["slip_ids"][0])
    deps.tracker.tasks[slip.asana["task_gid"]]["override"] = "手動：決済確認済み"
    original = deps.tracker.reset_override
    deps.tracker.reset_override = lambda gid: (_ for _ in ()).throw(RuntimeError("Asana が失敗"))
    try:
        overrides.apply(deps)
    except Exception:
        pass
    assert deps.store.get_doc("meta", "asana_overrides") is None     # 読み取り位置を進めない
    deps.tracker.reset_override = original


# ---------------------------------------------------------------- 現金のまとめ入金・復元
def test_cash_report_matches_combined_transfer():
    deps = make_deps([slip_msg(Entry(month=10, day=3, customer_name="顧客A", amount=71700),
                               Entry(month=10, day=3, customer_name="顧客B", amount=8300)),
                      Parsed(kind="cash_deposit_report", confidence=0.9,
                             cash_deposit={"customer_names": ["顧客A", "顧客B"], "total_amount": None,
                                           "deposit_date_text": "今日"})],
                     [pay("c1", "2026-10-03 23:00", 71700, source="CASH"),
                      pay("c2", "2026-10-03 23:30", 8300, source="CASH")], [],
                     [BankTxn("t7", "2026-10-06", 80000, "振込 スタッフ イチロウ")])
    with_staff(deps, "スタッフ イチロウ")
    r = handle_event(event("2026-10-04 01:00", "E1"), "message", deps)
    assert handle_event(event("2026-10-06 10:00", "E2"), "message", deps)["status"] == "cash_reported"
    deposits.run(deps, now=datetime(2026, 10, 6, 18, 0, tzinfo=JST), force_mf=True)
    for sid in r["slip_ids"]:
        slip = deps.store.get_slip(sid)
        assert slip.status == "入金確認完了" and slip.closed_at


def test_restore_task_to_slip():
    from barnull_sales.restore import task_to_slip

    task = {"gid": "123", "created_at": "2026-10-17T05:41:53Z", "completed": True, "completed_at": "2026-10-18T01:00:00Z",
            "permalink_url": "https://app.asana.com/x/123",
            "notes": "伝票ID: S-20260928-001\n\nLINE の原文:\n日付：9/28\n顧客名：顧客Aさま\n\n処理履歴:\n- ...",
            "custom_fields": [
                {"name": "伝票ID", "resource_subtype": "text", "text_value": "S-20260928-001"},
                {"name": "伝票ステータス", "resource_subtype": "enum", "enum_value": {"name": "入金確認完了"}},
                {"name": "営業日", "resource_subtype": "date", "date_value": {"date": "2026-09-28"}},
                {"name": "伝票金額", "resource_subtype": "number", "number_value": 24750},
                {"name": "Square決済額", "resource_subtype": "number", "number_value": 24750},
                {"name": "Square payment ID", "resource_subtype": "text", "text_value": "pA, pB"},
                {"name": "Slackスレッド", "resource_subtype": "text",
                 "text_value": "https://x.slack.com/archives/CTEST/p1792214978376079"}]}
    slip = task_to_slip(task)
    assert slip.slip_id == "S-20260928-001" and slip.status == "入金確認完了" and slip.closed_at
    assert slip.payment_ids == ["pA", "pB"] and slip.raw_text == "日付：9/28\n顧客名：顧客Aさま"
    assert slip.slack == {"channel_id": "CTEST", "thread_ts": "1792214978.376079",
                          "permalink": "https://x.slack.com/archives/CTEST/p1792214978376079"}


def test_other_payment_releases_square_payment_for_the_right_slip():
    """取り違えた決済を「Square以外で支払い」で外すと、次の照合で正しい伝票に割り当てられる（2026-10-25 の事例）。"""
    deps = make_deps([slip_msg(Entry(month=10, day=23, customer_name="顧客A", amount=40000, unpaid_amount=10000)),
                      slip_msg(Entry(month=10, day=23, customer_name="顧客B", amount=30000))],
                     [pay("p1", "2026-10-24 05:10", 30000)])
    a = handle_event(event("2026-10-24 05:30", "E1"), "message", deps)["slip_ids"][0]
    b = handle_event(event("2026-10-24 05:31", "E2"), "message", deps)["slip_ids"][0]
    reconcile.run(deps, now=datetime(2026, 10, 24, 7, 0, tzinfo=JST))           # 両方に「決済の取り合い」
    task = deps.tracker.tasks[deps.store.get_slip(a).asana["task_gid"]]
    task["override"] = Manual.OTHER_PAYMENT
    overrides.apply(deps)
    reconcile.run(deps, now=datetime(2026, 10, 24, 7, 30, tzinfo=JST))
    sa, sb = deps.store.get_slip(a), deps.store.get_slip(b)
    assert sb.payment_ids == ["p1"] and sb.status == Status.PAID and not sb.open_alerts()
    assert not sa.payment_ids and sa.status == Status.DEPOSITED and not sa.open_alerts()
    assert sa.unpaid_remaining == 10000 and not sa.closed_at                     # 未収は残る
