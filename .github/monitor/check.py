"""null システムの外部監視（GitHub Actions の system-monitor から 15 分ごとに動く）。標準ライブラリだけで動く。

GCP の外から各サービスを叩き、止まっていたら Slack の #null_system-monitor に知らせる（sales/docs/monitoring_requirements.md）。
  - 課金停止: 本文に「billing is disabled」         → 【課金停止】
  - 停止:     応答なし・5xx                          → 【停止】
  - 定期処理: /health の jobs が古い                → 【定期処理の停止】
  - 状態:     /health を読めない（ok=false など）    → 【監視の異常】
  - デプロイ: main の最新コミットが 40 分たっても動いていない → 【デプロイ失敗】
              GitHub から最新コミットを調べられない        → 【監視の異常】（その回はデプロイの確認だけ飛ばす）
同じ異常が CONSECUTIVE 回続いたら、本人をメンションした親メッセージを出す。続く間は RENOTIFY_MIN 分ごとにスレッドへ
リマインド、直ったらスレッドに【復旧】を書き、親の先頭に【復旧済み】を付ける。
状態（通知中の異常と親の ts）は STATE_FILE（GitHub Actions のキャッシュ）に置く。

公開リポジトリなので、ログには URL・本文を出さない（異常の種類と対象の名前だけ）。
Slack に投稿できなかったら・監視先の設定（MONITOR_TARGETS）や秘密情報の期限の設定に読めない要素があれば
終了コード 1（GitHub から本人にメールが届く）。読めない要素は飛ばし、ほかの監視・通知は続ける。
その回に確かめられなかった異常は【復旧】にせず持ち越す（sales/docs/monitoring_requirements.md の (3)）。

環境変数:
  MONITOR_TARGETS  JSON の配列（GitHub の Secrets）。要素: {"name": 表示名, "url": 叩く URL,
                   "kind": "sales"（/health。jobs と version を見る）か "plain"（応答だけ見る）, "deploy": true/false}
  MONITOR_EXTRA_TARGETS  試験用に足す対象（同じ形。手動実行の入力から）
  SLACK_BOT_TOKEN, SLACK_CHANNEL（監視の通知では本人をメンションしない。2026-11-23 本人）
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
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

JST = timezone(timedelta(hours=9), "JST")   # tzdata の無い環境（Windows）でも動くように固定の時差
TIMEOUT = 10
READ_LIMIT = 200_000            # 監視先の応答はこれだけ読めば足りる
GITHUB_READ_LIMIT = 20_000_000  # GitHub のコミットの詳細は差分の本文を含み、大きなコミットでは 200KB を超える（d43bde7 は 285KB）
GITHUB_PAGE_FILES = 300         # コミットの詳細は、変えたファイルを 1 ページに 300 件まで返す（page= で続き。3000 件まで）
GITHUB_DEADLINE = 60            # デプロイの確認に使う時間の上限（秒）。TIMEOUT は 1 回の読み取りごとで、ジョブは 5 分で切られる
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
def http_get(url: str, headers: dict | None = None, limit: int = READ_LIMIT) -> tuple[int | None, str]:
    """(状態コード, 本文)。本文は limit バイトまで。届かなければ (None, 理由)。"""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "null-system-monitor", **(headers or {})})  # URL の誤りも「応答なし」
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return r.status, r.read(limit).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        try:
            body = e.read(limit).decode("utf-8", "replace")
        except Exception:   # noqa: BLE001  5xx の本文を読む途中で切れても、状態コードで判定する（【停止】のまま）
            body = ""
        return e.code, body
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
                 "text": f"{name}: Cloud Run が「課金が無効」と応答しています。処理がすべて止まっています。\n"
                         "まず Google Cloud の「お支払い」で「利用額上限が適用されたため、サービスが一時停止」の表示を確かめる"
                         "（2026-10-28・11-13 の停止の原因。billingEnabled は true のままでも止まる）。"
                         "上限の解除・引き上げか、請求先アカウントのつなぎ直しは本人の作業。"
                         "戻した後も Cloud Run に反映されるまで時間がかかることがある。"}]
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
                 "text": f"{name}: /health から状態を読めません（{why}）。定期処理が動いているか確かめられません。"}]
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
    """main で、デプロイの対象になる最新のコミット（sha, 時刻）。直近 15 件に無ければ None。試験では MONITOR_EXPECT_SHA で上書きする。
    GitHub から読めない・応答の形が違うときは LookupError（理由だけ。公開ログに出るので URL・本文は入れない）。
    main がその回のデプロイの確認だけ飛ばし、続けば【監視の異常】で知らせる。"""
    if os.environ.get("MONITOR_EXPECT_SHA"):
        return os.environ["MONITOR_EXPECT_SHA"], datetime.now(UTC) - timedelta(days=1)
    repo, token = os.environ.get("GITHUB_REPOSITORY"), os.environ.get("GITHUB_TOKEN")
    if not repo:
        return None
    headers = {"Accept": "application/vnd.github+json", **({"Authorization": f"Bearer {token}"} if token else {})}
    deadline = time.monotonic() + GITHUB_DEADLINE

    def get(path: str, shape: type):
        if time.monotonic() > deadline:
            raise LookupError(f"GitHub の応答に {GITHUB_DEADLINE} 秒より長くかかった")
        status, body = http_get(f"https://api.github.com/repos/{repo}/{path}", headers, limit=GITHUB_READ_LIMIT)
        if status != 200:
            raise LookupError(f"GitHub の API に届かない（{body if status is None else f'HTTP {status}'}）")
        try:
            data = json.loads(body)
        except ValueError:
            raise LookupError(f"GitHub の応答を読めない（{len(body)} 文字。大きすぎて途中で切れた可能性）") from None
        if not isinstance(data, shape):
            raise LookupError(f"GitHub の応答の形が違う（{type(data).__name__}）")
        return data

    for c in get("commits?sha=main&path=sales&per_page=15", list):
        sha = c.get("sha") if isinstance(c, dict) else None
        if not isinstance(sha, str) or not sha:
            raise LookupError("GitHub の応答にコミットの ID が無い")
        for page in range(1, 11):
            files = get(f"commits/{sha}?page={page}", dict).get("files")
            if not isinstance(files, list):
                raise LookupError("GitHub の応答に変えたファイルの一覧が無い")
            names = [f.get("filename") for f in files if isinstance(f, dict)]
            if any(isinstance(n, str) and n.startswith(DEPLOY_INCLUDE) and not n.startswith(DEPLOY_EXCLUDE) for n in names):
                try:
                    at = iso(c["commit"]["committer"]["date"])
                except (KeyError, TypeError, AttributeError, ValueError):
                    raise LookupError("GitHub の応答にコミットの時刻が無い") from None
                if at.tzinfo is None:
                    raise LookupError("GitHub の応答のコミットの時刻に時差が無い")
                return sha, at
            if len(files) < GITHUB_PAGE_FILES:
                break
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


def notify(state: dict, problems: list[dict], slack: Slack, now: datetime,
           keep: Callable[[str], bool] | None = None) -> dict:
    """keep(鍵) が真の異常は、この回に確かめられなかったもの。problems に無くても【復旧】にせず、状態をそのまま持ち越す。"""
    current = {p["key"]: p for p in problems}
    new_state: dict = {}
    for key, p in current.items():
        s = dict(state.get(key) or {"count": 0, "since": now.isoformat()})
        s["count"] += 1
        if not s.get("ts") and s["count"] >= CONSECUTIVE:
            text = f"【{p['title']}】{p['text']}（{jst(iso(s['since']))} から）\n対応: runbook「システム監視」の【{p['title']}】"
            ts = slack.post(text)
            if ts:
                s |= {"ts": ts, "text": text, "notified_at": now.isoformat()}
            print(f"通知: {key} {p['title']}")
        elif s.get("ts") and now - iso(s["notified_at"]) >= RENOTIFY:
            if slack.post(f"まだ続いています（{span(iso(s['since']), now)}）。{p['text']}", thread_ts=s["ts"]):
                s["notified_at"] = now.isoformat()
            print(f"リマインド: {key}")
        else:
            print(f"継続: {key} {p['title']}（{s['count']}回目）")
        new_state[key] = s
    for key, s in state.items():
        if key in current:
            continue
        if keep and keep(key):          # この回は確かめられなかった: 直ったとは言えないので、そのまま持ち越す
            new_state[key] = s
            continue
        if s.get("ts"):
            ok = slack.post(f"【復旧】{jst(iso(s['since']))}〜{jst(now)}（{span(iso(s['since']), now)}）", thread_ts=s["ts"])
            slack.update(s["ts"], "【復旧済み】" + s.get("text", ""))
            if not ok:
                new_state[key] = s      # 書けなかったら次の回にもう一度
                continue
            print(f"復旧: {key}")
    return new_state


EXPIRY_STAGES = (30, 7, 1, 0)       # sales/src/barnull_sales/monitor.py と同じ
WORKER_DOWN = (":down", ":billing", ":health", ":job:daily")


class ExpiryBroken(Exception):
    """期限の設定に読めない項目があった。state はほかの項目を知らせた後の状態（失わずに残す）。"""

    def __init__(self, state: dict, broken: int) -> None:
        super().__init__(f"読めない項目 {broken} 件")
        self.state = state


def expiry_backup(state: dict, problems: list[dict], slack: Slack, now: datetime) -> dict:
    """秘密情報の期限（sales/sales.toml の [[monitor.expiry]]）の予備の通知。ふだんは worker の毎日のステップ secret_expiry が
    知らせるので、売上の worker が動いていない（停止・課金停止・毎日の処理が古い）ときだけ、ここから知らせる（二重に出さない）。
    戻り値: 知らせた段階 {鍵|期限: 段階}。読めない項目は飛ばしてほかを知らせ、最後に ExpiryBroken を投げる。"""
    import tomllib

    path = Path(os.environ.get("MONITOR_EXPIRY_FILE", "sales/sales.toml"))
    if not path.exists() or not any(p["key"].endswith(WORKER_DOWN) for p in problems):
        return state
    items = tomllib.loads(path.read_text(encoding="utf-8")).get("monitor", {}).get("expiry", [])
    state, broken = dict(state), 0
    for item in items:
        try:
            exp = datetime.fromisoformat(str(item["expires"]))
            exp = exp if exp.tzinfo else exp.replace(tzinfo=JST)
            key, left = f"{item['name']}|{exp.isoformat()}", exp - now
        except Exception as e:   # noqa: BLE001  1 件の誤りで、ほかの項目の通知を止めない
            print(f"秘密情報の期限の設定: 読めない項目がある（{type(e).__name__}）。飛ばす")
            broken += 1
            continue
        stage = next((d for d in reversed(EXPIRY_STAGES) if left <= timedelta(days=d)), None)
        if stage is None or (key in state and state[key] <= stage):
            continue
        head = (f"【期限切れ】{item['name']} の有効期限（{jst(exp)}）を過ぎています。" if stage == 0 else
                f"【期限が近い】{item['name']} の有効期限は {jst(exp)}（あと{max(left.days, 0)}日）です。")
        if slack.post(f"{head}（売上の worker が止まっているため、外部監視から知らせています）\n"
                      f"更新の手順: {item.get('renew', 'runbook「秘密情報の更新」')}"):
            state[key] = stage
            print(f"期限の通知（予備）: 段階 {stage}")
    if broken:
        raise ExpiryBroken(state, broken)
    return state


def main() -> int:
    now = datetime.now(UTC)
    targets = json.loads(os.environ.get("MONITOR_TARGETS") or "[]")
    targets += json.loads(os.environ.get("MONITOR_EXTRA_TARGETS") or "[]")
    if not targets:
        print("MONITOR_TARGETS が空")
        return 1
    commit, problems = None, []
    unknown: list[Callable[[str], bool]] = []      # この回に確かめられなかった異常の鍵（【復旧】にしない）
    config_broken = False                           # 設定の誤り。終了コード 1 にして GitHub からのメールで気づく
    valid = [t for t in targets if isinstance(t, dict) and isinstance(t.get("name"), str) and t["name"]
             and isinstance(t.get("url"), str) and t["url"]]
    if len(valid) < len(targets):
        # 監視先の設定の誤り: その対象は叩いていないので、出ている異常は【復旧】にせず持ち越す（URL は出さない）
        config_broken = True
        print(f"監視先の設定（MONITOR_TARGETS）に読めない要素が {len(targets) - len(valid)} 件ある。飛ばす")
        # 叩けた対象の鍵だけ確かめられる（GitHub の確認は、デプロイを見る対象が残っているときだけ動く）
        heads = tuple(f"{t['name']}:" for t in valid) + (("GitHub:",) if any(t.get("deploy") for t in valid) else ())
        unknown.append(lambda k: not k.startswith(heads))
        targets = valid
    if any(t.get("deploy") for t in targets):
        try:
            commit = latest_deploy_commit()
        except Exception as e:   # noqa: BLE001  デプロイの確認で落ちても、ほかの監視（停止・課金停止・定期処理）は続ける
            why = str(e) if type(e) is LookupError else f"思わぬ失敗（{type(e).__name__}）"
            print(f"デプロイの確認: {why}。今回は見ない")
            # 続けば知らせる（黙って見張れなくならないように）。出ている【デプロイ失敗】は持ち越す
            problems.append({"key": "GitHub:deploy-check", "title": "監視の異常",
                             "text": f"GitHub からデプロイの対象のコミットを調べられません（{why}）。"
                                     "そのあいだ【デプロイ失敗】は見張れていません（停止・課金停止・定期処理の監視は続けています）。"})
            unknown.append(lambda k: k.endswith(":deploy"))
    for t in targets:
        name = t.get("name", "?") if isinstance(t, dict) else "?"
        head = f"{name}:"
        try:
            found = check_target(t, now, commit)
        except Exception as e:   # noqa: BLE001  /health の中身が思わぬ形でも、ほかの対象と通知は続ける
            print(f"{name}: 中身が思わぬ形（{type(e).__name__}）")
            # 鍵は「/health を読めない」と同じ（毎日の処理を確かめられないので、秘密情報の期限の予備の通知も出す。
            # worker が元気なら期限の通知が重なるが、見逃すよりよい）
            found = [{"key": head + "health", "title": "監視の異常",
                      "text": f"{name}: /health の中身を読めません（{type(e).__name__}）。定期処理が動いているか確かめられません。"}]
        problems += found
        # この回に確かめられなかった異常は、直ったとは言えないので【復旧】にせず持ち越す
        got = {p["key"] for p in found}
        if got & {head + "down", head + "billing"}:
            # 応答が無い・課金停止: /health の中身（定期処理・デプロイ・読めるか）も、止まり方の違い（停止か課金停止か）も分からない
            unknown.append(lambda k, head=head: k.startswith(head + "job:")
                           or k in (head + "deploy", head + "health", head + "down", head + "billing"))
        elif head + "health" in got:
            # /health を読めない: 応答はあり課金停止でもないので【停止】【課金停止】は直った。定期処理・デプロイは分からない
            unknown.append(lambda k, head=head: k.startswith(head + "job:") or k == head + "deploy")
    path = Path(os.environ.get("STATE_FILE", "state/monitor_state.json"))
    try:
        state = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        if not isinstance(state, dict):
            raise ValueError
    except Exception as e:   # noqa: BLE001  状態が壊れていても監視は止めない（出ている通知は新しく出し直しになる）
        print(f"前回の状態を読めない（{type(e).__name__}）。空から始める")
        state = {}
    slack = Slack()
    new_state = notify({k: v for k, v in state.items() if not k.startswith("_")}, problems, slack, now,
                       keep=lambda k: any(f(k) for f in unknown))
    # 期限の設定が読めなくても、通知した状態は必ず残す（残さないと次の回に同じ通知が重なる）。
    # 設定の誤りは終了コード 1 にして、GitHub からのメールで気づけるようにする
    try:
        new_state["_expiry"] = expiry_backup(state.get("_expiry") or {}, problems, slack, now)
    except ExpiryBroken as e:
        new_state["_expiry"], config_broken = e.state, True
    except Exception as e:   # noqa: BLE001
        print(f"秘密情報の期限の予備の通知: 設定を読めない（{type(e).__name__}）")
        new_state["_expiry"], config_broken = state.get("_expiry") or {}, True
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(new_state, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"対象 {len(targets)} 件、異常 {len(problems)} 件")
    return 1 if slack.failed or config_broken else 0


if __name__ == "__main__":
    sys.exit(main())
