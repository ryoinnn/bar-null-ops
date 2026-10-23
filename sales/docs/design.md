# 設計書（売上管理）

- 状態: 2026-10-17 本番稼働。2026-10-19 に全体を整理（状態の計算の一本化、障害時の動き、権限の分離、GitHub からのデプロイ）
- 前提: [requirements.md](requirements.md)（依頼書）、[decisions.md](decisions.md)（回答と決定。依頼書より優先）。経緯は phase1〜5_report.md
- 運用は [runbook.md](runbook.md)

## 1. 全体像

```
LINE グループ「Bar null」
  │ Webhook（@null で始まるテキストだけ。画像・メンション無し・他のトークは保存せずに捨てる）
  ▼
Cloud Run「sales-webhook」（公開。署名検証 → Cloud Tasks に積む → すぐ 200）
  │ Cloud Tasks（OIDC。失敗したら最大10回やり直す）
  ▼
Cloud Run「sales-worker」（非公開。Cloud Tasks と Cloud Scheduler からだけ呼べる。1件ずつ処理）
  ├─ /tasks/process-event   解析（Claude Haiku）→ 起票・訂正・回収報告・現金の入金報告
  ├─ /jobs/reconcile        30分ごと: 手動上書き、作りかけの作り直し、反映のやり直し、Square 突合、入金の照合
  ├─ /jobs/daily            毎日 10:05: 入金未確認、未収リマインド、古いイベントの削除
  └─ /jobs/weekly           毎週月曜 9:10: 売上週報（weekly_report.md）
  │
  ├─ Firestore（状態の正本）  ├─ Secret Manager（サービスごとの秘密情報）
  ▼
Slack（通知・スレッド） / Asana（台帳） / Square API / マネフォ API / Claude API

デプロイ: GitHub の main に push → Cloud Build（テスト → イメージ → 2つのサービス）
課金の防止: 予算（¥100）→ Pub/Sub → Cloud Run「billing-guard」→ 費用が出たら請求先アカウントを外す
```

### サービスを2つに分ける理由
- 組織の初期設定では Cloud Run を「誰でも呼べる」にできず、回避策（`--no-invoker-iam-check`）はサービス全体を公開にする。
- そこで公開するのは LINE の受け口だけの小さなサービス（sales-webhook）にし、処理本体（sales-worker）は IAM で守る。
- webhook が読める秘密情報は LINE のチャネルシークレットと Square アプリの ID・シークレットだけ（他の鍵は読めない）。
- 同じコンテナイメージを、環境変数 ROLE で役割を切り替えて2つのサービスとして動かす。

### リージョンは us-central1
- Cloud Run の外向き通信の無料枠は北米発だけ。Slack・Asana・Claude・Square の API は米国にあるため遅延も小さい。
- 顧客名と金額は米国のデータセンターに保存される（Slack・Asana と同じ）。

## 2. 伝票と状態

### 伝票（`slips/{伝票ID}`、`src/barnull_sales/slips.py`）
- 1通に顧客が複数並んでいれば、顧客ごとに伝票を作る。伝票 ID は `S-{営業日 YYYYMMDD}-{連番3桁}`（営業日は 6:00〜翌6:00）。
- 期待する Square の金額 = 伝票金額 − 未収。
- 持っている事実: Square の決済（`square`）、期待する入金と確認結果（`deposits`）、未収残高と回収の記録、アラート、Slack・Asana の ID。

### 状態は事実から計算する（`derive`）
各処理は事実だけを書き換え、保存のたびに `derive` が状態を決める。状態を直接書き換える処理は無い。

| 順 | 条件 | 伝票ステータス |
|---|---|---|
| 1 | 取り消した（手動上書き） | 対象外（完了） |
| 2 | 完了している | そのまま（凍結） |
| 3 | 未解決のアラートがある | 要確認 |
| 4 | 決済と入金が確認できた | 入金確認完了（未収残高が 0 なら完了） |
| 5 | 決済が確認できた | 決済確認済み |
| 6 | それ以外 | 起票済み |

- 決済の確認: Square の決済を割り当てた、人が確認した（手動上書き）、または全額未収で確認する決済が無い。
- 入金の確認: 期待する入金がすべて銀行で見つかった、人が確認した、または確認する決済が無い。
- 未収ステータス: 未収なし → なし／残高 0 → 回収入金確認済み／未確認の回収報告あり → 回収報告済み／それ以外 → 未収入金有。
- 完了したら Slack のスレッドに「完了」と出し、Asana のタスクを完了にする。

### 保存と反映（`slip_ops.py`）
- 伝票の保存は `save(fn)` だけ: 最新の伝票を読み → fn で事実を書き換え → `derive` → まるごと保存（Firestore のトランザクション）。
- 保存の後で、Slack の親メッセージと Asana のタスクを今の状態に合わせる（`sync`）。まだ投稿していないアラートもここで出す。
- 反映に失敗しても保存は取り消さない。`sync_pending` を立て、30分ごとの `resync` がやり直す。

## 3. LINE のメッセージ（`process.py`、`messages.py`）
1. webhook: 署名（`X-Line-Signature`）を検証し、対象（許可したグループの @null で始まるテキスト、参加イベント）だけを Cloud Tasks に積む。タスク名 = イベント ID（再送の重複を Cloud Tasks が弾く）。
2. worker: `line_events/{イベントID}` で処理済みかを見る。許可したグループかをもう一度確かめる。
3. Claude Haiku で分類・抽出する（失敗・低い確信度なら Sonnet）。結果は line_events に保存し、再試行では解析し直さない。金額は0以上の整数だけを受け付ける。
4. 種類ごとの処理: 起票（伝票は「イベント ID と顧客の順番」から1つに決まる）／訂正（送った人の24時間以内の伝票。適用したら履歴にイベント ID を残し、二度は適用しない）／未収の回収報告／現金の入金報告。
5. 受け付けたら、元のメッセージを引用して「OK」スタンプを返す（解析できなかったものには返さない）。
6. 最後の試行（10回目）でも失敗したら、Slack に原文つきで知らせて処理済みにする。

## 4. Square との突合（`reconcile.py`、`matching.py`）
- 読み取り専用の OAuth。アクセストークンは残り7日で自動更新し、`square-oauth` に保存する。許可の受け口は `square.oauth_enabled` が true のときだけ開く。
- 起票直後に1回、以後は30分ごと（送信から3日まで）。営業日の範囲の決済から、金額で探す。同額が複数あれば、同じ額の未確認の伝票の数まで時刻の早い順に割り当てる。
- 割り勘: 30分以内の最大5件の合計が一致すれば割り勘とみなす。
- 割り当ては `payment_claims/{決済ID}`（1つの決済を2つの伝票に使わない）。割り当ての後で止まっても、同じ伝票なら割り当て直せる。
- 伝票の無い決済: 営業日が終わって6時間たっても割り当てが無ければ、決済ごとに1回チャンネルに知らせる。後で割り当てられたら「解消」と返す。
- 返金: 割り当て済みの決済に返金があれば、決済ごとに1回知らせる。

## 5. 入金の確認（`deposits.py`）
- マネフォの明細（三井住友の入金）を3時間に1回読む。銀行の日付は Square の着金予定日の前日になることが多い。
- 期待する入金（`deposits.expected`）: カード等は決済を含む Square の振込ごと、現金は合計。決済の組み合わせ（`built_for`）が変わったら作り直し、確認済みのものは引き継ぐ。
- カード等: 振込額と「スクエア(カ」の入金を、着金予定日の前1日〜後3営業日で照合する。1つの入金で、その振込に含まれる伝票をすべて確認する。
- 現金: 担当者の名義（`mf.staff_bank_names`）からの振込と照合する。名義の一覧が空のあいだは照合しない（顧客の振込と区別できないため）。まとめて入金した報告があれば、その合計でも照合する。
- 割り当ては `deposit_claims/{明細ID}` に ref（`payout:振込ID`、`cash:伝票ID`、`cash_report:報告ID`、`unpaid:伝票ID`）で記録する。同じ ref からは何度でも照合し直せる。
- 入金未確認: 着金予定日から3営業日（現金は14日）を過ぎても見つからない、または振込が作られないまま5営業日たったら、毎日 10:05 に知らせる。

## 6. 未収・手動上書き（`unpaid.py`、`overrides.py`）
- 未収の回収: 後日の Square 決済（未収残高と同額が1件だけ）→ その入金の確認で回収済み。顧客名義の振込（名義と顧客名が同じ人かを Claude で判定し、結果を伝票に保存）。
- リマインド: 起票から7日ごとに、未収の伝票のスレッドで桝本をメンションする。
- 手動上書き（Asana）: 決済確認済み／入金確認済み／未収回収済み／対象外（取消）。反映したら「なし」に戻す。失敗したら読み取り位置を進めず、次回もう一度読む。

## 7. アラート
| アラート | 条件 | 閉じ方 |
|---|---|---|
| 解析の不備 | 金額・顧客名・日付・送信者が読めない | 手動上書き |
| ★決済未確認 | 送信から60分たっても決済が無い | 決済が見つかれば自動 |
| 突合候補が複数 | 候補が一意に決まらない | 自動（決まれば）／手動上書き |
| 日付ズレ疑い | 営業日の外にだけ候補がある | 自動／手動上書き |
| ★金額不一致 | 未収があるのに全額の決済がある | 訂正すれば自動／手動上書き |
| ★返金検知 | 割り当て済みの決済に返金 | 手動上書き |
| ★入金未確認 | 入金が予定より遅れている | 入金が見つかれば自動／手動上書き |
| 回収の振込か不明 | 未収と同額の振込の名義が判断できない | 手動上書き |
| ★完了後の訂正 | 完了した伝票への訂正 | （通知だけ） |

★はチャンネルにも表示し、桝本をメンションする。伝票に結び付かないもの（解析できないメッセージ、対象を決められない訂正・報告、伝票なし、障害）はチャンネルに投稿する。

## 8. 障害への備え
| 起きること | 備え |
|---|---|
| 連携先の一時的な失敗（429・5xx・通信エラー） | `http.request` が待ってやり直す。二重に実行されると困る POST は、送れていないと分かるときだけやり直す |
| メッセージの処理の失敗 | Cloud Tasks が最大10回やり直す。同じメッセージを何度処理しても結果は同じ。最後の失敗は Slack に原文つきで通知 |
| 定期ジョブの失敗 | ステップごと・伝票ごとに分けて続ける。続けて2回失敗したら Slack に通知（外部API障害か処理エラーかを区別）、直ったら「復旧」を1回 |
| Slack・Asana への反映の失敗 | 保存は残し、`sync_pending` で30分ごとにやり直す。作りかけ（Slack・Asana の ID が無い）の伝票は `republish` が作る |
| 同時の更新 | worker は1件ずつ処理する（concurrency=1）。伝票の保存はトランザクション |
| Firestore のデータを失った | Asana のタスク（全フィールド）から作り直す（`restore-from-asana`） |
| 調査 | ログは1行1件の JSON。`job`（ステップ）・`event_id`・`slip_id`・`api` で絞り込める。顧客名・金額・本文は書かない |

## 9. データ（Firestore）
| コレクション | 内容 |
|---|---|
| `slips/{伝票ID}` | 伝票（`slips.Slip`） |
| `slip_keys/{イベントID}_{順番}` | 伝票 ID（同じイベントから二重に作らない） |
| `counters/{営業日}` | 伝票 ID の連番 |
| `line_events/{イベントID}` | 受け取ったイベントの処理状況と解析結果（30日で削除） |
| `payment_claims/{決済ID}` | → 伝票 ID |
| `deposit_claims/{明細ID}` | → ref |
| `payouts/{振込ID}` | Square の振込に含まれる決済（キャッシュ） |
| `orphan_payments/{決済ID}` | 伝票の無い決済の通知記録 |
| `cash_reports/{イベントID}` | 現金のまとめ入金の報告 |
| `meta/*` | `job_health`（障害の通知）、`mf`（マネフォを読んだ時刻）、`asana_overrides`（読み取り位置）、`line_groups`・`unknown_groups_notified`（グループの通知。最大50件） |

## 10. 秘密情報・設定・権限
- 設定はすべて `sales.toml`（環境変数で変えられるのは DRY_RUN だけ）。テストは `tests/sales.test.toml` を使う。
- 秘密情報はサービスごとに分ける（`config.SECRETS_BY_SERVICE`）: `sales-config`（worker）、`sales-webhook-config`（webhook）、`billing-guard-config`（Slack のトークンだけ）、`square-oauth`（worker が読み書き、webhook は書くだけ）。
- サービスアカウント（`infra/setup.py iam`）

| アカウント | できること |
|---|---|
| sales-webhook | キュー sales-events にだけタスクを積む、sales-webhook-config を読む、square-oauth に版を足す |
| sales-worker | Firestore、sales-config と square-oauth を読む（square-oauth は更新も） |
| sales-invoker | sales-worker を呼ぶ（Cloud Tasks と Scheduler が使う） |
| sales-deployer | Cloud Run の更新、Artifact Registry への保存、worker・webhook のアカウントでの実行（Cloud Build が使う） |
| billing-guard | プロジェクトの請求先の管理、billing-guard-config を読む |

## 11. 課金の防止
- 予算 `barnull-sales-guard`（¥100）の通知を Pub/Sub トピック `billing-guard` で受け、費用が1円以上になったら Slack に知らせて請求先アカウントを外す。
- 予算と Pub/Sub の接続は、組織の設定の一時解除が必要で未完了（特権管理者に依頼中）。それまでは dry（判定と通知だけ）。
- 上限: Cloud Run は最大1インスタンス・512MiB、Artifact Registry は最新2つ、Cloud Scheduler は3本（無料枠）、Secret Manager の有効な版は4つ。

## 12. コードの構成（`src/barnull_sales/`）
| モジュール | 役割 |
|---|---|
| `app_webhook.py` / `app_worker.py` / `main.py` | 2つのサービスの入口 |
| `config.py` | 設定と秘密情報 |
| `slips.py` | 伝票のデータと状態の計算（`derive`）、列挙（Status・Unpaid・Alert） |
| `slip_ops.py` | 伝票の保存と Slack・Asana への反映（すべての処理がここを通る） |
| `process.py` → `messages.py` | LINE イベント1件 → メッセージの種類ごとの処理 |
| `reconcile.py` / `matching.py` | Square との突合（matching は外部に依存しない純粋な関数） |
| `deposits.py` / `unpaid.py` / `overrides.py` | 入金の確認 / 未収 / 手動上書き |
| `jobs.py` / `health.py` / `errors.py` | 定期ジョブのステップ / 障害の通知 / 伝票ごとの失敗の分離 |
| `store.py` | Firestore と、テスト用のメモリ版 |
| `http.py` / `logs.py` | 外部 API の呼び出し（やり直し・エラーの種類） / JSON のログ |
| `slack.py` `asana.py` `square.py` `mf.py` `line.py` `tasks.py` `extract.py` | 連携先 |
| `texts.py` / `report.py` / `restore.py` / `cli.py` / `testing.py` | 文面 / 週報 / 復元 / 手元のコマンド / 偽物の連携先 |

テスト（`tests/`）は外部 API をすべて偽物にし、起票から完了まで・再試行・障害時の動きを確かめる。
