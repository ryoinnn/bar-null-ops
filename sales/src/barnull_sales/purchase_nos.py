"""NOS（NIGITA の発注サイト）の仕入実績一覧を Playwright で読む。読み取りだけ。

- 操作するのは、取引日の期間・「総合計金額を表示する」・「検索」だけ（注文・カート・設定には触れない）
- ログイン状態は sales/samples/purchases/browser/ のプロファイルに保存する（git に入れない）
- ログイン画面に飛ばされたら、資格情報マネージャー（barnull-sales の NOS_ACCOUNT・NOS_PASSWORD）が
  あれば自動でログインし直す。無ければ LoginRequired（本人が `purchase-login` でログインする）。
  パスワードは本人が `purchase-set-login` で登録する（Claude は扱わない）。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from .purchase import NosFormatError, Totals, _int

log = logging.getLogger(__name__)

BASE = "https://order.nigita.co.jp"
LOGIN_URL = f"{BASE}/oms/login"
RESULTS_URL = f"{BASE}/oms/stockingResults"
BLOCKERS = re.compile(r"(認証コード|確認コード|ワンタイム|2段階|二段階|ロボットではありません|recaptcha|hcaptcha|turnstile)", re.I)

READ_TABLE = """() => {
  const t = document.querySelector('#data-table');
  if (!t) return null;
  const header = [...t.querySelectorAll('thead th')].map(th => th.innerText.trim());
  let rows;
  if (window.jQuery && jQuery.fn.dataTable && jQuery.fn.dataTable.isDataTable(t)) {
    rows = jQuery(t).DataTable().rows().data().toArray().map(r => Array.from(r, c => String(c ?? '')));
  } else {
    rows = [...t.querySelectorAll('tbody tr')].filter(tr => tr.cells.length === header.length)
      .map(tr => [...tr.cells].map(td => td.innerText.trim()));
  }
  const tmp = document.createElement('div');
  rows = rows.map(r => r.map(c => { tmp.innerHTML = c; return tmp.innerText.trim(); }));
  const sum = [...document.querySelectorAll('table')].map(x => x.innerText)
    .find(s => /総合計金額/.test(s) && /消費税/.test(s) && s.length < 300) || '';
  return { header, rows, sum, customer: (document.body.innerText.match(/^\\s*(.+?)\\s*様/m) || [])[1] || '' };
}"""


class LoginRequired(RuntimeError):
    """NOS へのログインが必要（自動でログインできない）"""


@dataclass
class NosResult:
    header: list[str]
    rows: list[list[str]]
    totals: Totals | None
    customer: str


def _totals(text: str) -> Totals | None:
    m = {k: re.search(k + r"\s*[￥¥]?\s*(-?[\d,]+)", text) for k in ("合計金額", "消費税", "総合計金額")}
    # 「総合計金額」の中にも「合計金額」があるので、先頭の「合計金額」を取る
    if not all(m.values()):
        return None
    return Totals(net=_int(m["合計金額"][1]), tax=_int(m["消費税"][1]), gross=_int(m["総合計金額"][1]))


def _is_login(page) -> bool:
    """ログインが必要な状態か。セッションが切れていると /oms/error?timeout に飛ばされる（2026-11-15 確認）。"""
    url = page.url
    return ("/oms/login" in url or "/oms/error" in url or url.rstrip("/").endswith("/oms")
            or page.locator("input[type=password]").count() > 0)


def _login(page, account: str, password: str) -> None:
    page.goto(LOGIN_URL, wait_until="domcontentloaded")
    form = page.locator("form").filter(has=page.locator("input[type=password]")).first
    form.locator("input:not([type=hidden]):not([type=password])").first.fill(account)
    form.locator("input[type=password]").fill(password)
    form.get_by_role("button", name=re.compile("ログイン")).click()
    page.wait_for_load_state("domcontentloaded")
    page.wait_for_timeout(1500)


def _close_notice(page) -> None:
    """ログイン直後のお知らせ（出ないこともある）を閉じる。"""
    btn = page.get_by_role("button", name=re.compile(r"^\s*閉じる\s*$"))
    for i in range(btn.count()):
        if btn.nth(i).is_visible():
            btn.nth(i).click()
            page.wait_for_timeout(500)


def open_context(profile: Path, *, headless: bool):
    from playwright.sync_api import sync_playwright

    pw = sync_playwright().start()
    profile.mkdir(parents=True, exist_ok=True)
    ctx = pw.chromium.launch_persistent_context(str(profile), headless=headless, locale="ja-JP",
                                                viewport={"width": 1280, "height": 900})
    return pw, ctx


def fetch(profile: Path, since: date, until: date, *, account: str = "", password: str = "",
          headless: bool = True) -> NosResult:
    pw, ctx = open_context(profile, headless=headless)
    try:
        page = ctx.new_page()
        page.goto(RESULTS_URL, wait_until="domcontentloaded")
        if _is_login(page):
            if not (account and password):
                raise LoginRequired("NOS のログインが切れています（自動ログインの ID・パスワードが未登録）")
            _login(page, account, password)
            if BLOCKERS.search(page.content()):
                raise LoginRequired("NOS のログインで追加の確認（認証コード等）を求められました")
            _close_notice(page)
            page.goto(RESULTS_URL, wait_until="domcontentloaded")
            if _is_login(page):
                raise LoginRequired("NOS に自動でログインできませんでした（ID・パスワードを確認してください）")
        _close_notice(page)
        if page.locator("#offsetDeliveryDate").count() == 0:
            raise NosFormatError("仕入実績一覧の検索欄（取引日）が見つかりません")
        page.fill("#offsetDeliveryDate", since.strftime("%Y/%m/%d"))
        page.fill("#limitDeliveryDate", until.strftime("%Y/%m/%d"))
        if page.locator("#selectOrderAssortment").count():
            page.select_option("#selectOrderAssortment", value="")
        page.check("#showTotalAmount")
        with page.expect_navigation(wait_until="domcontentloaded"):
            page.locator("button[onclick='search()']").click()
        page.wait_for_selector("#data-table", timeout=30000)
        page.wait_for_timeout(1000)
        got = page.evaluate(READ_TABLE)
        if not got:
            raise NosFormatError("仕入実績一覧の表が見つかりません")
        rows = [r for r in got["rows"] if len(r) == len(got["header"])]
        return NosResult(header=got["header"], rows=rows, totals=_totals(got["sum"]), customer=got["customer"])
    finally:
        ctx.close()
        pw.stop()


def login_interactive(profile: Path) -> bool:
    """ブラウザの画面を開き、本人がログインするのを待つ（最大10分）。ログインできたら True。"""
    pw, ctx = open_context(profile, headless=False)
    try:
        page = ctx.new_page()
        page.goto(RESULTS_URL, wait_until="domcontentloaded")
        if not _is_login(page):
            return True
        print("開いたブラウザで NOS にログインしてください（10分待ちます）。")
        for _ in range(600):
            page.wait_for_timeout(1000)
            try:
                if "/oms/" in page.url and not _is_login(page):
                    page.goto(RESULTS_URL, wait_until="domcontentloaded")
                    return not _is_login(page)
            except Exception:  # noqa: BLE001  画面の遷移中
                continue
        return False
    finally:
        ctx.close()
        pw.stop()
