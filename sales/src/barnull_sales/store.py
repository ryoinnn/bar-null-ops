"""状態の正本（Firestore）と、テスト用の同じ振る舞いのメモリ版。

コレクション
- slips            伝票（ドキュメント ID = 伝票 ID）
- slip_keys        (イベント ID, 顧客の順番) → 伝票 ID。同じイベントを何度処理しても伝票は1件
- counters         伝票 ID の連番（営業日ごと）
- payment_claims   Square の決済 ID → 割り当てた伝票 ID（1つの決済を2つの伝票に使わない）
- deposit_claims   銀行明細 ID → 割り当て先（ref）。同じ ref からは何度でも使える（途中で失敗しても照合し直せる）
- line_events      受け取った LINE イベント（重複の検出と、解析結果の保存）
- orphan_payments / payouts / cash_reports / meta   そのほかの記録

書き込みの決まり
- 伝票は update_slip(slip, fn) だけで更新する。最新の内容を読み、fn で書き換えて、まるごと保存する（トランザクション）
- それ以外のドキュメントも set_doc でまるごと保存する（Firestore の merge は入れ子まで混ぜるため使わない）
"""

from __future__ import annotations

import copy
from collections.abc import Callable
from typing import Protocol

from .slips import Slip, now_iso, plain


class Store(Protocol):
    def create_slip(self, event_id: str, index: int, business_date: str,
                    make: Callable[[str], Slip]) -> tuple[Slip, bool]: ...
    def get_slip(self, slip_id: str) -> Slip | None: ...
    def update_slip(self, slip: Slip, fn: Callable[[Slip], None]) -> None: ...
    def find_slips(self, field: str, op: str, value) -> list[Slip]: ...
    def payment_claims(self, payment_ids: list[str]) -> dict[str, str]: ...
    def claim_payments(self, slip_id: str, payment_ids: list[str]) -> bool: ...
    def release_payments(self, slip_id: str, payment_ids: list[str]) -> None: ...
    def deposit_claims(self, txn_ids: list[str]) -> dict[str, str]: ...
    def claim_deposit(self, txn_id: str, ref: str) -> bool: ...
    def get_doc(self, collection: str, doc_id: str) -> dict | None: ...
    def set_doc(self, collection: str, doc_id: str, data: dict) -> None: ...
    def find_docs(self, collection: str, field: str, op: str, value) -> dict[str, dict]: ...
    def delete_docs_before(self, collection: str, field: str, before: str, limit: int = 400) -> int: ...


def _slip_key(event_id: str, index: int) -> str:
    return f"{event_id}_{index}"


def _get_path(d: dict, path: str):
    for part in path.split("."):
        if not isinstance(d, dict):
            return None
        d = d.get(part)
    return d


def _match(d: dict, field: str, op: str, value) -> bool:
    v = _get_path(d, field)
    if op == "==":
        return v == value
    if op == "in":
        return v in value
    if op == ">=":
        return v is not None and v >= value
    raise ValueError(op)


class MemoryStore:
    def __init__(self) -> None:
        self.docs: dict[str, dict[str, dict]] = {}

    def _col(self, name: str) -> dict[str, dict]:
        return self.docs.setdefault(name, {})

    def create_slip(self, event_id, index, business_date, make):
        keys = self._col("slip_keys")
        key = _slip_key(event_id, index)
        if key in keys:
            return Slip.from_dict(copy.deepcopy(self._col("slips")[keys[key]["slip_id"]])), False
        day = business_date.replace("-", "")
        n = self._col("counters").get(day, {}).get("n", 0) + 1
        slip = make(f"S-{day}-{n:03d}")
        self._col("counters")[day] = {"n": n}
        self._col("slips")[slip.slip_id] = copy.deepcopy(slip.to_dict())
        keys[key] = {"slip_id": slip.slip_id}
        return slip, True

    def get_slip(self, slip_id):
        d = self._col("slips").get(slip_id)
        return Slip.from_dict(copy.deepcopy(d)) if d else None

    def update_slip(self, slip, fn):
        fresh = self.get_slip(slip.slip_id) or Slip.from_dict(copy.deepcopy(slip.to_dict()))
        fn(fresh)
        self._col("slips")[slip.slip_id] = copy.deepcopy(fresh.to_dict())
        slip.__dict__.update(copy.deepcopy(fresh.__dict__))

    def find_slips(self, field, op, value):
        return [Slip.from_dict(copy.deepcopy(d)) for d in self._col("slips").values() if _match(d, field, op, value)]

    def payment_claims(self, payment_ids):
        col = self._col("payment_claims")
        return {p: col[p]["slip_id"] for p in payment_ids if p in col}

    def claim_payments(self, slip_id, payment_ids):
        col = self._col("payment_claims")
        if any(p in col and col[p]["slip_id"] != slip_id for p in payment_ids):
            return False
        for p in payment_ids:
            col[p] = {"slip_id": slip_id, "claimed_at": now_iso()}
        return True

    def release_payments(self, slip_id, payment_ids):
        col = self._col("payment_claims")
        for p in payment_ids:
            if col.get(p, {}).get("slip_id") == slip_id:
                del col[p]

    def deposit_claims(self, txn_ids):
        col = self._col("deposit_claims")
        return {t: col[t]["ref"] for t in txn_ids if t in col}

    def claim_deposit(self, txn_id, ref):
        col = self._col("deposit_claims")
        if txn_id in col and col[txn_id]["ref"] != ref:
            return False
        col[txn_id] = {"ref": ref, "claimed_at": now_iso()}
        return True

    def get_doc(self, collection, doc_id):
        d = self._col(collection).get(doc_id)
        return copy.deepcopy(d) if d is not None else None

    def set_doc(self, collection, doc_id, data):
        self._col(collection)[doc_id] = copy.deepcopy(data)

    def find_docs(self, collection, field, op, value):
        return {k: copy.deepcopy(v) for k, v in self._col(collection).items() if _match(v, field, op, value)}

    def delete_docs_before(self, collection, field, before, limit=400):
        old = [k for k, v in self._col(collection).items() if v.get(field) is not None and v[field] < before][:limit]
        for k in old:
            del self._col(collection)[k]
        return len(old)


class FirestoreStore:
    def __init__(self, project: str, client=None) -> None:
        from google.cloud import firestore

        self._fs = firestore
        self.db = client or firestore.Client(project=project)

    @staticmethod
    def _filter(field: str, op: str, value):
        from google.cloud.firestore_v1.base_query import FieldFilter

        return FieldFilter(field, op, plain(value))

    def _slip_ref(self, slip_id: str):
        return self.db.collection("slips").document(slip_id)

    def create_slip(self, event_id, index, business_date, make):
        key_ref = self.db.collection("slip_keys").document(_slip_key(event_id, index))
        day = business_date.replace("-", "")
        counter_ref = self.db.collection("counters").document(day)

        @self._fs.transactional
        def run(tx):
            key = key_ref.get(transaction=tx)
            if key.exists:
                snap = self._slip_ref(key.to_dict()["slip_id"]).get(transaction=tx)
                return Slip.from_dict(snap.to_dict()), False
            counter = counter_ref.get(transaction=tx)
            n = (counter.to_dict() or {}).get("n", 0) + 1 if counter.exists else 1
            slip = make(f"S-{day}-{n:03d}")
            tx.set(counter_ref, {"n": n})
            tx.set(self._slip_ref(slip.slip_id), {**slip.to_dict(), "updated_at": now_iso()})
            tx.set(key_ref, {"slip_id": slip.slip_id, "created_at": now_iso()})
            return slip, True

        return run(self.db.transaction())

    def get_slip(self, slip_id):
        snap = self._slip_ref(slip_id).get()
        return Slip.from_dict(snap.to_dict()) if snap.exists else None

    def update_slip(self, slip, fn):
        ref = self._slip_ref(slip.slip_id)

        @self._fs.transactional
        def run(tx):
            snap = ref.get(transaction=tx)
            fresh = Slip.from_dict(snap.to_dict()) if snap.exists else copy.deepcopy(slip)
            fn(fresh)   # 競合するとやり直されるので、fn は伝票を書き換えるだけにする
            tx.set(ref, {**fresh.to_dict(), "updated_at": now_iso()})
            return fresh

        fresh = run(self.db.transaction())
        slip.__dict__.update(fresh.__dict__)

    def find_slips(self, field, op, value):
        q = self.db.collection("slips").where(filter=self._filter(field, op, value))
        return [Slip.from_dict(s.to_dict()) for s in q.stream()]

    def payment_claims(self, payment_ids):
        if not payment_ids:
            return {}
        refs = [self.db.collection("payment_claims").document(p) for p in payment_ids]
        return {s.id: s.to_dict()["slip_id"] for s in self.db.get_all(refs) if s.exists}

    def claim_payments(self, slip_id, payment_ids):
        refs = [self.db.collection("payment_claims").document(p) for p in payment_ids]

        @self._fs.transactional
        def run(tx):
            snaps = [r.get(transaction=tx) for r in refs]
            if any(s.exists and s.to_dict().get("slip_id") != slip_id for s in snaps):
                return False
            for r in refs:
                tx.set(r, {"slip_id": slip_id, "claimed_at": now_iso()})
            return True

        return run(self.db.transaction())

    def release_payments(self, slip_id, payment_ids):
        for p in payment_ids:
            ref = self.db.collection("payment_claims").document(p)
            snap = ref.get()
            if snap.exists and snap.to_dict().get("slip_id") == slip_id:
                ref.delete()

    def deposit_claims(self, txn_ids):
        if not txn_ids:
            return {}
        refs = [self.db.collection("deposit_claims").document(t) for t in txn_ids]
        return {s.id: s.to_dict().get("ref", "") for s in self.db.get_all(refs) if s.exists}

    def claim_deposit(self, txn_id, ref):
        doc = self.db.collection("deposit_claims").document(txn_id)

        @self._fs.transactional
        def run(tx):
            snap = doc.get(transaction=tx)
            if snap.exists and snap.to_dict().get("ref") != ref:
                return False
            tx.set(doc, {"ref": ref, "claimed_at": now_iso()})
            return True

        return run(self.db.transaction())

    def get_doc(self, collection, doc_id):
        snap = self.db.collection(collection).document(doc_id).get()
        return snap.to_dict() if snap.exists else None

    def set_doc(self, collection, doc_id, data):
        self.db.collection(collection).document(doc_id).set(plain(data))

    def find_docs(self, collection, field, op, value):
        q = self.db.collection(collection).where(filter=self._filter(field, op, value))
        return {s.id: s.to_dict() for s in q.stream()}

    def delete_docs_before(self, collection, field, before, limit=400):
        n = 0
        for snap in self.db.collection(collection).where(filter=self._filter(field, "<", before)).limit(limit).stream():
            snap.reference.delete()
            n += 1
        return n
