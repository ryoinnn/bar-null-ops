"""障害の通知: 定期処理が続けて失敗したら Slack に知らせ、知らせた後で直ったら「復旧」を1回だけ知らせる。

記録は Firestore の meta/job_health（ジョブごとの失敗回数・最後のエラー・知らせた時刻）。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from .errors import JobFailed
from .http import ExternalAPIError
from .texts import esc

log = logging.getLogger(__name__)
NOTIFY_AFTER = 2            # 続けてこの回数失敗したら知らせる（30分ごとのジョブなら約1時間）
RENOTIFY_HOURS = 6          # 失敗が続く間、この間隔でもう一度知らせる


def record(deps, job: str, error: BaseException | None) -> None:
    """ジョブの結果を記録する。error が None なら成功。Slack に出せなくても、ログには残る。"""
    try:
        doc = deps.store.get_doc("meta", "job_health") or {}
        state = doc.get(job) or {}
        now = datetime.now(UTC)
        if error is None:
            if state.get("notified_at"):
                deps.notifier.post(deps.settings.slack_channel, f"【復旧】定期処理「{job}」が正常に戻りました")
            if not state.get("failures") and not state.get("notified_at"):
                return            # 変化なし（書き込まない）
            state = {"failures": 0, "notified_at": None, "last_error": None}
        else:
            state = {**state, "failures": state.get("failures", 0) + 1, "last_error": str(error)[:300]}
            last = datetime.fromisoformat(state["notified_at"]) if state.get("notified_at") else None
            if state["failures"] >= NOTIFY_AFTER and (not last or now - last >= timedelta(hours=RENOTIFY_HOURS)):
                title, apis = classify(error)
                where = f"（{'、'.join(apis)}）" if apis else ""
                deps.notifier.post(deps.settings.slack_channel,
                                   f"{deps.settings.mention}【{title}】定期処理「{job}」が{state['failures']}回続けて失敗しています"
                                   f"{where}。\n"
                                   f"直近のエラー：{esc(state['last_error'])}\n"
                                   f"Cloud Logging で jsonPayload.job=\"{job}\" を絞り込むと詳しく見られます。")
                state["notified_at"] = now.isoformat()
        deps.store.set_doc("meta", "job_health", {**doc, job: state})
    except Exception:
        log.exception("障害の記録・通知に失敗（Slack や Firestore に届かない可能性）")


def classify(error: BaseException) -> tuple[str, list[str]]:
    """通知の見出しと、原因になった連携先。連携先の失敗だけなら「外部API障害」、それ以外は「処理エラー」。"""
    if isinstance(error, ExternalAPIError):
        return "外部API障害", [error.api]
    if isinstance(error, JobFailed):
        return ("外部API障害" if error.all_external else "処理エラー"), error.apis
    return "処理エラー", []
