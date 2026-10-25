"""定期ジョブを Cloud Tasks で動かす（[schedule] mode = "tasks"。福岡。Cloud Scheduler の無料枠を使わないため）。

30分ごとのジョブ（/jobs/reconcile）が、終わるときに次の回（30分区切りの時刻）を予約する。予約は時刻つきの名前なので、
何度予約しても1つ（Cloud Tasks が同じ名前を弾く）。毎日（10:05）・毎週（月曜 9:10）のジョブは、その時刻を過ぎた
最初の回で一緒に行う（実行した日を meta/schedule に残す）。
途切れたとき（予約に失敗したなど）は、POS からの伝票が届いた時点で予約し直す（ensure）。
Cloud Tasks の無料枠は月 100 万回。この使い方では月 1,500 回ほど。
"""

from __future__ import annotations

import base64
import logging
from datetime import UTC, datetime, time, timedelta

from . import jobs
from .deps import Deps

log = logging.getLogger(__name__)
INTERVAL_MIN = 30
DAILY_AT = time(10, 5)
WEEKLY_AT = (0, time(9, 10))   # 月曜


def next_slot(now: datetime) -> datetime:
    """次の30分区切りの時刻（ちょうど区切りなら、その次）。"""
    base = now.astimezone(UTC).replace(second=0, microsecond=0)
    minutes = (base.minute // INTERVAL_MIN + 1) * INTERVAL_MIN
    return base.replace(minute=0) + timedelta(minutes=minutes)


def due(deps: Deps, now: datetime) -> list[str]:
    """この回で一緒に行う毎日・毎週のジョブ。"""
    s = deps.settings
    local = now.astimezone(s.tz)
    meta = deps.store.get_doc("meta", "schedule") or {}
    out = []
    if local.time() >= DAILY_AT and meta.get("daily") != local.date().isoformat():
        out.append("daily")
    monday = (local - timedelta(days=local.weekday())).date()
    if (local.weekday() > WEEKLY_AT[0] or local.time() >= WEEKLY_AT[1]) and meta.get("weekly") != monday.isoformat():
        out.append("weekly")
    return out


def run_tick(deps: Deps, now: datetime | None = None) -> dict:
    """30分ごとの回。突合と入金の確認 → 必要なら毎日・毎週 → 次の回を予約。"""
    now = now or datetime.now(UTC)
    jobs.run_steps(deps, jobs.RECONCILE_STEPS)
    done = []
    meta = dict(deps.store.get_doc("meta", "schedule") or {})
    local = now.astimezone(deps.settings.tz)
    for name in due(deps, now):
        jobs.run_steps(deps, jobs.DAILY_STEPS if name == "daily" else jobs.WEEKLY_STEPS)
        meta[name] = (local.date() if name == "daily" else (local - timedelta(days=local.weekday())).date()).isoformat()
        done.append(name)
    if done:
        deps.store.set_doc("meta", "schedule", meta)
    return {"extra": done, "next": ensure(deps, now)}


def ensure(deps: Deps, now: datetime | None = None, enqueue=None) -> str:
    """次の回を予約する（すでにあれば何もしない）。戻り値: created / duplicate / skipped / failed。"""
    s = deps.settings
    if s.schedule_mode != "tasks":
        return "skipped"
    slot = next_slot(now or datetime.now(UTC))
    try:
        return (enqueue or _enqueue)(deps, slot)
    except Exception:
        log.exception("次の回の予約に失敗（POS からの伝票が届いたときに予約し直す）")
        return "failed"


def _enqueue(deps: Deps, slot: datetime) -> str:
    from .http import gcp_token, request

    s = deps.settings
    parent = f"projects/{s.project}/locations/{s.region}/queues/{s.tasks_queue}"
    task = {
        "name": f"{parent}/tasks/tick-{slot:%Y%m%d%H%M}",
        "scheduleTime": slot.isoformat().replace("+00:00", "Z"),
        "httpRequest": {
            "httpMethod": "POST", "url": f"{s.worker_url}/jobs/tick", "headers": {"Content-Type": "application/json"},
            "body": base64.b64encode(b"{}").decode(),
            "oidcToken": {"serviceAccountEmail": s.invoker_sa, "audience": s.worker_url},
        },
    }
    r = request("Cloud Tasks", "POST", f"https://cloudtasks.googleapis.com/v2/{parent}/tasks", idempotent=True,
                headers={"Authorization": f"Bearer {gcp_token()}"}, json={"task": task}, allow=(409,), timeout=10,
                attempts=2)
    return "duplicate" if r.status_code == 409 else "created"
