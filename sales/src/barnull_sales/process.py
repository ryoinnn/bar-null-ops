"""LINE イベント1件の処理（sales-worker の /tasks/process-event。Cloud Tasks から呼ばれる）。

- 処理済みのイベントは何もしない（line_events に processed_at）
- Claude の解析結果は line_events に保存し、再試行のときは解析し直さない
- 失敗したら例外を投げ、Cloud Tasks がやり直す。最後の試行でも失敗したら、Slack に原文つきで知らせる
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from . import logs, texts
from .deps import Deps
from .extract import Parsed
from .line import strip_mention
from .messages import Msg, handle
from .slips import now_iso
from .texts import esc

log = logging.getLogger(__name__)
MAX_GROUPS = 50       # 参加・未登録グループの記録の上限（招待を繰り返されても記録と通知が増え続けないように）


def event_id_of(event: dict) -> str:
    return event.get("webhookEventId") or (event.get("message") or {}).get("id") or ""


def handle_event(event: dict, kind: str, deps: Deps, *, attempt: int = 1) -> dict:
    """attempt: Cloud Tasks の何回目の試行か（1から）。"""
    event_id = event_id_of(event)
    with logs.context(job="process_event", event_id=event_id):
        rec = deps.store.get_doc("line_events", event_id) or {}
        if rec.get("processed_at"):
            log.info("処理済みのイベント")
            return {"status": "duplicate", "slip_ids": rec.get("slip_ids", [])}
        if not rec:
            rec = {"kind": kind, "received_at": now_iso()}
            deps.store.set_doc("line_events", event_id, rec)
        try:
            result = _dispatch(event, kind, event_id, rec, deps)
        except Exception as e:
            if attempt < deps.settings.tasks_max_attempts:
                raise
            log.exception("最後の試行でも処理できなかった")
            _give_up(event, event_id, deps, e)
            result = {"status": "failed", "error": str(e)[:300]}
        deps.store.set_doc("line_events", event_id, {**rec, **result, "processed_at": now_iso()})
        return result


def _dispatch(event: dict, kind: str, event_id: str, rec: dict, deps: Deps) -> dict:
    if kind == "join":
        return _join(event, deps)
    group_id = (event.get("source") or {}).get("groupId", "")
    if kind == "unknown_group_message" or group_id not in deps.settings.allowed_group_ids:
        return _unknown_group(event, deps)
    msg = _to_msg(event, event_id, deps)
    if "parsed" in rec:
        parsed = Parsed.from_json(rec["parsed"]) if rec["parsed"] else None
    else:
        parsed = deps.extractor.parse(msg.text, msg.sent_at)
        rec["parsed"] = parsed.to_json() if parsed else None
        deps.store.set_doc("line_events", event_id, rec)
    acked = {"done": False}

    def on_accepted() -> None:
        if not acked["done"]:
            acked["done"] = _ack(event, deps)

    result = handle(deps, msg, parsed, on_accepted)
    return {**result, **({"acked": True} if acked["done"] else {})}


def _to_msg(event: dict, event_id: str, deps: Deps) -> Msg:
    src, m = event.get("source") or {}, event.get("message") or {}
    group_id, user_id = src.get("groupId", ""), src.get("userId")
    sender = None
    if user_id and deps.line:
        try:
            sender = deps.line.member_display_name(group_id, user_id)
        except Exception:
            log.warning("表示名の取得に失敗", exc_info=True)
    return Msg(event_id=event_id, group_id=group_id, message_id=m.get("id", ""), user_id=user_id, sender=sender,
               text=strip_mention(m, deps.settings.bot_mention_name),
               sent_at=datetime.fromtimestamp(event.get("timestamp", 0) / 1000, tz=UTC))


def _ack(event: dict, deps: Deps) -> bool:
    """受け付けた @Bot メッセージに、引用付きの「OK」スタンプを返す。返せたら True。"""
    token = event.get("replyToken")
    if not deps.settings.reply_ack or not deps.line or not token:
        return False
    try:
        deps.line.reply_ok_sticker(token, (event.get("message") or {}).get("quoteToken"))
        return True
    except Exception:  # 返信トークンの期限切れなど。処理そのものは続ける
        log.warning("LINE へのスタンプの返信に失敗", exc_info=True)
        return False


def _give_up(event: dict, event_id: str, deps: Deps, error: Exception) -> None:
    s = deps.settings
    text = strip_mention(event.get("message") or {}, s.bot_mention_name)
    made = [x.slip_id for x in deps.store.find_slips("event_id", "==", event_id)]
    todo = (f"伝票 {'、'.join(made)} は作成済み（Slack・Asana は30分ごとのジョブが作る）。二重に起票しないでください。"
            if made else "手で起票・訂正してください。")
    deps.notifier.post(s.slack_channel,
                       f"{s.mention}【処理できなかったメッセージ】{s.tasks_max_attempts}回試しても処理できませんでした。"
                       f"{todo}\n原因：{esc(str(error)[:300])}\n{texts.quote(text)}")


def _group_name(deps: Deps, group_id: str) -> str:
    try:
        return (deps.line.group_name(group_id) if deps.line else None) or "（名前を取得できない）"
    except Exception:
        log.warning("グループ名の取得に失敗", exc_info=True)
        return "（名前を取得できない）"


def _remember_group(deps: Deps, key: str, group_id: str, data: dict) -> bool:
    """グループを記録する。初めてなら True（通知する）。上限を超えたら記録も通知もしない。"""
    seen = deps.store.get_doc("meta", key) or {}
    if group_id in seen or len(seen) >= MAX_GROUPS:
        return False
    deps.store.set_doc("meta", key, {**seen, group_id: data})
    return True


def _join(event: dict, deps: Deps) -> dict:
    group_id = (event.get("source") or {}).get("groupId", "")
    name = _group_name(deps, group_id)
    if _remember_group(deps, "line_groups", group_id, {"name": name, "joined_at": now_iso()}):
        allowed = group_id in deps.settings.allowed_group_ids
        deps.notifier.post(deps.settings.slack_channel,
                           f"LINE の Bot がグループ「{esc(name)}」に参加しました。\ngroupId: `{esc(group_id)}`\n"
                           + ("処理対象のグループです。" if allowed else
                              "まだ処理対象ではありません（sales.toml の allowed_group_ids に追加すると処理します）。"))
    return {"status": "joined"}


def _unknown_group(event: dict, deps: Deps) -> dict:
    group_id = (event.get("source") or {}).get("groupId", "")
    if _remember_group(deps, "unknown_groups_notified", group_id, {"at": now_iso()}):
        name = _group_name(deps, group_id)
        deps.notifier.post(deps.settings.slack_channel,
                           f"処理対象ではない LINE グループ「{esc(name)}」で Bot へのメンションがありました（処理していません）。\n"
                           f"groupId: `{esc(group_id)}`")
    return {"status": "ignored_group"}
