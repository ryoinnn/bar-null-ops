"""Square の決済との突合、伝票の無い決済・返金の通知。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from helpers import JST, SIX, event, make_deps, pay, slip_msg, threads

from barnull_sales import reconcile
from barnull_sales.extract import Entry
from barnull_sales.matching import find_match, search_window
from barnull_sales.process import handle_event
from barnull_sales.slip_ops import save


def window(d):
    return search_window(d, datetime(2026, 10, 11, 3, 0, tzinfo=JST), JST, SIX)


# ---------------------------------------------------------------- 照合の関数
def test_single_match_inside_business_day():
    ps = [pay("a", "2026-10-11 02:14", 60000), pay("b", "2026-10-11 07:00", 60000)]   # b は翌営業日
    r = find_match(60000, ps, set(), window("2026-10-10"))
    assert r.kind == "matched" and [p.id for p in r.payments] == ["a"]


def test_excludes_claimed_failed_and_refunded():
    ps = [pay("a", "2026-10-10 22:00", 60000), pay("b", "2026-10-10 23:00", 60000, status="FAILED"),
          pay("c", "2026-10-10 23:30", 60000, refunded=60000)]
    assert find_match(60000, ps, {"a"}, window("2026-10-10")).kind == "not_found"


def test_split_payment():
    ps = [pay("a", "2026-10-11 01:00", 30000), pay("b", "2026-10-11 01:05", 30000), pay("c", "2026-10-11 01:10", 40000),
          pay("d", "2026-10-10 20:00", 5000)]
    r = find_match(100000, ps, set(), window("2026-10-10"))
    assert r.kind == "matched" and r.method == "split" and {p.id for p in r.payments} == {"a", "b", "c"}


def test_split_outside_span_is_not_matched():
    ps = [pay("a", "2026-10-10 20:00", 50000), pay("b", "2026-10-11 01:00", 50000)]
    assert find_match(100000, ps, set(), window("2026-10-10")).kind == "not_found"


def test_two_same_amount_candidates_are_ambiguous_unless_two_open_slips():
    ps = [pay("a", "2026-10-04 05:59", 8500), pay("b", "2026-10-04 05:58", 8500)]
    w = window("2026-10-03")
    assert find_match(8500, ps, set(), w).kind == "ambiguous"
    r = find_match(8500, ps, set(), w, same_amount_open_slips=2)
    assert r.kind == "matched" and r.payments[0].id == "b"          # 早い方から


def test_date_shift():
    ps = [pay("a", "2026-10-12 01:00", 60000)]
    assert find_match(60000, ps, set(), window("2026-10-10"), neighbor_payments=ps).kind == "date_shift"


def test_no_date_searches_two_business_days_back():
    sent = datetime(2026, 10, 12, 1, 0, tzinfo=JST)      # 営業日 10/11
    start, end = search_window(None, sent, JST, SIX)
    assert start == datetime(2026, 10, 9, 6, 0, tzinfo=JST) and end == sent + timedelta(hours=3)


# ---------------------------------------------------------------- 起票から突合まで
def test_slip_is_matched_right_after_creation():
    deps = make_deps([slip_msg(Entry(month=10, day=5, customer_name="顧客A", amount=100000))],
                     [pay("p1", "2026-10-06 05:28", 100000, fee=2500, brand="AMERICAN_EXPRESS")])
    r = handle_event(event("2026-10-06 05:40"), "message", deps)
    slip = deps.store.get_slip(r["slip_ids"][0])
    assert slip.status == "決済確認済み" and slip.payment_ids == ["p1"] and slip.square["fee"] == 2500
    assert any(t.startswith("決済確認済み：Square ¥100,000") for t in threads(deps, slip))
    assert deps.tracker.tasks[slip.asana["task_gid"]]["status"] == "決済確認済み"


def test_unpaid_part_is_deducted_before_matching():
    deps = make_deps([slip_msg(Entry(month=10, day=10, customer_name="顧客A", amount=100000, unpaid_amount=40000))],
                     [pay("p1", "2026-10-11 02:14", 60000)])
    r = handle_event(event("2026-10-11 02:30"), "message", deps)
    assert deps.store.get_slip(r["slip_ids"][0]).square["amount"] == 60000


def test_full_amount_paid_despite_unpaid_note_is_flagged():
    deps = make_deps([slip_msg(Entry(month=9, day=28, customer_name="顧客B", amount=57750, unpaid_amount=40000))],
                     [pay("p1", "2026-09-28 22:35", 57750)])
    r = handle_event(event("2026-09-29 00:20"), "message", deps)
    reconcile.run(deps, now=datetime(2026, 9, 29, 1, 30, tzinfo=JST))
    slip = deps.store.get_slip(r["slip_ids"][0])
    assert slip.status == "要確認" and not slip.square
    assert any("金額不一致" in p["text"] and "全額" in p["text"] for p in deps.notifier.posts)


def test_payment_missing_alert_after_grace_then_resolved():
    deps = make_deps([slip_msg(Entry(month=10, day=10, customer_name="顧客A", amount=77000))])
    r = handle_event(event("2026-10-11 01:00"), "message", deps)
    slip_id = r["slip_ids"][0]
    sent = datetime(2026, 10, 11, 1, 0, tzinfo=JST)
    reconcile.run(deps, now=sent + timedelta(minutes=30))
    assert deps.store.get_slip(slip_id).status == "起票済み"           # まだ猶予内
    reconcile.run(deps, now=sent + timedelta(minutes=61))
    assert deps.store.get_slip(slip_id).status == "要確認"
    alert = [p for p in deps.notifier.posts if "決済未確認" in p["text"]]
    assert len(alert) == 1 and alert[0]["broadcast"] and "<@UALERT>" in alert[0]["text"]
    reconcile.run(deps, now=sent + timedelta(minutes=91))              # 同じアラートは繰り返さない
    assert len([p for p in deps.notifier.posts if "決済未確認" in p["text"]]) == 1
    deps.payments.payments.append(pay("late", "2026-10-11 01:50", 77000))
    reconcile.run(deps, now=sent + timedelta(hours=2))
    slip = deps.store.get_slip(slip_id)
    assert slip.status == "決済確認済み" and not slip.open_alerts()


def test_two_customers_each_get_their_own_payment():
    deps = make_deps([slip_msg(Entry(month=9, day=28, customer_name="顧客A", amount=24750),
                               Entry(month=9, day=28, customer_name="顧客B", amount=57750))],
                     [pay("p1", "2026-09-29 00:05", 57750), pay("p2", "2026-09-29 00:06", 24750)])
    r = handle_event(event("2026-09-29 00:20"), "message", deps)
    a, b = (deps.store.get_slip(x) for x in r["slip_ids"])
    assert a.payment_ids == ["p2"] and b.payment_ids == ["p1"]


def test_match_resumes_after_claim_succeeded_but_save_failed():
    """決済の割り当て（claim）の後で保存に失敗しても、次の突合で同じ決済を割り当て直せる。"""
    deps = make_deps([slip_msg(Entry(month=10, day=5, customer_name="顧客A", amount=100000))])
    r = handle_event(event("2026-10-06 05:40"), "message", deps)
    slip_id = r["slip_ids"][0]
    deps.store.claim_payments(slip_id, ["p1"])          # 割り当てだけ済んで、伝票の保存の前に止まった状態
    deps.payments.payments.append(pay("p1", "2026-10-06 05:28", 100000))
    reconcile.run(deps, now=datetime(2026, 10, 6, 6, 0, tzinfo=JST))
    assert deps.store.get_slip(slip_id).payment_ids == ["p1"]
    assert not [p for p in deps.notifier.posts if "伝票なし" in p["text"]]


def test_one_failing_slip_does_not_stop_the_others():
    deps = make_deps([slip_msg(Entry(month=10, day=5, customer_name="顧客A", amount=1000),
                               Entry(month=10, day=5, customer_name="顧客B", amount=2000))])
    r = handle_event(event("2026-10-06 05:40"), "message", deps)
    deps.payments.payments += [pay("p1", "2026-10-06 05:28", 1000), pay("p2", "2026-10-06 05:29", 2000)]
    original = deps.tracker.update_task

    def flaky(slip):
        if slip.slip_id == r["slip_ids"][0]:
            raise RuntimeError("Asana が一時的に失敗")
        original(slip)

    deps.tracker.update_task = flaky
    reconcile.run(deps, now=datetime(2026, 10, 6, 6, 0, tzinfo=JST))     # 反映の失敗は sync_pending にして続ける
    a, b = (deps.store.get_slip(x) for x in r["slip_ids"])
    assert a.payment_ids == ["p1"] and a.sync_pending and b.payment_ids == ["p2"] and not b.sync_pending


def test_orphan_payment_is_notified_once_and_resolved_later():
    p = pay("orph", "2026-10-11 02:00", 33000)          # 営業日 10/10 → 10/11 06:00 に終わる
    deps = make_deps([slip_msg(Entry(month=10, day=10, customer_name="顧客A", amount=33000))], [p])
    reconcile.run(deps, now=datetime(2026, 10, 11, 11, 0, tzinfo=JST))     # 猶予6時間の前
    assert not [x for x in deps.notifier.posts if "伝票なし" in x["text"]]
    reconcile.run(deps, now=datetime(2026, 10, 11, 12, 1, tzinfo=JST))
    reconcile.run(deps, now=datetime(2026, 10, 11, 12, 31, tzinfo=JST))
    orphan = [x for x in deps.notifier.posts if "伝票なし" in x["text"]]
    assert len(orphan) == 1
    handle_event(event("2026-10-11 13:00", "E9"), "message", deps)          # あとから伝票が届くと割り当てられる
    reconcile.run(deps, now=datetime(2026, 10, 11, 13, 1, tzinfo=JST))
    assert any(x["thread_ts"] == orphan[0]["ts"] and x["text"].startswith("解消") for x in deps.notifier.posts)


def test_orphan_ignores_payments_before_start_and_refunded():
    deps = make_deps([], [pay("old", "2026-09-20 22:00", 1000), pay("ref", "2026-10-10 22:00", 2000, refunded=2000)])
    reconcile.run(deps, now=datetime(2026, 10, 12, 12, 0, tzinfo=JST))
    assert not [x for x in deps.notifier.posts if "伝票なし" in x["text"]]


def test_refund_is_alerted_once_per_payment_even_after_resolved():
    p = pay("p1", "2026-10-06 05:28", 100000)
    deps = make_deps([slip_msg(Entry(month=10, day=5, customer_name="顧客A", amount=100000))], [p])
    r = handle_event(event("2026-10-06 05:40"), "message", deps)
    p.refunded = 100000
    now = datetime(2026, 10, 6, 9, 0, tzinfo=JST).astimezone(UTC)
    reconcile.run(deps, now=now)
    slip = deps.store.get_slip(r["slip_ids"][0])
    assert slip.status == "要確認" and len([x for x in deps.notifier.posts if "返金検知" in x["text"]]) == 1
    from barnull_sales.slip_ops import resolve_alerts
    save(deps, slip, lambda s: resolve_alerts(s), "人が確認")
    reconcile.run(deps, now=now + timedelta(minutes=30))
    assert len([x for x in deps.notifier.posts if "返金検知" in x["text"]]) == 1
