from __future__ import annotations

from datetime import date

from barnull_sales.report import Row, build, slack_text


def rows():
    return [
        Row(date(2026, 9, 3), 50000, 0, "入金確認完了"),
        Row(date(2026, 9, 10), 100000, 0, "入金確認完了"),
        Row(date(2026, 9, 25), 30000, 0, "入金確認完了"),
        Row(date(2026, 10, 2), 80000, 0, "入金確認完了"),
        Row(date(2026, 10, 6), 60000, 0, "決済確認済み"),      # 前週（10/5〜10/11）
        Row(date(2026, 10, 13), 100000, 40000, "決済確認済み"),  # 先週（10/12〜10/18）
        Row(date(2026, 10, 18), 20000, 0, "要確認"),
        Row(date(2026, 10, 19), 99999, 0, "起票済み"),          # 週報の日（含めない）
    ]


def test_weekly_numbers():
    r = build(rows(), date(2026, 10, 19))                      # 月曜
    assert r["week"] == (date(2026, 10, 12), date(2026, 10, 18))
    assert (r["week_sales"], r["week_count"], r["week_unit"]) == (120000, 2, 60000)
    assert (r["prev_week_sales"], r["prev_week_count"]) == (60000, 1)
    assert r["month"] == (date(2026, 10, 1), date(2026, 10, 18))
    assert (r["month_sales"], r["month_count"]) == (260000, 4)
    assert r["prev_month_same"] == (date(2026, 9, 1), date(2026, 9, 18))
    assert (r["prev_month_same_sales"], r["prev_month_same_count"]) == (150000, 2)
    assert r["prev_month_full_sales"] == 180000
    assert r["unpaid_total"] == 40000 and r["review_count"] == 1
    assert len(r["trend"]) == 8 and r["trend"][-1]["sales"] == 120000


def test_prev_month_same_period_is_capped_at_month_end():
    r = build([Row(date(2026, 2, 28), 1000, 0, "x")], date(2026, 4, 6))   # 先週は 3/30〜4/5 → 今月は 4/1〜4/5
    assert r["prev_month_same"] == (date(2026, 3, 1), date(2026, 3, 5))
    r = build([], date(2026, 4, 1))                                       # 水曜でも直前の日曜までを集計
    assert r["week"] == (date(2026, 3, 23), date(2026, 3, 29))
    assert r["prev_month_same"] == (date(2026, 2, 1), date(2026, 2, 28))


def test_slack_text_is_short():
    text = slack_text(build(rows(), date(2026, 10, 19)), "https://claude.ai/artifact/x")
    assert text.splitlines() == ["*売上週報 10/12〜10/18*", "先週（10/12〜10/18）の売上：¥120,000",
                                 "今月（10/1〜10/18）の売上：¥260,000", "詳細レポート：https://claude.ai/artifact/x"]


def test_rental_monthly_records_are_not_counted_as_bar_sales():
    from barnull_sales.report import fetch_rows

    def task(slip_id, amount, d):
        return {"custom_fields": [{"name": "営業日", "date_value": {"date": d}}, {"name": "伝票金額", "number_value": amount},
                                  {"name": "伝票ステータス", "enum_value": {"name": "起票済み"}},
                                  {"name": "伝票ID", "text_value": slip_id}]}

    class Api:
        def list(self, path, params):
            return [task("S-20261031-001", 50000, "2026-10-31"), task("RS-IB-202610", 120000, "2026-10-31")]

    rows = fetch_rows(Api(), "p")
    assert [r.amount for r in rows] == [50000]
