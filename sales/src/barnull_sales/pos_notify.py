"""POS（福岡 Node）へ知らせる（30分ごとのジョブのステップ）。

伝票の保存（slip_ops.save）で、未収の残りが減ったら pos.outbox に積む。ここでまとめて POS に送り、送れたら sent_at を付ける。
送れなければ次の回にやり直す。[pos] callback_url が無い店舗（Bar null）では何もしない。
"""

from __future__ import annotations

from typing import Protocol

from .deps import Deps
from .errors import ItemErrors
from .slip_ops import save
from .slips import Slip, now_iso


class PosClient(Protocol):
    def send(self, visit_id: str, events: list[dict], slip_id: str = "") -> None: ...


class HttpPosClient:
    """POS の受け口に、この worker のサービスアカウントの ID トークンを付けて送る。"""

    def __init__(self, url: str, audience: str) -> None:
        self.url, self.audience = url, audience

    def send(self, visit_id: str, events: list[dict], slip_id: str = "") -> None:
        from .http import gcp_id_token, request

        request("POS", "POST", self.url, idempotent=True, timeout=15, attempts=2,
                headers={"Authorization": f"Bearer {gcp_id_token(self.audience)}"},
                json={"visit_id": visit_id, "slip_id": slip_id, "events": events})


def run(deps: Deps) -> int:
    if not deps.settings.pos_callback_url or not deps.pos:
        return 0
    errs = ItemErrors("pos_notify")
    n = 0
    for slip in deps.store.find_slips("pos.outbox_pending", "==", True):
        with errs.item(slip_id=slip.slip_id):
            pending = [e for e in slip.pos.get("outbox") or [] if not e.get("sent_at")]
            if pending:
                deps.pos.send(slip.pos["visit_id"], [{k: v for k, v in e.items() if k != "sent_at"} for e in pending],
                              slip.slip_id)
            sent = {e["at"] for e in pending}

            def fn(s: Slip, sent=sent) -> None:
                now = now_iso()
                for e in s.pos.get("outbox") or []:
                    if e["at"] in sent and not e.get("sent_at"):
                        e["sent_at"] = now
                s.pos["outbox_pending"] = any(not e.get("sent_at") for e in s.pos.get("outbox") or [])

            save(deps, slip, fn, "POS に知らせた", notify=False)
            n += len(pending)
    errs.raise_if_any()
    return n
