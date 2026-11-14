# 開始用の文面: 人件費管理（セッション B）

新しい会話を、作業フォルダ `C:\Users\ryoui\Documents\bar-null-ops` で開き、次の枠の中をそのまま最初のメッセージにする。

```text
Bar null の売上管理自動化（sales/）を、利益ベースで見られるように拡張している。あなたはそのうち「人件費管理」を担当する。

目標: LINE で Bot（@null）にメンションして出勤・退勤の時刻を打つと、それが Asana に記録され、売上レポート（Artifact）の利益セクションに反映される。
売上の伝票（LINE → 解析 → 台帳）と同じ仕組みに乗せ、同じ作法で作る。

最初に、次の順で読んで、現行のシステムを理解してから始めること（コードも必要な範囲で読む）。
1. sales/docs/profit/README.md（全体の分担）
2. sales/docs/profit/handoff.md（現行の仕様・守るルール・作法）
3. sales/docs/profit/contract.md（レポートとの約束。あなたの担当は §2。確定したら最終版に書き換える）
4. sales/docs/design.md、sales/docs/decisions.md、sales/docs/staff_guide.md、sales/docs/runbook.md
5. sales/src/barnull_sales/ の extract.py、process.py、messages.py、slip_ops.py、asana.py、jobs.py、tests/

進め方:
- 最初に、本人への確認事項を1回にまとめて聞く。決められるものは推奨を添えて自分で決めて進める。
  聞くこと（例）: 同じ LINE グループ「Bar null」で足してよいか／勤怠の打ち方（「@null 出勤」など。自由な書き方も許すか）／休憩の扱い／
  朝6時をまたぐ勤務／打ち忘れ・打ち間違いの訂正の方法／時給の形（時給・日給、深夜割増、交通費）と、額を誰が見られるようにするか（contract.md §6-1）／
  対象のスタッフと LINE 表示名の対応／管理者（桝本）の勤務を含めるか。
- 設計案を短くまとめて本人に見せ、了承を得てから実装する。実装は dry_run（テスト用の Asana プロジェクト・Slack チャンネル）で動かし、本人が確認してから本番にする。
- 既存の worker に処理を足す方針（新しい Cloud Run や Cloud Scheduler を増やさない。無料枠と課金の自動停止に注意）。
  打ち忘れの検知など定期の処理は、既存の daily ジョブに足す。
- テストを書き（tests/）、`cd sales; uv run pytest` が通ることを確かめる。顧客名・従業員名・時給はテストにも実在のものを書かない。
- レポートの画面（sales/report/weekly_report.html）は別セッション（A）が担当する。あなたは直さない。
  Asana の項目・記録 ID・状態が確定したら contract.md §2 を最終版にして、本人に知らせる。
- 守るルール（handoff.md §4）: 日本語、パスワード等を入力しない、組織ポリシーや権限の変更は本人が実行、個人情報と時給を git・ログに入れない、
  コミットしてよいが push は本人に依頼する。

ゴールの完成条件: スタッフが LINE に打った勤怠が、営業日ごとに Asana の勤怠プロジェクトへ冪等に記録され、訂正・打ち忘れに対応でき、
スタッフ向けの案内（docs/staff_guide.md に節を追加）と運用手順（runbook.md）が揃い、contract.md の §2 が実装と一致していること。
```
