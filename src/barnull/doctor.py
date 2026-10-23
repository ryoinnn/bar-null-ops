"""設定・秘密情報・Slack・ログイン状態の確認（常駐プロセスを止めてから実行）。"""

from __future__ import annotations

from slack_sdk.web.async_client import AsyncWebClient

from . import secrets
from .config import Config
from .models import Platform
from .platforms.browser import BrowserPool, LoginRequired
from .platforms.ib import Instabase
from .platforms.sm import SpaceMarket


def _ok(label: str, ok: bool, note: str = "") -> None:
    print(f"  {'✓' if ok else '✗'} {label}{('  ' + note) if note else ''}")


async def doctor(cfg: Config) -> None:
    print(f"設定: dry_run={cfg.dry_run} 投稿先={cfg.channel} 承認者={', '.join(cfg.approvers)}")
    print(f"マニュアル PDF: {cfg.manual_pdf} {'(あり)' if cfg.manual_pdf.exists() else '(見つかりません)'}")
    print("秘密情報:")
    for name, ok in secrets.status().items():
        _ok(name, ok)

    print("Slack:")
    token = secrets.get_optional("SLACK_BOT_TOKEN")
    if token:
        client = AsyncWebClient(token=token)
        try:
            auth = await client.auth_test()
            _ok("Bot トークン", True, f"bot={auth['user_id']} team={auth.get('team')}")
            for ch in (cfg.channel_prod, cfg.channel_test):
                info = await client.conversations_info(channel=ch)
                _ok(f"チャンネル {info['channel']['name']}", info["channel"].get("is_member", False),
                    "" if info["channel"].get("is_member") else "Bot が参加していません（/invite @barnull）")
        except Exception as e:  # noqa: BLE001
            _ok("Slack 接続", False, str(e)[:200])
    else:
        _ok("Bot トークン", False, "未登録")

    print("管理画面のログイン:")
    pool = BrowserPool(cfg, headless=True)
    try:
        sm, ib = SpaceMarket(cfg, pool), Instabase(cfg, pool)
        for platform, fn in ((Platform.SM, sm.list_inbox), (Platform.IB, ib.list_bookings)):
            try:
                rows = await fn()
                _ok(platform.label, bool(rows), f"一覧 {len(rows)} 件")
            except LoginRequired as e:
                _ok(platform.label, False, f"ログインが必要: {e}")
            except Exception as e:  # noqa: BLE001
                _ok(platform.label, False, f"{type(e).__name__}: {str(e)[:200]}")
    finally:
        await pool.stop()
