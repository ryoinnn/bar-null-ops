"""テストの共通部品。顧客名・名義はすべて架空。"""

from __future__ import annotations

import os
from dataclasses import replace
from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

from barnull_sales.config import load_settings
from barnull_sales.deps import Deps
from barnull_sales.extract import Entry, Parsed
from barnull_sales.square import Payment
from barnull_sales.store import MemoryStore
from barnull_sales.testing import FakeBank, FakeNotifier, FakePayments, FakePayouts, FakeTracker

JST = ZoneInfo("Asia/Tokyo")
SIX = time(6, 0)
os.environ.pop("DRY_RUN", None)      # 手元の環境変数に左右されないように
SETTINGS = load_settings(Path(__file__).parent / "sales.test.toml")
SQUARE_TXN = "振込 スクエア(カ フリコミ"


def settings(**kw):
    return replace(SETTINGS, **kw)


def pay(pid, local, amount, source="CARD", fee=None, status="COMPLETED", refunded=0, brand="VISA") -> Payment:
    dt = datetime.strptime(local, "%Y-%m-%d %H:%M").replace(tzinfo=JST)
    return Payment(id=pid, created_at=dt, amount=amount, status=status, source_type=source,
                   fee=fee if fee is not None else (None if source == "CASH" else amount * 25 // 1000),
                   refunded=refunded, brand=None if source == "CASH" else brand)


def payout(pid, amount, arrival, payments) -> dict:
    return {"id": pid, "amount_money": {"amount": amount}, "arrival_date": arrival, "status": "PAID",
            "_payments": list(payments)}


class SeqExtractor:
    """呼ばれるたびに順に解析結果を返す（最後のものは繰り返す）。振込名義の判定は辞書で行う。"""

    def __init__(self, results=(), payers=None):
        self.results = list(results)
        self.payers = payers or {}
        self.calls = 0
        self.payer_calls = 0

    def parse(self, text, sent_at):
        self.calls += 1
        return self.results.pop(0) if len(self.results) > 1 else (self.results[0] if self.results else None)

    def judge_payer(self, bank_text, customer_name):
        self.payer_calls += 1
        return self.payers.get((bank_text, customer_name), (False, 0.1))


def slip_msg(*entries: Entry) -> Parsed:
    return Parsed(kind="slip_content", confidence=0.95, entries=list(entries))


def make_deps(results=(), payments=(), payouts=(), txns=(), payers=None, **settings_kw) -> Deps:
    return Deps(settings=settings(**settings_kw), store=MemoryStore(), notifier=FakeNotifier(), tracker=FakeTracker(),
                extractor=SeqExtractor(results, payers), payments=FakePayments(payments),
                payouts=FakePayouts(payouts), bank=FakeBank(txns))


def event(local, event_id="E1", user="U1", group="Cgroup", text="@null 伝票") -> dict:
    ts = datetime.strptime(local, "%Y-%m-%d %H:%M").replace(tzinfo=JST)
    return {"type": "message", "webhookEventId": event_id, "timestamp": int(ts.timestamp() * 1000),
            "source": {"type": "group", "groupId": group, "userId": user},
            "message": {"type": "text", "id": "m" + event_id, "text": text,
                        "mention": {"mentionees": [{"isSelf": True, "index": 0, "length": 5}]}}}


def threads(deps, slip) -> list[str]:
    return [p["text"] for p in deps.notifier.posts if p["thread_ts"] and p["thread_ts"] == slip.slack.get("thread_ts")]


def parents(deps) -> list[dict]:
    return [p for p in deps.notifier.posts if not p["thread_ts"]]


def with_staff(deps, *names: str) -> None:
    """現金を振り込む担当者の名義を設定する。"""
    deps.settings = replace(deps.settings, mf=replace(deps.settings.mf, staff_bank_names=tuple(names)))
