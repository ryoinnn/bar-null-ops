# 運用手順書（売上管理）

コマンドはリポジトリのルート（`C:\Users\ryoui\Documents\bar-null-ops`）の PowerShell で実行する。
gcloud は `gcloud.cmd` と打つ（この PC の PowerShell はスクリプトの実行が無効のため）。
0x0 の Workspace の設定で、gcloud は定期的にパスワードの再確認を求める（`Please enter your password:`）。
ふだんの変更は GitHub への push でデプロイされるので、gcloud を使うのは障害の調査と初期設定のときだけ。

## 全体の構成
| もの | 役割 |
|---|---|
| sales-webhook（Cloud Run、公開） | LINE の Webhook を受け、Cloud Tasks に積む。Square の許可の戻り先（普段は無効） |
| sales-worker（Cloud Run、非公開、1件ずつ処理） | メッセージの処理、定期ジョブ |
| Cloud Tasks `sales-events` | LINE のイベントを worker に回す（失敗したら最大10回までやり直し。最後も失敗したら Slack に原文つきで通知） |
| Cloud Scheduler | `sales-reconcile` 30分ごと／`sales-daily` 毎日 10:05／`sales-weekly` 毎週月曜 9:10（無料枠の3本） |
| Firestore | 状態の正本（伝票・決済と入金の割り当て・受け取ったイベントなど） |
| Secret Manager | `sales-config`（worker 用）、`sales-webhook-config`（webhook 用）、`billing-guard-config`（Slack のトークンだけ）、`square-oauth`（Square のトークン。自動更新） |
| Cloud Build トリガー `sales-deploy` | GitHub の main に `sales/` の変更が push されたら、テスト → デプロイ（`sales/cloudbuild.yaml`） |
| billing-guard（Cloud Run） | 費用が出たら請求先アカウントを外す（予算との接続が済むまで dry） |

Slack の通知は、本番が `#daily_barnull-sales`、週報が `#weekly_barnull-sales`、テストが `#test_barnull-sales`。

### 定期ジョブのステップ（ステップ名 = ログの `jsonPayload.job` = Slack の通知の「定期処理「…」」）
| ジョブ | ステップ |
|---|---|
| sales-reconcile | `manual_overrides`（Asana の手動上書き）→ `republish`（作りかけの伝票の Slack・Asana を作る）→ `resync`（反映に失敗した伝票をやり直す）→ `reconcile`（Square の決済・返金・伝票なし）→ `deposits`（銀行の入金・未収の振込） |
| sales-daily | `deposit_alerts`（入金未確認）→ `unpaid_reminders`（未収リマインド）→ `delete_old_events` |
| sales-weekly | `weekly_report` |

1つのステップが失敗しても次のステップは動く。伝票1件の失敗も、他の伝票の処理を止めない。

## Slack に出る障害の通知と対応
| 通知 | 意味 | まずやること |
|---|---|---|
| 【外部API障害】定期処理「X」が N回続けて失敗 | 連携先（Square・マネフォ・Slack・Asana など）の呼び出しが続けて失敗 | 「直近のエラー」を読む（下の表）。一時的なもの（429・5xx・通信エラー）なら待つ |
| 【処理エラー】定期処理「X」が N回続けて失敗 | プログラムの不具合か、想定外のデータ | ログを見る（下の「ログの見方」）。直して push |
| 【復旧】定期処理「X」が正常に戻りました | 上の通知の後で直った（1回だけ出る） | なし |
| 【処理できなかったメッセージ】 | LINE のメッセージを10回試しても処理できなかった | 原因を直し、メッセージを手で起票し直す（@null で送り直す） |

直近のエラーに出る言葉と対処:

| 言葉 | 原因 | 対処 |
|---|---|---|
| `Square の許可がまだです` / `Square ...: HTTP 401` | Square の許可が切れた・取り消された | 「Square の許可をやり直す」 |
| `MoneyForward ...: HTTP 401` | マネフォの APIキーが無効 | 新しい APIキーで「秘密情報の更新」 |
| `Asana ...: HTTP 401` | Asana のトークンが無効 | 新しいトークンで「秘密情報の更新」 |
| `Slack chat.postMessage: ... not_in_channel` / `invalid_auth` | Bot がチャンネルにいない・トークン無効 | チャンネルで `/invite @barnull_sales`、またはトークンを更新 |
| `HTTP 429` / `HTTP 5xx` / `通信エラー` | 相手側の一時的な障害（数回やり直した後でも失敗） | 待つ。直れば「【復旧】」が出る |

### ログの見方
Cloud Logging（コンソールの「ログエクスプローラ」）で、次のように絞り込む。ログは1行1件の JSON。

```
resource.labels.service_name="sales-worker"
severity>=WARNING
jsonPayload.job="deposits"              # ステップ名（通知の「定期処理「…」」）
jsonPayload.slip_id="S-20261017-001"    # 伝票 ID
jsonPayload.event_id="01J..."           # LINE のイベント
```
PowerShell からなら:
```powershell
gcloud.cmd logging read 'resource.labels.service_name="sales-worker" AND severity>=ERROR' --limit=20 --freshness=1d --project=barnull-sales
```
ログには顧客名・金額・本文を書かない（Slack のスレッドと Asana で見る）。

## よくある対応

### 伝票の状態を人の判断で進めたい・アラートを閉じたい
Asana のタスクの「手動上書き」を選ぶ。30分以内に反映され、Slack のスレッドに記録が残り、「なし」に戻る。

| 手動上書き | 使うとき |
|---|---|
| 決済確認済み | Square の決済を人が確認した（候補が複数・決済未確認などのアラートを閉じる） |
| 入金確認済み | 入金を人が確認した（入金未確認のアラートを閉じる。決済も確認済みにする） |
| 未収回収済み | 未収を人が確認した |
| 対象外（取消） | 伝票を取り消す。割り当てていた Square の決済は外れ、他の伝票に使える |

手動上書きは、その伝票のアラートをすべて閉じる（未収回収済みを除く）。

### 伝票ステータスの決まり方
状態は、事実から毎回計算する（`src/barnull_sales/slips.py` の `derive`）。

1. 取り消した → 対象外（完了）
2. 未解決のアラートがある → 要確認
3. 決済と入金が確認できた → 入金確認完了（未収も無くなれば完了。Asana のタスクも完了になる）
4. 決済が確認できた → 決済確認済み
5. それ以外 → 起票済み

全額未収（その日の決済なし）の伝票は、確認する決済・入金が無いので入金確認完了になり、回収できたら完了する。
完了した伝票はそのまま（後から来たアラートは Slack で知らせるだけ）。

### LINE で送ったのに起票されない
- `@null` で始まっているか。付け忘れは起票されない。決済が入っていれば、翌日の12時ごろに「【伝票なし】」が出る。
- グループが処理の対象か。対象外のグループで @null が送られると、Slack に「処理対象ではない LINE グループ」と groupId が出る（グループごとに1回）。
  対象にするなら `sales/sales.toml` の `line.allowed_group_ids` に追加して push。
- ログで `jsonPayload.job="process_event"` を見る。

### 設定を変える（担当者の名義、待ち時間など）
`sales/sales.toml` を直して GitHub の main に push する（例: 現金の照合のための `mf.staff_bank_names` にカナで追加）。
Cloud Build の結果はコンソールの「Cloud Build → 履歴」で見られる。テストが落ちるとデプロイされない。

### 秘密情報の更新（トークンを発行し直したとき）
```powershell
uv run python sales/scripts/set_secret.py ASANA_PAT        # 名前は変えたいものに置き換える
uv run python sales/infra/setup.py secrets deploy
```
どのサービスがどの秘密情報を使うかは `src/barnull_sales/config.py` の `SECRETS_BY_SERVICE`。

### Square の許可をやり直す
1. `sales/sales.toml` の `square.oauth_enabled` を `true` にして push（普段は false。許可の受け口を閉じておくため）。
2. ブラウザで `https://sales-webhook-752274rfja-uc.a.run.app/oauth/square/start` を開き、Bar null の Square アカウントで「許可」を押す。
3. `oauth_enabled` を `false` に戻して push。

## デプロイ
- ふだん: `sales/` の変更を GitHub の main に push する。Cloud Build がテストして、sales-worker と sales-webhook をデプロイする（数分）。
- requirements.txt を更新したとき: `cd sales; uv export --no-dev --no-hashes --no-emit-project -o requirements.txt`
- GitHub を使えないとき（手動）: `uv run python sales/infra/setup.py deploy`（同じ cloudbuild.yaml を、手元の sales/ から動かす）
- 戻したいとき: GitHub で直前のコミットを revert して push。急ぐならコンソールの Cloud Run →「リビジョン」で前のリビジョンにトラフィックを戻す。

### 最初に一度だけ（GitHub との接続）
1. GitHub で非公開のリポジトリ `bar-null-ops` を作り、このフォルダを push する。
2. Google Cloud コンソール →「Cloud Build」→「リポジトリ」（第2世代）→「ホスト接続を作成」で GitHub を選び、
   リージョン `us-central1`、名前 `github` で作る。GitHub の画面で Cloud Build アプリをこのリポジトリだけに許可する。
3. 「リポジトリをリンク」で `bar-null-ops` をリンクする。
4. `uv run python sales/infra/setup.py trigger`

## 課金停止が働いたとき（Slack に「【課金停止】」）
1. Google Cloud コンソールの「お支払い」で、何に費用が出たかを確認する。
2. 原因を止めたうえで、「お支払い」→「マイプロジェクト」から `barnull-sales` に請求先アカウントをつなぎ直す。
3. 動作を確認する。データが消えていたら、下の「Asana からの復元」。
- 予算と billing-guard の接続は、組織の設定（ドメインで制限された共有）の一時解除が必要で、まだ済んでいない（特権管理者に依頼中）。
  済んだら `uv run python sales/infra/setup.py guard-budget guard-live`。

## Asana からの復元（Firestore のデータを失ったとき）
```powershell
cd sales
uv run barnull-sales restore-from-asana --env prod --dry-run   # 何を戻すか確認
uv run barnull-sales restore-from-asana --env prod
```
Asana のタスクのカスタムフィールドと説明欄から、伝票・Square の割り当て・伝票 ID の連番を戻す。既にある伝票は上書きしない。

## 手元で試す
```powershell
cd sales
uv run pytest                                   # テスト（tests/sales.test.toml を使う。本番の設定には触らない）
uv run barnull-sales try "日付：10/5 顧客名：テストさま 金額：1,000"    # 1通の処理（既定ですべて偽物）
uv run barnull-sales report --env prod --date 2026-10-19                # 週報の文面を表示するだけ
```
