"""銀行の入金の確認（Square の振込、現金、まとめ入金の報告）。"""

from __future__ import annotations

from datetime import date, datetime

from helpers import JST, SQUARE_TXN, event, make_deps, pay, payout, slip_msg, threads, with_staff

from barnull_sales import deposits
from barnull_sales.extract import Entry, Parsed
from barnull_sales.mf import BankTxn
from barnull_sales.process import handle_event


def run(deps, y, m, d, h=12):
    return deposits.run(deps, now=datetime(y, m, d, h, 0, tzinfo=JST), force_mf=True)


def card_slip_deps(txns, payouts=None):
    return make_deps([slip_msg(Entry(month=10, day=5, customer_name="顧客A", amount=100000))],
                     [pay("p1", "2026-10-06 05:28", 100000, fee=2500)],
                     payouts if payouts is not None else [payout("po1", 97500, "2026-10-08", ["p1"])], txns)


def test_card_payout_confirmed_and_slip_closed():
    deps = card_slip_deps([BankTxn("t1", "2026-10-07", 97500, SQUARE_TXN)])
    r = handle_event(event("2026-10-06 05:40"), "message", deps)
    stats = run(deps, 2026, 10, 8)
    slip = deps.store.get_slip(r["slip_ids"][0])
    assert stats["payouts_confirmed"] == 1
    assert slip.status == "入金確認完了" and slip.closed_at and slip.deposits["expected"][0]["share"] == 97500
    assert any(t.startswith("入金確認完了：10/07 Square振込 ¥97,500") for t in threads(deps, slip))
    assert any(t.startswith("完了：") for t in threads(deps, slip))
    assert deps.tracker.tasks[slip.asana["task_gid"]]["completed"]


def test_merged_payout_confirms_all_slips_in_it():
    deps = make_deps([slip_msg(Entry(month=9, day=28, customer_name="顧客A", amount=24750),
                               Entry(month=9, day=28, customer_name="顧客B", amount=57750))],
                     [pay("a", "2026-09-28 22:35", 24750, fee=618), pay("b", "2026-09-28 22:36", 57750, fee=1443)],
                     [payout("po9", 225183, "2026-09-29", ["a", "b", "x"])],
                     [BankTxn("t9", "2026-09-28", 225183, SQUARE_TXN)])
    r = handle_event(event("2026-09-29 00:20"), "message", deps)
    run(deps, 2026, 9, 29)
    for sid in r["slip_ids"]:
        slip = deps.store.get_slip(sid)
        assert slip.status == "入金確認完了"
        assert any("（他の伝票と合算）" in t for t in threads(deps, slip))
    assert deps.payouts.entry_calls == 1          # 振込の明細はキャッシュする


def test_wrong_amount_or_description_does_not_confirm_then_alert():
    deps = card_slip_deps([BankTxn("t1", "2026-10-07", 97000, SQUARE_TXN),
                           BankTxn("t2", "2026-10-07", 97500, "振込 ヤマダ タロウ")])
    r = handle_event(event("2026-10-06 05:40"), "message", deps)
    run(deps, 2026, 10, 8)
    assert deps.store.get_slip(r["slip_ids"][0]).status == "決済確認済み"
    assert deposits.deposit_alerts(deps, today=date(2026, 10, 13)) == 0     # 予定日 10/8 + 3営業日 = 10/14 まで猶予
    assert deposits.deposit_alerts(deps, today=date(2026, 10, 15)) == 1
    assert deposits.deposit_alerts(deps, today=date(2026, 10, 16)) == 0     # 同じアラートは繰り返さない
    assert deps.store.get_slip(r["slip_ids"][0]).status == "要確認"


def test_late_deposit_after_alert_closes_slip():
    """「入金未確認」の後で入金が見つかれば、アラートが閉じて完了する（要確認のまま止まらない）。"""
    deps = card_slip_deps([])
    r = handle_event(event("2026-10-06 05:40"), "message", deps)
    run(deps, 2026, 10, 8)
    deposits.deposit_alerts(deps, today=date(2026, 10, 15))
    deps.bank.txns.append(BankTxn("t1", "2026-10-14", 97500, SQUARE_TXN))   # マネフォへの反映が遅れた入金
    run(deps, 2026, 10, 15, 18)
    slip = deps.store.get_slip(r["slip_ids"][0])
    assert slip.status == "入金確認完了" and slip.closed_at and not slip.open_alerts()


def test_payout_not_created_yet_waits_then_alerts():
    deps = card_slip_deps([], payouts=[])
    r = handle_event(event("2026-10-06 05:40"), "message", deps)
    run(deps, 2026, 10, 6)
    assert not deps.store.get_slip(r["slip_ids"][0]).deposits.get("expected")
    assert deposits.deposit_alerts(deps, today=date(2026, 10, 9)) == 0
    assert deposits.deposit_alerts(deps, today=date(2026, 10, 16)) == 1
    assert any("振込が作られていません" in p["text"] for p in deps.notifier.posts)


def test_deposit_confirmation_resumes_after_claim_succeeded_but_save_failed():
    """銀行明細の割り当て（claim）の後で保存に失敗しても、次の照合で同じ明細を割り当て直せる。"""
    deps = card_slip_deps([BankTxn("t1", "2026-10-07", 97500, SQUARE_TXN)])
    r = handle_event(event("2026-10-06 05:40"), "message", deps)
    run(deps, 2026, 10, 6)                                      # 期待する入金を作る（まだ明細は見ない日付）
    deps.store.claim_deposit("t1", "payout:po1")                # 割り当てだけ済んで止まった状態
    run(deps, 2026, 10, 8)
    assert deps.store.get_slip(r["slip_ids"][0]).closed_at


def test_cash_confirmed_only_with_staff_names():
    entries = [Entry(month=10, day=2, customer_name="顧客C", amount=80000)]
    payments = [pay("c1", "2026-10-03 00:34", 80000, source="CASH")]
    txns = [BankTxn("t5", "2026-10-06", 80000, "振込 スタッフ イチロウ")]
    deps = make_deps([slip_msg(*entries)], payments, [], txns)
    r = handle_event(event("2026-10-03 01:00"), "message", deps)
    run(deps, 2026, 10, 6)
    assert deps.store.get_slip(r["slip_ids"][0]).status == "決済確認済み"     # 名義の一覧が無いので照合しない

    deps = make_deps([slip_msg(*entries)], payments, [], txns)
    with_staff(deps, "スタッフ イチロウ")
    r = handle_event(event("2026-10-03 01:00"), "message", deps)
    run(deps, 2026, 10, 6)
    slip = deps.store.get_slip(r["slip_ids"][0])
    assert slip.status == "入金確認完了" and slip.closed_at


def test_unpaid_slip_is_not_closed_after_deposit():
    deps = make_deps([slip_msg(Entry(month=10, day=10, customer_name="顧客A", amount=100000, unpaid_amount=40000))],
                     [pay("p1", "2026-10-11 02:14", 60000, fee=1500)], [payout("po2", 58500, "2026-10-13", ["p1"])],
                     [BankTxn("t3", "2026-10-12", 58500, SQUARE_TXN)])
    r = handle_event(event("2026-10-11 02:30"), "message", deps)
    run(deps, 2026, 10, 13)
    slip = deps.store.get_slip(r["slip_ids"][0])
    assert slip.status == "入金確認完了" and not slip.closed_at          # 未収が残るので完了にしない


def test_mf_fetch_is_throttled():
    deps = card_slip_deps([])
    handle_event(event("2026-10-06 05:40"), "message", deps)
    s1 = deposits.run(deps, now=datetime(2026, 10, 8, 12, 0, tzinfo=JST))
    s2 = deposits.run(deps, now=datetime(2026, 10, 8, 12, 30, tzinfo=JST))
    assert "bank_incomes" in s1 and "bank_incomes" not in s2


def test_cash_report_needs_staff_names():
    """担当者の名義が無いと、顧客の振込を現金の入金と取り違えるおそれがあるので照合しない。"""
    deps = make_deps([slip_msg(Entry(month=10, day=3, customer_name="顧客A", amount=71700)),
                      Parsed(kind="cash_deposit_report", confidence=0.9,
                             cash_deposit={"customer_names": ["顧客A"], "total_amount": None, "deposit_date_text": "今日"})],
                     [pay("c1", "2026-10-03 23:00", 71700, source="CASH")], [],
                     [BankTxn("t7", "2026-10-06", 71700, "振込 ヤマダ タロウ")])
    r = handle_event(event("2026-10-04 01:00", "E1"), "message", deps)
    assert handle_event(event("2026-10-06 10:00", "E2"), "message", deps)["status"] == "cash_reported"
    run(deps, 2026, 10, 6, 18)
    assert not deps.store.get_slip(r["slip_ids"][0]).closed_at
