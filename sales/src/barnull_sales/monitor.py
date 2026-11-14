"""システム監視の GCP 側（docs/monitoring_requirements.md）。

- beat: 定期ジョブ（reconcile / daily / weekly）が最後まで動いた時刻を meta/heartbeat に残す（ジョブ1回につき1回の書き込み）。
- snapshot: sales-webhook の GET /healthz が返す状態（時刻と状態だけ。金額・名前・エラー本文は返さない）。
- watch: 外部監視（GitHub Actions の system-monitor）が動いているかを、公開の GitHub API で確かめる（相互監視）。
  3時間以上動いていない、または続けて失敗している（Slack に出せていない）なら例外にして、health が通知する。
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime, timedelta

from .deps import Deps

log = logging.getLogger(__name__)
WATCH_MAX_AGE = timedelta(hours=3)
WORKFLOW = "system-monitor.yml"


class MonitorStopped(RuntimeError):
    pass


def beat(deps: Deps, job: str, now: datetime | None = None) -> None:
    try:
        doc = deps.store.get_doc("meta", "heartbeat") or {}
        deps.store.set_doc("meta", "heartbeat", {**doc, job: (now or datetime.now(UTC)).isoformat()})
    except Exception:
        log.exception("heartbeat の記録に失敗（外部監視が「定期処理の停止」と判断する可能性）")


def snapshot(store) -> dict:
    """/healthz の中身。Firestore を読めなければ jobs は None（外部監視が「状態を読めない」と判断する）。"""
    out: dict = {"ok": True, "version": os.environ.get("GIT_SHA", ""), "jobs": None, "failing": None}
    try:
        beats = store.get_doc("meta", "heartbeat") or {}
        health = store.get_doc("meta", "job_health") or {}
        out["jobs"] = {k: beats[k] for k in ("reconcile", "daily", "weekly") if k in beats}
        out["failing"] = sorted(k for k, v in health.items() if isinstance(v, dict) and v.get("failures"))
    except Exception:
        log.exception("/healthz: Firestore を読めない")
        out["ok"] = False
    return out


def watch(deps: Deps, fetch=None, now: datetime | None = None) -> str:
    """外部監視が動いているか。[monitor] watch_repo が無い店舗では何もしない。"""
    repo = deps.settings.monitor_watch_repo
    if not repo:
        return "skipped"
    now = now or datetime.now(UTC)
    runs = (fetch or _fetch_runs)(repo)
    if runs is None:
        return "unknown"                      # GitHub の API の回数制限など（次の回にもう一度見る）
    scheduled = [r for r in runs if r.get("event") == "schedule"]
    if not scheduled:
        return "not-started"                  # まだ一度も定時実行していない（設定前）
    latest = datetime.fromisoformat(scheduled[0]["created_at"].replace("Z", "+00:00"))
    if now - latest > WATCH_MAX_AGE:
        raise MonitorStopped(f"外部監視（GitHub Actions の system-monitor）が {latest.astimezone(deps.settings.tz):%m/%d %H:%M} "
                             f"から動いていません。GitHub の Actions でワークフローが無効になっていないか確かめてください")
    done = [r for r in scheduled if r.get("status") == "completed"][:2]
    if len(done) == 2 and all(r.get("conclusion") == "failure" for r in done):
        raise MonitorStopped("外部監視（GitHub Actions の system-monitor）が続けて失敗しています（Slack のトークン切れなど）。"
                             "GitHub の Actions のログを確かめてください")
    return "ok"


def _fetch_runs(repo: str) -> list[dict] | None:
    import httpx

    try:
        r = httpx.get(f"https://api.github.com/repos/{repo}/actions/workflows/{WORKFLOW}/runs",
                      params={"per_page": 10, "branch": "main"}, timeout=10,
                      headers={"Accept": "application/vnd.github+json"})
    except httpx.HTTPError as e:
        log.warning("GitHub の API に届かない: %s", e)
        return None
    if r.status_code in (403, 429):
        log.warning("GitHub の API の回数制限（%s）", r.status_code)
        return None
    if r.status_code == 404:
        return []                             # ワークフローがまだ無い
    r.raise_for_status()
    return r.json().get("workflow_runs") or []
