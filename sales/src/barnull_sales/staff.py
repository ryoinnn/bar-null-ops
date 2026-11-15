"""スタッフ台帳（Asana「Bar null スタッフ台帳」の写しを Firestore の staff/{スタッフID} に持つ）。

- 管理者が Asana の台帳を直す → 30分ごとの sync が読み、Firestore に写す（時給が変わったら履歴に足す）
- 勤怠の送信者は LINE ID → 台帳、無ければ表示名を台帳の LINE名と照合する（合えば LINE ID を記録する）
- 伝票の帰属（本人の売上）: プロモーターフラグがあればその名前の人、無ければ伝票を送った人
- 時給・バック率は Firestore と非公開の Asana にだけ置く。ログ・Slack・git には出さない
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import asdict, dataclass, field
from datetime import date

from .slips import Slip, now_iso
from .texts import norm_person

log = logging.getLogger(__name__)
COLLECTION = "staff"
KINDS = ("スタッフ", "プロモーター", "管理者")
STAFF, PROMOTER, MANAGER = KINDS


@dataclass
class Staff:
    staff_id: str
    real_name: str = ""
    line_name: str = ""
    kind: str = ""                                    # スタッフ／プロモーター／管理者（空 ＝ 未設定）
    joined: str = ""                                  # 入社日 YYYY-MM-DD
    wages: list[dict] = field(default_factory=list)   # [{from: YYYY-MM-DD, wage: 円}]（from の早い順）
    wage_from_seen: str = ""                          # 前回読んだ Asana の「時給の適用開始日」（入れ直したかの判定用）
    back_rate: float | None = None                    # %（例: 10 → 10%）
    line_user_ids: list[str] = field(default_factory=list)
    asana: dict = field(default_factory=dict)         # task_gid, url
    asana_pending: bool = False                       # Asana の台帳タスクをまだ作れていない
    created_at: str = field(default_factory=now_iso)
    updated_at: str = ""

    @property
    def name(self) -> str:
        """表示に使う名前（本名。無ければ LINE名）。"""
        return self.real_name or self.line_name or "（名前未設定）"

    @property
    def is_manager(self) -> bool:
        return self.kind == MANAGER

    @property
    def is_promoter(self) -> bool:
        return self.kind == PROMOTER

    def wage_on(self, d: date | str) -> int | None:
        """その日に有効な時給。最初の履歴より前の日は、最初の時給。"""
        if not self.wages:
            return None
        key = d if isinstance(d, str) else d.isoformat()
        current = self.wages[0]["wage"]
        for w in self.wages:
            if (w.get("from") or "") <= key:
                current = w["wage"]
        return current

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> Staff:
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in d.items() if k in known})


def staff_id_for_line(user_id: str) -> str:
    """台帳に無い人を自動で足すときの ID（同じ LINE ID からは同じ ID。やり直しても二重にならない）。"""
    return "L" + hashlib.sha1(user_id.encode()).hexdigest()[:10]


def staff_id_for_task(task_gid: str) -> str:
    """管理者が Asana で作った台帳タスクの ID。"""
    return f"A{task_gid}"


class StaffIndex:
    """台帳の全員（数十人までの想定なので、まとめて読む）。"""

    def __init__(self, members: list[Staff]) -> None:
        self.members = {m.staff_id: m for m in members}

    @classmethod
    def load(cls, store) -> StaffIndex:
        return cls([Staff.from_dict(d) for d in store.find_docs(COLLECTION, "staff_id", ">=", "").values()])

    def get(self, staff_id: str | None) -> Staff | None:
        return self.members.get(staff_id or "")

    def by_line_user(self, user_id: str | None) -> Staff | None:
        if not user_id:
            return None
        return next((m for m in self.members.values() if user_id in m.line_user_ids), None)

    def by_name(self, name: str | None) -> Staff | None:
        """LINE名で照合する（空白・敬称を除く）。合わなければ本名。2人以上に合えば決めない（None）。"""
        key = norm_person(name)
        if not key:
            return None
        for attr in ("line_name", "real_name"):
            hits = [m for m in self.members.values() if norm_person(getattr(m, attr)) == key]
            if len(hits) == 1:
                return hits[0]
            if len(hits) > 1:
                return None
        return None

    def owner_of(self, slip: Slip) -> Staff | None:
        """伝票の売上が誰のものか（プロモーターフラグ → その人、無ければ送った人）。"""
        if slip.promoter_name:
            return self.by_name(slip.promoter_name)
        return self.by_line_user(slip.sender_line_user_id) or self.by_name(slip.sender_display_name)


def save(store, staff: Staff) -> None:
    staff.updated_at = now_iso()
    store.set_doc(COLLECTION, staff.staff_id, staff.to_dict())


def counts_for_sales(slip: Slip) -> bool:
    """本人の売上に数える伝票か（取消・POS（ほかの店舗）・金額や日付の無い伝票を除く）。"""
    return not slip.cancelled and slip.source == "line" and slip.slip_amount is not None and bool(slip.business_date)


def sales_by_owner(index: StaffIndex, slips: list[Slip]) -> dict[str, dict]:
    """{スタッフID: {"sales": 円, "count": 件数}}。持ち主の分からない伝票は数えない。"""
    out: dict[str, dict] = {}
    for s in slips:
        if not counts_for_sales(s):
            continue
        owner = index.owner_of(s)
        if not owner:
            continue
        t = out.setdefault(owner.staff_id, {"sales": 0, "count": 0})
        t["sales"] += s.slip_amount or 0
        t["count"] += 1
    return out


def slips_between(store, start: date, end: date) -> list[Slip]:
    """営業日が start〜end（両端を含む）の伝票。"""
    return [s for s in store.find_slips("business_date", ">=", start.isoformat())
            if s.business_date and s.business_date <= end.isoformat()]
