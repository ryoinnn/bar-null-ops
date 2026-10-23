"""Slack への通知（伝票ごとの親メッセージ、スレッド返信、チャンネルへの投稿）。"""

from __future__ import annotations

import logging
from typing import Protocol

from .http import ExternalAPIError, request

log = logging.getLogger(__name__)
API = "https://slack.com/api"


class Notifier(Protocol):
    def post(self, channel: str, text: str, thread_ts: str | None = None, broadcast: bool = False) -> str: ...
    def update(self, channel: str, ts: str, text: str) -> None: ...
    def permalink(self, channel: str, ts: str) -> str | None: ...


class SlackNotifier:
    def __init__(self, token: str) -> None:
        self.headers = {"Authorization": f"Bearer {token}"}

    def _call(self, method: str, payload: dict, *, get: bool = False, idempotent: bool = True) -> dict:
        if get:
            r = request("Slack", "GET", f"{API}/{method}", headers=self.headers, params=payload)
        else:
            r = request("Slack", "POST", f"{API}/{method}", headers=self.headers, json=payload, idempotent=idempotent)
        body = r.json()
        if not body.get("ok"):
            raise ExternalAPIError("Slack", method, r.status_code, str(body.get("error")))
        return body

    def post(self, channel: str, text: str, thread_ts: str | None = None, broadcast: bool = False) -> str:
        payload: dict = {"channel": channel, "text": text, "unfurl_links": False}
        if thread_ts:
            payload |= {"thread_ts": thread_ts, "reply_broadcast": broadcast}
        return self._call("chat.postMessage", payload, idempotent=False)["ts"]

    def update(self, channel: str, ts: str, text: str) -> None:
        self._call("chat.update", {"channel": channel, "ts": ts, "text": text})

    def permalink(self, channel: str, ts: str) -> str | None:
        try:
            return self._call("chat.getPermalink", {"channel": channel, "message_ts": ts}, get=True).get("permalink")
        except ExternalAPIError as e:
            log.warning("permalink の取得に失敗: %s", e)
            return None
