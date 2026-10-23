"""SQLite（WAL）。状態はすべてここに持つ（プロセスが再起動しても続きから動く）。"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from .textutil import iso, now

SCHEMA = """
CREATE TABLE IF NOT EXISTS reservations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform TEXT NOT NULL,
    platform_id TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'reservation',
    guest_name TEXT,
    guest_key TEXT,
    start_at TEXT, end_at TEXT, prep_start_at TEXT, cleanup_end_at TEXT,
    people TEXT, purpose TEXT, purpose_detail TEXT, options TEXT, plan TEXT,
    guest_total INTEGER, host_revenue INTEGER, revenue_is_estimate INTEGER DEFAULT 0,
    platform_status TEXT,
    state TEXT NOT NULL,
    hold_since TEXT, next_remind_at TEXT,
    thread_id TEXT,
    dashboard_url TEXT,
    slack_ts TEXT,
    template_sent_at TEXT,
    exit_status TEXT, exit_detail TEXT,
    review_posted_at TEXT, review_comment TEXT,
    approved_by TEXT,
    deadline_warned TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    UNIQUE(platform, platform_id)
);
CREATE TABLE IF NOT EXISTS inquiries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    reservation_id INTEGER NOT NULL REFERENCES reservations(id),
    source_key TEXT NOT NULL UNIQUE,
    guest_text TEXT,
    received_at TEXT,
    draft_text TEXT,
    draft_ts TEXT,
    needs_check INTEGER DEFAULT 0,
    state TEXT NOT NULL,
    hold_since TEXT, next_remind_at TEXT,
    sent_text TEXT, sent_by TEXT, sent_at TEXT,
    wait_for_template INTEGER DEFAULT 0,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS slack_messages (
    ts TEXT PRIMARY KEY,
    channel TEXT NOT NULL,
    kind TEXT NOT NULL,
    reservation_id INTEGER,
    inquiry_id INTEGER,
    extra TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS guest_history (
    platform TEXT NOT NULL,
    platform_id TEXT NOT NULL,
    guest_key TEXT NOT NULL,
    start_at TEXT,
    cancelled INTEGER DEFAULT 0,
    PRIMARY KEY (platform, platform_id)
);
CREATE TABLE IF NOT EXISTS listing_snapshots (
    platform TEXT NOT NULL,
    platform_id TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    position INTEGER,
    seen_at TEXT NOT NULL,
    PRIMARY KEY (platform, platform_id)
);
CREATE TABLE IF NOT EXISTS thread_cursor (
    platform TEXT NOT NULL,
    thread_key TEXT NOT NULL,
    last_guest_key TEXT,
    PRIMARY KEY (platform, thread_key)
);
CREATE TABLE IF NOT EXISTS action_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    reservation_id INTEGER,
    inquiry_id INTEGER,
    trigger TEXT,
    actor TEXT,
    action TEXT NOT NULL,
    result TEXT,
    detail TEXT,
    screenshots TEXT
);
CREATE TABLE IF NOT EXISTS alerts (
    key TEXT PRIMARY KEY,
    last_sent_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


def _split_columns(line: str) -> list[str]:
    """「a TEXT, b TEXT DEFAULT 0,」のような1行を列ごとに分ける（括弧の中のカンマでは分けない）"""
    out, depth, cur = [], 0, ""
    for ch in line.strip():
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            out.append(cur.strip())
            cur = ""
        else:
            cur += ch
    if cur.strip():
        out.append(cur.strip())
    return [x for x in out if x]


class DB:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """SCHEMA にあって既存の表に無い列を追加する（版を上げて列を増やしても、古い DB のまま動くように）"""
        import re

        for m in re.finditer(r"CREATE TABLE IF NOT EXISTS (\w+) \((.*?)\n\);", SCHEMA, re.S):
            table, body = m.group(1), m.group(2)
            have = {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            for line in body.splitlines():
                for part in _split_columns(line):
                    words = part.split()
                    if not words or re.match(r"(UNIQUE|PRIMARY|CONSTRAINT|FOREIGN|CHECK)\b", part, re.I):
                        continue
                    name = words[0]
                    if name in have:
                        continue
                    ddl = part.replace("NOT NULL", "").replace("UNIQUE", "").replace("PRIMARY KEY", "")
                    ddl = ddl.replace("AUTOINCREMENT", "").strip()
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")
                    have.add(name)

    # ---------- 汎用 ----------
    def one(self, sql: str, *args: Any) -> sqlite3.Row | None:
        with closing(self.conn.execute(sql, args)) as cur:
            return cur.fetchone()

    def all(self, sql: str, *args: Any) -> list[sqlite3.Row]:
        with closing(self.conn.execute(sql, args)) as cur:
            return cur.fetchall()

    def run(self, sql: str, *args: Any) -> int:
        """更新件数を返す"""
        with closing(self.conn.execute(sql, args)) as cur:
            return cur.rowcount

    def insert(self, table: str, **cols: Any) -> int:
        keys = ", ".join(cols)
        qs = ", ".join("?" for _ in cols)
        with closing(self.conn.execute(f"INSERT INTO {table} ({keys}) VALUES ({qs})", tuple(cols.values()))) as cur:
            return int(cur.lastrowid)

    def update(self, table: str, row_id: int, **cols: Any) -> None:
        if table in ("reservations", "inquiries"):
            cols.setdefault("updated_at", iso(now()))
        sets = ", ".join(f"{k}=?" for k in cols)
        self.run(f"UPDATE {table} SET {sets} WHERE id=?", *cols.values(), row_id)

    # ---------- 状態の比較更新（同時実行の防止） ----------
    def transition(self, table: str, row_id: int, from_states: tuple[str, ...], to_state: str, **cols: Any) -> bool:
        cols["state"] = to_state
        cols["updated_at"] = iso(now())
        sets = ", ".join(f"{k}=?" for k in cols)
        qs = ", ".join("?" for _ in from_states)
        n = self.run(
            f"UPDATE {table} SET {sets} WHERE id=? AND state IN ({qs})",
            *cols.values(), row_id, *from_states,
        )
        return n == 1

    # ---------- 予約 ----------
    def reservation(self, rid: int) -> sqlite3.Row | None:
        return self.one("SELECT * FROM reservations WHERE id=?", rid)

    def reservation_by_pid(self, platform: str, pid: str) -> sqlite3.Row | None:
        return self.one("SELECT * FROM reservations WHERE platform=? AND platform_id=?", platform, pid)

    def reservation_by_ts(self, ts: str) -> sqlite3.Row | None:
        return self.one("SELECT * FROM reservations WHERE slack_ts=?", ts)

    def inquiry(self, iid: int) -> sqlite3.Row | None:
        return self.one("SELECT * FROM inquiries WHERE id=?", iid)

    # ---------- Slack ----------
    def save_slack(self, ts: str, channel: str, kind: str, reservation_id: int | None = None,
                   inquiry_id: int | None = None, extra: dict | None = None) -> None:
        self.run(
            "INSERT OR REPLACE INTO slack_messages (ts, channel, kind, reservation_id, inquiry_id, extra, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ts, channel, kind, reservation_id, inquiry_id, json.dumps(extra or {}, ensure_ascii=False), iso(now()),
        )

    def slack_msg(self, ts: str) -> sqlite3.Row | None:
        return self.one("SELECT * FROM slack_messages WHERE ts=?", ts)

    # ---------- ログ ----------
    def log(self, action: str, *, reservation_id: int | None = None, inquiry_id: int | None = None,
            trigger: str | None = None, actor: str | None = None, result: str | None = None,
            detail: str | None = None, screenshots: list[str] | None = None) -> None:
        from .secrets import redact
        self.insert(
            "action_log", at=iso(now()), reservation_id=reservation_id, inquiry_id=inquiry_id,
            trigger=trigger, actor=actor, action=action, result=result,
            detail=redact(detail) if detail else None,
            screenshots=json.dumps(screenshots or [], ensure_ascii=False),
        )

    # ---------- kv ----------
    def kv_get(self, key: str) -> str | None:
        r = self.one("SELECT value FROM kv WHERE key=?", key)
        return r["value"] if r else None

    def kv_set(self, key: str, value: str | None) -> None:
        self.run("INSERT OR REPLACE INTO kv (key, value) VALUES (?, ?)", key, value)
