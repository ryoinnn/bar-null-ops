# 引き継ぎ: 売上管理とレポートの仕様（B・C のセッション向け）

詳細は `sales/docs/design.md`（設計書）、`decisions.md`（決定事項）、`runbook.md`（運用）、`weekly_report.md`（レポート）。
ここには、利益管理を作るうえで先に知っておくべきことだけをまとめる。内容は 2026-11-14 時点。実装と食い違うときは実装が正しい。

## 1. 事業と用語
- 店: 代官山の Bar null（運営: 株式会社0x0）。夜はバー営業、昼の空き時間はレンタルスペース（インスタベース・スペースマーケット）として貸す。
  福岡にもう1店舗（Node）があり、売上管理は店舗ごとに設定を分けられる（`sales/stores/<店舗>/`）。利益管理もまず Bar null だけを対象にし、店舗を区別できる形にしておく。
- **営業日**: 6:00〜翌6:00 を1営業日とする。朝6時までの会計は前日の営業分。勤怠・仕入の日付も、売上と同じ営業日で扱う（日付の項目名は「営業日」）。
- 伝票: LINE に送られる会計1件（1組）。売上 ＝ 伝票金額の合計（未収を含み、取消を除く）。
- 担当者: LINE で伝票を送った人（LINE の表示名）。管理者の桝本 崚允は、担当者別の比較から除いている。
- 管理者・本人: 桝本 崚允（Ryoin）。メールは ryoin.masumoto@0x0.co.jp。Asana・Slack・Claude の持ち主。

## 2. 現行システムの構成（売上管理）
```
LINE グループ「Bar null」 ─ @null で始まるテキストだけ ─▶ Cloud Run「sales-webhook」（公開・署名検証）
   ─▶ Cloud Tasks ─▶ Cloud Run「sales-worker」（非公開・1件ずつ処理）
        ├ /tasks/process-event  Claude Haiku で解析 → 起票・訂正・回収報告・現金入金報告
        ├ /jobs/reconcile       30分ごと: Square 突合、入金照合、手動上書き、反映のやり直し
        ├ /jobs/daily           毎日 10:05
        └ /jobs/weekly          毎週月曜 9:10: Slack の売上週報
   Firestore（状態の正本）／ Secret Manager（サービスごと）／ Slack（通知・スレッド）／ Asana（台帳）／ Square・マネフォ API
```
- GCP プロジェクト `barnull-sales`、リージョン us-central1。デプロイは GitHub の main に push → Cloud Build（テスト → イメージ → 2サービス）。
- 設定はすべて `sales/sales.toml`（秘密情報は Secret Manager。`config.SECRETS_BY_SERVICE` でサービスごとに分離）。`dry_run=true` ならテスト用の Slack チャンネル・Asana プロジェクトに書く。
- コードは `sales/src/barnull_sales/`。役割は design.md「コードの構成」。LINE の1通は `process.py` → `messages.py` で種類ごとに処理する。
  解析の種類は `extract.py` の `KINDS`（slip_content / correction / unpaid_collection_report / cash_deposit_report / other）。
- **状態は事実から計算する**（`slips.derive`）。処理は事実だけを書き換え、保存のたびに状態を決める。保存は `slip_ops.save`（トランザクション）。
  反映（Slack・Asana）が失敗しても保存は残し、`sync_pending` で30分ごとにやり直す。**同じ入力を何度処理しても結果が同じ**（冪等）になるように作る。
- 伝票の台帳は Asana のプロジェクト「Bar null 売上管理（伝票）」（本番 `1219600211224482`、テスト `1219599891506891`）。
  カスタムフィールドの ID は `sales/asana_ids.json`（`uv run barnull-sales asana-setup --env test|prod` が作る。`cli.py`）。
  Firestore を失っても Asana から復元できる（`restore-from-asana`）。台帳に必要な事実はすべて Asana のフィールドに持つ。
- Slack: 通知は #daily_barnull-sales（`C0C96EJ013J`）、週報は #weekly_barnull-sales。アラートのメンションは桝本。

### 伝票の Asana フィールド（レポートが読むもの）
営業日（日付）、顧客名、伝票金額、Square決済額、決済手数料、入金予定額、入金日、未収残高、担当（LINE）、伝票ID（`S-YYYYMMDD-NNN`）、
Square payment ID、Slackスレッド、伝票ステータス（起票済み／決済確認済み／入金確認完了／要確認／対象外）、未収ステータス、手動上書きフラグ。
レンタルスペースの月次確定値は同じプロジェクトに伝票ID `RS-…` のタスクとして入る（売上の集計からは除く）。

## 3. 売上レポート（Artifact）の仕様
- URL: https://claude.ai/artifact/8BynYgvNxC4UyCQVGRUDnC。原本は `sales/report/weekly_report.html` 1ファイル。Claude が Artifact ツールで公開し直す（URL は変わらない）。
- **データはサーバーを経由しない**。開いた人の claude.ai の Asana・Slack コネクタ（`mcp` ケイパビリティ。`get_tasks`、`slack_read_channel`）で、その場で Asana のタスクを読み、集計する（1分キャッシュ）。
  読むプロジェクトは JS の `PROJECTS = { prod, test }` に書く。新しい Asana プロジェクトを足すときは、ここに ID を足し、`get_tasks` で同じように読む（コネクタの追加宣言は不要）。
- 画面: 左に年→月→週のサイドバー（週は月曜〜日曜、月をまたぐ週は月で区切る）、本文に通常営業・レンタルスペースの数字とグラフ、担当者別、内訳、伝票の一覧。
  広い画面（1500px 以上）は右に AI の示唆を固定する。見た目は Liquid Glass 風・赤ピンクのアクセント・ライト／ダーク両対応。
- AI の示唆: 開いた人が「示唆を作成」を押すと、ページから Claude（`sample` ケイパビリティ）に **集計した数字だけ**を渡して分析させる。
  結果は Artifact のデータベース（`insights/…`）に保存して共有。顧客名・伝票 ID は渡さない。担当者は「担当A」の記号で渡し、表示で名前に戻す。
- 確定した週の売上は Artifact の db（`weeks/prod_<月曜>`）に記録し、Asana を読めないときの控えにする。
- 利益セクションは A が実装する。**B・C のデータは Asana の専用プロジェクトに置き、レポートが同じ方法で読む**（contract.md）。

## 4. 守るルール（必ず）
- **日本語**で話し、文面・資料・ドキュメントも日本語。指示や確認の依頼は、本人がそのまま実行できる手順で書く。
- **秘密情報**: パスワード・認証コード・API キー・トークンを、チャットへ書かせない・自分でも入力しない。ログインが必要なサイトは、本人が手元でログインした状態を使う。
  Google Cloud の組織ポリシーや権限（IAM）の変更、設定の特権操作は、本人が実行する。
- **個人情報**: 顧客名・金額・従業員名・時給をログに出さない。Claude API やページの AI に渡すのは集計値だけ（名前は記号に置き換える）。
  時給や人件費の額は git に入れない（設定は Secret Manager か Firestore。Asana に置くなら閲覧者を限る）。
- **git**: コミットしてよい。push は本人に依頼する（Claude の `git push` は拒否されることがある）。次は **コミットしない**:
  `knowledge/past_replies/`、`knowledge/faq_learned.md`、`assets/*.pdf`、`sales/samples/`。ファイルの削除は避ける。
  リポジトリには予約対応（`src/barnull/`）の未コミットの変更が残っていることがある。それは別のプロジェクトのものなので、触らず、コミットにも含めない。
- **コスト**: 課金の自動停止（予算 ¥100）の対象。無料枠を超える構成にしない。Cloud Scheduler は無料枠が3本で、すでに3本（reconcile・daily・weekly）使っている。
  新しい定期実行をクラウドに足すと有料になりうるので、**既存のジョブの中に処理を足す**か、本人の PC での実行にする。
- **外部サービスの規約**: ブラウザを自動操作するサイトは、先に利用規約を読み、自動化・ボット・スクレイピングの禁止条項を確認する（禁止なら操作せず、本人に報告）。
- 送信（メール・チャット・LINE への返信）は、仕様で決めたものだけ。企業・取引先へ個別に連絡しない。
- 変更は、まずテスト（`cd sales; uv run pytest`）と `dry_run`（テスト用プロジェクト・テスト用チャンネル）で動かし、本人が見てから本番にする。

## 5. 作法
- 環境: Windows 11、Git Bash。Python は `uv`（`cd sales; uv sync` / `uv run pytest`）。gcloud は `gcloud.cmd`。パスワードでの再認証が定期的に必要になる（そのときは本人にログインし直してもらう）。
- 設計の書き方: 伝票と同じく「事実を保存 → 状態を計算」「保存と外部反映を分ける」「やり直しても同じ結果」。失敗は握りつぶさず、Slack に知らせる（`health.py`・`errors.py`）。
- 設定・決定・仕様は `sales/docs/` に日付つきで残す。依頼への回答は decisions.md、フェーズごとの結果は `phaseN_report.md` の形式に倣ってよい。
- 見た目（レポートの画面）の好み: 極端を嫌う。角丸は同心で控えめ、ガラスはやりすぎず確実に、設定は右上にまとめる。
- 本人が忙しいので、聞くことは最初にまとめて短く聞き、決められることは推奨を添えて自分で決めて進める。
