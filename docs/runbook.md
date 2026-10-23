# 障害対応手順（runbook）

まず `data/logs/barnull.log` と Slack の⚠️メッセージを見る。DB の操作ログは `action_log` テーブル。
起動に失敗した場合（タスクスケジューラから起動すると画面に何も出ない）は `data/logs/startup_error.log` を見る。
`login` などのコマンドのログは `data/logs/barnull-cli.log`。

```powershell
# 直近の操作ログ（DB）
.\.venv\Scripts\python.exe -c "import sqlite3;c=sqlite3.connect('data/barnull.db');[print(r) for r in c.execute('select at,reservation_id,action,result,substr(detail,1,120) from action_log order by id desc limit 30')]"
```

## 0. 常駐の止め方・再開のしかた
常駐はタスクスケジューラの `BarNullOps`（PC にログオンしている間、5分ごとに起動を確認し、止まっていれば起動する）。
- 止める: `Stop-ScheduledTask -TaskName BarNullOps`
  - 止めても5分以内に起動確認が来て再開する。しばらく止めておくときは、先に `Disable-ScheduledTask -TaskName BarNullOps` で無効にする
- 再開: `Enable-ScheduledTask -TaskName BarNullOps`（無効にした場合）→ `Start-ScheduledTask -TaskName BarNullOps`
- `login` / `capture` / `backfill` / `doctor` は常駐と同じロックを使う。常駐が動いていると「常駐プロセスが動いているため実行できません」と出るので、止めてから実行する（実行中は常駐が起動しない）
- 本番・DRY_RUN・模擬は同時に動かせない（ブラウザのログイン状態と Slack の接続を共有しているため）

## 1. システムが止まっている（「停止している可能性があります」「異常終了しました」が出た）
1. PC が起動しているか、スリープしていないか。Windows Update などで再起動した後は、ログオンするまで常駐は動かない
2. タスクスケジューラの `BarNullOps` の状態（`Get-ScheduledTask BarNullOps | Get-ScheduledTaskInfo`）
3. `data/logs/startup_error.log` と `data/logs/barnull.log` の最後を見る
4. 手動で起動して、エラーを見る: 常駐を止めてから `.\.venv\Scripts\barnull.exe run`
5. 「barnull は既に起動しています」と出る場合は、別のプロセスが動いている（タスクマネージャーで `pythonw.exe` を確認）
6. 「巡回が … から成功していません」は、プロセスは動いているが管理画面を読めていない状態。2（ログイン切れ）か 3（画面の変更）を見る

## 2. ログイン切れ（「◯◯への再ログインが必要です」）
保存済みの ID・パスワードで自動再ログインできなかった場合に出る。2段階認証、CAPTCHA、規約同意の画面、パスワード変更などが原因。
自動ログインに失敗すると、30分は再試行しない（同じ失敗を繰り返さないため）。失敗時の画面は `data/screenshots/日付/` の `*_login_failed.png`。
1. 常駐を止める: `Stop-ScheduledTask -TaskName BarNullOps`
2. `.\.venv\Scripts\barnull.exe login sm`（または `ib`）→ 開いた画面でログインを済ませる（実行中は常駐が起動しない）
3. パスワードを変えた場合は `barnull set-secret SM_PASSWORD` で登録し直す
4. 常駐を再開: `Start-ScheduledTask -TaskName BarNullOps`（何もしなくても5分以内に再開する）

## 3. 画面の仕様変更（「巡回が N 回続けて失敗」「入力欄が見つかりません」）
1. 常駐を止めて `.\.venv\Scripts\barnull.exe capture` を実行する。`tests/fixtures/pages/` に現在の画面が、個人情報と暗証番号を伏せた状態で保存される
2. セレクタは `src/barnull/platforms/selectors.py` に集めてある。保存した HTML と見比べて直す
   - SM は styled-components のクラス名を、表示名（例 `Reservation__Container`）の部分一致で指定している。ハッシュ部分（`-sc-xxxx` 以降）は使わない
   - SM のボタンは `<a type="anchor">` が多い（`SM_BUTTON`）。確認画面は独自の Modal（`Modal__ChildrenWrapper`）
   - IB はサーバー描画のフォーム（`#message_context` 等）と Tailwind のクラス（`justify-end` でホストの発言を判定）。送信ボタンには確認ダイアログがある
3. 文字列の解釈は `parse_sm.py` / `parse_ib.py`。`uv run pytest` でテストを通す
4. DRY_RUN で `simulate` を使って確認してから本番に戻す
5. 「ブラウザ（Chromium）が見つかりません」は画面の変更ではない。`.venv\Scripts\python -m playwright install chromium`

## 4. 二重送信・誤送信が疑われる
送信の仕組み:
- 状態の比較更新（`NOTIFIED/ON_HOLD/ERROR → EXECUTING`）で、同じ予約への処理は1回しか始まらない
- 定型文（暗証番号）は、予約が確定している（SM: 予約完了、IB: 予約確定）ときだけ送る。承認制リクエストは承認後に状態が変わったことを確かめる。支払い待ちなら送らず、支払いの確認後にもう一度スタンプしてもらう
- 送信の直前に管理画面を読み、定型文（「キーボックスの暗証番号」の語と暗証番号）と PDF が既にあれば送らない
- 返信は、送信の直前にスレッドを読み、同じ文面が送信済み、管理画面で手動返信済み、ゲストの新着がある、のどれかなら送らない
- プロセスが送信の途中で止まった場合は、起動時に「⚠️ 処理の途中でシステムが停止しました」を出す。管理画面で送れたかを確認し、送れていなければそのメッセージにスタンプ（送信済みの部分は飛ばす）

確認の手順:
1. 管理画面のスレッドで、ホストの発言を時刻つきで確認する
2. `action_log` で、その予約の `template` / `send_inquiry` の行と `actor`（誰のスタンプか）、`trigger`（どのイベントか）を見る
3. スクリーンショット（`data/screenshots/日付/`）の `_before` / `_after` を見る（Slack には上げていない）
4. 本当に二重送信・誤送信していたら、ゲストへのお詫びは手動で送る。原因を直すまで常駐を止める
   - `dry_run = true` に戻すだけでは、動いている常駐は止まらない（設定は起動時にしか読まない）。先に「0. 常駐の止め方」で止め、`config.toml` を書き換えてから再開する

## 5. 文案が来ない、変な文案が来る
- `ANTHROPIC_API_KEY` が未登録または無効 →「文案を作れませんでした」「Claude API の呼び出しに失敗しています」と出る。`barnull set-secret ANTHROPIC_API_KEY`
- 文案の根拠は `knowledge/manual.md`・`knowledge/faq.md`（人が書く）と、自動で追記される `knowledge/faq_learned.md`（承認者が修正して送った回答）・`knowledge/past_replies/`（送った返信）。
  FAQ に正しい回答を書き足すと、以後の文案に反映される。自動で追記された回答が不適切なら、`faq_learned.md` から消してよい
- 文案に【要確認】や、LINE・メール・振込・緊急連絡先以外の電話番号があると「要確認」になり、スタンプでは送れない（修正文を返信して送る）
- モデルと effort は `config.toml` の `[llm]`

## 6. 暗証番号を変えた
1. `.\.venv\Scripts\barnull.exe set-secret KEYBOX_CODE`（以後の定型文は新しい番号で送る。ログ・Slack の伏せ字も10分以内に新しい値になる）
2. 常駐を再起動する（「0. 常駐の止め方」）
3. **すでに旧番号を受け取った、これから利用する予約のゲストに、新しい番号を知らせる**（自動では送り直さない）。
   対象は Slack の親メッセージが「✅ 送信済み」で利用日がこれからの予約。管理画面のメッセージで連絡する

## 7. Slack で反応しない
- スタンプ・返信は承認者（`config.toml` の `approvers`）のものだけ受け付ける
- 承認として扱うスタンプは `config.toml` の `approve_reactions`（✅ 👍 🆗 など）。それ以外（👀 🤔 など）では何もしない（一度だけ案内を出す）
- スタンプは「親メッセージ」「リマインド」「文案」「⚠️ エラー」「確認メッセージ」に付けたものだけが対象
- 修正文の返信は、すぐには送らず「この文面で送信します」の確認を出す。確認のメッセージにスタンプで送信
- 未送信の文案を送らない場合は、スレッドに「送らない」と返信
- 停止中に、同じ予約へスタンプと返信の両方があった場合は、どちらが先か分からないためスタンプは実行しない（確認を求める）
- Bot がチャンネルにいるか（`barnull doctor`。常駐を止めてから）
- DRY_RUN 中はテスト用チャンネルに投稿される

## 8. 本番でまだ一度も動いていない操作（最初の1件はスクリーンショットで確認する）
- SM の「承認」ボタンと承認の確認画面（画面文言から推定して作っている）
- IB の承認制リクエストの承認、IB でスレッドがまだ無い予約のスレッド作成
- IB のレビュー投稿（SM のレビュー画面・確認画面・投稿の通信は 2026-10-12 に実画面で確認し、投稿を止めた状態で手順を検証済み）

## 8b. レビュー投稿
- 既定: 利用後（退室時の状態が「問題なく退室しました」）に★5・お礼文を自動で投稿する
- 承認者がスレッドに返信して指示できる（利用前・利用中でも可）
  - `★4` / `★3 コメント「…」`（コメントは10〜400字）→ 確認メッセージが出る。スタンプで投稿
  - `コメントは「…」で` → 評価はそのままで、コメントだけ変える
  - `レビューしない` → 投稿せずに終了。期限内なら、あとで `★5` などと返信すれば再開できる
  - `レビューはまだ` `レビューは一旦止めて` → 保留（確認メッセージへのスタンプまで投稿しない）
  - レビュー・評価・星に触れた、はっきりしない返信 → 自動投稿を止めて確認を求める（ゲストへの修正文なら、通常どおり送信の確認も出る）
  - ゲストに送る文面で「レビュー」「評価」から始まるものは、先頭に【修正】を付ける（付けないとレビューの指示として読む）
- ⚠️「自動処理に失敗しました：レビュー投稿」→ 内容の「再実行で投稿する内容」を確かめ、**最新の**⚠️メッセージにスタンプで再実行。
  失敗時の画面は `data/screenshots/日付/`。投稿済みか分からない場合は投稿しないので、管理画面で確認してから再実行する
- ⚠️ に「投稿の操作の後に止まったため、投稿されたか分かりません」→ 再実行のスタンプは受け付けない（二重投稿の防止）。
  管理画面でレビューを確認し、投稿されていなければスレッドに `★5` などと返信 → 確認メッセージにスタンプ
- 「投稿されていない SM のレビュー」一覧 → システムを入れる前の予約など。必要なら管理画面で手動で投稿する（期限に注意）

## 9. PC を再起動してもログオンせずに動かしたい
今のタスクは「ログオン時」に起動する。Windows Update の夜間の再起動などの後もログオンせずに動かすには、
Windows のパスワードを入力して「PC 起動時」に登録し直す: `powershell -ExecutionPolicy Bypass -File scripts\install_task.ps1 -AtStartup`
（2026-10-17 に登録済み。**管理者の PowerShell** で実行し、パスワード欄には PIN ではなく **Microsoft アカウントのパスワード**を入れる。Microsoft アカウントのパスワードを変えたら、同じ手順で登録し直す。PC を閉じてスリープしている間は動かない）
