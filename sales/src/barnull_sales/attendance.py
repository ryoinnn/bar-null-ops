"""勤怠（LINE の @null 勤怠メッセージ → Firestore attendance/{記録ID} → Slack・Asana）。

- 1件 ＝ 1人 × 1営業日。記録 ID は W-{YYYYMMDD}-{スタッフID}。同じ人・同じ日の2回目は上書き（訂正）
- 勤怠サイクルは 21:00〜翌9:00（[labor]）。日付はサイクルの開始日。0:00〜8:59 と 24時以降の書き方は翌日の時刻
- 期限（営業日 D は D+2 日の 9:00 まで）を過ぎた・未来の勤怠は、Slack で桝本の承認を待つ（approvals）
- 保存してから Slack・Asana に反映する。反映に失敗したら sync_pending を立て、30分ごとの resync がやり直す
- 時給の額・人件費はログと Slack に出さない（Firestore と非公開の Asana だけ）
"""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Callable
from datetime import date, datetime, time, timedelta

from . import bizdate
from . import staff as staff_mod
from .deps import Deps
from .errors import ItemErrors
from .extract import Parsed
from .slips import now_iso
from .staff import Staff, StaffIndex
from .texts import esc, quote, who

log = logging.getLogger(__name__)
COLLECTION = "attendance"
PENDING = "attendance_pending"
OK, VOID = "記録済み", "対象外"


# ---------------------------------------------------------------- 日付・時刻
def _tz(deps: Deps):
    return deps.settings.tz


def cycle_date(dt: datetime, deps: Deps) -> date:
    """その時刻が属する（または直前に終わった）勤怠サイクルの日付。例: 11/24 03:00 → 11/23、11/24 10:00 → 11/23。"""
    start = deps.settings.labor.cycle_start_time
    local = dt.astimezone(_tz(deps))
    return (local - timedelta(hours=start.hour, minutes=start.minute)).date()


def resolve_date(month: int | None, day: int | None, sent_at: datetime, deps: Deps) -> date | None:
    """年の無い月日を、送信時刻に最も近い日付にする（未来の日付も分かるように、前後の年から選ぶ）。"""
    ref = cycle_date(sent_at, deps)
    if not (month and day):
        return ref
    cands = []
    for y in (ref.year - 1, ref.year, ref.year + 1):
        try:
            cands.append(date(y, month, day))
        except ValueError:
            pass
    return min(cands, key=lambda d: abs((d - ref).days)) if cands else None


HM = re.compile(r"^\s*(\d{1,2})\s*[:：]\s*(\d{2})\s*$")


def at_time(d: date, hm: str, deps: Deps) -> datetime | None:
    """営業日 d の「HH:MM」を日時にする。24時以降・サイクルの終わり（9:00）より前は翌日。"""
    m = HM.match(hm or "")
    if not m:
        return None
    h, mi = int(m[1]), int(m[2])
    if mi >= 60 or h >= 48:
        return None
    day = d
    if h >= 24:
        day, h = d + timedelta(days=1), h - 24
    elif time(h, mi) < deps.settings.labor.cycle_end_time:
        day = d + timedelta(days=1)
    return datetime.combine(day, time(h, mi), tzinfo=_tz(deps))


def parse_iso_or_hm(value: str, d: date, deps: Deps) -> datetime | None:
    """Asana で管理者が直した時刻（ISO でも「HH:MM」でもよい）。"""
    v = (value or "").strip()
    if not v:
        return None
    try:
        dt = datetime.fromisoformat(v)
        return dt if dt.tzinfo else dt.replace(tzinfo=_tz(deps))
    except ValueError:
        return at_time(d, v, deps)


def span(d: date, start_hm: str, end_hm: str, deps: Deps) -> tuple[datetime, datetime] | str:
    """(開始, 終了) か、読めない理由。"""
    start, end = at_time(d, start_hm, deps), at_time(d, end_hm, deps)
    if not start or not end:
        return "開始・終了の時刻が読み取れない"
    return check_span(start, end, deps)


def check_span(start: datetime, end: datetime, deps: Deps) -> tuple[datetime, datetime] | str:
    if end <= start:
        end += timedelta(days=1)
    hours = (end - start).total_seconds() / 3600
    if hours <= 0 or hours > deps.settings.labor.max_shift_hours:
        return f"勤務時間が {hours:.1f} 時間になる（0〜{deps.settings.labor.max_shift_hours:g} 時間の範囲外）"
    return start, end


def deadline(d: date, deps: Deps) -> datetime:
    cfg = deps.settings.labor
    return datetime.combine(d + timedelta(days=1 + cfg.deadline_extra_days), cfg.cycle_end_time, tzinfo=_tz(deps))


def hours_of(start: datetime, end: datetime) -> float:
    return round((end - start).total_seconds() / 3600, 2)


def fmt_h(h: float | None) -> str:
    return "—" if h is None else (f"{h:.2f}".rstrip("0").rstrip(".") + "h")


def hm(iso: str, deps: Deps) -> str:
    return datetime.fromisoformat(iso).astimezone(_tz(deps)).strftime("%H:%M")


def month_of(d: date | str) -> str:
    return (d if isinstance(d, str) else d.isoformat())[:7]


def record_id(d: date, staff_id: str) -> str:
    return f"W-{d:%Y%m%d}-{staff_id}"


# ---------------------------------------------------------------- 本人の特定
def resolve_sender(deps: Deps, index: StaffIndex, user_id: str | None, display_name: str | None) -> Staff | None:
    """LINE ID → 台帳。無ければ表示名を LINE名と照合（合えば LINE ID を記録）。どちらも無ければ台帳に仮の人を足す。"""
    if not user_id:
        return None
    found = index.by_line_user(user_id)
    if found:
        return found
    found = index.by_name(display_name)
    if found:
        found.line_user_ids.append(user_id)
        found.asana_pending = True          # 台帳の「LINE ID」を書く
        staff_mod.save(deps.store, found)
        publish_staff(deps, found)
        return found
    sid = staff_mod.staff_id_for_line(user_id)
    existing = deps.store.get_doc(staff_mod.COLLECTION, sid)
    if existing:                            # 前の試行で作った
        return Staff.from_dict(existing)
    new = Staff(staff_id=sid, line_name=display_name or "", line_user_ids=[user_id], asana_pending=True)
    staff_mod.save(deps.store, new)
    index.members[sid] = new
    deps.notifier.post(deps.settings.labor_channel,
                       f"{deps.settings.mention}【台帳に無い人】LINE「{who(display_name)}」さんの勤怠を受け付けました。"
                       "スタッフ台帳に仮のタスクを作ったので、本名・区分・時給・バック率を入れてください。")
    publish_staff(deps, new)
    return new


def publish_staff(deps: Deps, s: Staff) -> None:
    """台帳の Asana タスクを作る／システムの欄（スタッフID・LINE ID）だけを書く。失敗したら次回やり直す。"""
    tracker = deps.labor
    if not tracker:
        return
    try:
        values = {"staff_id": s.staff_id, "line_ids": ", ".join(s.line_user_ids)}
        if s.asana.get("task_gid"):
            tracker.upsert_staff(s.asana["task_gid"], s.name, values)
        else:
            gid, url = tracker.upsert_staff(None, s.line_name or s.name, {**values, "line_name": s.line_name})
            s.asana = {"task_gid": gid, "url": url}
        s.asana_pending = False
    except Exception:
        log.exception("台帳の Asana への反映に失敗（30分ごとにやり直す）", extra={"staff_id": s.staff_id})
        s.asana_pending = True
    staff_mod.save(deps.store, s)


# ---------------------------------------------------------------- LINE のメッセージ
def _notice(deps: Deps, title: str, sender: str | None, text: str, mention: bool = False) -> None:
    deps.notifier.post(deps.settings.labor_channel,
                       f"{deps.settings.mention if mention else ''}【勤怠：{title}】送信者：{who(sender)}（LINE）\n{quote(text)}")


def handle(deps: Deps, msg, parsed: Parsed, on_accepted: Callable[[], None]) -> dict:
    """messages.handle から呼ばれる（kind == "attendance"）。"""
    if not deps.settings.labor.enabled:
        return {"status": "labor_disabled"}
    a = parsed.attendance or {}
    d = resolve_date(a.get("month"), a.get("day"), msg.sent_at, deps)
    if d is None:
        _notice(deps, "日付が正しくない", msg.sender, msg.text)
        return {"status": "attendance_unparsed"}
    sp = span(d, a.get("start") or "", a.get("end") or "", deps)
    if isinstance(sp, str):
        _notice(deps, sp, msg.sender, msg.text)
        return {"status": "attendance_unparsed"}
    start, end = sp
    index = StaffIndex.load(deps.store)
    person = resolve_sender(deps, index, msg.author_id, msg.author)   # 引用して送り直したときは引用元の人
    if not person:
        _notice(deps, "送信者が分からない（LINE のプロフィール未同意）", msg.sender, msg.text)
        return {"status": "attendance_unknown_sender"}
    rid = record_id(d, person.staff_id)
    rec = deps.store.get_doc(COLLECTION, rid)
    if rec and msg.event_id in rec.get("event_ids", []):        # 再試行: 記録済み
        on_accepted()
        return {"status": "attendance", "record_id": rid}
    from . import payroll

    if payroll.is_confirmed(deps, month_of(d)):
        _notice(deps, f"{d.month}月の給与は確定済みのため記録しませんでした。直すときは管理者が Asana で直してください",
                msg.sender, msg.text, mention=True)
        return {"status": "attendance_month_closed", "record_id": rid}
    late = msg.sent_at > deadline(d, deps)
    future = start > msg.sent_at
    if late or future:
        return request_approval(deps, msg, person, d, start, end, "未来の日付" if future else "期限切れ")
    rec = save_record(deps, person, d, start, end, event_id=msg.event_id, line_user_id=msg.author_id,
                      display_name=msg.author, reason="LINE" if not msg.origin_user_id else f"LINE（{msg.sender}さんが引用して送り直し）",
                      text=msg.text)
    on_accepted()
    return {"status": "attendance", "record_id": rec["record_id"]}


# ---------------------------------------------------------------- 記録
def save_record(deps: Deps, person: Staff, d: date, start: datetime, end: datetime, *, event_id: str,
                line_user_id: str | None, display_name: str | None, reason: str, text: str = "") -> dict:
    """記録を作る・上書きする（同じイベントは二度当てない）。保存してから Slack・Asana に反映する。"""
    rid = record_id(d, person.staff_id)
    rec = deps.store.get_doc(COLLECTION, rid)
    before = None
    if rec:
        if event_id in rec.get("event_ids", []):
            return rec
        before = {k: rec.get(k) for k in ("start", "end", "hours", "status")}
    else:
        rec = {"record_id": rid, "staff_id": person.staff_id, "business_date": d.isoformat(), "event_ids": [],
               "history": [], "slack": {}, "asana": {}, "created_at": now_iso(), "dry_run": deps.settings.labor.dry_run}
    rec.update(start=start.isoformat(), end=end.isoformat(), hours=hours_of(start, end), status=OK,
               line_user_id=line_user_id or rec.get("line_user_id"), display_name=display_name or rec.get("display_name"),
               text=text or rec.get("text", ""), updated_at=now_iso(), sync_pending=True)
    rec["event_ids"].append(event_id)
    rec["history"].append({"at": now_iso(), "event_id": event_id, "reason": reason, "before": before})
    _fill_derived(deps, rec, person)
    deps.store.set_doc(COLLECTION, rid, rec)
    log.info("勤怠を保存（%s）", "訂正" if before else "新規", extra={"record_id": rid})
    sync(deps, rec, person)
    if before and deps.settings.labor.enabled:
        _post_thread(deps, rec, f"訂正：{_span_text(before, deps)} → {_span_text(rec, deps)}（{reason}）")
    _after_change(deps, rec)
    return rec


def _span_text(r: dict, deps: Deps) -> str:
    if not r.get("start"):
        return "なし"
    return f"{hm(r['start'], deps)}〜{hm(r['end'], deps)} {fmt_h(r.get('hours'))}" + ("（対象外）" if r.get("status") == VOID else "")


def _fill_derived(deps: Deps, rec: dict, person: Staff | None) -> None:
    """本人のその日の売上・組数と、時給の額（勤務日に有効な時給 × 時間。プロモーターは 0）を計算し直す。"""
    index = StaffIndex.load(deps.store)
    person = index.get(rec["staff_id"]) or person
    d = date.fromisoformat(rec["business_date"])
    t = staff_mod.sales_by_owner(index, staff_mod.slips_between(deps.store, d, d)).get(rec["staff_id"], {})
    rec["own_sales"], rec["sales_count"] = t.get("sales", 0), t.get("count", 0)
    wage = person.wage_on(d) if person and not person.is_promoter else None
    rec["wage_amount"] = int(rec["hours"] * wage) if wage is not None and rec.get("status") == OK else None


def _after_change(deps: Deps, rec: dict) -> None:
    """給与が計算済みの月なら、その人の給与を計算し直す。"""
    from . import payroll

    try:
        payroll.on_change(deps, rec["staff_id"], month_of(rec["business_date"]), "勤怠の変更")
    except Exception:
        log.exception("勤怠の変更による給与の再計算に失敗", extra={"record_id": rec["record_id"]})


def records_of(deps: Deps, staff_id: str, month: str | None = None) -> list[dict]:
    recs = deps.store.find_docs(COLLECTION, "staff_id", "==", staff_id).values()
    return [r for r in recs if not month or r["business_date"].startswith(month)]


def month_summary(deps: Deps, staff_id: str, month: str) -> tuple[int, float]:
    """(勤務日数, 総時間)。対象外を除く。"""
    recs = [r for r in records_of(deps, staff_id, month) if r.get("status") == OK]
    return len(recs), round(sum(r.get("hours") or 0 for r in recs), 2)


# ---------------------------------------------------------------- Slack・Asana
def slack_parent(deps: Deps, rec: dict, person: Staff | None) -> str:
    d = date.fromisoformat(rec["business_date"])
    name = person.name if person else (rec.get("display_name") or "不明")
    days, total = month_summary(deps, rec["staff_id"], month_of(d))
    avg = total / days if days else 0
    lines = [("【対象外】" if rec.get("status") == VOID else "") +
             f"{esc(name)}｜{bizdate.label(d)}　{hm(rec['start'], deps)}〜{hm(rec['end'], deps)}　{fmt_h(rec.get('hours'))}",
             f"{d.month}月の稼働：{days}日・{fmt_h(total)}（平均 {fmt_h(round(avg, 2))}/日）"]
    if rec.get("asana", {}).get("url"):
        lines.append(f"Asana：{rec['asana']['url']}")
    if rec.get("dry_run"):
        lines.append("（テスト）")
    return "\n".join(lines)


def asana_values(rec: dict, person: Staff | None) -> dict:
    return {
        "staff_name": person.name if person else (rec.get("display_name") or ""),
        "line_name": (person.line_name if person else "") or rec.get("display_name") or "",
        "work_date": rec["business_date"], "hours": rec.get("hours"), "start": rec.get("start", ""),
        "end": rec.get("end", ""), "own_sales": rec.get("own_sales", 0), "wage_amount": rec.get("wage_amount"),
        "att_status": rec.get("status", OK), "record_id": rec["record_id"], "staff_id": rec["staff_id"], "store": "",
        "slack_url": (rec.get("slack") or {}).get("permalink") or "",
    }


def task_name(rec: dict, person: Staff | None) -> str:
    d = date.fromisoformat(rec["business_date"])
    return f"{person.name if person else (rec.get('display_name') or '不明')} {bizdate.label(d)}"


def sync(deps: Deps, rec: dict, person: Staff | None = None, *, raise_errors: bool = False) -> None:
    """Slack の親メッセージ（無ければ作る）と Asana のタスク（無ければ作る）を、記録の今の内容に合わせる。"""
    person = person or StaffIndex.load(deps.store).get(rec["staff_id"])
    failed: Exception | None = None
    ch = deps.settings.labor_channel
    try:
        slack = rec.get("slack") or {}
        if slack.get("ts"):
            deps.notifier.update(slack["channel_id"], slack["ts"], slack_parent(deps, rec, person))
        else:
            ts = deps.notifier.post(ch, slack_parent(deps, rec, person))
            rec["slack"] = {"channel_id": ch, "ts": ts, "permalink": deps.notifier.permalink(ch, ts)}
            _store_fields(deps, rec, slack=rec["slack"])
    except Exception as e:
        log.exception("勤怠の Slack への反映に失敗", extra={"record_id": rec["record_id"]})
        failed = e
    if deps.labor:
        try:
            gid = (rec.get("asana") or {}).get("task_gid")
            new_gid, url = deps.labor.upsert_attendance(gid, task_name(rec, person), asana_values(rec, person))
            if not gid:
                rec["asana"] = {"task_gid": new_gid, "url": url}
                _store_fields(deps, rec, asana=rec["asana"])
                if (rec.get("slack") or {}).get("ts"):     # Asana のリンクを親メッセージに載せる
                    deps.notifier.update(rec["slack"]["channel_id"], rec["slack"]["ts"], slack_parent(deps, rec, person))
        except Exception as e:
            log.exception("勤怠の Asana への反映に失敗", extra={"record_id": rec["record_id"]})
            failed = failed or e
    _store_fields(deps, rec, sync_pending=bool(failed))
    if failed and raise_errors:
        raise failed


def _store_fields(deps: Deps, rec: dict, **fields) -> None:
    cur = deps.store.get_doc(COLLECTION, rec["record_id"]) or rec
    cur.update(fields)
    rec.update(fields)
    deps.store.set_doc(COLLECTION, rec["record_id"], cur)


def _post_thread(deps: Deps, rec: dict, text: str, mention: bool = False) -> None:
    slack = rec.get("slack") or {}
    if not slack.get("ts"):
        return
    try:
        deps.notifier.post(slack["channel_id"], (deps.settings.mention if mention else "") + text, thread_ts=slack["ts"])
    except Exception:
        log.exception("勤怠のスレッドへの投稿に失敗", extra={"record_id": rec["record_id"]})


def refresh(deps: Deps, staff_id: str, d: date | str) -> bool:
    """伝票・台帳が変わったとき: その人のその日の記録の売上・時給の額を計算し直す。変わったら反映して True。"""
    key = d if isinstance(d, str) else d.isoformat()
    rid = f"W-{key.replace('-', '')}-{staff_id}"
    rec = deps.store.get_doc(COLLECTION, rid)
    if not rec:
        return False
    before = (rec.get("own_sales"), rec.get("sales_count"), rec.get("wage_amount"))
    _fill_derived(deps, rec, None)
    if before == (rec.get("own_sales"), rec.get("sales_count"), rec.get("wage_amount")):
        return False
    rec["sync_pending"] = True
    deps.store.set_doc(COLLECTION, rid, rec)
    sync(deps, rec)
    return True


# ---------------------------------------------------------------- 期限外の承認
def request_approval(deps: Deps, msg, person: Staff, d: date, start: datetime, end: datetime, why: str) -> dict:
    """承認待ちにする: LINE に「承認待ち」と返信し、Slack で桝本に承認を求める（同じイベントは二度出さない）。"""
    pend = deps.store.get_doc(PENDING, msg.event_id)
    if not pend:
        pend = {"event_id": msg.event_id, "state": "waiting", "staff_id": person.staff_id, "business_date": d.isoformat(),
                "start": start.isoformat(), "end": end.isoformat(), "sent_at": msg.sent_at.isoformat(),
                "line_user_id": msg.author_id, "display_name": msg.author, "group_id": msg.group_id,
                "quote_token": msg.quote_token, "text": msg.text, "why": why, "asked_at": now_iso(), "slack": {}}
        deps.store.set_doc(PENDING, msg.event_id, pend)
    if not pend["slack"].get("ts"):
        ch = deps.settings.labor_channel
        ts = deps.notifier.post(ch, (
            f"{deps.settings.mention}【勤怠の承認依頼（{why}）】{esc(person.name)}｜{bizdate.label(d)}　"
            f"{hm(pend['start'], deps)}〜{hm(pend['end'], deps)}　{fmt_h(hours_of(start, end))}\n"
            f"送信：{bizdate.hm(msg.sent_at, _tz(deps))}（期限 {bizdate.hm(deadline(d, deps), _tz(deps))}）\n"
            f"{quote(msg.text)}\n"
            "承認するなら ✅、却下するなら ❌ をこのメッセージに付けてください（30分以内に反映）。"
            + ("\n（テスト）" if deps.settings.labor.dry_run else "")))
        pend["slack"] = {"channel_id": ch, "ts": ts}
        deps.store.set_doc(PENDING, msg.event_id, pend)
        _reply(deps, msg, f"{d.month}/{d.day} の勤怠は入力の期限を過ぎているため、承認待ちです。承認されたらお知らせします。"
               if why == "期限切れ" else f"{d.month}/{d.day} の勤怠はまだ先の日付のため、承認待ちです。承認されたらお知らせします。")
    return {"status": "attendance_pending", "record_id": record_id(d, person.staff_id)}


def _reply(deps: Deps, msg, text: str) -> None:
    if not (deps.settings.reply_ack and deps.line and getattr(msg, "reply_token", None)):
        return
    try:
        deps.line.reply_text(msg.reply_token, text, msg.quote_token)
    except Exception:   # 返信トークンの期限切れなど
        log.warning("LINE への返信に失敗", exc_info=True)


def approvals(deps: Deps) -> int:
    """30分ごと: 承認待ちの Slack 投稿のリアクションを読み、承認・却下を反映する（桝本のリアクションだけ数える）。"""
    cfg, s = deps.settings.labor, deps.settings
    errs = ItemErrors("attendance_approvals")
    n = 0
    for eid, pend in deps.store.find_docs(PENDING, "state", "==", "waiting").items():
        with errs.item(event_id=eid):
            slack = pend.get("slack") or {}
            if not slack.get("ts"):
                continue
            reacted = deps.notifier.reactions(slack["channel_id"], slack["ts"])
            def by(names, reacted=reacted):
                return any(s.slack_alert_mention in reacted.get(e, []) for e in names)

            if by(cfg.approve_emoji):
                _approve(deps, eid, pend)
                n += 1
            elif by(cfg.reject_emoji):
                pend.update(state="rejected", decided_at=now_iso())
                deps.store.set_doc(PENDING, eid, pend)
                deps.notifier.post(slack["channel_id"], "却下しました（記録していません）。", thread_ts=slack["ts"])
                n += 1
            elif not pend.get("reminded") and datetime.fromisoformat(pend["asked_at"]) + timedelta(
                    days=cfg.approval_remind_days) <= datetime.fromisoformat(now_iso()):
                deps.notifier.post(slack["channel_id"], f"{s.mention}承認待ちのままです。✅ か ❌ を付けてください。",
                                   thread_ts=slack["ts"])
                pend["reminded"] = True
                deps.store.set_doc(PENDING, eid, pend)
    errs.raise_if_any()
    return n


def _approve(deps: Deps, eid: str, pend: dict) -> None:
    from . import payroll

    slack = pend["slack"]
    d = date.fromisoformat(pend["business_date"])
    if payroll.is_confirmed(deps, month_of(d)):
        pend.update(state="rejected", decided_at=now_iso(), note="給与が確定済み")
        deps.store.set_doc(PENDING, eid, pend)
        deps.notifier.post(slack["channel_id"], f"{d.month}月の給与が確定済みのため記録できません。管理者が Asana で直してください。",
                           thread_ts=slack["ts"])
        return
    index = StaffIndex.load(deps.store)
    person = index.get(pend["staff_id"]) or Staff(staff_id=pend["staff_id"], line_name=pend.get("display_name") or "")
    save_record(deps, person, d, datetime.fromisoformat(pend["start"]), datetime.fromisoformat(pend["end"]),
                event_id=eid, line_user_id=pend.get("line_user_id"), display_name=pend.get("display_name"),
                reason="承認（期限外）", text=pend.get("text", ""))
    pend.update(state="approved", decided_at=now_iso())
    deps.store.set_doc(PENDING, eid, pend)
    deps.notifier.post(slack["channel_id"], "承認しました。記録しました。", thread_ts=slack["ts"])
    if deps.line and pend.get("group_id"):
        try:
            deps.line.push_text(pend["group_id"],
                                f"{person.line_name or person.name}さんの {d.month}/{d.day} の勤怠"
                                f"（{hm(pend['start'], deps)}〜{hm(pend['end'], deps)}）が承認され、記録しました。",
                                pend.get("quote_token"), str(uuid.uuid5(uuid.NAMESPACE_URL, f"approve-{eid}")))
        except Exception:
            log.exception("承認の LINE への連絡に失敗", extra={"event_id": eid})
            deps.notifier.post(slack["channel_id"], "LINE への承認の連絡は送れませんでした（記録は済んでいます）。",
                               thread_ts=slack["ts"])


# ---------------------------------------------------------------- Asana の手動上書き・やり直し
def overrides(deps: Deps) -> int:
    """30分ごと: 管理者が Asana の勤怠タスクで「時間を反映」「対象外」にしたものを取り込む。"""
    if not deps.labor:
        return 0
    meta = deps.store.get_doc("meta", "labor_overrides") or {}
    started = datetime.fromisoformat(now_iso())
    errs = ItemErrors("attendance_overrides")
    n = 0
    for t in deps.labor.read_attendance_overrides(meta.get("last")):
        with errs.item(task_gid=t["gid"]):
            found = deps.store.find_docs(COLLECTION, "asana.task_gid", "==", t["gid"])
            if not found:
                deps.labor.reset_attendance_override(t["gid"])
                continue
            rec = next(iter(found.values()))
            d = date.fromisoformat(rec["business_date"])
            before = {k: rec.get(k) for k in ("start", "end", "hours", "status")}
            if t["att_manual"] == VOID:
                rec["status"] = VOID
            else:
                start, end = parse_iso_or_hm(t.get("start", ""), d, deps), parse_iso_or_hm(t.get("end", ""), d, deps)
                sp = check_span(start, end, deps) if start and end else "時刻が読み取れない"
                if isinstance(sp, str):
                    _post_thread(deps, rec, f"Asana の「時間を反映」を取り込めませんでした（{sp}）。「なし」に戻しました。", mention=True)
                    deps.labor.reset_attendance_override(t["gid"])
                    continue
                rec.update(start=sp[0].isoformat(), end=sp[1].isoformat(), hours=hours_of(*sp), status=OK)
            rec["history"].append({"at": now_iso(), "reason": f"Asana の手動上書き（{t['att_manual']}）", "before": before})
            rec["updated_at"] = now_iso()
            _fill_derived(deps, rec, None)
            deps.store.set_doc(COLLECTION, rec["record_id"], rec)
            sync(deps, rec)
            _post_thread(deps, rec, f"手動上書き（Asana）：{_span_text(before, deps)} → {_span_text(rec, deps)}")
            deps.labor.reset_attendance_override(t["gid"])
            _after_change(deps, rec)
            n += 1
    if not errs.errors:
        deps.store.set_doc("meta", "labor_overrides", {"last": (started - timedelta(minutes=5)).isoformat()})
    errs.raise_if_any()
    return n


def resync(deps: Deps) -> int:
    """反映に失敗した勤怠・台帳をやり直す。"""
    errs = ItemErrors("attendance_resync")
    n = 0
    for rid, rec in deps.store.find_docs(COLLECTION, "sync_pending", "==", True).items():
        with errs.item(record_id=rid):
            sync(deps, rec, raise_errors=True)
            n += 1
    for sid, d in deps.store.find_docs(staff_mod.COLLECTION, "asana_pending", "==", True).items():
        with errs.item(staff_id=sid):
            s = Staff.from_dict(d)
            publish_staff(deps, s)
            if s.asana_pending:
                raise RuntimeError("台帳の Asana への反映に失敗")
            n += 1
    errs.raise_if_any()
    return n
