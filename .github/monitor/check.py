"""null システムの外部監視（GitHub Actions の system-monitor から 15 分ごとに動く）。標準ライブラリだけで動く。

GCP の外から各サービスを叩き、止まっていたら Slack の #null_system-monitor に知らせる（sales/docs/monitoring_requirements.md）。
  - 課金停止: 本文に「billing is disabled」         → 【課金停止】
  - 停止:     応答なし・5xx                          → 【停止】
  - 定期処理: /healthz の jobs が古い                → 【定期処理の停止】
  - 状態:     /healthz を読めない（ok=false など）    → 【監視の異常】
  - デプロイ: main の最新コミットが 40 分たっても動いていない → 【デプロイ失敗】
同じ異常が CONSECUTIVE 回続いたら、本人をメンションした親メッセージを出す。続く間は RENOTIFY_MIN 分ごとにスレッドへ
リマインド、直ったらスレッドに【復旧】を書き、親の先頭に【復旧済み】を付ける。
状態（通知中の異常と親の ts）は STATE_FILE（GitHub Actions のキャッシュ）に置く。

公開リポジトリなので、ログには URL・本文を出さない（異常の種類と対象の名前だけ）。
Slack に投稿できなかったら終了コード 1（GitHub から本人にメールが届く）。

環境変数:
  MONITOR_TARGETS  JSON の配列（GitHub の Secrets）。要素: {"name": 表示名, "url": 叩く URL,
                   "kind": "sales"（/healthz。jobs と version を見る）か "plain"（応答だけ見る）, "deploy": true/false}
  MONITOR_EXTRA_TARGETS  試験用に足す対象（同じ形。手動実行の入力から）
  SLACK_BOT_TOKEN, SLACK_CHANNEL, SLACK_MENTION（ユーザー ID）
  GITHUB_TOKEN, GITHUB_REPOSITORY（デプロイの確認に使う）
  MONITOR_CONSECUTIVE（既定 2）, MONITOR_RENOTIFY_MIN（既定 480）, MONITOR_DEPLOY_GRACE_MIN（既定 40）
  MONITOR_EXPECT_SHA（試験用。デプロイの確認で「動いているべきコミット」を上書きする）
  試験用の対象に "simulate": "billing" を付けると、その応答を記録した課金停止の 500 に差し替える
  STATE_FILE（既定 state/monitor_state.json）, MONITOR_DRY（1 なら Slack に投稿せず内容を表示する）
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

JST = timezone(timedelta(hours=9), "JST")   # tzdata の無い環境（Windows）でも動くように固定の時差
TIMEOUT = 10
BILLING_TEXT = "billing is disabled"
RECORDED_BILLING_BODY = "The request failed because billing is disabled for this project."   # 2026-10-28〜11-15 の実際の応答
JOB_MAX_AGE = {"reconcile": timedelta(minutes=75), "daily": timedelta(hours=26), "weekly": timedelta(days=8)}
JOB_LABEL = {"reconcile": "30分ごとの処理", "daily": "毎日の処理", "weekly": "毎週の処理"}
# Cloud Build のトリガー sales-deploy と同じ対象（sales/** から下を除く）
DEPLOY_INCLUDE = ("sales/",)
DEPLOY_EXCLUDE = ("sales/report/", "sales/docs/", "sales/README.md")


def env_int(name: str, default: int) -> int:
    v = os.environ.get(name, "").strip()
    return int(v) if v else default


CONSECUTIVE = env_int("MONITOR_CONSECUTIVE", 2)
RENOTIFY = timedelta(minutes=env_int("MONITOR_RENOTIFY_MIN", 480))
DEPLOY_GRACE = timedelta(minutes=env_int("MONITOR_DEPLOY_GRACE_MIN", 40))


# ---------------------------------------------------------------- HTTP
def http_get(url: str, headers: dict | None = None) -> tuple[int | None, str]:
    """(状態コード, 本文)。届かなければ (None, 理由)。"""
    req = urllib.request.Request(url, headers={"User-Agent": "null-system-monitor", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return r.status, r.read(200_000).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read(200_000).decode("utf-8", "replace")
    except Exception as e:   # noqa: BLE001  タイムアウト・名前解決・接続拒否など
        return None, type(e).__name__


def iso(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def jst(t: datetime) -> str:
    return t.astimezone(JST).strftime("%m/%d %H:%M")


# ---------------------------------------------------------------- 判定
def check_target(t: dict, now: datetime, latest_commit=None) -> list[dict]:
    """対象1つの異常の一覧。要素: {key, title, text}。"""
    name, url = t["name"], t["url"]
    if t.get("simulate") == "billing":
        status, body = 500, RECORDED_BILLING_BODY
    else:
        status, body = http_get(url)
    if status is not None and status >= 500 and BILLING_TEXT in body.lower():
        return [{"key": f"{name}:billing", "title": "課金停止",
                 "text": f"{name}: Cloud Run が「課金が無効」と応答しています。GCP の課金が止まり、処理がすべて止まっています。\n"
                         "確かめ方: `gcloud billing projects describe <プロジェクト>` の billingEnabled。"
                         "再有効化した後も、Cloud Run に反映されるまで時間がかかることがあります（2026-11-15 は 30 分以上）。"}]
    if status is None or status >= 500:
        why = f"応答なし（{body}）" if status is None else f"HTTP {status}"
        return [{"key": f"{name}:down", "title": "停止", "text": f"{name}: サービスが応答しません（{why}）。"}]
    if t.get("kind") != "sales":
        return []
    try:
        h = json.loads(body)
    except ValueError:
        h = None
    if not isinstance(h, dict) or not h.get("ok") or h.get("jobs") is None:
        why = f"HTTP {status}" if not isinstance(h, dict) else "Firestore を読めない（ok=false）"
        return [{"key": f"{name}:health", "title": "監視の異常",
                 "text": f"{name}: /healthz から状態を読めません（{why}）。定期処理が動いているか確かめられません。"}]
    out = []
    for job, at in (h.get("jobs") or {}).items():
        limit = JOB_MAX_AGE.get(job)
        if limit and now - iso(at) > limit:
            out.append({"key": f"{name}:job:{job}", "title": "定期処理の停止",
                        "text": f"{name}: {JOB_LABEL.get(job, job)}（{job}）が {jst(iso(at))} から動いていません。"
                                "Cloud Scheduler（福岡は Cloud Tasks の連鎖）か sales-worker が止まっている可能性があります。"})
    if t.get("deploy") and latest_commit:
        sha, at = latest_commit
        version = h.get("version") or ""
        if version and not sha.startswith(version) and now - at > DEPLOY_GRACE:
            repo = os.environ.get("GITHUB_REPOSITORY", "")
            out.append({"key": f"{name}:deploy", "title": "デプロイ失敗",
                        "text": f"{name}: {jst(at)} のコミット {sha[:7]} が、{int((now - at).total_seconds() // 60)}分たっても"
                                f"動いていません（動いているのは {version}）。Cloud Build の履歴（リージョン us-central1）で"
                                f"失敗したステップを確かめてください。https://github.com/{repo}/commit/{sha}"})
    return out


def latest_deploy_commit() -> tuple[str, datetime] | None:
    """main で、デプロイの対象になる最新のコミット（sha, 時刻）。試験では MONITOR_EXPECT_SHA で上書きする。"""
    if os.environ.get("MONITOR_EXPECT_SHA"):
        return os.environ["MONITOR_EXPECT_SHA"], datetime.now(UTC) - timedelta(days=1)
    repo, token = os.environ.get("GITHUB_REPOSITORY"), os.environ.get("GITHUB_TOKEN")
    if not repo:
        return None
    headers = {"Accept": "application/vnd.github+json", **({"Authorization": f"Bearer {token}"} if token else {})}
    status, body = http_get(f"https://api.github.com/repos/{repo}/commits?sha=main&path=sales&per_page=15", headers)
    if status != 200:
        print(f"デプロイの確認: GitHub の API に届かない（{status}）。今回は見ない")
        return None
    for c in json.loads(body):
        status, detail = http_get(f"https://api.github.com/repos/{repo}/commits/{c['sha']}", headers)
        if status != 200:
            return None
        files = [f["filename"] for f in json.loads(detail).get("files", [])]
        if any(f.startswith(DEPLOY_INCLUDE) and not f.startswith(DEPLOY_EXCLUDE) for f in files):
            return c["sha"], iso(c["commit"]["committer"]["date"])
    return None


# ---------------------------------------------------------------- Slack
class Slack:
    def __init__(self) -> None:
        self.token = os.environ.get("SLACK_BOT_TOKEN", "")
        self.channel = os.environ.get("SLACK_CHANNEL", "")
        self.dry = os.environ.get("MONITOR_DRY") == "1"
        self.failed = False

    def call(self, method: str, payload: dict) -> dict | None:
        if self.dry:
            print(f"[dry] {method}: {payload.get('text', '')[:200]}")
            return {"ok": True, "ts": "dry"}
        req = urllib.request.Request(f"https://slack.com/api/{method}", data=json.dumps(payload).encode(),
                                     headers={"Authorization": f"Bearer {self.token}",
                                              "Content-Type": "application/json; charset=utf-8"})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                body = json.loads(r.read())
        except Exception as e:   # noqa: BLE001
            body = {"ok": False, "error": type(e).__name__}
        if not body.get("ok"):
            print(f"Slack の {method} に失敗: {body.get('error')}")
            self.failed = True
            return None
        return body

    def post(self, text: str, thread_ts: str | None = None) -> str | None:
        p = {"channel": self.channel, "text": text, "unfurl_links": False}
        if thread_ts:
            p["thread_ts"] = thread_ts
        body = self.call("chat.postMessage", p)
        return body.get("ts") if body else None

    def update(self, ts: str, text: str) -> None:
        self.call("chat.update", {"channel": self.channel, "ts": ts, "text": text})


# ---------------------------------------------------------------- 状態と通知
def span(a: datetime, b: datetime) -> str:
    m = int((b - a).total_seconds() // 60)
    return f"{m // 60}時間{m % 60}分" if m >= 60 else f"{m}分"


def notify(state: dict, problems: list[dict], slack: Slack, now: datetime) -> dict:
    mention = f"<@{os.environ['SLACK_MENTION']}> " if os.environ.get("SLACK_MENTION") else ""
    current = {p["key"]: p for p in problems}
    new_state: dict = {}
    for key, p in current.items():
        s = dict(state.get(key) or {"count": 0, "since": now.isoformat()})
        s["count"] += 1
        if not s.get("ts") and s["count"] >= CONSECUTIVE:
            text = f"{mention}【{p['title']}】{p['text']}（{jst(iso(s['since']))} から）\n対応: runbook「システム監視」の【{p['title']}】"
            ts = slack.post(text)
            if ts:
                s |= {"ts": ts, "text": text, "notified_at": now.isoformat()}
            print(f"通知: {key} {p['title']}")
        elif s.get("ts") and now - iso(s["notified_at"]) >= RENOTIFY:
            if slack.post(f"{mention}まだ続いています（{span(iso(s['since']), now)}）。{p['text']}", thread_ts=s["ts"]):
                s["notified_at"] = now.isoformat()
            print(f"リマインド: {key}")
        else:
            print(f"継続: {key} {p['title']}（{s['count']}回目）")
        new_state[key] = s
    for key, s in state.items():
        if key in current:
            continue
        if s.get("ts"):
            ok = slack.post(f"【復旧】{jst(iso(s['since']))}〜{jst(now)}（{span(iso(s['since']), now)}）", thread_ts=s["ts"])
            slack.update(s["ts"], "【復旧済み】" + s.get("text", ""))
            if not ok:
                new_state[key] = s      # 書けなかったら次の回にもう一度
                continue
            print(f"復旧: {key}")
    return new_state


def main() -> int:
    now = datetime.now(UTC)
    targets = json.loads(os.environ.get("MONITOR_TARGETS") or "[]")
    targets += json.loads(os.environ.get("MONITOR_EXTRA_TARGETS") or "[]")
    if not targets:
        print("MONITOR_TARGETS が空")
        return 1
    commit = latest_deploy_commit() if any(t.get("deploy") for t in targets) else None
    problems = [p for t in targets for p in check_target(t, now, commit)]
    path = Path(os.environ.get("STATE_FILE", "state/monitor_state.json"))
    state = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    slack = Slack()
    new_state = notify(state, problems, slack, now)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(new_state, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"対象 {len(targets)} 件、異常 {len(problems)} 件")
    return 1 if slack.failed else 0


if __name__ == "__main__":
    sys.exit(main())
