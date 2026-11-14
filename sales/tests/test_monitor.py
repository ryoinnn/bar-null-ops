"""システム監視の GCP 側: 通知先・スレッドでのリマインド・heartbeat・/health の中身・外部監視の見張り。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from helpers import make_deps

from barnull_sales import health, jobs, monitor
from barnull_sales.http import ExternalAPIError

ERR = ExternalAPIError("Square", "GET /v2/payments", 503, "Service Unavailable")


def live(**kw):
    return make_deps(dry_run=False, slack_monitor_channel_prod="CMON", **kw)


def test_alert_goes_to_monitor_channel_with_reminders_and_recovery_in_thread(monkeypatch):
    deps = live()
    for _ in range(2):
        health.record(deps, "deposits", ERR)
    parent = deps.notifier.posts[-1]
    assert parent["channel"] == "CMON" and parent["thread_ts"] is None
    assert "<@UALERT>" in parent["text"] and "【外部API障害】" in parent["text"] and "Square" in parent["text"]
    health.record(deps, "deposits", ERR)                       # 8時間たつまでは何も出さない
    assert len(deps.notifier.posts) == 1
    doc = deps.store.get_doc("meta", "job_health")
    doc["deposits"]["notified_at"] = (datetime.now(UTC) - timedelta(hours=8, minutes=1)).isoformat()
    deps.store.set_doc("meta", "job_health", doc)
    health.record(deps, "deposits", ERR)
    remind = deps.notifier.posts[-1]
    assert remind["thread_ts"] == parent["ts"] and "まだ続いています" in remind["text"]
    health.record(deps, "deposits", None)
    rec = deps.notifier.posts[-1]
    assert rec["thread_ts"] == parent["ts"] and "【復旧】" in rec["text"]
    health.record(deps, "deposits", None)
    assert len([p for p in deps.notifier.posts if "【復旧】" in p["text"]]) == 1
    assert deps.notifier.updates == [{"channel": "CMON", "ts": parent["ts"], "text": "【復旧済み】" + parent["text"]}]


def test_falls_back_to_business_channel_when_monitor_channel_fails():
    deps = live()
    real = deps.notifier.post

    def post(channel, text, thread_ts=None, broadcast=False):
        if channel == "CMON":
            raise ExternalAPIError("Slack", "chat.postMessage", 200, "not_in_channel")
        return real(channel, text, thread_ts, broadcast)

    deps.notifier.post = post
    for _ in range(2):
        health.record(deps, "reconcile", KeyError("x"))
    assert deps.notifier.posts[-1]["channel"] == "CPROD" and "【処理エラー】" in deps.notifier.posts[-1]["text"]


def test_dry_run_uses_test_channel():
    deps = make_deps(dry_run=True, slack_monitor_channel_prod="CMON")
    for _ in range(2):
        health.record(deps, "deposits", ERR)
    assert deps.notifier.posts[-1]["channel"] == "CTEST"


def test_heartbeat_and_snapshot(monkeypatch):
    deps = live()
    monkeypatch.setenv("GIT_SHA", "abc1234")
    jobs.run_steps(deps, [("ok", lambda d: 1), ("ng", lambda d: 1 / 0)], job="reconcile")
    jobs.run_steps(deps, [("ng", lambda d: 1 / 0)])               # job を渡さなければ残さない
    snap = monitor.snapshot(deps.store)
    assert snap["ok"] and snap["version"] == "abc1234"
    assert set(snap["jobs"]) == {"reconcile"} and snap["failing"] == ["ng"]
    assert "last_error" not in str(snap) and "division" not in str(snap)   # エラー本文は返さない


def test_snapshot_reports_store_failure():
    class Broken:
        def get_doc(self, *a):
            raise RuntimeError("permission denied")
    snap = monitor.snapshot(Broken())
    assert snap["ok"] is False and snap["jobs"] is None


NOW = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)


def run(event="schedule", ago_min=10, status="completed", conclusion="success"):
    return {"event": event, "created_at": (NOW - timedelta(minutes=ago_min)).isoformat().replace("+00:00", "Z"),
            "status": status, "conclusion": conclusion}


def test_watch_external_monitor():
    deps = make_deps(monitor_watch_repo="o/r")
    assert monitor.watch(make_deps(), fetch=lambda r: []) == "skipped"         # 設定の無い店舗
    assert monitor.watch(deps, fetch=lambda r: None, now=NOW) == "unknown"      # API の回数制限
    assert monitor.watch(deps, fetch=lambda r: [run("workflow_dispatch")], now=NOW) == "not-started"
    assert monitor.watch(deps, fetch=lambda r: [run(), run(ago_min=25)], now=NOW) == "ok"
    with pytest.raises(monitor.MonitorStopped, match="動いていません"):
        monitor.watch(deps, fetch=lambda r: [run(ago_min=200)], now=NOW)
    with pytest.raises(monitor.MonitorStopped, match="続けて失敗"):
        monitor.watch(deps, fetch=lambda r: [run(conclusion="failure"), run(ago_min=25, conclusion="failure")], now=NOW)
    # 実行中の回は数えない
    assert monitor.watch(deps, fetch=lambda r: [run(status="in_progress", conclusion=None), run(ago_min=20),
                                                 run(ago_min=35, conclusion="failure")], now=NOW) == "ok"


def test_health_endpoint(monkeypatch):
    from fastapi.testclient import TestClient

    from barnull_sales import app_webhook
    from barnull_sales.store import MemoryStore

    store = MemoryStore()
    store.set_doc("meta", "heartbeat", {"reconcile": "2026-11-15T12:00:00+00:00"})
    monkeypatch.setattr("barnull_sales.store.FirestoreStore", lambda project: store)
    app_webhook._health["body"] = None
    r = TestClient(app_webhook.app).get("/health")
    assert r.status_code == 200 and r.json()["jobs"] == {"reconcile": "2026-11-15T12:00:00+00:00"}


# ---------------------------------------------------------------- 認証切れ・処理できなかったメッセージ（2026-11-15 B の指摘）
def test_auth_error_is_notified_at_first_failure_as_auth():
    from barnull_sales.errors import JobFailed

    deps = live()
    health.record(deps, "deposits", ExternalAPIError("Square", "GET", 401, "UNAUTHORIZED"))   # 401
    assert "【認証切れ】" in deps.notifier.posts[-1]["text"] and "秘密情報の更新" in deps.notifier.posts[-1]["text"]
    deps = live()
    part = JobFailed("deposits", [("a", ExternalAPIError("Asana", "PUT", 403, "")), ("b", KeyError("k"))])
    health.record(deps, "deposits", part)
    assert "【認証切れ】" in deps.notifier.posts[-1]["text"] and "Asana" in deps.notifier.posts[-1]["text"]
    deps = live()
    health.record(deps, "deposits", ExternalAPIError("Square", "GET", 503, ""))   # 一時的な障害は2回目から
    assert not deps.notifier.posts


def test_claude_key_invalid_on_line_event_goes_to_monitor_channel():
    from helpers import event

    from barnull_sales.process import handle_event

    deps = live()

    class BadKey:
        def parse(self, text, sent_at):
            raise ExternalAPIError("Claude", "messages.create", 401, "invalid x-api-key")

    deps.extractor = BadKey()
    ev = event("2026-11-10 22:00", event_id="E9")
    with pytest.raises(ExternalAPIError):
        handle_event(ev, "message", deps)
    first = deps.notifier.posts[-1]
    assert first["channel"] == "CMON" and "【認証切れ】" in first["text"] and "Claude" in first["text"]
    r = handle_event(ev, "message", deps, attempt=10)                      # 最後の試行
    assert r["status"] == "failed"
    mon = [p for p in deps.notifier.posts if p["channel"] == "CMON" and "処理できなかったメッセージ" in p["text"]]
    biz = [p for p in deps.notifier.posts if p["channel"] == "CPROD" and "処理できなかったメッセージ" in p["text"]]
    assert len(mon) == 1 and len(biz) == 1 and "伝票" not in mon[0]["text"].split("原因")[0].replace("業務", "")


# ---------------------------------------------------------------- 秘密情報の期限（2026-11-15 本人の依頼）
def test_secret_expiry_stages():
    item = {"name": "Claude API キー", "expires": "2027-11-22T04:00:00+09:00", "renew": "set_secret.py ANTHROPIC_API_KEY"}
    deps = live(monitor_expiry=(item,))
    exp = datetime(2027, 11, 21, 19, 0, tzinfo=UTC)
    assert monitor.expiry(deps, now=exp - timedelta(days=40)) == []
    assert monitor.expiry(deps, now=exp - timedelta(days=29)) == ["Claude API キー"]
    p = deps.notifier.posts[-1]
    assert p["channel"] == "CMON" and "<@UALERT>" in p["text"] and "【期限が近い】" in p["text"]
    assert "2027-11-22 04:00" in p["text"] and "set_secret.py ANTHROPIC_API_KEY" in p["text"]
    assert monitor.expiry(deps, now=exp - timedelta(days=28)) == []           # 同じ段階は1回だけ
    assert monitor.expiry(deps, now=exp - timedelta(days=3)) == ["Claude API キー"]   # 7日前（1日遅れても知らせる）
    assert monitor.expiry(deps, now=exp - timedelta(hours=10)) == ["Claude API キー"]  # 前日
    assert monitor.expiry(deps, now=exp + timedelta(hours=1)) == ["Claude API キー"]
    assert "【期限切れ】" in deps.notifier.posts[-1]["text"]
    assert monitor.expiry(deps, now=exp + timedelta(days=2)) == []
    # 発行し直して期限を書き換えたら、数え直す
    deps.settings = deps.settings.__class__(**{**deps.settings.__dict__,
                                              "monitor_expiry": ({**item, "expires": "2028-11-22T04:00:00+09:00"},)})
    assert monitor.expiry(deps, now=exp + timedelta(days=2)) == []
    assert monitor.expiry(deps, now=exp + timedelta(days=340)) == ["Claude API キー"]


def test_sales_toml_expiry_parses():
    from barnull_sales.config import load_settings

    s = load_settings()
    assert any("Claude" in x["name"] for x in s.monitor_expiry)
    for x in s.monitor_expiry:
        datetime.fromisoformat(str(x["expires"]))
