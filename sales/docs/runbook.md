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
2026-11-15 から、下の【外部API障害】【処理エラー】【復旧】は `#null_system-monitor` に出る（2 回目の失敗で親メッセージ、8 時間ごとのリマインドと復旧はスレッド）。
投稿できないとき（bot が未招待など）は今までどおり `#daily_barnull-sales` に出る。詳しくは「システム監視」。

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
| 決済確認済み | Square の決済を人が確認した（候補が複数・決済未確認・決済の取り合いなどのアラートを閉じる） |
| 入金確認済み | 入金を人が確認した（入金未確認のアラートを閉じる。決済も確認済みにする） |
| 未収回収済み | 未収を人が確認した |
| 対象外（取消） | 伝票を取り消す。割り当てていた Square の決済は外れ、他の伝票に使える |
| Square以外で支払い | 銀行振込などで払われた。割り当てていた Square の決済は外れ（他の伝票に使える）、決済・入金は確認済みとする（未収は残る） |

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

## システム監視（#null_system-monitor。2026-11-15 追加）
要件は `docs/monitoring_requirements.md`。システム系の不具合（課金停止・停止・定期処理の停止・デプロイ失敗・外部API障害・認証切れ）は
すべて `#null_system-monitor`（C0CHSLZA8RY、公開）に出る（2026-11-15 本人の指示で一元化。福岡も同じ）。
業務の通知（伝票のスレッド、★決済未確認、【伝票なし】、未収のリマインド、【要確認】、勤怠の承認・給与、週報）は今までどおり各チャンネル。

| 見張るもの | どこで | 間隔 |
|---|---|---|
| 外部監視 `system-monitor` | GitHub Actions（`.github/workflows/system-monitor.yml`、`.github/monitor/check.py`） | 15 分ごと |
| 外部API障害・処理エラー | sales-worker の health（`health.py`） | 30 分ごとのジョブの中 |
| 外部監視が動いているか | sales-worker のステップ `monitor_watch`（公開の GitHub API を読む） | 30 分ごと |

通知の形: 異常が 2 回続いたら、桝本をメンションした親メッセージ。続く間は 8 時間ごとにスレッドへ「まだ続いています」。
直ったらスレッドに【復旧】、親の先頭に【復旧済み】。1 回だけの失敗は何も出さない。

### 通知ごとの意味と最初にやること
| 通知 | 意味 | まずやること |
|---|---|---|
| 【課金停止】 | Cloud Run が「billing is disabled」と応答。LINE の受付も定期処理もすべて止まっている。**原因はプロジェクトの利用額上限（spend cap）に達したこと**が多い（2026-10-28・11-13。月初にリセットされて自然に戻る。billingEnabled は true のまま） | Google Cloud の「お支払い」で「利用額上限が適用されたため…一時停止」の表示を確かめ、上限を解除・引き上げる（本人）。表示が無ければ `gcloud billing projects describe barnull-sales`（福岡は `node-fukuoka-sales`）が false でないか見て、請求先アカウントをつなぎ直す。下の「止まったときの復旧」へ |
| 【停止】 | サービスが応答しない（応答なし・5xx） | Cloud Run のコンソールで該当サービスのリビジョンとログを見る。直前のデプロイが原因なら前のリビジョンにトラフィックを戻す |
| 【定期処理の停止】 | 30分ごと（75 分）・毎日（26 時間）・毎週（8 日）の処理が動いていない | Cloud Scheduler の `sales-reconcile` などが「有効」か、sales-worker のログに 429・500 が無いか。福岡は Cloud Tasks のキューが一時停止していないか（`gcloud tasks queues describe`）。POS の伝票が来れば予約し直されるが、待たずに手で `/jobs/tick` を積む |
| 【監視の異常】 | `/health` から状態を読めない（Firestore を読めない・古い版） | sales-webhook に `roles/datastore.viewer` があるか（下の「最初に一度だけ」）。デプロイ直後なら次の回まで待つ |
| 【デプロイ失敗】 | main に push したコミットが 40 分たっても動いていない | Cloud Build の履歴（`gcloud builds list --region us-central1`）で失敗したステップを見る。`deploy-worker` の「container failed to start」でアプリのログが 1 行も無いときは、課金の反映待ちを疑う（2026-11-15 の例） |
| 【外部API障害】【処理エラー】 | 上の「Slack に出る障害の通知と対応」と同じ（通知先がここに変わった） | 同上 |
| 【認証切れ】 | 連携先（Claude・Square・マネフォ・Asana・Slack・LINE）が 401・403。待っても直らないので 1 回目から出る。LINE の伝票の処理（process_event）でも出る（2026-11-10 の Claude のキー無効の例） | 新しいトークン・APIキーを発行し「秘密情報の更新」。Square は「Square の許可をやり直す」 |
| 【期限が近い】【期限切れ】 | sales.toml の `[[monitor.expiry]]` に載せた鍵の期限の 30日前・7日前・前日・期限切れ（毎日 10:05 のステップ secret_expiry） | 通知の「更新の手順」に従って発行し直し、`expires` を新しい期限に書き換えて push |
| 【処理できなかったメッセージ】 | LINE のメッセージを 10 回試しても処理できなかった（原文つき。2026-11-15 から業務のチャンネルには出さない） | 通知に従って起票し直す（伝票が作成済みなら二重に起票しない） |
| LINE の Bot の参加・処理対象ではないグループ | 運用の通知（2026-11-15 からここ） | 処理するなら sales.toml の allowed_group_ids に追加 |
| 【処理エラー】定期処理「monitor_watch」 | 外部監視（GitHub Actions）が 3 時間以上動いていない、または続けて失敗（Slack のトークン切れなど） | GitHub の Actions →「system-monitor」。無効になっていれば「Enable workflow」。失敗ならログの「Slack の … に失敗」を見てトークンを更新 |
| GitHub からのメール「system-monitor failed」 | 外部監視が Slack に投稿できなかった | Slack のトークン（GitHub の Secrets `SLACK_BOT_TOKEN`）と、bot がチャンネルにいるかを確かめる |

### 止まったときの復旧（課金停止・長い停止の後）
1. 課金を戻す（本人）。Cloud Run が普通に応答するまで待つ（30 分以上かかることがある）。
   `curl https://<sales-webhook の URL>/health` が `{"ok": true, ...}` を返せば戻っている。
2. 失敗したデプロイがあれば、Cloud Build のトリガー `sales-deploy` を「実行」し直す（main の最新が入る）。
3. 定期処理は次の回から動く。止まっていた間の入金・突合は、30 分ごとの処理が過去の分も読み直すので、自然に追いつく。
4. 停止中に届いた LINE のメッセージは、LINE の再送の期限を過ぎると戻らない。停止の期間をスタッフに伝え、伝票を送り直してもらう
   （「LINE で送ったのに起票されない」）。
5. すべての通知に【復旧】が付いたことを確かめる。

### 最初に一度だけ（本人の作業。Claude は秘密情報を入力しない）
- 状態の道は `/health`（`/healthz` は Cloud Run が予約していてアプリに届かない）。福岡の売上は再デプロイ（`setup.py deploy`）までは `/health` が無いので、`MONITOR_TARGETS` では `"kind":"plain"`（応答と課金停止だけを見る）にしてある。
- PowerShell で「スクリプトの実行が無効」と出るときは、`gcloud` を `gcloud.cmd` に置き換える（実行ポリシーは変えなくてよい）。
- 2026-11-15: 1〜4 はすべて済み（招待・IAM・Secrets 2 つ・LINE の再送とエラーの統計情報）。
1. Slack の `#null_system-monitor` で `/invite @barnull_sales`。
2. sales-webhook が Firestore を読めるようにする（`/health` のため。読み取りだけ）:
   ```powershell
   gcloud projects add-iam-policy-binding barnull-sales --member=serviceAccount:sales-webhook@barnull-sales.iam.gserviceaccount.com --role=roles/datastore.viewer --condition=None
   gcloud projects add-iam-policy-binding node-fukuoka-sales --member=serviceAccount:sales-webhook@node-fukuoka-sales.iam.gserviceaccount.com --role=roles/datastore.viewer --condition=None
   ```
3. GitHub の Secrets（PowerShell。トークンは画面に出さずにそのまま渡る）:
   ```powershell
   (gcloud secrets versions access latest --secret=billing-guard-config --project barnull-sales | ConvertFrom-Json).SLACK_BOT_TOKEN | gh secret set SLACK_BOT_TOKEN -R ryoinnn/bar-null-ops
   gh secret set MONITOR_TARGETS -R ryoinnn/bar-null-ops --body '[{"name":"売上（Bar null）","url":"https://sales-webhook-755193406929.us-central1.run.app/health","kind":"sales","deploy":true},{"name":"売上（福岡）","url":"https://sales-webhook-685670396881.us-central1.run.app/health","kind":"sales"},{"name":"福岡 node-api","url":"https://node-api-864625751000.asia-northeast1.run.app/api/health","kind":"plain"}]'
   ```
4. LINE Developers →（Bar null の売上の）チャネル →「Messaging API設定」→「Webhookの再送」と「エラーの統計情報」を有効にする。
   同じイベントが再送されても、イベント ID から作るタスク名で 1 件にまとまる（`tasks.py`）。

### 利用額上限の手前の通知（2026-11-15 設定）
- 予算 `barnull-sales-guard`（対象 barnull-sales）を ¥1,000・実績 50%・80%・100% にした（本人）。請求先アカウントの管理者とプロジェクトのオーナーにメールが届く。上限に達して止まったときは、外部監視が【課金停止】を出す。
- 利用額上限そのものの金額を変えたら、この予算の金額も合わせる。福岡（`node-fukuoka-sales-guard`・`node-fukuoka-guard`）は ¥100 のまま。
- billing-guard（予算と Pub/Sub をつないだら課金を外す仕組み）は、予算の金額ではなく費用 1 円以上で動く（THRESHOLD_JPY=1）。live にする前に見直す。

### 期限のある秘密情報（2026-11-15 追加）
- 一覧は `sales/sales.toml` の `[[monitor.expiry]]`（null システム全体の鍵をここにまとめる。福岡・予約対応の鍵も）。名前・期限・更新の手順だけで、キー本体は書かない。
- 鍵を発行するときに期限を決められるもの（Anthropic など）は、必ずここに足す。期限の無い鍵は載せない（切れたら【認証切れ】で分かる）。
- 2026-11-15 時点で載っているもの: 売上管理の Claude API キー（2027-11-22 04:00）。ほかの鍵の期限は本人に確認中。

### 監視そのものを止める・戻す
- 外部監視を止める: GitHub の Actions →「system-monitor」→「…」→「Disable workflow」（3 時間たつと `monitor_watch` が知らせる。長く止めるときは先に一言書いておく）。
- 戻す: 同じ画面で「Enable workflow」。
- 監視先を変える: Secrets の `MONITOR_TARGETS` を上書きする（形は上の 3）。
- 手で 1 回動かす・試験する: Actions →「system-monitor」→「Run workflow」。`mode=test` は `#test_barnull-sales` に出し、本番の状態を汚さない。
  例: 停止の試験は `extra_targets` に `[{"name":"試験","url":"https://127.0.0.1:9/","kind":"plain"}]`、課金停止の模擬は
  `[{"name":"試験","url":"https://example.invalid/","kind":"sales","simulate":"billing"}]`、`consecutive` に 1。
- 手元で判定だけ見る: `MONITOR_DRY=1 MONITOR_TARGETS='[...]' python .github/monitor/check.py`（テスト: `python -m pytest .github/monitor -q`）。

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

## 勤怠・スタッフ台帳・給与（2026-11-14 追加）
要件は `docs/profit/labor_requirements.md`、作りは `docs/profit/labor_design.md`、データの約束は `docs/profit/contract.md` §2。
設定は `sales.toml` の `[labor]`。`dry_run = true` のあいだは、勤怠・台帳・給与をテスト用の Asana と `#test_barnull-sales` に書く（売上は本番のまま）。

### 定期処理（30分ごとの sales-reconcile に追加したステップ）
| ステップ | 内容 |
|---|---|
| `staff_sync` | Asana の台帳を読み、Firestore（`staff/`）に写す。時給が変わったら履歴に足し、今月・前月の勤怠と給与を直す |
| `attendance_approvals` | 期限外の勤怠の承認依頼（Slack）に付いた桝本の ✅／❌ を読んで反映する。承認したら LINE で知らせる（プッシュ送信） |
| `attendance_overrides` | 勤怠タスクの「勤怠の手動上書き」（時間を反映／対象外）を取り込む |
| `attendance_resync` | Slack・Asana への反映に失敗した勤怠・台帳をやり直す |
| `payroll` | 翌月の第1水曜 17:00 を過ぎたら前月分を計算、10日 9:00 を過ぎたら確定（どちらも1回だけ） |
| `payroll_adjustments` | 給与タスクの「調整額」「調整の理由」を取り込む。反映に失敗した給与をやり直す |

### よくある対応
- **新しいスタッフ**: 初めて勤怠を送ると、台帳に仮のタスクができて Slack に「台帳に無い人」と出る。そのタスクに本名・区分・入社日・時給・バック率を入れる
  （LINE名は自動で入る。新しいタスクを別に作らないこと。二重になる）。勤怠を送らないプロモーターは、台帳にタスクを作って LINE名（伝票の「プロモーターフラグ」に書かれる名前）を入れる。
- **時給を変える**: 台帳の「時給」と「時給の適用開始日」を入れる。適用開始日より前の勤務は前の時給のまま。
- **勤怠を直す（管理者）**: 勤怠タスクの「勤務開始」「勤務終了」を書き換え（`22:00` のような書き方でよい）、「勤怠の手動上書き」を「時間を反映」にする。取り消すなら「対象外」。
- **期限外の勤怠**: Slack の承認依頼に ✅（承認）か ❌（却下）を付ける。桝本のリアクションだけが数えられる。7日たっても無ければもう一度知らせる。
- **確定した給与を変える**: 給与タスクの「調整額」（マイナス可）と「調整の理由」を入れる。確定後は売上が変わっても自動では計算し直さず、Slack に「確定後の変更」と出る。
- 給与の額・時給は Slack とログには出ない。Asana の非公開プロジェクト（管理者2人だけ）で見る。

### 最初に一度だけ
1. Slack: チャンネル `#null_staff-management` を作り、`/invite @barnull_sales`。チャンネル ID を `[labor] slack_channel_prod` に入れる。
2. Slack アプリ（api.slack.com/apps → barnull_sales → OAuth & Permissions）: Bot Token Scopes に `reactions:read` を足し、「Reinstall to Workspace」。
   トークンが変わったら「秘密情報の更新」（`SLACK_BOT_TOKEN`）。
3. Asana: `uv run python -m barnull_sales.cli asana-setup-labor --env prod`（本番のプロジェクトを作る）。3つのプロジェクトに岡本さんをメンバーとして追加する。
4. `[labor] dry_run = false` にして push。

## 仕入（NOS → Asana「Bar null 仕入」。2026-11-15 追加）
要件・設計: `docs/profit/purchasing_requirements.md`、データの約束: `docs/profit/contract.md` §3。コード: `src/barnull_sales/purchase*.py`。
本人の PC で毎週月曜 9:30 に、タスクスケジューラ「BarNullPurchase」→ `scripts/purchase/run_purchase.ps1` →
`uv run --group purchase python -m barnull_sales.cli purchase-fetch` が動く。クラウドには何も足していない（課金なし）。

- 読むのは NOS の「仕入実績一覧」だけ（期間・チェックボックス・検索ボタンだけを操作する）。期間は当月1日〜今日、1〜7日は前月1日から。
- Playwright のブラウザ（画面は出さない）。ログイン状態は `sales/samples/purchases/browser/`、取得した表の控えは `sales/samples/purchases/nos_*.json`、
  ログは `sales/samples/purchases/logs/`（どれも git に入れない）。
- NOS のセッションはすぐ切れる（`/oms/error?timeout`）。資格情報マネージャー（barnull-sales の NOS_ACCOUNT・NOS_PASSWORD）で自動でログインし直す。
- Slack には知らせない（2026-11-15 本人。`[purchase] slack = false`）。結果は `sales/samples/purchases/last_result.txt`、失敗（終了コード 2）と要確認（3）は PC に通知が出る。
- 仕入と備品・消耗品は分けて管理する。NIGITA から仕入れた物は、区分が「消耗品」でも仕入で数える（備品・消耗品の記録には入れない）。
- 2026-11-15 本番化: 本番プロジェクト `1220540183336163`、`dry_run = false`、8/1〜の35伝票を取り込み済み。商品マスタ29件はテストで本人が確定した表示名・区分を写した

### 最初に一度だけ
1. `cd sales; uv sync --group purchase`（Playwright を入れる。ブラウザはリポジトリ直下の予約対応と共用）
2. `uv run --group purchase barnull-sales purchase-set-login` … NOS のアカウントとパスワードを本人が入力（資格情報マネージャーに保存。画面に出ない）
3. `uv run --group purchase barnull-sales purchase-setup --env test`（本番は `--env prod`。プロジェクト・欄・セクションを作り asana_ids.json に保存）
4. `uv run --group purchase barnull-sales purchase-fetch --initial` … 2026-08-01 からの過去分を取り込む
5. `powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\purchase\install_purchase_task.ps1`（毎週月曜 9:30 のタスクを登録）

### 本番に切り替える
1. `purchase-setup --env prod` → `asana_ids.json` の `purchase.prod.project` を `report/portal/pages/purchase.html` の `PROJECT.prod` に入れて公開
2. `sales.toml` の `[purchase] dry_run = false` → `purchase-fetch --initial`
3. A（売上レポート）に本番のプロジェクト ID を知らせる（利益の変動費に使う）

### よくある対応
| PC の通知・last_result.txt | 対応 |
|---|---|
| ★ ログインが切れています／自動でログインできませんでした | パスワードを変えたなら `purchase-set-login` で登録し直す。画面で確かめたいときは `purchase-login`（ブラウザが開くのでログイン） |
| ★ 追加の確認（認証コード等）を求められました | 自動では進めない。`purchase-login` で本人がログインし、続くようなら NIGITA に確認 |
| ★ 画面の形が変わった可能性 | NOS の画面が変わった。`purchase-fetch --headed --dry-run --no-slack` で様子を見て、`purchase_nos.py` を直す |
| ★ 税抜の合計が NOS と合いません | 取り込み中に NOS が更新された可能性。翌日の実行で直らなければ、控え（nos_*.json）と Asana を見比べる |
| 新しい商品 n件 | ポータルの仕入管理 →「商品マスタ」で表示名・区分を直して「確定」 |
| 要確認: 区分の分からない NOS カテゴリ | `purchase.py` の `NOS_CATEGORY` にカテゴリを足す（税率が 8% なら `REDUCED_RATE_CATEGORIES` にも） |

- 手で1回動かす: `uv run --group purchase barnull-sales purchase-fetch`（`--dry-run` で Asana に書かない、`--since 2026-10-01` で期間を指定）
- 止める: `scripts\purchase\uninstall_purchase_task.ps1`

## 備品・消耗品（マネフォのカード「BAR NULL」→ Asana「Bar null 備品消耗品」。2026-11-15 追加）
要件は [profit/supplies_requirements.md](profit/supplies_requirements.md)、データの約束は [profit/contract.md](profit/contract.md) §3b。
クラウドの `/jobs/reconcile`（30 分ごと）のステップ `supplies` が、**3 時間に1回**だけマネフォを読み、カード補助口座の明細を Asana に合わせる。PC は要らない。新しい定期実行は無い。

| やること | 方法 |
|---|---|
| 有効にする（最初の1回） | `sales.toml` の `[supplies]` で `dry_run = false`・`enabled = true` にして push（デプロイ）。最初の同期で 2026-08-01 からの分を取り込む。先に手元で `uv run barnull-sales supplies-sync --env prod --since 2026-08-01` を実行してもよい（同じ処理） |
| 手元で1回同期する | `cd sales; uv run barnull-sales supplies-sync [--env test\|prod] [--since YYYY-MM-DD] [--dry-run]` |
| テスト用プロジェクトを作り直す | `uv run barnull-sales supplies-setup --env test`（冪等）。ID は `asana_ids.json` の `supplies` |
| 訂正（金額・区分・支払先・営業日） | ポータルの備品・消耗品の「明細」で区分を直す（その場で保存）。それ以外は Asana のタスクで直し、「手動修正」を「あり」にする。「あり」は同期が上書きしない |
| 取消 | 明細の「対象外にする」（または Asana で「備品ステータス」を対象外）。同期は取り込み済みに戻さない。削除はしない |
| 現金・個人カードの立替 | Asana にタスクを足し、「取得元」＝手入力、営業日・金額（税込）・支払先を入れる。同期は触らない |
| NIGITA が混ざった | 摘要に NIGITA・ニギタを含む明細は自動で対象外（`exclude_patterns`）。他の取引先を除くときは `sales.toml` に足して push |
| ツール・サブスクの区分を足す | `tool_patterns` に摘要の一部を足して push（作る時だけ判定。既存の明細は明細の画面で直す） |
| カードを変える・増やす | `card_subaccount`（マネフォの `connected_sub_account_id`）を差し替える。`GET /api/v3/connected_accounts` で調べる（カード番号は載らない） |

- 取り込みの結果は Slack に知らせない（2026-11-15 本人。`[supplies] notify = false`。true にすると、変化があった回だけ件数・金額を #daily_barnull-sales に出す）。結果はポータルの備品・消耗品か Asana で見る。1件 10 万円以上はポータルの「高額」の件数に出る（固定資産・減価償却にするかを本人が決める。システムは扱わない）。
- マネフォの読み取りに失敗すると、既存の障害通知（health）が `supplies` のステップ名で知らせる（2 回続けて失敗で ★）。
- マネフォを 0 件で返したときは、読み違いの可能性があるため、既存の明細を「対象外」にしない。
- カードの明細は利用から 1〜2 日遅れて入る。PENDING（速報）の明細が確定で置き換わると、古いほうは同期が「対象外」にする。
- 仕入との線引き: NIGITA（NOS）の雑貨・備品は仕入（区分「消耗品」）。カード「BAR NULL」の利用は備品・消耗品。重複しない。
- 秘密情報: マネフォの API キーは既存（`MF_API_KEY`）。カードの明細（店名・金額）は git に入れない。

## 固定費（経営ダッシュボード → Asana「Bar null 固定費」。2026-11-15 追加）
要件は [profit/fixed_costs_requirements.md](profit/fixed_costs_requirements.md)、データの約束は [profit/contract.md](profit/contract.md) §3c。
定期実行は無い。正本は「Bar null 固定費」（管理者だけの非公開）。1タスク ＝ 1項目 × 1適用期間。レポートは開いたときに読む。

| やること | 方法 |
|---|---|
| 最初の写し（本番） | `cd sales; uv run barnull-sales fixed-setup --env prod` → `uv run barnull-sales fixed-import --env prod --dry-run` で中身を見る → `--dry-run` を外して実行。ID は `asana_ids.json` の `fixed.prod`（A にレポートの読み先として伝える） |
| 確かめる | `uv run barnull-sales fixed-show --env test\|prod [--until YYYY-MM] [--months 6]`（月ごとの合計と項目。書き込まない） |
| 値上げ・値下げ | 同じ項目名で新しいタスクを足し、「適用開始月」を変わる月、「金額（税込）」を新しい額にする。古いタスクはそのまま（適用終了月を入れてもよい）。新しい開始月のほうが使われる |
| 光熱費などの請求額 | その月だけのタスクを足す: 「固定費種別」＝実績、適用開始月・適用終了月 ＝ その月、金額 ＝ 請求額。その月は通常の額の代わりに実績が使われる |
| 年払い・半年払い | 「金額（税込）」＝ 1回の支払額、「按分月数」＝ 12 / 6、「適用開始月」＝ 対象期間の最初の月。毎月に割って計上（端数は区切りの最後の月） |
| 減価償却 | 「金額（税込）」＝ 取得額、「按分月数」＝ 償却月数、適用開始月 ＝ 使い始めた月、適用終了月 ＝ 開始月 ＋ 償却月数 − 1 |
| 解約・やめた項目 | 「適用終了月」に最後の月を入れる |
| 間違えて入れた | 「固定費ステータス」を対象外にする（削除しない） |
| 新しい項目 | 項目名をタスク名にしてタスクを足す（固定費区分・金額・適用開始月）。固定費ID は空でよい |
| 経営ダッシュボードを更新し続ける場合 | `fixed-import` をもう一度実行する（冪等）。写したタスク（本文に「写し元: 経営ダッシュボード」）は上書きされ、手で足したタスクには触らない。写したタスクを Asana で直したときは、再実行で戻るので注意 |
| 知らない項目名が出た | `fixed-import` が「知らない項目」と表示する。`src/barnull_sales/fixed.py` の `_ITEMS` に項目コード・区分を足す |

- 金額は税込として扱う（経営ダッシュボードの値をそのまま）。日・週は「月の固定費 ÷ その月の日数」で日割り。
- 「返済・配当」（出資者配当金・銀行借入の返済）は経営ダッシュボードどおり固定費に含める。会計上の費用ではないため、画面では区分の小計で分けて見せる。
- 実際の金額・取引先名は git に入れない（Asana にだけ置く）。
