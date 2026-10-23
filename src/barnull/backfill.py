"""過去データの取り込み（常駐プロセスを止めてから実行）。

- guest_history: リピート回数の判定用（SM 受信トレイ、IB 予約一覧）
- knowledge/past_replies: 過去のホストの返信（文案の言い回しと回答内容の拠り所）。ゲスト名は伏せる
Slack には投稿しない。新しい予約の取り込み（seed）は常駐プロセスの初回ポーリングで行う。
"""

from __future__ import annotations

import logging
import re

from . import secrets
from .config import ROOT, Config
from .db import DB
from .models import Message
from .platforms.browser import BrowserPool
from .platforms.ib import Instabase
from .platforms.sm import SpaceMarket
from .textutil import iso, mask_contacts, normalize_name, parse_ib_range, parse_sm_range, strip_honorific

log = logging.getLogger(__name__)
OUT = ROOT / "knowledge" / "past_replies"


def _mask_names(text: str, names: list[str]) -> str:
    for n in sorted({x for x in names if x}, key=len, reverse=True):
        for part in [n, *re.split(r"[\s　]+", n)]:
            if len(part) >= 2:
                text = text.replace(part, "◯◯")
    return mask_contacts(text)


def _pairs(messages: list[Message], marker: str, code: str | None) -> list[tuple[str, str]]:
    """（直前のゲスト発言, ホストの返信）の組。定型文と添付のみは除く"""
    out = []
    last_guest = ""
    for m in messages:
        if m.role == "guest":
            last_guest = m.text
        elif m.role == "host" and m.text and marker not in m.text:
            t = m.text.replace(code, "［暗証番号］") if code else m.text
            out.append((last_guest, t))
    return out


def _write(name: str, label: str, pairs: list[tuple[str, str]], names: list[str]) -> None:
    if not pairs:
        return
    OUT.mkdir(parents=True, exist_ok=True)
    body = [f"# {label}"]
    for g, h in pairs:
        body.append(f"ゲスト: {_mask_names(g, names)[:600]}\nホスト: {_mask_names(h, names)}")
    (OUT / f"{name}.md").write_text("\n\n".join(body) + "\n", encoding="utf-8")


async def backfill(cfg: Config, announce_unsent: bool = False) -> None:
    db = DB(cfg.db_path)
    code = secrets.get_optional("KEYBOX_CODE")
    pool = BrowserPool(cfg, headless=True)
    sm, ib = SpaceMarket(cfg, pool), Instabase(cfg, pool)
    n_hist = n_files = 0
    try:
        # ---- SM ----
        rows = await sm.list_inbox()
        for row in rows:
            rng = parse_sm_range(row.datetime_text)
            if row.guest_name and rng:
                cancelled = any(w in row.status for w in ("キャンセル", "否認", "期限切れ"))
                db.run("INSERT OR IGNORE INTO guest_history (platform, platform_id, guest_key, start_at, cancelled) "
                       "VALUES (?, ?, ?, ?, ?)", "sm", row.platform_id, f"sm:name:{normalize_name(row.guest_name)}",
                       iso(rng[0]), int(cancelled))
                n_hist += 1
            try:
                d = await sm.get_detail(row.platform_id)
            except Exception as e:  # noqa: BLE001
                log.warning("SM %s: %s", row.platform_id, e)
                continue
            pairs = _pairs(d.messages, cfg.sent_marker, code)
            _write(f"sm_{row.platform_id}", f"スペースマーケット（{row.status}）", pairs, [row.guest_name or "", d.guest_name or ""])
            n_files += bool(pairs)
        # ---- IB ----
        rows = await ib.list_bookings(sort_by_booked=False)
        for row in rows:
            rng = parse_ib_range(row.datetime_text)
            key = f"ib:id:{row.guest_id}" if row.guest_id else (
                f"ib:name:{normalize_name(row.guest_name)}" if row.guest_name else None)
            if key:
                cancelled = "キャンセル" in row.status
                db.run("INSERT OR IGNORE INTO guest_history (platform, platform_id, guest_key, start_at, cancelled) "
                       "VALUES (?, ?, ?, ?, ?)", "ib", row.platform_id, key, iso(rng[0]) if rng else None, int(cancelled))
                n_hist += 1
        tids, _ = await ib.unreplied_threads()
        seen: set[str] = set()
        for row in rows:
            try:
                d = await ib.get_detail(row.platform_id)
            except Exception as e:  # noqa: BLE001
                log.warning("IB %s: %s", row.platform_id, e)
                continue
            if not d.thread_id or d.thread_id in seen:
                continue
            seen.add(d.thread_id)
            pairs = _pairs(d.messages, cfg.sent_marker, code)
            _write(f"ib_{d.thread_id}", "インスタベース", pairs, [strip_honorific(row.guest_name or ""), d.guest_name or ""])
            n_files += bool(pairs)
    finally:
        await pool.stop()
    print(f"ゲスト履歴 {n_hist} 件、過去の返信ファイル {n_files} 件を取り込みました（{OUT}）")
    if announce_unsent:
        print("--announce-unsent は常駐プロセスの初回ポーリングで扱います（未送信の今後の予約は自動で親メッセージを作ります）")
