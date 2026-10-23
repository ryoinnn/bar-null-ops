"""sales-webhook（公開）: LINE の Webhook を受け、対象のイベントだけを Cloud Tasks に積んで、すぐ 200 を返す。

LINE は早い応答を求めるので、ここでは重い処理をしない。
対象外（Bot へのメンションが無い、画像、他のトークなど）は保存もせずに捨てる。
Square の許可（/oauth/square/*）は、sales.toml の square.oauth_enabled が true のときだけ受け付ける。
"""

from __future__ import annotations

import html
import json
import logging

from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse

from . import logs
from .config import secret, settings
from .line import classify_event, verify_signature
from .tasks import enqueue

logs.setup()
log = logging.getLogger("sales-webhook")
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
MAX_BODY = 1_000_000      # LINE の Webhook は小さい。これより大きい本文は読まずに断る


@app.post("/webhook/line")
async def webhook_line(request: Request) -> Response:
    length = request.headers.get("content-length") or "0"
    if not length.isdigit() or int(length) > MAX_BODY:
        return Response(status_code=413)
    body = await request.body()
    if len(body) > MAX_BODY:
        return Response(status_code=413)
    if not verify_signature(secret("LINE_CHANNEL_SECRET"), body, request.headers.get("x-line-signature")):
        log.warning("署名が一致しない")
        return Response(status_code=400)
    s = settings()
    for event in json.loads(body or b"{}").get("events", []):
        kind = classify_event(event, s.allowed_group_ids, s.bot_mention_name)
        if kind is None:
            continue
        event_id = event.get("webhookEventId", "")
        result = enqueue(project=s.project, region=s.region, queue=s.tasks_queue, worker_url=s.worker_url,
                         invoker_sa=s.invoker_sa, event=event, kind=kind)
        log.info("kind=%s redelivery=%s task=%s", kind, (event.get("deliveryContext") or {}).get("isRedelivery"),
                 result, extra={"event_id": event_id})
    return Response(status_code=200)


def _page(title: str, body: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(f"<!doctype html><meta charset='utf-8'><title>{html.escape(title)}</title>"
                        f"<body style='font-family:sans-serif;max-width:640px;margin:40px auto'>"
                        f"<h2>{html.escape(title)}</h2><p>{html.escape(body)}</p></body>", status_code=status)


@app.get("/oauth/square/start")
def square_start() -> Response:
    from .square import authorize_url, make_state

    if not settings().square.oauth_enabled:
        return Response(status_code=404)
    return RedirectResponse(authorize_url(secret("SQUARE_APP_ID"), make_state(secret("SQUARE_APP_SECRET"))))


@app.get("/oauth/square/callback")
def square_callback(code: str = "", state: str = "", error: str = "") -> Response:
    from .square import SecretStore, check_state, exchange

    s = settings()
    if not s.square.oauth_enabled:
        return Response(status_code=404)
    if error or not code:
        return _page("Square の許可が完了しませんでした", f"理由: {error or 'コードがありません'}", 400)
    if not check_state(secret("SQUARE_APP_SECRET"), state):
        return _page("Square の許可を確認できませんでした", "期限切れの可能性があります。最初からやり直してください。", 400)
    tok = exchange(secret("SQUARE_APP_ID"), secret("SQUARE_APP_SECRET"), code=code)
    if tok.get("merchant_id") != s.square.merchant_id:
        log.warning("想定外の加盟店からの許可を拒否")
        return _page("このアカウントでは連携できません", "Bar null の Square アカウントで許可してください。", 403)
    SecretStore(s.project, "square-oauth").write(
        {k: tok[k] for k in ("access_token", "refresh_token", "expires_at", "merchant_id") if k in tok})
    log.info("Square の OAuth トークンを保存した")
    return _page("Square との連携が完了しました",
                 "読み取り専用（決済・注文・振込・店舗情報）で連携しました。この画面は閉じてください。"
                 "sales.toml の square.oauth_enabled を false に戻してください。")
