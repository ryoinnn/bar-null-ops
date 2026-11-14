"""定期ジョブ（Cloud Scheduler → sales-worker）。ジョブはいくつかのステップに分かれ、ステップごとに成否を記録する。

| ジョブ                  | いつ           | ステップ                                                               |
|------------------------|----------------|-----------------------------------------------------------------------|
| /jobs/reconcile        | 30分ごと        | manual_overrides → republish → resync → reconcile → deposits → pos_notify |
|                        |                | → staff_sync → attendance_approvals → attendance_overrides → attendance_resync → payroll → payroll_adjustments |
| /jobs/daily            | 毎日 10:05      | deposit_alerts → unpaid_reminders → delete_old_events                 |
| /jobs/weekly           | 毎週月曜 9:10   | weekly_report                                                         |

1つのステップが失敗しても、次のステップは動かす。失敗は health が数え、続いたら Slack に知らせる。
ログは jsonPayload.job（ステップ名）で絞り込める。
福岡（[schedule] mode = "tasks"）は Cloud Scheduler を使わず、/jobs/tick が同じステップを動かして次の回を予約する（schedule.py）。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from . import (attendance, deposits, health, labor_hooks, logs, monitor, overrides, payroll, pos_notify, reconcile,
               supplies, unpaid)
from .deps import Deps
from .errors import ItemErrors
from .slip_ops import publish, sync

log = logging.getLogger(__name__)
REPUBLISH_DAYS = 3


def run_steps(deps: Deps, steps: list[tuple[str, Callable[[Deps], object]]], job: str | None = None) -> bool:
    """ステップを順に動かす。job（reconcile / daily / weekly）を渡すと、最後まで動いた時刻を meta/heartbeat に残す
    （外部監視が /healthz で「定期処理が止まっていないか」を見る。ステップが失敗しても、ジョブ自体は動いたので残す）。"""
    ok = True
    for name, fn in steps:
        with logs.context(job=name):
            try:
                result = fn(deps)
                log.info("完了 %s", result)
                health.record(deps, name, None)
            except Exception as e:
                log.exception("失敗")
                health.record(deps, name, e)
                ok = False
    if job:
        monitor.beat(deps, job)
    return ok


def republish(deps: Deps) -> int:
    """Slack の親メッセージか Asana のタスクが無い伝票（LINE イベントの処理が途中で止まったもの）を作り直す。"""
    since = (datetime.now(UTC) - timedelta(days=REPUBLISH_DAYS)).isoformat()
    errs = ItemErrors("republish")
    n = 0
    for slip in deps.store.find_slips("created_at", ">=", since):
        if slip.slack.get("thread_ts") and slip.asana.get("task_gid"):
            continue
        with errs.item(slip_id=slip.slip_id):
            publish(deps, slip)
            n += 1
    errs.raise_if_any()
    return n


def resync(deps: Deps) -> int:
    """Slack・Asana への反映に失敗した伝票（sync_pending）を、もう一度反映する。"""
    errs = ItemErrors("resync")
    slips = deps.store.find_slips("sync_pending", "==", True)
    for slip in slips:
        with errs.item(slip_id=slip.slip_id):
            sync(deps, slip, raise_errors=True)
    errs.raise_if_any()
    return len(slips)


def delete_old_events(deps: Deps) -> int:
    days = deps.settings.followup.event_retention_days
    return deps.store.delete_docs_before("line_events", "received_at",
                                         (datetime.now(UTC) - timedelta(days=days)).isoformat())


def _labor(fn):
    """[labor] が無効の店舗（福岡）では何もしない。"""
    def run(deps: Deps):
        return fn(deps) if deps.settings.labor.enabled else None
    return run


RECONCILE_STEPS = [("manual_overrides", overrides.apply), ("republish", republish), ("resync", resync),
                   ("reconcile", reconcile.run), ("deposits", deposits.run), ("pos_notify", pos_notify.run),
                   # 勤怠・給与（docs/profit/labor_design.md）
                   ("staff_sync", _labor(labor_hooks.staff_sync)),
                   ("attendance_approvals", _labor(attendance.approvals)),
                   ("attendance_overrides", _labor(attendance.overrides)),
                   ("attendance_resync", _labor(attendance.resync)),
                   ("payroll", _labor(payroll.step)),
                   ("payroll_adjustments", _labor(payroll.adjustments)),
                   # 備品・消耗品（マネフォのカード → Asana。docs/profit/supplies_requirements.md）
                   ("supplies", supplies.step),
                   # 外部監視（GitHub Actions）が動いているか（docs/monitoring_requirements.md の相互監視）
                   ("monitor_watch", monitor.watch)]


def weekly_report(deps: Deps) -> dict:
    from .asana import AsanaAPI
    from .config import secret
    from .report import publish

    return publish(deps, AsanaAPI(secret("ASANA_PAT")))


DAILY_STEPS = [("deposit_alerts", deposits.deposit_alerts), ("unpaid_reminders", unpaid.reminders),
               ("delete_old_events", delete_old_events),
               ("secret_expiry", monitor.expiry)]   # 期限のある秘密情報（[[monitor.expiry]]）
WEEKLY_STEPS = [("weekly_report", weekly_report)]
