"""伝票（顧客1件 = 1伝票）のデータと、その状態の決め方。

状態（伝票ステータス・未収ステータス・完了）は、事実（決済・入金・未収・アラート）から毎回計算する（derive）。
各処理は事実だけを書き換え、状態は書き換えない。保存は必ず slip_ops.save を通す（そこで derive する）。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from enum import StrEnum

from . import bizdate
from .config import Settings
from .extract import Entry


class Status(StrEnum):
    DRAFTED = "起票済み"
    PAID = "決済確認済み"
    DEPOSITED = "入金確認完了"
    REVIEW = "要確認"
    CANCELLED = "対象外"


class Unpaid(StrEnum):
    NONE = "なし"
    OPEN = "未収入金有"
    REPORTED = "回収報告済み"
    COLLECTED = "回収入金確認済み"


class Alert(StrEnum):
    """アラートの種類。derive が、意味の無くなったアラートを自動で閉じる（決済が確認できたら決済のアラート、など）。
    それ以外は人が Asana の「手動上書き」で閉じる。"""
    PARSE = "解析の不備"
    PAYMENT_MISSING = "決済未確認"
    AMBIGUOUS = "突合候補が複数"
    DATE_SHIFT = "日付ズレ疑い"
    UNPAID_MISMATCH = "金額不一致"
    REFUND = "返金検知"
    DEPOSIT_MISSING = "入金未確認"
    PAYER_UNCLEAR = "回収の振込か不明"
    CORRECTION_AFTER_CLOSE = "完了後の訂正"
    PAYMENT_CONTESTED = "決済の取り合い"     # 同じ金額の決済が1件しか無く、別の伝票に割り当て済み
    RESTORED = "復元した伝票"


PAYMENT_ALERTS = (Alert.PAYMENT_MISSING, Alert.AMBIGUOUS, Alert.DATE_SHIFT)


class Manual(StrEnum):
    """Asana の「手動上書き」の選択肢（overrides.py が反映する）。"""
    NONE = "なし"
    PAID = "手動：決済確認済み"
    DEPOSITED = "手動：入金確認済み"
    COLLECTED = "手動：未収回収済み"
    CANCELLED = "手動：対象外（取消）"


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def plain(v):
    """保存用に、列挙型（Status など）をただの文字列にする（入れ子も）。"""
    if isinstance(v, StrEnum):
        return str(v.value)
    if isinstance(v, dict):
        return {k: plain(x) for k, x in v.items()}
    if isinstance(v, list | tuple):
        return [plain(x) for x in v]
    return v


@dataclass
class Slip:
    slip_id: str
    event_id: str
    entry_index: int
    group_id: str
    message_id: str
    sender_line_user_id: str | None
    sender_display_name: str | None
    sent_at: str                       # ISO（UTC）
    raw_text: str
    business_date: str | None          # YYYY-MM-DD
    business_date_source: str          # message / inferred
    customer_name: str | None
    slip_amount: int | None
    note: str
    unpaid_amount: int = 0
    unpaid_remaining: int = 0
    unpaid_status: str = Unpaid.NONE              # derive が決める
    unpaid_due_text: str = ""
    payment_hint: str = ""
    expected_square_amount: int | None = None     # 伝票金額 − 未収
    status: str = Status.DRAFTED                  # derive が決める
    status_history: list[dict] = field(default_factory=list)
    alerts: list[dict] = field(default_factory=list)       # {type, message, at, resolved_at, posted}
    square: dict = field(default_factory=dict)    # payment_ids, payments, amount, fee, summary, manual, cancelled
    deposits: dict = field(default_factory=dict)  # expected: [{key, kind, amount, share, confirmed}], built_for, completed_at, manual
    closed_at: str | None = None                  # derive が決める（すべて確認できたら）
    collection_reports: list[dict] = field(default_factory=list)   # 未収の回収（報告・着金）
    payer_checks: dict = field(default_factory=dict)  # 振込名義の判定の結果 {txn_id: {same, confidence, alerted}}
    reminder_count: int = 0
    slack: dict = field(default_factory=dict)     # channel_id, thread_ts, permalink
    asana: dict = field(default_factory=dict)     # task_gid, url
    sync_pending: bool = False                    # Slack・Asana への反映が失敗し、やり直しが必要
    dry_run: bool = True
    created_at: str = field(default_factory=now_iso)

    def to_dict(self) -> dict:
        return plain(asdict(self))

    @classmethod
    def from_dict(cls, d: dict) -> Slip:
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in d.items() if k in known})

    # ---------------------------------------------------------------- 事実
    @property
    def cancelled(self) -> bool:
        return bool(self.square.get("cancelled"))

    @property
    def payment_ids(self) -> list[str]:
        return list(self.square.get("payment_ids") or [])

    @property
    def main_payment_ids(self) -> list[str]:
        """その日の会計の決済（未収を後日回収した決済を除く）。"""
        records = self.square.get("payments")
        if not records:
            return self.payment_ids
        return [p["id"] for p in records if not p.get("unpaid_collection")]

    @property
    def payment_settled(self) -> bool:
        """その日の会計の決済が確認できたか（全額未収で決済が無い伝票は、確認するものが無いので済み）。"""
        return bool(self.main_payment_ids) or bool(self.square.get("manual")) or self.expected_square_amount == 0

    @property
    def deposit_settled(self) -> bool:
        """銀行への入金が確認できたか。Square の決済が1つも無ければ、自動で確認するものが無いので済み
        （全額未収、または人が決済を確認した伝票）。"""
        d = self.deposits
        if d.get("completed_at") or d.get("manual"):
            return True
        return self.payment_settled and not self.payment_ids

    @property
    def needs_payment(self) -> bool:
        """Square の決済を探す対象か。"""
        return not self.closed_at and self.expected_square_amount is not None and not self.payment_settled

    @property
    def awaiting_deposit(self) -> bool:
        """銀行の入金を探す対象か。"""
        return not self.closed_at and bool(self.payment_ids) and not self.deposit_settled

    def open_alerts(self, *kinds: str) -> list[dict]:
        return [a for a in self.alerts if not a.get("resolved_at") and (not kinds or a["type"] in kinds)]


def derive(slip: Slip) -> None:
    """事実から、伝票ステータス・未収ステータス・完了を決める。"""
    if slip.unpaid_amount <= 0:
        slip.unpaid_status = Unpaid.NONE
    elif slip.unpaid_remaining <= 0:
        slip.unpaid_status = Unpaid.COLLECTED
    elif any(not r.get("confirmed") for r in slip.collection_reports):
        slip.unpaid_status = Unpaid.REPORTED
    else:
        slip.unpaid_status = Unpaid.OPEN

    _resolve_moot_alerts(slip)
    if slip.cancelled:
        slip.status = Status.CANCELLED
        slip.closed_at = slip.closed_at or now_iso()
        return
    if slip.closed_at:          # 完了した伝票は凍結する（後から来たアラートは通知だけ）
        return
    if slip.open_alerts():
        slip.status = Status.REVIEW
    elif slip.payment_settled and slip.deposit_settled:
        slip.status = Status.DEPOSITED
    elif slip.payment_settled:
        slip.status = Status.PAID
    else:
        slip.status = Status.DRAFTED
    if slip.status == Status.DEPOSITED and slip.unpaid_remaining <= 0:
        slip.closed_at = now_iso()


def _resolve_moot_alerts(slip: Slip) -> None:
    """条件が解消して意味の無くなったアラートを閉じる。"""
    moot = []
    if slip.payment_settled:
        moot += PAYMENT_ALERTS
    if slip.deposit_settled:
        moot.append(Alert.DEPOSIT_MISSING)
    if slip.unpaid_remaining <= 0:
        moot.append(Alert.PAYER_UNCLEAR)
    now = now_iso()
    for a in slip.open_alerts(*moot) if moot else []:
        a["resolved_at"] = now


def business_date_of(entry: Entry, sent_at: datetime, settings: Settings) -> date | None:
    """伝票に書かれた月日を、送信日時から見た日付にする（書かれていない・ありえない日付なら None）。"""
    if not (entry.month and entry.day):
        return None
    return bizdate.resolve_month_day(entry.month, entry.day, sent_at, settings.tz, settings.day_start)


def build_slip(*, slip_id: str, event_id: str, index: int, entry: Entry, group_id: str, message_id: str,
               sender_id: str | None, sender_name: str | None, sent_at: datetime, raw_text: str,
               settings: Settings) -> Slip:
    d = business_date_of(entry, sent_at, settings)
    unpaid = entry.unpaid_amount or 0
    slip = Slip(
        slip_id=slip_id, event_id=event_id, entry_index=index, group_id=group_id, message_id=message_id,
        sender_line_user_id=sender_id, sender_display_name=sender_name,
        sent_at=sent_at.astimezone(UTC).isoformat(), raw_text=raw_text,
        business_date=d.isoformat() if d else None, business_date_source="message" if d else "inferred",
        customer_name=entry.customer_name, slip_amount=entry.amount, note=entry.note or "",
        unpaid_amount=unpaid, unpaid_remaining=unpaid,
        unpaid_due_text=entry.unpaid_due_text or "", payment_hint=entry.payment_hint or "",
        expected_square_amount=max(entry.amount - unpaid, 0) if entry.amount is not None else None,
        dry_run=settings.dry_run,
    )
    problems = []
    if entry.amount is None:
        problems.append("金額が読み取れない")
    if not entry.customer_name:
        problems.append("顧客名が読み取れない")
    if entry.month and entry.day and not d:
        problems.append("日付が正しくない")
    if not sender_id:
        problems.append("送信者が分からない（LINE のプロフィール未同意）")
    if problems:
        slip.alerts.append({"type": Alert.PARSE, "message": "、".join(problems), "at": slip.created_at,
                            "resolved_at": None, "posted": False})
    derive(slip)
    slip.status_history.append({"from": None, "to": slip.status, "at": slip.created_at, "reason": "起票"})
    return slip
