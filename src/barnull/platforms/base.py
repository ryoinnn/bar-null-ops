"""プラットフォーム操作の共通部分。"""

from __future__ import annotations

import contextvars
import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field

from ..models import Message


@dataclass
class ActionResult:
    ok: bool
    dry_run: bool = False
    note: str = ""
    screenshots: list[str] = field(default_factory=list)
    skipped: bool = False   # 実施済みだったので何もしなかった


class ActionFailed(RuntimeError):
    def __init__(self, step: str, message: str, screenshots: list[str] | None = None):
        super().__init__(f"{step}: {message}")
        self.step = step
        self.message = message
        self.screenshots = screenshots or []


class NotReady(ActionFailed):
    """承認は済んだが、ゲストの支払い待ちなどで、まだ定型文（暗証番号）を送れない"""


# レビュー投稿の最後のボタンを押した（True）／投稿されなかったと確定した（False）ことを、呼び出し元に知らせる。
# 押した後に時間切れ・停止した場合に、投稿されたか分からないまま再実行して二重に投稿しないため
REVIEW_SUBMIT_HOOK: contextvars.ContextVar[Callable[[bool], None] | None] = contextvars.ContextVar(
    "review_submit_hook", default=None)


def review_submitted(sent: bool = True) -> None:
    hook = REVIEW_SUBMIT_HOOK.get()
    if hook is not None:
        hook(sent)


class SendSkipped(Exception):
    """送信直前の確認で、送らないことにした（code: already_sent / manual_reply / new_guest_message）"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# 送信直前に画面のメッセージを見て、送るべきでなければ SendSkipped を投げる関数
SendGuard = Callable[[list[Message]], None]

# 定型文（暗証番号）を送ってよい予約の状態
SM_SEND_OK = ("予約完了", "予約確定")
SM_PAYMENT_WAIT = ("承認", "承認済み", "支払い待ち", "支払待ち")
# 承認待ちのリクエスト（2026-10-12 に「予約リクエスト」の表記を確認。以前の調査では「保留中」）
SM_PENDING = ("保留中", "予約リクエスト")
IB_SEND_OK = ("予約確定",)
IB_PAYMENT_WAIT = ("予約未確定",)


def norm_text(s: str) -> str:
    """送信文面と画面上の文面の比較用。空白・改行の有無や全角半角の違いは無視する
    （画面は改行や連続する空白をまとめて表示することがあり、送れているのに失敗と判定して再送しないように）"""
    s = unicodedata.normalize("NFKC", s or "")
    s = s.replace("\u200b", "").replace("\ufeff", "")
    return re.sub(r"\s+", "", s)


def same_text(a: str, b: str) -> bool:
    return norm_text(a) == norm_text(b)


def is_template_text(text: str, marker: str, code: str | None) -> bool:
    """定型文（暗証番号つき）か。マーカーの語だけでは、暗証番号に触れただけの返信も含まれてしまうので、
    暗証番号（分かる場合）か【数字】の形も求める"""
    if marker not in (text or ""):
        return False
    if code:
        return code in norm_text(text)
    return bool(re.search(r"【\s*\d{3,}\s*】", text))
