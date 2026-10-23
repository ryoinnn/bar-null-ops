"""Claude による @Bot メッセージの分類と値の取り出し。

まず Haiku で解析し（結果が不正なら Haiku でもう1回）、それでも失敗・確信度が低いときだけ Sonnet で解析し直す。
推測で埋めないこと（読めない値は null）をプロンプトで指示し、出力は JSON スキーマで受け取って検証する。
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Protocol

from .http import ExternalAPIError

log = logging.getLogger(__name__)

KINDS = ("slip_content", "correction", "unpaid_collection_report", "cash_deposit_report", "other")
FALLBACK_BETA = "server-side-fallback-2026-07-01"
MIN_CONFIDENCE = 0.6


def _nullable(t: dict) -> dict:
    return {"anyOf": [t, {"type": "null"}]}


SCHEMA = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": list(KINDS)},
        "confidence": {"type": "number"},
        "entries": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "month": _nullable({"type": "integer"}),
                    "day": _nullable({"type": "integer"}),
                    "customer_name": _nullable({"type": "string"}),
                    "amount": _nullable({"type": "integer"}),
                    "note": {"type": "string"},
                    "unpaid_amount": _nullable({"type": "integer"}),
                    "unpaid_due_text": {"type": "string"},
                    "payment_hint": {"type": "string"},
                },
                "required": ["month", "day", "customer_name", "amount", "note", "unpaid_amount",
                             "unpaid_due_text", "payment_hint"],
                "additionalProperties": False,
            },
        },
        "correction": _nullable({
            "type": "object",
            "properties": {
                "target_customer_name": _nullable({"type": "string"}),
                "new_amount": _nullable({"type": "integer"}),
                "new_customer_name": _nullable({"type": "string"}),
                "description": {"type": "string"},
            },
            "required": ["target_customer_name", "new_amount", "new_customer_name", "description"],
            "additionalProperties": False,
        }),
        "collection_report": _nullable({
            "type": "object",
            "properties": {
                "customer_name": _nullable({"type": "string"}),
                "amount": _nullable({"type": "integer"}),
                "method": {"type": "string", "enum": ["bank_transfer", "cash", "square", "unknown"]},
            },
            "required": ["customer_name", "amount", "method"],
            "additionalProperties": False,
        }),
        "cash_deposit": _nullable({
            "type": "object",
            "properties": {
                "customer_names": {"type": "array", "items": {"type": "string"}},
                "total_amount": _nullable({"type": "integer"}),
                "deposit_date_text": {"type": "string"},
            },
            "required": ["customer_names", "total_amount", "deposit_date_text"],
            "additionalProperties": False,
        }),
    },
    "required": ["kind", "confidence", "entries", "correction", "collection_report", "cash_deposit"],
    "additionalProperties": False,
}

SYSTEM = """あなたはバー「Bar null」のスタッフ用 LINE グループで、Bot 宛てに送られたメッセージを分類し、値を取り出す担当です。

kind の決め方:
- slip_content: 会計伝票の内容（日付・顧客名・金額・備考）。1つのメッセージに複数の顧客分が並ぶことがある（顧客ごとに entries を1件）
- correction: 直前に送った伝票の内容の訂正（金額・顧客名・日付など）
- unpaid_collection_report: 未収金（後払い）を回収した、振込があった、という報告
- cash_deposit_report: 現金の売上を銀行に入金した報告（複数日分をまとめて入金した、など）
- other: 上のどれでもない

値のルール:
- 推測で埋めない。書かれていない値、読めない値は null（文字列の項目は空文字）。
- 金額は円の整数。「4万」は 40000、「10万5千」は 105000。
- 顧客名は敬称（様・さま・さん）を除いた名前。
- note は備考の原文（無い・「なし」なら空文字）。
- 備考に未収（後払い）の金額があれば unpaid_amount、回収予定日の記述があれば unpaid_due_text。
- 支払方法の手がかり（カード、現金、割り勘、振込など）があれば payment_hint に短く書く。
- kind が slip_content 以外なら entries は空。対応する correction / collection_report / cash_deposit だけを埋め、ほかは null。
- correction の target_customer_name は訂正する伝票の顧客名（書かれていなければ null）、new_amount は訂正後の金額、new_customer_name は顧客名の訂正があれば訂正後の名前。
- confidence は分類と値の取り出しの自信（0〜1）。"""


@dataclass
class Entry:
    month: int | None
    day: int | None
    customer_name: str | None
    amount: int | None
    note: str = ""
    unpaid_amount: int | None = None
    unpaid_due_text: str = ""
    payment_hint: str = ""


@dataclass
class Parsed:
    kind: str
    confidence: float
    entries: list[Entry] = field(default_factory=list)
    correction: dict | None = None
    collection_report: dict | None = None
    cash_deposit: dict | None = None
    model: str = ""
    usage: dict = field(default_factory=dict)

    @classmethod
    def from_json(cls, data: dict, model: str = "", usage: dict | None = None) -> Parsed:
        if data.get("kind") not in KINDS:
            raise ValueError(f"不明な kind: {data.get('kind')}")
        entries = [Entry(**{**e, "amount": _amount(e.get("amount")), "unpaid_amount": _amount(e.get("unpaid_amount"))})
                   for e in data.get("entries") or []]
        correction = data.get("correction")
        if correction:
            correction = {**correction, "new_amount": _amount(correction.get("new_amount"))}
        return cls(kind=data["kind"], confidence=float(data.get("confidence", 0)), entries=entries,
                   correction=correction, collection_report=data.get("collection_report"),
                   cash_deposit=data.get("cash_deposit"), model=model or data.get("model", ""),
                   usage=usage or data.get("usage") or {})

    def to_json(self) -> dict:
        """line_events に保存する形（再試行のときに解析し直さないため）。"""
        return asdict(self)


def _amount(v) -> int | None:
    """金額は0以上の整数だけを受け付ける（それ以外は読めなかったものとして null）。"""
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else None


PAYER_SCHEMA = {
    "type": "object",
    "properties": {"same_person": {"type": "boolean"}, "confidence": {"type": "number"}, "reason": {"type": "string"}},
    "required": ["same_person", "confidence", "reason"],
    "additionalProperties": False,
}
PAYER_SYSTEM = """銀行の振込明細の摘要（振込人の名義。半角カナのことが多い）と、バーの顧客名（漢字のことが多い）が、
同じ人（またはその人の会社）からの振込と考えられるかを判定してください。
- 読みが一致するか（例:「ヤマダ タロウ」と「山田」）、会社名義か（「カ)」「(カ」など）を見る。
- 確信が持てなければ same_person は false、confidence は低くする。推測で埋めない。"""


class Extractor(Protocol):
    def parse(self, text: str, sent_at: datetime) -> Parsed | None: ...
    def judge_payer(self, bank_text: str, customer_name: str) -> tuple[bool, float]: ...


class ClaudeExtractor:
    def __init__(self, api_key: str, model_primary: str, model_fallback: str, effort: str = "low"):
        import anthropic

        self.client = anthropic.Anthropic(api_key=api_key, max_retries=3, timeout=120.0)
        self.models = (model_primary, model_fallback)
        self.effort = effort

    @staticmethod
    def _create(fn, **kwargs):
        """Claude API を呼ぶ（SDK が3回までやり直す）。それでも失敗したら ExternalAPIError（障害の通知で連携先が分かるように）。"""
        import anthropic

        try:
            return fn(**kwargs)
        except anthropic.APIStatusError as e:
            raise ExternalAPIError("Claude", "messages.create", e.status_code, str(e)[:300]) from e
        except anthropic.APIError as e:
            raise ExternalAPIError("Claude", "messages.create", None, str(e)[:300]) from e

    def _call(self, model: str, text: str, sent_at: datetime) -> Parsed | None:
        kwargs = dict(model=model, max_tokens=4000, system=SYSTEM,
                      messages=[{"role": "user", "content": f"送信日時: {sent_at.isoformat()}\nメッセージ:\n{text}"}],
                      output_config={"effort": self.effort, "format": {"type": "json_schema", "schema": SCHEMA}})
        if model.startswith("claude-sonnet"):
            resp = self._create(self.client.beta.messages.create, betas=[FALLBACK_BETA], fallbacks="default", **kwargs)
        else:
            resp = self._create(self.client.messages.create, **kwargs)
        usage = {"model": model, "in": resp.usage.input_tokens, "out": resp.usage.output_tokens}
        if resp.stop_reason == "refusal":
            log.warning("Claude が応答を拒否 model=%s", model)
            return None
        raw = next((b.text for b in resp.content if b.type == "text"), "")
        try:
            return Parsed.from_json(json.loads(raw), model=model, usage=usage)
        except (json.JSONDecodeError, TypeError, ValueError) as e:
            log.warning("解析結果が不正 model=%s err=%r", model, e)
            return None

    def parse(self, text: str, sent_at: datetime) -> Parsed | None:
        primary, fallback = self.models
        p = self._call(primary, text, sent_at) or self._call(primary, text, sent_at)
        if p and p.confidence >= MIN_CONFIDENCE:
            return p
        q = self._call(fallback, text, sent_at)
        return q or p

    def judge_payer(self, bank_text: str, customer_name: str) -> tuple[bool, float]:
        """振込名義と顧客名が同じ人か（Haiku）。判定できなければ (False, 0.0)。"""
        resp = self._create(
            self.client.messages.create, model=self.models[0], max_tokens=1000, system=PAYER_SYSTEM,
            messages=[{"role": "user", "content": f"振込明細の摘要: {bank_text}\n顧客名: {customer_name}"}],
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": PAYER_SCHEMA}})
        raw = next((b.text for b in resp.content if b.type == "text"), "{}")
        try:
            data = json.loads(raw)
            return bool(data.get("same_person")), float(data.get("confidence", 0))
        except (json.JSONDecodeError, TypeError, ValueError):
            return False, 0.0
