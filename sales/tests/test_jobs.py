"""定期ジョブ: 障害の通知、Slack・Asana への反映のやり直し、作りかけの伝票の作り直し。"""

from __future__ import annotations

from helpers import event, make_deps, parents, slip_msg

from barnull_sales import health, jobs
from barnull_sales.extract import Entry
from barnull_sales.http import ExternalAPIError
from barnull_sales.process import handle_event
from barnull_sales.slip_ops import raise_alert
from barnull_sales.slips import Alert, Status


def test_health_notifies_after_two_failures_and_recovers_once():
    deps = make_deps()
    err = ExternalAPIError("MoneyForward", "GET /api/v3/transactions", 500, "")
    health.record(deps, "deposits", err)
    assert not deps.notifier.posts                                   # 1回目は知らせない
    health.record(deps, "deposits", err)
    assert "【外部API障害】" in deps.notifier.posts[-1]["text"] and "MoneyForward" in deps.notifier.posts[-1]["text"]
    for _ in range(5):                                               # 直ったら「復旧」は1回だけ
        health.record(deps, "deposits", None)
    assert len([p for p in deps.notifier.posts if "【復旧】" in p["text"]]) == 1


def test_health_labels_internal_errors():
    deps = make_deps()
    for _ in range(2):
        health.record(deps, "reconcile", KeyError("x"))
    assert "【処理エラー】" in deps.notifier.posts[-1]["text"]


def test_failed_sync_is_retried_by_resync():
    deps = make_deps([slip_msg(Entry(month=10, day=5, customer_name="顧客A", amount=1000))])
    slip = deps.store.get_slip(handle_event(event("2026-10-06 05:40"), "message", deps)["slip_ids"][0])
    deps.tracker.fail = {"update"}                                   # Asana が落ちている
    raise_alert(deps, slip, Alert.PAYMENT_MISSING, "テスト", important=True)
    assert deps.store.get_slip(slip.slip_id).sync_pending
    assert any("決済未確認" in p["text"] for p in deps.notifier.posts)  # Asana が落ちていてもアラートは Slack に出る
    assert not jobs.run_steps(deps, [("resync", jobs.resync)])        # まだ失敗（health が数える）
    deps.tracker.fail = set()
    assert jobs.run_steps(deps, [("resync", jobs.resync)])
    slip = deps.store.get_slip(slip.slip_id)
    assert not slip.sync_pending and deps.tracker.tasks[slip.asana["task_gid"]]["status"] == Status.REVIEW


def test_republish_creates_missing_slack_and_asana():
    deps = make_deps([slip_msg(Entry(month=10, day=5, customer_name="顧客A", amount=1000))])
    deps.tracker.fail = {"create"}
    try:
        handle_event(event("2026-10-06 05:40"), "message", deps)
    except ExternalAPIError:
        pass
    deps.tracker.fail = set()
    from datetime import UTC, datetime
    slip = deps.store.find_slips("event_id", "==", "E1")[0]
    deps.store.update_slip(slip, lambda s: setattr(s, "created_at", datetime.now(UTC).isoformat()))
    assert jobs.republish(deps) == 1
    assert len(parents(deps)) == 1 and len(deps.tracker.tasks) == 1
    assert jobs.republish(deps) == 0
