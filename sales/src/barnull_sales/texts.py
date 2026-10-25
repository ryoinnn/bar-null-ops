"""Slack と Asana に出す文面。

LINE や銀行明細から来た文字列（顧客名・備考・原文・名義など）は、Slack に出す前に esc() を通す
（`<!channel>` のような書式で、全員への通知やリンクの偽装をされないように）。
"""

from __future__ import annotations

import re
import unicodedata
from datetime import date

from . import bizdate
from .slips import Slip


def esc(text: str | None) -> str:
    """Slack の書式として解釈される文字（& < >）を無害にする。"""
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def norm_text(s: str | None) -> str:
    """銀行明細などの比較用（全角・半角をそろえ、空白を除く）。"""
    return re.sub(r"\s", "", unicodedata.normalize("NFKC", s or ""))


def norm_person(s: str | None) -> str:
    """顧客名の比較用（norm_text に加えて敬称を除く）。"""
    return re.sub(r"(様|さま|さん)", "", norm_text(s))


def who(name: str | None) -> str:
    """LINE の表示名など（Slack 用にエスケープ。無ければ「不明」）。"""
    return esc(name) or "不明"


def unpaid_rest(remaining: int) -> str:
    return "（未収はすべて回収）" if remaining <= 0 else f"（未収残高 {yen(remaining)}）"


def quote(text: str) -> str:
    return "\n".join("> " + esc(line) for line in text.splitlines()) or "> （本文なし）"


def yen(v: int | None) -> str:
    return "不明" if v is None else f"¥{v:,}"


def date_label(slip: Slip) -> str:
    if not slip.business_date:
        return "日付なし"
    return bizdate.label(date.fromisoformat(slip.business_date)) + " 営業分"


def slack_parent(slip: Slip) -> str:
    """伝票ごとの親メッセージ（状態が変わるたびに書き換える）。"""
    lines = []
    if slip.unpaid_amount:
        lines.append(f"【未収あり {yen(slip.unpaid_amount)}】")
    lines.append(f"伝票 {slip.slip_id}｜{date_label(slip)}")
    lines.append(f"顧客：{esc(slip.customer_name) or '不明'}様　金額：{yen(slip.slip_amount)}")
    if slip.note:
        lines.append(f"備考：{esc(slip.note)}")
    if slip.source == "pos":
        lines.append(f"担当：{who(slip.sender_display_name)}（POS {slip.pos.get('visit_id', '')}）")
        refs = pos_referrals(slip)
        if refs:
            lines.append(f"紹介元：{esc(refs)}")
        method = {"cash": "現金", "square": "Square", "unpaid": "未収"}
        receipts = "、".join(f"{method.get(r['method'], r['method'])} {yen(r['amount'])}" for r in slip.pos.get("receipts") or [])
        if receipts:
            lines.append(f"受け取り：{receipts}")
    else:
        lines.append(f"担当：{who(slip.sender_display_name)}（LINE）")
    lines.append(f"ステータス：{slip.status}" + ("（完了）" if slip.closed_at and not slip.cancelled else ""))
    if slip.payment_ids:
        fee = slip.square.get("fee")
        lines.append(f"Square：{yen(slip.square.get('amount'))}（{slip.square.get('summary', '')}）"
                     + (f" 手数料 {yen(fee)}" if fee else ""))
    if slip.asana.get("url"):
        lines.append(f"Asana：{slip.asana['url']}")
    if slip.dry_run:
        lines.append("（テスト）")
    return "\n".join(lines)


def pos_referrals(slip: Slip) -> str:
    """POS の伝票の紹介元（2次は「1次 › 2次」）。重複は1つにまとめる。"""
    out = []
    for r in slip.pos.get("referrals") or []:
        name = f"{r['tenant_name']} › {r['agent_name']}" if r.get("tenant_name") else r.get("agent_name", "")
        if name and name not in out:
            out.append(name)
    return "、".join(out)


def review(settings, title: str, sender: str | None, text: str) -> str:
    """伝票に結び付けられないメッセージを、チャンネルに出して人に見てもらう。"""
    return f"{settings.mention}【要確認】{title}\n送信者：{who(sender)}（LINE）\n{quote(text)}"


def asana_task_name(slip: Slip) -> str:
    d = date.fromisoformat(slip.business_date) if slip.business_date else None
    head = f"{d.month}/{d.day}" if d else "日付なし"
    name = f"{head} {slip.customer_name or '不明'}様 {yen(slip.slip_amount)}"
    if slip.unpaid_amount:
        name += f" ［未収 {yen(slip.unpaid_amount)}］"
    return name


def asana_notes(slip: Slip) -> str:
    return "\n".join([
        f"伝票ID: {slip.slip_id}",
        f"営業日: {slip.business_date or '日付なし'}（{slip.business_date_source}）",
        f"顧客名: {slip.customer_name or '不明'}",
        f"伝票金額: {yen(slip.slip_amount)}",
        f"未収: {yen(slip.unpaid_amount) if slip.unpaid_amount else 'なし'} {slip.unpaid_due_text}".rstrip(),
        f"Square で期待する金額: {yen(slip.expected_square_amount)}",
        f"担当（{'POS' if slip.source == 'pos' else 'LINE'}）: {slip.sender_display_name or '不明'}",
        "",
        "POS の伝票:" if slip.source == "pos" else "LINE の原文:",
        slip.raw_text,
        "",
        "処理履歴:",
        f"- {slip.created_at} 起票（{slip.status}）",
    ])
