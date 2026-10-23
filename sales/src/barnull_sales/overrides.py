"""Asana の「手動上書き」を読み取り、伝票に反映する（30分ごと）。反映したら「なし」に戻す。

| 手動上書き        | 反映すること                                                         |
|------------------|----------------------------------------------------------------------|
| 決済確認済み      | 決済を人が確認した（Square の割り当ては残す）。アラートをすべて閉じる      |
| 入金確認済み      | 決済と入金を人が確認した。アラートをすべて閉じる                          |
| 未収回収済み      | 未収残高を 0 にする                                                    |
| 対象外（取消）    | 伝票を取り消す。Square の割り当てを外す（他の伝票に使えるように）          |
状態（伝票ステータス・完了）は slips.derive が事実から決める。
反映できない値・伝票の見つからないタスクは、チャンネルで知らせて「なし」に戻す。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from . import bizdate
from .deps import Deps
from .errors import ItemErrors
from .slip_ops import post, resolve_alerts, save
from .slips import Manual, Slip, now_iso
from .texts import esc, who

log = logging.getLogger(__name__)


def _paid(s: Slip) -> None:
    s.square = {**s.square, "manual": True, "summary": s.square.get("summary") or "手動で確認"}
    resolve_alerts(s)


def _deposited(s: Slip) -> None:
    _paid(s)
    s.deposits = {**s.deposits, "manual": True, "completed_at": s.deposits.get("completed_at") or now_iso()}


def _collected(s: Slip) -> None:
    s.unpaid_remaining = 0
    for r in s.collection_reports:
        r["confirmed"] = True


def _cancelled(s: Slip) -> None:
    released = s.square.get("released_payment_ids") or s.payment_ids   # もう一度反映しても消さない
    s.square = {**s.square, "released_payment_ids": released, "payment_ids": [], "cancelled": True}
    resolve_alerts(s)


ACTIONS = {Manual.PAID: _paid, Manual.DEPOSITED: _deposited, Manual.COLLECTED: _collected,
           Manual.CANCELLED: _cancelled}


def apply(deps: Deps) -> int:
    meta = deps.store.get_doc("meta", "asana_overrides") or {}
    started = datetime.fromisoformat(now_iso())
    errs = ItemErrors("manual_overrides")
    n = 0
    for ov in deps.tracker.read_overrides(meta.get("last")):
        with errs.item(task_gid=ov["task_gid"]):
            if _apply_one(deps, ov):
                n += 1
    # 失敗があったときは読み取り位置を進めない（次回もう一度読む）。読み取り中の変更を取りこぼさないよう少し戻す
    if not errs.errors:
        deps.store.set_doc("meta", "asana_overrides", {"last": (started - timedelta(minutes=5)).isoformat()})
    errs.raise_if_any()
    return n


def _apply_one(deps: Deps, ov: dict) -> bool:
    gid, value, by = ov["task_gid"], ov["value"], ov.get("by") or "不明"
    action = ACTIONS.get(value)
    found = deps.store.find_slips("asana.task_gid", "==", gid)
    if not action or not found:
        log.warning("手動上書きを反映できない（%s）", "伝票が無い" if action else "不明な値")
        deps.notifier.post(deps.settings.slack_channel,
                           f"手動上書き「{esc(value)}」（{esc(by)}）を反映できませんでした"
                           f"（{'このタスクの伝票が見つかりません' if action else '選択肢が分かりません'}）。"
                           f"「なし」に戻しました。https://app.asana.com/0/0/{gid}")
        deps.tracker.reset_override(gid)
        return False
    slip = found[0]
    if action is _cancelled:   # この伝票に割り当てたすべての決済を外す（途中で止まった割り当ても含む）
        claimed = deps.store.find_docs("payment_claims", "slip_id", "==", slip.slip_id)
        deps.store.release_payments(slip.slip_id, sorted(set(claimed) | set(slip.payment_ids)))
    save(deps, slip, action, f"手動上書き（{value}、{by}）")
    when = bizdate.hm(datetime.fromisoformat(ov["at"].replace("Z", "+00:00")), deps.settings.tz) if ov.get("at") else ""
    post(deps, slip, f"手動上書き：{who(by)} が「{value.replace('手動：', '')}」にしました（{when}、Asana）")
    deps.tracker.reset_override(gid)
    log.info("手動上書きを反映: %s", value, extra={"slip_id": slip.slip_id, "task_gid": gid})
    return True
