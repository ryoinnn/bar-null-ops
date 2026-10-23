# Asana / Slack 調査要点（2026-10-10、サブエージェント）
- Asana Personal（無料）はカスタムフィールド不可（API は 402）。Starter 年払い $10.99/人/月〜。Personal は2ユーザーまで
- Asana PAT: 無期限、本人権限そのまま（範囲を絞れない）。添付 POST /attachments 100MB/ファイル、容量無制限（全プラン）
- セクション移動 addTask、完了 PUT completed、タグ addTag。変更検知は GET /tasks?project&modified_since（CF変更が modified に入るか実機確認要）
- Asana レート: 無料 150/分、有料 1,500/分
- Slack files.upload は 2025-11-12 終了 → getUploadURLExternal → POST → completeUploadExternal(channel_id, thread_ts)。ts は返らない
- chat.update は自 Bot の投稿のみ。update でのメンションは通知されない可能性 → 通知はスレッド返信で
- Slack 無料: 90日閲覧、1年超は削除、アプリ10個まで。Pro ¥925/人/月（年払い）
- スコープ案: chat:write, files:write, channels:join, users:read, (users:read.email), (channels:history)
