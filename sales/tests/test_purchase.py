"""仕入（NOS → 伝票 → Asana）の計算と冪等性。値は架空（実際の価格・取引は git に入れない）。"""

from datetime import date

import pytest

from barnull_sales.purchase import (
    COLUMNS,
    NosFormatError,
    Totals,
    check_totals,
    fetch_range,
    group,
    parse_data,
    parse_rows,
    rule_name,
)
from barnull_sales.purchase_asana import sync


def row(status="出荷確定（完納）", d="2026/11/12", voucher="V1", order="O1", code="C1", name="テスト ウイスキー ４０°７００ｍｌ",
        cat="ウイスキー", ordered=2, qty=2, price=1000, amount=None):
    amount = qty * price if amount is None else amount
    return [status, d, voucher, order, "490000", code, name, cat, "6", "700ml", "バラ", str(ordered), str(qty),
            f"{price:,}", f"{amount:,}"]


ROWS = [
    row(),
    row(voucher="V1", code="C2", name="テスト ひげ茶 １．５Ｌ", cat="飲料", ordered=4, qty=4, price=250),
    row(voucher="V2", d="2026/11/13", code="C3", name="※テスト ジン ４５°７００ｍｌ", cat="スピリッツ", ordered=3, qty=1,
        price=3000, status="出荷確定（欠品有）"),
    row(voucher="R1", d="2026/11/14", order="", code="C2", name="テスト ひげ茶 １．５Ｌ", cat="飲料", ordered=-2, qty=-2,
        price=250),
]


def vouchers():
    return group(parse_rows(list(COLUMNS), ROWS))


def test_group_tax_and_returns():
    v = {x.voucher: x for x in vouchers()}
    assert v["V1"].net == 3000 and v["V1"].tax == 200 + 80 and v["V1"].gross == 3280
    assert v["V1"].record_id == "P-20261112-V1"
    assert v["V2"].shortage and v["V2"].qty == 1 and v["V2"].tax == 300
    assert v["R1"].net == -500 and v["R1"].tax == -40       # 返品はマイナス、端数は0の側へ
    assert v["R1"].order_nos == ""


def test_columns_by_name_and_format_error():
    header = list(reversed(COLUMNS))
    lines = parse_rows(header, [list(reversed(r)) for r in ROWS])
    assert lines[0].amount == 2000 and lines[0].nos_category == "ウイスキー"
    with pytest.raises(NosFormatError):
        parse_rows(["ステータス", "取引日"], [["x", "2026/11/12"]])


def test_fetch_range():
    assert fetch_range(date(2026, 11, 15)) == (date(2026, 11, 1), date(2026, 11, 15))
    assert fetch_range(date(2026, 11, 3)) == (date(2026, 10, 1), date(2026, 11, 3))
    assert fetch_range(date(2026, 1, 5)) == (date(2025, 12, 1), date(2026, 1, 5))
    assert fetch_range(date(2026, 11, 15), date(2026, 8, 1))[0] == date(2026, 8, 1)


def test_check_totals():
    vs = vouchers()
    net = sum(v.net for v in vs)
    tax = sum(v.tax for v in vs)
    assert check_totals(vs, Totals(net, tax + 2, net + tax + 2)) == []
    assert check_totals(vs, Totals(net + 1, tax, 0))
    assert check_totals(vs, None)


def test_notes_roundtrip():
    v = vouchers()[0]
    data = parse_data(v.notes({"C1": "テストウイスキー"}))
    assert [d[0] for d in data] == ["C1", "C2"] and data[0][5] == 2000
    assert "テストウイスキー × 2" in v.notes({"C1": "テストウイスキー"})


def test_rule_name():
    assert rule_name("クエルボ１８００アネホ ４０°７５０ｍｌ") == "クエルボ1800アネホ"
    assert rule_name("※スタンダード ゾンビ ３００ｍｌ １８０－９６") == "スタンダード ゾンビ"
    assert rule_name("コカ コーラペット ５００ｍｌ") == "コカ コーラ"


class FakeTracker:
    def __init__(self):
        self.tasks = {}
        self.writes = 0

    def list_tasks(self):
        return [dict(t, gid=g) for g, t in self.tasks.items()]

    def upsert(self, gid, name, values, notes, section):
        self.writes += 1
        if gid is None:
            gid = f"g{len(self.tasks) + 1}"
            self.tasks[gid] = {}
        t = self.tasks[gid]
        t.update(values)
        t["name"] = name
        t["section"] = section
        if notes is not None:
            t["notes"] = notes
        return gid


def test_sync_idempotent_and_exclusion():
    tr = FakeTracker()
    asked = []

    def suggest(items):
        asked.append(dict(items))
        return {k: "名" + k for k in items}, True

    st = sync(tr, vouchers(), "2026-11-01", "2026-11-15", suggest)
    assert st.created == 3 and len(st.new_items) == 3 and asked[0].keys() == {"C1", "C2", "C3"}
    masters = [t for t in tr.tasks.values() if t["record_id"].startswith("M-")]
    assert {m["m_status"] for m in masters} == {"案（自動）"} and all("business_date" not in m for m in masters)

    # 2回目: 何も変わらない（書き込みなし、名前も聞かない）
    w = tr.writes
    st = sync(tr, vouchers(), "2026-11-01", "2026-11-15", suggest)
    assert (st.created, st.updated, st.unchanged) == (0, 0, 3) and tr.writes == w and len(asked) == 1

    # 本人が表示名を直して確認済みにした → 次の取り込みで伝票の要約に反映され、マスタは上書きしない
    m1 = next(t for t in tr.tasks.values() if t["record_id"] == "M-C1")
    m1.update(display_name="ウイスキーA", m_status="確認済み")
    st = sync(tr, vouchers(), "2026-11-01", "2026-11-15", suggest)
    assert st.updated == 1 and m1["display_name"] == "ウイスキーA"
    assert "ウイスキーA" in next(t for t in tr.tasks.values() if t["record_id"] == "P-20261112-V1")["summary"]

    # NOS から V2 が消えた → 対象外（期間外の伝票は触らない）
    rest = [v for v in vouchers() if v.voucher != "V2"]
    st = sync(tr, rest, "2026-11-01", "2026-11-15", suggest)
    v2 = next(t for t in tr.tasks.values() if t["record_id"] == "P-20261113-V2")
    assert st.excluded == 1 and v2["p_status"] == "対象外" and v2["section"] == "対象外"
    st = sync(tr, [], "2026-11-14", "2026-11-15", suggest)
    assert st.excluded == 1   # R1 だけ（V1 は期間外）


def test_unknown_category_is_review():
    rows = [row(cat="新しいカテゴリ")]
    v = group(parse_rows(list(COLUMNS), rows))
    tr = FakeTracker()
    st = sync(tr, v, "2026-11-01", "2026-11-30", lambda items: ({k: k for k in items}, True))
    assert st.review and next(t for t in tr.tasks.values() if t["record_id"].startswith("P-"))["section"] == "要確認"


def test_rename_drafts_keeps_confirmed():
    tr = FakeTracker()
    sync(tr, vouchers(), "2026-11-01", "2026-11-15", lambda items: ({k: "規則" + k for k in items}, False))
    next(t for t in tr.tasks.values() if t["record_id"] == "M-C1").update(display_name="本人の名前", m_status="確認済み")
    st = sync(tr, vouchers(), "2026-11-01", "2026-11-15", lambda items: ({k: "AI" + k for k in items}, True),
              rename_drafts=True)
    names = {t["record_id"]: t["display_name"] for t in tr.tasks.values() if t["record_id"].startswith("M-")}
    assert names == {"M-C1": "本人の名前", "M-C2": "AIC2", "M-C3": "AIC3"} and st.renamed == 2
    assert "AIC2" in next(t for t in tr.tasks.values() if t["record_id"] == "P-20261112-V1")["notes"]
