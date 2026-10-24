"""Square の決済との突合（起票直後と、30分ごとのジョブ）。伝票の無い決済・返金の通知もここ。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from . import bizdate, unpaid
from .deps import Deps
from .errors import ItemErrors
from .matching import describe, find_match, orphan_due, search_window
from .slip_ops import post, raise_alert, resolve_alerts, save
from .slips import PAYMENT_ALERTS, Alert, Slip, Status, now_iso
from .square import Payment
from .texts import yen


def _sent(slip: Slip) -> datetime:
    return datetime.fromisoformat(slip.sent_at)


def _window(deps: Deps, slip: Slip) -> tuple[datetime, datetime]:
    s = deps.settings
    return search_window(slip.business_date, _sent(slip), s.tz, s.day_start)


def _fetch(deps: Deps, slips: list[Slip], now: datetime, days_back: float) -> list[Payment]:
    begin = min([_window(deps, x)[0] for x in slips] + [now - timedelta(days=days_back)]) - timedelta(days=1)
    return deps.payments.list_payments(begin, now)


def run(deps: Deps, now: datetime | None = None) -> dict:
    """30分ごと: 決済が未確認の伝票の突合、未収の Square での回収、返金・伝票の無い決済の通知。"""
    now = now or datetime.now(UTC)
    sq = deps.settings.square
    retry = timedelta(days=sq.payment_retry_days)
    slips = [x for x in deps.store.find_slips("status", "in", [Status.DRAFTED, Status.REVIEW])
             if x.needs_payment and now - _sent(x) <= retry]
    payments = _fetch(deps, slips, now, days_back=3)
    claims = deps.store.payment_claims([p.id for p in payments])
    errs = ItemErrors("reconcile")
    stats = match(deps, slips, payments, claims, now, alert_missing=True, errs=errs)
    stats["unpaid_square"] = unpaid.collect_by_square(deps, payments, claims, errs)
    stats["refunds"] = check_refunds(deps, payments, claims, errs)
    stats["orphans_notified"] = notify_orphans(deps, payments, claims, now)
    stats["orphans_resolved"] = resolve_orphans(deps)
    errs.raise_if_any()
    return stats


def match_now(deps: Deps, slips: list[Slip]) -> dict:
    """起票・訂正の直後に1回だけ突合する（見つからなくても「決済未確認」は出さない。以後は30分ごとのジョブが探す）。"""
    slips = [x for x in slips if x.needs_payment]
    if not slips or not deps.payments:
        return {}
    now = datetime.now(UTC)
    payments = _fetch(deps, slips, now, days_back=0)
    claims = deps.store.payment_claims([p.id for p in payments])
    errs = ItemErrors("match_now")
    stats = match(deps, slips, payments, claims, now, alert_missing=False, errs=errs)
    errs.raise_if_any()
    return stats


def match(deps: Deps, slips: list[Slip], payments: list[Payment], claims: dict[str, str], now: datetime, *,
          alert_missing: bool, errs: ItemErrors) -> dict:
    stats = {"open": len(slips), "payments": len(payments)}
    for slip in sorted(slips, key=lambda x: x.sent_at):
        with errs.item(slip_id=slip.slip_id):
            r = try_match(deps, slip, payments, claims, now, slips, alert_missing)
            stats[r] = stats.get(r, 0) + 1
    return stats


def try_match(deps: Deps, slip: Slip, payments: list[Payment], claims: dict[str, str], now: datetime,
              open_slips: list[Slip], alert_missing: bool) -> str:
    s, sq = deps.settings, deps.settings.square
    expected = slip.expected_square_amount
    if not slip.needs_payment:
        return "skip"
    # 他の伝票に割り当て済みの決済は使わない（この伝票への割り当てが途中で止まった決済は使ってよい）
    blocked = {pid for pid, sid in claims.items() if sid != slip.slip_id}
    core = _window(deps, slip)
    same = sum(1 for o in open_slips if o.needs_payment and o.expected_square_amount == expected
               and _window(deps, o) == core)
    # まず営業日（6:00〜翌6:00）の中を探し、無ければ翌日の朝まで広げて探す（営業日と決済の日がずれることがあるため。
    # 先に営業日の中を探すのは、翌日の別の伝票の決済を取らないように）
    windows = [core]
    if slip.business_date and sq.payment_window_extra_days > 0:
        windows.append((core[0], core[1] + timedelta(days=sq.payment_window_extra_days)))
    for i, window in enumerate(windows):
        widest = i == len(windows) - 1
        neighbors = [p for p in payments if window[0] - timedelta(days=1) <= p.created_at < window[1] + timedelta(days=1)]
        result = find_match(expected, payments, blocked, window, split_max=sq.split_max,
                            split_span_min=sq.split_span_min, neighbor_payments=neighbors if widest else None,
                            same_amount_open_slips=max(same, 1))
        if result.kind != "not_found":
            break
    if result.kind == "matched":
        if not apply_match(deps, slip, result.payments, result.method):
            return "claim_conflict"
        claims.update({p.id: slip.slip_id for p in result.payments})
        return "matched"
    if result.kind == "ambiguous":
        lines = [f"・{yen(sum(p.amount for p in c))}（{describe(c, s.tz)}）" for c in result.candidates]
        raise_alert(deps, slip, Alert.AMBIGUOUS,
                    "次の候補から決められません。Asana の「手動上書き」で決めてください。\n" + "\n".join(lines))
        return "ambiguous"
    if result.kind == "date_shift":
        lines = [f"・{yen(c[0].amount)}（{describe(c, s.tz)}）" for c in result.candidates]
        raise_alert(deps, slip, Alert.DATE_SHIFT, "指定の営業日の外に、金額が一致する決済があります。\n" + "\n".join(lines))
        return "date_shift"
    if slip.unpaid_amount and slip.slip_amount:
        # 未収分はその日の Square に無い前提。伝票金額の全額の決済があれば、前提と違うので知らせる
        full = find_match(slip.slip_amount, payments, blocked, window, split_max=sq.split_max,
                          split_span_min=sq.split_span_min)
        if full.kind in ("matched", "ambiguous"):
            found = full.payments or full.candidates[0]
            raise_alert(deps, slip, Alert.UNPAID_MISMATCH,
                        f"Square に伝票金額の全額 {yen(slip.slip_amount)} の決済があります（{describe(found, s.tz)}）。"
                        f"未収 {yen(slip.unpaid_amount)} の前提と違います。未収が無かったなら @Bot で訂正してください",
                        important=True)
            return "unpaid_mismatch"
    if alert_missing and now - _sent(slip) >= timedelta(minutes=sq.payment_grace_min):
        rival = next((p for p in payments if p.usable and p.amount == expected and window[0] <= p.created_at < window[1]
                      and claims.get(p.id) not in (None, slip.slip_id)), None)
        if rival:
            contested(deps, slip, rival, claims[rival.id])
            return "contested"
        raise_alert(deps, slip, Alert.PAYMENT_MISSING,
                    f"送信から{int(sq.payment_grace_min)}分たっても、Square に {yen(expected)} の決済が見つかりません"
                    "（会計を受け取っていない可能性があります）", important=True)
    return "not_found"


def contested(deps: Deps, slip: Slip, payment: Payment, other_id: str) -> None:
    """同じ金額の決済が1件しか無く、別の伝票に割り当て済み。どちらの伝票の決済か、人に決めてもらう（両方の伝票に出す）。"""
    tz = deps.settings.tz
    what = f"{yen(payment.amount)}（{describe([payment], tz)}）"
    raise_alert(deps, slip, Alert.PAYMENT_CONTESTED,
                f"同じ時間帯に {what} の決済が1件だけあり、伝票 {other_id} に割り当て済みです。"
                "どちらの伝票の決済かを確かめ、Asana の「手動上書き」で決めてください", important=True)
    other = deps.store.get_slip(other_id)
    if other and not other.closed_at:
        raise_alert(deps, other, Alert.PAYMENT_CONTESTED,
                    f"この伝票に割り当てた {what} の決済は、同じ金額の伝票 {slip.slip_id} の決済かもしれません"
                    "（決済は1件だけ）。どちらの伝票の決済かを確かめてください")


def apply_match(deps: Deps, slip: Slip, payments: list[Payment], method: str) -> bool:
    if not deps.store.claim_payments(slip.slip_id, [p.id for p in payments]):
        return False
    tz = deps.settings.tz
    square = {
        "payment_ids": [p.id for p in payments], "payments": [p.record() for p in payments],
        "amount": sum(p.amount for p in payments), "fee": sum(p.fee or 0 for p in payments),
        "methods": sorted({p.source_type for p in payments}), "summary": describe(payments, tz),
        "match_method": method, "matched_at": now_iso(),
    }

    def fn(s: Slip) -> None:
        s.square = dict(square)
        resolve_alerts(s, *PAYMENT_ALERTS, Alert.PAYMENT_CONTESTED)   # 自分の決済が見つかった

    save(deps, slip, fn, "Square 決済を確認")
    fee_text = f"手数料 {yen(square['fee'])}" if square["fee"] else "手数料なし"
    split = f"（{len(payments)}件の合計。割り勘とみなした）" if method == "split" else ""
    post(deps, slip, f"決済確認済み：Square {yen(square['amount'])}（{square['summary']}）{fee_text}{split}")
    return True


def check_refunds(deps: Deps, payments: list[Payment], claims: dict[str, str], errs: ItemErrors) -> int:
    """割り当て済みの決済に返金があれば、決済ごとに1回だけ知らせる。"""
    n = 0
    for p in payments:
        if p.refunded <= 0 or p.id not in claims:
            continue
        with errs.item(slip_id=claims[p.id]):
            slip = deps.store.get_slip(claims[p.id])
            if not slip or p.id in slip.square.get("refunds_seen", []):
                continue
            save(deps, slip, lambda s, pid=p.id: s.square.setdefault("refunds_seen", []).append(pid), "返金を記録",
                 notify=False)
            text = f"割り当て済みの決済（{p.id}）に {yen(p.refunded)} の返金がありました"
            if not raise_alert(deps, slip, Alert.REFUND, text, important=True):   # 2件目の返金も知らせる
                post(deps, slip, f"{deps.settings.mention}★{Alert.REFUND}：{text}", broadcast=True)
            n += 1
    return n


def notify_orphans(deps: Deps, payments: list[Payment], claims: dict[str, str], now: datetime) -> int:
    """伝票の無い決済を、決済ごとに1回だけチャンネルに知らせる。"""
    s, sq = deps.settings, deps.settings.square
    since = sq.orphan_since
    n = 0
    for p in payments:
        if not p.usable or p.id in claims or (since and p.created_at < since):
            continue
        if not orphan_due(p, now, s.tz, s.day_start, sq.orphan_grace_hours) or deps.store.get_doc("orphan_payments", p.id):
            continue
        t = bizdate.hm(p.created_at, s.tz)
        ts = deps.notifier.post(
            s.slack_channel,
            f"{s.mention}【伝票なし】Square の決済に対応する伝票がありません\n"
            f"{t}　{yen(p.amount)}（{p.method_label}）　payment: {p.id}\n"
            "@Bot の付け忘れの可能性があります。伝票メッセージを送ると自動で割り当てます。")
        deps.store.set_doc("orphan_payments", p.id, {"notified_at": now_iso(), "channel": s.slack_channel, "ts": ts,
                                                     "amount": p.amount, "resolved_at": None})
        n += 1
    return n


def resolve_orphans(deps: Deps) -> int:
    """あとから伝票に割り当てられた「伝票なし」の決済に、通知のスレッドで「解消」と返す。"""
    pending = deps.store.find_docs("orphan_payments", "resolved_at", "==", None)
    claims = deps.store.payment_claims(list(pending))
    n = 0
    for pid, rec in pending.items():
        if pid not in claims:
            continue
        deps.notifier.post(rec["channel"], f"解消：伝票 {claims[pid]} に割り当てました", thread_ts=rec["ts"])
        deps.store.set_doc("orphan_payments", pid, {**rec, "resolved_at": now_iso(), "slip_id": claims[pid]})
        n += 1
    return n
