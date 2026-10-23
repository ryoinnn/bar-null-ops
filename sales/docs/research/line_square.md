# LINE / Square 調査要点（2026-10-10、サブエージェント）
## LINE
- 無料200通/月（Push 系。グループ Push は人数分）。Reply は対象外。受信は無料（推測）
- 署名: 生 body を HMAC-SHA256(channel secret) → Base64。再送は初期オフ、回数・期間非公開、保証なし。2秒以内に 2xx 必須
- source.userId はプロフィール取得未同意ユーザーで欠ける（PC専用ユーザー。まれ）
- members/ids は認証済/プレミアムのみ。member/{userId} は未認証でも使える見込み（中）
- 画像コンテンツは保存期間保証なし → 即保存。contentProvider=line のみ取得可。imageSet{id,index(1〜),total}、順不同、Android 11.15以前は index なし
- 開設: LINE ビジネスID、2025-06-25 からビジネスマネージャー組織との接続必須。未認証可。グループ参加許可は Developers Console（初期無効）。あいさつ・応答は初期オン → OA Manager でオフ
- Webhook: HTTPS（自己署名不可）、3xx はエラー扱いの見込み（中）→ GAS の 302 は不可の可能性
## Square
- ListPayouts / ListPayments は location_id 省略でメイン location のみ → 必ず指定
- Payout: location_id, arrival_date, status(SENT/PAID/FAILED), type, amount_money, payout_fee
- PayoutEntries: CHARGE → type_charge_details.payment_id、REFUND → type_refund_details。gross/fee/net
- Payouts API の日本可否は明記なし（可能性高）
- Payments: source_type CARD/BANK_ACCOUNT/WALLET/BUY_NOW_PAY_LATER/SQUARE_ACCOUNT/CASH/EXTERNAL。cash_details、processing_fee、note(500)、customer_id、refund_ids、order_id。現金取得は Square-Version 2021-03-17 以降
- Orders: 注文メモ欄なし。line_items[].note、tenders[].note
- PAT は全権限・絞れない。読み取り専用は OAuth コードフロー（アクセス30日、リフレッシュ無期限、HTTPS リダイレクト要）
- 振込: 三井住友・みずほは翌営業日（連休分はまとめて）、他行は木〜水を金曜。名義「スクエア (カ」、手数料控除後。店舗ごとに振込口座を分けられる。同一口座の location 間合算は不明（payout は location ごと → 別振込の可能性高）
- 日本は即時入金（1.5%）あり
