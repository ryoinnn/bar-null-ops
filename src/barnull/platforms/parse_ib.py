"""インスタベースの画面から取り出した生データ（selectors.IB_*_JS の戻り値）を解釈する。"""

from __future__ import annotations

import re

from ..models import Detail, ListingRow, Message, Platform
from ..textutil import parse_ib_datetime, parse_ib_range, parse_slash_datetime, parse_yen, strip_honorific

_SLASH_DT_LINE = re.compile(r"^\d{4}/\d{2}/\d{2} \d{1,2}:\d{2}")
_NOISE = {"既読", "未読", "最新メッセージ", "送信を取り消す"}  # 画面のボタン・表示の文字（発言の本文ではない）


def parse_bookings(rows: list[dict]) -> list[ListingRow]:
    out = []
    for r in rows:
        pid = (r.get("id") or "").strip()
        if not pid.isdigit():
            continue
        out.append(ListingRow(
            platform=Platform.IB, platform_id=pid, status=r.get("status", ""),
            datetime_text=r.get("datetime", ""), guest_name=strip_honorific(r.get("guest") or "") or None,
            guest_id=r.get("guest_id"), price_text=r.get("price", ""),
        ))
    return out


def _row_map(rows: list[list[str]]) -> dict[str, str]:
    m: dict[str, str] = {}
    for cells in rows:
        if len(cells) == 2 and cells[0] and cells[0] not in m:
            m[cells[0]] = cells[1]
    return m


def parse_detail(raw: dict, pid: str) -> Detail:
    text = raw.get("text", "")
    rows = raw.get("rows", [])
    rm = _row_map(rows)
    d = Detail(platform=Platform.IB, platform_id=pid, raw_status_text=text[:3000])

    # 利用日時・予約状況（見出し行の次の行）
    for i, cells in enumerate(rows):
        if cells[:2] == ["利用日時", "予約状況"] and i + 1 < len(rows):
            data = rows[i + 1]
            rng = parse_ib_range(data[0]) if data else None
            if rng:
                d.start_at, d.end_at = rng
            if len(data) > 1:
                d.status = data[1].split("\n")[0].strip()
            break
    if d.start_at is None:
        rng = parse_ib_range(text)
        if rng:
            d.start_at, d.end_at = rng
    if not d.status:
        for word in ("予約確定", "予約リクエスト中", "利用者キャンセル", "特別キャンセル", "予約リクエストキャンセル",
                     "予約リクエスト非承認", "予約未確定"):
            if word in text:
                d.status = word
                break

    for label, attr in (("準備日時", "prep_start_at"), ("準備時間", "prep_start_at")):
        if label in rm and parse_ib_datetime(rm[label]):
            setattr(d, attr, parse_ib_datetime(rm[label]))
    for label in ("清掃日時", "清掃時間"):
        if label in rm:
            r2 = parse_ib_range(rm[label])
            if r2:
                d.cleanup_end_at = r2[1]

    if "氏名" in rm:
        d.guest_name = strip_honorific(rm["氏名"].split("\n")[0])
    d.purpose = rm.get("用途") or None
    d.purpose_detail = (rm.get("用途詳細") or "").strip() or None
    d.people = rm.get("利用人数") or None
    m = re.search(r"料金プラン\s*\n\s*(.+)", text)
    d.plan = m.group(1).strip() if m else None
    m = re.search(r"(?:無料設備|有料オプション|オプション)\s*\n((?:.*×\s*\d+\s*個.*\n?)+)", text)
    if m:
        d.options = "、".join(x.strip() for x in m.group(1).splitlines() if x.strip())
    m = re.search(r"合計\s*\n?\s*([\d,]+)円\s*\(税込\)", text)
    d.guest_total = parse_yen(m.group(1) + "円") if m else None
    m = re.search(r"振込額合計\s*\(税込\)\s*\n?\s*([\d,]+)円", text)
    d.host_revenue = parse_yen(m.group(1) + "円") if m else None
    d.guest_id = raw.get("guest_id")
    d.thread_id = raw.get("thread_id")
    if "申込日時" in rm:
        d.applied_at = parse_ib_datetime(rm["申込日時"])
    return d


def parse_thread(raw: dict) -> tuple[list[Message], str | None, list[str]]:
    """(古い順のメッセージ, ゲストID, 右側の予約一覧のご注文番号) を返す"""
    msgs: list[Message] = []
    for it in reversed(raw.get("items", [])):  # 画面は新しい順
        lines = [ln for ln in it.get("text", "").splitlines()]
        at = None
        body: list[str] = []
        for ln in lines:
            s = ln.strip()
            if at is None and _SLASH_DT_LINE.match(s):
                at = parse_slash_datetime(s)
                continue
            if s in _NOISE:
                continue
            body.append(ln.rstrip())
        text = "\n".join(body).strip()
        files = it.get("files") or []
        if it.get("host"):
            role = "host"
            if files and text in files:
                text = ""
        elif text.startswith("【自動配信メッセージ】"):
            role = "system"
        else:
            role = "guest"
        if at is None:
            continue  # 日時の無い部品はメッセージではない（ダイアログのボタン等）
        msgs.append(Message(role=role, text=text, files=files, at=at))
    for i, m in enumerate(msgs):
        m.index = i
    gm = re.search(r"\(ID:\s*(\d+)\)", raw.get("title", ""))
    orders = re.findall(r"ご注文番号[:：]\s*(\d+)", raw.get("text", ""))
    return msgs, (gm.group(1) if gm else None), orders


def is_change_request(text: str) -> bool:
    return text.startswith("予約内容の変更を希望します")


def is_change_approved(text: str) -> bool:
    """【自動配信メッセージ】送信した予約内容の変更リクエストが承認され、予約内容が変更されました"""
    return "変更リクエストが承認され" in text
