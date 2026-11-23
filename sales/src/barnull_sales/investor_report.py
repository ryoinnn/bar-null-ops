"""出資者向けの月次損益計算書（docs/investor_report.md）。

ポータル（Artifact「Bar null ポータル」）と同じ Asana のデータから、前月分の損益計算書を PDF 1枚で作り、
Google ドライブの共有フォルダに置いて URL を発行する。出資者に見せるのは売上高・売上原価・販管費（と、その差の
売上総利益・営業利益）だけ。顧客名・従業員名・時給は載せない（人件費はシートと同じく店長・バイトの2行。2026-11-23 本人の指示）。

| いつ                     | 何をする                                                                 |
|-------------------------|-------------------------------------------------------------------------|
| 毎日のジョブ（10:05）      | 10日以降で、前月分がまだ発行されていなければ、集計 → そろっていれば PDF → ドライブ → Slack |
| そろっていない             | 足りないものを #null_system-monitor に知らせる（メンションなし。内容が同じなら3日おき）。翌日もう一度 |
| 発行済み                  | 何もしない（同じ月を二度作らない。フォルダに同じ名前のファイルがあれば、それを発行済みとして使う） |

項目と元のデータ（contract.md §4 の定義に合わせる。金額は税込）
- 売上高 ＝ 伝票金額の合計（対象外を除き未収を含む）＋ レンタルスペースの店舗収益（RS- の入金予定額。月次確定値）
- 売上原価 ＝ 仕入（NIGITA。仕入金額（税込）、返品はマイナス）
- 販管費 ＝ 固定費（返済・配当を除く。役員報酬は人件費(店長)）＋ 人件費(バイト)（給与。プロモーターを除く）
          ＋ 広告宣伝費（プロモーターの給与＝バック）
          ＋ 備品・消耗品（カード）＋ 決済手数料（Square）
"""

from __future__ import annotations

import calendar
import io
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

from . import fixed as fixed_mod

log = logging.getLogger(__name__)

COLLECTION = "investor_reports"          # Firestore: 月ごとの発行の状態（investor_reports/YYYY-MM）
DRIVE_SCOPE = "https://www.googleapis.com/auth/drive"
IMPORT_START = "2026-08-01"              # 仕入・備品消耗品の取り込みの始まり（contract.md §5）
LAG_DAYS = {"purchase": 10, "supplies": 14}   # 最新の記録がこれ以上古いと「一部のみ」（contract.md §5）
PROMOTER = "プロモーター"
CONFIRMED = "確定"

# 販管費の行（出資者向けシートと同じ並びと名前）
LABOR_OWNER, LABOR_STAFF = "人件費(店長)", "人件費(バイト)"
SGA_ORDER = ("地代家賃", "水道光熱費", LABOR_OWNER, LABOR_STAFF, "広告宣伝費", "リース費", "カラオケ費", "備品・消耗品費", "決済手数料",
             "ゴミ回収費", "通信費", "その他")
# 固定費の項目名（頭の一致）→ 販管費の行。返済・配当の区分は販管費に入れない（費用ではない）
FIXED_LINES = (("家賃", "地代家賃"), ("共益費", "地代家賃"), ("ガス代", "水道光熱費"), ("設備利用費", "水道光熱費"),
               ("水道", "水道光熱費"), ("電気", "水道光熱費"), ("リース費", "リース費"), ("カラオケ費", "カラオケ費"),
               ("ゴミ回収費", "ゴミ回収費"), ("通信費", "通信費"), ("役員報酬", LABOR_OWNER), ("広告宣伝費", "広告宣伝費"),
               ("その他", "その他"))
EXCLUDED_FIXED_CATEGORIES = ("返済・配当",)


# ---------- データ（Asana の1タスク ＝ {欄の名前: 値}） ----------

@dataclass
class Sources:
    slips: list[dict]
    payroll: list[dict]
    purchase: list[dict]
    supplies: list[dict]
    fixed: list[dict]


_OPT = ("name,custom_fields.name,custom_fields.resource_subtype,custom_fields.type,custom_fields.text_value,"
        "custom_fields.number_value,custom_fields.date_value,custom_fields.enum_value.name")


def read_project(api, gid: str) -> list[dict]:
    from .fixed_asana import _value

    out = []
    for t in api.list("/tasks", {"project": gid, "opt_fields": _OPT, "limit": 100}):
        f = {c.get("name"): _value(c) for c in t.get("custom_fields") or []}
        f["_name"] = t.get("name") or ""
        out.append(f)
    return out


def project_ids(ids: dict, env: str) -> dict[str, str]:
    return {"slips": ids[env]["project"], "payroll": ids["labor"][env]["payroll"],
            "purchase": ids["purchase"][env]["project"], "supplies": ids["supplies"][env]["project"],
            "fixed": ids["fixed"][env]["project"]}


def load(api, ids: dict, env: str) -> Sources:
    p = project_ids(ids, env)
    return Sources(**{k: read_project(api, gid) for k, gid in p.items()})


# ---------- 集計 ----------

def _txt(v) -> str:
    return str(v or "").strip()


def _int(v) -> int | None:
    if v is None or v == "":
        return None
    return int(round(float(v)))


def month_bounds(month: str) -> tuple[date, date]:
    y, m = map(int, month.split("-"))
    return date(y, m, 1), date(y, m, calendar.monthrange(y, m)[1])


def prev_month(d: date) -> str:
    first = d.replace(day=1) - timedelta(days=1)
    return f"{first.year:04d}-{first.month:02d}"


def fixed_task(f: dict) -> dict:
    return {"name": f.get("_name", ""), "record_id": f.get("固定費ID"), "category": f.get("固定費区分"),
            "amount": f.get("金額（税込）"), "months": f.get("按分月数"), "start": f.get("適用開始月"),
            "end": f.get("適用終了月"), "kind": f.get("固定費種別"), "status": f.get("固定費ステータス"),
            "store": _txt(f.get("店舗"))}


def fixed_line(name: str) -> str | None:
    n = "".join((name or "").split())
    return next((line for key, line in FIXED_LINES if n.startswith(key)), None)


@dataclass
class Statement:
    month: str
    sales: int = 0
    cogs: int = 0
    sga: dict[str, int] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)   # そろっていないもの（あれば発行しない）
    notes: list[str] = field(default_factory=list)      # 本人への補足（発行はする。PDF には出さない）
    detail: dict = field(default_factory=dict)          # 突き合わせ用の内訳（PDF には出さない）

    @property
    def gross(self) -> int:
        return self.sales - self.cogs

    @property
    def sga_total(self) -> int:
        return sum(self.sga.values())

    @property
    def operating(self) -> int:
        return self.gross - self.sga_total

    @property
    def sga_lines(self) -> list[tuple[str, int]]:
        """表に出す販管費の行。「その他」は 0 円なら出さない（シートに無い行のため）。"""
        return [(k, self.sga.get(k, 0)) for k in SGA_ORDER if k != "その他" or self.sga.get(k)]

    def totals(self) -> dict:
        return {"sales": self.sales, "cogs": self.cogs, "gross": self.gross, "sga": self.sga_total,
                "operating": self.operating}


def _coverage(kind: str, first: str | None, last: str | None, month: str, today: date) -> str:
    """仕入・備品消耗品の「記録あり」の判定（contract.md §5 の 1〜4）。ok／partial／none。"""
    a, end = month_bounds(month)
    b = min(end, today)
    if not first or date.fromisoformat(first) > b:
        return "none"
    if date.fromisoformat(first) > a and a < date.fromisoformat(IMPORT_START):
        return "partial"
    if (b - date.fromisoformat(last)).days >= LAG_DAYS[kind]:
        return "partial"
    return "ok"


def build(src: Sources, month: str, today: date) -> Statement:
    st = Statement(month=month)
    a, b = month_bounds(month)
    sga = dict.fromkeys(SGA_ORDER, 0)
    in_month = lambda d: bool(d) and d[:7] == month   # noqa: E731

    # 売上（通常営業）・決済手数料・レンタル
    regular = fee = n = rental = 0
    rental_months: set[str] = set()
    missing_amount = 0
    for f in src.slips:
        d = _txt(f.get("営業日"))[:10]
        if not d or _txt(f.get("伝票ステータス")) == "対象外":
            continue
        if _txt(f.get("伝票ID")).startswith("RS-"):
            rental_months.add(d[:7])
            if in_month(d):
                rental += _int(f.get("入金予定額")) or 0
            continue
        if not in_month(d):
            continue
        amt = _int(f.get("伝票金額"))
        if amt is None:
            missing_amount += 1
            continue
        regular += amt
        fee += _int(f.get("決済手数料")) or 0
        n += 1
    if n == 0:
        st.problems.append("伝票（通常営業の売上）が1件もありません")
    if missing_amount:
        st.problems.append(f"伝票金額が空の伝票が {missing_amount} 件あります")
    if not any(m >= month for m in rental_months):
        st.problems.append("レンタルスペースの月次確定（伝票ID が RS- の記録）がまだありません")
    st.sales = regular + rental
    sga["決済手数料"] += fee

    # 売上原価（仕入）
    pur = 0
    first = last = None
    pur_partial = False
    for f in src.purchase:
        d = _txt(f.get("営業日"))[:10]
        if (not _txt(f.get("仕入記録ID")).startswith("P-") or not d or _txt(f.get("仕入ステータス")) == "対象外"
                or _txt(f.get("店舗"))):
            continue
        first, last = min(first or d, d), max(last or d, d)
        if in_month(d):
            v = _int(f.get("仕入金額（税込）"))
            if v is None:
                pur_partial = True
            else:
                pur += v
    cov = _coverage("purchase", first, last, month, today)
    if cov != "ok" or pur_partial:
        st.problems.append(f"仕入の記録がそろっていません（{'記録なし' if cov == 'none' else '一部のみ'}。最新の記録 {last or 'なし'}）")
    st.cogs = pur

    # 備品・消耗品
    sup = 0
    first = last = None
    sup_partial = False
    for f in src.supplies:
        d = _txt(f.get("営業日"))[:10]
        if not d or _txt(f.get("備品ステータス")) == "対象外" or _txt(f.get("店舗")):
            continue
        first, last = min(first or d, d), max(last or d, d)
        if in_month(d):
            v = _int(f.get("金額（税込）"))
            if v is None:
                sup_partial = True
            else:
                sup += v
    cov = _coverage("supplies", first, last, month, today)
    if cov != "ok" or sup_partial:
        st.problems.append(f"備品・消耗品の記録がそろっていません（{'記録なし' if cov == 'none' else '一部のみ'}。最新の記録 {last or 'なし'}）")
    sga["備品・消耗品費"] += sup

    # 人件費(バイト)・広告宣伝費（給与）。プロモーターの給与（バック）は広告宣伝費（出資者向けシートの「広告宣伝費(バック)」）
    rows = [f for f in src.payroll if _txt(f.get("対象月")) == month]
    labor = promo = 0
    if not rows:
        st.problems.append("給与の記録がありません（Asana「Bar null 給与」に対象月の記録が無い）")
    for f in rows:
        v = _int(f.get("給与"))
        if v is None:
            st.problems.append("給与が空の記録があります")
            continue
        if _txt(f.get("区分")) == PROMOTER:
            promo += v
        else:
            labor += v
    not_confirmed = sum(1 for f in rows if _txt(f.get("給与ステータス")) != CONFIRMED)
    if not_confirmed:
        st.problems.append(f"給与がまだ確定していません（確定でない記録が {not_confirmed} 件）")
    sga[LABOR_STAFF] += labor
    sga["広告宣伝費"] += promo

    # 固定費（返済・配当を除く）。役員報酬は人件費(店長)
    items = fixed_mod.month_amounts([fixed_task(f) for f in src.fixed], month)
    if not items:
        st.problems.append("固定費が未設定です（Asana「Bar null 固定費」に対象月の項目が無い）")
    excluded = {}
    for name, it in items.items():
        if it.get("category") in EXCLUDED_FIXED_CATEGORIES:
            excluded[name] = it["amount"]
            continue
        line = fixed_line(name)
        if line is None:
            line = "その他"
            st.notes.append(f"固定費の項目「{name}」の行が決まっていないため「その他」に入れました")
        sga[line] += it["amount"]
    st.sga = sga
    st.detail = {"sales_regular": regular, "rental": rental, "slips": n, "fee": fee, "purchase": pur,
                 "supplies": sup, "labor_payroll": labor, "promoter": promo, "payroll_rows": len(rows),
                 "fixed": {k: v["amount"] for k, v in items.items()}, "excluded_fixed": excluded,
                 "period": [a.isoformat(), b.isoformat()]}
    return st


# ---------- PDF ----------

FONT_CANDIDATES = (   # 埋め込む日本語フォント（見つかった最初のもの。TTC は何番目の書体か）。無ければ PDF 標準の CID フォント
    ("C:/Windows/Fonts/BIZ-UDGothicR.ttc", "C:/Windows/Fonts/BIZ-UDGothicB.ttc", 1),   # BIZ UDPゴシック（ポータルと同じ）
    ("/usr/share/fonts/opentype/ipaexfont-gothic/ipaexg.ttf", None, 0),               # Cloud Run（Dockerfile で入れる）
    ("/usr/share/fonts/truetype/ipaexfont-gothic/ipaexg.ttf", None, 0),
)


def _fonts() -> tuple[str, str]:
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.pdfbase.ttfonts import TTFont

    if "JP" in pdfmetrics.getRegisteredFontNames():
        return "JP", "JP-B" if "JP-B" in pdfmetrics.getRegisteredFontNames() else "JP"
    for regular, bold, index in FONT_CANDIDATES:
        if Path(regular).exists():
            try:
                pdfmetrics.registerFont(TTFont("JP", regular, subfontIndex=index))
                if bold and Path(bold).exists():
                    pdfmetrics.registerFont(TTFont("JP-B", bold, subfontIndex=index))
                    return "JP", "JP-B"
                return "JP", "JP"
            except Exception:
                log.exception("フォントを読めない: %s", regular)
    pdfmetrics.registerFont(UnicodeCIDFont("HeiseiKakuGo-W5"))
    return "HeiseiKakuGo-W5", "HeiseiKakuGo-W5"


def yen(v: int) -> str:
    return f"-¥{-v:,}" if v < 0 else f"¥{v:,}"


def wareki(d: date) -> str:
    return f"令和{d.year - 2018}年{d.month}月{d.day}日"


def file_title(month: str, store: str = "Bar null") -> str:
    y, m = month.split("-")
    return f"{y}{m}_月次損益計算書（{store}）.pdf"


def render(st: Statement, *, company: str, store: str = "Bar null", created: date | None = None,
           draft: bool = False) -> bytes:
    """A4 縦1枚の PDF。出資者向けシート（月次損益計算書）と同じ並び。"""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    reg, bold = _fonts()
    a, b = month_bounds(st.month)
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=22 * mm, rightMargin=22 * mm, topMargin=22 * mm,
                            bottomMargin=18 * mm, title=f"月次損益計算書 {a.year}年{a.month}月（{store}）",
                            author=company, subject="月次損益計算書")
    ink, muted, line, band = (colors.HexColor(x) for x in ("#1a1a1a", "#666666", "#bfbfbf", "#f1f1ef"))
    title = ParagraphStyle("t", fontName=bold, fontSize=18, leading=24, alignment=1, textColor=ink)
    small = ParagraphStyle("s", fontName=reg, fontSize=9.5, leading=14, textColor=ink)
    small_r = ParagraphStyle("sr", parent=small, alignment=2)
    note = ParagraphStyle("n", fontName=reg, fontSize=8, leading=12, textColor=muted)

    story = []
    if draft:
        story.append(Paragraph("<font color='#b3261e'>試作（数字が確定していないため、出資者には送らない）</font>",
                               ParagraphStyle("d", parent=small, alignment=1)))
        story.append(Spacer(1, 4 * mm))
    story += [Paragraph("月次損益計算書", title), Spacer(1, 8 * mm)]
    head = Table([[Paragraph(company, small), Paragraph(f"（期間：{wareki(a)}～{wareki(b)}）", small_r)]],
                 colWidths=[83 * mm, 83 * mm])
    head.setStyle(TableStyle([("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0)]))
    story += [head, Spacer(1, 3 * mm)]

    rows = [["勘定科目", "", "金額"],
            ["売上高", f"売上高（{store}）", yen(st.sales)],
            ["売上原価", f"売上原価（{store}）", yen(st.cogs)],
            ["売上総利益", "", yen(st.gross)]]
    sga_start = len(rows)
    rows += [["販売費及び一般管理費" if i == 0 else "", k, yen(v)] for i, (k, v) in enumerate(st.sga_lines)]
    rows.append(["", "販管費合計", yen(st.sga_total)])
    sga_end = len(rows) - 1
    rows.append(["営業利益", "", yen(st.operating)])
    last = len(rows) - 1
    style = [
        ("FONTNAME", (0, 0), (-1, -1), reg), ("FONTSIZE", (0, 0), (-1, -1), 10), ("TEXTCOLOR", (0, 0), (-1, -1), ink),
        ("GRID", (0, 0), (-1, -1), 0.6, line), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 4.2), ("BOTTOMPADDING", (0, 0), (-1, -1), 4.2),
        ("LEFTPADDING", (0, 0), (-1, -1), 7), ("RIGHTPADDING", (0, 0), (-1, -1), 7),
        ("ALIGN", (2, 0), (2, -1), "RIGHT"),
        ("SPAN", (0, 0), (1, 0)), ("ALIGN", (0, 0), (-1, 0), "CENTER"), ("BACKGROUND", (0, 0), (-1, 0), band),
        ("FONTNAME", (0, 0), (-1, 0), bold),
        ("SPAN", (0, 3), (1, 3)), ("SPAN", (0, last), (1, last)),
        ("SPAN", (0, sga_start), (0, sga_end)), ("VALIGN", (0, sga_start), (0, sga_end), "MIDDLE"),
        ("FONTNAME", (0, 3), (-1, 3), bold), ("BACKGROUND", (0, 3), (-1, 3), band),
        ("FONTNAME", (1, sga_end), (-1, sga_end), bold),
        ("FONTNAME", (0, last), (-1, last), bold), ("BACKGROUND", (0, last), (-1, last), band),
    ]
    for r, v in ((3, st.gross), (last, st.operating)):
        if v < 0:
            style.append(("TEXTCOLOR", (2, r), (2, r), colors.HexColor("#b3261e")))
    table = Table(rows, colWidths=[46 * mm, 74 * mm, 46 * mm], repeatRows=1)
    table.setStyle(TableStyle(style))
    story += [table, Spacer(1, 5 * mm),
              Paragraph("金額は税込（円）。営業日（6:00〜翌6:00）で月ごとに集計しています。", note),
              Paragraph(f"作成日：{wareki(created or date.today())}", note)]
    doc.build(story)
    return buf.getvalue()


# ---------- Google ドライブ ----------

class Drive:
    """Drive API v3（共有ドライブのフォルダに PDF を置く）。token は呼ぶたびに取り直す関数。"""

    API = "https://www.googleapis.com/drive/v3/files"
    UPLOAD = "https://www.googleapis.com/upload/drive/v3/files"

    def __init__(self, token_fn) -> None:
        self.token_fn = token_fn

    def _h(self) -> dict:
        return {"Authorization": f"Bearer {self.token_fn()}"}

    def find(self, folder: str, name: str) -> dict | None:
        from .http import request

        q = f"'{folder}' in parents and name = '{name.replace(chr(39), chr(92) + chr(39))}' and trashed = false"
        r = request("Google Drive", "GET", self.API, headers=self._h(), params={
            "q": q, "fields": "files(id,name,webViewLink)", "supportsAllDrives": "true",
            "includeItemsFromAllDrives": "true", "corpora": "allDrives"})
        files = r.json().get("files") or []
        return files[0] if files else None

    def upload(self, folder: str, name: str, data: bytes) -> dict:
        from .http import request

        boundary = "bn" + uuid.uuid4().hex
        meta = json.dumps({"name": name, "parents": [folder], "mimeType": "application/pdf"}, ensure_ascii=False)
        body = (f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n{meta}\r\n"
                f"--{boundary}\r\nContent-Type: application/pdf\r\n\r\n").encode() + data + f"\r\n--{boundary}--\r\n".encode()
        r = request("Google Drive", "POST", self.UPLOAD, idempotent=False, timeout=60,
                    headers={**self._h(), "Content-Type": f"multipart/related; boundary={boundary}"},
                    params={"uploadType": "multipart", "supportsAllDrives": "true", "fields": "id,name,webViewLink"},
                    content=body)
        return r.json()


def file_url(f: dict) -> str:
    return f.get("webViewLink") or f"https://drive.google.com/file/d/{f['id']}/view"


# ---------- 毎日のジョブのステップ ----------

def _month_label(month: str) -> str:
    y, m = month.split("-")
    return f"{int(y)}年{int(m)}月"


def step(deps, now: datetime | None = None, loader=None, drive=None) -> str:
    """前月分を発行する（10日以降、まだなら）。戻り値は結果の短い説明（ログ用。金額は出さない）。"""
    from . import health

    s = deps.settings
    cfg = s.investor_report
    if not cfg.enabled or not cfg.drive_folder_id:
        return "skipped"
    now = now or datetime.now(s.tz)
    today = now.astimezone(s.tz).date()
    if today.day < cfg.issue_day:
        return "not-yet"
    month = prev_month(today)
    state = deps.store.get_doc(COLLECTION, month) or {}
    if state.get("status") == "issued":
        return "issued"
    label = _month_label(month)
    try:
        if loader is None:
            from .asana import AsanaAPI
            from .config import secret

            api = AsanaAPI(secret("ASANA_PAT"))
            loader = lambda: load(api, s.asana_ids, s.asana_env)   # noqa: E731
        st = build(loader(), month, today)
        if st.problems:
            key = " / ".join(st.problems)
            last = state.get("notified_on")
            if state.get("problems") != key or not last or (today - date.fromisoformat(last)).days >= cfg.renotify_days:
                health.post(deps, f"【出資者向け資料】{label}分の月次損益計算書をまだ作れません（データがそろっていない）。"
                                  "そろえば次の毎日のジョブ（10:05）で自動で発行します。\n"
                            + "\n".join(f"・{p}" for p in st.problems) + "\n手順: runbook「出資者向けの月次資料」")
                state = {**state, "status": "waiting", "problems": key, "notified_on": today.isoformat()}
                deps.store.set_doc(COLLECTION, month, state)
            return "waiting"
        pdf = render(st, company=cfg.company, store=s.store_name, created=today)
        if drive is None:
            from .http import gcp_token

            drive = Drive(lambda: gcp_token(scopes=DRIVE_SCOPE))
        name = file_title(month, s.store_name)
        f = drive.find(cfg.drive_folder_id, name)
        reused = f is not None
        f = f or drive.upload(cfg.drive_folder_id, name, pdf)
        url = file_url(f)
        deps.store.set_doc(COLLECTION, month, {"status": "issued", "file_id": f["id"], "url": url, "name": name,
                                               "reused": reused, "issued_at": now.isoformat(), "totals": st.totals()})
        extra = "（同じ名前のファイルがフォルダにあったため、それを使いました）" if reused else ""
        notes = "".join(f"\n・{n}" for n in st.notes)
        health.post(deps, f"【出資者向け資料】{label}分の月次損益計算書を発行しました{extra}。\n{url}{notes}")
        return "issued-now"
    except Exception as e:
        try:
            health.post(deps, f"【出資者向け資料】{label}分の月次損益計算書の作成・発行に失敗しました。次の毎日のジョブでもう一度試します。\n"
                              f"エラー: {type(e).__name__}: {str(e)[:200]}\n手順: runbook「出資者向けの月次資料」")
        except Exception:
            log.exception("失敗の通知も送れない")
        raise
