"""SM のレビュー投稿の手順を、実画面の振る舞い（2026-10-12 に確認）を再現した偽のページで確かめる。

実画面の振る舞い:
- スレッド画面は予約情報 GET …/owners/<host>/reservations/<ID> を読む。reputation_id_by_owner はホストのレビューの ID
  （未投稿なら null）、can_be_reviewed はレビューを書ける状態か。GET …/reputation は呼ばない
- サイドバーの「ホストレビューあり」はゲストのバッジ。「レビューの投稿」ボタンは投稿後も（公開まで）残る
- レビュー画面は GET …/reputation を呼ぶ（200=投稿済み、404=未投稿）
- 星は選ぶ前は灰色、N 番目を押すと 1〜N 番目が緑
- 本文側の「レビューの投稿」は <a type="anchor" disabled>。星とコメントが入ると押せる
- 押すと「公開後にレビューの編集はできません。レビューを投稿してよろしいですか？」の確認（キャンセル／投稿）
- 「投稿」で POST …/reservations/<ID>/reputation（{"score": N, "description": "…"}）
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager

import pytest
from playwright.async_api import async_playwright

from barnull.config import load_config
from barnull.platforms.base import REVIEW_SUBMIT_HOOK, ActionFailed, NotReady
from barnull.platforms.sm import SpaceMarket

PID = "6219104"
API = "https://mp-gateway.spacemarket.com/rest/1/owners/h/reservations/__PID__"

THREAD_HTML = """<!doctype html><html><body>
<div class="MainContentsWithSidebarInbox__SidebarWrapper-x"><div class="HeaderStatus__StatusText-x">利用完了</div>
  <div>ゲスト詳細</div><div>山田 太郎</div><div>ホストレビューあり</div>
  <button class="ButtonBase__ButtonComponent-x">レビューの投稿</button></div>
<div class="DetailPage__PageContents-x"><div class="StatusDate__Date-x">今日</div>
<div class="MessageOpponent__Wrapper-x"><div class="MessageOpponent__Message-x">ありがとうございました</div> 0:01</div></div>
<script>fetch("__API__")</script>
</body></html>""".replace("__API__", API).replace("__PID__", PID)

REVIEW_HTML = """<!doctype html><html><body>
<div class="MainContentsWithSidebarInbox__SidebarWrapper-x"><button class="ButtonBase__ButtonComponent-x">レビューの投稿</button></div>
<div class="WriteReviewPage__Section-x">
  <div class="ReputationStar__StarIconWrapper-x">
    __STARS__
  </div>
  <textarea class="ant-input"></textarea>
  <div class="WriteReviewPage__ButtonWrapper-x"><a type="anchor" disabled="" id="submit" class="ButtonBase__ButtonComponent-x"><span>レビューの投稿</span></a></div>
</div>
<div id="modal"></div>
<script>
  fetch("__API__/reputation");
  let score = 0;
  const stars = [...document.querySelectorAll('a.ReputationStar__Star-x')];
  const paint = () => stars.forEach((a, i) => a.querySelector('i').style.color = i < score ? 'rgb(0, 162, 115)' : 'rgb(223, 231, 229)');
  stars.forEach((a, i) => a.addEventListener('click', () => { if (window.STARS_BROKEN) return; score = i + 1; paint(); upd(); }));
  const ta = document.querySelector('textarea');
  const sub = document.getElementById('submit');
  function upd() { if (score && ta.value.length >= 10 && !window.SUBMIT_BROKEN) sub.removeAttribute('disabled'); }
  ta.addEventListener('input', upd);
  paint();
  sub.addEventListener('click', () => {
    if (sub.hasAttribute('disabled') || window.NO_MODAL) return;
    document.getElementById('modal').innerHTML = '<div class="Modal__ChildrenWrapper-x"><p>レビューの投稿</p>' +
      '<p>公開後にレビューの編集はできません。レビューを投稿してよろしいですか？</p>' +
      '<a type="anchor" class="ButtonBase__ButtonComponent-x" id="cancel">キャンセル</a>' +
      '<a type="anchor" class="ButtonBase__ButtonComponent-x" id="ok">投稿</a></div>';
    document.getElementById('ok').addEventListener('click', () => {
      fetch("__API__/reputation", {method: "POST", body: JSON.stringify({score: score, description: ta.value})})
        .then(() => document.getElementById('modal').innerHTML = '');
    });
  });
</script></body></html>""".replace("__API__", API).replace("__PID__", PID).replace(
    "__STARS__", "".join('<a class="ReputationStar__Star-x"><i class="fa fa-star">★</i></a>' for _ in range(5)))


class FakePool:
    """BrowserPool の代わり（1つのページを使い回す）"""

    def __init__(self, page):
        self._page = page

    @asynccontextmanager
    async def page(self, platform):
        yield self._page

    async def goto(self, platform, page, url, wait_selector=None):
        await page.goto(url, wait_until="domcontentloaded")
        if wait_selector:
            try:
                await page.wait_for_selector(wait_selector, timeout=3000)
            except Exception:  # noqa: BLE001
                pass

    async def screenshot(self, *a, **kw):
        return None


async def _run(stars: int, post_status: int = 201, flags: dict | None = None, posted: bool = False,
               can_review: bool = True, api_reflects_post: bool = True, api_status: int = 200, marks: list | None = None):
    """api_reflects_post=False: 投稿しても API の表示がすぐには変わらない（確認が取れない）場合"""
    state = {"posted": posted, "payload": None, "posts": 0, "marks": [] if marks is None else marks}
    cors = {"Access-Control-Allow-Origin": "*"}

    async def handler(route):
        req = route.request
        url = req.url.split("?")[0]
        shown = state["posted"] and (api_reflects_post or posted)
        if url.endswith(f"/reservations/{PID}/reputation"):
            if req.method == "POST":
                state["posts"] += 1
                state["payload"] = json.loads(req.post_data or "{}")
                if 200 <= post_status < 300:
                    state["posted"] = True
                await route.fulfill(status=post_status, body="{}", headers=cors)
            else:
                await route.fulfill(status=(200 if shown else 404) if api_status == 200 else api_status, body="{}", headers=cors)
        elif url.endswith(f"/owners/h/reservations/{PID}"):
            body = {"status": 8, "reputation_id_by_owner": 999 if shown else None, "can_be_reviewed": can_review,
                    "review_expires_at": "2099-10-26T00:00:00.000+09:00"}
            await route.fulfill(status=api_status, body=json.dumps(body), headers={**cors, "Content-Type": "application/json"})
        elif url.endswith(f"/inbox/{PID}/review"):
            html = REVIEW_HTML
            if flags:
                html = html.replace("<script>", "<script>" + "".join(f"window.{k}={json.dumps(v)};" for k, v in flags.items()), 1)
            await route.fulfill(status=200, body=html, content_type="text/html; charset=utf-8")
        elif url.endswith(f"/inbox/{PID}"):
            await route.fulfill(status=200, body=THREAD_HTML, content_type="text/html; charset=utf-8")
        else:
            await route.abort()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context()
        await ctx.route("**/*", handler)
        page = await ctx.new_page()
        try:
            sm = SpaceMarket(load_config(), FakePool(page))
            token = REVIEW_SUBMIT_HOOK.set(state["marks"].append)  # 最後のボタンを押した印
            try:
                res = await sm.post_review(PID, stars, "この度はご利用いただき、誠にありがとうございました。", dry_run=False)
            finally:
                REVIEW_SUBMIT_HOOK.reset(token)
            return res, state
        finally:
            await browser.close()


async def test_review_is_posted_through_confirm_modal_even_with_guest_badge():
    res, state = await _run(4)
    assert res.ok and not res.skipped and "確認できていません" not in res.note
    assert state["posts"] == 1 and state["marks"] == [True]
    assert state["payload"] == {"score": 4, "description": "この度はご利用いただき、誠にありがとうございました。"}


async def test_already_posted_review_is_not_overwritten():
    res, state = await _run(5, posted=True)
    assert res.ok and res.skipped and state["posts"] == 0 and state["marks"] == []


async def test_successful_post_without_visible_confirmation_is_success():
    # 投稿の通信は成功したが、読み直しても投稿済みの表示がまだ出ない → 失敗にしない（再実行で二重に投稿しない）
    res, state = await _run(5, api_reflects_post=False)
    assert res.ok and state["posts"] == 1 and "確認できていません" in res.note


async def test_not_reviewable_yet_is_not_ready():
    with pytest.raises(NotReady):
        await _run(5, can_review=False)


async def test_review_rejected_by_site_is_reported():
    marks = []
    with pytest.raises(ActionFailed, match="受け付けられませんでした"):
        await _run(5, post_status=422, marks=marks)
    assert marks == [True, False]  # 押したが、受け付けられなかった（投稿されていない → 再実行してよい）


async def test_missing_confirm_modal_is_reported():
    # 確認画面が出ない → 最後のボタンは押していない（印なし。再実行してよい）
    marks = []
    with pytest.raises(ActionFailed, match="確認画面が出ません"):
        await _run(5, flags={"NO_MODAL": True}, marks=marks)
    assert marks == []


async def test_disabled_submit_is_reported():
    with pytest.raises(ActionFailed, match="押せる状態になりません"):
        await _run(5, flags={"SUBMIT_BROKEN": True})


async def test_star_selection_is_checked():
    with pytest.raises(ActionFailed, match="選べませんでした"):
        await _run(3, flags={"STARS_BROKEN": True})


async def test_unknown_review_status_does_not_post():
    # 予約情報もレビューの API も 500（投稿済みか分からない）→ 投稿しない
    with pytest.raises(ActionFailed, match="確認できないため"):
        await _run(5, posted=True, api_status=500)
