"""出資者向けの月次損益計算書（docs/investor_report.md）。金額・名前はすべて架空。"""

from dataclasses import replace
from datetime import date, datetime

import pytest
from helpers import JST, make_deps

from barnull_sales import investor_report as ir
from barnull_sales.config import InvestorReportConfig


def slip(d, amount, fee=0, status="決済確認済み", sid="S-1"):
    return {"営業日": d, "伝票金額": amount, "決済手数料": fee, "伝票ステータス": status, "伝票ID": sid}


def rental(d, amount, sid="RS-IB-202610"):
    return {"営業日": d, "伝票ID": sid, "入金予定額": amount, "伝票ステータス": "決済確認済み"}


def pay(month, amount, kind="スタッフ", status="確定"):
    return {"対象月": month, "給与": amount, "区分": kind, "給与ステータス": status}


def pur(d, amount, rid="P-1", status="取り込み済み"):
    return {"仕入記録ID": rid, "営業日": d, "仕入金額（税込）": amount, "仕入ステータス": status, "店舗": ""}


def sup(d, amount, status="取り込み済み"):
    return {"営業日": d, "金額（税込）": amount, "備品ステータス": status, "店舗": ""}


def fx(name, amount, category="契約・サービス", start="2026-01", end=None):
    return {"_name": name, "固定費区分": category, "金額（税込）": amount, "按分月数": 1, "適用開始月": start,
            "適用終了月": end, "固定費種別": "通常", "固定費ステータス": "有効", "店舗": ""}


def sources(**kw):
    base = dict(
        slips=[slip("2026-10-03", 100000, fee=3000), slip("2026-10-31", 50000, fee=1500),
               slip("2026-11-01", 999999), slip("2026-10-05", 77777, status="対象外"),
               rental("2026-10-31", 20000), rental("2026-10-31", 5000, "RS-SM-202610")],
        payroll=[pay("2026-10", 30000), pay("2026-10", 8000, "プロモーター"), pay("2026-09", 99999)],
        purchase=[pur("2026-08-10", 1), pur("2026-10-02", 12000), pur("2026-10-20", -1000, "P-2"),
                  pur("2026-10-21", 5555, status="対象外"), pur("2026-11-02", 7)],
        supplies=[sup("2026-08-10", 1), sup("2026-10-15", 4000), sup("2026-10-16", 300, "対象外"), sup("2026-11-03", 9)],
        fixed=[fx("家賃", 100000, "家賃・共益費"), fx("共益費", 10000, "家賃・共益費"), fx("ガス代", 500, "光熱・設備"),
               fx("設備利用費(セコム、水道、電気)", 600, "光熱・設備"), fx("通信費", 700), fx("リース費", 800),
               fx("カラオケ費", 900), fx("ゴミ回収費", 1000), fx("出資者配当金", 50000, "返済・配当"),
               fx("銀行借入の返済", 40000, "返済・配当"), fx("役員報酬", 20000, "その他", start="2026-10")],
    )
    base.update(kw)
    return ir.Sources(**base)


TODAY = date(2026, 11, 10)


def test_build_maps_items_like_the_investor_sheet():
    st = ir.build(sources(), "2026-10", TODAY)
    assert st.problems == []
    assert st.sales == 150000 + 25000                    # 通常営業（対象外・翌月を除く）＋ レンタル店舗収益
    assert st.cogs == 11000                              # 仕入（返品はマイナス、対象外は除く）
    assert st.sga["地代家賃"] == 110000                   # 家賃 ＋ 共益費
    assert st.sga["水道光熱費"] == 1100                   # ガス代 ＋ 設備利用費
    assert st.sga["人件費(店長)"] == 20000               # 固定費の役員報酬（シートの「人件費(店長)」）
    assert st.sga["人件費(バイト)"] == 30000             # 給与（プロモーター以外）
    assert st.sga["広告宣伝費"] == 8000                   # プロモーターの給与（バック）
    assert st.sga["決済手数料"] == 4500
    assert st.sga["備品・消耗品費"] == 4000
    assert (st.sga["通信費"], st.sga["リース費"], st.sga["カラオケ費"], st.sga["ゴミ回収費"]) == (700, 800, 900, 1000)
    assert "出資者配当金" not in str(st.sga_lines) and st.detail["excluded_fixed"] == {"出資者配当金": 50000,
                                                                                    "銀行借入の返済": 40000}
    assert st.sga_total == 110000 + 1100 + 50000 + 8000 + 800 + 900 + 4000 + 4500 + 1000 + 700
    assert st.gross == 175000 - 11000 and st.operating == st.gross - st.sga_total
    names = [k for k, _ in st.sga_lines]
    assert names[:5] == ["地代家賃", "水道光熱費", "人件費(店長)", "人件費(バイト)", "広告宣伝費"]   # シートと同じ並び
    assert names[-1] == "通信費"                          # その他が 0 円なら行を出さない


def test_unknown_fixed_item_goes_to_other_with_a_note():
    st = ir.build(sources(fixed=sources().fixed + [fx("謎の会費", 333)]), "2026-10", TODAY)
    assert st.sga["その他"] == 333 and ("その他", 333) in st.sga_lines
    assert any("謎の会費" in n for n in st.notes) and st.problems == []


@pytest.mark.parametrize("kw, word", [
    (dict(payroll=[]), "給与の記録がありません"),
    (dict(payroll=[pay("2026-10", 1000, status="見込み")]), "確定していません"),
    (dict(payroll=[pay("2026-10", None)]), "給与が空"),
    (dict(slips=[slip("2026-10-03", 1000)]), "レンタルスペース"),
    (dict(slips=[rental("2026-10-31", 1)]), "伝票（通常営業"),
    (dict(purchase=[pur("2026-08-10", 1), pur("2026-10-02", 12000)]), "仕入の記録"),        # 最新が月末より 10 日以上前
    (dict(purchase=[]), "仕入の記録"),
    (dict(supplies=[sup("2026-10-10", 1)]), "備品・消耗品の記録"),                         # 取り込み開始より前の月初を含まない
    (dict(fixed=[]), "固定費が未設定"),
])
def test_missing_data_blocks_issuing(kw, word):
    st = ir.build(sources(**kw), "2026-10", TODAY)
    assert any(word in p for p in st.problems), st.problems


def test_rental_settled_by_a_later_month_counts_as_zero():
    s = sources(slips=[slip("2026-10-03", 1000), rental("2026-11-30", 7, "RS-IB-202611")])
    st = ir.build(s, "2026-10", TODAY)
    assert not any("レンタル" in p for p in st.problems) and st.detail["rental"] == 0


def test_render_makes_a_one_page_pdf():
    st = ir.build(sources(), "2026-10", TODAY)
    pdf = ir.render(st, company="株式会社テスト", created=TODAY)
    assert pdf.startswith(b"%PDF") and pdf.count(b"/Type /Page\n") + pdf.count(b"/Type /Page ") <= 2
    assert ir.yen(-1234) == "-¥1,234" and ir.wareki(date(2026, 10, 1)) == "令和8年10月1日"
    assert ir.file_title("2026-10") == "202610_月次損益計算書（Bar null）.pdf"


# ---------- 毎日のジョブのステップ ----------

class FakeDrive:
    def __init__(self, existing=None, fail=False):
        self.existing, self.fail, self.uploads = existing, fail, []

    def find(self, folder, name):
        return self.existing

    def upload(self, folder, name, data):
        if self.fail:
            raise RuntimeError("drive down")
        self.uploads.append((folder, name, data[:4]))
        return {"id": "F1", "webViewLink": "https://drive.google.com/file/d/F1/view"}


def deps_on(**cfg):
    d = make_deps()
    d.settings = replace(d.settings, investor_report=InvestorReportConfig(enabled=True, drive_folder_id="FOLDER", **cfg))
    return d


def at(day, month=11):
    return datetime(2026, month, day, 10, 5, tzinfo=JST)


def test_step_waits_until_the_10th_and_skips_when_disabled():
    d = deps_on()
    assert ir.step(d, at(9), loader=lambda: pytest.fail("読まない")) == "not-yet"
    off = make_deps()
    assert ir.step(off, at(10), loader=lambda: pytest.fail("読まない")) == "skipped"


def test_step_issues_once_and_posts_the_url_without_amounts():
    d, drive = deps_on(), FakeDrive()
    assert ir.step(d, at(10), loader=sources, drive=drive) == "issued-now"
    assert drive.uploads == [("FOLDER", "202610_月次損益計算書（Bar null）.pdf", b"%PDF")]
    doc = d.store.get_doc(ir.COLLECTION, "2026-10")
    assert doc["status"] == "issued" and doc["url"].endswith("/F1/view")
    text = d.notifier.posts[-1]["text"]
    assert "2026年10月分" in text and "F1" in text and "¥" not in text and "<@" not in text
    assert ir.step(d, at(11), loader=lambda: pytest.fail("読まない"), drive=drive) == "issued"
    assert len(drive.uploads) == 1


def test_step_reuses_a_file_already_in_the_folder():
    d, drive = deps_on(), FakeDrive(existing={"id": "OLD"})
    assert ir.step(d, at(12), loader=sources, drive=drive) == "issued-now"
    assert drive.uploads == [] and d.store.get_doc(ir.COLLECTION, "2026-10")["file_id"] == "OLD"


def test_step_reports_missing_data_and_renotifies_only_when_needed():
    d, drive = deps_on(renotify_days=3), FakeDrive()
    load = lambda: sources(payroll=[])   # noqa: E731
    assert ir.step(d, at(10), loader=load, drive=drive) == "waiting"
    assert len(d.notifier.posts) == 1 and "給与の記録がありません" in d.notifier.posts[0]["text"]
    assert "¥" not in d.notifier.posts[0]["text"] and "<@" not in d.notifier.posts[0]["text"]
    assert ir.step(d, at(11), loader=load, drive=drive) == "waiting"
    assert len(d.notifier.posts) == 1                                   # 同じ理由は3日おき
    assert ir.step(d, at(13), loader=load, drive=drive) == "waiting"
    assert len(d.notifier.posts) == 2
    assert ir.step(d, at(14), loader=sources, drive=drive) == "issued-now"   # そろったら発行
    assert drive.uploads and "発行しました" in d.notifier.posts[-1]["text"]


def test_step_failure_is_reported_and_raised():
    d = deps_on()
    with pytest.raises(RuntimeError):
        ir.step(d, at(10), loader=sources, drive=FakeDrive(fail=True))
    assert "失敗しました" in d.notifier.posts[-1]["text"]
    assert d.store.get_doc(ir.COLLECTION, "2026-10") is None


def test_daily_job_includes_the_step():
    from barnull_sales import jobs

    assert ("investor_report", ir.step) in jobs.DAILY_STEPS
