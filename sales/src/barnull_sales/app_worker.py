"""sales-worker（非公開。Cloud Tasks と Cloud Scheduler からだけ呼べる）。

| パス                    | 呼び出し元        | 内容                                          |
|------------------------|------------------|-----------------------------------------------|
| /tasks/process-event   | Cloud Tasks      | LINE イベント1件（process.handle_event）         |
| /tasks/pos-slip        | Cloud Tasks      | POS（福岡）の伝票1件（pos_intake.handle）        |
| /jobs/tick             | Cloud Tasks      | 福岡の定期ジョブ（schedule.run_tick。次の回を予約）|
| /jobs/reconcile        | Scheduler 30分ごと | jobs.RECONCILE_STEPS                         |
| /jobs/daily            | Scheduler 毎日     | jobs.DAILY_STEPS                             |
| /jobs/weekly           | Scheduler 毎週月曜 | 売上週報（report.publish）                    |
同時に1件ずつしか処理しない（Cloud Run の concurrency=1）。伝票の同時更新を起こさないため。
"""

from __future__ import annotations

import logging
from functools import lru_cache

from fastapi import FastAPI, Request, Response

from . import jobs, logs, pos_intake, schedule
from .asana import AsanaTracker
from .config import secret, settings
from .deps import Deps
from .extract import ClaudeExtractor
from .line import LineClient
from .mf import MoneyForward
from .pos_notify import HttpPosClient
from .process import event_id_of, handle_event
from .slack import SlackNotifier
from .square import OAuthTokens, SecretStore, SquareAPI
from .store import FirestoreStore

logs.setup()
log = logging.getLogger("sales-worker")
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


@lru_cache
def deps() -> Deps:
    s = settings()
    square = SquareAPI(OAuthTokens(SecretStore(s.project, "square-oauth"), secret("SQUARE_APP_ID"),
                                   secret("SQUARE_APP_SECRET")), s.square.location_id)
    return Deps(
        settings=s,
        store=FirestoreStore(s.project),
        notifier=SlackNotifier(secret("SLACK_BOT_TOKEN")),
        tracker=AsanaTracker(secret("ASANA_PAT"), s.asana_ids, s.asana_env),
        extractor=ClaudeExtractor(secret("ANTHROPIC_API_KEY"), s.model_primary, s.model_fallback, s.effort),
        line=LineClient(secret("LINE_CHANNEL_ACCESS_TOKEN")) if s.line_enabled else None,   # LINE を使わない店舗（福岡）は無し
        payments=square,
        payouts=square,
        bank=MoneyForward(secret("MF_API_KEY"), s.mf.office_code, s.mf.bank_subaccount),
        pos=HttpPosClient(s.pos_callback_url, s.pos_callback_audience) if s.pos_callback_url else None,
    )


@app.post("/tasks/process-event")
def process_event(body: dict, request: Request) -> Response:
    event, kind = body["event"], body["kind"]
    attempt = int(request.headers.get("X-CloudTasks-TaskRetryCount", "0")) + 1
    with logs.context(job="process_event", event_id=event_id_of(event)):
        try:
            result = handle_event(event, kind, deps(), attempt=attempt)
        except Exception:
            log.exception("処理に失敗（Cloud Tasks がやり直す。%d回目）", attempt)
            return Response(status_code=500)
        log.info("kind=%s status=%s slips=%s usage=%s", kind, result.get("status"), result.get("slip_ids"),
                 result.get("usage"))
    return Response(status_code=200)


@app.post("/tasks/pos-slip")
def pos_slip(body: dict, request: Request) -> Response:
    attempt = int(request.headers.get("X-CloudTasks-TaskRetryCount", "0")) + 1
    with logs.context(job="pos_slip", event_id=f"pos-{body.get('visit_id', '')}"):
        try:
            result = pos_intake.handle(deps(), body)
        except pos_intake.Invalid as e:
            log.error("POS の伝票の形が正しくない（やり直さない）: %s", e)
            return Response(status_code=200)
        except Exception:
            log.exception("POS の伝票の処理に失敗（Cloud Tasks がやり直す。%d回目）", attempt)
            return Response(status_code=500)
        log.info("POS の伝票 status=%s slip=%s", result.get("status"), result.get("slip_id"))
    schedule.ensure(deps())   # 定期ジョブの予約が途切れていたら、ここで予約し直す（福岡だけ）
    return Response(status_code=200)


@app.post("/jobs/tick")
def job_tick() -> Response:
    # 福岡（[schedule] mode = "tasks"）: 30分ごとの回。次の回はここで予約する（失敗しても 200。やり直しは次の予約で）
    schedule.run_tick(deps())
    return Response(status_code=200)


def _run(steps) -> Response:
    # ステップの失敗は health が数えて知らせるので、200 を返す（Scheduler にやり直させると失敗を何重にも数えてしまう）。
    # Scheduler がやり直すのは、worker が別の処理中で受け付けられなかったとき（429）など
    jobs.run_steps(deps(), steps)
    return Response(status_code=200)


@app.post("/jobs/reconcile")
def job_reconcile() -> Response:
    return _run(jobs.RECONCILE_STEPS)


@app.post("/jobs/daily")
def job_daily() -> Response:
    return _run(jobs.DAILY_STEPS)


@app.post("/jobs/weekly")
def job_weekly() -> Response:
    return _run(jobs.WEEKLY_STEPS)
