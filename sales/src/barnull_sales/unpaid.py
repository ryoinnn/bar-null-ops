"""未収（後払い）の回収: 後日の Square 決済・顧客からの振込の割り当て、回収の記録、リマインド。"""

from __future__ import annotations

from datetime import UTC, date, datetime

from . import bizdate
from .deps import Deps
from .errors import ItemErrors
from .slip_ops import post, raise_alert, save
from .slips import Alert, Slip, Unpaid, now_iso
from .square import Payment
from .texts import esc, norm_text, unpaid_rest, who, yen


def open_slips(deps: Deps) -> list[Slip]:
    return [x for x in deps.store.find_slips("unpaid_status", "in", [Unpaid.OPEN, Unpaid.REPORTED])
            if not x.closed_at and x.unpaid_remaining > 0]


def record_collection(deps: Deps, slip: Slip, amount: int, entry: dict, label: str) -> None:
    """回収（着金）を記録する。未収残高が0になれば「回収入金確認済み」（状態は derive が決める）。"""
    def fn(s: Slip) -> None:
        if entry.get("txn_id") and any(r.get("txn_id") == entry["txn_id"] for r in s.collection_reports):
            return
        s.unpaid_remaining = max(s.unpaid_remaining - amount, 0)
        for r in s.collection_reports:
            if r.get("type") == "report":   # 回収の報告は、着金をもって確認済み
                r["confirmed"] = True
        s.collection_reports.append({**entry, "amount": amount, "at": now_iso(), "confirmed": True})

    save(deps, slip, fn, "未収の回収を確認")
    post(deps, slip, f"未収 回収入金確認：{label} {yen(amount)}{unpaid_rest(slip.unpaid_remaining)}")


def confirm_square_collections(s: Slip) -> int:
    """Square で回収した分の入金が確認できたとき（deposits から、save の fn の中で呼ぶ）。確認した金額を返す。"""
    total = 0
    for r in s.collection_reports:
        if r.get("type") == "square" and not r.get("confirmed"):
            r["confirmed"] = True
            total += r["amount"]
    s.unpaid_remaining = max(s.unpaid_remaining - total, 0)
    return total


def collect_by_square(deps: Deps, payments: list[Payment], claims: dict[str, str], errs: ItemErrors) -> int:
    """未収残高と同じ金額の、後日の Square 決済が1件だけあれば割り当てる（入金が確認できたら回収済み）。"""
    s = deps.settings
    n = 0
    for slip in open_slips(deps):
        if not slip.business_date or slip.needs_payment:   # その日の決済が見つかるまでは、回収の決済と区別できない
            continue
        with errs.item(slip_id=slip.slip_id):
            _, after = bizdate.business_window(date.fromisoformat(slip.business_date), s.tz, s.day_start)
            hits = [p for p in payments if p.usable
                    and claims.get(p.id) in (None, slip.slip_id) and p.id not in slip.payment_ids
                    and p.amount == slip.unpaid_remaining and p.created_at >= after]
            if len(hits) != 1 or not deps.store.claim_payments(slip.slip_id, [hits[0].id]):
                continue
            p = hits[0]
            claims[p.id] = slip.slip_id

            def fn(x: Slip, p=p) -> None:
                if p.id in x.payment_ids:
                    return
                x.square = {**x.square, "payment_ids": x.payment_ids + [p.id],
                            "payments": list(x.square.get("payments") or []) + [p.record(unpaid_collection=True)]}
                x.collection_reports.append({"type": "square", "payment_id": p.id, "amount": p.amount,
                                             "at": now_iso(), "confirmed": False})
                x.deposits = {**x.deposits, "completed_at": None, "manual": False}   # 回収分の入金をこれから確かめる

            save(deps, slip, fn, "未収を Square で回収")
            t = bizdate.hm(p.created_at, s.tz)
            post(deps, slip, f"未収 回収（Square）：{t} {p.method_label} {yen(p.amount)} を割り当てました → 着金確認待ち")
            n += 1
    return n


def collect_by_bank(deps: Deps, txns, claims: dict[str, str], exclude: list[str], errs: ItemErrors) -> int:
    """顧客名義の振込が未収と同じ金額なら回収済みにする（名義と顧客名が同じ人かは Claude で判定し、結果を保存する）。

    exclude: Square の振込や担当者の名義など、顧客からの振込ではない明細の目印（正規化済み）。
    """
    threshold = deps.settings.followup.payer_match_confidence
    n = 0
    for slip in open_slips(deps):
        if not slip.business_date:
            continue
        with errs.item(slip_id=slip.slip_id):
            ref = f"unpaid:{slip.slip_id}"
            reported = [r["amount"] for r in slip.collection_reports if r.get("type") == "report" and not r.get("confirmed")]
            amounts = {slip.unpaid_remaining, *reported}
            done = {r.get("txn_id") for r in slip.collection_reports}
            for t in txns:
                text = norm_text(t.content)
                if (claims.get(t.id) not in (None, ref) or t.id in done or t.amount not in amounts
                        or t.date < slip.business_date or any(x in text for x in exclude)):
                    continue
                check = slip.payer_checks.get(t.id)
                if check is None:
                    same, conf = deps.extractor.judge_payer(t.content, slip.customer_name or "")
                    check = {"same": same, "confidence": conf, "alerted": False}
                    save(deps, slip, lambda s, k=t.id, c=check: s.payer_checks.__setitem__(k, dict(c)), "振込名義を判定",
                         notify=False)
                if not (check["same"] and check["confidence"] >= threshold):
                    if not check.get("alerted"):
                        raise_alert(deps, slip, Alert.PAYER_UNCLEAR,
                                    f"{t.date} 「{esc(t.content)}」{yen(t.amount)} が未収の回収か判断できません"
                                    f"（名義の判定 {check['confidence']:.2f}）")
                        save(deps, slip, lambda s, k=t.id: s.payer_checks[k].__setitem__("alerted", True),
                             "判定の通知を記録", notify=False)
                    continue
                if not deps.store.claim_deposit(t.id, ref):
                    continue
                claims[t.id] = ref
                record_collection(deps, slip, t.amount, {"type": "bank", "txn_id": t.id},
                                  f"{t.date} 振込「{esc(t.content)}」")
                n += 1
                break
    return n


def reminders(deps: Deps, now: datetime | None = None) -> int:
    """起票日から一定日数ごとに、未収の伝票のスレッドでリマインドする（毎日 10:00）。"""
    now = now or datetime.now(UTC)
    interval = deps.settings.followup.unpaid_reminder_interval_days
    errs = ItemErrors("unpaid_reminders")
    n = 0
    for slip in open_slips(deps):
        with errs.item(slip_id=slip.slip_id):
            days = (now - datetime.fromisoformat(slip.created_at)).total_seconds() / 86400
            if days < interval * (slip.reminder_count + 1):
                continue
            # 先に回数を保存する（投稿に失敗しても、毎日同じリマインドを繰り返さないように）
            save(deps, slip, lambda s: setattr(s, "reminder_count", s.reminder_count + 1), "未収リマインド", notify=False)
            state = "回収報告済み・着金待ち" if slip.unpaid_status == Unpaid.REPORTED else "未回収"
            post(deps, slip, f"{deps.settings.mention}未収リマインド（{int(days)}日経過）：{who(slip.customer_name)}様 "
                             f"未収残高 {yen(slip.unpaid_remaining)}／状況：{state}／担当：{who(slip.sender_display_name)}")
            n += 1
    errs.raise_if_any()
    return n
