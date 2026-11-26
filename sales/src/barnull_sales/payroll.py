"""給与（月末締め・翌月25日払い。docs/profit/labor_requirements.md §5）。

給与 ＝ Σ(勤務時間 × その日の時給) ＋ 前月の本人の売上 × バック率 ＋ 調整額（円未満切り捨て。プロモーターは時給の分が 0）

| いつ                         | 何をする                                   |
|-----------------------------|-------------------------------------------|
| 計算日の前                     | 売上・勤怠・台帳が変わるたびに、その人の「見込み」を更新（その日時点の給与） |
| 翌月の第1水曜 17:00 以降の最初の回 | 前月分を全員計算（計算済み）                   |
| 計算済み〜10日 9:00            | 売上・勤怠・台帳が変わったら、その人だけ計算し直す（on_change。見回りはしない） |
| 10日 9:00 以降の最初の回        | 確定（以後は自動で計算し直さず、変化を Slack で知らせるだけ） |
| いつでも                       | 管理者が Asana の「調整額」「調整の理由」を入れたら取り込む |
30分ごとのジョブ（step・adjustments）が動かす。給与の額は Slack・ログに出さない（非公開の Asana だけ）。
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta

from . import attendance
from . import staff as staff_mod
from .errors import ItemErrors
from .slips import now_iso
from .staff import Staff, StaffIndex
from .texts import esc

log = logging.getLogger(__name__)
COLLECTION = "payroll"
RUNS = "payroll_runs"
ESTIMATE, CALCULATED, CONFIRMED = "見込み", "計算済み", "確定"   # 見込み: 計算日より前（その日時点の給与）


def _now(deps) -> datetime:
    return datetime.now(deps.settings.tz)


def _hm(s: str) -> time:
    h, m = (int(x) for x in s.split(":"))
    return time(h, m)


def first_weekday(year: int, month: int, weekday: int) -> date:
    d = date(year, month, 1)
    return d + timedelta(days=(weekday - d.weekday()) % 7)


def prev_month(d: date) -> str:
    return (d.replace(day=1) - timedelta(days=1)).isoformat()[:7]


def month_range(month: str) -> tuple[date, date]:
    y, m = (int(x) for x in month.split("-"))
    start = date(y, m, 1)
    end = (date(y + (m == 12), m % 12 + 1, 1)) - timedelta(days=1)
    return start, end


def run_times(deps, month: str) -> tuple[datetime, datetime]:
    """その月（対象月）の給与を計算する日時と、確定する日時（翌月）。"""
    cfg, tz = deps.settings.labor, deps.settings.tz
    nxt = month_range(month)[1] + timedelta(days=1)
    calc = datetime.combine(first_weekday(nxt.year, nxt.month, cfg.payroll_weekday), _hm(cfg.payroll_time), tzinfo=tz)
    confirm = datetime.combine(nxt.replace(day=cfg.confirm_day), _hm(cfg.confirm_time), tzinfo=tz)
    return calc, confirm


def _run(deps, month: str) -> dict:
    return deps.store.get_doc(RUNS, month) or {}


def is_calculated(deps, month: str) -> bool:
    return bool(_run(deps, month).get("calculated_at"))


def is_confirmed(deps, month: str) -> bool:
    return bool(_run(deps, month).get("confirmed_at"))


# ---------------------------------------------------------------- 計算
def compute(deps, month: str, person: Staff, index: StaffIndex, slips=None) -> dict:
    """その人のその月の給与（保存はしない）。"""
    start, end = month_range(month)
    recs = [r for r in attendance.records_of(deps, person.staff_id, month) if r.get("status") == attendance.OK]
    hours = round(sum(r.get("hours") or 0 for r in recs), 2)
    wage_part = 0
    if not person.is_promoter:
        for r in recs:
            w = person.wage_on(r["business_date"])
            wage_part += int((r.get("hours") or 0) * w) if w is not None else 0
    slips = staff_mod.slips_between(deps.store, start, end) if slips is None else slips
    t = staff_mod.sales_by_owner(index, slips).get(person.staff_id, {})
    sales = t.get("sales", 0)
    rate = person.back_rate
    back = int(sales * rate / 100) if rate else 0
    missing = []
    if not person.kind:
        missing.append("区分")
    if not person.is_promoter and recs and person.wage_on(start) is None:
        missing.append("時給")
    return {"month": month, "staff_id": person.staff_id, "staff_name": person.name, "staff_kind": person.kind,
            "days": len(recs), "hours": hours, "own_sales": sales, "count": t.get("count", 0),
            "wage_part": wage_part, "back_rate": rate, "back": back, "missing": missing}


def targets(deps, month: str, index: StaffIndex, slips) -> list[Staff]:
    """計算する人: 管理者を除き、区分がスタッフ・プロモーターの人と、その月に勤怠か売上がある人。"""
    sold = set(staff_mod.sales_by_owner(index, slips))
    out = []
    for p in index.members.values():
        if p.is_manager:
            continue
        if p.kind or p.staff_id in sold or attendance.month_summary(deps, p.staff_id, month)[0]:
            out.append(p)
    return sorted(out, key=lambda p: (p.kind != staff_mod.STAFF, p.name))


def _doc_id(month: str, staff_id: str) -> str:
    return f"{month}_{staff_id}"


NUMBERS = ("days", "hours", "own_sales", "count", "wage_part", "back_rate", "back", "staff_name", "staff_kind")


def upsert(deps, month: str, person: Staff, index: StaffIndex, reason: str, slips=None,
           status: str = CALCULATED) -> bool:
    """計算して保存し、Asana に反映する。数字も状態も変わらなければ何もしない（False）。"""
    new = compute(deps, month, person, index, slips)
    doc = deps.store.get_doc(COLLECTION, _doc_id(month, person.staff_id))
    if doc and all(doc.get(k) == new[k] for k in NUMBERS) and doc.get("status") == status:
        return False
    doc = doc or {"adjustment": 0, "adjust_reason": "", "history": [], "asana": {},
                  "created_at": now_iso(), "dry_run": deps.settings.labor.dry_run}
    doc.update(new)
    doc["status"] = status
    doc["pay"] = doc["wage_part"] + doc["back"] + int(doc.get("adjustment") or 0)
    doc["history"].append({"at": now_iso(), "reason": reason, "pay": doc["pay"]})
    doc["updated_at"] = now_iso()
    doc["sync_pending"] = True
    deps.store.set_doc(COLLECTION, _doc_id(month, person.staff_id), doc)
    sync(deps, doc)
    return True


def sync(deps, doc: dict, *, raise_errors: bool = False) -> None:
    if not deps.labor:
        return
    values = {k: doc.get(k) for k in ("month", "staff_name", "staff_kind", "days", "hours", "own_sales", "count",
                                      "wage_part", "back_rate", "back", "pay", "staff_id")}
    values["pay_status"] = doc.get("status")
    try:
        gid = (doc.get("asana") or {}).get("task_gid")
        new_gid, url = deps.labor.upsert_payroll(gid, f"{doc['staff_name']} {doc['month']} 給与", values)
        if not gid:
            doc["asana"] = {"task_gid": new_gid, "url": url}
        doc["sync_pending"] = False
    except Exception:
        log.exception("給与の Asana への反映に失敗（30分ごとにやり直す）", extra={"payroll": _doc_id(doc["month"], doc["staff_id"])})
        doc["sync_pending"] = True
        if raise_errors:
            deps.store.set_doc(COLLECTION, _doc_id(doc["month"], doc["staff_id"]), doc)
            raise
    deps.store.set_doc(COLLECTION, _doc_id(doc["month"], doc["staff_id"]), doc)


def calculate_all(deps, month: str) -> int:
    index = StaffIndex.load(deps.store)
    start, end = month_range(month)
    slips = staff_mod.slips_between(deps.store, start, end)
    people = targets(deps, month, index, slips)
    errs = ItemErrors("payroll")
    missing = []
    for p in people:
        with errs.item(staff_id=p.staff_id):
            upsert(deps, month, p, index, "計算", slips)
            m = compute(deps, month, p, index, slips)["missing"]
            if m:
                missing.append(f"{esc(p.name)}（{'・'.join(m)}）")
    y, mo = month.split("-")
    confirm_day = run_times(deps, month)[1].day
    note = ("\n条件が未設定の人：" + "、".join(missing) + "。台帳に入れると計算し直します。") if missing else ""
    new, fix = attendance.waiting_in(deps, month)
    if new or fix:   # 承認されていない勤怠は給与に入らない（承認すれば、確定までは計算し直す）
        note += ("\n" + (f"承認待ちの勤怠が{new}件あり、給与に入っていません。" if new else "")
                 + (f"承認待ちの訂正が{fix}件あり、前の記録のまま計算しています。" if fix else "")
                 + f"{confirm_day}日 {deps.settings.labor.confirm_time} の確定までに承認すれば、計算し直します。")
    deps.notifier.post(deps.settings.labor_channel,
                       f"{deps.settings.mention}{int(mo)}月分の給与を計算しました（{len(people)}人）。"
                       f"Asana の「Bar null 給与」で確認してください。{confirm_day}日 "
                       f"{deps.settings.labor.confirm_time} に確定します。{note}"
                       + ("\n（テスト）" if deps.settings.labor.dry_run else ""))
    errs.raise_if_any()
    return len(people)


def confirm_all(deps, month: str) -> int:
    docs = {k: v for k, v in deps.store.find_docs(COLLECTION, "month", "==", month).items()}
    for doc in docs.values():
        doc["status"] = CONFIRMED
        doc["history"].append({"at": now_iso(), "reason": "確定", "pay": doc.get("pay")})
        sync(deps, doc)
    deps.notifier.post(deps.settings.labor_channel,
                       f"{int(month[5:])}月分の給与を確定しました（{len(docs)}人）。以後の変更は管理者が Asana の調整額で行います。"
                       + ("\n（テスト）" if deps.settings.labor.dry_run else ""))
    return len(docs)


def step(deps) -> dict:
    """30分ごと: 前月分の計算・確定の時刻を過ぎていれば、まだなら1回だけ動かす。"""
    if not deps.settings.labor.enabled:
        return {}
    now = _now(deps)
    month = prev_month(now.date())
    since = deps.settings.labor.payroll_since
    if since and month < since:
        return {"month": month, "skipped": "payroll_since より前"}
    calc_at, confirm_at = run_times(deps, month)
    run = _run(deps, month)
    done = {}
    if not run.get("calculated_at") and now >= calc_at:
        done["calculated"] = calculate_all(deps, month)
        run["calculated_at"] = now_iso()
        deps.store.set_doc(RUNS, month, run)
    if run.get("calculated_at") and not run.get("confirmed_at") and now >= confirm_at:
        done["confirmed"] = confirm_all(deps, month)
        run["confirmed_at"] = now_iso()
        deps.store.set_doc(RUNS, month, run)
    return {"month": month, **done}


def on_change(deps, staff_id: str, month: str, reason: str) -> None:
    """売上・勤怠・台帳が変わったとき（保存のたびに呼ばれる）。計算済みなら計算し直し、確定後なら知らせるだけ。"""
    if not deps.settings.labor.enabled:
        return
    since = deps.settings.labor.payroll_since
    if since and month < since:
        return
    index = StaffIndex.load(deps.store)
    person = index.get(staff_id)
    if not person or person.is_manager:
        return
    if not is_calculated(deps, month):     # 計算日の前: その日時点の見込みを更新するだけ（知らせない）
        upsert(deps, month, person, index, reason, status=ESTIMATE)
        return
    if is_confirmed(deps, month):
        new = compute(deps, month, person, index)
        doc = deps.store.get_doc(COLLECTION, _doc_id(month, staff_id)) or {}
        if all(doc.get(k) == new[k] for k in ("days", "hours", "own_sales", "count", "wage_part", "back")):
            return
        deps.notifier.post(deps.settings.labor_channel,
                           f"{deps.settings.mention}【確定後の変更】{esc(person.name)}さんの{int(month[5:])}月の"
                           f"{'売上' if reason == '売上の変化' else '勤怠・条件'}が、給与の確定後に変わりました（{reason}）。"
                           "給与は自動では計算し直していません。必要なら Asana の給与の「調整額」で直してください。")
        return
    if upsert(deps, month, person, index, reason, status=CALCULATED):
        deps.notifier.post(deps.settings.labor_channel,
                           f"{esc(person.name)}さんの{int(month[5:])}月分の給与を計算し直しました（{reason}）。")


def adjustments(deps) -> int:
    """30分ごと: 管理者が Asana の給与タスクに入れた「調整額」「調整の理由」を取り込む。やり直しの反映もここで行う。"""
    if not deps.labor:
        return 0
    meta = deps.store.get_doc("meta", "payroll_adjustments") or {}
    started = datetime.fromisoformat(now_iso())
    errs = ItemErrors("payroll_adjustments")
    n = 0
    for t in deps.labor.read_payroll_adjustments(meta.get("last")):
        with errs.item(task_gid=t["gid"]):
            found = deps.store.find_docs(COLLECTION, "asana.task_gid", "==", t["gid"])
            if not found:
                continue
            doc = next(iter(found.values()))
            adj, why = int(t.get("adjustment") or 0), t.get("adjust_reason") or ""
            if adj == int(doc.get("adjustment") or 0) and why == doc.get("adjust_reason", ""):
                continue
            doc.update(adjustment=adj, adjust_reason=why, updated_at=now_iso())
            doc["pay"] = doc["wage_part"] + doc["back"] + adj
            doc["history"].append({"at": now_iso(), "reason": "調整額（Asana）", "pay": doc["pay"]})
            sync(deps, doc)
            deps.notifier.post(deps.settings.labor_channel,
                               f"{esc(doc['staff_name'])}さんの{int(doc['month'][5:])}月分の給与に、調整額を反映しました"
                               f"（理由：{esc(why) or 'なし'}）。")
            n += 1
    for key, doc in deps.store.find_docs(COLLECTION, "sync_pending", "==", True).items():
        with errs.item(payroll=key):
            sync(deps, doc, raise_errors=True)
    if not errs.errors:
        deps.store.set_doc("meta", "payroll_adjustments", {"last": (started - timedelta(minutes=5)).isoformat()})
    errs.raise_if_any()
    return n
