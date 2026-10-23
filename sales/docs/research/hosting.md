# 基盤・無料枠 調査要点（2026-10-10、サブエージェント）
## GCP
- 無料枠も請求先アカウント必須（実質クレカ）。無料枠は請求先アカウント単位で合算（他プロジェクトと共有）
- Cloud Run 200万req/18万vCPU秒/36万GiB秒、asia-northeast1 は Tier1 で満額。Tasks 100万op無料。Scheduler 3ジョブ/請求先アカウント
- Firestore 1GiB、読5万/書2万/削2万 per day、1プロジェクト1DBのみ無料。TTL・PITR・バックアップは対象外
- GCS 無料5GB は US 3リージョンのみ。東京 $0.023/GB-月（3GB で約10円/月）
- Secret Manager 無料6バージョン・1万アクセス。disabled も課金、destroyed は無料 → MF リフレッシュトークンは追加後に古い版を destroy（放置すると30分更新で初月 約6,500円）
- Artifact Registry 0.5GB、source deploy はクリーンアップポリシー必須
- 予算アラートは通知のみ（止まらない）
- Workspace 管理コンソールで「Google Cloud Platform」サービス OFF だとプロジェクト作成不可（OAuth クライアントを作れなかった原因候補）
- 2024-05-03 以降作成の組織は Domain restricted sharing 強制 → allUsers 不可 → Cloud Run は --no-invoker-iam-check
- 見積り: 既定構成 50〜130円/月、対策後 0〜10円/月
- コールドスタート FastAPI 約3秒 vs LINE 2秒制限 → Webhook 経路の import を軽く、eventId で冪等
## 代替
- GAS: ヘッダー読めない（署名検証不可）、302 応答、6分/実行、トリガー 6時間/日（Workspace）、OAuth クライアント不要、管理者が無効化可能
- Cloudflare Workers 無料: 10万req/日、CPU 10ms/req（I/O 待ち除く）、Cron 5本、Queues 無料可、D1 5GB、R2 10GB（カード要求の報告あり）。Python Workers は 2026-09-21 GA
- Vercel Hobby（cron 1日1回）・Render（スリープ）・Fly（無料なし）・Deno（Python なし）は不適
- GCP 不可時の推奨: Cloudflare Worker（TS、署名検証→Queue→200）＋ GAS（本処理・Sheets/Drive・トリガー）。または Python Workers＋D1（CPU 10ms を要実測）
