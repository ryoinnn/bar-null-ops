"""状態・種類の定義と、管理画面から読み取ったデータの型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum


class Platform(StrEnum):
    SM = "sm"
    IB = "ib"

    @property
    def label(self) -> str:
        return {"sm": "スペースマーケット", "ib": "インスタベース"}[self.value]


class Kind(StrEnum):
    RESERVATION = "reservation"
    INQUIRY_ONLY = "inquiry_only"  # SM の予約前問い合わせ（予約に至っていない）


class ResState(StrEnum):
    NOTIFIED = "NOTIFIED"
    ON_HOLD = "ON_HOLD"
    EXECUTING = "EXECUTING"
    CONFIRMED = "CONFIRMED"
    COMPLETING = "COMPLETING"
    REVIEW_CONFIRM = "REVIEW_CONFIRM"
    ERROR = "ERROR"            # 定型文の送信に失敗（スタンプで再実行）
    REVIEW_ERROR = "REVIEW_ERROR"  # レビューの投稿に失敗（定型文の失敗とは分ける。スタンプでレビューを再実行）
    CLOSED = "CLOSED"
    MANUAL = "MANUAL"  # IB の承認制リクエスト等、手動対応待ち（承認されたら NOTIFIED へ）


ACTIVE_RES_STATES = (
    ResState.NOTIFIED, ResState.ON_HOLD, ResState.EXECUTING, ResState.CONFIRMED,
    ResState.COMPLETING, ResState.REVIEW_CONFIRM, ResState.ERROR, ResState.REVIEW_ERROR, ResState.MANUAL,
)
# 処理の途中を表す状態（プロセスが止まると取り残されるので、起動時に回収する）
BUSY_RES_STATES = (ResState.EXECUTING, ResState.COMPLETING)


class InqState(StrEnum):
    DRAFT_POSTED = "DRAFT_POSTED"
    ON_HOLD = "ON_HOLD"
    APPROVED = "APPROVED"      # 承認済み。定型文の送信後に送る（予約時の問い合わせ）
    SENDING = "SENDING"
    SENT = "SENT"
    SENT_MANUALLY = "SENT_MANUALLY"
    ERROR = "ERROR"
    SUPERSEDED = "SUPERSEDED"
    NO_DRAFT = "NO_DRAFT"  # 文案を作れなかった（手動対応を依頼済み）


OPEN_INQ_STATES = (InqState.DRAFT_POSTED, InqState.ON_HOLD, InqState.ERROR, InqState.NO_DRAFT)
UNSENT_INQ_STATES = (*OPEN_INQ_STATES, InqState.APPROVED)


class MsgKind(StrEnum):
    """Slack の投稿の種類（スタンプの対象を決めるため ts ごとに保存）"""
    PARENT = "parent"                  # 予約・問い合わせの親メッセージ
    DRAFT = "draft"                    # 返信文案
    REMINDER = "reminder"              # 保留・未送信のリマインド
    ERROR = "error"                    # 失敗（スタンプで再実行）
    REVIEW_CONFIRM = "review_confirm"  # 退室に問題あり。スタンプでレビュー投稿
    REVISION_CONFIRM = "revision_confirm"  # 修正文案の確認。スタンプでその文面を送信
    INFO = "info"


# ---------- 管理画面から読み取ったもの ----------

@dataclass
class Message:
    """スレッドの1発言（古い順に並べる）"""
    role: str                 # "host" | "guest" | "system"
    text: str
    files: list[str] = field(default_factory=list)
    at: datetime | None = None
    index: int = 0            # 画面上の通し番号（古い順、0 始まり）
    uid: str | None = None    # プラットフォームのメッセージID（取れる場合）
    at_dom: datetime | None = None  # SM: 画面の表示から読んだ時刻（API の正確な時刻で上書きする前の値）


def _hash(m: Message) -> str:
    import hashlib

    return hashlib.sha1(f"{m.role}|{m.text}|{','.join(m.files)}".encode()).hexdigest()[:12]


def message_keys(messages: list[Message]) -> dict[int, str]:
    """発言ごとの識別キー（index → key）。本文＋（同文の出現順）で作る。
    以前は「ID → 日時＋本文」を優先していたが、SM はメッセージ API を受け取れたかどうかで ID・時刻の有無が変わり、
    同じ発言が別のキーになって新着と誤認された。本文と出現順だけなら、読み方に左右されない。
    同じ文面の新着が既存のキーと重ならないよう、出現順は古い方から数える"""
    keys: dict[int, str] = {}
    seen: dict[str, int] = {}
    for m in messages:
        h = _hash(m)
        n = seen.get(h, 0)
        seen[h] = n + 1
        keys[m.index] = f"h:{h}#{n}"
    return keys


def message_key_variants(messages: list[Message]) -> dict[int, set[str]]:
    """発言ごとの、過去の版で使っていたキーも含めた全キー（処理済みかの判定と、文案の元発言の照合に使う）"""
    stable = message_keys(messages)
    out: dict[int, set[str]] = {}
    for m in messages:
        ks = {stable[m.index]}
        if m.uid:
            ks.add(f"id:{m.uid}")
        for t in (m.at, m.at_dom):
            if t:
                ks.add(f"at:{t.isoformat()}|{_hash(m)}")
        out[m.index] = ks
    return out


@dataclass
class Detail:
    """予約（または問い合わせ）の詳細"""
    platform: Platform
    platform_id: str
    kind: Kind = Kind.RESERVATION
    status: str = ""                      # 管理画面の表記そのまま（予約完了、予約確定、利用完了、期限切れ…）
    guest_name: str | None = None
    guest_id: str | None = None
    start_at: datetime | None = None
    end_at: datetime | None = None
    prep_start_at: datetime | None = None
    cleanup_end_at: datetime | None = None
    people: str | None = None
    purpose: str | None = None
    purpose_detail: str | None = None
    options: str | None = None
    plan: str | None = None
    guest_total: int | None = None
    host_revenue: int | None = None
    messages: list[Message] = field(default_factory=list)
    thread_id: str | None = None          # IB のスレッドID
    can_approve: bool = False             # SM: 承認ボタンがある
    exit_status: str | None = None        # SM: 退室時状態（「問題なく退室しました」等）
    exit_detail: str | None = None
    review_posted: bool | None = None     # この予約にホストのレビューを投稿済みか（SM は予約情報の API で判定。不明なら None）
    review_open: bool | None = None       # SM: レビューを書ける状態か（予約情報の can_be_reviewed）
    review_expires_at: datetime | None = None  # SM: レビューの投稿期限
    change_request: bool = False          # ゲストからの変更リクエストが回答待ち
    raw_status_text: str = ""
    applied_at: datetime | None = None    # IB: 申込日時（この予約の発言の起点を決めるのに使う）

    @property
    def closed_reason(self) -> str | None:
        """キャンセル・否認・期限切れなら理由を返す"""
        s = self.status
        for word in ("キャンセル", "否認", "期限切れ", "非承認"):
            if word in s:
                return s
        return None


@dataclass
class ListingRow:
    """一覧の1行"""
    platform: Platform
    platform_id: str
    status: str
    datetime_text: str = ""
    guest_name: str | None = None
    guest_id: str | None = None
    price_text: str = ""
    substatus: str = ""
    extra: str = ""

    @property
    def fingerprint(self) -> str:
        return "|".join([self.status, self.substatus, self.datetime_text, self.price_text, self.extra])
