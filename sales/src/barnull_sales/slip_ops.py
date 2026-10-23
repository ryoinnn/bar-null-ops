"""伝票の保存と、Slack・Asana への反映（すべての処理がここを通る）。

- save:        伝票の事実を書き換えて保存し、状態を計算し直し（slips.derive）、Slack と Asana に反映する
- raise_alert: アラートを足す（スレッドに投稿。重要なものはチャンネルにも出して桝本をメンション）
- post:        伝票のスレッドに投稿する
- publish:     Slack の親メッセージと Asana のタスクを作る（作成済みなら作らない）
- sync:        Slack の親メッセージと Asana のタスクを、保存した状態に合わせる

保存（Firestore）が正本。Slack・Asana への反映に失敗しても保存は取り消さず、sync_pending を立てて
30分ごとのジョブ（jobs.resync）でやり直す。
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from . import texts
from .deps import Deps
from .slips import Slip, derive, now_iso

log = logging.getLogger(__name__)


def resolve_alerts(slip: Slip, *kinds: str) -> None:
    """未解決のアラートを解決済みにする（kinds を省くとすべて）。save に渡す fn の中で使う。"""
    now = now_iso()
    for a in slip.open_alerts(*kinds):
        a["resolved_at"] = now


def save(deps: Deps, slip: Slip, fn: Callable[[Slip], None], reason: str, *, event_id: str | None = None,
         notify: bool = True) -> None:
    """fn で伝票を書き換えて保存する。状態が変わったら履歴に残し、完了したらスレッドで知らせる。

    event_id を渡すと、状態が変わらなくても履歴に残す（同じ LINE メッセージを二度適用しないための目印）。
    """
    closed = False

    def apply(s: Slip) -> None:
        nonlocal closed
        before, was_closed = s.status, s.closed_at
        fn(s)
        derive(s)
        closed = bool(s.closed_at and not was_closed and not s.cancelled)
        if s.status != before or event_id:
            s.status_history.append({"from": before, "to": s.status, "at": now_iso(), "reason": reason,
                                     **({"event_id": event_id} if event_id else {})})

    deps.store.update_slip(slip, apply)
    # ログには理由の見出しだけ（「訂正：金額 …」の後ろには顧客名や金額が入るため）
    log.info("保存: %s（%s）", reason.split("：")[0], slip.status, extra={"slip_id": slip.slip_id})
    if notify:
        sync(deps, slip)
        if closed:
            post(deps, slip, "完了：伝票・決済・入金" + ("・未収回収" if slip.unpaid_amount else "") + "をすべて確認しました")


def applied(slip: Slip, event_id: str) -> bool:
    """この LINE メッセージ（event_id）を、すでにこの伝票に適用したか。"""
    return any(h.get("event_id") == event_id for h in slip.status_history)


def raise_alert(deps: Deps, slip: Slip, kind: str, message: str, important: bool = False) -> bool:
    """同じ種類の未解決のアラートが無ければ足す。足したら True。"""
    if slip.open_alerts(kind):
        return False

    def add(s: Slip) -> None:
        if not s.open_alerts(kind):
            s.alerts.append({"type": kind, "message": message, "at": now_iso(), "resolved_at": None,
                             "posted": False, "important": important})

    save(deps, slip, add, kind)
    return True


def post(deps: Deps, slip: Slip, text: str, broadcast: bool = False) -> None:
    """伝票のスレッドに投稿する。失敗しても止めない（ログに残し、親メッセージは後で sync で直す）。"""
    if not slip.slack.get("thread_ts"):
        return
    try:
        deps.notifier.post(slip.slack["channel_id"], text, thread_ts=slip.slack["thread_ts"], broadcast=broadcast)
    except Exception:
        log.exception("スレッドへの投稿に失敗", extra={"slip_id": slip.slip_id, "stage": "post"})
        _mark(deps, slip, sync_pending=True)


def sync(deps: Deps, slip: Slip, *, raise_errors: bool = False) -> None:
    """まだ投稿していないアラートを出し、Slack の親メッセージ・Asana のタスクを今の状態に合わせる。

    3つは別々に試す（Asana のタスクが消されていても、アラートは Slack に出す）。
    """
    ch, ts = slip.slack.get("channel_id"), slip.slack.get("thread_ts")
    parts = [("alerts", lambda: _post_alerts(deps, slip, ch, ts))]
    if ts:
        parts.append(("slack", lambda: deps.notifier.update(ch, ts, texts.slack_parent(slip))))
    if slip.asana.get("task_gid"):
        parts.append(("asana", lambda: deps.tracker.update_task(slip)))
    failed: Exception | None = None
    for stage, fn in parts:
        try:
            fn()
        except Exception as e:
            log.exception("%s への反映に失敗（30分ごとのジョブでやり直す）", stage,
                          extra={"slip_id": slip.slip_id, "stage": f"sync:{stage}"})
            failed = failed or e
    if failed or slip.sync_pending:
        _mark(deps, slip, sync_pending=bool(failed))
    if failed and raise_errors:
        raise failed


def _post_alerts(deps: Deps, slip: Slip, ch: str | None, ts: str | None) -> None:
    if not ts:
        return
    for a in list(slip.alerts):
        if a.get("resolved_at") or a.get("posted", True):
            continue
        important = a.get("important", False)
        text = (deps.settings.mention + "★" if important else "") + f"{a['type']}：{a['message']}"
        deps.notifier.post(ch, text, thread_ts=ts, broadcast=important)
        _mark(deps, slip, posted_alert_at=a["at"])


def _mark(deps: Deps, slip: Slip, *, sync_pending: bool | None = None, posted_alert_at: str | None = None) -> None:
    """反映の記録（状態には関係しない項目）だけを保存する。"""
    def fn(s: Slip) -> None:
        if sync_pending is not None:
            s.sync_pending = sync_pending
        if posted_alert_at:
            for a in s.alerts:
                if a.get("at") == posted_alert_at:
                    a["posted"] = True
    try:
        deps.store.update_slip(slip, fn)
    except Exception:
        log.exception("反映の記録に失敗", extra={"slip_id": slip.slip_id})


def publish(deps: Deps, slip: Slip) -> None:
    """Slack の親メッセージと Asana のタスクを作る（作成済みなら作らない）。失敗したら例外（呼び出し元がやり直す）。"""
    channel = deps.settings.slack_channel
    if not slip.slack.get("thread_ts"):
        ts = deps.notifier.post(channel, texts.slack_parent(slip))
        link = {"channel_id": channel, "thread_ts": ts, "permalink": deps.notifier.permalink(channel, ts)}
        deps.store.update_slip(slip, lambda s: setattr(s, "slack", link))
    if not slip.asana.get("task_gid"):
        gid, url = deps.tracker.create_task(slip)
        deps.store.update_slip(slip, lambda s: setattr(s, "asana", {"task_gid": gid, "url": url}))
    sync(deps, slip, raise_errors=True)
