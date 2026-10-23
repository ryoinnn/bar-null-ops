# Bar null 予約対応システム（barnull）

代官山の Bar null（運営: 株式会社0x0）のレンタルスペース予約対応を自動化する常駐プログラム。
スペースマーケット（SM）とインスタベース（IB）の管理画面を定期的に巡回し、Slack の `#notif_space-rent_null` に通知します。
承認者のスタンプや返信をきっかけに、承認、定型文とマニュアルの送信、問い合わせへの返信、レビュー投稿を行います。

- 設計: [docs/design.md](docs/design.md)（2026-10-09 承認）
- 障害対応: [docs/runbook.md](docs/runbook.md)
- 調査メモ: [docs/research/](docs/research/)

売上管理（LINE の伝票 → Square → 銀行入金の確認）は別のシステムとして [sales/](sales/README.md) にあります。

## 動作の概要

| 場面 | Slack での見え方 | 人の操作 |
|---|---|---|
| 新しい予約 | 🟦 SM／🟧 IB の親メッセージ（日付・時間帯・予約者とリピート回数・ゲスト支払・店舗収益） | 親メッセージにスタンプ → 承認（SM）＋定型文＋マニュアル PDF |
| 保留したい | | スレッドに「待って」等 → 4時間ごとにリマインド |
| ゲストからのメッセージ | スレッドに原文と返信文案 | 文案にスタンプ → 送信。修正するなら全文を返信 → その文面のまま送信 |
| 利用完了 | 🏁 利用完了、★5 レビュー投稿済み | 不要（退室時に「気になる点」があった場合だけ確認） |
| 失敗、ログイン切れ | ⚠️（承認者にメンション） | スタンプで再実行／`barnull login` |

ブラウザ操作は Playwright の決まった手順で行います。Claude（Anthropic API）は、返信の意図分類、問い合わせの判定、返信文案、お礼文の作成だけに使います。

## セットアップ（Windows）

前提: Python 3.12 と uv（`winget install -e --id Python.Python.3.12` / `winget install -e --id astral-sh.uv`）

```powershell
cd C:\Users\ryoui\Documents\bar-null-ops
uv sync
uv run playwright install chromium
```

### 1. Slack App
1. https://api.slack.com/apps → Create New App → From a manifest → `slack/manifest.yml` の中身を貼り付けて作成
2. Install App → ワークスペースにインストール → Bot User OAuth Token（`xoxb-`）
3. Basic Information → App-Level Tokens → スコープ `connections:write` で作成（`xapp-`）
4. `#notif_space-rent_null` と `#test_space-rent_null` で `/invite @barnull`

### 2. 秘密情報とログイン
```powershell
.\.venv\Scripts\barnull.exe setup
```
未登録の秘密情報を順に聞かれます。入力は画面に表示されず、Windows 資格情報マネージャー（サービス名 `bar-null-ops`）に保存されます。
続けて SM と IB のブラウザが開き、保存した ID とパスワードでログインします（ログイン状態は `data/browser/` に保存）。

| 名前 | 内容 |
|---|---|
| `SLACK_BOT_TOKEN` / `SLACK_APP_TOKEN` | 上の 2・3 |
| `ANTHROPIC_API_KEY` | Claude API キー（未登録でも動くが、文案・分類は使えない） |
| `KEYBOX_CODE` | キーボックスの暗証番号（変更したら `barnull set-secret KEYBOX_CODE`） |
| `SM_EMAIL` / `SM_PASSWORD`, `IB_EMAIL` / `IB_PASSWORD` | 自動再ログイン用 |

秘密でない設定（チャンネル、承認者、間隔、手数料率、定型文、モデル名、DRY_RUN）は `config.toml` にあります。

### 3. 確認と過去データの取り込み
```powershell
.\.venv\Scripts\barnull.exe doctor     # Slack・ログイン・秘密情報の確認
.\.venv\Scripts\barnull.exe backfill   # リピート判定の履歴と、過去のホスト返信（knowledge/past_replies）
```

### 4. DRY_RUN で試す
`config.toml` の `dry_run = true`（既定）で起動すると、投稿先はテスト用チャンネルになり、管理画面の最後のボタン（承認・送信・投稿）は押しません。
```powershell
.\.venv\Scripts\barnull.exe run
# 別のターミナルで、ポーリングが見つけたことにしてイベントを投入
.\.venv\Scripts\barnull.exe simulate new sm 6271768 --text "シーシャは使えますか？"
.\.venv\Scripts\barnull.exe simulate guest-message ib 8797784002 --text "ありがとうございました！"
.\.venv\Scripts\barnull.exe simulate manual-sent sm 6271768
.\.venv\Scripts\barnull.exe simulate complete sm 6248079 --exit "気になる点があります"
.\.venv\Scripts\barnull.exe simulate cancel ib 8797784002
```
リマインドの間隔を短くして試す場合は、`$env:BARNULL_REMIND_MINUTES = 2` を設定してから `run` します。

### 5. 本番
1. `config.toml` を `dry_run = false` にする（投稿先が `#notif_space-rent_null` になり、実際に送信される）
2. 最初の数件は立ち会いで確認する（特に SM の「承認」とファイル送信の確認画面は、実物の画面で初めて動く）
3. 常駐化: `powershell -ExecutionPolicy Bypass -File scripts\install_task.ps1`（PC 起動時から動かすなら `-AtStartup`）
4. Windows の電源設定で「スリープしない」にする（アプリもスリープを抑止するが、念のため）

## コマンド

| コマンド | 内容 |
|---|---|
| `barnull run` | 常駐（Slack・ポーリング 5分±1分・リマインド・利用完了） |
| `barnull setup` / `set-secret NAME` / `secrets` | 秘密情報の登録・確認 |
| `barnull login sm\|ib` | 画面付きでログインし直す（常駐を止めてから） |
| `barnull doctor` | 接続とログインの確認（常駐を止めてから） |
| `barnull backfill` | 履歴と過去の返信の取り込み（常駐を止めてから） |
| `barnull capture [sm\|ib]` | 管理画面の構造を個人情報を伏せて保存（画面変更の調査用） |
| `barnull simulate …` | DRY_RUN の結合テスト用イベント投入 |

## テスト
```powershell
uv run pytest
```

## データ
- `data/barnull.db`: 予約・問い合わせ・Slack の対応表・操作ログ（SQLite）
- `data/browser/`: ログイン状態（ブラウザのプロファイル）
- `data/screenshots/`: 外部操作の前後のスクリーンショット（30日で自動削除。定型文の送信画面には暗証番号が写るので共有しない）
- `data/logs/barnull.log`: ログ（暗証番号・パスワード・トークンは伏せる）
- `knowledge/`: 返信文案の根拠（マニュアル、FAQ、過去の返信）
