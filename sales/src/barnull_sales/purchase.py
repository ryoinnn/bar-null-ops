"""仕入（NOS の仕入実績一覧 → 伝票 → Asana）の計算。ブラウザ・外部 API に依存しない部分。

要件: docs/profit/purchasing_requirements.md。NOS の明細1行 → Line、伝票番号ごとにまとめて Voucher。
- 金額は NOS の仕入金額（税抜）。返品はマイナスの別伝票として出るので、そのまま記録して集計で差し引く。
- 消費税は伝票ごとに出ないため、行の NOS カテゴリで税率を決めて推定する（飲料・調味料 8%、ほか 10%）。
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, timedelta

SUPPLIER = "NIGITA"
# NOS の一覧の列（2026-11-15 に確認した順）
COLUMNS = ("ステータス", "取引日", "伝票番号", "注文番号", "JANコード", "商品コード", "商品名", "カテゴリ名",
           "入数", "規格", "販売単位", "注文数量", "仕入数量", "仕入単価", "仕入金額")
CATEGORIES = ("酒類", "ソフトドリンク", "食材", "消耗品", "その他")
NOS_CATEGORY = {
    "焼酎": "酒類", "ビール類": "酒類", "果実酒": "酒類", "ウイスキー": "酒類", "スピリッツ": "酒類", "リキュール": "酒類",
    "清酒": "酒類", "日本酒": "酒類", "ワイン": "酒類", "ブランデー": "酒類",
    "飲料": "ソフトドリンク",
    "調味料": "食材", "食品": "食材",
    "雑貨販促品類": "消耗品", "備品類": "消耗品",
}
REDUCED_RATE_CATEGORIES = ("飲料", "調味料", "食品")   # 軽減税率（8%）の NOS カテゴリ
VOUCHER_STATUS = ("取り込み済み", "要確認", "対象外")
MASTER_STATUS = ("案（自動）", "確認済み")
DATA_MARK = "DATA:"   # タスク本文の、この印から後ろがシステムが読む明細（JSON）


class NosFormatError(RuntimeError):
    """NOS の画面の形が想定と違う（列が変わった等）"""


def _int(s) -> int:
    s = unicodedata.normalize("NFKC", str(s or "")).replace(",", "").replace("￥", "").replace("¥", "").strip()
    if not s:
        return 0
    try:
        return int(float(s))
    except ValueError as e:
        raise NosFormatError(f"数値として読めない値: {s!r}") from e


@dataclass(frozen=True)
class Line:
    status: str
    date: str          # YYYY-MM-DD
    voucher: str
    order_no: str
    jan: str
    code: str
    name: str
    nos_category: str
    ordered: int
    qty: int           # 仕入数量（欠品を除いた実際の数量。返品はマイナス）
    unit_price: int
    amount: int        # 仕入金額（税抜）

    @property
    def rate(self) -> float:
        return 0.08 if self.nos_category in REDUCED_RATE_CATEGORIES else 0.10

    @property
    def category(self) -> str:
        return category_of(self.nos_category)

    @property
    def key(self) -> str:
        """商品マスタのキー（商品コード。無ければ元の商品名）"""
        return self.code or self.name

    def to_data(self) -> list:
        return [self.code, self.name, self.nos_category, self.qty, self.unit_price, self.amount, self.status,
                self.ordered]


def category_of(nos_category: str) -> str:
    return NOS_CATEGORY.get(nos_category, "その他")


def parse_rows(header: list[str], rows: list[list[str]]) -> list[Line]:
    """NOS の表（DataTables の全行）を Line にする。列の名前で読むので、列の順が変わっても読める。"""
    idx = {h.strip(): i for i, h in enumerate(header)}
    missing = [c for c in ("取引日", "伝票番号", "商品コード", "商品名", "カテゴリ名", "仕入数量", "仕入単価", "仕入金額")
               if c not in idx]
    if missing:
        raise NosFormatError(f"仕入実績一覧に列がありません: {', '.join(missing)}")

    def g(r: list[str], col: str) -> str:
        i = idx.get(col)
        return str(r[i]).strip() if i is not None and i < len(r) else ""

    out = []
    for r in rows:
        d = g(r, "取引日").replace("/", "-")
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", d):
            raise NosFormatError(f"取引日の形が違います: {d!r}")
        out.append(Line(status=g(r, "ステータス"), date=d, voucher=g(r, "伝票番号"), order_no=g(r, "注文番号"),
                        jan=g(r, "JANコード"), code=g(r, "商品コード"), name=g(r, "商品名"),
                        nos_category=g(r, "カテゴリ名"), ordered=_int(g(r, "注文数量")), qty=_int(g(r, "仕入数量")),
                        unit_price=_int(g(r, "仕入単価")), amount=_int(g(r, "仕入金額"))))
    return out


@dataclass
class Voucher:
    date: str
    voucher: str
    lines: list[Line] = field(default_factory=list)

    @property
    def record_id(self) -> str:
        return f"P-{self.date.replace('-', '')}-{self.voucher}"

    @property
    def order_nos(self) -> str:
        return ", ".join(sorted({ln.order_no for ln in self.lines if ln.order_no}))

    @property
    def net(self) -> int:
        return sum(ln.amount for ln in self.lines)

    @property
    def tax(self) -> int:
        """消費税（推定）。税率ごとに合計して端数を切り捨てる（マイナスは0の側へ）。"""
        by_rate: dict[float, int] = {}
        for ln in self.lines:
            by_rate[ln.rate] = by_rate.get(ln.rate, 0) + ln.amount
        return sum(int(v * r) for r, v in by_rate.items())

    @property
    def gross(self) -> int:
        return self.net + self.tax

    @property
    def qty(self) -> int:
        return sum(ln.qty for ln in self.lines)

    @property
    def shortage(self) -> bool:
        return any(ln.qty < ln.ordered for ln in self.lines)

    @property
    def unknown_categories(self) -> list[str]:
        return sorted({ln.nos_category for ln in self.lines if ln.nos_category not in NOS_CATEGORY})

    @property
    def status(self) -> str:
        return "要確認" if self.unknown_categories else "取り込み済み"

    def summary(self, names: dict[str, str]) -> str:
        """品目の要約（表示名で「クエルボ1800アネホ 4ほか3品」）。"""
        if not self.lines:
            return ""
        top = max(self.lines, key=lambda ln: (abs(ln.amount), ln.name))
        head = f"{names.get(top.key) or top.name} {top.qty}"
        rest = len(self.lines) - 1
        return head + (f"ほか{rest}品" if rest else "")

    def notes(self, names: dict[str, str]) -> str:
        """タスクの本文。人が読む部分と、システムが読む明細（DATA: の後ろの JSON）。"""
        order = f"（注文 {self.order_nos}）" if self.order_nos else "（注文番号なし）"
        out = [f"{SUPPLIER} 伝票 {self.voucher}{order} 取引日 {self.date}", ""]
        for ln in self.lines:
            short = f"（注文 {ln.ordered}・{ln.status}）" if ln.qty != ln.ordered else ""
            out.append(f"・{names.get(ln.key) or ln.name} × {ln.qty}  ¥{ln.amount:,}{short}")
        out += ["", f"税抜 ¥{self.net:,}／消費税（推定）¥{self.tax:,}／税込 ¥{self.gross:,}", "",
                "（この下はシステムが読む明細です。編集しないでください）",
                DATA_MARK + json.dumps([ln.to_data() for ln in self.lines], ensure_ascii=False, separators=(",", ":"))]
        return "\n".join(out)


def group(lines: list[Line]) -> list[Voucher]:
    """伝票番号（と取引日）ごとにまとめる。並びは取引日・伝票番号の順。"""
    by: dict[tuple[str, str], Voucher] = {}
    for ln in lines:
        by.setdefault((ln.date, ln.voucher), Voucher(ln.date, ln.voucher)).lines.append(ln)
    return [by[k] for k in sorted(by)]


def fetch_range(today: date, since: date | None = None) -> tuple[date, date]:
    """読む期間。当月1日〜今日。1〜7日は前月1日から（月末の分を取りこぼさない）。"""
    if since:
        return since, today
    first = today.replace(day=1)
    if today.day <= 7:
        first = (first - timedelta(days=1)).replace(day=1)
    return first, today


@dataclass(frozen=True)
class Totals:
    net: int
    tax: int
    gross: int


def check_totals(vouchers: list[Voucher], nos: Totals | None) -> list[str]:
    """NOS の合計と、伝票の合計を照らし合わせる。合わない点を文で返す（合っていれば空）。"""
    if nos is None:
        return ["NOS の合計（総合計金額）が読めませんでした"]
    net = sum(v.net for v in vouchers)
    tax = sum(v.tax for v in vouchers)
    out = []
    if net != nos.net:
        out.append(f"税抜の合計が NOS と合いません（明細 ¥{net:,}／NOS ¥{nos.net:,}）")
    if abs(tax - nos.tax) > max(10, len(vouchers)):
        out.append(f"消費税の推定が NOS と離れています（推定 ¥{tax:,}／NOS ¥{nos.tax:,}）")
    return out


def parse_data(notes: str) -> list[list]:
    """タスク本文の明細（DATA: の JSON）を読む。無ければ空。"""
    i = (notes or "").rfind(DATA_MARK)
    if i < 0:
        return []
    try:
        return json.loads(notes[i + len(DATA_MARK):].strip())
    except json.JSONDecodeError:
        return []


# ---------------------------------------------------------------- 表示名
_DROP = re.compile(
    r"(\d+(\.\d+)?\s*(ml|l|cl|g|kg)\b|\d+(\.\d+)?\s*°|\d+\s*度|\d+\s*本入|\d+\s*入|ペット|pet|瓶|缶|紙パック|パック|"
    r"業務用|nv\b|\d{3}\s*-\s*\d{2})", re.I)


def rule_name(name: str) -> str:
    """Claude を使えないときの表示名（全角→半角、記号・容量・度数・容器を落とす）。"""
    s = unicodedata.normalize("NFKC", name)
    s = re.sub(r"^[※☆★◎○●\s]+", "", s)
    s = _DROP.sub(" ", s)
    s = re.sub(r"[（(][^）)]*[）)]", lambda m: m.group(0) if re.search(r"[ァ-ヶ]", m.group(0)) else " ", s)
    s = re.sub(r"\s+", " ", s).strip(" ・-")
    return s or unicodedata.normalize("NFKC", name).strip()
