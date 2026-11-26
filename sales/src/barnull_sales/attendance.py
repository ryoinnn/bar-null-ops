"""勤怠（LINE の @null 勤怠メッセージ → Firestore attendance/{記録ID} → Slack・Asana）。

- 1件 ＝ 1人 × 1営業日。記録 ID は W-{YYYYMMDD}-{スタッフID}。同じ人・同じ日の2回目は上書き（訂正）
- 勤怠サイクルは 21:00〜翌9:00（[labor]）。日付はサイクルの開始日。0:00〜8:59 と 24時以降の書き方は翌日の時刻
- **すべての入力**（期限内・期限切れ・未来の日付・訂正）は、まず承認待ち（attendance_pending）にし、Slack で桝本の承認を求める。
  承認されるまでは attendance/・Asana・給与（とそれを読むポータル）のどこにも載せない。承認で記録し、却下なら記録しない
  （2026-11-26 本人の指示。期限は承認依頼に「期限切れ」と出す目印として残す）
- 承認待ちのあいだは、毎日 1 回（approval_remind_time 以降の最初の回）依頼のスレッドでリマインドする
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
from .http import ExternalAPIError
from .slips import now_iso
from .staff import Staff, StaffIndex
from .texts import esc, quote, who

log = logging.getLogger(__name__)
COLLECTION = "attendance"
PENDING = "attendance_pending"
OK, VOID = "記録済み", "対象外"
# 承認待ちの状態。superseded: 同じ人・同じ日の新しい入力に置き換わった／closed: 承認されないまま給与が確定した
WAITING, APPROVED, REJECTED, SUPERSEDED, CLOSED = "waiting", "approved", "rejected", "superseded", "closed"


# ---------------------------------------------------------------- 日付・時刻
def _tz(deps: Deps):
    return deps.settings.tz


def _now(deps: Deps) -> datetime:
    return datetime.now(_tz(deps))


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


def _when(dt: datetime, deps: Deps) -> str:
    """「11/25（水）9:00」（LINE・Slack の期限や給与の日時）"""
    local = dt.astimezone(_tz(deps))
    return f"{bizdate.label(local.date())}{local.hour}:{local.minute:02d}"


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
    if rec and msg.event_id in rec.get("event_ids", []):        # 再試行: 記録済み（承認済み、または前の版で記録した）
        if not deps.store.get_doc(PENDING, msg.event_id):       # 前の版が受け取ってすぐ記録した入力だけ OK スタンプ
            on_accepted()                                       # （承認待ちにした入力は、返信トークンを使い済み）
        return {"status": "attendance", "record_id": rid}
    from . import payroll

    closed_note = f"{d.month}月の給与は確定済みのため記録しませんでした。直すときは管理者が Asana で直してください"
    closed_reply = f"{d.month}月の給与は確定済みのため、LINE からは記録・訂正できません。管理者に伝えてください。"
    pend = deps.store.get_doc(PENDING, msg.event_id)
    if pend:                                                    # 再試行: 承認待ちにした後で失敗した（取り下げ・依頼・返信の続き）
        if pend.get("state") == WAITING and payroll.is_confirmed(deps, month_of(d)):
            # 落ちているあいだに給与が確定した: 依頼は出さずに閉じ、ふつうの受け付けと同じく知らせる
            if not (pend.get("slack") or {}).get("ts"):
                _notice(deps, closed_note, msg.sender, msg.text, mention=True)
            _decide(deps, msg.event_id, pend, CLOSED, "締め切り",
                    f"{d.month}月の給与が確定したため、この依頼は閉じました（記録していません）。"
                    "直すときは Asana の給与の「調整額」で直してください。", mention=True, closed_reason="給与が確定済み")
            if not pend.get("replied"):
                _reply(deps, msg, closed_reply)
            return {"status": "attendance_month_closed", "record_id": rid}
        if pend.get("state") == WAITING and _newer_exists(
                deps, rec, {**deps.store.find_docs(PENDING, "record_id", "==", rid), **waiting_of(deps, person.staff_id, d)},
                input_time(pend), msg.event_id):
            # 落ちているあいだに、後に送られた入力が届いていた: この依頼は出さずに取り下げる
            _supersede(deps, {msg.event_id: pend}, "", "同じ日のもっと新しい入力があるため、この依頼は取り下げました（記録していません）。")
            if not pend.get("replied"):
                _reply(deps, msg, f"{d.month}/{d.day} の勤怠（{_span_line(start, end, deps)}）より後に送られた同じ日の勤怠があるため、"
                                  "この入力は使いません。")
            return {"status": "attendance_superseded", "record_id": rid}
        return _ask(deps, msg, pend)
    if payroll.is_confirmed(deps, month_of(d)):
        _notice(deps, closed_note, msg.sender, msg.text, mention=True)
        _reply(deps, msg, closed_reply)
        return {"status": "attendance_month_closed", "record_id": rid}
    waiting = waiting_of(deps, person.staff_id, d)
    what = f"{d.month}/{d.day} の勤怠（{_span_line(start, end, deps)}）"
    # その日の依頼すべて（却下・取り下げを含む。record_id で引くので、その日の数件だけを読む）。後の入力が却下されても、前の入力は使わない
    same_day = deps.store.find_docs(PENDING, "record_id", "==", rid)
    if _newer_exists(deps, rec, {**same_day, **waiting}, msg.sent_at, msg.event_id):
        # 遅れて届いた古い入力（同じ日のもっと新しい入力が承認待ち・記録済み）: 後の入力を優先し、これは使わない
        log.info("古い勤怠の入力が遅れて届いた（使わない）", extra={"record_id": rid})
        _reply(deps, msg, f"{what}より後に送られた同じ日の勤怠があるため、この入力は使いません。")
        return {"status": "attendance_superseded", "record_id": rid}
    if rec and rec.get("status") == OK and _same_span(rec, start, end):
        # 記録と同じ: 変わるものが無いので依頼は出さない。承認待ちの訂正があれば、後の入力（＝今の記録のまま）を優先して取り下げる。
        # この入力の時刻を記録に残す（遅れて届いたもっと古い入力を、後から当てないように）
        if (record_time(deps, rec) or msg.sent_at) <= msg.sent_at:
            _store_fields(deps, rec, applied_sent_at=msg.sent_at.isoformat())
        _supersede(deps, waiting, msg.event_id, "記録と同じ内容の入力が後から届いたため、この依頼は取り下げました（記録は変えていません）。")
        _reply(deps, msg, f"{what}は、同じ内容ですでに記録されています。"
               + ("承認待ちだった訂正は取り下げました。" if waiting else ""))
        return {"status": "attendance_unchanged", "record_id": rid}
    same = next((p for p in waiting.values() if _same_span(p, start, end)), None)
    rt = record_time(deps, rec)
    if same and rt is not None and rt > input_time(same):
        same = None              # 記録より古い依頼（30分ごとの approvals が取り下げる）なので、新しい依頼を出す
    if same:                                                    # 同じ内容の送り直し: 依頼を増やさない
        if input_time(same) < msg.sent_at:                      # この入力の時刻を依頼に残す（後の入力を優先する判定に使う）
            same["latest_sent_at"] = msg.sent_at.isoformat()
            deps.store.set_doc(PENDING, same["event_id"], same)
        _reply(deps, msg, f"{what}は、同じ内容で承認待ちです。{_after_text(deps, bool(rec))}")   # 記録があれば置き換え
        return {"status": "attendance_pending", "record_id": rid, "same_as": same["event_id"]}
    return request_approval(deps, msg, person, d, start, end, rec, waiting)


def input_time(pend: dict) -> datetime:
    """依頼の入力の時刻: 送信時刻と、同じ内容で送り直された時刻（latest_sent_at）の遅いほう。"""
    t = datetime.fromisoformat(pend["sent_at"])
    if pend.get("latest_sent_at"):
        t = max(t, datetime.fromisoformat(pend["latest_sent_at"]))
    return t


def _newer_exists(deps: Deps, rec: dict | None, waiting: dict[str, dict], sent_at: datetime, event_id: str) -> bool:
    """同じ人・同じ日に、この入力（送信 sent_at）より後に送られた入力が、承認待ちか記録済みか（同じ内容の送り直しも数える）。"""
    if any(eid != event_id and input_time(p) > sent_at for eid, p in waiting.items()):
        return True
    applied = record_time(deps, rec)
    return applied is not None and applied > sent_at


def record_time(deps: Deps, rec: dict | None) -> datetime | None:
    """記録に当てた入力のいちばん新しい送信時刻。前の版の記録（applied_sent_at なし）は履歴から求める:
    承認で当てた入力は、その依頼（attendance_pending）の送信時刻。前の版が受け取ってすぐ記録した入力は、記録した時刻（送信の直後）。"""
    if not rec:
        return None
    if rec.get("applied_sent_at"):
        return datetime.fromisoformat(rec["applied_sent_at"])
    times = []
    for h in rec.get("history") or []:
        if not (h.get("event_id") and h.get("at")):
            continue                                        # Asana の手動上書きなど（入力ではない）
        p = deps.store.get_doc(PENDING, h["event_id"])
        times.append(datetime.fromisoformat(p["sent_at"] if p and p.get("sent_at") else h["at"]))
    return max(times) if times else None


def _same_span(doc: dict, start: datetime, end: datetime) -> bool:
    try:
        return datetime.fromisoformat(doc["start"]) == start and datetime.fromisoformat(doc["end"]) == end
    except (KeyError, TypeError, ValueError):
        return False


def _span_line(start: datetime, end: datetime, deps: Deps) -> str:
    """LINE 向け「21:00〜03:00 6時間」"""
    return f"{hm(start.isoformat(), deps)}〜{hm(end.isoformat(), deps)} {fmt_h(hours_of(start, end))[:-1]}時間"


# ---------------------------------------------------------------- 記録
def save_record(deps: Deps, person: Staff, d: date, start: datetime, end: datetime, *, event_id: str,
                line_user_id: str | None, display_name: str | None, reason: str, text: str = "",
                slack: dict | None = None, sent_at: datetime | None = None) -> dict:
    """記録を作る・上書きする（同じイベントは二度当てない）。保存してから Slack・Asana に反映する。

    slack: 新しい記録の親メッセージにする Slack の投稿（承認依頼の投稿を、承認後にそのまま記録の親メッセージに書き換える）。
    sent_at: 当てる入力の送信時刻。記録に当てた入力のいちばん新しい送信時刻を applied_sent_at に持つ（それより古い入力は当てない）。
    """
    rid = record_id(d, person.staff_id)
    rec = deps.store.get_doc(COLLECTION, rid)
    before = None
    if rec:
        if event_id in rec.get("event_ids", []):
            return rec
        before = {k: rec.get(k) for k in ("start", "end", "hours", "status")}
    else:
        rec = {"record_id": rid, "staff_id": person.staff_id, "business_date": d.isoformat(), "event_ids": [],
               "history": [], "slack": dict(slack or {}), "asana": {}, "created_at": now_iso(),
               "dry_run": deps.settings.labor.dry_run}
    rec.update(start=start.isoformat(), end=end.isoformat(), hours=hours_of(start, end), status=OK,
               line_user_id=line_user_id or rec.get("line_user_id"), display_name=display_name or rec.get("display_name"),
               text=text or rec.get("text", ""), updated_at=now_iso(), sync_pending=True)
    rec["event_ids"].append(event_id)
    rec["history"].append({"at": now_iso(), "event_id": event_id, "reason": reason, "before": before})
    if sent_at and (not rec.get("applied_sent_at") or datetime.fromisoformat(rec["applied_sent_at"]) < sent_at):
        rec["applied_sent_at"] = sent_at.isoformat()
    note = f"訂正：{_span_text(before, deps)} → {_span_text(rec, deps)}（{reason}）" if before else ""
    left = (rec.get("after_save") or {}).get("note") or ""     # 前の訂正のスレッドへの投稿がまだなら、消さずに続ける
    rec["after_save"] = {"note": "\n".join(x for x in (left, note) if x), "payroll": True}
    rec["after_save_pending"] = True
    _fill_derived(deps, rec, person)
    deps.store.set_doc(COLLECTION, rid, rec)
    log.info("勤怠を保存（%s）", "訂正" if before else "新規", extra={"record_id": rid})
    after_save(deps, rec, person)
    return rec


def after_save(deps: Deps, rec: dict, person: Staff | None = None) -> bool:
    """保存の後の反映: Slack・Asana、訂正ならスレッドに変更前後、給与の計算し直し。済んだら True。
    保存と一緒に after_save（残っている仕事）と after_save_pending を立て、済んだものから下ろす。
    失敗した・途中で落ちたものは、30分ごとの attendance_resync（と approvals）がやり直す（何度やっても同じ結果）。
    Slack・Asana の失敗は sync_pending で別にやり直す。"""
    sync(deps, rec, person)
    left = dict(rec.get("after_save") or {})
    if left.get("note") and (not deps.settings.labor.enabled or _post_thread(deps, rec, left["note"])):
        left["note"] = ""
    if left.get("payroll", True) and _after_change(deps, rec):
        left["payroll"] = False
    done = not left.get("note") and not left.get("payroll", True)
    _store_fields(deps, rec, after_save=None if done else left, after_save_pending=not done)
    return done


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


def _after_change(deps: Deps, rec: dict) -> bool:
    """給与が計算済みの月なら、その人の給与を計算し直す。できたら True。"""
    from . import payroll

    try:
        payroll.on_change(deps, rec["staff_id"], month_of(rec["business_date"]), "勤怠の変更")
        return True
    except Exception:
        log.exception("勤怠の変更による給与の再計算に失敗", extra={"record_id": rec["record_id"]})
        return False


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
            try:
                deps.notifier.update(slack["channel_id"], slack["ts"], slack_parent(deps, rec, person))
            except ExternalAPIError as e:
                if "message_not_found" not in str(e):
                    raise
                # 親メッセージが Slack で消されていた（チャンネルの整理など）: 出し直す（やり直しが毎回失敗し続けないように）
                log.warning("勤怠の親メッセージが見つからないので出し直す", extra={"record_id": rec["record_id"]})
                slack = {}
        if not slack.get("ts"):
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


def _post_thread(deps: Deps, rec: dict, text: str, mention: bool = False) -> bool:
    """記録のスレッドに投稿する。できたら True（親メッセージがまだ無い・失敗したら False）。"""
    slack = rec.get("slack") or {}
    if not slack.get("ts"):
        return False
    try:
        deps.notifier.post(slack["channel_id"], (deps.settings.mention if mention else "") + text, thread_ts=slack["ts"])
        return True
    except Exception:
        log.exception("勤怠のスレッドへの投稿に失敗", extra={"record_id": rec["record_id"]})
        return False


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


# ---------------------------------------------------------------- 承認（すべての勤怠。2026-11-26 から）
def waiting_of(deps: Deps, staff_id: str, d: date | str) -> dict[str, dict]:
    """その人のその営業日の、承認待ちの依頼 {イベントID: 依頼}（前の版の依頼も含む）。
    記録に当て済みのもの（承認の途中で落ち、状態がまだ waiting のもの）は除く（30分ごとの approvals が承認済みにする）。"""
    key = d if isinstance(d, str) else d.isoformat()
    rec = deps.store.get_doc(COLLECTION, record_id(date.fromisoformat(key), staff_id))
    applied = set((rec or {}).get("event_ids") or [])
    # 承認待ちの一覧（いつも少ない）から絞る。staff_id で引くと、過去の依頼をすべて読むことになる（Firestore の読み取りの無料枠）
    return {eid: p for eid, p in deps.store.find_docs(PENDING, "state", "==", WAITING).items()
            if p.get("staff_id") == staff_id and p.get("business_date") == key and eid not in applied}


def waiting_in(deps: Deps, month: str) -> tuple[int, int]:
    """その月（YYYY-MM）の承認待ちの勤怠の数 (記録の無い日, 訂正＝記録のある日)。給与の計算の知らせに出す。
    記録の無い日は給与に入っていない。訂正は前の記録のまま給与に入っている。"""
    new = fix = 0
    for p in deps.store.find_docs(PENDING, "state", "==", WAITING).values():
        bd = str(p.get("business_date", ""))
        if not bd.startswith(month):
            continue
        rec = deps.store.get_doc(COLLECTION, record_id(date.fromisoformat(bd), p["staff_id"]))
        if rec and p["event_id"] in (rec.get("event_ids") or []):
            continue                                       # 記録に当て済み（承認の途中で落ちたもの）
        if rec and rec.get("status") == OK:
            fix += 1
        else:
            new += 1
    return new, fix


def request_approval(deps: Deps, msg, person: Staff, d: date, start: datetime, end: datetime,
                     rec: dict | None, waiting: dict[str, dict]) -> dict:
    """承認待ちにする: 依頼を保存し、Slack で桝本に承認を求め、LINE に「承認待ち」と返信する。

    同じ人・同じ日の（もっと古い）承認待ちは「取り下げ」にする（後の入力で上書きする決まり）。
    """
    rid = record_id(d, person.staff_id)
    why = [w for w, hit in (("訂正", bool(rec)), ("期限切れ", msg.sent_at > deadline(d, deps)),
                            ("未来の日付", start > msg.sent_at)) if hit]
    pend = {"event_id": msg.event_id, "state": WAITING, "record_id": rid, "staff_id": person.staff_id,
            "business_date": d.isoformat(), "start": start.isoformat(), "end": end.isoformat(),
            "sent_at": msg.sent_at.isoformat(), "line_user_id": msg.author_id, "display_name": msg.author,
            "group_id": msg.group_id, "quote_token": msg.quote_token, "text": msg.text, "why": "・".join(why),
            "resent_by": msg.sender if msg.origin_user_id else None,     # 別の人が引用して送り直した
            "before": {k: rec.get(k) for k in ("start", "end", "hours", "status")} if rec else None,
            "asked_at": _now(deps).isoformat(), "slack": {}, "replied": False}
    deps.store.set_doc(PENDING, msg.event_id, pend)
    return _ask(deps, msg, pend)


NEWER_NOTE = "同じ日の新しい入力が届いたため、この依頼は取り下げました（記録していません）。新しい依頼に ✅ か ❌ を付けてください。"


def _settle(deps: Deps, pend: dict, siblings: dict[str, dict] | None = None) -> None:
    """同じ人・同じ日の承認待ちのうち、この依頼より古いものを取り下げる（承認待ちは、いつも最新の入力の1つだけにする）。
    siblings: 同じ人・同じ日の承認待ち（読み直したもの）。無ければ読む。"""
    sent = input_time(pend)
    if siblings is None:
        siblings = waiting_of(deps, pend["staff_id"], pend["business_date"])
    older = {eid: p for eid, p in siblings.items() if eid != pend["event_id"] and input_time(p) <= sent}
    _supersede(deps, older, pend["event_id"], NEWER_NOTE)


def _supersede(deps: Deps, waiting: dict[str, dict], by_event_id: str, note: str) -> None:
    for eid, old in waiting.items():
        _decide(deps, eid, old, SUPERSEDED, "取り下げ", note, superseded_by=by_event_id)


def _decide(deps: Deps, eid: str, pend: dict, state: str, label: str | None, note: str, mention: bool = False,
            **fields) -> None:
    """依頼の状態を決めて保存し（後始末の中身も一緒に保存する）、Slack の後始末をする。
    保存の後で落ちても、30分ごとの approvals が finish_pending を見て後始末をやり直す。"""
    pend.update(state=state, decided_at=now_iso(), finish_pending=True,
                finish={"label": label, "note": note, "mention": mention}, **fields)
    deps.store.set_doc(PENDING, eid, pend)
    _run_finish(deps, eid, pend)


FINISH_TRIES = 48     # Slack の後始末のやり直しの上限（30分ごと × 48 ＝ 約1日。投稿が消されたときなどに、やり直し続けないように）


def _run_finish(deps: Deps, eid: str, pend: dict) -> None:
    f = pend.get("finish") or {}
    ok = _finish(deps, pend, f.get("label"), f.get("note") or "", bool(f.get("mention")))
    tries = int(pend.get("finish_tries") or 0) + 1
    pend.update(finish_pending=not ok and tries < FINISH_TRIES, finish_tries=tries)
    if not ok and tries >= FINISH_TRIES:
        pend["finish_failed"] = True
        log.error("承認依頼の Slack の後始末を諦めた", extra={"event_id": eid})
    deps.store.set_doc(PENDING, eid, pend)


def _ask(deps: Deps, msg, pend: dict) -> dict:
    """同じ日の古い依頼を取り下げ、Slack に依頼を出し（まだなら）、LINE に返信する（一度だけ）。
    Slack に出せなければ 30分ごとの approvals が出し直す。途中で落ちても、再試行でここから続ける。"""
    if pend.get("state") == WAITING:
        _settle(deps, pend)
        try:
            _post_request(deps, pend)
        except Exception:
            log.exception("勤怠の承認依頼を Slack に出せなかった（30分ごとにやり直す）", extra={"event_id": pend["event_id"]})
    if pend.get("state") == WAITING and not pend.get("replied"):
        d = date.fromisoformat(pend["business_date"])
        start, end = datetime.fromisoformat(pend["start"]), datetime.fromisoformat(pend["end"])
        span_text = _span_line(start, end, deps)
        if pend.get("before"):
            head = f"{d.month}/{d.day} の勤怠の訂正（{span_text}）を受け付けました。"
        else:
            head = f"{d.month}/{d.day} の勤怠（{span_text}）を受け付けました。"
        tail = _after_text(deps, bool(pend.get("before")))
        why = pend.get("why") or ""
        note = ("入力の期限（" + _when(deadline(d, deps), deps) + "）を過ぎています。" if "期限切れ" in why else "") + \
               ("まだ先の日付です。" if "未来の日付" in why else "")
        _reply(deps, msg, head + note + tail)
        pend["replied"] = True
        deps.store.set_doc(PENDING, pend["event_id"], pend)
    status = "attendance_pending" if pend.get("state") == WAITING else f"attendance_{pend.get('state')}"
    return {"status": status, "record_id": pend.get("record_id") or record_id(date.fromisoformat(pend["business_date"]),
                                                                              pend["staff_id"])}


def _after_text(deps: Deps, correction: bool) -> str:
    """LINE の受け付けの返信の結び（承認したら何が起きるか）。"""
    tell = deps.settings.labor.approval_line_notice
    if correction:
        return "管理者が承認したら、前の記録と置き換えて、お知らせします。" if tell else "管理者が承認したら、前の記録と置き換えます。"
    return "管理者が承認したら記録し、お知らせします。" if tell else "管理者が承認したら記録します。"


def _request_text(deps: Deps, pend: dict, result: str | None = None) -> str:
    """承認依頼の本文。result（承認済み・却下・取り下げ・締め切り）を付けると、決まった後の書き換え用（メンションと操作の案内なし）。"""
    person = StaffIndex.load(deps.store).get(pend["staff_id"])
    name = person.name if person else (pend.get("display_name") or "不明")
    d = date.fromisoformat(pend["business_date"])
    start, end = datetime.fromisoformat(pend["start"]), datetime.fromisoformat(pend["end"])
    why = pend.get("why") or ""
    body = f"{esc(name)}｜{bizdate.label(d)}　{hm(pend['start'], deps)}〜{hm(pend['end'], deps)}　{fmt_h(hours_of(start, end))}"
    if result:
        lines = [f"【{result}】{body}" + (f"（{why}）" if why else "")]
    else:
        lines = [f"{deps.settings.mention}【勤怠の承認依頼{f'（{why}）' if why else ''}】{body}"]
    if result == "承認済み":
        if pend.get("before"):          # 承認で置き換えた記録（_approve が入れ直す）
            lines.append(f"前の記録：{_span_text(pend['before'], deps)}")
    else:                               # 依頼・却下・取り下げ・締め切り: 今の記録（出し直し・Asana での直しにも合う。前の版の依頼にも出る）
        cur = deps.store.get_doc(COLLECTION, record_id(d, pend["staff_id"]))
        if cur:
            lines.append(f"{'記録（この依頼では変えていません）' if result else '今の記録'}：{_span_text(cur, deps)}")
    sent = datetime.fromisoformat(pend["sent_at"])
    lines.append(f"送信：{bizdate.hm(sent, _tz(deps))}（期限 {bizdate.hm(deadline(d, deps), _tz(deps))}）"
                 + (f"　{who(pend['resent_by'])}さんが引用して送り直し" if pend.get("resent_by") else ""))
    lines.append(quote(pend.get("text") or ""))
    if not result:
        lines.append("承認は ✅、却下は ❌ をこのメッセージに付けてください（30分以内に反映）。")
    if deps.settings.labor.dry_run:
        lines.append("（テスト）")
    return "\n".join(lines)


def _post_request(deps: Deps, pend: dict) -> None:
    if (pend.get("slack") or {}).get("ts"):
        return
    ch = deps.settings.labor_channel
    ts = deps.notifier.post(ch, _request_text(deps, pend))
    pend["slack"] = {"channel_id": ch, "ts": ts, "permalink": deps.notifier.permalink(ch, ts)}
    deps.store.set_doc(PENDING, pend["event_id"], pend)


def _finish(deps: Deps, pend: dict, result: str | None, note: str, mention: bool = False) -> bool:
    """決まった依頼の後始末（失敗しても止めない）: 依頼の投稿の見出しを結果に書き換え、スレッドに一言残す。できたら True。
    result が None なら書き換えない（承認して、依頼の投稿を記録の親メッセージにしたとき）。"""
    slack = pend.get("slack") or {}
    if not slack.get("ts"):
        return True             # 投稿が無い（Slack に出す前に決まった）: することが無い
    try:
        if result:
            deps.notifier.update(slack["channel_id"], slack["ts"], _request_text(deps, pend, result))
        deps.notifier.post(slack["channel_id"], (deps.settings.mention if mention else "") + note, thread_ts=slack["ts"])
        return True
    except Exception:
        log.exception("承認依頼の投稿の書き換えに失敗（30分ごとにやり直す）", extra={"event_id": pend.get("event_id")})
        return False


def _reply(deps: Deps, msg, text: str) -> None:
    if not (deps.settings.reply_ack and deps.line and getattr(msg, "reply_token", None)):
        return
    try:
        deps.line.reply_text(msg.reply_token, text, msg.quote_token)
    except Exception:   # 返信トークンの期限切れなど
        log.warning("LINE への返信に失敗", exc_info=True)


def approvals(deps: Deps) -> int:
    """30分ごと: 承認待ちの依頼を見回る。

    - Slack に出せていない依頼を出す
    - 給与が確定した月の依頼は閉じる（記録しない）
    - 同じ日のもっと新しい入力がある依頼は取り下げる（承認待ちは、いつも最新の入力の1つだけ）
    - リアクションを読み、承認・却下を反映する（桝本のリアクションだけ数える）。承認は LINE でまとめて知らせる
    - どちらも無ければ、毎日 1 回（approval_remind_time 以降の最初の回）スレッドでリマインドする
    """
    from . import payroll

    cfg, s = deps.settings.labor, deps.settings
    errs = ItemErrors("attendance_approvals")
    now = _now(deps)
    n = 0
    waiting = sorted(deps.store.find_docs(PENDING, "state", "==", WAITING).items(),
                     key=lambda kv: kv[1].get("sent_at") or "")
    same_day: dict[tuple, list[str]] = {}        # 同じ人・同じ日の依頼（ふつうは1つ。読み取りを増やさないよう、この一覧から探す）
    for eid, p in waiting:
        same_day.setdefault((p.get("staff_id"), p.get("business_date")), []).append(eid)
    for eid, _ in waiting:
        with errs.item(event_id=eid):
            pend = deps.store.get_doc(PENDING, eid)      # 一覧を読んだ後に LINE の処理が変えたかもしれないので読み直す
            if not pend or pend.get("state") != WAITING:
                continue
            d = date.fromisoformat(pend["business_date"])
            rec = deps.store.get_doc(COLLECTION, record_id(d, pend["staff_id"]))
            if rec and eid in (rec.get("event_ids") or []):
                # 承認して記録した後、状態を保存する前に落ちていた: 保存の後の反映の続きをしてから、リアクションに関係なく
                # 承認済みにする（記録は二重に当てない）
                if rec.get("after_save_pending") or rec.get("after_save"):
                    after_save(deps, rec)
                _approve(deps, eid, pend)
                n += 1
                continue
            if payroll.is_confirmed(deps, month_of(d)):
                if not (pend.get("slack") or {}).get("ts"):         # 依頼を Slack に出せないまま確定した: 閉じたことだけ知らせる
                    _notice(deps, f"{d.month}月の給与は確定済みのため記録しませんでした"
                                  f"（{bizdate.label(d)}　{hm(pend['start'], deps)}〜{hm(pend['end'], deps)}）。"
                                  "直すときは管理者が Asana で直してください",
                            pend.get("display_name"), pend.get("text") or "", mention=True)
                _decide(deps, eid, pend, CLOSED, "締め切り",
                        f"{d.month}月の給与が確定したため、この依頼は閉じました（記録していません）。"
                        "直すときは Asana の給与の「調整額」で直してください。", mention=True, closed_reason="給与が確定済み")
                n += 1
                continue
            applied = set((rec or {}).get("event_ids") or [])
            decided = {}
            if not pend.get("record_id"):
                # 前の版の依頼（一度だけ）: 前の版で決まった同じ日の依頼（record_id が無く、record_id では引けない）も見る。
                # 後に送られた入力が無ければ record_id を付ける（以後は過去の依頼を読まない。読み取りを増やさないため）
                decided = {oid: o for oid, o in deps.store.find_docs(PENDING, "staff_id", "==", pend["staff_id"]).items()
                           if oid != eid and o.get("business_date") == pend["business_date"] and o.get("state") != WAITING}
                if not _newer_exists(deps, rec, decided, input_time(pend), eid):
                    pend["record_id"] = record_id(d, pend["staff_id"])
                    deps.store.set_doc(PENDING, eid, pend)
            siblings = {}
            for other in same_day.get((pend["staff_id"], pend["business_date"]), []):
                if other != eid and other not in applied:
                    o = deps.store.get_doc(PENDING, other)        # この回の中で変わったかもしれないので読み直す
                    if o and o.get("state") == WAITING:
                        siblings[other] = o
            if _newer_exists(deps, rec, {**decided, **siblings}, input_time(pend), eid):
                _supersede(deps, {eid: pend}, "", "同じ日のもっと新しい入力があるため、この依頼は取り下げました（記録していません）。")
                continue
            _settle(deps, pend, siblings)
            slack = pend.get("slack") or {}
            if not slack.get("ts"):                              # Slack に出せていなかった依頼（まだ有効なものだけ）を出す
                _post_request(deps, pend)
                continue
            try:
                reacted = deps.notifier.reactions(slack["channel_id"], slack["ts"])
            except ExternalAPIError as e:
                if "message_not_found" not in str(e):
                    raise
                # 依頼の投稿が消された（チャンネルの整理など）: 出し直す（リアクションを付ける先が無いと承認できない）
                log.warning("承認依頼の投稿が見つからないので出し直す", extra={"event_id": eid})
                pend.update(slack={}, reposted=int(pend.get("reposted") or 0) + 1)
                deps.store.set_doc(PENDING, eid, pend)
                _post_request(deps, pend)
                continue

            def by(names, reacted=reacted):
                return any(s.slack_alert_mention and s.slack_alert_mention in reacted.get(e, []) for e in names)

            if by(cfg.approve_emoji):
                _approve(deps, eid, pend)
                n += 1
            elif by(cfg.reject_emoji):
                _decide(deps, eid, pend, REJECTED, "却下", "却下しました（記録していません）。")
                n += 1
            elif _remind_due(deps, pend, now):
                _remind(deps, pend, now)
    for eid, pend in deps.store.find_docs(PENDING, "finish_pending", "==", True).items():
        with errs.item(event_id=eid, step="finish"):      # 決めた後、Slack の後始末の前に落ちていたもの
            _run_finish(deps, eid, pend)
    with errs.item(step="line_announce"):
        _announce(deps)
    errs.raise_if_any()
    return n


def _asked_on(deps: Deps, pend: dict) -> date:
    return datetime.fromisoformat(pend.get("asked_at") or pend["sent_at"]).astimezone(_tz(deps)).date()


def _remind_due(deps: Deps, pend: dict, now: datetime) -> bool:
    """依頼した日の翌日から、毎日 approval_remind_time 以降の最初の回に1回。"""
    local = now.astimezone(_tz(deps))
    if local.time() < deps.settings.labor.remind_time:
        return False
    if pend.get("reminded_on") == local.date().isoformat():
        return False
    return _asked_on(deps, pend) < local.date()


def _remind(deps: Deps, pend: dict, now: datetime) -> None:
    from . import payroll

    slack = pend["slack"]
    now = now.astimezone(_tz(deps))
    days = (now.date() - _asked_on(deps, pend)).days
    text = (f"{deps.settings.mention}リマインド：この勤怠はまだ承認待ちです（依頼から{days}日）。"
            "スレッドの一番上の依頼に ✅ か ❌ を付けてください。")
    month = month_of(pend["business_date"])
    since = deps.settings.labor.payroll_since
    rec = deps.store.get_doc(COLLECTION, record_id(date.fromisoformat(pend["business_date"]), pend["staff_id"]))
    fix = bool(rec) and rec.get("status") == OK          # 訂正: 承認されるまでは前の記録のまま給与に入っている
    if month < month_of(now.date()) and not (since and month < since):
        calc, confirm = payroll.run_times(deps, month)
        m = int(month[5:])
        if now < calc:
            text += (f"\n{m}月分の給与は {_when(calc, deps)} に計算します。"
                     + ("この訂正は、承認されるまで前の記録のまま給与に入ります。" if fix else "承認されていない勤怠は給与に入りません。"))
        else:
            text += (f"\n{m}月分の給与は {_when(confirm, deps)} に確定します。"
                     + ("それまでに承認すれば、給与を計算し直します。" if fix else "それまでに承認すれば給与に入ります。"))
    if rec:
        text += f"\n今の記録：{_span_text(rec, deps)}（承認すると、この依頼の内容に置き換わります）"
    deps.notifier.post(slack["channel_id"], text, thread_ts=slack["ts"])
    pend.update(reminded_on=now.date().isoformat(), reminders=int(pend.get("reminders") or 0) + 1)
    deps.store.set_doc(PENDING, pend["event_id"], pend)


def _approve(deps: Deps, eid: str, pend: dict) -> None:
    """承認: 記録し（Slack・Asana・給与に反映）、LINE で知らせる中身を積む（announce_pending。_announce が送る）。"""
    slack = pend["slack"]
    d = date.fromisoformat(pend["business_date"])
    index = StaffIndex.load(deps.store)
    person = index.get(pend["staff_id"]) or Staff(staff_id=pend["staff_id"], line_name=pend.get("display_name") or "")
    rid = record_id(d, pend["staff_id"])
    existing = deps.store.get_doc(COLLECTION, rid)
    # 新しい記録なら、依頼の投稿をそのまま記録の親メッセージにする（チャンネルに投稿が二重にならないように）
    fresh = not existing or (existing.get("slack") or {}).get("ts") == slack["ts"]
    corrected = bool(existing) and not fresh
    if existing and eid not in (existing.get("event_ids") or []):
        # 承認で置き換える記録（依頼のあとに Asana で直されていることがある）を、結果の見出しの「前の記録」にする
        pend["before"] = {k: existing.get(k) for k in ("start", "end", "hours", "status")}
    elif existing:
        # 当て済み（承認の途中で落ちたもののやり直し）: 当てたときに置き換えた記録は、記録の履歴にある
        h = next((h for h in existing.get("history") or [] if h.get("event_id") == eid), None)
        if h and h.get("before"):
            pend["before"] = h["before"]
    seed = None
    if not existing:
        seed = {"channel_id": slack["channel_id"], "ts": slack["ts"],
                "permalink": slack.get("permalink") or deps.notifier.permalink(slack["channel_id"], slack["ts"])}
    why = pend.get("why") or ""
    save_record(deps, person, d, datetime.fromisoformat(pend["start"]), datetime.fromisoformat(pend["end"]),
                event_id=eid, line_user_id=pend.get("line_user_id"), display_name=pend.get("display_name"),
                reason="承認" + (f"（{why}）" if why else "")
                + (f"（{pend['resent_by']}さんが引用して送り直し）" if pend.get("resent_by") else ""),
                text=pend.get("text", ""), slack=seed, sent_at=input_time(pend))
    announce = {}
    if deps.settings.labor.approval_line_notice and deps.line and pend.get("group_id"):
        span_text = f"{hm(pend['start'], deps)}〜{hm(pend['end'], deps)}"
        name = person.line_name or person.name
        announce = {"announce_pending": True, "announce_text": (
            f"{name}さんの {d.month}/{d.day} の勤怠の訂正（{span_text}）が承認され、記録を直しました。" if corrected
            else f"{name}さんの {d.month}/{d.day} の勤怠（{span_text}）が承認され、記録しました。")}
    if corrected:
        _decide(deps, eid, pend, APPROVED, "承認済み", "承認しました。記録を直しました。", corrected=True, **announce)
    else:      # 依頼の投稿は記録の親メッセージになったので、見出しは書き換えない
        _decide(deps, eid, pend, APPROVED, None, "承認しました。記録しました。", corrected=False, **announce)


def _announce(deps: Deps) -> None:
    """承認の連絡（announce_pending）を LINE にプッシュ送信する。グループごとに 1 回の送信へまとめる（1回に5件まで）。
    月の無料枠は「送信の回数 × グループの人数」で数えるので、1件ずつ送るより枠を使わない。
    送る組と retry_key は送る前に保存する（announce_key）。送った後で落ちても、次の回は同じ組・同じ key で送るので、
    LINE が二重に届けない。送れなければ Slack のスレッドに残して諦める。"""
    if not deps.line:
        return
    pending = sorted(deps.store.find_docs(PENDING, "announce_pending", "==", True).items(),
                     key=lambda kv: kv[1].get("sent_at") or "")
    fresh: dict[str, list[tuple[str, dict]]] = {}
    for eid, p in pending:
        if not p.get("announce_key"):
            fresh.setdefault(p.get("group_id") or "", []).append((eid, p))
    for its in fresh.values():
        for i in range(0, len(its), 5):
            chunk = its[i:i + 5]
            key = str(uuid.uuid5(uuid.NAMESPACE_URL, "approve-" + ",".join(sorted(eid for eid, _ in chunk))))
            for eid, p in chunk:
                p["announce_key"] = key
                deps.store.set_doc(PENDING, eid, p)
    batches: dict[tuple[str, str], list[tuple[str, dict]]] = {}
    for eid, p in pending:
        batches.setdefault((p.get("group_id") or "", p["announce_key"]), []).append((eid, p))
    for (gid, key), chunk in batches.items():
        ok = True
        try:
            deps.line.push_texts(gid, [(p["announce_text"], p.get("quote_token")) for _, p in chunk[:5]], key)
        except Exception:
            ok = False
            log.exception("承認の LINE への連絡に失敗", extra={"event_ids": [eid for eid, _ in chunk]})
        for eid, p in chunk:
            p.update(announce_pending=False, **({"announced_at": now_iso()} if ok else {"announce_failed": True}))
            deps.store.set_doc(PENDING, eid, p)
            if not ok:
                _finish(deps, p, None, "LINE への承認の連絡は送れませんでした（記録は済んでいます）。")


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
    for rid, rec in deps.store.find_docs(COLLECTION, "after_save_pending", "==", True).items():
        with errs.item(record_id=rid, step="after_save"):          # 訂正のスレッド・給与の計算し直しのやり直し
            if not after_save(deps, deps.store.get_doc(COLLECTION, rid) or rec):
                raise RuntimeError("勤怠の保存の後の反映（訂正のスレッド・給与の計算し直し）に失敗")
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
