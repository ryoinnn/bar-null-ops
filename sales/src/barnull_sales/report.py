"""売上週報（毎週月曜）: Asana のタスクから集計し、Slack に短い通知を送る。詳しいレポートは Artifact（report/weekly_report.html）。

定義（2026-10-17）
- 売上: 伝票金額の合計（未収分を含む。対象外（取消）の伝票は除く）
- 予約数: 伝票の件数（1伝票＝1組）
- 客単価: 売上 ÷ 予約数（1組あたり。人数は記録していないため）
- 期間はすべて営業日（6:00〜翌6:00）で数える。週は月曜〜日曜
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from .asana import FIELDS, AsanaAPI
from .slips import Status

EXCLUDED = (Status.CANCELLED,)
RENTAL_PREFIX = "RS-"   # レンタルスペースの月次記録（予約対応システムが起票。通常営業の売上には数えない）


def is_rental(slip_id: str | None) -> bool:
    return bool(slip_id) and slip_id.startswith(RENTAL_PREFIX)


@dataclass
class Row:
    business_date: date
    amount: int
    unpaid_remaining: int
    status: str


def fetch_rows(api: AsanaAPI, project_gid: str) -> list[Row]:
    tasks = api.list("/tasks", params={
        "project": project_gid,
        "opt_fields": "custom_fields.name,custom_fields.number_value,custom_fields.date_value,custom_fields.enum_value.name,"
                      "custom_fields.text_value"})
    name = {key: FIELDS[key][0] for key in ("business_date", "slip_amount", "status", "unpaid_remaining", "slip_id")}
    rows = []
    for t in tasks:
        f = {c["name"]: c for c in t.get("custom_fields", [])}
        d = ((f.get(name["business_date"]) or {}).get("date_value") or {}).get("date")
        amount = (f.get(name["slip_amount"]) or {}).get("number_value")
        status = ((f.get(name["status"]) or {}).get("enum_value") or {}).get("name") or ""
        if not d or amount is None or status in EXCLUDED or is_rental((f.get(name["slip_id"]) or {}).get("text_value")):
            continue
        rows.append(Row(date.fromisoformat(d[:10]), int(amount),
                        int((f.get(name["unpaid_remaining"]) or {}).get("number_value") or 0), status))
    return rows


def _sum(rows: list[Row], start: date, end: date) -> tuple[int, int]:
    """[start, end] の売上と件数。"""
    sel = [r for r in rows if start <= r.business_date <= end]
    return sum(r.amount for r in sel), len(sel)


def _unit(sales: int, count: int) -> int | None:
    return round(sales / count) if count else None


def _yen(v: int | None) -> str:
    return "—" if v is None else f"¥{v:,}"


def build(rows: list[Row], today: date, weeks: int = 8) -> dict:
    """today（週報を出す日。通常は月曜）の前の週までを集計する。"""
    week_end = today - timedelta(days=today.weekday() + 1)        # 直前の日曜
    week_start = week_end - timedelta(days=6)
    prev_week = (week_start - timedelta(days=7), week_start - timedelta(days=1))
    month_start = week_end.replace(day=1)
    pm_end_full = month_start - timedelta(days=1)
    pm_start = pm_end_full.replace(day=1)
    pm_same_end = min(pm_start.replace(day=1) + timedelta(days=week_end.day - 1), pm_end_full)

    w_sales, w_count = _sum(rows, week_start, week_end)
    pw_sales, pw_count = _sum(rows, *prev_week)
    m_sales, m_count = _sum(rows, month_start, week_end)
    pm_sales, pm_count = _sum(rows, pm_start, pm_same_end)
    pmf_sales, pmf_count = _sum(rows, pm_start, pm_end_full)
    trend = []
    for i in range(weeks - 1, -1, -1):
        s = week_start - timedelta(days=7 * i)
        sales, count = _sum(rows, s, s + timedelta(days=6))
        trend.append({"start": s, "sales": sales, "count": count, "unit": _unit(sales, count)})
    return {
        "week": (week_start, week_end), "week_sales": w_sales, "week_count": w_count, "week_unit": _unit(w_sales, w_count),
        "prev_week_sales": pw_sales, "prev_week_count": pw_count, "prev_week_unit": _unit(pw_sales, pw_count),
        "month": (month_start, week_end), "month_sales": m_sales, "month_count": m_count,
        "month_unit": _unit(m_sales, m_count),
        "prev_month_same": (pm_start, pm_same_end), "prev_month_same_sales": pm_sales, "prev_month_same_count": pm_count,
        "prev_month_same_unit": _unit(pm_sales, pm_count),
        "prev_month_full_sales": pmf_sales, "prev_month_full_count": pmf_count,
        "trend": trend,
        "unpaid_total": sum(r.unpaid_remaining for r in rows),
        "review_count": sum(1 for r in rows if r.status == Status.REVIEW),
    }


def _md(d: date) -> str:
    return f"{d.month}/{d.day}"


def title(r: dict) -> str:
    s, e = r["week"]
    return f"売上週報 {_md(s)}〜{_md(e)}"


def slack_text(r: dict, link: str, test: bool = False) -> str:
    """Slack の通知は短く: 週の売上と今月の売上（現時点）だけ。詳しくはレポート（Artifact）で見る。"""
    s, e = r["week"]
    ms, me = r["month"]
    lines = [
        f"*{title(r)}*" + ("（テスト）" if test else ""),
        f"先週（{_md(s)}〜{_md(e)}）の売上：{_yen(r['week_sales'])}",
        f"今月（{_md(ms)}〜{_md(me)}）の売上：{_yen(r['month_sales'])}",
    ]
    if link:
        lines.append(f"詳細レポート：{link}")
    return "\n".join(lines)


def publish(deps, api: AsanaAPI, today: date | None = None) -> dict:
    """集計して Slack に短い週報を送る。Asana は伝票データの蓄積場所なので、Asana には書かない（2026-10-17 本人）。"""
    s = deps.settings
    today = today or datetime.now(UTC).astimezone(s.tz).date()
    rows = fetch_rows(api, s.asana_ids[s.asana_env]["project"])
    r = build(rows, today)
    deps.notifier.post(s.slack_report_channel, slack_text(r, s.report_url, s.dry_run))
    return {"rows": len(rows), "week_sales": r["week_sales"], "month_sales": r["month_sales"]}
