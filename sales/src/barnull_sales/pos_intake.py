"""POS（福岡 Node）からの伝票の受け付け（LINE と Claude の解析を通らない。node-fukuoka の docs/pos_phase3_plan.md §3.2）。

POS は会計の確定・訂正・取り消しのたびに、伝票の今の内容を版（revision）つきで送ってくる（Cloud Tasks 経由）。
- 伝票は (pos-{POS の伝票ID}, 0) から1つに決まる（同じ伝票を何度送っても1件。slip_keys）
- 送られた版が保存済みの版より新しいときだけ反映する（遅れて届いた古い版は無視する）
- 金額・受け取りが変わったら、Square の割り当てを外して照合し直す。取り消しなら「対象外」にする
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from .deps import Deps
from .reconcile import match_now
from .slip_ops import post, publish, raise_alert, resolve_alerts, save
from .slips import PAYMENT_ALERTS, Alert, Slip, derive
from .texts import yen

log = logging.getLogger(__name__)
METHODS = ("cash", "square", "unpaid")


class Invalid(ValueError):
    pass


def _receipts(body: dict) -> list[dict]:
    out = []
    for r in body.get("receipts") or []:
        if r.get("method") not in METHODS or not isinstance(r.get("amount"), int) or r["amount"] <= 0:
            raise Invalid("receipts")
        datetime.fromisoformat(r["at"])
        out.append({"id": str(r["id"]), "method": r["method"], "amount": r["amount"], "at": r["at"]})
    return out


def validate(body: dict) -> dict:
    """POS から届いた中身を確かめ、使う項目だけにする。"""
    try:
        visit_id, revision = str(body["visit_id"]), int(body["revision"])
        state = body["state"]
        total = int(body["total"])
        day = datetime.fromisoformat(body["business_date"]).date().isoformat()
        closed_at = datetime.fromisoformat(body["closed_at"])
    except (KeyError, TypeError, ValueError) as e:
        raise Invalid(f"項目が足りない・形が違う: {e}") from None
    if state not in ("closed", "void") or total < 0 or revision < 1:
        raise Invalid("state・total・revision")
    receipts = _receipts(body)
    if state == "closed" and sum(r["amount"] for r in receipts) != total:
        raise Invalid("受け取りの合計が合計金額と合わない")
    return {
        "visit_id": visit_id, "revision": revision, "state": state, "total": total, "business_date": day,
        "closed_at": closed_at.astimezone(UTC).isoformat(), "name": str(body.get("name") or ""),
        "staff": str(body.get("staff") or ""), "note": str(body.get("note") or ""), "party": int(body.get("party") or 0),
        "seats": [str(x) for x in body.get("seats") or []], "receipts": receipts,
        "unpaid_due": str((body.get("unpaid") or {}).get("due") or ""),
        "customers": [{"code": str(c.get("code", "")), "visit_no": int(c.get("visit_no") or 0)}
                      for c in body.get("customers") or []],
        "referrals": [{k: str(r.get(k) or "") for k in ("agent_id", "agent_name", "tenant_agent_id", "tenant_name",
                                                         "member_id", "member_name")}
                      for r in body.get("referrals") or []],
    }


def _summary(v: dict) -> str:
    """伝票の「原文」の欄に入れる、POS の伝票の要約（顧客の LINE の情報は含めない）。"""
    method = {"cash": "現金", "square": "Square", "unpaid": "未収"}
    lines = [f"POS 伝票 {v['visit_id']}（版 {v['revision']}）", f"席: {'・'.join(v['seats']) or 'なし'}　{v['party']}名",
             f"合計: {yen(v['total'])}",
             "受け取り: " + ("、".join(f"{method[r['method']]} {yen(r['amount'])}" for r in v["receipts"]) or "なし")]
    if v["customers"]:
        lines.append("顧客コード: " + ", ".join(c["code"] for c in v["customers"]))
    if v["note"]:
        lines.append(f"備考: {v['note']}")
    return "\n".join(lines)


def _apply(s: Slip, v: dict) -> None:
    """POS の内容を伝票に写す（起票と訂正で共通）。"""
    unpaid = sum(r["amount"] for r in v["receipts"] if r["method"] == "unpaid")
    collected = s.unpaid_amount - s.unpaid_remaining if s.unpaid_amount else 0
    s.customer_name = v["name"] or None
    s.slip_amount = v["total"]
    s.note = v["note"]
    s.sender_display_name = v["staff"] or None
    s.raw_text = _summary(v)
    s.business_date = v["business_date"]
    s.unpaid_amount = unpaid
    s.unpaid_remaining = max(unpaid - collected, 0)
    s.unpaid_due_text = v["unpaid_due"]
    s.expected_square_amount = max(v["total"] - unpaid, 0)
    s.pos = {**s.pos, "visit_id": v["visit_id"], "revision": v["revision"], "state": v["state"], "receipts": v["receipts"],
             "customers": v["customers"], "referrals": v["referrals"], "party": v["party"], "seats": v["seats"]}


def handle(deps: Deps, body: dict) -> dict:
    v = validate(body)
    s = deps.settings
    event_id = f"pos-{v['visit_id']}"

    def make(slip_id: str) -> Slip:
        slip = Slip(slip_id=slip_id, event_id=event_id, entry_index=0, group_id="pos", message_id=v["visit_id"],
                    sender_line_user_id=None, sender_display_name=None, sent_at=v["closed_at"], raw_text="",
                    business_date=v["business_date"], business_date_source="pos", customer_name=None, slip_amount=None,
                    note="", dry_run=s.dry_run, source="pos")
        _apply(slip, v)
        if v["state"] == "void":
            slip.square = {"cancelled": True}
        derive(slip)
        slip.status_history.append({"from": None, "to": slip.status, "at": slip.created_at, "reason": "起票（POS）"})
        return slip

    slip, created = deps.store.create_slip(event_id, 0, v["business_date"], make)
    if created:
        publish(deps, slip)
        try:
            match_now(deps, [slip])
        except Exception:
            log.warning("起票直後の突合に失敗（30分ごとのジョブで探す）", exc_info=True)
        return {"status": "created", "slip_id": slip.slip_id}
    if not slip.slack.get("thread_ts") or not slip.asana.get("task_gid"):
        publish(deps, slip)   # 前回、反映の途中で止まった
    if v["revision"] <= int(slip.pos.get("revision") or 0):
        return {"status": "stale", "slip_id": slip.slip_id}
    return update(deps, slip, v)


def update(deps: Deps, slip: Slip, v: dict) -> dict:
    """POS での訂正・取り消し。"""
    if slip.closed_at and not slip.cancelled:
        text = f"完了済みの伝票が POS で{'取り消され' if v['state'] == 'void' else '訂正され'}ました（版 {v['revision']}）。" \
               "自動では開き直しません。内容を確かめてください"
        if not raise_alert(deps, slip, Alert.CORRECTION_AFTER_CLOSE, text, important=True):
            post(deps, slip, deps.settings.mention + "★" + text, broadcast=True)
        save(deps, slip, lambda s: s.pos.__setitem__("revision", v["revision"]), "POS の版を記録", notify=False)
        return {"status": "correction_after_close", "slip_id": slip.slip_id}
    old_receipts = [(r["method"], r["amount"]) for r in slip.pos.get("receipts") or []]
    new_receipts = [(r["method"], r["amount"]) for r in v["receipts"]]
    money_changed = slip.slip_amount != v["total"] or sorted(old_receipts) != sorted(new_receipts) or v["state"] == "void"
    if money_changed:
        held = set(slip.payment_ids) | set((slip.square.get("receipt_matches") or {}).values())
        if held:
            deps.store.release_payments(slip.slip_id, sorted(held))   # 照合し直す

    def fn(s: Slip) -> None:
        _apply(s, v)
        if money_changed:
            s.square, s.deposits = ({"cancelled": True} if v["state"] == "void" else {}), {}
            resolve_alerts(s, *PAYMENT_ALERTS, Alert.UNPAID_MISMATCH, Alert.PAYMENT_CONTESTED)

    reason = "POS で取り消し" if v["state"] == "void" else "POS で訂正"
    save(deps, slip, fn, f"{reason}（版 {v['revision']}）", event_id=f"pos-{v['visit_id']}-r{v['revision']}")
    post(deps, slip, f"{reason}：合計 {yen(v['total'])}（版 {v['revision']}）" if v["state"] != "void" else f"{reason}（版 {v['revision']}）")
    if money_changed and v["state"] != "void":
        try:
            match_now(deps, [slip])
        except Exception:
            log.warning("訂正後の突合に失敗（30分ごとのジョブで探す）", exc_info=True)
    return {"status": "void" if v["state"] == "void" else "corrected", "slip_id": slip.slip_id}

