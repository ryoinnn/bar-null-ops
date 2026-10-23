"""LINE Messaging API: 署名検証、対象イベントの判定、メンションの除去、表示名の取得、OK スタンプの返信。"""

from __future__ import annotations

import base64
import hashlib
import hmac

from .http import request

API = "https://api.line.me/v2/bot"
OK_STICKER = ("6362", "11087920")   # ブラウンの「OK」（Messaging API で送れる公式スタンプ）


def verify_signature(channel_secret: str, body: bytes, signature: str | None) -> bool:
    if not signature:
        return False
    mac = hmac.new(channel_secret.encode("utf-8"), body, hashlib.sha256).digest()
    return hmac.compare_digest(base64.b64encode(mac).decode("ascii"), signature)


def is_bot_mentioned(message: dict, bot_mention_name: str) -> bool:
    """Bot へのメンション（一覧から選んだもの = isSelf）か、本文が「@Bot名」で始まるか。"""
    for m in (message.get("mention") or {}).get("mentionees", []) or []:
        if m.get("isSelf"):
            return True
    text = (message.get("text") or "").lstrip()
    name = bot_mention_name.strip()
    return bool(name) and (text == name or any(text.startswith(name + sep) for sep in (" ", "\n", "　")))


def classify_event(event: dict, allowed_group_ids: tuple[str, ...], bot_mention_name: str) -> str | None:
    """処理するイベントの種類を返す。対象外なら None（保存も処理もしない）。

    - "join": Bot がグループに参加した（グループ ID を知らせるため）
    - "message": 許可されたグループの、Bot へのメンションで始まるテキスト
    - "unknown_group_message": 許可されていないグループでの Bot へのメンション（ID を知らせるため）
    """
    src = event.get("source") or {}
    if event.get("type") == "join" and src.get("type") == "group":
        return "join"
    if event.get("type") != "message" or src.get("type") != "group":
        return None
    msg = event.get("message") or {}
    if msg.get("type") != "text" or not is_bot_mentioned(msg, bot_mention_name):
        return None
    if src.get("groupId") not in allowed_group_ids:
        return "unknown_group_message"
    return "message"


def strip_mention(message: dict, bot_mention_name: str) -> str:
    """Bot へのメンション部分を本文から除く。"""
    text = message.get("text") or ""
    spans = [(m["index"], m["length"]) for m in (message.get("mention") or {}).get("mentionees", []) or []
             if m.get("isSelf") and "index" in m and "length" in m]
    for index, length in sorted(spans, reverse=True):
        text = text[:index] + text[index + length:]
    stripped = text.lstrip()
    if stripped.startswith(bot_mention_name):
        stripped = stripped[len(bot_mention_name):]
    return stripped.strip()


class LineClient:
    def __init__(self, access_token: str) -> None:
        self.headers = {"Authorization": f"Bearer {access_token}"}

    def _get(self, path: str) -> dict | None:
        r = request("LINE", "GET", f"{API}{path}", headers=self.headers, allow=(404,), timeout=15)
        return None if r.status_code == 404 else r.json()

    def member_display_name(self, group_id: str, user_id: str) -> str | None:
        return (self._get(f"/group/{group_id}/member/{user_id}") or {}).get("displayName")

    def group_name(self, group_id: str) -> str | None:
        return (self._get(f"/group/{group_id}/summary") or {}).get("groupName")

    def reply_ok_sticker(self, reply_token: str, quote_token: str | None) -> None:
        """受け付けたメッセージを引用して「OK」スタンプを返す（Messaging API にリアクションの機能は無いため）。
        reply は無料（月の送信数に数えない）。返信トークンは一度しか使えず、有効期間も短いので、やり直さない。"""
        msg = {"type": "sticker", "packageId": OK_STICKER[0], "stickerId": OK_STICKER[1]}
        if quote_token:
            msg["quoteToken"] = quote_token
        request("LINE", "POST", f"{API}/message/reply", headers=self.headers, attempts=1, timeout=15,
                json={"replyToken": reply_token, "messages": [msg]})
