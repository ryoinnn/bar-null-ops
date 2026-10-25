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


def receipt_matches(receipts: list[dict], payments: list[Payment], claimed: set[str], *, before_min: float = 10,
                    after_min: float = 60) -> tuple[dict[str, Payment], dict[str, list[Payment]]]:
    """POS の受け取り（id, method, amount, at）の1件ずつに、Square の決済を割り当てる。

    現金の受け取りは Square の現金の決済、Square の受け取りは現金以外の決済だけを見る。金額が同じで、
    受け取りの時刻の before_min 分前〜after_min 分後にあるもの。
    同じ方法・同じ金額の受け取りが複数あるときは、候補の数が受け取りの数以下なら時刻の順に組にする。
    候補が多すぎるときは、時刻がはっきり近い1件（3分以内で、次の候補より5分以上近い）だけを割り当てる。
    戻り値: (受け取り ID → 決済, 決められなかった受け取り ID → 候補)
    """
    matched: dict[str, Payment] = {}
    unclear: dict[str, list[Payment]] = {}
    used = set(claimed)
    groups: dict[tuple[bool, int], list[dict]] = {}
    for r in receipts:
        groups.setdefault((r["method"] == "cash", r["amount"]), []).append(r)
    for (cash, amount), rs in groups.items():
        rs = sorted(rs, key=lambda r: r["at"])
        times = [datetime.fromisoformat(r["at"]) for r in rs]
        lo, hi = min(times) - timedelta(minutes=before_min), max(times) + timedelta(minutes=after_min)
        pool = sorted([p for p in payments if usable(p, used) and (p.source_type == "CASH") == cash
                       and p.amount == amount and lo <= p.created_at <= hi], key=lambda p: p.created_at)
        if pool and len(pool) <= len(rs):
            for r, p in zip(rs, pool, strict=False):   # 候補が受け取りより少なければ、残りは後の回で
                matched[r["id"]] = p
                used.add(p.id)
            continue
        for r, t in zip(rs, times, strict=True):
            near = sorted([p for p in pool if p.id not in used
                           and t - timedelta(minutes=before_min) <= p.created_at <= t + timedelta(minutes=after_min)],
                          key=lambda p: abs((p.created_at - t).total_seconds()))
            gaps = [abs((p.created_at - t).total_seconds()) / 60 for p in near]
            if len(near) == 1 or (len(near) > 1 and gaps[0] <= 3 and gaps[1] - gaps[0] >= 5):
                matched[r["id"]] = near[0]
                used.add(near[0].id)
            elif near:
                unclear[r["id"]] = near[:5]
    return matched, unclear


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
