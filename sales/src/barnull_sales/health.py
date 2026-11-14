"""障害の通知: 定期処理が続けて失敗したら Slack に知らせ、知らせた後で直ったら「復旧」を1回だけ知らせる。

記録は Firestore の meta/job_health（ジョブごとの失敗回数・最後のエラー・知らせた時刻・最初の通知の ts）。
通知先はシステム監視のチャンネル（settings.monitor_channel。#null_system-monitor）。投稿できなければ業務のチャンネルに出す。
最初の通知は本人をメンションした親メッセージ、続いている間のリマインドと「復旧」はそのスレッドに付ける（2026-11-15 本人）。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from .errors import JobFailed
from .http import ExternalAPIError
from .texts import esc

log = logging.getLogger(__name__)
NOTIFY_AFTER = 2            # 続けてこの回数失敗したら知らせる（30分ごとのジョブなら約1時間）
RENOTIFY_HOURS = 8          # 失敗が続く間、この間隔でスレッドにリマインドする（2026-11-15 本人。6 → 8 時間）


def post(deps, text: str, thread_ts: str | None = None) -> tuple[str, str]:
    """監視のチャンネルに投稿する。失敗したら業務のチャンネルに出す（bot が未招待など）。戻り値: (チャンネル, ts)。"""
    s = deps.settings
    channels = [s.monitor_channel] + ([s.slack_channel] if s.slack_channel != s.monitor_channel else [])
    for i, ch in enumerate(channels):
        try:
            return ch, deps.notifier.post(ch, text, thread_ts=thread_ts if i == 0 else None)
        except Exception:
            if i == len(channels) - 1:
                raise
            log.exception("監視のチャンネルに投稿できない。業務のチャンネルに出す")
    raise AssertionError("unreachable")


def _thread(deps, state: dict) -> str | None:
    """最初の通知が監視のチャンネルに出ていれば、その ts（スレッドに付ける）。"""
    return state.get("thread_ts") if state.get("channel") == deps.settings.monitor_channel else None


def record(deps, job: str, error: BaseException | None) -> None:
    """ジョブの結果を記録する。error が None なら成功。Slack に出せなくても、ログには残る。"""
    try:
        doc = deps.store.get_doc("meta", "job_health") or {}
        state = doc.get(job) or {}
        now = datetime.now(UTC)
        if error is None:
            if state.get("notified_at"):
                took = f"（{span(datetime.fromisoformat(state['since']), now)}続いた）" if state.get("since") else ""
                post(deps, f"【復旧】定期処理「{job}」が正常に戻りました{took}", thread_ts=_thread(deps, state))
                if _thread(deps, state) and state.get("text"):
                    try:   # 親の先頭に【復旧済み】を付ける（ポータルが未復旧の通知を数えるときの目印）
                        deps.notifier.update(state["channel"], state["thread_ts"], "【復旧済み】" + state["text"])
                    except Exception:
                        log.exception("親メッセージの更新に失敗")
            if not state.get("failures") and not state.get("notified_at"):
                return            # 変化なし（書き込まない）
            state = {"failures": 0, "notified_at": None, "last_error": None}
        else:
            state = {**state, "failures": state.get("failures", 0) + 1, "last_error": str(error)[:300]}
            if not state.get("since"):
                state["since"] = now.isoformat()
            last = datetime.fromisoformat(state["notified_at"]) if state.get("notified_at") else None
            if state["failures"] >= NOTIFY_AFTER and not last:
                title, apis = classify(error)
                where = f"（{'、'.join(apis)}）" if apis else ""
                text = (f"{deps.settings.mention}【{title}】{deps.settings.store_name}: 定期処理「{job}」が"
                        f"{state['failures']}回続けて失敗しています{where}。\n"
                        f"直近のエラー：{esc(state['last_error'])}\n"
                        f"Cloud Logging で jsonPayload.job=\"{job}\" を絞り込むと詳しく見られます（runbook「システム監視」）。")
                ch, ts = post(deps, text)
                state |= {"notified_at": now.isoformat(), "channel": ch, "thread_ts": ts, "text": text}
            elif last and now - last >= timedelta(hours=RENOTIFY_HOURS):
                post(deps, f"{deps.settings.mention}まだ続いています（{span(datetime.fromisoformat(state['since']), now)}・"
                           f"{state['failures']}回）。直近のエラー：{esc(state['last_error'])}",
                     thread_ts=_thread(deps, state))
                state["notified_at"] = now.isoformat()
        deps.store.set_doc("meta", "job_health", {**doc, job: state})
    except Exception:
        log.exception("障害の記録・通知に失敗（Slack や Firestore に届かない可能性）")


def span(start: datetime, end: datetime) -> str:
    minutes = int((end - start).total_seconds() // 60)
    return f"{minutes // 60}時間{minutes % 60}分" if minutes >= 60 else f"{minutes}分"


def classify(error: BaseException) -> tuple[str, list[str]]:
    """通知の見出しと、原因になった連携先。連携先の失敗だけなら「外部API障害」、それ以外は「処理エラー」。"""
    if isinstance(error, ExternalAPIError):
        return "外部API障害", [error.api]
    if isinstance(error, JobFailed):
        return ("外部API障害" if error.all_external else "処理エラー"), error.apis
    return "処理エラー", []
