"""Slack の文面（Block Kit）。文面の見本は設計書 §5。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ..models import Kind, Platform, ResState
from ..textutil import fmt_date, fmt_duration, fmt_md_hm, fmt_range, fmt_yen, from_iso, mask_contacts
from ..textutil import mrkdwn_escape as esc

COLORS = {Platform.SM: "#1E6FD9", Platform.IB: "#F28C28"}
COLOR_DRAFT = "#2EB67D"        # 通常の文案（緑）
COLOR_NEEDS_CHECK = "#E01E5A"  # 要確認のある文案（赤）
TEST_PREFIX = ""  # 模擬モードでは「🧪【テスト】」
ICONS = {Platform.SM: "🟦", Platform.IB: "🟧"}

STATUS_LABEL = {
    ResState.NOTIFIED: "🟡 承認待ち",
    ResState.ON_HOLD: "⏸ 保留中",
    ResState.EXECUTING: "⏳ 実行中",
    ResState.CONFIRMED: "✅ 送信済み",
    ResState.COMPLETING: "⏳ 完了処理中",
    ResState.REVIEW_CONFIRM: "⚠️ 要対応（退室時の状態を確認）",
    ResState.ERROR: "⚠️ 要対応（自動処理に失敗）",
    ResState.REVIEW_ERROR: "⚠️ 要対応（レビューの投稿に失敗）",
    ResState.CLOSED: "🏁 対応完了",
    ResState.MANUAL: "🟡 承認待ち（承認制リクエスト）",
}


SECTION_MAX = 2900  # Slack の section の上限は 3000 字（超えると投稿そのものが失敗する）


def _md(text: str) -> dict[str, Any]:
    if len(text) > SECTION_MAX:
        text = text[:SECTION_MAX - 20] + "\n…（長いため省略）"
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}


def _code_sections(title: str, text: str) -> list[dict[str, Any]]:
    """長い文面を、途中で切らずに複数の section（コードブロック）に分ける（文案は全文が見えないと承認できない）"""
    body = esc(text).replace("```", "ʼʼʼ")  # 文面中の ``` でコードブロックが崩れないように
    size = SECTION_MAX - len(title) - 40
    chunks = [body[i:i + size] for i in range(0, len(body), size)] or [""]
    out = []
    for n, c in enumerate(chunks):
        head = title if n == 0 else f"（続き {n + 1}/{len(chunks)}）"
        out.append({"type": "section", "text": {"type": "mrkdwn", "text": f"{head}\n```{c}```"}})
    return out


def _ctx(text: str) -> dict[str, Any]:
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": text}]}


def quote(text: str) -> str:
    """ゲストが書いた文字列の引用（mrkdwn の制御文字は無害化する）"""
    return "\n".join("> " + esc(line) for line in (text or "").splitlines() or [""])


def mention(users: list[str] | tuple[str, ...]) -> str:
    return " ".join(f"<@{u}>" for u in users)


def headline(r: dict) -> str:
    return TEST_PREFIX + _headline(r)


def _headline(r: dict) -> str:
    p = Platform(r["platform"])
    if r["kind"] == Kind.INQUIRY_ONLY:
        return f"{ICONS[p]} {p.label}｜【問い合わせ】"
    if p is Platform.SM:
        return f"{ICONS[p]} {p.label}｜新規予約リクエスト"
    if r.get("was_request") or r["state"] == ResState.MANUAL:
        return f"{ICONS[p]} {p.label}｜予約リクエスト（承認制）"
    st = (r.get("platform_status") or "").strip()
    if st and st != "予約確定" and r["state"] != ResState.CLOSED:
        return f"{ICONS[p]} {p.label}｜新規予約（{esc(st)}）"
    return f"{ICONS[p]} {p.label}｜新規予約（確定済み）"


def time_band(r: dict) -> str:
    start, end = from_iso(r["start_at"]), from_iso(r["end_at"])
    if not start or not end:
        return "—"
    s = f"{fmt_range(start, end)}（{fmt_duration(start, end)}）"
    prep, clean = from_iso(r["prep_start_at"]), from_iso(r["cleanup_end_at"])
    extra = []
    if prep and prep < start:
        extra.append(f"準備 {prep.hour}:{prep:%M}〜")
    if clean and clean > end:
        extra.append(f"清掃 〜{clean.hour}:{clean:%M}")
    if extra:
        s += "　" + "／".join(extra)
    return s


def extract_checks(draft: str | None) -> list[str]:
    """文案の【要確認: …】の中身を取り出す"""
    import re

    items = [x.strip() for x in re.findall(r"【要確認[:：]?\s*(.*?)】", draft or "", re.S)]
    return [x or "（内容の記載なし）" for x in items]


def status_line(r: dict, open_drafts: int, closed_label: str | None = None, needs_check: int = 0,
                approved_waiting: int = 0) -> str:
    """open_drafts: 未承認の文案の数、approved_waiting: 承認済みで定型文の送信後に送る返信の数"""
    st = ResState(r["state"])
    label = closed_label if (st is ResState.CLOSED and closed_label) else STATUS_LABEL[st]
    if open_drafts and st in (ResState.CONFIRMED, ResState.CLOSED, ResState.NOTIFIED, ResState.ON_HOLD):
        extra = f"、うち⚠️要確認 {needs_check}件" if needs_check else ""
        label += f"　💬 返信待ちあり（{open_drafts}件{extra}）"
    if approved_waiting:
        label += f"　👌 承認済みの返信 {approved_waiting}件（定型文の送信後に送ります）"
    return label


def parent_message(r: dict, repeat_label: str, open_drafts: int = 0, closed_label: str | None = None,
                   needs_check: int = 0, approved_waiting: int = 0) -> dict[str, Any]:
    """親メッセージ（予約・問い合わせ通知）。r は reservations の行（dict）"""
    p = Platform(r["platform"])
    start = from_iso(r["start_at"])
    lines = []
    if r["kind"] == Kind.INQUIRY_ONLY:
        lines.append(f"お問い合わせ者：{esc(r['guest_name']) or '—'} 様（{repeat_label}）")
        if start:
            lines.append(f"希望日時　：{fmt_date(start)} {time_band(r)}")
    else:
        lines += [
            f"日付　　　：{fmt_date(start) if start else '—'}",
            f"時間帯　　：{time_band(r)}",
            f"予約者　　：{esc(r['guest_name']) or '—'} 様（{repeat_label}）",
            f"ゲスト支払：{fmt_yen(r['guest_total'])}（税込）",
            f"店舗収益　：{fmt_yen(r['host_revenue'])}{'（概算）' if r['revenue_is_estimate'] else ''}",
        ]
        pu = "／".join(esc(x) for x in (r["people"], r["purpose"]) if x)
        if pu:
            lines.append(f"人数・用途：{pu}")
        if r["options"]:
            lines.append(f"オプション：{esc(r['options'])}")
    if r["purpose_detail"]:
        lines.append(f"用途詳細　：{esc(mask_contacts(r['purpose_detail'])[:300])}")
    lines.append(f"予約ID：{r['platform_id']}｜<{r['dashboard_url']}|管理画面を開く>")
    body = "\n".join(lines)

    st = ResState(r["state"])
    guide = []
    if st in (ResState.NOTIFIED, ResState.ON_HOLD, ResState.MANUAL) and r["kind"] == Kind.RESERVATION:
        approve = p is Platform.SM or "リクエスト" in (r.get("platform_status") or "")
        action = "承認して定型文＋マニュアルを送信" if approve else "定型文＋マニュアルを送信"
        guide.append(f"✅ このメッセージにスタンプ → {action}")
        guide.append("⏸ 保留する場合 → スレッドに「待って」等と返信")
    blocks = [
        _md(f"*{headline(r)}*"),
        _md(body),
        {"type": "divider"},
        _md(f"*ステータス：{status_line(r, open_drafts, closed_label, needs_check, approved_waiting)}*"),
    ]
    if guide:
        blocks.append(_ctx("\n".join(guide)))
    return {
        "text": f"{headline(r)} {esc(r['guest_name'])} {fmt_date(start) if start else ''}",
        "attachments": [{"color": COLORS[p], "blocks": blocks}],
    }


def draft_message(guest_text: str, received_at: datetime | None, draft: str, needs_check: bool,
                  reason_note: str | None = None, checks: list[str] | None = None) -> dict[str, Any]:
    """返信文案。【要確認】がある場合は赤帯・見出し・確認項目の一覧で目立たせる"""
    when = f"（{received_at:%m/%d %H:%M} 受信）" if received_at else ""
    guest = _md(f"💬 *ゲストからのメッセージ*{when}\n{quote(mask_contacts(guest_text)[:2500])}")
    body = _code_sections("✍️ *返信文案*", draft)
    notes = [_ctx(reason_note)] if reason_note else []
    if needs_check:
        items = checks if checks else extract_checks(draft)
        blocks = [
            {"type": "header", "text": {"type": "plain_text", "text": "⚠️ 要確認あり｜このままでは送信できません"}},
            _md(f"返信文案に、送る前に確認が必要な箇所が *{len(items)}件* あります。\n"
                + "\n".join(f"• {esc(x)}" for x in items)),
            *notes,
            {"type": "divider"},
            guest,
            *body,
            _ctx("🚫 この文案にスタンプを押しても送信されません（誤送信防止）\n"
                 "✏️ 確認した内容で、修正後の全文をこのスレッドに返信 → 確認のあと、その文面で送信します"),
        ]
        return {"text": f"⚠️【要確認】ゲストへの返信文案に確認が必要です（{len(items)}件）",
                "attachments": [{"color": COLOR_NEEDS_CHECK, "blocks": blocks}]}
    blocks = [
        *notes,
        guest,
        *body,
        _ctx("✅ スタンプ → この文案で送信\n✏️ 修正 → このスレッドに修正後の全文を返信（確認のあと、その文面で送信します）"),
    ]
    return {"text": "💬 ゲストからのメッセージと返信文案", "attachments": [{"color": COLOR_DRAFT, "blocks": blocks}]}


def needs_check_refused(user: str, draft: str, checks: list[str] | None = None) -> str:
    items = checks if checks else extract_checks(draft)
    return (f"🚫 <@{user}> この文案には【要確認】が{len(items)}件残っているため、スタンプでは送信しません（誤送信防止）。\n"
            "確認が必要な箇所：\n" + "\n".join(f"• {esc(x)}" for x in items)
            + "\n確認した内容で、修正後の全文をこのスレッドに返信してください。確認のあと、その文面で送信します。")


def no_draft_message(guest_text: str, received_at: datetime | None, reason: str) -> dict[str, Any]:
    when = f"（{received_at:%m/%d %H:%M} 受信）" if received_at else ""
    return {
        "text": "💬 ゲストからのメッセージ（文案なし）",
        "blocks": [
            _md(f"💬 *ゲストからのメッセージ*{when}\n{quote(mask_contacts(guest_text)[:2500])}"),
            _md(f"⚠️ 文案を作れませんでした（{esc(reason)}）。返信文をこのスレッドに書けば、確認のあと、その文面で送信します"),
        ],
    }


def reminder(r: dict, hours: float, what: str) -> str:
    start = from_iso(r["start_at"])
    when = f"（{fmt_md_hm(start)}〜）" if start else ""
    return (f"⏰ リマインド（{what}から{_hours(hours)}経過）\n"
            f"{esc(r['guest_name']) or 'ゲスト'} 様の予約{when}が{what}です。\n"
            f"✅ このメッセージにスタンプ → 承認して送信")


def draft_reminder(hours: float, n: int) -> str:
    return (f"⏰ リマインド（未送信の返信文案 {n}件、{_hours(hours)}経過）\n"
            f"文案にスタンプ → そのまま送信／修正後の全文を返信 → その文面で送信")


def _hours(h: float) -> str:
    if h < 1:
        return f"{int(h * 60)}分"
    return f"{h:.0f}時間" if abs(h - round(h)) < 0.05 else f"{h:.1f}時間"


def completion(r: dict, exit_status: str | None, stars: int | None, comment: str | None) -> str:
    start, end = from_iso(r["start_at"]), from_iso(r["end_at"])
    when = f"（{fmt_md_hm(start)}〜{fmt_range(start, end).split('〜')[1]}）" if start and end else ""
    lines = [f"🏁 利用完了：{esc(r['guest_name']) or 'ゲスト'} 様{when}"]
    if exit_status:
        lines.append(f"退室時状態：{esc(exit_status)}")
    if stars:
        lines.append(f"⭐ レビュー投稿済み：★{stars}「{esc(comment)}」")
    lines.append("本予約の対応を完了しました。")
    return "\n".join(lines)
