"""自動ログインのテスト（本番のログイン状態には触れない）。

保存済みの ID・パスワード（資格情報マネージャー）で、空の一時ブラウザからログインできるかを試す。
本番の常駐とはブラウザのプロファイルが別なので、常駐を止めずに実行できる。終わったら一時ブラウザは消える。

  .venv\\Scripts\\python.exe scripts\\test_autologin.py sm          （スペースマーケット）
  .venv\\Scripts\\python.exe scripts\\test_autologin.py ib          （インスタベース）
  .venv\\Scripts\\python.exe scripts\\test_autologin.py sm --show   （ブラウザの画面を表示して試す）

ログインに成功すると、サービスによっては「新しい端末からのログイン」のお知らせメールが届く。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from barnull import secrets  # noqa: E402
from barnull.config import load_config  # noqa: E402
from barnull.logsetup import setup_logging  # noqa: E402
from barnull.models import Platform  # noqa: E402
from barnull.platforms.browser import BrowserPool, LoginRequired, _safe_url  # noqa: E402


async def main(platform: Platform, show: bool) -> int:
    from playwright.async_api import async_playwright

    cfg = load_config()
    secrets.load_redactions()
    setup_logging(cfg, filename="barnull-cli.log")
    url = {
        Platform.SM: f"https://dashboard.spacemarket.com/{cfg.sm_host_id}/inbox",
        Platform.IB: "https://www.instabase.jp/partners/bookings",
    }[platform]
    pool = BrowserPool(cfg, headless=not show)
    with tempfile.TemporaryDirectory(prefix="barnull-logintest-") as tmp:
        async with async_playwright() as pw:
            ctx = await pw.chromium.launch_persistent_context(tmp, headless=not show, locale="ja-JP",
                                                              timezone_id="Asia/Tokyo",
                                                              viewport={"width": 1400, "height": 960})
            try:
                page = await ctx.new_page()
                await page.goto(url, wait_until="domcontentloaded")
                await pool._settle(page, None)
                if not await pool._needs_login(platform, page):
                    print("ログイン画面が出ませんでした（想定外）。URL:", _safe_url(page.url))
                    return 2
                print(f"{platform.label}: ログイン画面を開きました。保存済みの ID・パスワードで自動ログインを試します…")
                try:
                    await pool._auto_login(platform, page)
                except LoginRequired as e:
                    print(f"✗ 自動ログインに失敗しました: {secrets.redact(str(e))}")
                    print("  原因の記録: data/logs/barnull-cli.log（「自動ログイン失敗」の行）と "
                          "data/screenshots/ の *_login_failed.png")
                    return 1
                await page.goto(url, wait_until="domcontentloaded")
                await pool._settle(page, None)
                if await pool._needs_login(platform, page):
                    print("✗ ログインの操作は通りましたが、管理画面を開くとまたログイン画面になりました。URL:", _safe_url(page.url))
                    return 1
                print(f"✓ 自動ログインに成功しました（開いた画面: {_safe_url(page.url)}）")
                return 0
            finally:
                await ctx.close()


if __name__ == "__main__":
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="自動ログインのテスト（本番のログイン状態には触れない）")
    ap.add_argument("platform", choices=["sm", "ib"])
    ap.add_argument("--show", action="store_true", help="ブラウザの画面を表示する")
    a = ap.parse_args()
    sys.exit(asyncio.run(main(Platform(a.platform), a.show)))
