"""外部監視（check.py）の判定と通知のテスト。手元で: python -m pytest .github/monitor -q"""

from __future__ import annotations

import http.server
import io
import json
import threading
import types
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta

import check
import pytest

NOW = datetime(2026, 11, 15, 3, 0, tzinfo=UTC)


def stub(monkeypatch, status, body):
    monkeypatch.setattr(check, "http_get", lambda url, headers=None, limit=check.READ_LIMIT: (status, body))


def health(now=NOW, **jobs_ago_min):
    return json.dumps({"ok": True, "version": "abc1234", "failing": [],
                       "jobs": {k: (now - timedelta(minutes=v)).isoformat() for k, v in jobs_ago_min.items()}})


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


REPO = "owner/repo"
API = f"https://api.github.com/repos/{REPO}/"


def commit(sha, at="2026-11-26T16:06:00Z"):
    return {"sha": sha, "commit": {"committer": {"date": at}}}


def detail(*files, patch_bytes=0):
    """GitHub の commits/{sha} の応答。patch_bytes で差分の本文を大きくする（日本語は1文字3バイト）。"""
    pad = "あ" * (patch_bytes // 3)
    return json.dumps({"files": [{"filename": f, "patch": pad} for f in files]}, ensure_ascii=False)


def github(monkeypatch, pages: dict):
    """URL → (状態, 本文) の偽の GitHub。本物と同じく、本文は limit バイトで切れる。読んだ URL と limit を返す。"""
    seen = []
    monkeypatch.setenv("GITHUB_REPOSITORY", REPO)
    monkeypatch.delenv("MONITOR_EXPECT_SHA", raising=False)

    def fake(url, headers=None, limit=check.READ_LIMIT):
        seen.append((url.removeprefix(API), limit))
        status, body = pages.get(url.removeprefix(API), (404, "{}"))
        return status, body.encode("utf-8")[:limit].decode("utf-8", "replace")
    monkeypatch.setattr(check, "http_get", fake)
    return seen


LIST = "commits?sha=main&path=sales&per_page=15"


def page(sha, n=1):
    return f"commits/{sha}?page={n}"


def test_large_commit_is_read(monkeypatch):
    big = detail("sales/src/barnull_sales/attendance.py", patch_bytes=285_469)   # d43bde7 と同じくらい
    assert len(big.encode("utf-8")) > check.READ_LIMIT
    sha = "d43bde7" + "0" * 33
    seen = github(monkeypatch, {LIST: (200, json.dumps([commit(sha)])), page(sha): (200, big)})
    assert check.latest_deploy_commit() == (sha, datetime(2026, 11, 26, 16, 6, tzinfo=UTC))
    assert seen and all(limit == check.GITHUB_READ_LIMIT for _, limit in seen)


def test_truncated_commit_is_reported_not_crashed(monkeypatch):
    """2026-11-27 07:51 の失敗: 本文が 200KB で切れて JSONDecodeError になり、監視ごと落ちた。今は理由つきの LookupError。"""
    github(monkeypatch, {LIST: (200, json.dumps([commit("abc")])),
                         page("abc"): (200, detail("sales/src/x.py", patch_bytes=285_469))})
    monkeypatch.setattr(check, "GITHUB_READ_LIMIT", 200_000)          # 前の上限に戻すと切れる
    with pytest.raises(LookupError, match="応答を読めない"):
        check.latest_deploy_commit()


def test_docs_only_commit_is_skipped(monkeypatch):
    github(monkeypatch, {LIST: (200, json.dumps([commit("new"), commit("old", "2026-11-25T00:00:00Z")])),
                         page("new"): (200, detail("sales/docs/runbook.md", "sales/report/a.html", "sales/README.md")),
                         page("old"): (200, detail("sales/src/barnull_sales/jobs.py"))})
    assert check.latest_deploy_commit()[0] == "old"


def test_no_deploy_commit_in_recent_ones(monkeypatch):
    github(monkeypatch, {LIST: (200, json.dumps([commit("a")])), page("a"): (200, detail("sales/docs/x.md"))})
    assert check.latest_deploy_commit() is None                       # 異常ではない（直近はすべて文書だけ）
    github(monkeypatch, {LIST: (200, "[]")})
    assert check.latest_deploy_commit() is None


def test_many_files_are_read_page_by_page(monkeypatch):
    docs = [f"sales/docs/d{i}.md" for i in range(check.GITHUB_PAGE_FILES)]
    seen = github(monkeypatch, {LIST: (200, json.dumps([commit("big"), commit("old")])),
                                page("big"): (200, detail(*docs)),                    # 1ページ目は文書だけで満杯
                                page("big", 2): (200, detail("sales/src/a.py")),     # 2ページ目にコード
                                page("old"): (200, detail("sales/src/b.py"))})
    assert check.latest_deploy_commit()[0] == "big"                  # 前は 1 ページ目だけ見て「文書だけ」と誤っていた
    assert [u for u, _ in seen] == [LIST, page("big"), page("big", 2)]
    seen = github(monkeypatch, {LIST: (200, json.dumps([commit("small"), commit("old")])),
                                page("small"): (200, detail(*docs[:-1])),           # 満杯でなければ続きは読まない
                                page("old"): (200, detail("sales/src/b.py"))})
    assert check.latest_deploy_commit()[0] == "old"
    assert [u for u, _ in seen] == [LIST, page("small"), page("old")]


@pytest.mark.parametrize(("pages", "why"), [
    ({LIST: (500, "oops")}, "HTTP 500"),                                                    # 一覧に届かない
    ({LIST: (None, "URLError")}, "URLError"),                                               # 名前解決・接続の失敗
    ({LIST: (200, "<html>")}, "応答を読めない"),                                             # 一覧が JSON でない
    ({LIST: (200, '{"message": "x"}')}, "形が違う"),                                        # 一覧が配列でない
    ({LIST: (200, '["x"]')}, "ID が無い"),
    ({LIST: (200, json.dumps([commit(123)]))}, "ID が無い"),                                 # ID が文字列でない
    ({LIST: (200, json.dumps([commit("a")])), page("a"): (403, "{}")}, "HTTP 403"),         # 回数の上限など
    ({LIST: (200, json.dumps([commit("a")])), page("a"): (200, "{")}, "応答を読めない"),
    ({LIST: (200, json.dumps([commit("a")])), page("a"): (200, "[]")}, "形が違う"),
    ({LIST: (200, json.dumps([commit("a")])), page("a"): (200, '{"files": null}')}, "一覧が無い"),
    ({LIST: (200, '[{"sha": "a"}]'), page("a"): (200, detail("sales/src/a.py"))}, "時刻が無い"),
    ({LIST: (200, json.dumps([commit("a", "2026-11-26T16:06:00")])),                        # 時差なし → 比べると TypeError
      page("a"): (200, detail("sales/src/a.py"))}, "時差が無い"),
])
def test_github_trouble_is_a_lookup_error(monkeypatch, pages, why):
    github(monkeypatch, pages)
    with pytest.raises(LookupError, match=why) as e:
        check.latest_deploy_commit()
    assert type(e.value) is LookupError and "https://" not in str(e.value) and REPO not in str(e.value)


def test_deadline(monkeypatch):
    github(monkeypatch, {LIST: (200, "[]")})
    monkeypatch.setattr(check, "GITHUB_DEADLINE", -1)
    with pytest.raises(LookupError, match="秒より長く"):
        check.latest_deploy_commit()


def run_main(monkeypatch, tmp_path, posts, reconcile_ago_min=5):
    monkeypatch.setenv("MONITOR_TARGETS", json.dumps([SALES]))
    monkeypatch.setenv("STATE_FILE", str(tmp_path / "s.json"))
    monkeypatch.setenv("MONITOR_EXPIRY_FILE", str(tmp_path / "none.toml"))
    stub(monkeypatch, 200, health(datetime.now(UTC), reconcile=reconcile_ago_min))   # main は本物の時計で見る
    monkeypatch.setattr(check.Slack, "call",
                        lambda self, m, p: posts.append({"method": m, **p}) or {"ok": True, "ts": "1"})
    return check.main()


def test_main_tells_when_deploy_check_keeps_failing(monkeypatch, tmp_path, capsys):
    """黙って【デプロイ失敗】を見張れなくならない: 続けて調べられなければ【監視の異常】、直れば【復旧】。"""
    monkeypatch.setattr(check, "CONSECUTIVE", 2)

    def fail():
        raise LookupError("GitHub の API に届かない（HTTP 403）")
    monkeypatch.setattr(check, "latest_deploy_commit", fail)
    posts = []
    assert run_main(monkeypatch, tmp_path, posts) == 0 and posts == []           # 1回だけなら黙る
    assert run_main(monkeypatch, tmp_path, posts) == 0
    assert len(posts) == 1 and posts[0]["text"].startswith("【監視の異常】GitHub から")
    assert "（GitHub の API に届かない（HTTP 403））" in posts[0]["text"] and "見張れていません" in posts[0]["text"]
    out = capsys.readouterr().out
    assert "今回は見ない" in out and "https://" not in out
    monkeypatch.setattr(check, "latest_deploy_commit", lambda: None)
    assert run_main(monkeypatch, tmp_path, posts) == 0
    assert "【復旧】" in posts[-2]["text"] and posts[-2]["thread_ts"] == "1"
    assert posts[-1]["method"] == "chat.update" and posts[-1]["text"].startswith("【復旧済み】【監視の異常】")


def test_main_keeps_watching_when_deploy_check_crashes(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(check, "CONSECUTIVE", 1)

    def boom():
        raise KeyError("files")                                       # 思わぬ形（コードの誤りなど）
    monkeypatch.setattr(check, "latest_deploy_commit", boom)
    posts = []
    assert run_main(monkeypatch, tmp_path, posts, reconcile_ago_min=80) == 0    # 定期処理は 80 分止まっている
    heads = {p["text"].split("】")[0] + "】" for p in posts}
    assert heads == {"【定期処理の停止】", "【監視の異常】"}                       # ほかの監視は続けて知らせる
    assert "思わぬ失敗（KeyError）" in capsys.readouterr().out


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
    monkeypatch.setenv("SLACK_MENTION", "UALERT")      # 残っていても使わない（監視の通知はメンションしない。2026-11-23 本人）
    s = FakeSlack()
    st = check.notify({}, P, s, NOW)                                # 1回目は知らせない
    assert not s.posts and st["売上:billing"]["count"] == 1
    st = check.notify(st, P, s, NOW + timedelta(minutes=15))
    assert s.posts[-1]["thread_ts"] is None and s.posts[-1]["text"].startswith("【課金停止】")
    st = check.notify(st, P, s, NOW + timedelta(hours=4))           # 8時間までは黙る
    assert len(s.posts) == 1
    st = check.notify(st, P, s, NOW + timedelta(hours=8, minutes=16))
    assert s.posts[-1]["thread_ts"] == "ts1" and s.posts[-1]["text"].startswith("まだ続いています")
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
    monkeypatch.setenv("SLACK_MENTION", "UALERT")
    s = FakeSlack()
    assert check.expiry_backup({}, [], s, NOW) == {} and not s.posts                 # worker が元気なら worker に任せる
    down = [{"key": "売上:billing"}]
    st = check.expiry_backup({}, down, s, NOW)
    assert s.posts[-1]["text"].startswith("【期限が近い】") and "外部監視から" in s.posts[-1]["text"] and list(st.values()) == [7]
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


def test_page_limit(monkeypatch):
    docs = detail(*[f"sales/docs/d{i}.md" for i in range(check.GITHUB_PAGE_FILES)])
    pages = {page("huge", n): (200, docs) for n in range(1, 12)}       # 文書だけで 3300 件（GitHub の上限を超える）
    pages |= {LIST: (200, json.dumps([commit("huge"), commit("old")])), page("old"): (200, detail("sales/src/b.py"))}
    seen = github(monkeypatch, pages)
    assert check.latest_deploy_commit()[0] == "old"
    assert [u for u, _ in seen] == [LIST, *[page("huge", n) for n in range(1, 11)], page("old")]   # 10 ページで止める


def test_no_repository_means_no_deploy_check(monkeypatch):
    seen = github(monkeypatch, {})
    monkeypatch.delenv("GITHUB_REPOSITORY")
    assert check.latest_deploy_commit() is None and seen == []


def deploy_alert(now):
    """通知済みの【デプロイ失敗】の状態。"""
    t = (now - timedelta(hours=1)).isoformat()
    return {"売上:deploy": {"count": 3, "since": t, "ts": "D1", "text": "【デプロイ失敗】…", "notified_at": t}}


def test_deploy_alert_is_kept_while_github_fails(monkeypatch, tmp_path):
    """GitHub を調べられない回に、続いている【デプロイ失敗】へ【復旧】を書かない（2回目のレビューの指摘）。"""
    before = deploy_alert(datetime.now(UTC))
    (tmp_path / "s.json").write_text(json.dumps(before), encoding="utf-8")

    def fail():
        raise LookupError("GitHub の API に届かない（HTTP 503）")
    monkeypatch.setattr(check, "latest_deploy_commit", fail)
    posts = []
    assert run_main(monkeypatch, tmp_path, posts) == 0
    assert posts == []                                                # 【復旧】も【復旧済み】も出さない
    st = json.loads((tmp_path / "s.json").read_text(encoding="utf-8"))
    assert st["売上:deploy"] == before["売上:deploy"]                    # そのまま持ち越す
    assert st["GitHub:deploy-check"]["count"] == 1
    old = datetime.now(UTC) - timedelta(hours=2)
    monkeypatch.setattr(check, "latest_deploy_commit", lambda: ("abc1234" + "0" * 33, old))   # 調べられて、反映も済んだ
    assert run_main(monkeypatch, tmp_path, posts) == 0
    assert [p["thread_ts"] for p in posts if p["method"] == "chat.postMessage"] == ["D1"] and "【復旧】" in posts[0]["text"]
    assert set(json.loads((tmp_path / "s.json").read_text(encoding="utf-8"))) == {"_expiry"}


@pytest.mark.parametrize("body", [
    {"ok": True, "version": "abc1234", "jobs": ["reconcile"]},                       # jobs が配列
    {"ok": True, "version": "abc1234", "jobs": {"reconcile": "2026-11-27T10:00:00"}},  # 時差なし
    {"ok": True, "version": "abc1234", "jobs": {"reconcile": None}},
    {"ok": True, "version": 1234567, "jobs": {}},                                      # version が数
])
def test_odd_health_does_not_stop_the_monitor(monkeypatch, tmp_path, capsys, body):
    monkeypatch.setattr(check, "CONSECUTIVE", 1)
    now = datetime.now(UTC)
    st = {"売上:job:reconcile": {"count": 2, "since": now.isoformat(), "ts": "J1", "text": "【定期処理の停止】…",
                                  "notified_at": now.isoformat()}}
    (tmp_path / "s.json").write_text(json.dumps(st), encoding="utf-8")
    monkeypatch.setenv("MONITOR_TARGETS", json.dumps([SALES, {"name": "node-api", "url": "u2", "kind": "plain"}]))
    monkeypatch.setenv("STATE_FILE", str(tmp_path / "s.json"))
    monkeypatch.setenv("MONITOR_EXPIRY_FILE", str(tmp_path / "none.toml"))
    monkeypatch.setattr(check, "latest_deploy_commit", lambda: ("def5678" + "0" * 33, now - timedelta(hours=2)))
    monkeypatch.setattr(check, "http_get", lambda url, headers=None, limit=check.READ_LIMIT:
                        (200, json.dumps(body)) if url == SALES["url"] else (None, "TimeoutError"))
    posts = []
    monkeypatch.setattr(check.Slack, "call", lambda self, m, p: posts.append({"method": m, **p}) or {"ok": True, "ts": "1"})
    assert check.main() == 0
    heads = sorted(p["text"].split("】")[0] + "】" for p in posts)
    assert heads == ["【停止】", "【監視の異常】"]                            # ほかの対象も見て知らせる。【復旧】は出さない
    assert "/health の中身を読めません" in next(p["text"] for p in posts if p["text"].startswith("【監視の異常】"))
    saved = json.loads((tmp_path / "s.json").read_text(encoding="utf-8"))
    assert saved["売上:job:reconcile"] == st["売上:job:reconcile"]                # 確かめられなかったので持ち越す
    out = capsys.readouterr().out
    assert "中身が思わぬ形" in out and "https://" not in out


def test_crash_keeps_health_alerts_but_recovers_down(monkeypatch, tmp_path):
    """/health の中身で失敗した回: 【定期処理の停止】は持ち越し、応答があったので【停止】は【復旧】にする（3回目のレビューの指摘）。"""
    now = datetime.now(UTC)
    alert = {"count": 3, "since": now.isoformat(), "notified_at": now.isoformat()}
    st = {"売上:down": {**alert, "ts": "S1", "text": "【停止】…"},
          "売上:job:reconcile": {**alert, "ts": "J1", "text": "【定期処理の停止】…"},
          "売上:deploy": {**alert, "ts": "D1", "text": "【デプロイ失敗】…"}}
    (tmp_path / "s.json").write_text(json.dumps(st), encoding="utf-8")
    monkeypatch.setattr(check, "latest_deploy_commit", lambda: None)
    posts = []
    stub_body = json.dumps({"ok": True, "version": "abc1234", "jobs": ["reconcile"]})       # 形が変わった /health
    monkeypatch.setenv("MONITOR_TARGETS", json.dumps([SALES]))
    monkeypatch.setenv("STATE_FILE", str(tmp_path / "s.json"))
    monkeypatch.setenv("MONITOR_EXPIRY_FILE", str(tmp_path / "none.toml"))
    stub(monkeypatch, 200, stub_body)
    monkeypatch.setattr(check.Slack, "call", lambda self, m, p: posts.append({"method": m, **p}) or {"ok": True, "ts": "1"})
    assert check.main() == 0
    recovered = [p for p in posts if p["method"] == "chat.postMessage" and p["text"].startswith("【復旧】")]
    assert [p["thread_ts"] for p in recovered] == ["S1"]
    saved = json.loads((tmp_path / "s.json").read_text(encoding="utf-8"))
    assert "売上:down" not in saved
    assert saved["売上:job:reconcile"] == st["売上:job:reconcile"] and saved["売上:deploy"] == st["売上:deploy"]


def test_deadline_between_pages(monkeypatch):
    docs = detail(*[f"sales/docs/d{i}.md" for i in range(check.GITHUB_PAGE_FILES)])
    seen = github(monkeypatch, {LIST: (200, json.dumps([commit("big")])), page("big"): (200, docs),
                                page("big", 2): (200, detail("sales/src/a.py"))})
    clock = iter([0, 0, 0, 61])                                       # 期限を決める・一覧・1ページ目・2ページ目の前
    monkeypatch.setattr(check, "time", types.SimpleNamespace(monotonic=lambda: next(clock)))
    with pytest.raises(LookupError, match="60 秒より長く"):
        check.latest_deploy_commit()
    assert [u for u, _ in seen] == [LIST, page("big")]


def test_http_get_reads_up_to_limit(monkeypatch):
    """本物の http_get と urllib で、上限まで読むこと・5xx の本文も読むことを確かめる（手元の HTTP サーバー）。"""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # 手元のプロキシ設定に左右されない
    monkeypatch.setattr(check.urllib.request, "urlopen", opener.open)
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = b"x" * 300_000 if self.path == "/big" else b"billing is disabled"
            self.send_response(200 if self.path == "/big" else 503)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        assert check.http_get(f"{base}/big") == (200, "x" * check.READ_LIMIT)               # 監視先は 200KB まで
        assert check.http_get(f"{base}/big", limit=check.GITHUB_READ_LIMIT) == (200, "x" * 300_000)
        assert check.http_get(f"{base}/down") == (503, "billing is disabled")
    finally:
        server.shutdown()
        server.server_close()


def test_http_get_5xx_body_cut_is_still_down(monkeypatch):
    """5xx の本文を読む途中で切れても、【監視の異常】ではなく【停止】（3回目のレビューの指摘）。"""
    class Cut(io.BytesIO):
        def read(self, *a):
            raise TimeoutError

    def urlopen(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 503, "Service Unavailable", {}, Cut())
    monkeypatch.setattr(check.urllib.request, "urlopen", urlopen)
    assert check.http_get("https://x/health") == (503, "")
    assert keys(check.check_target(SALES, NOW)) == ["売上:down"]


def alerts(now, *keys_):
    a = {"count": 3, "since": now.isoformat(), "notified_at": now.isoformat()}
    return {k: {**a, "ts": f"T{i}", "text": f"【{k}】…"} for i, k in enumerate(keys_)}


def setup_main(monkeypatch, tmp_path, st, http, targets=(SALES,)):
    (tmp_path / "s.json").write_text(json.dumps(st), encoding="utf-8")
    monkeypatch.setenv("MONITOR_TARGETS", json.dumps(list(targets)))
    monkeypatch.setenv("STATE_FILE", str(tmp_path / "s.json"))
    monkeypatch.setenv("MONITOR_EXPIRY_FILE", str(tmp_path / "none.toml"))
    monkeypatch.setattr(check, "CONSECUTIVE", 1)
    monkeypatch.setattr(check, "latest_deploy_commit", lambda: None)
    monkeypatch.setattr(check, "http_get", http)
    posts = []
    monkeypatch.setattr(check.Slack, "call", lambda self, m, p: posts.append({"method": m, **p}) or {"ok": True, "ts": "1"})
    return posts


def answer(status, body):
    return lambda url, headers=None, limit=check.READ_LIMIT: (status, body)


@pytest.mark.parametrize(("status", "body", "new", "title"), [
    (503, "Service Unavailable", "売上:down", "【停止】"),
    (None, "TimeoutError", "売上:down", "【停止】"),
    (500, check.RECORDED_BILLING_BODY, "売上:billing", "【課金停止】"),
    (200, json.dumps({"ok": False, "jobs": None}), "売上:health", "【監視の異常】"),
])
def test_job_and_deploy_alerts_are_kept_while_health_is_unknown(monkeypatch, tmp_path, status, body, new, title):
    """止まっている・/health を読めない回に、【定期処理の停止】【デプロイ失敗】へ【復旧】を書かない（4回目のレビューの指摘）。"""
    st = alerts(datetime.now(UTC), "売上:job:reconcile", "売上:deploy")
    posts = setup_main(monkeypatch, tmp_path, st, answer(status, body))
    assert check.main() == 0
    assert [p["text"].split("】")[0] + "】" for p in posts] == [title]       # 新しい異常だけ。【復旧】は出さない
    saved = json.loads((tmp_path / "s.json").read_text(encoding="utf-8"))
    assert saved["売上:job:reconcile"] == st["売上:job:reconcile"] and saved["売上:deploy"] == st["売上:deploy"]
    assert new in saved


def test_alerts_recover_once_health_is_read_again(monkeypatch, tmp_path):
    st = alerts(datetime.now(UTC), "売上:down", "売上:job:reconcile", "売上:deploy")
    posts = setup_main(monkeypatch, tmp_path, st, answer(200, health(datetime.now(UTC), reconcile=5)))
    assert check.main() == 0
    assert sorted(p["thread_ts"] for p in posts if p["method"] == "chat.postMessage") == ["T0", "T1", "T2"]   # すべて【復旧】
    assert set(json.loads((tmp_path / "s.json").read_text(encoding="utf-8"))) == {"_expiry"}


def test_bad_url_is_down_not_answered(monkeypatch, tmp_path):
    """URL の誤り（scheme なし）は「応答なし」。出ている【停止】に【復旧】を書かない（4回目のレビューの指摘）。"""
    assert check.http_get("no-scheme/health") == (None, "ValueError")
    st = alerts(datetime.now(UTC), "n:down")
    posts = setup_main(monkeypatch, tmp_path, st, check.http_get, targets=({"name": "n", "url": "no-scheme", "kind": "plain"},))
    assert check.main() == 0 and posts == []
    assert json.loads((tmp_path / "s.json").read_text(encoding="utf-8"))["n:down"]["count"] == 4


@pytest.mark.parametrize("broken", ["{", "[]", '"x"'])
def test_broken_state_does_not_stop_the_monitor(monkeypatch, tmp_path, capsys, broken):
    posts = setup_main(monkeypatch, tmp_path, {}, answer(503, "x"))
    (tmp_path / "s.json").write_text(broken, encoding="utf-8")
    assert check.main() == 0
    assert posts[0]["text"].startswith("【停止】") and "前回の状態を読めない" in capsys.readouterr().out
    assert "売上:down" in json.loads((tmp_path / "s.json").read_text(encoding="utf-8"))


def test_expiry_failure_still_saves_state(monkeypatch, tmp_path, capsys):
    """期限の設定が読めなくても、通知した状態は残す（残さないと次の回に同じ親メッセージが重なる。4回目のレビューの指摘）。"""
    posts = setup_main(monkeypatch, tmp_path, {"_expiry": {"k|t": 7}}, answer(503, "x"))
    bad = tmp_path / "bad.toml"
    bad.write_text("[[monitor.expiry]]\nname = 'x'\nexpires = 'not a date'\n", encoding="utf-8")
    monkeypatch.setenv("MONITOR_EXPIRY_FILE", str(bad))
    assert check.main() == 1 and posts[0]["text"].startswith("【停止】")     # 設定の誤りは GitHub からのメールで気づく
    saved = json.loads((tmp_path / "s.json").read_text(encoding="utf-8"))
    assert saved["売上:down"]["ts"] == "1" and saved["_expiry"] == {"k|t": 7}
    assert "読めない項目がある（ValueError）" in capsys.readouterr().out


def test_one_broken_expiry_item_does_not_repeat_the_others(monkeypatch, tmp_path, capsys):
    """期限の設定の 1 件が壊れていても、ほかの項目は 1 回だけ知らせ、その状態を残す（5回目のレビューの指摘）。"""
    posts = setup_main(monkeypatch, tmp_path, {}, answer(503, "x"))
    soon = (datetime.now(UTC) + timedelta(days=5)).isoformat()
    toml = tmp_path / "exp.toml"
    toml.write_text(f'[[monitor.expiry]]\nname = "A"\nexpires = "{soon}"\n\n[[monitor.expiry]]\nname = "B"\n', encoding="utf-8")
    monkeypatch.setenv("MONITOR_EXPIRY_FILE", str(toml))
    assert check.main() == 1 and check.main() == 1
    assert [p["text"][:10] for p in posts if p["text"].startswith("【期限")] == ["【期限が近い】A の"]
    assert list(json.loads((tmp_path / "s.json").read_text(encoding="utf-8"))["_expiry"].values()) == [7]
    assert capsys.readouterr().out.count("読めない項目がある（KeyError）") == 2


def test_unreadable_expiry_file_fails_the_run(monkeypatch, tmp_path, capsys):
    posts = setup_main(monkeypatch, tmp_path, {}, answer(503, "x"))
    toml = tmp_path / "exp.toml"
    toml.write_text("[[monitor.expiry]\n", encoding="utf-8")                         # 書き方の誤り
    monkeypatch.setenv("MONITOR_EXPIRY_FILE", str(toml))
    assert check.main() == 1 and posts[0]["text"].startswith("【停止】")
    assert "設定を読めない（TOMLDecodeError）" in capsys.readouterr().out
    assert "売上:down" in json.loads((tmp_path / "s.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize(("open_key", "status", "body"), [
    ("売上:billing", None, "TimeoutError"),                  # 課金停止の最中に応答なし → 課金停止は直っていない
    ("売上:billing", 503, ""),                               # 5xx の本文を読めなかった
    ("売上:down", 500, check.RECORDED_BILLING_BODY),          # 停止の最中に課金停止 → 停止も直っていない
    ("売上:health", 503, "Service Unavailable"),              # /health を読めない最中に停止 → 読めるかは分からない
])
def test_other_down_alerts_are_kept_while_down(monkeypatch, tmp_path, open_key, status, body):
    st = alerts(datetime.now(UTC), open_key)
    posts = setup_main(monkeypatch, tmp_path, st, answer(status, body))
    assert check.main() == 0
    assert not [p for p in posts if "復旧" in p["text"]]
    assert json.loads((tmp_path / "s.json").read_text(encoding="utf-8"))[open_key] == st[open_key]


def test_down_and_billing_recover_when_health_is_unreadable(monkeypatch, tmp_path):
    """/health を読めない（応答はある）回は、【停止】【課金停止】は直ったので【復旧】を書く。"""
    st = alerts(datetime.now(UTC), "売上:down", "売上:billing")
    posts = setup_main(monkeypatch, tmp_path, st, answer(200, json.dumps({"ok": False, "jobs": None})))
    assert check.main() == 0
    assert sorted(p["thread_ts"] for p in posts if p["text"].startswith("【復旧】")) == ["T0", "T1"]


@pytest.mark.parametrize("broken", [
    {"name": "売上2", "URL": "https://secret.example/health", "kind": "sales"},   # url の綴りの誤り
    {"url": "https://secret.example/health"},                                     # name が無い
    "売上2",                                                                       # 要素が文字列
    {"name": "", "url": "https://secret.example/health"},
])
def test_broken_target_is_a_config_error_not_a_recovery(monkeypatch, tmp_path, capsys, broken):
    """監視先の設定の誤り: 叩いていない対象に【復旧】を書かず、終了コード 1（7回目のレビューの指摘）。ほかの対象は見る。"""
    st = alerts(datetime.now(UTC), "売上:down", "売上2:down")
    posts = setup_main(monkeypatch, tmp_path, st, answer(200, health(datetime.now(UTC), reconcile=5)),
                       targets=(SALES, broken))
    assert check.main() == 1
    assert [p["thread_ts"] for p in posts if p["text"].startswith("【復旧】")] == ["T0"]   # 叩けた 売上 だけ直った
    saved = json.loads((tmp_path / "s.json").read_text(encoding="utf-8"))
    assert saved["売上2:down"] == st["売上2:down"] and "売上:down" not in saved
    out = capsys.readouterr().out
    assert "MONITOR_TARGETS）に読めない要素が 1 件" in out and "secret.example" not in out


def test_all_targets_broken(monkeypatch, tmp_path):
    st = alerts(datetime.now(UTC), "売上:down", "GitHub:deploy-check")
    posts = setup_main(monkeypatch, tmp_path, st, answer(200, "{}"), targets=({"name": "売上"},))
    assert check.main() == 1 and posts == []
    saved = json.loads((tmp_path / "s.json").read_text(encoding="utf-8"))
    assert saved["売上:down"] == st["売上:down"] and saved["GitHub:deploy-check"] == st["GitHub:deploy-check"]
