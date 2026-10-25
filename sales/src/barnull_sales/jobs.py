"""定期ジョブ（Cloud Scheduler → sales-worker）。ジョブはいくつかのステップに分かれ、ステップごとに成否を記録する。

| ジョブ                  | いつ           | ステップ                                                               |
|------------------------|----------------|-----------------------------------------------------------------------|
| /jobs/reconcile        | 30分ごと        | manual_overrides → republish → resync → reconcile → deposits → pos_notify |
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

from . import deposits, health, logs, overrides, pos_notify, reconcile, unpaid
from .deps import Deps
from .errors import ItemErrors
from .slip_ops import publish, sync

log = logging.getLogger(__name__)
REPUBLISH_DAYS = 3


def run_steps(deps: Deps, steps: list[tuple[str, Callable[[Deps], object]]]) -> bool:
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


RECONCILE_STEPS = [("manual_overrides", overrides.apply), ("republish", republish), ("resync", resync),
                   ("reconcile", reconcile.run), ("deposits", deposits.run), ("pos_notify", pos_notify.run)]


def weekly_report(deps: Deps) -> dict:
    from .asana import AsanaAPI
    from .config import secret
    from .report import publish

    return publish(deps, AsanaAPI(secret("ASANA_PAT")))


DAILY_STEPS = [("deposit_alerts", deposits.deposit_alerts), ("unpaid_reminders", unpaid.reminders),
               ("delete_old_events", delete_old_events)]
WEEKLY_STEPS = [("weekly_report", weekly_report)]
