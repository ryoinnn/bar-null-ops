# 手作業のセットアップ手順（売上管理）

アカウントの作成、トークンの発行、規約への同意、カード登録、権限の付与は、ご本人の作業です（Claude は代行しません）。
トークンはチャットに貼らず、「6. この PC への登録」で伏せ字のウィンドウから登録してください。
多くのトークンは発行時に一度しか表示されません。6 の登録まで、メモ帳などに一時的に控えておき、登録したら消してください。

Phase 0 の疎通確認に必要なのは **1〜6**。LINE（7）は Phase 1 の前まででかまいません。

## 1. Google Cloud（0x0.co.jp の Google アカウントで）

### 1-1. プロジェクトを作る
1. https://console.cloud.google.com/projectcreate を開く
2. プロジェクト名に `barnull-sales` と入力する。その下に表示される **プロジェクト ID** を控える（後で使う。秘密ではない）
3. 組織・場所はそのまま →「作成」

### 1-2. 請求先アカウントをつなぐ
1. 画面上部のプロジェクト選択で `barnull-sales` を選ぶ
2. 左上のメニュー →「お支払い」→「請求先アカウントをリンク」
3. 既存の請求先アカウントがあればそれを選ぶ。無ければ「請求先アカウントを作成」で、国は日本、支払い方法（カード）を登録する
   - 無料トライアル（クレジット付与）が表示された場合は、使っても使わなくてもかまいません

### 1-3. 予算アラートを作る（課金を止める仕組みの土台）
1. 左上のメニュー →「お支払い」→「予算とアラート」→「予算を作成」
2. 名前: `barnull-sales-guard`。範囲: プロジェクトは `barnull-sales`、サービスは「すべてのサービス」→「次へ」
3. 金額: 予算タイプは「指定額」、目標金額は `100`（円）→「次へ」
4. しきい値: 「実際」で 1%・50%・100% の3つにする
5. 「課金管理者とユーザーにメールアラートを送信する」をオンにする
6. 「Pub/Sub トピックを接続」は空のままでよい（Phase 1 の最初に Claude が gcloud で接続し、費用が出たら課金を止める関数を置く）
7. 「終了」

### 1-4. この PC に gcloud CLI を入れてログインする
PowerShell で次を順に実行します（2行目以降は、インストール後に新しい PowerShell を開いてから）。
```powershell
winget install -e --id Google.CloudSDK
gcloud.cmd auth login
gcloud.cmd config set project <1-1 で控えたプロジェクト ID>
```
- `gcloud.cmd auth login` でブラウザが開くので、0x0.co.jp のアカウントを選んで許可してください。
- この PC の PowerShell はスクリプト（.ps1）の実行が無効になっているので、`gcloud` ではなく **`gcloud.cmd`** と打つ（Windows の設定は変えない）。
- インストール直後の同じ画面で `gcloud.cmd` が見つからない場合は、PowerShell を開き直すか、次の1行で PATH を読み直す。
  ```powershell
  $env:Path = [Environment]::GetEnvironmentVariable('Path','Machine') + ';' + [Environment]::GetEnvironmentVariable('Path','User')
  ```

## 2. Square（開発者アプリ。疎通確認用のトークン）
1. https://developer.squareup.com/apps を開き、Bar null の Square アカウント（オーナー）でサインインする
2. 「Create your first application」（または「+」）→ アプリ名 `Bar null 売上管理` → 保存
3. 作成したアプリを開く → 画面上部の切り替えを **Production** にする → 左メニュー「Credentials」
4. 「Production Access token」の「Show」→ コピー → **`SQUARE_ACCESS_TOKEN`**
   - このトークンは全権限なので、この PC での疎通確認（読み取り）にだけ使う
   - 本番は読み取り専用の OAuth にする（Phase 1 以降で、許可の画面を1回押していただく）

## 3. マネーフォワード クラウド会計（APIキー）
1. マネーフォワード クラウドにログインし、アプリポータルを開く → 左メニュー「連携設定」→「APIキー管理」
   - メニューが無い場合は、アプリポータルの権限（「アプリ開発」など。付与できるのはシステム管理者）が必要
2. 「単一事業者」タブ →「新規登録」
3. APIキー名: `barnull-sales`。利用可能サービス: **クラウド会計だけ** を選ぶ
4. 利用規約を確認して「各利用規約に同意する」にチェック → 右上の「登録」
5. 「APIキーを作成しました」画面の APIキーをコピー → **`MF_API_KEY`**（`mf_api_` で始まる。**再表示できない**）→「確認しました」
   - キーは発行したユーザーの権限で動きます（閲覧のみに絞る設定はありません）。システムは読み取りの API だけを呼びます

## 4. Asana（0x0.co.jp のアカウントで）
1. https://app.asana.com/0/my-apps を開く（右上のプロフィール →「設定」→「アプリ」→「開発者アプリを管理」からも行ける）
2. 「Personal access tokens」→「Create new token」（新しいトークンを作成）
3. 名前: `barnull-sales`。API の利用規約に同意 →「Create token」
4. 表示されたトークンをコピー → **`ASANA_PAT`**（一度しか表示されない）
   - プロジェクト「Bar null 売上管理（伝票）」は、Phase 1 でセットアップスクリプトが作る（作る前に確認をお願いします）

## 5. Slack
1. https://api.slack.com/apps →「Create New App」→「From a manifest」
2. @barnull（予約対応）と同じワークスペースを選ぶ →「Next」
3. YAML のタブを選び、`sales/slack/manifest.yml` の中身をすべて貼り付ける →「Next」→「Create」
4. 左メニュー「Install App」→「Install to ワークスペース名」→「許可する」
5. 表示された **Bot User OAuth Token**（`xoxb-` で始まる）をコピー → **`SLACK_BOT_TOKEN`**
6. Slack でチャンネルを2つ作る: `#barnull-sales`（本番）と `#test_barnull-sales`（テスト）。公開・非公開はどちらでもよい
7. 両方のチャンネルで `/invite @barnull_sales` を送る
8. （任意）App の設定画面「Basic Information」→「Display Information」でアイコンを設定する

## 6. この PC への登録
1. PowerShell（または Claude の Terminal タブ）で次を実行する
   ```powershell
   cd C:\Users\ryoui\Documents\bar-null-ops
   uv run python sales/scripts/set_secret.py
   ```
2. 未登録の項目ごとに、伏せ字の入力ウィンドウが順に開く。Ctrl+V で貼り付けて「登録」を押す。順番は次のとおり
   1. `SQUARE_ACCESS_TOKEN`
   2. `MF_API_KEY`
   3. `ASANA_PAT`
   4. `SLACK_BOT_TOKEN`
   5. `ANTHROPIC_API_KEY`（売上管理用に新しく発行する場合だけ。予約対応のキーを使い回すなら空のまま閉じる）
   6. `LINE_CHANNEL_SECRET`、`LINE_CHANNEL_ACCESS_TOKEN`（今は空のまま閉じる。7 が終わってから登録）
3. 先頭の文字が違う値（例: Slack なのに `xoxb-` で始まらない）は、もう一度聞かれる
4. 登録状況の確認
   ```powershell
   uv run python sales/scripts/set_secret.py --status
   ```
5. 一時的に控えたトークンのメモを消す

### Anthropic の APIキー（任意）
- https://console.anthropic.com →「Settings」→「API Keys」→「Create Key」→ 名前 `barnull-sales` → コピー → `ANTHROPIC_API_KEY`
- 同じ画面の「Limits」で、月の使用上限を設定できる（売上管理の Claude の費用は、月数円〜数十円の見込み）

## 7. LINE 公式アカウント（Phase 1 の前まででよい）
1. LINE ビジネスID でログインし、社内 Bot 用の公式アカウントを新規作成する（未認証で可）
   - アカウント名がグループでのメンション名になる（「@アカウント名」）。名前は後で決めるとのことなので、仮の名前でも可（後から変更できる）
   - 2025-06 から、作成時にビジネスマネージャーの組織との接続が必要
2. LINE Official Account Manager →「設定」→「Messaging API」→「Messaging API を利用する」（プロバイダーは 0x0 で作成）
3. LINE Official Account Manager →「応答設定」で、応答メッセージとあいさつメッセージをオフにする
4. LINE Developers コンソール → 作成されたプロバイダー → Messaging API チャネル
   - 「チャネル基本設定」のチャネルシークレット → `LINE_CHANNEL_SECRET`
   - 「Messaging API 設定」のチャネルアクセストークン（長期）を発行 → `LINE_CHANNEL_ACCESS_TOKEN`
   - 「グループトーク・複数人トークへの参加を許可する」をオンにする
   - Webhook の URL・利用・再送は Phase 1 で設定する
5. グループ「Bar null」への招待は、Phase 1 の動作確認が済んでから行う（それまではテスト用のグループで試す）

## 終わったら
チャットで「登録した」と伝えてください。あわせて、次の2つを教えてください（秘密ではありません）。
- GCP のプロジェクト ID
- 作った Slack チャンネルの名前（変えた場合）
