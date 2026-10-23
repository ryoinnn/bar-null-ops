"""インスタベースの管理画面操作（決定的なスクリプト。LLM は使わない）。

メッセージのスレッドは予約ごとではなく「スペース×ゲスト」ごと。予約詳細の「メッセージを確認する」から開く。
"""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path

from playwright.async_api import Page

from ..config import Config
from ..models import Detail, ListingRow, Message, Platform
from . import parse_ib
from . import selectors as S
from .base import (
    IB_PAYMENT_WAIT,
    IB_SEND_OK,
    ActionFailed,
    ActionResult,
    NotReady,
    SendGuard,
    is_template_text,
    review_submitted,
    same_text,
)
from .browser import BrowserPool

log = logging.getLogger(__name__)
P = Platform.IB
THREAD_URL_RE = re.compile(r"/partners/inbox/(\d+)")
# 「承認」「承認する」「予約を承認する」だけに一致させる（「承認しない」「非承認」は押さない）
APPROVE_TEXT = re.compile(r"^\s*(予約(リクエスト)?を)?承認(する)?\s*$")
IB_PENDINGS_URL = "https://www.instabase.jp/partners/bookings/pendings"


class Instabase:
    platform = P

    def __init__(self, cfg: Config, pool: BrowserPool):
        self.cfg = cfg
        self.pool = pool

    def booking_url(self, pid: str) -> str:
        return S.IB_BOOKING_URL.format(pid=pid)

    # ---------- 読み取り ----------
    async def list_bookings(self, sort_by_booked: bool = True) -> list[ListingRow]:
        """予約一覧。既定は「予約日時の新しい順」（新しい予約が先頭に来る）"""
        url = S.IB_BOOKINGS_URL + ("?order_column=created_at&order_direction=desc" if sort_by_booked else "")
        async with self.pool.page(P) as page:
            await self.pool.goto(P, page, url, wait_selector="table.partner-bookings-table")
            rows = await page.evaluate(S.IB_BOOKINGS_JS)
        return parse_ib.parse_bookings(rows)

    async def _read_booking(self, page: Page, pid: str) -> tuple[Detail, dict]:
        await self.pool.goto(P, page, self.booking_url(pid))
        raw = await page.evaluate(S.IB_DETAIL_JS)
        raw["guest_id"] = None
        users = await page.locator('a[href^="/partners/users/"]').evaluate_all(
            "els => els.map(e => (e.getAttribute('href').match(/users\\/(\\d+)/) || [])[1]).filter(Boolean)"
        )
        if users:
            raw["guest_id"] = users[0]
        return parse_ib.parse_detail(raw, pid), raw

    async def _open_thread_from_booking(self, page: Page, create: bool) -> str | None:
        """予約詳細のメッセージボタンからスレッドを開く。create=False なら作成はしない"""
        btn = page.get_by_role("button", name="メッセージを確認する")
        if await btn.count() == 0:
            if not create:
                return None
            btn = page.locator("button").filter(has_text=re.compile("メッセージ"))
            if await btn.count() == 0:
                raise ActionFailed("スレッド作成", "予約詳細にメッセージのボタンがありません")
        await btn.first.click()
        await page.wait_for_url(THREAD_URL_RE, timeout=20_000)
        await page.wait_for_load_state("domcontentloaded")
        m = THREAD_URL_RE.search(page.url)
        return m.group(1) if m else None

    async def _read_thread(self, page: Page) -> tuple[list[Message], str | None, list[str]]:
        await page.wait_for_timeout(1000)
        raw = await page.evaluate(S.IB_THREAD_JS)
        return parse_ib.parse_thread(raw)

    async def get_detail(self, pid: str, with_thread: bool = True) -> Detail:
        async with self.pool.page(P) as page:
            d, _ = await self._read_booking(page, pid)
            if with_thread:
                tid = await self._open_thread_from_booking(page, create=False)
                if tid:
                    d.thread_id = tid
                    msgs, gid, _ = await self._read_thread(page)
                    d.messages = msgs
                    d.guest_id = d.guest_id or gid
            return d

    async def get_thread(self, tid: str) -> tuple[list[Message], str | None, list[str]]:
        async with self.pool.page(P) as page:
            await self.pool.goto(P, page, S.IB_THREAD_URL.format(tid=tid))
            return await self._read_thread(page)

    async def unreplied_threads(self) -> tuple[list[str], int]:
        """（未返信のスレッドID, ヘッダの未読件数）。一覧の「未返信のメッセージ」で絞り込む"""
        async with self.pool.page(P) as page:
            await self.pool.goto(P, page, S.IB_INBOX_UNREPLIED_URL)
            unread = await page.evaluate(S.IB_UNREAD_JS)
            rows = await page.evaluate(S.IB_INBOX_JS)
        return [r["thread_id"] for r in rows], unread

    async def review_pending_ids(self) -> set[str]:
        async with self.pool.page(P) as page:
            await self.pool.goto(P, page, S.IB_REVIEW_CREATABLES_URL)
            ids = await page.evaluate(
                "() => [...document.querySelectorAll('a[href*=\"/partners/bookings/\"]')]"
                ".map(a => (a.getAttribute('href').match(/bookings\\/(\\d{6,})/) || [])[1]).filter(Boolean)"
            )
        return set(ids)

    async def review_posted_ids(self) -> set[str]:
        async with self.pool.page(P) as page:
            await self.pool.goto(P, page, S.IB_REVIEW_POSTED_URL)
            text = await page.locator("body").inner_text()
        return set(re.findall(r"\b(\d{10})\b", text))

    @staticmethod
    def template_sent(messages: list[Message], marker: str, since_index: int = 0, code: str | None = None) -> bool:
        host = [m for m in messages if m.role == "host" and m.index >= since_index]
        return any(is_template_text(m.text, marker, code) for m in host) and any(
            any(f.lower().endswith(".pdf") for f in m.files) for m in host)

    # ---------- 書き込み ----------
    async def _approve_request(self, page: Page, pid: str, dry_run: bool, res: ActionResult) -> None:
        """承認制リクエストを承認する（予約詳細 → 無ければ承認待ち一覧の該当行のボタン）"""
        async def find_button():
            btn = page.locator("a, button").filter(has_text=APPROVE_TEXT)
            if await btn.count():
                return btn.first
            sub = page.locator('input[type=submit][value="承認する"], input[type=submit][value="承認"]')
            return sub.first if await sub.count() else None

        btn = await find_button()
        if btn is None:
            await self.pool.goto(P, page, IB_PENDINGS_URL)
            # 予約IDを含み、承認ボタンをちょうど1つ持つ要素のうち一番内側のもの（別の予約のボタンを押さないように）
            cands = page.locator("tr, li, div, section, article").filter(has_text=pid)
            n = await cands.count()
            for i in range(n - 1, max(-1, n - 40), -1):
                b = cands.nth(i).locator("a, button, input[type=submit]").filter(has_text=APPROVE_TEXT)
                k = await b.count()
                if k == 1:
                    btn = b.first
                    break
                if k > 1:
                    break  # 複数の予約を含む要素まで来た（それより外側も同じ）
        if btn is None:
            raise ActionFailed("承認", "承認ボタンが見つかりません（画面の変更の可能性）",
                               [await self.pool.screenshot(page, P, f"{pid}_approve_missing") or ""])
        await btn.scroll_into_view_if_needed()
        if dry_run:
            res.screenshots.append(await self.pool.screenshot(page, P, f"{pid}_dryrun_approve") or "")
            res.note += "(DRY_RUN) 承認ボタンの直前で停止。"
            return
        # Rails の確認ダイアログ（data-confirm）が出たら OK を押す
        page.once("dialog", lambda dlg: asyncio.ensure_future(dlg.accept()))
        await btn.click()
        await page.wait_for_load_state("domcontentloaded")
        await page.wait_for_timeout(1500)
        # 画面内の確認モーダルが出る場合
        modal = page.locator("[role=dialog]:visible, .modal:visible").filter(has_text="承認")
        if await modal.count():
            res.screenshots.append(await self.pool.screenshot(page, P, f"{pid}_approve_modal") or "")
            ok = modal.first.locator("a, button, input[type=submit]").filter(has_text=APPROVE_TEXT)
            if await ok.count():
                page.once("dialog", lambda dlg: asyncio.ensure_future(dlg.accept()))
                await ok.first.click()
                await page.wait_for_load_state("domcontentloaded")
        d, _ = await self._read_booking(page, pid)
        res.screenshots.append(await self.pool.screenshot(page, P, f"{pid}_approved") or "")
        if "リクエスト" in d.status:
            raise ActionFailed("承認", f"承認後も予約状況が「{d.status}」のままです", res.screenshots)
        res.note += "承認しました。"

    async def send_template(self, pid: str, text: str, pdf: Path, marker: str, dry_run: bool,
                            code: str | None = None) -> tuple[ActionResult, str | None]:
        """（承認制リクエストなら承認してから）予約の確定を確かめ、予約詳細からスレッドを開き（無ければ作り）、
        定型文と PDF を1回で送る"""
        res = ActionResult(ok=False, dry_run=dry_run)
        async with self.pool.page(P) as page:
            d, _ = await self._read_booking(page, pid)
            res.screenshots.append(await self.pool.screenshot(page, P, f"{pid}_before") or "")
            if d.closed_reason:
                raise ActionFailed("状態確認", f"予約が「{d.status}」のため送信しません", res.screenshots)
            if "リクエスト" in d.status:
                await self._approve_request(page, pid, dry_run, res)
                if dry_run:
                    res.ok = True
                    return res, None
                d, _ = await self._read_booking(page, pid)
            # 予約が確定しているか（支払い前・手続き未完了の相手に暗証番号を送らない）
            status = (d.status or "").strip()
            if status not in IB_SEND_OK:
                if status in IB_PAYMENT_WAIT:
                    raise NotReady("支払い待ち", ("承認しました。" if "承認しました" in res.note else "") + f"予約の状態が「{status}」のため、定型文（暗証番号）はまだ送りません",
                                   res.screenshots)
                raise ActionFailed("状態確認", f"予約の状態が「{status or '不明'}」のため、定型文（暗証番号）を送りません",
                                   res.screenshots)
            tid = await self._open_thread_from_booking(page, create=False)
            existing = tid is not None
            if tid is None:
                if dry_run:
                    res.screenshots.append(await self.pool.screenshot(page, P, f"{pid}_dryrun_create_thread") or "")
                    res.ok, res.note = True, "(DRY_RUN) スレッド作成の直前で停止"
                    return res, None
                tid = await self._open_thread_from_booking(page, create=True)
                if not tid:
                    raise ActionFailed("スレッド作成", "メッセージのスレッドを開けません",
                                       [await self.pool.screenshot(page, P, f"{pid}_thread_missing") or ""])
            msgs, _, _ = await self._read_thread(page)
            if existing and not msgs:
                raise ActionFailed("画面の読み込み", "メッセージのスレッドを読み取れないため、送信を中止しました（読み込みの遅れか画面の変更）",
                                   [await self.pool.screenshot(page, P, f"{pid}_thread_unreadable") or ""])
            since = self._index_after_booking(msgs, pid, d.applied_at)
            if self.template_sent(msgs, marker, since, code):
                res.ok, res.skipped, res.note = True, True, "定型文とマニュアルは送信済みでした（手動対応）"
                return res, tid
            await self._fill_and_submit(page, pid, text, pdf, dry_run, res)
            if not dry_run:
                await self.pool.goto(P, page, S.IB_THREAD_URL.format(tid=tid))
                msgs, _, _ = await self._read_thread(page)
                res.screenshots.append(await self.pool.screenshot(page, P, f"{pid}_after") or "")
                if not self.template_sent(msgs, marker, since, code):
                    raise ActionFailed("送信後の確認", "画面上で定型文または PDF を確認できません", res.screenshots)
            res.ok = True
            return res, tid

    @staticmethod
    def _index_after_booking(msgs: list[Message], pid: str, applied_at=None) -> int:
        """この予約の発言の起点（リピーターの過去の定型文と区別する）。
        1) この予約IDが出てくる最後の自動メッセージ 2) 申込日時以降の最初の発言
        3) 予約変更の承認の自動メッセージ（変更で作られた予約は、自動メッセージに自分の ID が出てこない） 4) 先頭"""
        own = [m.index for m in msgs if m.role == "system" and pid in m.text]
        if own:
            return own[-1]
        if applied_at is not None:
            later = [m.index for m in msgs if m.at and m.at >= applied_at.replace(second=0, microsecond=0)]
            if later:
                return later[0]
        changed = [m.index for m in msgs if m.role == "system" and parse_ib.is_change_approved(m.text)]
        if changed:
            return changed[-1]
        return 0

    async def _fill_and_submit(self, page: Page, pid: str, text: str, pdf: Path | None, dry_run: bool,
                               res: ActionResult) -> None:
        await page.locator(S.IB_MSG_TEXTAREA).fill(text)
        if pdf is not None:
            await page.locator(S.IB_MSG_FILE).set_input_files(str(pdf))
        res.screenshots.append(await self.pool.screenshot(page, P, f"{pid}_filled") or "")
        if dry_run:
            first = ((await page.locator(S.IB_MSG_TEXTAREA).input_value()).splitlines() or [""])[0]
            res.note += f"（入力した文面の1行目:「{first}」）"
            await page.locator(S.IB_MSG_TEXTAREA).fill("")
            if pdf is not None:
                await page.locator(S.IB_MSG_FILE).set_input_files([])
            res.note += "(DRY_RUN) 送信ボタンの直前で停止。"
            return
        # 送信ボタンには「メッセージを送信します。よろしいですか？」の確認（data-confirm）があるので OK を押す
        page.once("dialog", lambda dlg: asyncio.ensure_future(dlg.accept()))
        async with page.expect_navigation(wait_until="domcontentloaded", timeout=90_000):
            await page.locator(S.IB_MSG_SUBMIT).click()
        res.note += "送信しました。"

    async def send_message(self, tid: str, text: str, dry_run: bool, already_sent_check: str | None = None,
                           guard: SendGuard | None = None) -> ActionResult:
        """guard: 送信直前のスレッドを見て、送るべきでなければ SendSkipped を投げる（手動返信済み・送信済み・新着あり）"""
        res = ActionResult(ok=False, dry_run=dry_run)
        async with self.pool.page(P) as page:
            await self.pool.goto(P, page, S.IB_THREAD_URL.format(tid=tid))
            msgs, _, _ = await self._read_thread(page)
            if not msgs:
                raise ActionFailed("画面の読み込み", "スレッドのメッセージを読み取れないため、送信を中止しました",
                                   [await self.pool.screenshot(page, P, f"t{tid}_thread_unreadable") or ""])
            if guard is not None:
                guard(msgs)
            elif already_sent_check and any(m.role == "host" and same_text(m.text, already_sent_check) for m in msgs):
                res.ok, res.skipped, res.note = True, True, "同じ文面が既に送信されていました"
                return res
            before = sum(1 for m in msgs if m.role == "host" and same_text(m.text, text))
            await self._fill_and_submit(page, f"t{tid}", text, None, dry_run, res)
            if not dry_run:
                # 届いたかを読み直して確かめる（見つからなければ失敗にする。再実行時は送信済みの確認で二重送信を防ぐ）
                for attempt in range(2):
                    if attempt:
                        await page.wait_for_timeout(3000)
                        await self.pool.goto(P, page, S.IB_THREAD_URL.format(tid=tid))
                    after, _, _ = await self._read_thread(page)
                    if sum(1 for m in after if m.role == "host" and same_text(m.text, text)) > before:
                        break
                else:
                    shot = await self.pool.screenshot(page, P, f"t{tid}_send_failed")
                    raise ActionFailed("送信後の確認", "送信後、スレッドに送った文面が見つかりません",
                                       [*res.screenshots, shot or ""])
            res.ok = True
            return res

    async def post_review(self, pid: str, stars: int, comment: str, dry_run: bool) -> ActionResult:
        res = ActionResult(ok=False, dry_run=dry_run)
        if pid in await self.review_posted_ids():
            res.ok, res.skipped, res.note = True, True, "レビューは投稿済みでした"
            return res
        if pid not in await self.review_pending_ids():
            # 「レビュー待ち」の一覧に載るまでは、レビューの入力画面が使えない（利用直後など）
            raise NotReady("レビュー", "まだレビューを書ける状態になっていません（インスタベースの「レビュー待ち」に載っていません）")
        async with self.pool.page(P) as page:
            await self.pool.goto(P, page, S.IB_REVIEW_URL.format(pid=pid))
            point = page.locator(f"{S.IB_REVIEW_POINT}:visible").first
            if await point.count() == 0:
                raise ActionFailed("レビュー", "レビューの入力欄が見つかりません",
                                   [await self.pool.screenshot(page, P, f"{pid}_review_missing") or ""])
            await point.select_option(str(stars))
            await page.locator(f"{S.IB_REVIEW_COMMENT}:visible").first.fill(comment)
            res.screenshots.append(await self.pool.screenshot(page, P, f"{pid}_review_filled") or "")
            if dry_run:
                res.ok, res.note = True, "(DRY_RUN) レビュー投稿の直前で停止"
                return res
            page.once("dialog", lambda dlg: asyncio.ensure_future(dlg.accept()))
            async with page.expect_navigation(wait_until="domcontentloaded", timeout=90_000):
                review_submitted(True)
                await page.locator(f"{S.IB_REVIEW_SUBMIT}:visible").first.click()
            res.screenshots.append(await self.pool.screenshot(page, P, f"{pid}_review_after") or "")
            # 入力の不備などで受け付けられなかった場合は、画面のエラー表示を報告に含める
            page_error = ""
            try:
                page_error = (await page.locator(".alert:visible, .error:visible, .invalid-feedback:visible, "
                                                 "[class*=error i]:visible, [role=alert]:visible")
                              .first.inner_text(timeout=1500)).strip()[:150]
            except Exception:  # noqa: BLE001
                pass
        if pid not in await self.review_posted_ids():
            raise ActionFailed("レビュー", "投稿済みのレビュー一覧に表示されません"
                               + (f"（画面の表示: {page_error}）" if page_error else ""), res.screenshots)
        res.ok, res.note = True, f"★{stars}でレビューを投稿しました"
        return res
