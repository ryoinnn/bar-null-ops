"""売上（伝票）・台帳の変化を、勤怠の売上・時給の額と給与に伝える。

- 伝票の保存（slip_ops.save）と起票のたびに、「誰の・どの日の売上か」と金額が変わったかを見る（sales_key）。
  変わったら、その前後の人・日の勤怠を直し、給与が計算済みの月なら計算し直す（確定後は知らせるだけ）。定期的な見回りはしない
- 30分ごとの staff_sync が Asana の台帳を読み、Firestore に写す（時給が変わったら履歴に足し、影響する記録を直す）
どれも失敗しても伝票の処理は止めない（ログに残す）。
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

from . import staff as staff_mod
from .errors import ItemErrors
from .staff import Staff, StaffIndex

log = logging.getLogger(__name__)


def sales_key(slip) -> dict:
    """本人の売上を決める事実（これが変わったら勤怠・給与を直す）。"""
    counts = staff_mod.counts_for_sales(slip)
    return {"date": slip.business_date, "amount": (slip.slip_amount or 0) if counts else 0, "counts": counts,
            "promoter": slip.promoter_name or "", "sender_id": slip.sender_line_user_id,
            "sender_name": slip.sender_display_name}


def _owner(index: StaffIndex, key: dict) -> Staff | None:
    if key["promoter"]:
        return index.by_name(key["promoter"])
    return index.by_line_user(key["sender_id"]) or index.by_name(key["sender_name"])


def on_slip_changed(deps, before: dict | None, slip) -> None:
    if not deps.settings.labor.enabled:
        return
    after = sales_key(slip)
    if not after["counts"] and not (before and before["counts"]):
        return
    from . import attendance, payroll

    try:
        index = StaffIndex.load(deps.store)
        targets = set()
        for key in (before, after):
            if key and key["date"]:
                owner = _owner(index, key)
                if owner:
                    targets.add((owner.staff_id, key["date"]))
        for staff_id, d in sorted(targets):
            attendance.refresh(deps, staff_id, d)
            payroll.on_change(deps, staff_id, d[:7], "売上の変化")
    except Exception:
        log.exception("売上の変化を勤怠・給与に反映できなかった", extra={"slip_id": slip.slip_id})


# ---------------------------------------------------------------- 台帳
def staff_sync(deps) -> int:
    """30分ごと: Asana の台帳を Firestore に写す。管理者が作ったタスクにはスタッフID を書き戻す。"""
    if not deps.labor:
        return 0
    from . import attendance, payroll

    index = StaffIndex.load(deps.store)
    by_gid = {m.asana.get("task_gid"): m for m in index.members.values() if m.asana.get("task_gid")}
    today = attendance.cycle_date(datetime.now(deps.settings.tz), deps)
    errs = ItemErrors("staff_sync")
    n = 0
    for t in deps.labor.list_staff():
        with errs.item(task_gid=t["gid"]):
            sid = t.get("staff_id") or ""
            s = index.get(sid) or by_gid.get(t["gid"]) or Staff(staff_id=sid or staff_mod.staff_id_for_task(t["gid"]))
            before = s.to_dict()
            s.asana = {**s.asana, "task_gid": t["gid"]}
            s.real_name = (t.get("real_name") or "").strip()
            s.line_name = (t.get("line_name") or "").strip() or s.line_name
            s.kind = t.get("staff_kind") or ""
            s.joined = t.get("joined") or ""
            s.back_rate = float(t["back_rate"]) if t.get("back_rate") is not None else None
            _apply_wage(s, t.get("wage"), t.get("wage_from"), today)
            if not t.get("staff_id"):
                deps.labor.upsert_staff(t["gid"], s.name, {"staff_id": s.staff_id,
                                                           "line_ids": ", ".join(s.line_user_ids)})
            after = s.to_dict()
            keys = ("real_name", "line_name", "kind", "joined", "back_rate", "wages", "asana")
            if any(before.get(k) != after.get(k) for k in keys):
                staff_mod.save(deps.store, s)
                index.members[s.staff_id] = s
                n += 1
                if any(before.get(k) != after.get(k) for k in ("real_name", "line_name", "kind", "back_rate", "wages")):
                    _ripple(deps, s, today, attendance, payroll)
    errs.raise_if_any()
    return n


def _apply_wage(s: Staff, wage, wage_from: str | None, today: date) -> None:
    """時給の欄を履歴に足す。適用開始日が無ければ、最初の時給は過去すべてに、変更は今日から当てる。"""
    if wage is None:
        return
    w = int(wage)
    frm = wage_from or ("" if not s.wages else today.isoformat())
    same_from = next((x for x in s.wages if x.get("from") == frm), None)
    if s.wages and s.wages[-1]["wage"] == w and (not wage_from or s.wages[-1].get("from") == wage_from):
        return
    if same_from:
        same_from["wage"] = w
    else:
        s.wages.append({"from": frm, "wage": w})
    s.wages.sort(key=lambda x: x.get("from") or "")


def _ripple(deps, s: Staff, today: date, attendance, payroll) -> None:
    """台帳の変更（名前・区分・時給・バック率）を、今月と前月の勤怠と、前月の給与に反映する。"""
    months = {today.isoformat()[:7], (today.replace(day=1) - timedelta(days=1)).isoformat()[:7]}
    for m in sorted(months):
        for rec in attendance.records_of(deps, s.staff_id, m):
            if not attendance.refresh(deps, s.staff_id, rec["business_date"]):
                attendance.sync(deps, rec, s)      # 名前だけが変わったとき
        payroll.on_change(deps, s.staff_id, m, "台帳の変更")
