"""外部監視（check.py）の判定と通知のテスト。手元で: python -m pytest .github/monitor -q"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import check
import pytest

NOW = datetime(2026, 11, 15, 3, 0, tzinfo=UTC)


def stub(monkeypatch, status, body):
    monkeypatch.setattr(check, "http_get", lambda url, headers=None: (status, body))


def health(**jobs_ago_min):
    return json.dumps({"ok": True, "version": "abc1234", "failing": [],
                       "jobs": {k: (NOW - timedelta(minutes=v)).isoformat() for k, v in jobs_ago_min.items()}})


SALES = {"name": "売上", "url": "https://x/health", "kind": "sales", "deploy": True}


def keys(problems):
    return [p["key"] for p in problems]


def test_billing_down_and_ok(monkeypatch):
    stub(monkeypatch, 500, check.RECORDED_BILLING_BODY)
    assert keys(check.check_target(SALES, NOW)) == ["売上:billing"]
    stub(monkeypatch, None, "TimeoutError")
    assert keys(check.check_target(SALES, NOW)) == ["売上:down"]
    stub(monkeypatch, 503, "Service Unavailable")
    assert keys(check.check_target(SALES, NOW)) == ["売上:down"]
    stub(monkeypatch, 200, health(reconcile=20, daily=60 * 20, weekly=60 * 24 * 6))
    assert check.check_target(SALES, NOW) == []
    stub(monkeypatch, 404, "not found")                     # plain は応答さえあればよい
    assert check.check_target({"name": "node-api", "url": "u", "kind": "plain"}, NOW) == []


def test_simulated_billing():
    assert keys(check.check_target({**SALES, "simulate": "billing"}, NOW)) == ["売上:billing"]


def test_stale_jobs_and_unreadable_health(monkeypatch):
    stub(monkeypatch, 200, health(reconcile=80, daily=60 * 27, weekly=60))
    assert keys(check.check_target(SALES, NOW)) == ["売上:job:reconcile", "売上:job:daily"]
    stub(monkeypatch, 200, json.dumps({"ok": False, "jobs": None}))
    assert keys(check.check_target(SALES, NOW)) == ["売上:health"]
    stub(monkeypatch, 404, "{}")                             # /health が無い（古い版）
    assert keys(check.check_target(SALES, NOW)) == ["売上:health"]


def test_deploy(monkeypatch):
    stub(monkeypatch, 200, health(reconcile=5))
    old = NOW - timedelta(minutes=41)
    assert keys(check.check_target(SALES, NOW, ("def5678" + "0" * 33, old))) == ["売上:deploy"]
    assert check.check_target(SALES, NOW, ("abc1234" + "0" * 33, old)) == []               # 反映済み
    assert check.check_target(SALES, NOW, ("def5678" + "0" * 33, NOW - timedelta(minutes=10))) == []  # まだ猶予内
    assert check.check_target({**SALES, "deploy": False}, NOW, ("def5678", old)) == []


class FakeSlack:
    def __init__(self, fail=False):
        self.posts, self.updates, self.fail, self.failed = [], [], fail, False

    def post(self, text, thread_ts=None):
        if self.fail:
            self.failed = True
            return None
        self.posts.append({"text": text, "thread_ts": thread_ts})
        return f"ts{len(self.posts)}"

    def update(self, ts, text):
        self.updates.append((ts, text))


P = [{"key": "売上:billing", "title": "課金停止", "text": "売上: 課金が無効"}]


def test_notify_flow(monkeypatch):
    monkeypatch.setenv("SLACK_MENTION", "UALERT")
    s = FakeSlack()
    st = check.notify({}, P, s, NOW)                                # 1回目は知らせない
    assert not s.posts and st["売上:billing"]["count"] == 1
    st = check.notify(st, P, s, NOW + timedelta(minutes=15))
    assert s.posts[-1]["thread_ts"] is None and "<@UALERT> 【課金停止】" in s.posts[-1]["text"]
    st = check.notify(st, P, s, NOW + timedelta(hours=4))           # 8時間までは黙る
    assert len(s.posts) == 1
    st = check.notify(st, P, s, NOW + timedelta(hours=8, minutes=16))
    assert s.posts[-1]["thread_ts"] == "ts1" and "まだ続いています" in s.posts[-1]["text"]
    st = check.notify(st, [], s, NOW + timedelta(hours=9))
    assert s.posts[-1]["thread_ts"] == "ts1" and "【復旧】" in s.posts[-1]["text"]
    assert s.updates[-1][1].startswith("【復旧済み】") and st == {}


def test_blip_is_not_notified():
    s = FakeSlack()
    st = check.notify({}, P, s, NOW)
    st = check.notify(st, [], s, NOW + timedelta(minutes=15))       # 1回だけの失敗は何も出さずに消える
    assert not s.posts and st == {}


def test_slack_failure_keeps_state_and_fails(monkeypatch):
    s = FakeSlack(fail=True)
    st = check.notify({"売上:billing": {"count": 1, "since": NOW.isoformat()}}, P, s, NOW)
    assert s.failed and "ts" not in st["売上:billing"]               # 次の回にもう一度出そうとする


def test_main_exit_code(monkeypatch, tmp_path):
    monkeypatch.setenv("MONITOR_TARGETS", json.dumps([{**SALES, "deploy": False, "simulate": "billing"}]))
    monkeypatch.setenv("STATE_FILE", str(tmp_path / "s.json"))
    monkeypatch.setenv("MONITOR_CONSECUTIVE", "1")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "")
    monkeypatch.setattr(check, "CONSECUTIVE", 1)
    monkeypatch.setattr(check.Slack, "call", lambda self, m, p: (setattr(self, "failed", True), None)[1])
    assert check.main() == 1                                        # Slack に出せない → 失敗（GitHub からメール）
    monkeypatch.setattr(check.Slack, "call", lambda self, m, p: {"ok": True, "ts": "1"})
    assert check.main() == 0


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))


def test_expiry_backup_only_when_worker_is_down(tmp_path, monkeypatch):
    f = tmp_path / "sales.toml"
    f.write_text('[[monitor.expiry]]\nname = "Claude キー"\nexpires = "2026-11-20T04:00:00+09:00"\nrenew = "set_secret.py"\n',
                 encoding="utf-8")
    monkeypatch.setenv("MONITOR_EXPIRY_FILE", str(f))
    s = FakeSlack()
    assert check.expiry_backup({}, [], s, NOW) == {} and not s.posts                 # worker が元気なら worker に任せる
    down = [{"key": "売上:billing"}]
    st = check.expiry_backup({}, down, s, NOW)
    assert "【期限が近い】" in s.posts[-1]["text"] and "外部監視から" in s.posts[-1]["text"] and list(st.values()) == [7]
    assert check.expiry_backup(st, down, s, NOW + timedelta(hours=1)) == st and len(s.posts) == 1


def test_expiry_state_is_not_treated_as_problem(monkeypatch, tmp_path):
    monkeypatch.setenv("MONITOR_TARGETS", json.dumps([{"name": "n", "url": "u", "kind": "plain"}]))
    monkeypatch.setenv("STATE_FILE", str(tmp_path / "s.json"))
    monkeypatch.setenv("MONITOR_EXPIRY_FILE", str(tmp_path / "none.toml"))
    (tmp_path / "s.json").write_text(json.dumps({"_expiry": {"k|t": 7}}), encoding="utf-8")
    monkeypatch.setattr(check, "http_get", lambda url, headers=None: (200, "ok"))
    calls = []
    monkeypatch.setattr(check.Slack, "call", lambda self, m, p: calls.append(p) or {"ok": True, "ts": "1"})
    assert check.main() == 0 and not calls                                          # 「復旧」を誤って出さない
    assert json.loads((tmp_path / "s.json").read_text(encoding="utf-8"))["_expiry"] == {"k|t": 7}
