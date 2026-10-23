"""スペースマーケットの画面から取り出した生データ（selectors.SM_*_JS の戻り値）を解釈する。"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta

from ..models import Detail, Kind, ListingRow, Message, Platform
from ..textutil import JST, now, parse_sm_range, parse_yen

_TIME_LINE = re.compile(r"^\d{1,2}:\d{2}$")
_NOISE_LINES = {"既読", "未読"}
SERVICE_TYPES = ("時間貸し", "1日貸し", "宿泊", "月額貸し")


def parse_inbox(rows: list[dict]) -> list[ListingRow]:
    out = []
    for r in rows:
        if not r.get("id", "").isdigit():
            continue
        out.append(ListingRow(
            platform=Platform.SM, platform_id=r["id"], status=r.get("status", ""),
            substatus=r.get("substatus", ""), datetime_text=r.get("datetime", ""),
            guest_name=(r.get("guest") or "").strip() or None, price_text=r.get("price", ""),
        ))
    return out


def _lines(text: str) -> list[str]:
    return [ln.strip() for ln in (text or "").splitlines() if ln.strip()]


def _after(lines: list[str], label: str, skip: int = 0) -> str | None:
    for i, ln in enumerate(lines):
        if ln == label and i + 1 + skip < len(lines):
            return lines[i + 1 + skip]
    return None


def _clean_message(text: str) -> str:
    out = []
    for ln in (text or "").splitlines():
        s = ln.strip()
        if _TIME_LINE.match(s) or s in _NOISE_LINES:
            continue
        out.append(ln.rstrip())
    return "\n".join(out).strip()


_MD = re.compile(r"(?:(\d{4})年)?(\d{1,2})月(\d{1,2})日")
_WEEKDAY = {"月": 0, "火": 1, "水": 2, "木": 3, "金": 4, "土": 5, "日": 6}


def message_datetime(date_text: str | None, time_text: str | None, ref: datetime | None = None) -> datetime | None:
    """スレッドの日付見出し（10月4日／昨日／今日／金曜日）と時刻（1:01）から日時を作る"""
    if not date_text or not time_text:
        return None
    ref = ref or now()
    m = re.match(r"(\d{1,2}):(\d{2})$", time_text.strip())
    if not m:
        return None
    hh, mm = int(m.group(1)), int(m.group(2))
    t = date_text.strip()
    d: date | None = None
    if t.startswith("今日"):
        d = ref.date()
    elif t.startswith("昨日"):
        d = (ref - timedelta(days=1)).date()
    elif (md := _MD.search(t)):
        y = int(md.group(1)) if md.group(1) else ref.year
        d = date(y, int(md.group(2)), int(md.group(3)))
        if not md.group(1) and d > ref.date() + timedelta(days=1):
            d = d.replace(year=y - 1)
    elif (wd := re.match(r"([月火水木金土日])曜", t)):
        back = (ref.weekday() - _WEEKDAY[wd.group(1)]) % 7 or 7
        d = (ref - timedelta(days=back)).date()
    if d is None:
        return None
    return datetime(d.year, d.month, d.day, hh, mm, tzinfo=JST)


def parse_status(sidebar: str) -> str:
    lines = _lines(sidebar)
    for i, ln in enumerate(lines):
        if ln == "現在のステータス":
            for nxt in lines[i + 1:i + 4]:
                if nxt not in SERVICE_TYPES:
                    return nxt
    return ""


def parse_thread(raw: dict, pid: str, ref: datetime | None = None) -> Detail:
    sidebar, main = raw.get("sidebar", ""), raw.get("main", "")
    lines = _lines(sidebar)
    fields: dict = raw.get("fields") or {}
    fees: list = raw.get("fees") or []
    labels: list = raw.get("labels") or []
    d = Detail(platform=Platform.SM, platform_id=pid, raw_status_text=(sidebar + "\n" + "\n".join(labels))[:3000])
    # 部品単位で読めた値を優先し、無ければ画面の文字列から読む
    d.status = (raw.get("status") or "").strip() or parse_status(sidebar)

    rng = parse_sm_range(fields.get("日時") or "") or parse_sm_range(sidebar)
    if rng:
        d.start_at, d.end_at = rng
    d.host_revenue = parse_yen(fields.get("あなたの収益")) or parse_yen(_after(lines, "あなたの収益"))
    fee_map = {h: b for h, b in fees}
    d.guest_total = parse_yen(fee_map.get("合計")) or parse_yen(_after(lines, "合計"))
    if fees:
        d.plan = fees[0][0]
    else:
        for i, ln in enumerate(lines):
            if ln == "ゲストのご利用料金明細" and i + 1 < len(lines):
                d.plan = lines[i + 1]
                break
    name = (raw.get("guest") or "").strip() or _after(lines, "ゲスト詳細")
    if name and not name.startswith("予約ID"):
        d.guest_name = name
    when = fields.get("日時", None)
    # 「お問い合わせ」のステータスは、希望日時が書かれていても予約ではない（スタンプで暗証番号を送らないように）
    if ("お問い合わせ" in d.status or "日時指定なし" in sidebar
            or (d.start_at is None and (when == "" or "お問い合わせ" in sidebar))):
        d.kind = Kind.INQUIRY_ONLY

    buttons = raw.get("buttons", [])
    d.can_approve = "承認" in buttons and "否認" in buttons
    # レビューを投稿済みかは画面の文字では判定しない（attach_api で予約情報の API から判定する）。
    # サイドバーの「ホストレビューあり」はゲストのバッジ（他のホストからレビューを受けたことがある）で、
    # 「レビューの投稿」ボタンは投稿後も公開まで（編集用に）残るため
    d.change_request = "ゲストから予約変更リクエストあり" in sidebar

    # 予約リクエストの内容（画面の「予約リクエスト」の枠 → 無ければ [利用目的] パーティー 等の文字列）
    outline = {k: v for k, v in (raw.get("outline") or []) if k}

    def tag(label: str) -> str | None:
        if outline.get(label):
            return outline[label]
        m = re.search(r"\[" + label + r"\]\s*([^\[\n]+)", main)
        return m.group(1).strip() if m else None
    d.purpose = tag("利用目的")
    d.people = tag("人数")
    detail = tag("利用用途の詳細")
    if detail:
        d.purpose_detail = detail
    if not d.plan:
        d.plan = tag("プラン")

    # 退室時状態（最後の記録を使う）
    rows = raw.get("exit_rows") or []
    if rows:
        ex = {}
        for k, v in rows:
            if k == "状態" and ex.get("状態"):
                ex = {}  # 2件目以降の記録
            ex[k] = v
        d.exit_status = ex.get("状態") or None
        if ex.get("種類") or ex.get("詳細"):
            d.exit_detail = "：".join(x for x in (ex.get("種類"), ex.get("詳細")) if x)
    else:
        m = re.search(r"ゲストが退室時の状態を記録しました\s*状態\s*(\S+)", main)
        if m:
            d.exit_status = m.group(1).strip()
            dm = re.search(r"ゲストが退室時の状態を記録しました.*?種類\s*(.+?)\s*詳細\s*(.+?)(?:\n\s*写真|\Z)", main, re.S)
            if dm:
                d.exit_detail = f"{dm.group(1).strip()}：{dm.group(2).strip()}"

    # メッセージ（古い順）
    msgs: list[Message] = []
    for it in raw.get("items", []):
        kind = it.get("kind")
        text = _clean_message(it.get("text", ""))
        files = it.get("files") or []
        at = message_datetime(it.get("date"), it.get("time"), ref)
        if kind in ("system", "exit"):
            msgs.append(Message(role="system", text=text, at=at))
        elif kind in ("host", "file_host"):
            if kind == "file_host" and not files and text:
                files = [text]
            msgs.append(Message(role="host", text="" if kind == "file_host" else text, files=files, at=at))
        elif kind in ("guest", "file_guest"):
            body = text if kind == "guest" else f"（ファイル添付: {', '.join(files) or text}）"
            msgs.append(Message(role="guest", text=body, files=files, at=at))
    for i, m in enumerate(msgs):
        m.index = i
    d.messages = msgs
    return d


def attach_api(d: Detail, api_messages: list[dict], reputation_status: int | None = None,
               reservation_api: dict | None = None) -> None:
    """画面が受け取った API の応答を反映する。
    - 予約情報（GET …/owners/<host>/reservations/<ID>）: reputation_id_by_owner（ホストのレビューの ID。未投稿なら null）、
      can_be_reviewed（レビューを書ける状態か）、review_expires_at（投稿期限）
    - レビュー API（GET …/reservations/<ID>/reputation。レビュー画面が呼ぶ）: 200=投稿済み、404=未投稿
    - メッセージ API: 各発言に ID と正確な送信時刻を付ける"""
    if isinstance(reservation_api, dict) and "reputation_id_by_owner" in reservation_api:
        d.review_posted = reservation_api.get("reputation_id_by_owner") is not None
        if "can_be_reviewed" in reservation_api:
            d.review_open = bool(reservation_api.get("can_be_reviewed"))
        exp = reservation_api.get("review_expires_at")
        if exp:
            try:
                d.review_expires_at = datetime.fromisoformat(str(exp).replace("Z", "+00:00")).astimezone(JST)
            except ValueError:
                pass
    if reputation_status == 200:
        d.review_posted = True
    elif reputation_status == 404 and d.review_posted is None:
        d.review_posted = False
    uniq = {m.get("id"): m for m in api_messages if isinstance(m, dict) and m.get("id") is not None}
    seq = sorted((m for m in uniq.values() if m.get("timeline_type") == 0),
                 key=lambda m: (m.get("created_at") or "", m.get("id")))
    dom = [m for m in d.messages if m.role in ("host", "guest")]
    if not seq or len(seq) != len(dom):
        return
    if any((a.get("from_type") == "Owner") != (b.role == "host") for a, b in zip(seq, dom, strict=True)):
        return
    for a, b in zip(seq, dom, strict=True):
        b.uid = str(a["id"])
        b.at_dom = b.at
        try:
            at = datetime.fromisoformat(str(a.get("created_at")).replace("Z", "+00:00"))
            b.at = at.astimezone(JST)
        except (TypeError, ValueError):
            pass
