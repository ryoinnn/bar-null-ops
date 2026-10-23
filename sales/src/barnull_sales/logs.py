"""ログ（Cloud Logging で読みやすい1行1件の JSON）。

どの処理の、どのイベント・伝票のログかを、`context(job=..., slip_id=...)` で付ける。
Cloud Logging では jsonPayload.slip_id="S-20261017-001" のように絞り込める。
顧客名・金額・メッセージ本文はログに書かない（Slack と Asana にだけ出す）。
"""

from __future__ import annotations

import contextvars
import json
import logging
import sys
from contextlib import contextmanager

_ctx: contextvars.ContextVar[dict | None] = contextvars.ContextVar("log_ctx", default=None)
FIELDS = ("job", "event_id", "slip_id", "task_gid", "cash_report", "api", "status", "stage")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out = {"severity": record.levelname, "message": record.getMessage(), "logger": record.name}
        out.update(_ctx.get() or {})
        for k in FIELDS:
            if hasattr(record, k):
                out[k] = getattr(record, k)
        if record.exc_info:
            out["exception"] = self.formatException(record.exc_info)
            err = record.exc_info[1]
            if hasattr(err, "api"):          # http.ExternalAPIError: どの連携先が何を返したか
                out.setdefault("api", err.api)
                out.setdefault("status", getattr(err, "status", None))
        return json.dumps(out, ensure_ascii=False, default=str)


def setup() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.INFO)
    for name in ("httpx", "httpx2", "httpcore"):   # 1リクエストごとの INFO は出さない（URL に ID が入るため）
        logging.getLogger(name).setLevel(logging.WARNING)


@contextmanager
def context(**fields):
    """この中のログに fields を付ける（入れ子にすると足される）。"""
    token = _ctx.set({**(_ctx.get() or {}), **{k: v for k, v in fields.items() if v is not None}})
    try:
        yield
    finally:
        _ctx.reset(token)
