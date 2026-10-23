"""伝票と Square の決済の突合（外部に依存しない純粋な関数）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from itertools import combinations
from zoneinfo import ZoneInfo

from . import bizdate
from .bizdate import business_window
from .square import Payment


@dataclass
class MatchResult:
    kind: str                                # matched / ambiguous / date_shift / not_found
    payments: list[Payment] = field(default_factory=list)
    method: str = ""                         # single / split
    candidates: list[list[Payment]] = field(default_factory=list)


def search_window(business_date: str | None, sent_at: datetime, tz: ZoneInfo, day_start,
                  fallback_days: int = 2) -> tuple[datetime, datetime]:
    """営業日が分かれば、その営業日。分からなければ送信日時の営業日の2営業日前の 6:00 〜 送信の3時間後。"""
    if business_date:
        return business_window(date.fromisoformat(business_date), tz, day_start)
    ds = bizdate.business_date(sent_at, tz, day_start)
    start, _ = business_window(ds - timedelta(days=fallback_days), tz, day_start)
    return start, sent_at + timedelta(hours=3)


def usable(p: Payment, claimed: set[str]) -> bool:
    return p.usable and p.id not in claimed


def find_match(expected: int, payments: list[Payment], claimed: set[str], window: tuple[datetime, datetime],
               *, split_max: int = 5, split_span_min: float = 30, neighbor_payments: list[Payment] | None = None,
               same_amount_open_slips: int = 1) -> MatchResult:
    """expected 円に一致する決済を探す。

    same_amount_open_slips: 同じ期待額でまだ決済が見つかっていない伝票の数（自分を含む）。
      同額の候補がその数以下なら、時刻の早い順に割り当ててよい（同額の伝票が複数ある日のため）。
    """
    start, end = window
    pool = [p for p in payments if usable(p, claimed) and start <= p.created_at < end]
    singles = sorted([p for p in pool if p.amount == expected], key=lambda p: p.created_at)
    if len(singles) == 1 or (singles and len(singles) <= same_amount_open_slips):
        return MatchResult("matched", [singles[0]], "single")
    if len(singles) > 1:
        return MatchResult("ambiguous", candidates=[[p] for p in singles])

    combos = []
    small = sorted([p for p in pool if p.amount < expected], key=lambda p: p.created_at)
    for n in range(2, min(split_max, len(small)) + 1):
        for combo in combinations(small, n):
            span = (combo[-1].created_at - combo[0].created_at).total_seconds() / 60
            if span <= split_span_min and sum(p.amount for p in combo) == expected:
                combos.append(list(combo))
        if len(combos) > 3:
            break
    if len(combos) == 1:
        return MatchResult("matched", combos[0], "split")
    if combos:
        return MatchResult("ambiguous", candidates=combos[:5])

    if neighbor_payments:
        near = [p for p in neighbor_payments if usable(p, claimed) and p.amount == expected
                and not (start <= p.created_at < end)]
        if near:
            return MatchResult("date_shift", candidates=[[p] for p in near[:5]])
    return MatchResult("not_found")


def describe(payments: list[Payment], tz: ZoneInfo) -> str:
    """「10/11 02:14 カード」「10/02 03:51 交通系IC ほか1件」"""
    first = payments[0]
    t = bizdate.hm(first.created_at, tz)
    more = f" ほか{len(payments) - 1}件" if len(payments) > 1 else ""
    return f"{t} {first.method_label}{more}"


def orphan_due(p: Payment, now: datetime, tz: ZoneInfo, day_start, grace_hours: float) -> bool:
    """決済の営業日が終わってから grace_hours 過ぎたか。"""
    d = bizdate.business_date(p.created_at, tz, day_start)
    _, end = business_window(d, tz, day_start)
    return now >= end + timedelta(hours=grace_hours)
