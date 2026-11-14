"""備品・消耗品（マネフォのカード → Asana）の取り込みと冪等性。店名・金額はすべて架空。"""

from dataclasses import replace
from datetime import UTC, datetime

from helpers import make_deps

from barnull_sales import jobs, supplies
from barnull_sales.config import SuppliesConfig
from barnull_sales.mf import BankTxn

CFG = SuppliesConfig(enabled=True, card_subaccount="CARD", tool_patterns=("ＡＩサービス",), high_value=100000)


class FakeSupplies:
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


def tx(i, d, amount, content="テスト商店"):
    return BankTxn(id=i, date=d, amount=amount, content=content)


def run(tracker, txns, start="2026-11-01", end="2026-11-30", **kw):
    return supplies.sync(tracker, txns, start, end, CFG, **kw)


def test_creates_once_and_numbers_per_day():
    t = FakeSupplies()
    txns = [tx("a", "2026-11-05", 1000), tx("b", "2026-11-05", 2000), tx("c", "2026-11-06", 500)]
    st = run(t, txns)
    assert (st.created, st.new_amount) == (3, 3500)
    ids = sorted(x["record_id"] for x in t.tasks.values())
    assert ids == ["E-20261105-001", "E-20261105-002", "E-20261106-001"]
    st = run(t, txns)                                     # 同じ入力は何度でも同じ（二重に作らない）
    assert (st.created, st.updated, st.unchanged) == (0, 0, 3) and len(t.tasks) == 3


def test_classify_exclude_and_tool():
    t = FakeSupplies()
    run(t, [tx("a", "2026-11-05", 3000, "ＮＩＧＩＴＡ 雑貨"), tx("b", "2026-11-05", 2200, "AIサービス月額")])
    by = {x["mf_id"]: x for x in t.tasks.values()}
    assert by["a"]["s_status"] == "対象外" and by["a"]["section"] == "対象外"
    assert by["b"]["category"] == "ツール・サブスク" and by["b"]["s_status"] == "取り込み済み"


def test_refund_is_negative_and_high_value_reported():
    t = FakeSupplies()
    st = run(t, [tx("a", "2026-11-05", 120000), tx("b", "2026-11-06", -800)])
    assert st.high == [120000] and st.new_amount == 119200
    assert "高額" in supplies.summary(st, CFG, "2026-11-01", "2026-11-30", 2)


def test_manual_edits_are_respected():
    t = FakeSupplies()
    run(t, [tx("a", "2026-11-05", 1000), tx("b", "2026-11-06", 500), tx("c", "2026-11-07", 700)])
    by = {x["mf_id"]: x for x in t.tasks.values()}
    by["a"].update(amount=900, manual="あり", category="その他")           # 訂正（手動修正あり）
    by["b"].update(s_status="対象外", section="対象外")                    # 取消
    by["c"].update(category="その他")                                      # 区分だけ直した
    st = run(t, [tx("a", "2026-11-05", 1000), tx("b", "2026-11-06", 500), tx("c", "2026-11-07", 750)])
    assert by["a"]["amount"] == 900 and by["a"]["category"] == "その他"
    assert by["b"]["s_status"] == "対象外"                                  # 取り込み済みに戻さない
    assert by["c"]["amount"] == 750 and by["c"]["category"] == "その他"    # 金額は更新、区分は上書きしない
    assert st.created == 0 and st.updated == 1


def test_vanished_txn_becomes_excluded_but_manual_entries_stay():
    t = FakeSupplies()
    run(t, [tx("a", "2026-11-05", 1000), tx("b", "2026-11-06", 500)])
    t.upsert(None, "現金立替", {"business_date": "2026-11-06", "amount": 300, "source": "手入力", "s_status": "取り込み済み"}, None, "備品消耗品")
    old = tx("z", "2026-10-01", 100)
    run(t, [old], start="2026-10-01", end="2026-10-31")                     # 期間外のものは触らない
    st = run(t, [tx("a", "2026-11-05", 1000)])
    by = {x.get("mf_id"): x for x in t.tasks.values()}
    assert by["b"]["s_status"] == "対象外" and by["a"]["s_status"] == "取り込み済み"
    assert st.excluded == 1
    assert any(x.get("source") == "手入力" and x["s_status"] == "取り込み済み" for x in t.tasks.values())
    before = {k: dict(v) for k, v in t.tasks.items()}
    run(t, [])                                                              # 0件は読み違いかもしれないので何もしない
    assert t.tasks == before


def test_dry_run_writes_nothing():
    t = FakeSupplies()
    st = run(t, [tx("a", "2026-11-05", 1000)], dry_run=True)
    assert st.created == 1 and not t.tasks


def test_step_gates_by_interval_and_backfills_first():
    deps = make_deps()
    deps.settings = replace(deps.settings, supplies=CFG)
    deps.supplies = FakeSupplies()
    deps.bank.cards = [tx("a", "2026-08-03", 1000), tx("b", "2026-11-14", 2000)]
    now = datetime(2026, 11, 15, 3, 0, tzinfo=UTC)
    r = supplies.step(deps, now)
    assert r["created"] == 2                                                 # 初回は 2026-08-01 から
    assert supplies.step(deps, now.replace(hour=4)) == {"skipped": "interval"}
    deps.bank.cards.append(tx("c", "2026-11-15", 300))
    r = supplies.step(deps, now.replace(hour=7))
    assert r["created"] == 1
    assert any("備品・消耗品" in p["text"] and p["channel"] == "CTEST" for p in deps.notifier.posts)


def test_step_is_noop_when_disabled_and_runs_in_job():
    deps = make_deps()
    assert supplies.step(deps) == {"skipped": True}
    assert "supplies" in [n for n, _ in jobs.RECONCILE_STEPS]
