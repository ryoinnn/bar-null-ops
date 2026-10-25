"""偽物の連携先（テストと、手元で試す `barnull-sales try` で使う）。"""

from __future__ import annotations

from datetime import date

from .slips import Slip
from .texts import asana_task_name


class FakeNotifier:
    """Slack の代わり。投稿を記録する。"""

    def __init__(self) -> None:
        self.posts: list[dict] = []
        self.updates: list[dict] = []

    def post(self, channel: str, text: str, thread_ts: str | None = None, broadcast: bool = False) -> str:
        ts = f"1000.{len(self.posts) + 1:04d}"
        self.posts.append({"channel": channel, "text": text, "thread_ts": thread_ts, "broadcast": broadcast, "ts": ts})
        return ts

    def update(self, channel: str, ts: str, text: str) -> None:
        self.updates.append({"channel": channel, "ts": ts, "text": text})

    def permalink(self, channel: str, ts: str) -> str | None:
        return f"https://slack.example/archives/{channel}/p{ts.replace('.', '')}"


class FakeTracker:
    """Asana の代わり。タスクを辞書で持つ。テストでは tasks[gid]["override"] に手動上書きを入れられる。"""

    def __init__(self) -> None:
        self.tasks: dict[str, dict] = {}
        self.fail: set[str] = set()     # 失敗させる操作（"create" / "update"）。障害時の動きのテスト用

    def _maybe_fail(self, op: str) -> None:
        if op in self.fail:
            from .http import ExternalAPIError
            raise ExternalAPIError("Asana", op, 503, "テストで失敗させた")

    def create_task(self, slip: Slip) -> tuple[str, str]:
        self._maybe_fail("create")
        gid = f"t{len(self.tasks) + 1}"
        self.tasks[gid] = {"name": asana_task_name(slip), "status": slip.status}
        return gid, f"https://asana.example/{gid}"

    def update_task(self, slip: Slip) -> None:
        self._maybe_fail("update")
        self.tasks[slip.asana["task_gid"]].update(name=asana_task_name(slip), status=slip.status,
                                                  completed=bool(slip.closed_at), square=dict(slip.square))

    def read_overrides(self, since: str | None = None) -> list[dict]:
        return [{"task_gid": g, "value": t["override"], "by": "テスト担当", "at": "2026-10-17T10:00:00Z"}
                for g, t in self.tasks.items() if t.get("override")]

    def reset_override(self, task_gid: str) -> None:
        self.tasks[task_gid]["override"] = None


class FakePayments:
    def __init__(self, payments=None) -> None:
        self.payments = list(payments or [])

    def list_payments(self, begin, end):
        return [p for p in self.payments if begin <= p.created_at < end]


class FakePayouts:
    """payouts: [{"id", "amount_money": {"amount"}, "arrival_date", "status", "_payments": [決済ID]}]"""

    def __init__(self, payouts=None) -> None:
        self.payouts = list(payouts or [])
        self.entry_calls = 0

    def list_payouts_for(self, location_id, begin):
        """ほかの店舗の振込（テストでは others に入れておく）。"""
        return list(getattr(self, "others", {}).get(location_id, []))

    def list_payouts(self, begin):
        return self.payouts

    def payment_ids(self, payout_id):
        self.entry_calls += 1
        return next(p["_payments"] for p in self.payouts if p["id"] == payout_id)


class FakeBank:
    def __init__(self, txns=None) -> None:
        self.txns = list(txns or [])

    def incomes(self, start, end):
        return [t for t in self.txns if start <= date.fromisoformat(t.date) <= end]


class FakeLine:
    def __init__(self) -> None:
        self.replies: list[tuple] = []

    def member_display_name(self, group_id, user_id):
        return "テスト担当"

    def group_name(self, group_id):
        return "テストグループ"

    def reply_ok_sticker(self, reply_token, quote_token):
        self.replies.append((reply_token, quote_token))


class FakePos:
    """POS へ知らせた中身を覚える。fail を立てると送れない。"""

    def __init__(self) -> None:
        self.sent: list[tuple[str, list[dict]]] = []
        self.slip_ids: list[str] = []
        self.fail = False

    def send(self, visit_id: str, events: list[dict], slip_id: str = "") -> None:
        if self.fail:
            raise RuntimeError("POS に届かない")
        self.sent.append((visit_id, events))
        self.slip_ids.append(slip_id)
