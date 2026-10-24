"""Asana から伝票の状態を作り直す（Firestore のデータを失ったとき用。運用手順書を参照）。

Asana のタスク（カスタムフィールドと説明欄）に、伝票の状態の写しがある。それを Firestore に書き戻す。
既にある伝票は上書きしない。Square の決済の割り当て（payment_claims）と伝票 ID の連番も戻す。
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

from .asana import FIELDS, AsanaAPI
from .slips import Alert, Slip, Status, Unpaid, derive

NAME_TO_KEY = {name: key for key, (name, _, _) in FIELDS.items()}


def _value(cf: dict):
    t = cf.get("resource_subtype") or cf.get("type")
    if t == "enum":
        return (cf.get("enum_value") or {}).get("name")
    if t == "number":
        return int(cf["number_value"]) if cf.get("number_value") is not None else None
    if t == "date":
        return (cf.get("date_value") or {}).get("date")
    return cf.get("text_value") or None


def _slack_from_permalink(url: str | None) -> dict:
    m = re.search(r"/archives/([A-Z0-9]+)/p(\d{10})(\d{6})", url or "")
    return {"channel_id": m[1], "thread_ts": f"{m[2]}.{m[3]}", "permalink": url} if m else {}


def task_to_slip(task: dict) -> Slip | None:
    f = {NAME_TO_KEY[c["name"]]: _value(c) for c in task.get("custom_fields", []) if c.get("name") in NAME_TO_KEY}
    if not f.get("slip_id") or f["slip_id"].startswith("RS-"):   # RS- はレンタルスペースの月次記録（伝票ではない）
        return None
    notes = task.get("notes") or ""
    raw = notes.split("LINE の原文:\n", 1)[1].split("\n\n処理履歴:", 1)[0] if "LINE の原文:\n" in notes else ""
    payment_ids = [p.strip() for p in (f.get("payment_id") or "").split(",") if p.strip()]
    unpaid_remaining = f.get("unpaid_remaining") or 0
    created = task.get("created_at") or datetime.now(UTC).isoformat()
    slip = Slip(
        slip_id=f["slip_id"], event_id=f"restored-{task['gid']}", entry_index=0, group_id="", message_id="",
        sender_line_user_id=None, sender_display_name=f.get("sender"), sent_at=created, raw_text=raw,
        business_date=f.get("business_date"), business_date_source="message" if f.get("business_date") else "inferred",
        customer_name=f.get("customer"), slip_amount=f.get("slip_amount"), note="",
        unpaid_amount=unpaid_remaining, unpaid_remaining=unpaid_remaining,
        unpaid_status=f.get("unpaid") or Unpaid.NONE,
        expected_square_amount=(f["slip_amount"] - unpaid_remaining) if f.get("slip_amount") is not None else None,
        status=f.get("status") or Status.DRAFTED,
        square={"payment_ids": payment_ids, "amount": f.get("square_amount"), "fee": f.get("fee") or 0,
                "summary": "Asana から復元"} if payment_ids else {},
        deposits={"completed_at": f["deposit_date"], "restored": True} if f.get("deposit_date") else {},
        closed_at=task.get("completed_at") if task.get("completed") else None,
        slack=_slack_from_permalink(f.get("slack_url")),
        asana={"task_gid": task["gid"], "url": task.get("permalink_url", "")},
        dry_run=False, created_at=created,
    )
    if not slip.closed_at:
        # Asana には支払手段・決済日時・元の未収額・アラートが無いので、完了していない伝票は人に確かめてもらう
        slip.alerts.append({"type": Alert.RESTORED, "message": "Asana から復元した伝票です。決済・入金・未収を確かめ、"
                            "Asana の「手動上書き」で状態を決めてください", "at": created, "resolved_at": None,
                            "posted": False, "important": False})
        slip.sync_pending = True     # 30分ごとのジョブが Slack・Asana に反映し、アラートを出す
    derive(slip)
    return slip


def restore(api: AsanaAPI, project_gid: str, db, dry_run: bool) -> dict:
    """db は google.cloud.firestore.Client。"""
    tasks = api.list("/tasks", {
        "project": project_gid,
        "opt_fields": "name,notes,completed,completed_at,created_at,permalink_url,custom_fields.name,"
                      "custom_fields.resource_subtype,custom_fields.type,custom_fields.text_value,"
                      "custom_fields.number_value,custom_fields.date_value,custom_fields.enum_value.name"})
    stats = {"tasks": len(tasks), "restored": 0, "skipped_existing": 0, "no_slip_id": 0}
    counters: dict[str, int] = {}
    for t in tasks:
        slip = task_to_slip(t)
        if not slip:
            stats["no_slip_id"] += 1
            continue
        day, n = slip.slip_id.split("-")[1], int(slip.slip_id.split("-")[2])
        counters[day] = max(counters.get(day, 0), n)
        ref = db.collection("slips").document(slip.slip_id)
        if ref.get().exists:
            stats["skipped_existing"] += 1
            continue
        stats["restored"] += 1
        print(("[dry-run] " if dry_run else "") + f"復元: {slip.slip_id} {slip.status} {t['name']}")
        if dry_run:
            continue
        ref.set(slip.to_dict())
        for pid in slip.square.get("payment_ids", []):
            db.collection("payment_claims").document(pid).set({"slip_id": slip.slip_id, "restored": True})
    if not dry_run:
        for day, n in counters.items():
            ref = db.collection("counters").document(day)
            snap = ref.get()
            if not snap.exists or snap.to_dict().get("n", 0) < n:
                ref.set({"n": n})
    return stats
