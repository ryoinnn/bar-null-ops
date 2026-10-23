# インスタベース（IB）管理画面 調査メモ

- 調査日: 2026-10-09（Claude in Chrome。ログインはご本人が実施。読み取りのみで、送信・投稿・スレッド作成は未実施）
- ゲスト氏名・電話番号は本メモに記録しない
- Rails（サーバーレンダリング）と一部 React コンポーネント。CSS は Bootstrap と Tailwind が混在

## 1. ログイン（自動再ログイン用）
- URL: `https://www.instabase.jp/partners/sign_in`（セッション切れ時、`/partners/*` からここへリダイレクト）
- `form[action="/partners/sign_in"][method=post]`
  - `#owner_email`（`owner[email]`）、`#owner_password`（`owner[password]`）
  - `#remmenber_me`（`owner[remember_me]`、綴りは原文ママ）→ チェックする
  - `input[type=submit][name=commit][value="ログイン"]`
- reCAPTCHA 等は無し（2026-10-09 時点）
- 画面に「プライバシーポリシー・サービス利用規約・スペース掲載規約に同意の上ログインしてください」の文言あり
- 2026-10-09 に「プライバシーポリシー改定に伴い、再度ログインをお願いします」でログアウトされていた。規約改定時は強制ログアウトがありうる

## 2. 予約一覧 `https://www.instabase.jp/partners/bookings`
- `table.partner-bookings-table` / 行 `tr.partner-bookings-table-body-row`
- 列: 予約ID（`td:nth-child(1) a[href="/partners/bookings/<予約ID>"]`）／スペース名＋オプション／ステータス／予約者名（`a[href="/partners/users/<ユーザーID>"]` と人数）／利用日時（`span.booked-date`）／予約金額（税込）と税
- ステータス表記: 予約確定／予約リクエスト中／予約リクエスト非承認／利用者キャンセル（「利用日のN日前」付き）／特別キャンセル／予約リクエストキャンセル／予約未確定、決済: 支払い済み／返金済み、支払方法: クレジットカード／PayPay
- 既定では利用日が3ヶ月以上前の予約は非表示（絞り込みで変更可）。CSV ダウンロードボタンあり
- 予約IDは10桁の乱数風（予約順に増えない）

## 3. 予約詳細 `https://www.instabase.jp/partners/bookings/<予約ID>`
取得できる項目（テキスト）:
- 料金プラン、無料設備・オプション（例「ゴミ処理 ×1個」）
- 利用日時、予約状況（予約確定 等）、決済状況
- スペース利用料（税抜）、オプション利用料、消費税、合計（税込）
- **手数料差引後の支払い額**: 内容／予約金額(税込)／手数料／差引後の支払い額(税込)、行ラベル「スペース利用料 (売上料率65%)」
- **振込額合計(税込)** ← 店舗収益に使う（例 合計 18,150円 → 振込 11,797円）。フォールバック率は 0.65
- ご利用内容: 予約ID、氏名、フリガナ、電話番号（ボタンで表示。**押さない・保存しない**）、用途、用途詳細、利用人数、申込日時
- ボタン: 「変更手続き」（`/partners/bookings/<id>/booking_changes/new`）、「予約キャンセル」→ **押さない**
- メッセージ欄（React コンポーネント `BookingMessage`）:
  - props: `hasThread`, `threadPath`, `createThreadPath="/api/v1/service/partners/message_threads"`, `roomId`（例 299651）, `userId`, `isOwnerUnread`
  - スレッドあり: 「メッセージを確認する」ボタン → `window.location.href = threadPath`（遷移のみ）
  - スレッドなし: ボタン押下で `createThreadPath` に POST してスレッドを作る作り（文言・挙動は未確認。スレッド未作成の予約が来たときに確定）
  - 「未読のメッセージがあります」表示あり
- 準備・清掃時間の表示位置は未確認（該当予約が無かった。メールには「準備日時」「清掃日時」が載る）

## 4. メッセージ
### 一覧 `https://www.instabase.jp/partners/inbox`
- 10件／ページ（ページャ `?page=2` 等）。ヘッダに「メッセージ N 未読 N 件」
- 絞り込み: `message_filter[room_id]`（スペース）、**`message_filter[unreplied]`（未返信のみ）** → 返信漏れ検知のポーリングに使う
- 行リンク `a[href^="/partners/inbox/<スレッドID>"]`、最終メッセージ日付

### スレッド `https://www.instabase.jp/partners/inbox/<スレッドID>`
- タイトル: `<氏名>様 (ID: <ゲストID>) : メッセージ｜インスタベース` → **ゲストID（10桁）が取れる。リピート判定に使う**
- **スレッドは予約単位ではなく「スペース×ゲスト」単位**。右側に「予約（最新5件）」としてそのゲストの予約一覧（ステータス・日時・ご注文番号＝予約ID）が出る。リピーターは同じスレッドに続く
- メッセージ要素 `div.flex.mt-6`（新しい順に上から表示）
  - ホスト: クラスに `justify-end` を含む（「既読」表示あり）
  - システム: 本文が「【自動配信メッセージ】」で始まる（予約確定、変更承認 等）
  - ゲスト: 上記以外
  - 日時: `YYYY/MM/DD HH:MM`
  - 添付: `a[href^="https://instabase-attached-document.s3"]`、ファイル名は `<数字>-Bar_null_スペース利用マニュアル.pdf`
- 既存運用では定型文と PDF が同時刻の別メッセージとして表示されていた（1回の送信で両方送った可能性が高い）
- 予約変更リクエスト（ゲスト発）もこのスレッドに「予約内容の変更を希望します。」として届き、回答 URL `https://www.instabase.jp/partners/bookings/<変更ID>/booking_change_responses/new` が載る → **ホストの回答が必要。Slack に⚠️で通知する（自動回答はしない）**

### 送信フォーム
- `form[action="/partners/messages"][method=post]`（hidden: `authenticity_token`, `message_thread_id`）
- 本文 `textarea#message_context`（`message[context]`、placeholder「メッセージを記入してください」）
- 添付 `input#message_file`（`message[file]`、accept に `application/pdf` を含む）
- 送信 `input[type=submit][name=commit][value="送信"]`
- → 本文と PDF を**1回の送信**で送れる見込み

## 5. レビュー
- 投稿前の一覧: `https://www.instabase.jp/partners/bookings/user_review_creatables`（予約ID／物件名／予約者名／利用日時）
- 投稿済み一覧: `/partners/bookings/has_user_reviews`
- フォーム: `https://www.instabase.jp/partners/bookings/<予約ID>/user_reviews/new`
  - `form[action="/partners/bookings/<予約ID>/user_reviews"][method=post]`（PC用と SP用の2つ。表示中のもの＝1つ目を使う）
  - 評価 `select#user_review_point`（`user_review[point]`、値 5〜1、初期値 5）
  - コメント `textarea#user_review_comment`（`user_review[comment]`、字数制限の属性なし）
  - 投稿 `input[type=submit][name=commit][value="レビューを投稿する"]`
  - 「投稿したレビューがこちらのスペース利用者様に通知されます」「スペースページには表示されません」

## 6. 調査中に見つけた要対応事項（システムとは別に、ご本人の確認が必要）
- 2026-10-05 07:28 にゲストから届いた予約変更リクエスト（予約 8797784002、11/01 の終了を 15:30 → 17:00 に延長する希望）が未読・未回答のまま。スレッド 4618365
- 同じ予約で、IB 側の定型文＋マニュアル PDF の送信履歴がスレッドに見当たらない

## 7. 未確認・リスク
- スレッド未作成の予約での「スレッドを作る」ボタンの文言と挙動
- 予約リクエスト制の予約（「予約リクエスト中」）が来た場合の承認 UI（Bar null は即時予約だが、ステータスとしては存在する）
- 準備・清掃時間の表示
- Tailwind のクラス名は変わりうるため、`justify-end` に加えて「既読」表示や右寄せのアバターの位置も判定材料にする
