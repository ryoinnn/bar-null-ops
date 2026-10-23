"""テスト用の模擬プラットフォーム（管理画面に触れずに Slack 側の流れを試す）。

`barnull run --mock`（DRY_RUN のときだけ）で使う。予約は `barnull simulate` で作る。
データはプロセスのメモリ上だけにあり、再起動で消える。
"""

from __future__ import annotations

import copy
from datetime import datetime, timedelta

from ..models import Detail, Kind, ListingRow, Message, Platform
from ..textutil import JST, now
from .base import ActionFailed, ActionResult
from .browser import LoginRequired
from .ib import Instabase
from .sm import SpaceMarket

NOTE = "(MOCK) 管理画面は操作していません。"


def _code() -> str:
    from .. import secrets

    return secrets.get_optional("KEYBOX_CODE") or "0000"


def _next_weekday(weekday: int, hour: int, minute: int = 0) -> datetime:
    base = now() + timedelta(days=3)
    d = base + timedelta(days=(weekday - base.weekday()) % 7)
    return datetime(d.year, d.month, d.day, hour, minute, tzinfo=JST)


def sample_detail(platform: Platform, pid: str, guest_text: str | None = None, variant: str | None = None) -> Detail:
    """variant: None（通常の予約）／"inquiry"（SM の予約前の問い合わせ）／"request"（IB の承認制リクエスト）"""
    if platform is Platform.SM and variant == "inquiry":
        d = Detail(platform=platform, platform_id=pid, kind=Kind.INQUIRY_ONLY, status="お問い合わせ",
                   guest_name="鈴木 一郎", messages=[])
    elif platform is Platform.SM:
        start = _next_weekday(4, 20)  # 金曜 20:00〜23:00
        d = Detail(
            platform=platform, platform_id=pid, status="保留中", guest_name="山田 太郎",
            start_at=start, end_at=start + timedelta(hours=3), guest_total=33000, host_revenue=23100,
            people="〜15人", purpose="パーティー", plan="夜プラン", can_approve=True,
            # 既定の発言は日時なし（再起動で模擬データを作り直しても同じ発言とみなされるように）
            messages=[Message("guest", "[利用目的] パーティー\n[利用形態] 個人利用\n[人数] 〜15人\n[プラン] 夜プラン")],
        )
    else:
        start = _next_weekday(6, 10)  # 日曜 10:00〜15:30（準備 9:30〜、清掃 〜16:00）
        d = Detail(
            platform=platform, platform_id=pid, status="予約確定", guest_name="佐藤 花子", guest_id=f"mock-{pid}",
            start_at=start, end_at=start + timedelta(hours=5, minutes=30),
            prep_start_at=start - timedelta(minutes=30), cleanup_end_at=start + timedelta(hours=6),
            guest_total=18150, host_revenue=11797, people="7名",
            purpose="パーティー・飲み会 - その他のパーティー・飲み会", purpose_detail="大学の同窓会です。",
            options="ゴミ処理 ×1個", thread_id=f"mock-thread-{pid}",
            messages=[Message("system", f"【自動配信メッセージ】\n予約ID：{pid} の予約状況が「予約確定」になりました")],
        )
        if variant == "request":
            d.status, d.guest_name, d.messages = "予約リクエスト中", "高橋 美咲", []
    if guest_text:
        d.messages.append(Message("guest", guest_text, at=now()))
    _reindex(d)
    return d


def _reindex(d: Detail) -> None:
    for i, m in enumerate(d.messages):
        m.index = i


class MockStore:
    def __init__(self, loader=None, marker: str = "キーボックスの暗証番号") -> None:
        self.details: dict[tuple[Platform, str], Detail] = {}
        self.fail_next: dict[str, str] = {}  # 予約ID → 次の書き込み操作を失敗させる種類（"action" / "login"）
        self.loader = loader  # (platform, pid) -> (reservations の行, inquiries の行)
        self.marker = marker

    def maybe_fail(self, step: str, pid: str) -> None:
        kind = self.fail_next.pop(pid, None)
        if kind == "login":
            raise LoginRequired("(MOCK) ログイン画面が表示されました（テスト用の模擬エラー）")
        if kind:
            raise ActionFailed(step, "(MOCK) 画面の要素が見つかりません（テスト用の模擬エラー）")

    def _rebuild(self, platform: Platform, pid: str) -> Detail | None:
        loaded = self.loader(platform, pid) if self.loader else None
        if not loaded or loaded[0] is None:
            return None
        row, inquiries = loaded
        from ..textutil import from_iso

        d = sample_detail(platform, pid, variant="inquiry" if row["kind"] == "inquiry_only" else None)
        d.guest_name = row["guest_name"] or d.guest_name
        if row["platform_status"]:
            d.status = row["platform_status"]
        d.start_at = from_iso(row["start_at"]) or d.start_at
        d.end_at = from_iso(row["end_at"]) or d.end_at
        if row["thread_id"]:
            d.thread_id = row["thread_id"]
        self.details[(platform, pid)] = d
        if row["template_sent_at"]:
            self.add_message(platform, pid, "host", f"ご予約ありがとうございます！{self.marker}は【{_code()}】です")
            self.add_message(platform, pid, "host", "", ["Bar_null_スペース利用マニュアル.pdf"])
            d.status, d.can_approve = ("予約完了" if platform is Platform.SM else d.status), False
        for q in inquiries:
            if q["guest_text"] and q["state"] != "SUPERSEDED":
                d.messages.append(Message("guest", q["guest_text"], at=from_iso(q["received_at"])))
            if q["state"] == "SENT" and q["sent_text"]:
                d.messages.append(Message("host", q["sent_text"], at=from_iso(q["sent_at"])))
        _reindex(d)
        return d

    def create(self, platform: Platform, pid: str, guest_text: str | None = None, variant: str | None = None) -> Detail:
        d = sample_detail(platform, pid, guest_text, variant)
        self.details[(platform, pid)] = d
        return copy.deepcopy(d)

    def get(self, platform: Platform, pid: str) -> Detail:
        if (platform, pid) not in self.details:
            self._rebuild(platform, pid)
        if (platform, pid) not in self.details:
            raise ActionFailed("模擬データ", f"{pid} は模擬データにありません（再起動で消えた可能性）")
        return self.details[(platform, pid)]

    def add_message(self, platform: Platform, pid: str, role: str, text: str, files: list[str] | None = None) -> None:
        d = self.get(platform, pid)
        d.messages.append(Message(role, text, files=files or [], at=now()))
        _reindex(d)

    def mark_template_sent(self, platform: Platform, pid: str, marker: str) -> None:
        self.add_message(platform, pid, "host", f"（手動で送信）ご予約ありがとうございます！{marker}は【{_code()}】です")
        self.add_message(platform, pid, "host", "", ["Bar_null_スペース利用マニュアル.pdf"])
        d = self.get(platform, pid)
        d.status, d.can_approve = ("予約完了" if platform is Platform.SM else d.status), False


class MockSM(SpaceMarket):
    def __init__(self, cfg, store: MockStore):
        super().__init__(cfg, None)
        self.store = store

    async def list_inbox(self) -> list[ListingRow]:
        return []

    async def get_detail(self, pid: str) -> Detail:
        return copy.deepcopy(self.store.get(Platform.SM, pid))

    async def approve_and_send_template(self, pid, text, pdf, marker, dry_run, code=None) -> ActionResult:
        self.store.maybe_fail("承認・定型文の送信", pid)
        d = self.store.get(Platform.SM, pid)
        if d.closed_reason:
            raise ActionFailed("状態確認", f"予約が「{d.status}」のため送信しません")
        if self.template_sent(d, marker, code):
            return ActionResult(ok=True, skipped=True, dry_run=True, note="定型文とマニュアルは送信済みでした（手動対応）")
        self.store.add_message(Platform.SM, pid, "host", text)
        self.store.add_message(Platform.SM, pid, "host", "", [pdf.name])
        d.status, d.can_approve = "予約完了", False
        return ActionResult(ok=True, dry_run=True, note=NOTE)

    async def send_message(self, pid, text, dry_run, already_sent_check=None, guard=None) -> ActionResult:
        self.store.maybe_fail("メッセージ送信", pid)
        if guard is not None:
            guard(copy.deepcopy(self.store.get(Platform.SM, pid).messages))
        self.store.add_message(Platform.SM, pid, "host", text)
        return ActionResult(ok=True, dry_run=True, note=NOTE)

    async def post_review(self, pid, stars, comment, dry_run) -> ActionResult:
        self.store.maybe_fail("レビュー投稿", pid)
        self.store.get(Platform.SM, pid).review_posted = True
        return ActionResult(ok=True, dry_run=True, note=NOTE)


class MockIB(Instabase):
    def __init__(self, cfg, store: MockStore):
        super().__init__(cfg, None)
        self.store = store

    async def list_bookings(self, sort_by_booked: bool = True) -> list[ListingRow]:
        return []

    async def get_detail(self, pid: str, with_thread: bool = True) -> Detail:
        return copy.deepcopy(self.store.get(Platform.IB, pid))

    async def get_thread(self, tid: str):
        for (p, pid), d in self.store.details.items():
            if p is Platform.IB and d.thread_id == tid:
                return copy.deepcopy(d.messages), d.guest_id, [pid]
        return [], None, []

    async def unreplied_threads(self):
        return [], 0

    async def send_template(self, pid, text, pdf, marker, dry_run, code=None):
        self.store.maybe_fail("定型文の送信", pid)
        d = self.store.get(Platform.IB, pid)
        if d.closed_reason:
            raise ActionFailed("状態確認", f"予約が「{d.status}」のため送信しません")
        approved = ""
        if "リクエスト" in d.status:
            d.status = "予約確定"
            approved = "承認しました。"
        if self.template_sent(d.messages, marker, 0, code):
            return ActionResult(ok=True, skipped=True, dry_run=True, note="定型文とマニュアルは送信済みでした（手動対応）"), d.thread_id
        self.store.add_message(Platform.IB, pid, "host", text, [pdf.name])
        return ActionResult(ok=True, dry_run=True, note=NOTE + approved), d.thread_id

    async def send_message(self, tid, text, dry_run, already_sent_check=None, guard=None) -> ActionResult:
        for (p, pid), d in list(self.store.details.items()):
            if p is Platform.IB and d.thread_id == tid:
                self.store.maybe_fail("メッセージ送信", pid)
                if guard is not None:
                    guard(copy.deepcopy(d.messages))
                self.store.add_message(Platform.IB, pid, "host", text)
        return ActionResult(ok=True, dry_run=True, note=NOTE)

    async def post_review(self, pid, stars, comment, dry_run) -> ActionResult:
        self.store.maybe_fail("レビュー投稿", pid)
        self.store.get(Platform.IB, pid).review_posted = True
        return ActionResult(ok=True, dry_run=True, note=NOTE)

    async def review_pending_ids(self) -> set[str]:
        return set()
