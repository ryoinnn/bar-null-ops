# 設計: 勤怠・スタッフ管理・給与（実装計画）

- 状態: **案**（2026-11-14）。本人の了承後に実装する
- 要件: [labor_requirements.md](labor_requirements.md)。作法は売上の伝票と同じ（事実を保存 → 状態を計算、保存と反映を分ける、やり直しても同じ結果）

## 1. 全体
```
LINE「Bar null」 @null ─▶ sales-webhook（変更なし）─▶ Cloud Tasks ─▶ sales-worker /tasks/process-event
   extract.py が種類を判定: slip_content（伝票。プロモーターフラグを追加）／ attendance（勤怠。新）／ …
   ├ 勤怠 → attendance.py: 本人の特定 → 期限の判定 → 記録 or 承認待ち
   └ 伝票 → 既存の処理。保存のたびに「本人の売上」が変わったかを見て、勤怠の売上と給与を直す
/jobs/reconcile（30分ごと。既存）に足すステップ:
   staff_sync（Asana の台帳を読む）→ attendance_approvals（Slack の ✅/❌）→ attendance_overrides（Asana の手動上書き）
   → attendance_resync（反映のやり直し）→ payroll（第1水曜 17:00 の計算、10日 9:00 の確定）
```
新しい Cloud Run・Cloud Scheduler・Secret は作らない。

### 本番に出す前の試し方
`[labor] dry_run = true`（勤怠だけの切替。売上は今の本番のまま）。true のあいだ、勤怠・台帳・給与は **テスト用の Asana プロジェクトと #test_barnull-sales** に書く。
LINE の本番グループで実際に打って確かめ、本人の確認後に false にする。

## 2. データ（Firestore が正本。Asana は台帳）

| コレクション | 内容 |
|---|---|
| `staff/{スタッフID}` | 台帳の写し: 本名、LINE名、区分、入社日、時給の履歴 `[{from, wage}]`、バック率、LINE ID の一覧、Asana の gid |
| `attendance/{記録ID}` | 勤怠1件: スタッフID、LINE ID、営業日、開始・終了（ISO）、時間、本人の売上、時給の額、状態、訂正の履歴、Slack・Asana の ID、sync_pending |
| `attendance_pending/{イベントID}` | 承認待ち: 解析結果、送信時刻、quoteToken、Slack の ts、結果（承認／却下） |
| `payroll/{YYYY-MM}_{スタッフID}` | 給与1件: 勤務日数、時間、時給の分、売上、バック、調整額、給与、状態（計算済み／確定）、再計算の履歴、Asana の gid |
| `payroll_runs/{YYYY-MM}` | 月の計算・確定をしたか（二重に動かさない） |

- スタッフID: 台帳の Asana タスクの gid（台帳に無い人は `line-{LINE ID の先頭8文字}` で仮に作る）
- 記録ID: `W-{営業日 YYYYMMDD}-{スタッフID}`。同じ人・同じ日は同じ ID なので、二重にできない（2回目は上書き）
- 時給・バック率・給与は Firestore と非公開の Asana にだけ置く。git・ログ・Slack には出さない

## 3. 勤怠の処理（`attendance.py`）
1. 解析（`extract.py` に `attendance` を追加）: 日付（月・日）、勤務開始・勤務終了（`HH:MM`、`25:00` 可）。時刻の日付への当てはめは Python で行う（§要件 2-1）
2. 検証: 時間が 0 より大きく 15 時間以下。外れたら「読み取れない勤怠」として Slack に出す（スタンプなし）
3. 本人の特定: LINE ID → 台帳。無ければ表示名を台帳の LINE名と照合（空白・敬称を除く）し、合えば LINE ID を台帳に記録。
   どちらも無ければ台帳に仮の人を作り（LINE名 ＝ 表示名、ほかは空）、Slack で「台帳に足してください」
4. 期限: `送信時刻 > 営業日+2日 9:00` か `勤務開始 > 送信時刻` なら承認待ち（§5）。給与が確定した月なら記録せず Slack で知らせる
5. 記録: `attendance/{記録ID}` を保存（トランザクション）→ その日の本人の売上・時給の額を計算 → Slack に投稿・Asana を作成／更新 → LINE に OK スタンプ
6. 同じ記録 ID がすでにあれば訂正: 変更前後を Slack のスレッドに出す。同じイベントを二度は当てない（履歴にイベント ID）
7. 給与が「計算済み」の月なら、その人の給与を計算し直す（§6）

### Slack（`#null_staff-management`）
- 1件ごとに親メッセージ: `本名｜11/23（日）21:00〜03:00　6.0h｜今月の平均 5.4h（12日・65.0h）`。訂正はスレッドと親の書き換え
- 承認の依頼、台帳に無い人、読み取れない勤怠、給与の計算・確定・再計算、確定後の売上の変化もここに出す（額は出さない。「Asana の給与を見てください」とリンクだけ）

## 4. 伝票のプロモーターフラグ
- `extract.py` の伝票の項目に `promoter_name`（無ければ空）を足し、プロンプトに「プロモーターフラグ：」の行を説明する
- `Slip.promoter_name` を足す。Slack の親メッセージに `プロモーター：〇〇`、Asana に「プロモーター」（テキスト）、`restore-from-asana` で戻す
- 訂正（`correction`）でプロモーターも直せるようにする
- 本人の売上の帰属（§要件 4-2）: プロモーター名があれば台帳の LINE名（無ければ本名）で照合したその人、無ければ送った人の LINE ID

## 5. 期限外の承認
1. LINE: 元のメッセージを引用して「期限外のため承認待ちです」と返信（reply。無料）
2. Slack: 桝本をメンションして依頼を投稿。`attendance_pending` に ts を保存
3. 30分ごとの `attendance_approvals`: 承認待ちの投稿のリアクションを読む（`reactions.get`）。**桝本（alert_mention）が付けたものだけ**数える
   - ✅（`white_check_mark`）→ 記録（§3 の 5〜7）し、LINE に元のメッセージを引用して「承認されました」とプッシュ送信
   - ❌（`x`）→ 記録しない。Slack のスレッドに「却下」を残す
4. 7日たっても反応が無ければ、もう一度だけメンションする

## 6. 給与（`payroll.py`）
- 計算: `給与 ＝ Σ(勤務時間 × その日の時給) ＋ 本人の前月の売上 × バック率 ＋ 調整額`（円未満切り捨て）。時給は勤務日に有効な履歴の額
- `payroll` ステップ（30分ごと）: 
  - 今が「その月の第1水曜 17:00」を過ぎ、`payroll_runs/{前月}` が未計算 → 管理者を除く全員を計算（状態: 計算済み）→ Asana に作成、Slack に「計算しました」
  - 今が「10日 9:00」を過ぎ、計算済み → 確定（Asana の状態も確定）
- 計算し直し（定期的な見回りはしない。保存のときに動く）:
  - 伝票の保存（`slip_ops.save`・起票）で、伝票の「帰属先・営業日・金額・取消」のどれかが変わったら、変わる前後の人と月を調べる
  - 勤怠の記録・訂正・承認・手動上書きのとき、その人と月
  - その月が計算済み → その人の給与を計算し直して Asana を更新し、Slack に「再計算しました」
  - その月が確定 → 計算し直さず、Slack に「確定後に前月の売上が変わりました（〇〇さん）」
- 確定後の変更: 管理者が Asana の給与タスクの「調整額」「調整の理由」を入れる → 30分以内に取り込み、給与に足す

## 7. Asana（新しいプロジェクト。本番とテストの2つずつ、非公開・管理者2人だけ）

| プロジェクト | 1タスク | 主な項目 |
|---|---|---|
| Bar null 勤怠 | 1人 × 1営業日 | スタッフ名、稼働日、稼働時間（h）、勤務開始、勤務終了、売上（本人）、時給の額、状態（記録済み／対象外）、記録ID、スタッフID、店舗、Slackスレッド、手動上書き（なし／時間を反映／対象外） |
| Bar null スタッフ台帳 | 1人 | 本名、LINE名、区分（スタッフ／プロモーター／管理者）、入社日、時給、時給の適用開始日、バック率、スタッフID |
| Bar null 給与 | 1人 × 1か月 | 対象月、区分、勤務日数、勤務時間、売上、時給の分、バック、調整額、調整の理由、給与、状態（計算済み／確定）、スタッフID |

- 勤怠の「時給の額」は、画面で「その日時点のコスト」を正しく出すため（時給を途中で変えても、過去の日の額が変わらない）
- 台帳は管理者が Asana で直す（画面から書き込めるようになれば画面から）。30分ごとの `staff_sync` が変更を読み、時給が変わったら履歴に足す
- 勤怠の手動上書き: 管理者が「勤務開始・勤務終了」を書き換えて「時間を反映」、または「対象外」→ 30分以内に取り込む
- ID は `asana_ids.json` の `labor` に保存（`uv run barnull-sales asana-setup-labor --env test|prod`）。カスタムフィールドはワークスペース共通

## 8. 設定（`sales.toml` に `[labor]`）
```toml
[labor]
enabled = true
dry_run = true                 # true: 勤怠・台帳・給与はテスト用の Asana と #test_barnull-sales
slack_channel_prod = ""        # #null_staff-management（本人が作成後に ID を入れる）
cycle_start = "21:00"
cycle_end = "09:00"
deadline_extra_days = 1        # 営業日の次のサイクルの終わり（D+2 9:00）まで
max_shift_hours = 15
payroll_run = "first_wednesday 17:00"
payroll_confirm = "10 09:00"
```

## 9. コードの変更
| ファイル | 変更 |
|---|---|
| `extract.py` | 種類 `attendance` と、伝票の `promoter_name` |
| `messages.py` | `attendance` を `attendance.handle` に回す。訂正でプロモーターも直す |
| `slips.py`・`texts.py`・`asana.py`・`restore.py` | プロモーター |
| `slip_ops.py` | 保存の前後で帰属・金額が変わったら `labor_hooks.on_slip_changed` を呼ぶ（失敗しても伝票の保存は止めない） |
| `line.py` | テキストの返信（承認待ち）、引用付きのプッシュ送信（承認の連絡） |
| `slack.py` | `reactions.get` |
| 新 `staff.py`・`attendance.py`・`payroll.py`・`labor_asana.py`・`labor_hooks.py` | 上の各節 |
| `jobs.py` | reconcile にステップを足す |
| `config.py`・`cli.py` | `[labor]`、`asana-setup-labor`、`try` で勤怠も試せるように |
| `tests/` | 勤怠（期限内・期限外・承認・却下・訂正・6時またぎ・確定月）、プロモーター、給与（計算・再計算・確定・時給の変更）。名前と額は架空 |

## 10. 本人にお願いすること（実装の途中で、手順を出す）
1. Slack: チャンネル `#null_staff-management` を作り、`/invite @barnull_sales`
2. Slack アプリ: 権限 `reactions:read` を足して入れ直す（トークンが変わったら「秘密情報の更新」）
3. Asana: 新しいプロジェクトに岡本さんを追加する（作成はコマンドで行う）
4. LINE: プッシュ送信の無料枠の確認（グループへの送信の数え方を含め、月の上限に収まるか）
5. push（デプロイ）

## 11. 進める順番
1. 伝票のプロモーターフラグ（A が待っている。小さい）→ テスト → コミット → push → A に知らせる
2. 勤怠の受け付け・台帳・Slack・Asana・承認 → `[labor] dry_run = true` で本番の LINE から試す
3. 給与（計算・再計算・確定）→ 日付を指定して手元で試す
4. 画面（A のポータルの試作の結果を待って）
5. staff_guide.md・runbook.md・contract.md §2 の確定版 → 本番（`dry_run = false`）
