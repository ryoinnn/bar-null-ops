# 店舗ごとの設定

Bar null の設定は `sales/sales.toml`（と `asana_ids.json`）のまま。ほかの店舗はここに `stores/<店舗>/sales.toml` と `asana_ids.json` を置く。
コンテナでは環境変数 `SALES_TOML`（Cloud Build の `_SALES_TOML`）で、どの店舗の設定を読むかを選ぶ。
`infra/setup.py --config stores/<店舗>/sales.toml` で、その店舗の GCP を準備する。
