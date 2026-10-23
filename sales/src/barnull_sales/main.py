"""コンテナの入口。環境変数 ROLE で sales-webhook / sales-worker を切り替える（同じイメージを2つのサービスで使う）。"""

import os

if os.environ.get("ROLE", "worker") == "webhook":
    from .app_webhook import app
else:
    from .app_worker import app

__all__ = ["app"]
