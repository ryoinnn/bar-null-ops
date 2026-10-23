"""保存した管理画面（tests/fixtures/pages、個人情報は伏せ字）に、本番と同じ読み取り処理をかけて確かめる。

画面の仕様変更のときは `barnull capture` で取り直してからこのテストを流す。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from playwright.async_api import async_playwright

from barnull.models import Kind
from barnull.platforms import parse_ib, parse_sm
from barnull.platforms import selectors as S

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "pages"


def _need(name: str) -> Path:
    p = FIX / name
    if not p.exists():
        pytest.skip(f"{name} がありません（barnull capture で保存してください）")
    return p


async def _eval(name: str, js: str):
    html = _need(name).read_text(encoding="utf-8")
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.set_content(html, wait_until="domcontentloaded")
        try:
            return await page.evaluate(js)
        finally:
            await browser.close()


def run(name: str, js: str):
    return asyncio.run(_eval(name, js))


# ---------------- スペースマーケット ----------------
def test_sm_inbox_rows():
    rows = parse_sm.parse_inbox(run("sm_inbox.html", S.SM_INBOX_JS))
    assert len(rows) >= 10
    assert all(r.platform_id.isdigit() for r in rows)
    statuses = {r.status for r in rows}
    assert statuses & {"予約完了", "利用完了", "お問い合わせ", "期限切れ"}
    assert any("〜" in r.datetime_text for r in rows)


def test_sm_thread_confirmed_sent():
    d = parse_sm.parse_thread(run("sm_thread_confirmed_sent.html", S.SM_THREAD_JS), "6271768")
    assert d.status == "予約完了"
    assert d.host_revenue and d.guest_total and d.host_revenue < d.guest_total
    assert d.start_at and d.end_at and d.end_at > d.start_at
    assert not d.can_approve
    host = [m for m in d.messages if m.role == "host"]
    assert any("キーボックスの暗証番号" in m.text for m in host)
    assert any(any(f.endswith(".pdf") for f in m.files) for m in host)
    assert all(m.at is not None for m in d.messages if m.role in ("host", "guest"))
    from barnull.platforms.sm import SpaceMarket
    assert SpaceMarket.template_sent(None, d, "キーボックスの暗証番号")


def test_sm_thread_completed_with_exit_record():
    d = parse_sm.parse_thread(run("sm_thread_completed_exit.html", S.SM_THREAD_JS), "6248079")
    assert d.status == "利用完了"
    assert d.exit_status == "問題なく退室しました"
    assert d.review_posted is None  # 投稿済みかは画面の文字ではなく API で判定する
    assert d.purpose  # [利用目的]
    roles = [m.role for m in d.messages]
    assert "guest" in roles and "host" in roles and "system" in roles
    # 発言の日時は古い順
    ats = [m.at for m in d.messages if m.at]
    assert ats == sorted(ats)


def test_sm_thread_expired():
    d = parse_sm.parse_thread(run("sm_thread_inquiry_expired.html", S.SM_THREAD_JS), "6167621")
    assert d.status == "期限切れ" and d.closed_reason
    d = parse_sm.parse_thread(run("sm_thread_request_expired.html", S.SM_THREAD_JS), "6126305")
    assert d.status == "期限切れ" and d.closed_reason and d.kind is Kind.RESERVATION


def test_sm_review_form_elements():
    counts = run("sm_review_form.html", f"""() => ({{
        stars: document.querySelectorAll('{S.SM_REVIEW_STAR}').length,
        textarea: document.querySelectorAll('{S.SM_REVIEW_TEXTAREA}').length,
        submit: [...document.querySelectorAll('a')].filter(a => a.innerText.trim() === 'レビューの投稿').length,
    }})""")
    assert counts == {"stars": 5, "textarea": 1, "submit": 1}


def test_sm_message_form_elements():
    counts = run("sm_thread_confirmed_sent.html", f"""() => ({{
        textarea: document.querySelectorAll('{S.SM_TEXTAREA}').length,
        file: document.querySelectorAll('{S.SM_FILE_INPUT}').length,
        send: [...document.querySelectorAll('{S.SM_SEND}')].filter(a => a.innerText.trim() === '送信').length,
    }})""")
    assert counts == {"textarea": 1, "file": 1, "send": 1}


# ---------------- インスタベース ----------------
def test_ib_bookings():
    rows = parse_ib.parse_bookings(run("ib_bookings.html", S.IB_BOOKINGS_JS))
    assert len(rows) >= 5
    assert all(len(r.platform_id) == 10 for r in rows)
    assert all(r.guest_id for r in rows)
    assert any("予約確定" in r.status for r in rows)


def test_ib_booking_detail():
    d = parse_ib.parse_detail(run("ib_booking_detail.html", S.IB_DETAIL_JS), "8797784002")
    assert d.status == "予約確定"
    assert d.start_at and d.end_at
    assert d.guest_total == 18150 and d.host_revenue == 11797
    assert d.guest_name and d.people
    d = parse_ib.parse_detail(run("ib_booking_detail_options.html", S.IB_DETAIL_JS), "7000288937")
    assert d.options and "×" in d.options
    d = parse_ib.parse_detail(run("ib_booking_detail_cancelled.html", S.IB_DETAIL_JS), "8969626646")
    assert d.closed_reason


def test_ib_thread():
    msgs, gid, orders = parse_ib.parse_thread(run("ib_thread_with_host.html", S.IB_THREAD_JS))
    roles = [m.role for m in msgs]
    assert "host" in roles and "system" in roles
    assert gid and orders
    host = [m for m in msgs if m.role == "host"]
    assert any("キーボックスの暗証番号" in m.text for m in host)
    assert any(any(f.endswith(".pdf") for f in m.files) for m in host)
    assert all(m.at for m in msgs)
    assert [m.at for m in msgs] == sorted(m.at for m in msgs)
    msgs, _, _ = parse_ib.parse_thread(run("ib_thread_change_request.html", S.IB_THREAD_JS))
    assert any(m.role == "guest" and parse_ib.is_change_request(m.text) for m in msgs)


def test_ib_message_form_and_inbox():
    counts = run("ib_thread_with_host.html", f"""() => ({{
        form: document.querySelectorAll('{S.IB_MSG_FORM}').length,
        textarea: document.querySelectorAll('{S.IB_MSG_TEXTAREA}').length,
        file: document.querySelectorAll('{S.IB_MSG_FILE}').length,
        submit: document.querySelectorAll('{S.IB_MSG_SUBMIT}').length,
        pdf: (document.querySelector('{S.IB_MSG_FILE}') || {{}}).accept || '',
    }})""")
    assert counts["form"] == counts["textarea"] == counts["file"] == counts["submit"] == 1
    assert "application/pdf" in counts["pdf"]
    rows = run("ib_inbox.html", S.IB_INBOX_JS)
    assert rows and all(r["thread_id"].isdigit() for r in rows)


def test_ib_review_form():
    r = run("ib_review_form.html", f"""() => ({{
        point: [...document.querySelectorAll('{S.IB_REVIEW_POINT}')].map(s => [...s.options].map(o => o.value)),
        comment: document.querySelectorAll('{S.IB_REVIEW_COMMENT}').length,
        submit: document.querySelectorAll('{S.IB_REVIEW_SUBMIT}').length,
    }})""")
    assert r["point"] and r["point"][0][0] == "5"
    assert r["comment"] >= 1 and r["submit"] >= 1


def test_sm_api_ids_attached_to_messages():
    import json

    api = json.loads(_need("api_responses.json").read_text(encoding="utf-8"))
    msgs = [m for r in api if r["url"].split("?")[0].endswith("/reservations/6248079/messages")
            and isinstance(r["body"], list) for m in r["body"]]
    d = parse_sm.parse_thread(run("sm_thread_completed_exit.html", S.SM_THREAD_JS), "6248079")
    parse_sm.attach_api(d, msgs, 404)
    talk = [m for m in d.messages if m.role in ("host", "guest")]
    assert talk and all(m.uid for m in talk)
    assert d.review_posted is False


def test_sm_thread_reads_image_messages():
    # 2026-10-22: ゲストが画像（image.jpg）だけを送った発言は SendImageOpponent で描かれ、読み取りから漏れていた。
    # API の発言数と画面の発言数が合わず、返信の送信が「スレッドのメッセージを読み切れない」で止まった
    html = """<!doctype html><html><body>
    <div class="MainContentsWithSidebarInbox__SidebarWrapper-x"><div>利用完了</div></div>
    <div class="DetailPage__PageContents-x">
      <div class="MessagesForm__Section-x"><div class="MessageSelf__Wrapper-x"><div class="MessageSelf__Message-x">ご予約ありがとうございます</div></div> 15:07</div>
      <div class="MessagesForm__Section-x"><div class="SendImageOpponent__Wrapper-x"><div class="SendImageOpponent__Container-x">
        <a><div class="SendImageOpponent__ThumbnailTitle-x">image.jpg</div></a></div></div> 16:43</div>
      <div class="MessagesForm__Section-x"><div class="SendImageSelf__Wrapper-x"><a><div class="SendImageSelf__ThumbnailTitle-x">photo.png</div></a></div> 16:45</div>
      <div class="MessagesForm__Section-x"><div class="MessageOpponent__Wrapper-x"><div class="MessageOpponent__Message-x">ご確認いただけますか</div></div> 16:44</div>
    </div></body></html>"""

    async def go():
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.set_content(html, wait_until="domcontentloaded")
            try:
                return await page.evaluate(S.SM_THREAD_JS)
            finally:
                await browser.close()
    raw = asyncio.run(go())
    d = parse_sm.parse_thread(raw, "1")
    talk = [m for m in d.messages if m.role in ("host", "guest")]
    assert len(talk) == 4
    assert any(m.role == "guest" and m.files == ["image.jpg"] for m in talk)
    assert any(m.role == "host" and m.files == ["photo.png"] for m in talk)
