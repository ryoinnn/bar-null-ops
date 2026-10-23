"""入金の確認: Square の振込と現金の入金を、マネフォの銀行明細と照合する（30分ごと。マネフォは3時間に1回だけ読む）。

- カード等: 決済を含む Square の振込（payout）を特定し、振込額と銀行の入金を照合する。
  振込は複数の決済の合算なので、1つの入金で、その振込に含まれるすべての伝票を確認済みにする。
- 現金: 担当者の個人口座からの振込（名義の一覧が設定されているときだけ）。まとめて入金した報告の合計とも照合する。
- 未収: 顧客名義の振込（unpaid.collect_by_bank）。

期待する入金（deposits.expected）は、伝票の決済の組み合わせ（built_for）が変わるたびに作り直す。
作り直しても、確認済みの入金（confirmed）は引き継ぐ。
銀行明細の割り当て（deposit_claims）は「payout:振込ID」などの ref ごと。同じ ref なら何度でも照合し直せる。
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta

from . import bizdate, unpaid
from .deps import Deps
from .errors import ItemErrors
from .slip_ops import post, raise_alert, resolve_alerts, save
from .slips import Alert, Slip, Status, now_iso
from .texts import norm_text, unpaid_rest, yen

log = logging.getLogger(__name__)


def payments_of(slip: Slip) -> list[dict]:
    sq = slip.square
    if sq.get("payments"):
        return sq["payments"]
    # Phase 3 より前に割り当てた伝票（支払手段は伝票全体で1種類とみなす）
    st = (sq.get("methods") or ["CARD"])[0]
    return [{"id": pid, "amount": None, "fee": 0, "source_type": st, "paid_at": (sq.get("paid_at") or [None])[0]}
            for pid in slip.payment_ids]


def entry_key(e: dict) -> str:
    return e.get("key") or (f"payout:{e['payout_id']}" if e["kind"] == "square_payout" else "cash")


def build_expected(slip: Slip, payout_index: dict[str, dict]) -> list[dict] | None:
    """期待する入金を作る。振込がまだ作られていない決済があれば None（次回に回す）。
    確認済みの入金は、同じ振込・同じ金額のときだけ引き継ぐ（現金に後から決済が足された場合などは確かめ直す）。"""
    old = {entry_key(e): e for e in slip.deposits.get("expected") or []}
    out: dict[str, dict] = {}
    for p in payments_of(slip):
        share = (p["amount"] - (p.get("fee") or 0)) if p["amount"] is not None \
            else slip.square.get("amount", 0) - (slip.square.get("fee") or 0)
        if p["source_type"] == "CASH":
            # その日の現金はまとめて1件。後日の未収の回収は別の振込になるので、決済ごとに分ける
            key = f"cash:{p['id']}" if p.get("unpaid_collection") else "cash"
            e = out.setdefault(key, {"key": key, "kind": "cash", "amount": 0, "share": 0, "paid_at": p["paid_at"]})
            e["amount"] += share
            e["share"] += share
            continue
        po = payout_index.get(p["id"])
        if not po:
            return None
        key = f"payout:{po['id']}"
        e = out.setdefault(key, {"key": key, "kind": "square_payout", "payout_id": po["id"], "amount": po["amount"],
                                 "arrival_date": po["arrival_date"], "share": 0})
        e["share"] += share
    for key, e in out.items():
        prev = old.get(key) or {}
        e["confirmed"] = prev.get("confirmed") if prev.get("amount") == e["amount"] else None
    return list(out.values())


def payout_index(deps: Deps, slips: list[Slip]) -> dict[str, dict]:
    """決済 ID → 振込。振込の明細は Firestore にキャッシュする（送金済みの振込の中身は変わらない）。"""
    paid = [datetime.fromisoformat(p["paid_at"]) for s in slips for p in payments_of(s)
            if p["source_type"] != "CASH" and p.get("paid_at")]
    if not paid:
        return {}
    index: dict[str, dict] = {}
    for po in deps.payouts.list_payouts(min(paid) - timedelta(days=2)):
        if po.get("status") not in ("SENT", "PAID"):
            continue
        cached = deps.store.get_doc("payouts", po["id"])
        if not cached:
            cached = {"id": po["id"], "amount": (po.get("amount_money") or {}).get("amount", 0),
                      "arrival_date": po.get("arrival_date"), "payment_ids": deps.payouts.payment_ids(po["id"])}
            deps.store.set_doc("payouts", po["id"], cached)
        for pid in cached["payment_ids"]:
            index[pid] = cached
    return index


def awaiting_slips(deps: Deps) -> list[Slip]:
    return [x for x in deps.store.find_slips("status", "in", [Status.PAID, Status.REVIEW]) if x.awaiting_deposit]


def run(deps: Deps, now: datetime | None = None, force_mf: bool = False) -> dict:
    s = deps.settings
    now = now or datetime.now(UTC)
    errs = ItemErrors("deposits")
    slips = awaiting_slips(deps)
    stats = {"awaiting": len(slips), "expected_built": refresh_expected(deps, slips, errs)}
    if not slips and not unpaid.open_slips(deps):   # 入金待ちも未収も無ければ、マネフォを読まない
        errs.raise_if_any()
        return stats

    meta = deps.store.get_doc("meta", "mf") or {}
    last = datetime.fromisoformat(meta["last_fetch"]) if meta.get("last_fetch") else None
    if not force_mf and last and now - last < timedelta(hours=s.mf.fetch_interval_hours):
        errs.raise_if_any()
        return stats
    today = now.astimezone(s.tz).date()
    txns = deps.bank.incomes(today - timedelta(days=45), today)
    deps.store.set_doc("meta", "mf", {"last_fetch": now.isoformat(), "count": len(txns)})
    claims = deps.store.deposit_claims([t.id for t in txns])
    patterns = [norm_text(p) for p in s.mf.square_patterns]
    staff = [norm_text(n) for n in s.mf.staff_bank_names if n.strip()]
    stats["bank_incomes"] = len(txns)
    stats["payouts_confirmed"] = match_payouts(deps, awaiting_slips(deps), txns, claims, patterns, errs)
    if staff:   # 担当者の名義が分からないと、現金の振込と顧客の振込を区別できないので照合しない
        stats["cash_reports"] = match_cash_reports(deps, txns, claims, staff, errs)
    stats["unpaid_bank"] = unpaid.collect_by_bank(deps, txns, claims, patterns + staff, errs)
    if staff:
        stats["cash_confirmed"] = match_cash(deps, awaiting_slips(deps), txns, claims, staff, today, errs)
    errs.raise_if_any()
    return stats


def refresh_expected(deps: Deps, slips: list[Slip], errs: ItemErrors) -> int:
    stale = [x for x in slips if x.deposits.get("built_for") != sorted(x.payment_ids)]
    if not stale:
        return 0
    try:
        index = payout_index(deps, stale)
    except Exception as e:   # Square の振込が読めなくても、作成済みの予定で銀行の照合は続ける
        errs.errors.append(("payout_index", e))
        log.exception("Square の振込の取得に失敗（銀行の照合は続ける）", extra={"stage": "deposits"})
        return 0
    n = 0
    for slip in stale:
        with errs.item(slip_id=slip.slip_id):
            expected = build_expected(slip, index)
            if expected is None:
                continue

            def fn(x: Slip, expected=expected) -> None:
                x.deposits = {**x.deposits, "expected": expected, "built_for": sorted(x.payment_ids)}
                _complete_if_all_confirmed(x)

            save(deps, slip, fn, "入金の予定を作成")
            n += 1
    return n


def _complete_if_all_confirmed(x: Slip) -> int:
    """期待する入金がすべて確認できていれば完了にする（save の fn の中で呼ぶ）。Square で回収した未収の金額を返す。"""
    expected = x.deposits.get("expected") or []
    if not expected or not all(e.get("confirmed") for e in expected) or x.deposits.get("completed_at"):
        return 0
    x.deposits["completed_at"] = now_iso()
    resolve_alerts(x, Alert.DEPOSIT_MISSING)
    return unpaid.confirm_square_collections(x)


def confirm(deps: Deps, slip: Slip, key: str, txn) -> None:
    """期待する入金（key）を、銀行明細 txn で確認済みにする。"""
    result = {"completed": False, "collected": 0}

    def fn(x: Slip) -> None:
        result["completed"], result["collected"] = False, 0
        for e in x.deposits.get("expected") or []:
            if entry_key(e) == key and not e.get("confirmed"):
                e["confirmed"] = {"date": txn.date, "txn_id": txn.id}
        was = bool(x.deposits.get("completed_at"))
        result["collected"] = _complete_if_all_confirmed(x)
        result["completed"] = not was and bool(x.deposits.get("completed_at"))

    save(deps, slip, fn, "入金を確認")
    if result["completed"]:
        parts = []
        for e in slip.deposits["expected"]:
            day = e["confirmed"]["date"][5:].replace("-", "/")
            if e["kind"] == "square_payout":
                merged = "（他の伝票と合算）" if e["amount"] != e["share"] else ""
                parts.append(f"{day} Square振込 {yen(e['amount'])}{merged}")
            else:
                parts.append(f"{day} 現金の振込 {yen(e['amount'])}")
        post(deps, slip, "入金確認完了：" + "、".join(parts) + " をマネフォで確認")
    if result["collected"]:
        post(deps, slip, f"未収 回収入金確認：Square で回収した分の入金 {yen(result['collected'])}"
                         f"{unpaid_rest(slip.unpaid_remaining)}")


def _usable(claims: dict[str, str], txn_id: str, ref: str) -> bool:
    return claims.get(txn_id) in (None, ref)


def match_payouts(deps: Deps, slips: list[Slip], txns, claims: dict[str, str], patterns: list[str],
                  errs: ItemErrors) -> int:
    grace = deps.settings.deposit.grace_bizdays
    by_payout: dict[str, tuple[dict, list[Slip]]] = {}
    for slip in slips:
        for e in slip.deposits.get("expected") or []:
            if e["kind"] == "square_payout" and not e.get("confirmed"):
                by_payout.setdefault(e["payout_id"], (e, []))[1].append(slip)
    n = 0
    for payout_id, (e, group) in by_payout.items():
        ref = f"payout:{payout_id}"
        arrival = date.fromisoformat(e["arrival_date"])
        lo, hi = arrival - timedelta(days=1), bizdate.add_bank_days(arrival, grace)
        hit = next((t for t in txns if _usable(claims, t.id, ref) and t.amount == e["amount"]
                    and lo <= date.fromisoformat(t.date) <= hi and any(p in norm_text(t.content) for p in patterns)), None)
        if not hit or not deps.store.claim_deposit(hit.id, ref):
            continue
        claims[hit.id] = ref
        n += 1
        for slip in group:
            with errs.item(slip_id=slip.slip_id):
                confirm(deps, slip, ref, hit)
    return n


def _cash_window(deps: Deps, paid_at: str | None, today: date) -> tuple[date, date]:
    s = deps.settings
    paid = bizdate.local_date(paid_at, s.tz) if paid_at else today
    return paid, paid + timedelta(days=s.deposit.cash_window_days)


def match_cash(deps: Deps, slips: list[Slip], txns, claims: dict[str, str], staff: list[str], today: date,
               errs: ItemErrors) -> int:
    n = 0
    for slip in slips:
        for e in slip.deposits.get("expected") or []:
            if e["kind"] != "cash" or e.get("confirmed"):
                continue
            with errs.item(slip_id=slip.slip_id):
                key = entry_key(e)
                ref = f"cash:{slip.slip_id}" if key == "cash" else f"{key}@{slip.slip_id}"
                lo, hi = _cash_window(deps, e.get("paid_at"), today)
                hit = next((t for t in txns if _usable(claims, t.id, ref) and t.amount == e["amount"]
                            and lo <= date.fromisoformat(t.date) <= hi and any(x in norm_text(t.content) for x in staff)), None)
                if hit and deps.store.claim_deposit(hit.id, ref):
                    claims[hit.id] = ref
                    confirm(deps, slip, key, hit)
                    n += 1
    return n


def match_cash_reports(deps: Deps, txns, claims: dict[str, str], staff: list[str], errs: ItemErrors) -> int:
    """現金をまとめて入金した報告（LINE）の合計と、担当者名義の振込を照合する。"""
    days = deps.settings.deposit.cash_window_days
    n = 0
    for report_id, rep in deps.store.find_docs("cash_reports", "resolved_at", "==", None).items():
        with errs.item(cash_report=report_id):
            ref = f"cash_report:{report_id}"
            start = bizdate.local_date(rep["sent_at"], deps.settings.tz) - timedelta(days=1)
            hits = [t for t in txns if _usable(claims, t.id, ref) and t.amount == rep["total"]
                    and start <= date.fromisoformat(t.date) <= start + timedelta(days=days)
                    and any(x in norm_text(t.content) for x in staff)]
            slips = [x for x in (deps.store.get_slip(sid) for sid in rep["slip_ids"]) if x]
            if any(not x.deposits.get("expected") for x in slips):   # 期待する入金ができてから照合する
                continue
            if len(hits) != 1 or not deps.store.claim_deposit(hits[0].id, ref):
                continue
            t = hits[0]
            claims[t.id] = ref
            for slip in slips:
                confirm(deps, slip, "cash", t)
            deps.store.set_doc("cash_reports", report_id, {**rep, "resolved_at": now_iso(), "txn_id": t.id})
            n += 1
    return n


def deposit_alerts(deps: Deps, today: date | None = None) -> int:
    """入金が予定より遅れている伝票に「入金未確認」を出す（毎日 10:00）。"""
    s = deps.settings
    today = today or datetime.now(UTC).astimezone(s.tz).date()
    grace, cash_days = s.deposit.grace_bizdays, s.deposit.cash_window_days
    errs = ItemErrors("deposit_alerts")
    n = 0
    for slip in awaiting_slips(deps):
        with errs.item(slip_id=slip.slip_id):
            msg = _overdue(deps, slip, today, grace, cash_days)
            if msg and raise_alert(deps, slip, Alert.DEPOSIT_MISSING, msg, important=True):
                n += 1
    errs.raise_if_any()
    return n


def _overdue(deps: Deps, slip: Slip, today: date, grace: int, cash_days: int) -> str | None:
    expected = slip.deposits.get("expected")
    if not expected:   # 振込がまだ作られていない
        paid = [bizdate.local_date(p["paid_at"], deps.settings.tz) for p in payments_of(slip) if p.get("paid_at")]
        if paid and today > bizdate.add_bank_days(max(paid), grace + 2):
            return f"決済から{grace + 2}営業日たっても、Square の振込が作られていません（Square の管理画面で確認してください）"
        return None
    for e in expected:
        if e.get("confirmed"):
            continue
        if e["kind"] == "square_payout":
            if today > bizdate.add_bank_days(date.fromisoformat(e["arrival_date"]), grace):
                return (f"Square振込 {yen(e['amount'])}（着金予定 {e['arrival_date']}）が、"
                        f"{grace}営業日を過ぎても銀行の入金に見当たりません")
        elif today > _cash_window(deps, e.get("paid_at"), today)[1]:
            note = "" if deps.settings.mf.staff_bank_names else "（担当者の名義の一覧が未設定のため、現金は自動で照合していません）"
            return f"現金 {yen(e['amount'])} の銀行への振込が、決済から{cash_days}日たっても見当たりません{note}"
    return None
