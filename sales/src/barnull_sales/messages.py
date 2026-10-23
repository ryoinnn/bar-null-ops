"""@Bot メッセージの種類ごとの処理: 伝票の起票、訂正、未収の回収報告、現金の入金報告。

どれも、同じメッセージを二度処理しても結果が同じになるようにする（Cloud Tasks の再試行のため）。
- 起票: 伝票は (イベント ID, 顧客の順番) から1つに決まる。Slack・Asana は作成済みなら作らない
- 訂正: 適用したら伝票の履歴にイベント ID を残し、二度目は適用しない
- 回収報告: 報告にイベント ID を付け、同じ報告は足さない
- 現金の入金報告: 報告の ID がイベント ID
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from . import bizdate, texts
from .deposits import awaiting_slips, entry_key, payments_of
from .deps import Deps
from .extract import Parsed
from .reconcile import match_now
from .slip_ops import applied, post, publish, raise_alert, resolve_alerts, save
from .slips import PAYMENT_ALERTS, Alert, Slip, build_slip, business_date_of, now_iso
from .texts import esc, norm_person, who, yen
from .unpaid import open_slips

log = logging.getLogger(__name__)


@dataclass
class Msg:
    event_id: str
    group_id: str
    message_id: str
    user_id: str | None
    sender: str | None      # LINE の表示名
    text: str               # メンションを除いた本文
    sent_at: datetime


def review(deps: Deps, title: str, msg: Msg) -> None:
    deps.notifier.post(deps.settings.slack_channel, texts.review(deps.settings, title, msg.sender, msg.text))


def handle(deps: Deps, msg: Msg, parsed: Parsed | None, on_accepted: Callable[[], None]) -> dict:
    """on_accepted: メッセージを受け付けた（伝票に反映できた）ときに呼ぶ。LINE に「OK」スタンプを返す。"""
    if parsed is None or parsed.kind == "other" or (parsed.kind == "slip_content" and not parsed.entries):
        review(deps, "解析できないメッセージ", msg)
        return {"status": "unparsed"}
    handler = {"slip_content": create_slips, "correction": correction, "unpaid_collection_report": collection_report,
               "cash_deposit_report": cash_report}[parsed.kind]
    return handler(deps, msg, parsed, on_accepted)


# ---------------------------------------------------------------- 起票
def create_slips(deps: Deps, msg: Msg, parsed: Parsed, on_accepted: Callable[[], None]) -> dict:
    s = deps.settings
    slips: list[Slip] = []
    for i, entry in enumerate(parsed.entries):
        def make(slip_id: str, entry=entry, i=i) -> Slip:
            return build_slip(slip_id=slip_id, event_id=msg.event_id, index=i, entry=entry, group_id=msg.group_id,
                              message_id=msg.message_id, sender_id=msg.user_id, sender_name=msg.sender,
                              sent_at=msg.sent_at, raw_text=msg.text, settings=s)

        bday = business_date_of(entry, msg.sent_at, s) or bizdate.business_date(msg.sent_at, s.tz, s.day_start)
        slip, _ = deps.store.create_slip(msg.event_id, i, bday.isoformat(), make)
        slips.append(slip)
    on_accepted()   # 伝票を保存できたので、LINE に「OK」を返す
    for slip in slips:
        publish(deps, slip)
    try:
        match_now(deps, slips)
    except Exception:
        log.warning("起票直後の突合に失敗（30分ごとのジョブで探す）", exc_info=True)
    return {"status": "slips", "slip_ids": [x.slip_id for x in slips], "model": parsed.model, "usage": parsed.usage}


# ---------------------------------------------------------------- 訂正
def _recent_by_sender(deps: Deps, msg: Msg) -> list[Slip]:
    if not msg.user_id:
        return []
    window = timedelta(hours=deps.settings.followup.correction_window_hours)
    out = [x for x in deps.store.find_slips("sender_line_user_id", "==", msg.user_id)
           if msg.sent_at - datetime.fromisoformat(x.sent_at) <= window]
    return sorted(out, key=lambda x: x.sent_at, reverse=True)


def correction(deps: Deps, msg: Msg, parsed: Parsed, on_accepted: Callable[[], None]) -> dict:
    c = parsed.correction or {}
    candidates = _recent_by_sender(deps, msg)
    done = [x for x in candidates if applied(x, msg.event_id)]
    if done:              # 再試行: すでに適用した（顧客名を訂正した後でも見つかるように、絞り込む前に見る）
        on_accepted()
        return {"status": "corrected", "slip_ids": [done[0].slip_id]}
    target = norm_person(c.get("target_customer_name"))
    if target:
        candidates = [x for x in candidates if norm_person(x.customer_name) == target]
    elif len(candidates) > 1 and candidates[0].event_id != candidates[1].event_id:
        candidates = candidates[:1]          # 顧客名が無ければ、直前のメッセージの伝票（1件だけのとき）
    new_amount, new_name = c.get("new_amount"), c.get("new_customer_name")
    if len(candidates) != 1 or (new_amount is None and not new_name):
        review(deps, "訂正（対象の伝票を決められない）", msg)
        return {"status": "review_correction"}
    slip = candidates[0]
    result = {"slip_ids": [slip.slip_id]}
    if slip.closed_at:
        text = f"完了済みの伝票への訂正です（自動では開き直しません）：{esc(c.get('description') or msg.text)}"
        if not raise_alert(deps, slip, Alert.CORRECTION_AFTER_CLOSE, text, important=True):
            post(deps, slip, deps.settings.mention + "★" + text, broadcast=True)   # 2回目以降の訂正も知らせる
        return {"status": "correction_after_close", **result}
    changes = []
    amount_changed = new_amount is not None and new_amount != slip.slip_amount
    if amount_changed:
        changes.append(f"金額 {yen(slip.slip_amount)} → {yen(new_amount)}")
    name_changed = bool(new_name) and new_name != slip.customer_name
    if name_changed:
        changes.append(f"顧客名 {esc(slip.customer_name)}様 → {esc(new_name)}様")
    if not changes:
        on_accepted()
        post(deps, slip, f"訂正の連絡がありましたが、内容は変わりません（{who(msg.sender)}さん）")
        return {"status": "correction_nochange", **result}
    if amount_changed and slip.payment_ids:
        # 金額が変わったので、Square の割り当てを外して照合し直す
        deps.store.release_payments(slip.slip_id, slip.payment_ids)

    def fn(s: Slip) -> None:
        if amount_changed:
            s.slip_amount = new_amount
            s.expected_square_amount = max(new_amount - (s.unpaid_amount or 0), 0)
            s.square, s.deposits = {}, {}
            # 外した決済に結び付いていた、未確認の Square での回収も外す（照合し直すと、また割り当てられる）
            s.collection_reports = [r for r in s.collection_reports if r.get("type") != "square" or r.get("confirmed")]
            resolve_alerts(s, *PAYMENT_ALERTS, Alert.UNPAID_MISMATCH)
        if name_changed:
            s.customer_name = new_name

    save(deps, slip, fn, "訂正：" + "、".join(changes), event_id=msg.event_id)
    on_accepted()
    post(deps, slip, f"訂正：{'、'.join(changes)}（{who(msg.sender)}さんより）")
    if amount_changed:
        try:
            match_now(deps, [slip])
        except Exception:
            log.warning("訂正後の突合に失敗（30分ごとのジョブで探す）", exc_info=True)
    return {"status": "corrected", **result}


# ---------------------------------------------------------------- 未収の回収報告
METHOD_LABEL = {"bank_transfer": "振込", "cash": "現金", "square": "Square", "unknown": "方法不明"}


def collection_report(deps: Deps, msg: Msg, parsed: Parsed, on_accepted: Callable[[], None]) -> dict:
    r = parsed.collection_report or {}
    name = norm_person(r.get("customer_name"))
    candidates = [x for x in open_slips(deps) if name and norm_person(x.customer_name) == name]
    if r.get("amount") and len(candidates) > 1:
        candidates = [x for x in candidates if x.unpaid_remaining == r["amount"]] or candidates
    if len(candidates) != 1:
        review(deps, "未収の回収報告（対象の伝票を決められない）", msg)
        return {"status": "review_collection"}
    slip = candidates[0]
    result = {"status": "collection_reported", "slip_ids": [slip.slip_id]}
    if any(x.get("event_id") == msg.event_id for x in slip.collection_reports):
        on_accepted()
        return result
    method = r.get("method") or "unknown"
    amount = r.get("amount") or slip.unpaid_remaining

    def fn(s: Slip) -> None:
        if not any(x.get("event_id") == msg.event_id for x in s.collection_reports):
            s.collection_reports.append({"type": "report", "event_id": msg.event_id, "by": msg.sender, "method": method,
                                         "amount": amount, "at": now_iso(), "text": msg.text, "confirmed": False})

    save(deps, slip, fn, "未収の回収報告")
    on_accepted()
    day = bizdate.hm(msg.sent_at, deps.settings.tz)
    post(deps, slip, f"未収 回収報告：{who(msg.sender)}さんより「{esc(msg.text)}」"
                     f"（{day}、{METHOD_LABEL.get(method, '方法不明')} {yen(amount)}）→ 着金確認待ち")
    return result


# ---------------------------------------------------------------- 現金の入金報告
def _unconfirmed_cash(slip: Slip) -> int:
    """その日の現金のうち、まだ銀行で確認できていない金額（後日の未収の回収は含めない）。"""
    if any(entry_key(e) == "cash" and e.get("confirmed") for e in slip.deposits.get("expected") or []):
        return 0
    return sum(p["amount"] or 0 for p in payments_of(slip)
               if p["source_type"] == "CASH" and not p.get("unpaid_collection"))


def cash_report(deps: Deps, msg: Msg, parsed: Parsed, on_accepted: Callable[[], None]) -> dict:
    r = parsed.cash_deposit or {}
    names = {norm_person(n) for n in r.get("customer_names") or [] if n}
    slips = [x for x in awaiting_slips(deps) if norm_person(x.customer_name) in names and _unconfirmed_cash(x)]
    if not slips:
        review(deps, "現金の入金報告（対象の伝票が見つからない）", msg)
        return {"status": "review_cash_report"}
    total = sum(_unconfirmed_cash(x) for x in slips)
    result = {"status": "cash_reported", "slip_ids": [x.slip_id for x in slips]}
    if deps.store.get_doc("cash_reports", msg.event_id):    # 再試行: 記録と投稿は済んでいる
        on_accepted()
        return result
    deps.store.set_doc("cash_reports", msg.event_id, {
        "slip_ids": [x.slip_id for x in slips], "total": r.get("total_amount") or total,
        "sent_at": msg.sent_at.isoformat(), "by": msg.sender, "resolved_at": None})
    on_accepted()
    for x in slips:
        post(deps, x, f"現金の入金報告：{who(msg.sender)}さんより、{len(slips)}件まとめて {yen(total)} を入金（着金確認待ち）")
    return result
