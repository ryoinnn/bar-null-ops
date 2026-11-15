"""固定費: 経営ダッシュボードからの写しと、月・日の固定費の計算。契約は docs/profit/contract.md §3c。

タスクは dict（キー: name, record_id, category, amount, months, start, end, kind, status, store, notes, gid）。
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass, field
from datetime import date, timedelta

CATEGORIES = ("家賃・共益費", "光熱・設備", "契約・サービス", "返済・配当", "その他")
KINDS = ("通常", "実績")
STATUSES = ("有効", "対象外")
SOURCE_MARK = "写し元: 経営ダッシュボード"

# 経営ダッシュボードの項目名（表記ゆれを含む）→ (項目コード, 表示する項目名, 区分)
_ITEMS = [
    (("家賃",), "rent", "家賃", "家賃・共益費"),
    (("共益費",), "common", "共益費", "家賃・共益費"),
    (("ガス代",), "gas", "ガス代", "光熱・設備"),
    (("設備利用費",), "facility", "設備利用費(セコム、水道、電気)", "光熱・設備"),
    (("通信費",), "telecom", "通信費", "契約・サービス"),
    (("カラオケ費",), "karaoke", "カラオケ費", "契約・サービス"),
    (("リース費",), "lease", "リース費", "契約・サービス"),
    (("ゴミ回収費",), "garbage", "ゴミ回収費", "契約・サービス"),
    (("出資者配当金",), "dividend", "出資者配当金", "返済・配当"),
    (("銀行借入の返済", "銀行借り入れの返済"), "loan", "銀行借入の返済", "返済・配当"),
    (("その他",), "other", "その他", "その他"),
]


def normalize_item(name: str) -> tuple[str, str, str] | None:
    """項目名 → (コード, 表示名, 区分)。知らない項目は None（写さずに報告する）。"""
    n = re.sub(r"\s+", "", name or "")
    for keys, code, disp, cat in _ITEMS:
        if any(n.startswith(k) for k in keys):
            return code, disp, cat
    return None


def parse_ym(text: str | None) -> str | None:
    """「26年9月」「2025年11月」→ "2026-09"。"""
    m = re.fullmatch(r"\s*(\d{2}|\d{4})年(\d{1,2})月\s*", text or "")
    if not m:
        return None
    y = int(m.group(1))
    return f"{y + 2000 if y < 100 else y:04d}-{int(m.group(2)):02d}"


def add_months(ym: str, n: int) -> str:
    y, m = map(int, ym.split("-"))
    k = y * 12 + (m - 1) + n
    return f"{k // 12:04d}-{k % 12 + 1:02d}"


def months_between(a: str, b: str) -> int:
    """a から b までの月数（同じ月なら 0）。"""
    ya, ma = map(int, a.split("-"))
    yb, mb = map(int, b.split("-"))
    return (yb - ya) * 12 + (mb - ma)


# ---------- 経営ダッシュボード → 固定費のタスク ----------

@dataclass
class ImportPlan:
    records: list[dict] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)       # 知らない項目名（写さない）
    conflicts: list[str] = field(default_factory=list)     # 同じ項目・同じ月に違う金額（あとの値を使う）


def plan_from_dashboard(rows: list[dict]) -> ImportPlan:
    """rows: {gid, name, ym ("YYYY-MM"), amount (int|None)}。金額が空の月は飛ばす（前の金額が続く）。

    項目ごとに、金額が変わった月で区切って「通常」のタスクにまとめる。最後の区切りは適用終了月を空にする。
    """
    plan = ImportPlan()
    by_item: dict[str, dict] = {}
    for r in rows:
        if not r.get("ym") or r.get("amount") is None:
            continue
        it = normalize_item(r["name"])
        if not it:
            if r["name"] not in plan.unknown:
                plan.unknown.append(r["name"])
            continue
        code, disp, cat = it
        d = by_item.setdefault(code, {"name": disp, "category": cat, "months": {}})
        prev = d["months"].get(r["ym"])
        if prev and prev[0] != int(r["amount"]):
            plan.conflicts.append(f"{disp} {r['ym']}: {prev[0]:,} と {int(r['amount']):,}")
        d["months"][r["ym"]] = (int(r["amount"]), r["gid"])
    for code, d in by_item.items():
        runs: list[dict] = []
        for ym in sorted(d["months"]):
            amount, gid = d["months"][ym]
            if runs and runs[-1]["amount"] == amount:
                runs[-1]["last"] = ym
                runs[-1]["src"].append(f"{ym}:{gid}")
                continue
            runs.append({"start": ym, "last": ym, "amount": amount, "src": [f"{ym}:{gid}"]})
        for i, run in enumerate(runs):
            end = add_months(runs[i + 1]["start"], -1) if i + 1 < len(runs) else None
            plan.records.append({
                "record_id": f"F-{code}-{run['start'].replace('-', '')}", "name": d["name"], "category": d["category"],
                "amount": run["amount"], "months": 1, "start": run["start"], "end": end, "kind": "通常",
                "status": "有効", "store": "", "notes": f"{SOURCE_MARK}（年月:タスクID）{' '.join(run['src'])}"})
    plan.records.sort(key=lambda x: (x["name"], x["start"]))
    return plan


@dataclass
class SyncStats:
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    excluded: int = 0


_COMPARE = ("name", "category", "amount", "months", "start", "end", "kind", "status", "store")


def _norm(v):
    return "" if v is None else v


def sync(tracker, records: list[dict], *, dry_run: bool = False, log_fn=print) -> SyncStats:
    """固定費ID で突き合わせて作る・直す（冪等）。写し元が経営ダッシュボードで、今回の写しに無いタスクは「対象外」にする。
    手入力のタスク（本文に写し元の印が無いもの）には触らない。"""
    st = SyncStats()
    have = {t.get("record_id"): t for t in tracker.list_tasks() if t.get("record_id")}
    want = {r["record_id"] for r in records}
    for r in records:
        cur = have.get(r["record_id"])
        section = "固定費"
        if cur and all(_norm(cur.get(k)) == _norm(r.get(k)) for k in _COMPARE) and cur.get("section") == section:
            st.unchanged += 1
            continue
        verb = "更新" if cur else "作成"
        log_fn(f"{'[dry-run] ' if dry_run else ''}{verb}: {r['record_id']} {r['name']} {r['start']}〜{r['end'] or ''}")
        if not dry_run:
            tracker.upsert(cur["gid"] if cur else None, r["name"], _values(r), r["notes"], section)
        if cur:
            st.updated += 1
        else:
            st.created += 1
    for rid, cur in have.items():
        if rid in want or SOURCE_MARK not in (cur.get("notes") or "") or cur.get("status") == "対象外":
            continue
        log_fn(f"{'[dry-run] ' if dry_run else ''}対象外: {rid} {cur.get('name')}（経営ダッシュボードの写しから消えた）")
        if not dry_run:
            tracker.upsert(cur["gid"], cur.get("name", ""), {"status": "対象外"}, None, "対象外")
        st.excluded += 1
    return st


def _values(r: dict) -> dict:
    return {k: r.get(k) for k in ("record_id", "category", "amount", "months", "start", "end", "kind", "status", "store")}


# ---------- 月・日の固定費 ----------

_YM = re.compile(r"\d{4}-\d{2}")


def _active(t: dict, ym: str, store: str) -> bool:
    """その月に効くタスクか。前後の空白は無視し、適用開始月が YYYY-MM でないものは数えない（ポータルの common.js と同じ）。"""
    if t.get("status", "有効") == "対象外" or (t.get("store") or "").strip() != store:
        return False
    s, e = (t.get("start") or "").strip(), (t.get("end") or "").strip()
    return bool(_YM.fullmatch(s)) and s <= ym and (not e or ym <= e) and t.get("amount") is not None


def item_month_amount(t: dict, ym: str) -> int:
    """タスク1つの、その月の額（按分月数で割り、区切りの最後の月に端数を足す）。"""
    n = int(t.get("months") or 1)
    n = max(n, 1)
    total = int(t["amount"])
    base = total // n
    k = months_between(t["start"].strip(), ym) % n
    return base + (total - base * n if k == n - 1 else 0)


def month_amounts(tasks: list[dict], ym: str, store: str = "") -> dict[str, dict]:
    """その月の項目ごとの額 {項目名: {amount, category, kind}}。項目が無ければ {}（未設定）。"""
    picked: dict[str, dict] = {}
    for t in tasks:
        if not _active(t, ym, store):
            continue
        name = (t.get("name") or "").strip()      # 「家賃 」と「家賃」は同じ項目
        cur = picked.get(name)
        kind = t.get("kind") or "通常"
        if cur is None:
            picked[name] = t
        elif (cur.get("kind") or "通常") != "実績" and kind == "実績":
            picked[name] = t
        elif (cur.get("kind") or "通常") == kind and t["start"].strip() > cur["start"].strip():
            picked[name] = t
    return {n: {"amount": item_month_amount(t, ym), "category": t.get("category") or "その他",
                "kind": t.get("kind") or "通常"} for n, t in picked.items()}


def month_total(tasks: list[dict], ym: str, store: str = "") -> int | None:
    a = month_amounts(tasks, ym, store)
    return sum(v["amount"] for v in a.values()) if a else None


def period_total(tasks: list[dict], start: date, end: date, store: str = "") -> float | None:
    """start〜end（両端を含む）の固定費。日 ＝ 月の固定費 ÷ その月の日数。どの月も未設定なら None。"""
    total, any_set, d = 0.0, False, start
    while d <= end:
        ym = f"{d.year:04d}-{d.month:02d}"
        days_in = calendar.monthrange(d.year, d.month)[1]
        last = min(end, date(d.year, d.month, days_in))
        m = month_total(tasks, ym, store)
        if m is not None:
            any_set = True
            total += m * ((last - d).days + 1) / days_in
        d = last + timedelta(days=1)
    return total if any_set else None
