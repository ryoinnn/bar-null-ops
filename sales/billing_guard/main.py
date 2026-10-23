"""billing-guard: 予算の通知（Pub/Sub）を受け、費用が出たらプロジェクトの請求先アカウントを外す。

Google 公式の「通知で課金を無効にする」と同じ仕組み。売上管理の本体とは独立して動く。

環境変数:
  GCP_PROJECT            対象のプロジェクト ID
  GUARD_MODE             dry（判定と通知だけ）/ live（実際に外す）
  THRESHOLD_JPY          この金額以上で外す（既定 1）
  SLACK_CHANNEL          通知先のチャンネル ID
  SLACK_ALERT_MENTION    メンションする Slack ユーザー ID
  SALES_CONFIG           Secret Manager の sales-config（JSON。SLACK_BOT_TOKEN を使う）
"""

from __future__ import annotations

import base64
import json
import logging
import os

import httpx
from fastapi import FastAPI, Request, Response

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("billing-guard")

PROJECT = os.environ.get("GCP_PROJECT", "")
MODE = os.environ.get("GUARD_MODE", "dry")
THRESHOLD = float(os.environ.get("THRESHOLD_JPY", "1"))
CHANNEL = os.environ.get("SLACK_CHANNEL", "")
MENTION = os.environ.get("SLACK_ALERT_MENTION", "")
PERMISSION = "resourcemanager.projects.deleteBillingAssignment"

app = FastAPI()


def _slack_token() -> str | None:
    try:
        return json.loads(os.environ.get("SALES_CONFIG", "{}")).get("SLACK_BOT_TOKEN")
    except json.JSONDecodeError:
        return None


def _access_token() -> str:
    r = httpx.get("http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token",
                  headers={"Metadata-Flavor": "Google"}, timeout=10)
    r.raise_for_status()
    return r.json()["access_token"]


def _slack(text: str) -> None:
    token = _slack_token()
    if not (token and CHANNEL):
        log.warning("Slack の設定が無いので通知しない")
        return
    try:
        r = httpx.post("https://slack.com/api/chat.postMessage", headers={"Authorization": f"Bearer {token}"},
                       json={"channel": CHANNEL, "text": text}, timeout=10).json()
        if not r.get("ok"):
            log.error("Slack 投稿に失敗: %s", r.get("error"))
    except httpx.HTTPError as e:
        log.error("Slack 投稿に失敗: %r", e)


def _billing_enabled(token: str) -> bool | None:
    """請求先がつながっているか。読み取りの権限が無い場合は None（不明。止める処理は続ける）。"""
    r = httpx.get(f"https://cloudbilling.googleapis.com/v1/projects/{PROJECT}/billingInfo",
                  headers={"Authorization": f"Bearer {token}"}, timeout=10)
    if r.status_code == 403:
        return None
    r.raise_for_status()
    return bool(r.json().get("billingEnabled"))


def _can_disable(token: str) -> bool:
    r = httpx.post(f"https://cloudresourcemanager.googleapis.com/v3/projects/{PROJECT}:testIamPermissions",
                   headers={"Authorization": f"Bearer {token}"}, json={"permissions": [PERMISSION]}, timeout=10)
    r.raise_for_status()
    return PERMISSION in r.json().get("permissions", [])


def _disable(token: str) -> None:
    r = httpx.put(f"https://cloudbilling.googleapis.com/v1/projects/{PROJECT}/billingInfo",
                  headers={"Authorization": f"Bearer {token}"}, json={"billingAccountName": ""}, timeout=30)
    r.raise_for_status()


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True, "mode": MODE}


@app.post("/pubsub")
async def pubsub(request: Request) -> Response:
    body = await request.json()
    msg = body.get("message", {})
    try:
        data = json.loads(base64.b64decode(msg.get("data", "")).decode("utf-8"))
    except (ValueError, json.JSONDecodeError):
        log.error("通知の data を読めない")
        return Response(status_code=204)
    cost = float(data.get("costAmount", 0))
    budget = data.get("budgetAmount")
    currency = data.get("currencyCode")
    log.info("budget notification cost=%s budget=%s %s interval=%s", cost, budget, currency,
             data.get("costIntervalStart"))
    if currency and currency != "JPY":
        log.warning("通貨が JPY ではない: %s", currency)
    if cost < THRESHOLD:
        return Response(status_code=204)

    token = _access_token()
    if _billing_enabled(token) is False:
        log.info("すでに請求先アカウントが外れている")
        return Response(status_code=204)
    mention = f"<@{MENTION}> " if MENTION else ""
    if MODE != "live":
        able = _can_disable(token)
        _slack(f"{mention}【課金停止テスト（dry）】今月の費用 ¥{cost:,.0f}（しきい値 ¥{THRESHOLD:,.0f}）。"
               f"本番モードなら請求先アカウントを外してすべて止めます。権限: {'あり' if able else 'なし（要設定）'}")
        return Response(status_code=204)
    _slack(f"{mention}【課金停止】今月の費用が ¥{cost:,.0f} になったため、プロジェクト {PROJECT} の請求先アカウントを外します。"
           "売上管理のシステムはすべて止まります。原因を確認してから、手動で請求先アカウントをつなぎ直してください。")
    _disable(token)
    log.warning("請求先アカウントを外した")
    return Response(status_code=204)
