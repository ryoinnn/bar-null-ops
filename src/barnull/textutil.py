"""日時・氏名・金額の文字列処理（管理画面の表記 ⇔ 内部表現）。すべて JST。"""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

JST = ZoneInfo("Asia/Tokyo")
WEEKDAYS = "月火水木金土日"
_TILDE = "[〜~～]"


def now() -> datetime:
    return datetime.now(JST)


def iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def from_iso(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s) if s else None


# ---------- 日時の解析 ----------

_SM_RANGE = re.compile(
    r"(\d{4})/(\d{1,2})/(\d{1,2})\s*\(.\)\s*(\d{1,2}):(\d{2})\s*" + _TILDE +
    r"\s*(?:(\d{4})/(\d{1,2})/(\d{1,2})\s*\(.\)\s*)?(\d{1,2}):(\d{2})"
)


def parse_sm_range(text: str) -> tuple[datetime, datetime] | None:
    """`2026/10/30(金) 20:00〜23:00` / `2026/10/8(木) 21:00〜2026/10/9(金) 0:00`"""
    m = _SM_RANGE.search(text)
    if not m:
        return None
    y, mo, d, h, mi = (int(x) for x in m.group(1, 2, 3, 4, 5))
    start = datetime(y, mo, d, h, mi, tzinfo=JST)
    if m.group(6):
        ey, emo, ed = (int(x) for x in m.group(6, 7, 8))
    else:
        ey, emo, ed = y, mo, d
    end = datetime(ey, emo, ed, int(m.group(9)), int(m.group(10)), tzinfo=JST)
    if end <= start:
        end += timedelta(days=1)
    return start, end


_IB_RANGE = re.compile(
    r"(\d{4})年(\d{1,2})月(\d{1,2})日\s*\(.\)\s*(\d{1,2})時(\d{2})分\s*" + _TILDE +
    r"\s*(?:(\d{4})年(\d{1,2})月(\d{1,2})日\s*\(.\)\s*)?(\d{1,2})時(\d{2})分"
)


def parse_ib_range(text: str) -> tuple[datetime, datetime] | None:
    """`2026年11月01日(日) 10時00分〜15時30分`（終了が開始以前なら翌日とみなす）"""
    m = _IB_RANGE.search(text)
    if not m:
        return None
    y, mo, d, h, mi = (int(x) for x in m.group(1, 2, 3, 4, 5))
    start = datetime(y, mo, d, h, mi, tzinfo=JST)
    if m.group(6):
        ey, emo, ed = (int(x) for x in m.group(6, 7, 8))
    else:
        ey, emo, ed = y, mo, d
    eh = int(m.group(9))
    end_day = datetime(ey, emo, ed, tzinfo=JST) + timedelta(days=eh // 24)
    end = end_day.replace(hour=eh % 24, minute=int(m.group(10)))
    if end <= start:
        end += timedelta(days=1)
    return start, end


_IB_DT = re.compile(r"(\d{4})年(\d{1,2})月(\d{1,2})日\s*\(.\)\s*(\d{1,2})時(\d{2})分")


def parse_ib_datetime(text: str) -> datetime | None:
    m = _IB_DT.search(text)
    if not m:
        return None
    y, mo, d, h, mi = (int(x) for x in m.groups())
    return datetime(y, mo, d, h, mi, tzinfo=JST)


_SLASH_DT = re.compile(r"(\d{4})/(\d{1,2})/(\d{1,2})\s+(\d{1,2}):(\d{2})")


def parse_slash_datetime(text: str) -> datetime | None:
    """`2026/10/05 07:28`（IB のメッセージ時刻）"""
    m = _SLASH_DT.search(text)
    if not m:
        return None
    y, mo, d, h, mi = (int(x) for x in m.groups())
    return datetime(y, mo, d, h, mi, tzinfo=JST)


# ---------- 日時の表示 ----------

def fmt_date(dt: datetime) -> str:
    """2026/10/30（金）"""
    return f"{dt:%Y/%m/%d}（{WEEKDAYS[dt.weekday()]}）"


def fmt_md_hm(dt: datetime) -> str:
    """10/30 20:00"""
    return f"{dt.month}/{dt.day} {dt:%H:%M}"


def fmt_duration(start: datetime, end: datetime) -> str:
    minutes = int((end - start).total_seconds() // 60)
    h, m = divmod(minutes, 60)
    if m == 0:
        return f"{h}時間"
    if h == 0:
        return f"{m}分"
    return f"{h}時間{m}分"


def fmt_hm(dt: datetime, base: datetime) -> str:
    """base と別の日なら「翌」「翌々」を付ける（例: 翌0:00）"""
    days = (dt.date() - base.date()).days
    prefix = {0: "", 1: "翌", 2: "翌々"}.get(days, f"{dt.month}/{dt.day} ")
    return f"{prefix}{dt.hour}:{dt:%M}"


def fmt_range(start: datetime, end: datetime) -> str:
    """20:00〜23:00 / 21:00〜翌0:00"""
    return f"{start.hour}:{start:%M}〜{fmt_hm(end, start)}"


# ---------- 金額 ----------

_YEN = re.compile(r"[￥¥]\s*([\d,]+)|([\d,]+)\s*円")


def parse_yen(text: str | None) -> int | None:
    if not text:
        return None
    m = _YEN.search(text)
    if not m:
        return None
    return int((m.group(1) or m.group(2)).replace(",", ""))


def fmt_yen(v: int | None) -> str:
    return "—" if v is None else f"¥{v:,}"


# ---------- 氏名 ----------

_HONORIFIC = re.compile(r"\s*(様|さま|さん|殿)\s*$")


def strip_honorific(name: str) -> str:
    return _HONORIFIC.sub("", name.strip())


def normalize_name(name: str) -> str:
    """リピート判定用: NFKC、敬称・空白除去、小文字化"""
    s = unicodedata.normalize("NFKC", strip_honorific(name))
    return re.sub(r"\s+", "", s).lower()


def surname(name: str) -> str | None:
    """宛名用の名字（最初の空白より前）。分からなければ None。"""
    s = strip_honorific(unicodedata.normalize("NFKC", name))
    parts = s.split()
    if len(parts) < 2:
        return None
    first = parts[0]
    if first.isascii():
        # 英字表記は姓名の順が分からない。イニシャルも不可
        return None
    return first


def salutation(name: str | None) -> str:
    sn = surname(name) if name else None
    return f"{sn}さま\n\n" if sn else ""


# ---------- 連絡先の伏せ字 ----------

# 区切り（ハイフン類・空白・括弧・ドット。全角も）。文字列は正規化せずにそのまま照合する
_D = "[0-9０-９]"
_SEP = r"[\s\-‐‑‒–—―−ー－()（）.．]{0,2}"
_PHONE = re.compile(rf"(?<![0-9０-９.．])(?:[+＋]?[8８][1１]{_SEP}|[0０]){_D}{{1,4}}{_SEP}{_D}{{1,4}}{_SEP}{_D}{{3,4}}(?![0-9０-９])")
_EMAIL = re.compile(r"[\w.+\-．＋－]+\s?[@＠]\s?[\w\-－]+(?:[.．][\w\-－]+)+")


def mask_contacts(text: str) -> str:
    """メールアドレスと電話番号を伏せる（全角・空白区切り・括弧付きも）"""
    text = _EMAIL.sub("［メールアドレス］", text or "")
    return _PHONE.sub("［電話番号］", text)


# ---------- Slack の mrkdwn ----------

def mrkdwn_escape(text: str | None) -> str:
    """ゲストが書いた文字列を Slack の mrkdwn に入れる前に、制御文字（& < >）を実体参照にする。
    <!channel> の一斉メンションや <https://…|偽の表示> のリンクとして解釈されないようにする"""
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ---------- Slack の書式 → 送信用の平文 ----------

def slack_to_plain(text: str) -> tuple[str, bool]:
    """Slack のメッセージ本文をゲストに送る平文に戻す。(平文, コードブロックで囲まれていたか)
    - 全体を囲む ``` を外す
    - <https://…|表示> → 表示が URL そのもの（スキーム無し）なら URL、違えば「表示 URL」。<mailto:…|表示> → 表示
    - <@U…>・<!here> などのメンションは除き、<#C…|名前> → #名前
    - :bow: などの絵文字のショートコードは取り除く（ゲストへの文面は絵文字なし）
    - *太字* の記号を外す
    - &amp; &lt; &gt; を元の文字に戻す
    """
    t = (text or "").strip()
    fenced = False
    m = re.fullmatch(r"```\s*\n?(.*?)\n?\s*```", t, re.S)
    if m:
        t, fenced = m.group(1).strip("\n"), True

    def link(mm: re.Match) -> str:
        url, label = mm.group(1), mm.group(2)
        if url.startswith(("mailto:", "tel:")):
            return label
        bare = re.sub(r"^https?://", "", url).rstrip("/")
        return url if label.rstrip("/") in (url.rstrip("/"), bare) else f"{label} {url}"

    t = re.sub(r"<((?:https?|mailto|tel):[^|>]+)\|([^>]+)>", link, t)
    t = re.sub(r"<((?:https?|tel):[^>]+)>", r"\1", t)
    t = re.sub(r"<mailto:([^>]+)>", r"\1", t)
    t = re.sub(r"<@[UW][A-Z0-9]+(?:\|[^>]+)?>\s*", "", t)
    t = re.sub(r"<!(?:here|channel|everyone)(?:\|[^>]+)?>\s*", "", t)
    t = re.sub(r"<!subteam\^[A-Z0-9]+(?:\|[^>]+)?>\s*", "", t)
    t = re.sub(r"<#C[A-Z0-9]+\|([^>]+)>", r"#\1", t)
    # :bow: :blush: などのショートコード（英字を含むものだけ。10:30:00 のような時刻は残す）
    t = re.sub(r":(?=[a-z0-9_+\-]*[a-z])[a-z0-9_+\-]+(?:::skin-tone-\d)?:", "", t)
    t = re.sub(r"\*([^*\n]+)\*", r"\1", t)
    t = t.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
    t = re.sub(r"[ \t]+\n", "\n", t)
    return t.strip(), fenced
