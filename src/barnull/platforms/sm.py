"""スペースマーケットの管理画面操作（決定的なスクリプト。LLM は使わない）。"""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path

from playwright.async_api import Page

from ..config import Config
from ..models import Detail, Kind, ListingRow, Platform
from ..textutil import now
from . import parse_sm
from . import selectors as S
from .base import (
    SM_PAYMENT_WAIT,
    SM_PENDING,
    SM_SEND_OK,
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
P = Platform.SM


def _button(scope, label: str):
    """表示文字がちょうど label のボタン（<button> と <a type=anchor> の両方）"""
    return scope.locator(S.SM_BUTTON).filter(has_text=re.compile(rf"^\s*{label}\s*$"))


def _has_pdf(d: Detail) -> bool:
    return any(m.role == "host" and any(f.lower().endswith(".pdf") for f in m.files) for m in d.messages)


class SpaceMarket:
    platform = P

    def __init__(self, cfg: Config, pool: BrowserPool):
        self.cfg = cfg
        self.pool = pool

    def thread_url(self, pid: str) -> str:
        return S.SM_THREAD_URL.format(host=self.cfg.sm_host_id, pid=pid)

    # ---------- 読み取り ----------
    async def list_inbox(self) -> list[ListingRow]:
        async with self.pool.page(P) as page:
            await self.pool.goto(P, page, S.SM_INBOX_URL.format(host=self.cfg.sm_host_id), wait_selector=S.SM_ROW)
            rows = await page.evaluate(S.SM_INBOX_JS)
        return parse_sm.parse_inbox(rows)

    async def _open_thread(self, page: Page, pid: str, require_messages: bool = False) -> Detail:
        """スレッドを開いて読む。require_messages=True（書き込みの前）は、メッセージ一覧を読み切れるまで
        読み直し、読めなければ失敗にする（未描画を「未送信」と取り違えて二重に送らないように）"""
        for attempt in range(3):
            api_messages: list[dict] = []
            reputation: dict[str, int] = {}
            reservation_api: dict = {}

            async def on_response(resp, api_messages=api_messages, reputation=reputation,
                                  reservation_api=reservation_api) -> None:
                url = resp.url.split("?")[0]
                if url.endswith(f"/reservations/{pid}/messages"):
                    try:
                        body = await resp.json()
                    except Exception:  # noqa: BLE001
                        return
                    if isinstance(body, list):
                        api_messages.extend(body)
                elif url.endswith(f"/reservations/{pid}/reputation") and resp.request.method == "GET":
                    reputation["status"] = resp.status
                elif url.endswith(f"/reservations/{pid}") and "/owners/" in url and resp.status == 200:
                    # 予約情報（ホストのレビューを投稿済みか・書ける状態か）
                    try:
                        body = await resp.json()
                    except Exception:  # noqa: BLE001
                        return
                    if isinstance(body, dict):
                        reservation_api.update(body)

            page.on("response", on_response)
            try:
                await self.pool.goto(P, page, self.thread_url(pid), wait_selector=S.SM_SIDEBAR)
                await page.wait_for_timeout(1500 + 1500 * attempt)
                raw = await page.evaluate(S.SM_THREAD_JS)
            finally:
                page.remove_listener("response", on_response)
            d = parse_sm.parse_thread(raw, pid)
            parse_sm.attach_api(d, api_messages, reputation.get("status"), reservation_api or None)
            if not require_messages:
                return d
            api_n = len({m.get("id") for m in api_messages if isinstance(m, dict) and m.get("timeline_type") == 0})
            dom_n = sum(1 for m in d.messages if m.role in ("host", "guest"))
            if d.messages and dom_n >= api_n:
                return d
            log.info("SM %s: メッセージ一覧の読み込みを待ちます（画面 %d 件 / API %d 件）", pid, dom_n, api_n)
        shot = await self.pool.screenshot(page, P, f"{pid}_thread_unreadable")
        raise ActionFailed("画面の読み込み", "スレッドのメッセージを読み切れないため、送信を中止しました（読み込みの遅れか画面の変更）",
                           [shot or ""])

    async def get_detail(self, pid: str) -> Detail:
        async with self.pool.page(P) as page:
            return await self._open_thread(page, pid)

    def template_sent(self, d: Detail, marker: str, code: str | None = None) -> bool:
        """ホストの発言に定型文（マーカーの語と暗証番号）があり、PDF も送っている"""
        has_text = any(m.role == "host" and is_template_text(m.text, marker, code) for m in d.messages)
        return has_text and _has_pdf(d)

    # ---------- 書き込み ----------
    async def approve_and_send_template(self, pid: str, text: str, pdf: Path, marker: str, dry_run: bool,
                                        code: str | None = None) -> ActionResult:
        """承認（必要なら）→ 予約の確定を確認 → 定型文 → PDF。各段階の直前に画面を読み、実施済みなら飛ばす"""
        res = ActionResult(ok=False, dry_run=dry_run)
        async with self.pool.page(P) as page:
            d = await self._open_thread(page, pid, require_messages=True)
            res.screenshots.append(await self.pool.screenshot(page, P, f"{pid}_before") or "")
            if d.closed_reason:
                raise ActionFailed("状態確認", f"予約が「{d.status}」のため送信しません", res.screenshots)
            if d.kind is Kind.INQUIRY_ONLY:
                raise ActionFailed("状態確認", f"予約ではなく問い合わせ（{d.status or '状態不明'}）のため、"
                                   "定型文（暗証番号）は送りません", res.screenshots)

            # 1) 承認
            approved_in_dry_run = False
            if d.can_approve:
                if dry_run:
                    await _button(page.locator(S.SM_SIDEBAR), "承認").first.scroll_into_view_if_needed()
                    res.screenshots.append(await self.pool.screenshot(page, P, f"{pid}_dryrun_approve") or "")
                    res.note += "(DRY_RUN) 承認ボタンの直前で停止。"
                    approved_in_dry_run = True
                else:
                    await self._approve(page, pid, res)
                    d = await self._wait_after_approve(page, pid, res)
            elif (d.status or "").strip() in SM_PENDING:
                raise ActionFailed("承認", "予約リクエストですが、承認ボタンが見つかりません（画面の変更の可能性）。"
                                   "管理画面で承認してから、もう一度スタンプしてください", res.screenshots)

            # 2) 予約が確定しているか（確定していない相手に暗証番号を送らない）
            status = (d.status or "").strip()
            if not approved_in_dry_run and status not in SM_SEND_OK:
                if status in SM_PAYMENT_WAIT:
                    raise NotReady("支払い待ち", ("承認しました。" if "承認しました" in res.note else "") + f"予約の状態が「{status}」（ゲストの支払い待ち）のため、"
                                   "定型文（暗証番号）はまだ送りません", res.screenshots)
                raise ActionFailed("状態確認", f"予約の状態が「{status or '不明'}」のため、定型文（暗証番号）を送りません",
                                   res.screenshots)

            # 3) 定型文（テキスト）と PDF。送信済みなら飛ばす
            has_text = any(m.role == "host" and is_template_text(m.text, marker, code) for m in d.messages)
            has_pdf = _has_pdf(d)
            if has_text and has_pdf:
                res.ok, res.skipped = True, True
                res.note += "定型文とマニュアルは送信済みでした（手動対応）"
                return res
            if not has_text:
                await self._send_text(page, pid, text, dry_run, res, d)
            if not has_pdf:
                await self._send_file(page, pid, pdf, dry_run, res, d)

            if not dry_run:
                d = await self._open_thread(page, pid, require_messages=True)
                res.screenshots.append(await self.pool.screenshot(page, P, f"{pid}_after") or "")
                if not self.template_sent(d, marker, code):
                    raise ActionFailed("送信後の確認", "画面上で定型文または PDF を確認できません", res.screenshots)
            res.ok = True
            return res

    async def _approve(self, page: Page, pid: str, res: ActionResult) -> None:
        await _button(page.locator(S.SM_SIDEBAR), "承認").first.click()
        modal = page.locator(S.SM_MODAL).filter(has_text="承認")
        await modal.first.wait_for(state="visible", timeout=10_000)
        res.screenshots.append(await self.pool.screenshot(page, P, f"{pid}_approve_modal") or "")
        await _button(modal.first, "承認").first.click()
        res.note += "承認しました。"

    async def _wait_after_approve(self, page: Page, pid: str, res: ActionResult, timeout_s: int = 60) -> Detail:
        """承認の後、承認が反映されたこと（承認ボタンが消え、保留中でなくなった）を確かめる。
        クレジットカードなら同時に予約完了になるので、予約完了になるまで少し待つ"""
        d: Detail | None = None
        for i in range(max(1, timeout_s // 5)):
            await page.wait_for_timeout(3000 if i == 0 else 5000)
            try:
                d = await self._open_thread(page, pid, require_messages=True)
            except ActionFailed:
                continue
            if d.can_approve or (d.status or "").strip() in SM_PENDING:
                continue
            if (d.status or "").strip() in SM_SEND_OK or d.closed_reason:
                break
        if d is None or d.can_approve or (d.status or "").strip() in SM_PENDING:
            shot = await self.pool.screenshot(page, P, f"{pid}_approve_failed")
            raise ActionFailed("承認", f"承認ボタンを押しましたが、予約の状態が「{(d.status if d else '') or '保留中'}」のままです",
                               [*res.screenshots, shot or ""])
        res.screenshots.append(await self.pool.screenshot(page, P, f"{pid}_approved") or "")
        return d

    async def _send_text(self, page: Page, pid: str, text: str, dry_run: bool, res: ActionResult,
                         before_detail: Detail | None = None) -> None:
        # 送信前から同じ文面の発言がある場合に、それを「送れた」と取り違えないよう、送信前の件数を控える
        before = sum(1 for m in (before_detail.messages if before_detail else []) if m.role == "host" and same_text(m.text, text))
        box = page.locator(S.SM_TEXTAREA)
        await box.fill(text)
        if dry_run:
            res.screenshots.append(await self.pool.screenshot(page, P, f"{pid}_dryrun_text") or "")
            first = ((await box.input_value()).splitlines() or [""])[0]
            await box.fill("")
            res.note += f"(DRY_RUN) テキスト送信の直前で停止（入力した文面の1行目:「{first}」）。"
            return
        # 確認ダイアログが出た場合は OK（既定ではキャンセルされてしまう）
        page.once("dialog", lambda dlg: asyncio.ensure_future(dlg.accept()))
        await page.locator(S.SM_SEND).filter(has_text="送信").first.click()
        # 送れたかどうかは、スレッドを読み直して判定する（画面の発言数の増減は当てにならない）
        if not await self._wait_host(page, pid, lambda ms: sum(1 for m in ms if m.role == "host"
                                                               and same_text(m.text, text)) > before):
            shot = await self.pool.screenshot(page, P, f"{pid}_send_failed")
            raise ActionFailed("メッセージ送信", "送信後、スレッドに送った文面が見つかりません",
                               [*res.screenshots, shot or ""])
        res.note += "メッセージを送信しました。"

    async def _wait_host(self, page: Page, pid: str, pred, timeout_s: int = 40) -> bool:
        """スレッドを読み直しながら、発言の一覧が条件を満たす（送った発言が増えた）のを待つ"""
        for i in range(timeout_s // 5):
            await page.wait_for_timeout(3000 if i == 0 else 5000)
            try:
                d = await self._open_thread(page, pid)
            except Exception as e:  # noqa: BLE001
                log.debug("読み直し失敗: %s", e)
                continue
            if pred(d.messages):
                return True
        return False

    async def _send_file(self, page: Page, pid: str, pdf: Path, dry_run: bool, res: ActionResult,
                         before_detail: Detail | None = None) -> None:
        def pdfs(ms) -> int:
            return sum(1 for m in ms if m.role == "host" and any(f.lower().endswith(".pdf") for f in m.files))
        before = pdfs(before_detail.messages) if before_detail else 0
        if dry_run:
            res.screenshots.append(await self.pool.screenshot(page, P, f"{pid}_dryrun_file") or "")
            res.note += "(DRY_RUN) PDF 添付の直前で停止。"
            return
        page.once("dialog", lambda dlg: asyncio.ensure_future(dlg.accept()))
        await page.locator(S.SM_FILE_INPUT).set_input_files(str(pdf))
        # ファイルを選ぶと「ファイルの送信／送信するファイルに間違いはありませんか？」のモーダルが出る
        modal = page.locator(S.SM_MODAL).filter(has_text="ファイルの送信")
        try:
            await modal.first.wait_for(state="visible", timeout=15_000)
        except Exception as e:  # noqa: BLE001
            shot = await self.pool.screenshot(page, P, f"{pid}_file_no_modal")
            raise ActionFailed("PDF 送信", f"ファイル送信の確認画面が出ません: {e}", [*res.screenshots, shot or ""]) from e
        res.screenshots.append(await self.pool.screenshot(page, P, f"{pid}_file_modal") or "")
        await _button(modal.first, "送信").first.click()
        try:
            await modal.first.wait_for(state="hidden", timeout=30_000)
        except Exception:  # noqa: BLE001
            log.warning("ファイル送信のモーダルが閉じません（送信中の可能性）")
        if not await self._wait_host(page, pid, lambda ms: pdfs(ms) > before):
            shot = await self.pool.screenshot(page, P, f"{pid}_file_failed")
            raise ActionFailed("PDF 送信", "送信後、スレッドに PDF が見つかりません", [*res.screenshots, shot or ""])
        res.note += "PDF を送信しました。"

    async def send_message(self, pid: str, text: str, dry_run: bool, already_sent_check: str | None = None,
                           guard: SendGuard | None = None) -> ActionResult:
        """guard: 送信直前のスレッドを見て、送るべきでなければ SendSkipped を投げる（手動返信済み・送信済み・新着あり）"""
        res = ActionResult(ok=False, dry_run=dry_run)
        async with self.pool.page(P) as page:
            d = await self._open_thread(page, pid, require_messages=True)
            if guard is not None:
                guard(d.messages)
            elif already_sent_check and any(m.role == "host" and same_text(m.text, already_sent_check)
                                            for m in d.messages):
                res.ok, res.skipped, res.note = True, True, "同じ文面が既に送信されていました"
                return res
            await self._send_text(page, pid, text, dry_run, res, d)
            res.ok = True
            return res

    async def _open_review_page(self, page: Page, pid: str) -> int | None:
        """レビュー画面を開き、画面が呼ぶ GET …/reputation の応答（200=投稿済み、404=未投稿）を返す"""
        status: dict[str, int] = {}

        def on_response(resp) -> None:
            if resp.request.method == "GET" and resp.url.split("?")[0].endswith(f"/reservations/{pid}/reputation"):
                status["s"] = resp.status
        page.on("response", on_response)
        try:
            await self.pool.goto(P, page, S.SM_REVIEW_URL.format(host=self.cfg.sm_host_id, pid=pid),
                                 wait_selector=S.SM_REVIEW_TEXTAREA)
            await page.wait_for_timeout(1500)
        finally:
            page.remove_listener("response", on_response)
        return status.get("s")

    async def post_review(self, pid: str, stars: int, comment: str, dry_run: bool) -> ActionResult:
        """レビュー画面で星とコメントを入れ、「レビューの投稿」→ 確認画面の「投稿」。
        - 投稿済みか（予約情報の API・レビュー画面の API）を先に確かめ、投稿済みなら何もしない（手動の投稿を上書きしない）
        - まだ書けない（can_be_reviewed が偽）なら NotReady（後で再試行）
        - 投稿の通信が成功したら投稿済みとして扱う（確認の読み直しに失敗しても、再実行で二重に投稿しない）"""
        res = ActionResult(ok=False, dry_run=dry_run)
        async with self.pool.page(P) as page:
            d = await self._open_thread(page, pid)
            if d.review_posted:
                res.ok, res.skipped, res.note = True, True, "レビューは投稿済みでした"
                return res
            if d.review_open is False:
                if d.review_expires_at and d.review_expires_at <= now():
                    raise ActionFailed("レビュー", "レビューの投稿期限が過ぎています")
                raise NotReady("レビュー", "まだレビューを書ける状態になっていません（スペースマーケット）")
            rep = await self._open_review_page(page, pid)
            if rep == 200:
                res.ok, res.skipped, res.note = True, True, "レビューは投稿済みでした"
                return res
            if not (rep == 404 or d.review_posted is False):
                # 未投稿と分かったとき（レビュー画面の API が 404、または予約情報で未投稿）だけ書き込む
                raise ActionFailed("レビュー", f"投稿済みかどうかを確認できないため、投稿しません（レビューの API の応答: {rep}）",
                                   [await self.pool.screenshot(page, P, f"{pid}_review_unknown") or ""])
            ta = page.locator(S.SM_REVIEW_TEXTAREA)
            if await ta.count() == 0:
                raise ActionFailed("レビュー", "レビューの入力欄が見つかりません（期限切れの可能性）",
                                   [await self.pool.screenshot(page, P, f"{pid}_review_missing") or ""])
            if not dry_run:
                await self._select_stars(page, pid, stars, res)
            await ta.first.fill(comment)
            await page.wait_for_timeout(500)
            submit = page.locator(S.SM_REVIEW_SUBMIT).filter(has_text=re.compile(r"^\s*レビューの投稿\s*$")).last
            res.screenshots.append(await self.pool.screenshot(page, P, f"{pid}_review_filled") or "")
            if dry_run:
                await ta.first.fill("")
                res.ok, res.note = True, "(DRY_RUN) レビュー投稿の直前で停止"
                return res
            if await submit.count() == 0:
                raise ActionFailed("レビュー", "レビューの投稿ボタンが見つかりません（画面の変更の可能性）", res.screenshots)
            if await submit.get_attribute("disabled") is not None:
                raise ActionFailed("レビュー", "星とコメントを入れても、投稿ボタンが押せる状態になりません", res.screenshots)
            await submit.click()
            # 「公開後にレビューの編集はできません。レビューを投稿してよろしいですか？」→「投稿」
            modal = page.locator(S.SM_MODAL).filter(has_text=S.SM_REVIEW_CONFIRM_TEXT)
            try:
                await modal.first.wait_for(state="visible", timeout=15_000)
            except Exception as e:  # noqa: BLE001
                shot = await self.pool.screenshot(page, P, f"{pid}_review_no_confirm")
                raise ActionFailed("レビュー", f"投稿の確認画面が出ません: {e}", [*res.screenshots, shot or ""]) from e
            res.screenshots.append(await self.pool.screenshot(page, P, f"{pid}_review_confirm") or "")
            ok_btn = _button(modal.first, "投稿")
            if await ok_btn.count() != 1:
                raise ActionFailed("レビュー", "確認画面の「投稿」ボタンが見つかりません", res.screenshots)
            # 「投稿」で出るレビュー投稿の通信（POST …/reservations/<ID>/reputation）の結果を見る
            try:
                async with page.expect_request(
                        lambda r: r.method == "POST" and r.url.split("?")[0].endswith(f"/reservations/{pid}/reputation"),
                        timeout=20_000) as req_info:
                    review_submitted(True)
                    await ok_btn.first.click()
                req = await req_info.value
            except Exception as e:  # noqa: BLE001
                shot = await self.pool.screenshot(page, P, f"{pid}_review_no_request")
                raise ActionFailed("レビュー", f"確認画面の「投稿」を押しましたが、投稿の通信が出ませんでした: {e}",
                                   [*res.screenshots, shot or ""]) from e
            resp = await req.response()
            if resp is None:
                shot = await self.pool.screenshot(page, P, f"{pid}_review_request_failed")
                raise ActionFailed("レビュー", f"レビュー投稿の通信が失敗しました（{req.failure or '理由不明'}）",
                                   [*res.screenshots, shot or ""])
            if not (200 <= resp.status < 300):
                review_submitted(False)  # サイトが受け付けなかった（投稿されていない）
                shot = await self.pool.screenshot(page, P, f"{pid}_review_rejected")
                raise ActionFailed("レビュー", f"レビューの投稿がサイト側で受け付けられませんでした（応答 {resp.status}）",
                                   [*res.screenshots, shot or ""])
            # ここで投稿は成立している。投稿済みになったことを読み直して確かめる（確かめられなくても失敗にはしない）
            confirmed = False
            for i in range(3):
                await page.wait_for_timeout(2000 if i == 0 else 4000)
                try:
                    d = await self._open_thread(page, pid)
                    if d.review_posted is True or await self._open_review_page(page, pid) == 200:
                        confirmed = True
                        break
                except Exception as e:  # noqa: BLE001
                    log.warning("SM %s: 投稿後の確認の読み直しに失敗: %s", pid, e)
            res.screenshots.append(await self.pool.screenshot(page, P, f"{pid}_review_after") or "")
            res.ok = True
            res.note = f"★{stars}でレビューを投稿しました" + ("" if confirmed else "（投稿の通信は成功。投稿済みの表示はまだ確認できていません）")
            return res

    async def _select_stars(self, page: Page, pid: str, stars: int, res: ActionResult) -> None:
        """星を選び、選ばれた数が stars になったことを確かめる（選んだ星は色が変わる）"""
        star = page.locator(S.SM_REVIEW_STAR)
        if await star.count() != 5:
            raise ActionFailed("レビュー", f"評価の星が見つかりません（{await star.count()} 個）", res.screenshots)
        before = await page.evaluate(S.SM_REVIEW_STARS_JS)
        await star.nth(stars - 1).click()
        await page.wait_for_timeout(300)
        after = await page.evaluate(S.SM_REVIEW_STARS_JS)
        base = before[-1] if before else None  # 選ぶ前（または5番目）の色
        chosen = [c != base for c in after] if base and len(set(before)) == 1 else None
        if chosen is not None and chosen != [True] * stars + [False] * (5 - stars):
            shot = await self.pool.screenshot(page, P, f"{pid}_review_stars")
            raise ActionFailed("レビュー", f"★{stars} を選べませんでした（選ばれた星: {sum(chosen)}）", [*res.screenshots, shot or ""])
