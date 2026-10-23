"""Playwright のブラウザ（プラットフォームごとに永続プロファイル1つ）。

- ログイン状態は data/browser/<platform>/ のプロファイルに保存される
- セッション切れは自動で再ログイン（資格情報マネージャーの ID・パスワード）。
  2段階認証・CAPTCHA・規約同意の画面が出たら止めて LoginRequired を投げる
- ブラウザ操作はプラットフォームごとのロックで直列化する
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from playwright.async_api import BrowserContext, Page, Playwright, async_playwright

from .. import secrets
from ..config import Config
from ..models import Platform
from ..textutil import now

log = logging.getLogger(__name__)

LOGIN_URL = {
    Platform.SM: "https://www.spacemarket.com/login",
    Platform.IB: "https://www.instabase.jp/partners/sign_in",
}
LOGIN_URL_PATTERN = {
    Platform.SM: re.compile(r"spacemarket\.com/login"),
    Platform.IB: re.compile(r"/partners/sign_in"),
}
BLOCKER_PATTERNS = re.compile(r"(認証コード|確認コード|ワンタイム|2段階|二段階|ロボットではありません|recaptcha|hcaptcha|turnstile)", re.I)
# ブラウザ（Chromium）やドライバが落ちた・閉じられたときの例外の文言
CLOSED_PATTERNS = re.compile(r"(Target page, context or browser has been closed|Browser has been closed|"
                             r"Connection closed|browser has disconnected|Target closed)", re.I)
LOGIN_RETRY_AFTER_SEC = 30 * 60  # 自動ログインに失敗したら、この間は再試行しない（同じ失敗を繰り返さない）


def _safe_url(url: str) -> str:
    """ログ用の URL（? 以降を消す。フォームが GET で送られた場合、入力値が URL に載るため）"""
    return (url or "").split("?")[0]


class LoginRequired(RuntimeError):
    """自動ログインできない（人の対応が必要）"""


class DryRunStop(Exception):
    """DRY_RUN で最後のボタンの直前に止まった"""

    def __init__(self, step: str, screenshot: str | None):
        super().__init__(step)
        self.step = step
        self.screenshot = screenshot


class BrowserPool:
    def __init__(self, cfg: Config, headless: bool = True):
        self.cfg = cfg
        self.headless = headless
        self._pw: Playwright | None = None
        self._ctx: dict[Platform, BrowserContext] = {}
        self.locks = {p: asyncio.Lock() for p in Platform}
        self._login_failed_at: dict[Platform, float] = {}

    async def start(self) -> None:
        if self._pw is None:
            self._pw = await async_playwright().start()

    async def stop(self) -> None:
        # close のイベントで self._ctx から消えるので、先に取り出してから閉じる
        ctxs = list(self._ctx.values())
        self._ctx.clear()
        try:
            for ctx in ctxs:
                try:
                    await ctx.close()
                except Exception:  # noqa: BLE001
                    pass
        finally:
            if self._pw:
                try:
                    await self._pw.stop()
                except Exception:  # noqa: BLE001
                    pass
                self._pw = None

    async def context(self, platform: Platform) -> BrowserContext:
        await self.start()
        ctx = self._ctx.get(platform)
        if ctx is None:
            user_dir = self.cfg.browser_dir / platform.value
            user_dir.mkdir(parents=True, exist_ok=True)
            ctx = await self._pw.chromium.launch_persistent_context(  # type: ignore[union-attr]
                str(user_dir),
                headless=self.headless,
                locale="ja-JP",
                timezone_id="Asia/Tokyo",
                viewport={"width": 1400, "height": 960},
                accept_downloads=False,
            )
            ctx.set_default_timeout(30_000)
            # ブラウザが閉じられたら次回は作り直す（キャッシュしたまま使い続けて毎回失敗しないように）
            ctx.on("close", lambda _c, platform=platform: self._ctx.pop(platform, None))
            self._ctx[platform] = ctx
        return ctx

    async def _reset(self, platform: Platform) -> None:
        ctx = self._ctx.pop(platform, None)
        if ctx is not None:
            try:
                await ctx.close()
            except Exception:  # noqa: BLE001
                pass
        if not self._ctx and self._pw is not None:
            # ドライバごと落ちている場合に備えて、Playwright も起動し直す
            try:
                await self._pw.stop()
            except Exception:  # noqa: BLE001
                pass
            self._pw = None

    @asynccontextmanager
    async def page(self, platform: Platform) -> AsyncIterator[Page]:
        """プラットフォームのロックを取って新しいタブを開き、終わったら閉じる"""
        async with self.locks[platform]:
            try:
                ctx = await self.context(platform)
                page = await ctx.new_page()
            except Exception as e:  # noqa: BLE001
                if not CLOSED_PATTERNS.search(str(e)):
                    raise
                log.warning("%s: ブラウザが閉じていたため起動し直します（%s）", platform.value, str(e)[:120])
                await self._reset(platform)
                ctx = await self.context(platform)
                page = await ctx.new_page()
            try:
                yield page
            finally:
                try:
                    await page.close()
                except Exception:  # noqa: BLE001
                    pass

    # ---------- 画面遷移とログイン ----------
    def is_login_page(self, platform: Platform, page: Page) -> bool:
        return bool(LOGIN_URL_PATTERN[platform].search(page.url))

    async def goto(self, platform: Platform, page: Page, url: str, wait_selector: str | None = None) -> None:
        await page.goto(url, wait_until="domcontentloaded")
        await self._settle(page, wait_selector)
        if await self._needs_login(platform, page):
            log.info("%s: ログイン画面を検出。自動ログインします", platform.value)
            await self.auto_login(platform, page)
            await page.goto(url, wait_until="domcontentloaded")
            await self._settle(page, wait_selector)
            if await self._needs_login(platform, page):
                raise LoginRequired(f"{platform.label}: 再ログイン後もログイン画面のままです")

    async def _settle(self, page: Page, wait_selector: str | None) -> None:
        try:
            # IB は通信が止まらない（解析タグ等）ので、待ちすぎないよう短めにする
            await page.wait_for_load_state("networkidle", timeout=5_000)
        except Exception:  # noqa: BLE001
            pass
        if wait_selector:
            try:
                await page.wait_for_selector(wait_selector, timeout=15_000)
            except Exception:  # noqa: BLE001
                pass

    async def _needs_login(self, platform: Platform, page: Page) -> bool:
        if self.is_login_page(platform, page):
            return True
        # URL が変わらずにログインフォームが出るケース（表示されているものだけ数える）
        return await page.locator("input[type=password]:visible").count() > 0

    async def auto_login(self, platform: Platform, page: Page) -> None:
        import time

        failed = self._login_failed_at.get(platform)
        if failed and time.monotonic() - failed < LOGIN_RETRY_AFTER_SEC:
            raise LoginRequired(f"{platform.label}: 自動ログインに失敗した直後のため、再試行を控えています")
        try:
            await self._auto_login(platform, page)
        except LoginRequired:
            self._login_failed_at[platform] = time.monotonic()
            raise
        self._login_failed_at.pop(platform, None)

    async def _auto_login(self, platform: Platform, page: Page) -> None:
        email = secrets.get_optional(f"{platform.value.upper()}_EMAIL")
        password = secrets.get_optional(f"{platform.value.upper()}_PASSWORD")
        if not email or not password:
            raise LoginRequired(f"{platform.label}: ログイン情報が未登録です（set-secret）")
        if platform is Platform.SM:
            # SM のログインは React のフォーム。読み込みが終わる前に押すと、フォームの既定の動作（GET）で
            # 入力値が URL に載って送られてしまうので、画面の準備ができるまで待つ
            for _ in range(30):
                ready = await page.evaluate(
                    "() => { const f = [...document.querySelectorAll('form')].find(x => x.offsetParent && "
                    "x.querySelector('input[type=password]')); return !!f && Object.keys(f).some(k => k.startsWith('__react')); }")
                if ready:
                    break
                await page.wait_for_timeout(500)
            else:
                raise LoginRequired(f"{platform.label}: ログイン画面の読み込みが終わらないため、自動ログインを中止しました")
        token_status: dict[str, int] = {}

        def on_response(resp) -> None:
            if "/users/token" in resp.url or "/sign_in" in resp.url:
                token_status["status"] = resp.status
        page.on("response", on_response)
        body = await page.locator("body").inner_text()
        if BLOCKER_PATTERNS.search(body) or await page.locator("iframe[src*='captcha']").count():
            raise LoginRequired(f"{platform.label}: 2段階認証または CAPTCHA の画面です")
        if platform is Platform.IB:
            email_box, pw_box, remember = "#owner_email", "#owner_password", "#remmenber_me"
        else:
            email_box, pw_box, remember = "input[name=email]", "input[name=password]", "input[name=rememberMe]"
        # PC 用と SP 用で同じフォームが2つあるので、表示されている方を使う
        await page.locator(f"{email_box}:visible").first.fill(email)
        await page.locator(f"{pw_box}:visible").first.fill(password)
        box = page.locator(f"{remember}:visible").first
        if await box.count() and not await box.is_checked():
            await box.check()
        await page.locator("input[type=submit][name=commit]:visible").first.click()
        # 画面遷移の有無はサイトによって違う（SM は画面内で処理することがある）ので、
        # ログイン画面から抜けるか、エラー・2段階認証が出るまで最大30秒待つ
        for _ in range(60):
            await page.wait_for_timeout(500)
            try:
                if not await self._needs_login(platform, page):
                    page.remove_listener("response", on_response)
                    await self._settle(page, None)
                    return
                body = await page.locator("body").inner_text()
            except Exception:  # noqa: BLE001  （遷移中）
                continue
            if BLOCKER_PATTERNS.search(body):
                raise LoginRequired(f"{platform.label}: ログイン後に2段階認証・CAPTCHA が出ました")
        err = ""
        try:
            err = (await page.locator("[class*=error i]:visible, [class*=alert i]:visible, [role=alert]:visible")
                   .first.inner_text(timeout=1000)).strip()[:120]
        except Exception:  # noqa: BLE001
            pass
        # 原因調査用（パスワード欄は伏せ字のまま写る。URL は ? 以降を消して記録する）
        page.remove_listener("response", on_response)
        shot = await self.screenshot(page, platform, "login_failed")
        log.warning("%s: 自動ログイン失敗 url=%s err=%s login_api=%s shot=%s", platform.value, _safe_url(page.url), err,
                    token_status.get("status"), shot)
        raise LoginRequired(f"{platform.label}: 自動ログインに失敗しました"
                            + (f"（画面の表示: {err}）" if err else "（ID・パスワード、または画面の変更）"))

    # ---------- スクリーンショット ----------
    async def screenshot(self, page: Page, platform: Platform, label: str) -> str | None:
        try:
            d = self.cfg.screenshot_dir / now().strftime("%Y%m%d")
            d.mkdir(parents=True, exist_ok=True)
            safe = re.sub(r"[^\w.-]", "_", label)
            path = d / f"{now():%H%M%S}_{platform.value}_{safe}.png"
            await page.screenshot(path=str(path), full_page=True)
            return str(path)
        except Exception as e:  # noqa: BLE001
            log.warning("screenshot failed: %s", e)
            return None


def purge_old_screenshots(cfg: Config) -> int:
    """保存期間を過ぎたスクリーンショットのフォルダを消す（日付フォルダ単位）"""
    import shutil
    from datetime import timedelta

    root: Path = cfg.screenshot_dir
    if not root.exists():
        return 0
    limit = (now() - timedelta(days=cfg.screenshot_retention_days)).strftime("%Y%m%d")
    n = 0
    for d in root.iterdir():
        if d.is_dir() and d.name.isdigit() and d.name < limit:
            shutil.rmtree(d, ignore_errors=True)
            n += 1
    return n
