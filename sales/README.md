# Bar null 売上管理（sales）

LINE グループに投稿される伝票を起点に、Square の決済との突合と、銀行入金（マネーフォワード クラウド会計）の確認までを追跡し、
伝票ごとの状態を Slack のスレッドと Asana のタスクに反映するシステム。
予約対応（リポジトリ直下の `src/barnull/`）とは別のパッケージ・デプロイで、同じリポジトリに置いている。

| 文書 | 内容 |
|---|---|
| [docs/requirements.md](docs/requirements.md) | 依頼書（原文） |
| [docs/decisions.md](docs/decisions.md) | 依頼書への回答と決定事項（依頼書より優先） |
| [docs/phase0_report.md](docs/phase0_report.md) | Phase 0 調査レポート（方式・費用・リスク・サンプル読み取りテスト） |
| [docs/design.md](docs/design.md) | 設計書 |
| [docs/setup_checklist.md](docs/setup_checklist.md) | ご本人の手作業（アカウント・トークン） |
| [docs/research/](docs/research/) | 各サービスの公開ドキュメント調査メモ |

## 進捗
- Phase 0（調査・準備）: 完了（2026-10-11）
- Phase 1（受信〜起票）: 完了（2026-10-17、[docs/phase1_report.md](docs/phase1_report.md)）。課金の自動停止は接続待ち
- Phase 2（Square との突合）: 完了（2026-10-17、[docs/phase2_report.md](docs/phase2_report.md)）
- Phase 3（入金の確認）: 完了（2026-10-17、[docs/phase3_report.md](docs/phase3_report.md)）。現金の自動照合は担当者の名義の一覧待ち
- Phase 4（未収・訂正・手動対応）: 完了（2026-10-17、[docs/phase4_report.md](docs/phase4_report.md)）
- Phase 5（本番化）: 完了（**2026-10-17 19:23 本番稼働**、[docs/phase5_report.md](docs/phase5_report.md)）

運用は [docs/runbook.md](docs/runbook.md)、スタッフ向けは [docs/staff_guide.md](docs/staff_guide.md)、売上週報は [docs/weekly_report.md](docs/weekly_report.md)。

## 使い方（開発）
- 依存関係: `cd sales; uv sync`
- テスト: `uv run pytest`
- 1通の処理を手元で試す: `uv run barnull-sales try "日付：10/5 顧客名：顧客Aさま 金額：100,000" [--real-claude] [--real-slack] [--real-asana]`
- デプロイ: `sales/` の変更を GitHub の main に push すると、Cloud Build がテストしてデプロイする（`cloudbuild.yaml`）。
  GitHub を使えないときは、リポジトリのルートで `uv run python sales/infra/setup.py deploy`
  （requirements.txt は `uv export --no-dev --no-hashes --no-emit-project -o requirements.txt` で更新）

## フォルダ
- `src/barnull_sales/` 本体（モジュールの役割は [docs/design.md](docs/design.md) の「コードの構成」）
- `tests/` テスト（設定は `tests/sales.test.toml`。顧客名はすべて架空）
- `infra/setup.py` Google Cloud の準備（権限・秘密情報・トリガーなど）、`cloudbuild.yaml` デプロイ
- `scripts/` Phase 0 の検証用スクリプト、秘密情報の登録（`set_secret.py`）
- `slack/manifest.yml` Slack App のマニフェスト
- `samples/` 伝票サンプルとテスト結果（顧客名を含むため git に入れない）
