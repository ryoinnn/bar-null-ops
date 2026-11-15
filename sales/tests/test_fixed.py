"""固定費（経営ダッシュボードの写しと、月・日の計算）。金額・項目の値はすべて架空。"""

from datetime import date

from barnull_sales import fixed


class FakeFixed:
    def __init__(self) -> None:
        self.tasks: dict[str, dict] = {}

    def list_tasks(self):
        return [dict(t) for t in self.tasks.values()]

    def upsert(self, gid, name, values, notes, section):
        gid = gid or f"g{len(self.tasks) + 1}"
        t = self.tasks.setdefault(gid, {"gid": gid})
        t.update(values, name=name, section=section)
        if notes is not None:
            t["notes"] = notes
        return gid


def row(name, ym, amount, gid="x"):
    return {"gid": gid, "name": name, "ym": ym, "amount": amount}


def test_parse_ym_and_items():
    assert fixed.parse_ym("26年9月") == "2026-09"
    assert fixed.parse_ym("2025年11月") == "2025-11"
    assert fixed.parse_ym("9月") is None
    assert fixed.normalize_item("銀行借り入れの返済")[0] == "loan"
    assert fixed.normalize_item("銀行借入の返済")[0] == "loan"
    assert fixed.normalize_item("設備利用費(セコム、水道、電気)")[2] == "光熱・設備"
    assert fixed.normalize_item("謎の費用") is None


def test_plan_collapses_runs_and_skips_blank_months():
    rows = [row("家賃", "2026-04", 1000), row("家賃", "2026-05", 1000), row("家賃", "2026-06", 1200),
            row("家賃", "2026-07", 1200), row("家賃", "2026-09", None), row("謎の費用", "2026-04", 5)]
    plan = fixed.plan_from_dashboard(rows)
    assert [(r["record_id"], r["amount"], r["start"], r["end"]) for r in plan.records] == [
        ("F-rent-202604", 1000, "2026-04", "2026-05"), ("F-rent-202606", 1200, "2026-06", None)]
    assert plan.unknown == ["謎の費用"]


def test_sync_is_idempotent_and_excludes_vanished():
    t = FakeFixed()
    rows = [row("家賃", "2026-04", 1000), row("通信費", "2026-04", 50)]
    st = fixed.sync(t, fixed.plan_from_dashboard(rows).records, log_fn=lambda *_: None)
    assert (st.created, st.updated) == (2, 0)
    st = fixed.sync(t, fixed.plan_from_dashboard(rows).records, log_fn=lambda *_: None)
    assert (st.created, st.updated, st.unchanged) == (0, 0, 2)
    # 手入力のタスク（写し元の印なし）には触らない
    t.upsert(None, "保険", {"record_id": "F-ins-202604", "amount": 30, "start": "2026-04", "status": "有効"}, "", "固定費")
    st = fixed.sync(t, fixed.plan_from_dashboard(rows[:1]).records, log_fn=lambda *_: None)
    assert st.excluded == 1
    by_name = {x["name"]: x for x in t.tasks.values()}
    assert by_name["通信費"]["status"] == "対象外" and by_name["保険"]["status"] == "有効"


def task(name, amount, start, end=None, months=1, kind="通常", status="有効", store=""):
    return {"name": name, "amount": amount, "start": start, "end": end, "months": months, "kind": kind,
            "status": status, "store": store, "category": "その他"}


def test_month_rules():
    ts = [task("家賃", 1000, "2026-01"), task("家賃", 1200, "2026-06"),      # 値上げは新しい開始月を足すだけ
          task("ガス代", 100, "2026-01"), task("ガス代", 130, "2026-07", "2026-07", kind="実績"),
          task("保険", 1200, "2026-04", months=12),                           # 年払いの按分
          task("古い", 999, "2026-01", "2026-03"), task("取消", 5, "2026-01", status="対象外"),
          task("福岡", 7, "2026-01", store="node")]
    assert fixed.month_total(ts, "2025-12") is None
    a = fixed.month_amounts(ts, "2026-07")
    assert a["家賃"]["amount"] == 1200 and a["ガス代"]["amount"] == 130 and a["保険"]["amount"] == 100
    assert "古い" not in a and "取消" not in a and "福岡" not in a
    assert fixed.month_total(ts, "2026-05") == 1000 + 100 + 100
    assert fixed.month_total(ts, "2026-08") == 1200 + 100 + 100


def test_rounding_remainder_goes_to_last_month_of_cycle():
    t = task("ソフト", 1000, "2026-01", months=3)
    got = [fixed.item_month_amount(t, m) for m in ("2026-01", "2026-02", "2026-03", "2026-04")]
    assert got == [333, 333, 334, 333]


def test_period_total_prorates_by_days():
    ts = [task("家賃", 3000, "2026-01"), task("家賃", 3100, "2026-03")]
    assert fixed.period_total(ts, date(2026, 1, 1), date(2026, 1, 31)) == 3000
    # 2月の最後の2日（28日の月）＋3月の最初の2日（31日の月）
    v = fixed.period_total(ts, date(2026, 2, 27), date(2026, 3, 2))
    assert abs(v - (3000 * 2 / 28 + 3100 * 2 / 31)) < 1e-9
    assert fixed.period_total(ts, date(2025, 12, 1), date(2025, 12, 31)) is None


def test_month_rules_ignore_spaces_and_bad_month_format():
    """ポータル（common.js の BN.fixed）と同じ扱い: 前後の空白は無視、適用開始月が YYYY-MM でないものは数えない。"""
    base = {"category": "その他", "months": 1, "end": "", "kind": "通常", "status": "有効", "store": ""}
    ts = [
        {**base, "name": "家賃", "amount": 300000, "start": "2026-01"},
        {**base, "name": "家賃 ", "amount": 330000, "start": " 2026-10 "},     # 空白つき: 同じ項目の値上げ
        {**base, "name": "形式違い", "amount": 7777, "start": "2026/01"},        # 例外にせず、数えない
        {**base, "name": "店舗の空白", "amount": 500, "start": "2026-01", "store": " "},
    ]
    assert {n: v["amount"] for n, v in fixed.month_amounts(ts, "2026-09").items()} == {"家賃": 300000, "店舗の空白": 500}
    assert {n: v["amount"] for n, v in fixed.month_amounts(ts, "2026-11").items()} == {"家賃": 330000, "店舗の空白": 500}
