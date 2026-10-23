"""管理画面の構造をテスト用に保存する（個人情報は伏せる）。開発・画面変更時の調査用。

uv run barnull capture  →  tests/fixtures/pages/<platform>_<name>.html / .json
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from playwright.async_api import Page

from ..config import ROOT, Config
from ..models import Platform
from ..secrets import redact
from ..textutil import mask_contacts
from .browser import BrowserPool

log = logging.getLogger(__name__)
OUT = ROOT / "tests" / "fixtures" / "pages"

SM_BASE = "https://dashboard.spacemarket.com/{host}"
SM_TARGETS = {
    "inbox": "/inbox",
    "thread_confirmed_sent": "/inbox/6271768",
    "thread_completed_exit": "/inbox/6248079",
    "thread_inquiry_expired": "/inbox/6167621",
    "thread_request_expired": "/inbox/6126305",
    "thread_confirmed_future": "/inbox/6219104",
    "thread_completed_2": "/inbox/6087596",
    "review_form": "/inbox/6248079/review",
}
IB_BASE = "https://www.instabase.jp"
IB_TARGETS = {
    "bookings": "/partners/bookings",
    "booking_detail": "/partners/bookings/8797784002",
    "booking_detail_options": "/partners/bookings/7000288937",
    "booking_detail_cancelled": "/partners/bookings/8969626646",
    "inbox": "/partners/inbox",
    "thread_change_request": "/partners/inbox/4618365",
    "thread_with_host": "/partners/inbox/4574232",
    "review_creatables": "/partners/bookings/user_review_creatables",
    "review_posted_list": "/partners/bookings/has_user_reviews",
    "review_form": "/partners/bookings/5752070359/user_reviews/new",
}

# 画面から氏名を集める（伏せ字の対象）
NAMES_JS = r"""() => {
  const out = new Set();
  const add = s => { if (s) { s = s.replace(/\s*(様|さま|さん)?\s*(\(\d+名\))?\s*$/, '').trim(); if (s && s.length <= 30) out.add(s); } };
  document.querySelectorAll('[class*="ListItemUserInfo__UserNameContainer"]').forEach(e => add(e.innerText));
  document.querySelectorAll('a[href^="/partners/users/"]').forEach(e => add(e.innerText));
  const t = document.body.innerText;
  for (const re of [/ゲスト詳細\s*\n+\s*([^\n]+)/g, /氏名\t([^\n]+)/g, /フリガナ\t([^\n]+)/g, /お客様情報\s*\n+\s*([^\n]+)/g, /\n([^\n]+?)さんとのメッセージ/g]) {
    let m; while ((m = re.exec(t))) add(m[1]);
  }
  const tm = document.title.match(/^(.+?)様 \(ID/); if (tm) add(tm[1]);
  return [...out];
}"""

# 保存する HTML（スクリプト・画像を除く）
HTML_JS = r"""() => {
  const root = document.body.cloneNode(true);
  root.querySelectorAll('script, style, noscript, iframe, link, svg, img, video, picture, source').forEach(e => e.remove());
  root.querySelectorAll('input[name="authenticity_token"]').forEach(e => e.setAttribute('value', 'REDACTED'));
  // 署名付き URL は、署名（? 以降）だけを消してドメインとパスは残す
  root.querySelectorAll('a[href*="amazonaws.com"]').forEach(e => e.setAttribute('href', (e.getAttribute('href') || '').split('?')[0]));
  root.querySelectorAll('[style]').forEach(e => e.removeAttribute('style'));
  return '<!doctype html><html><head><meta charset="utf-8"><title>' + document.title.replace(/</g,'') + '</title></head>' + root.outerHTML + '</html>';
}"""

DUMMIES = ["山田 太郎", "佐藤 花子", "鈴木 一郎", "高橋 美咲", "田中 健", "伊藤 愛", "渡辺 翔", "中村 葵",
           "小林 蓮", "加藤 陽菜", "吉田 湊", "山本 結衣", "松本 大和", "井上 凛", "木村 陽翔", "林 莉子"]


class Masker:
    def __init__(self) -> None:
        self.map: dict[str, str] = {}

    def add(self, name: str) -> None:
        name = name.strip()
        if not name or name in self.map or name.startswith("株式会社0x0"):
            return
        dummy = DUMMIES[len(self.map) % len(DUMMIES)]
        self.map[name] = dummy
        parts = re.split(r"[\s　]+", name)
        dparts = dummy.split()
        if len(parts) >= 2:
            self.map.setdefault("".join(parts), "".join(dparts))
            self.map.setdefault(parts[0], dparts[0])
            if len(parts[-1]) >= 2:
                self.map.setdefault(parts[-1], dparts[-1])

    def apply(self, text: str) -> str:
        """1回の置換で全氏名を置き換える（置き換えたダミー名が再置換されないように）"""
        keys = sorted((k for k in self.map if len(k) >= 2), key=len, reverse=True)
        if keys:
            # 名前の間の空白（半角・全角・&nbsp;）の違いも吸収する
            def pat(k: str) -> str:
                parts = re.split(r"[\s　]+", k)
                return r"(?:\s|　|&nbsp;)*".join(re.escape(x) for x in parts)
            rx = re.compile("|".join(f"(?P<k{i}>{pat(k)})" for i, k in enumerate(keys)))
            text = rx.sub(lambda m: self.map[keys[int(m.lastgroup[1:])]], text)
        text = re.sub(r"(ID: )\d{10}", r"\g<1>9000000001", text)
        text = re.sub(r"/partners/users/\d+", "/partners/users/5000000001", text)
        # 暗証番号・パスワード・トークン（定型文の中の暗証番号など）は 0000 に置き換える
        text = redact(text).replace("****", "0000")
        text = re.sub(r"(キーボックスの暗証番号は?\s*【)\s*\d+\s*(】)", r"\g<1>0000\g<2>", text)
        return mask_contacts(text)

    def leaks(self, text: str) -> int:
        """伏せ字の漏れの数（ダミー名と同じ文字列は数えない）"""
        dummy_parts = {x for d in DUMMIES for x in [d, d.replace(" ", ""), *d.split()]}
        return sum(1 for k in self.map if len(k) >= 2 and k not in dummy_parts and k in text)


_PII_KEY = re.compile(r"(^|_)(name|first_name|last_name|family_name|given_name|kana|nickname|tel|phone|phone_number|"
                      r"email|address|address\d|street|city|prefecture|zip|zipcode|zip_code|postal_code|postcode|"
                      r"birthday|birth_date|birthdate|gender|image_url|avatar|profile_image|token|access_token|"
                      r"refresh_token|referral_token|password|secret|company|company_name|card|last4)$", re.I)
_KEEP_KEY = re.compile(r"(room|space|plan|venue|building|option|purpose|status|type|file)", re.I)


def _scrub(obj):
    """API 応答の個人情報らしいキーの値を伏せる"""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if isinstance(v, str) and _PII_KEY.search(k) and not _KEEP_KEY.search(k):
                out[k] = "REDACTED"
            else:
                out[k] = _scrub(v)
        return out
    if isinstance(obj, list):
        return [_scrub(v) for v in obj]
    return obj


async def _grab(pool: BrowserPool, platform: Platform, page: Page, name: str, url: str, masker: Masker,
                pages: dict[str, str]) -> None:
    """画面の HTML をメモリに取り、氏名を集める（伏せ字は全画面を取り終えてから一括で行う）"""
    await pool.goto(platform, page, url)
    await page.wait_for_timeout(2500)
    for n in await page.evaluate(NAMES_JS):
        masker.add(n)
    pages[f"{platform.value}_{name}.html"] = await page.evaluate(HTML_JS)
    log.info("read %s_%s (%s)", platform.value, name, page.url)


async def capture(cfg: Config, only: str | None = None) -> Path:
    pool = BrowserPool(cfg, headless=True)
    masker = Masker()
    api: list[dict] = []
    pages: dict[str, str] = {}
    try:
        for platform, base, targets in (
            (Platform.SM, SM_BASE.format(host=cfg.sm_host_id), SM_TARGETS),
            (Platform.IB, IB_BASE, IB_TARGETS),
        ):
            if only and only != platform.value:
                continue
            async with pool.page(platform) as page:
                async def on_response(resp, platform=platform):
                    if "mp-gateway.spacemarket.com" in resp.url or "/api/" in resp.url:
                        try:
                            body = await resp.json()
                        except Exception:  # noqa: BLE001
                            return
                        api.append({"platform": platform.value, "url": resp.url, "status": resp.status, "body": body})
                page.on("response", on_response)
                for name, path in targets.items():
                    try:
                        await _grab(pool, platform, page, name, base + path, masker, pages)
                    except Exception as e:  # noqa: BLE001
                        log.warning("capture failed %s %s: %s", platform.value, name, e)
        # 全画面の氏名を集め終えてから、一括で伏せ字にして保存する
        OUT.mkdir(parents=True, exist_ok=True)
        pages["api_responses.json"] = json.dumps(_scrub(api), ensure_ascii=False, indent=1)
        leak_files = []
        for fname, raw in pages.items():
            masked = masker.apply(raw)
            if masker.leaks(masked):
                leak_files.append(fname)
                continue  # 漏れがあるファイルは保存しない
            (OUT / fname).write_text(masked, encoding="utf-8")
            log.info("saved %s", fname)
        if leak_files:
            log.warning("伏せ字の漏れがあるため保存しなかったファイル: %s", leak_files)
        log.info("masked names: %d", len(masker.map))
    finally:
        await pool.stop()
    return OUT
