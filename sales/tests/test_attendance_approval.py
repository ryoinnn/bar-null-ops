"""勤怠はすべて桝本の承認を待つ（2026-11-26 本人の指示。docs/profit/labor_design.md §5）。

- 期限内・期限切れ・未来の日付・訂正のどれも、承認されるまでは attendance/・Asana（ポータルが読む）・給与のどこにも載せない
- 却下・取り下げ・締め切り（給与の確定）は記録しない
- 承認待ちのあいだは、毎日 1 回（10:00 以降の最初の回）依頼のスレッドでリマインドする
名前・時給・金額はすべて架空。
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime

import pytest
from helpers import (
    APPROVER,
    JST,
    approve_all,
    att,
    event,
    labor_deps,
    react,
    slip_msg,
    staff_texts,
    waiting,
)

from barnull_sales import attendance, jobs, labor_hooks, payroll
from barnull_sales.errors import JobFailed
from barnull_sales.extract import Entry
from barnull_sales.process import handle_event
from barnull_sales.staff import StaffIndex


# ---------------------------------------------------------------- 部品
@pytest.fixture
def clock(monkeypatch):
    now = {"t": datetime(2026, 11, 24, 3, 31, tzinfo=JST)}
    monkeypatch.setattr(attendance, "_now", lambda deps: now["t"])
    monkeypatch.setattr(payroll, "_now", lambda deps: now["t"])
    return now


def at(local: str) -> datetime:
    return datetime.strptime(local, "%Y-%m-%d %H:%M").replace(tzinfo=JST)


def send(deps, clock, when, eid, user="U1"):
    """LINE で勤怠を送った（返信・引用のトークン付き）。処理の時刻（依頼の時刻）は送信と同じ。"""
    clock["t"] = at(when)
    ev = event(when, eid, user=user)
    ev["replyToken"], ev["message"]["quoteToken"] = "R" + eid, "Q" + eid
    return handle_event(ev, "message", deps)


def run(deps, clock, when) -> int:
    """30分ごとの attendance_approvals（その時刻に動いた）。失敗はテストの失敗にする。返り値は承認・却下・締め切りの数。"""
    clock["t"] = at(when)
    return attendance.approvals(deps)


def ledger(deps, **values) -> str:
    gid, _ = deps.labor.upsert_staff(None, values.get("real_name", ""), values)
    jobs.run_steps(deps, [("staff_sync", labor_hooks.staff_sync)])
    return gid


def taro(deps):
    return ledger(deps, real_name="山田花子", line_name="たろう", staff_kind="スタッフ", wage=1500, back_rate=10)


def sid(deps, user="U1") -> str:
    """スタッフID（台帳にある人は、まだ勤怠を送っていなくても LINE名で分かる）。"""
    index = StaffIndex.load(deps.store)
    return (index.by_line_user(user) or index.by_name(deps.line.names[user])).staff_id


def rec(deps, day="20261123", user="U1"):
    return deps.store.get_doc("attendance", f"W-{day}-{sid(deps, user)}")


def pend(deps, eid):
    return deps.store.get_doc("attendance_pending", eid)


def requests(deps) -> list[dict]:
    return [p for p in deps.notifier.posts if p["channel"] == "CSTAFF" and not p["thread_ts"] and "【勤怠の承認依頼" in p["text"]]


def thread(deps, ts) -> list[str]:
    return [p["text"] for p in deps.notifier.posts if p["thread_ts"] == ts]


def current(deps, ts) -> str:
    """その投稿の今の文面（書き換えた後なら最後の書き換え）。"""
    ups = [u["text"] for u in deps.notifier.updates if u["ts"] == ts]
    return ups[-1] if ups else next(p["text"] for p in deps.notifier.posts if p["ts"] == ts)


def reminders(deps, ts) -> list[str]:
    return [t for t in thread(deps, ts) if "リマインド" in t]


def att_tasks(deps) -> list[dict]:
    """ポータルが読む Asana「Bar null 勤怠」のタスク。"""
    return [t for t in deps.labor.tasks.values() if t["project"] == "attendance"]


def pay_tasks(deps, month="2026-11") -> list[dict]:
    """ポータル・出資者向け資料が読む Asana「Bar null 給与」のその月のタスク（台帳を入れると、0 時間の見込みができる）。"""
    return [t for t in deps.labor.tasks.values() if t["project"] == "payroll" and t["month"] == month]


def replies(deps) -> list[str]:
    return [r[2] for r in deps.line.replies if len(r) == 3]


def pushes(deps) -> list[tuple]:
    return getattr(deps.line, "pushes", [])


# ---------------------------------------------------------------- 期限内（いちばん多い場合）
def test_in_deadline_input_waits_and_nothing_is_reflected_until_approved(clock):
    deps = labor_deps([att(11, 23, "21:00", "03:00")], reply_ack=True)
    taro(deps)
    assert send(deps, clock, "2026-11-24 03:30", "E1")["status"] == "attendance_pending"

    # 承認前: 記録・Asana（ポータル）・給与のどこにも無い
    assert deps.store.find_docs("attendance", "staff_id", "==", sid(deps)) == {}
    assert att_tasks(deps) == [] and [t["hours"] for t in pay_tasks(deps)] in ([], [0])
    assert all(d["hours"] == 0 for d in deps.store.find_docs("payroll", "month", "==", "2026-11").values())
    [ask] = requests(deps)
    assert ask["text"].startswith("<@UALERT> 【勤怠の承認依頼】山田花子｜11/23（月）　21:00〜03:00　6h\n")
    assert "送信：11/24 03:30（期限 11/25 09:00）" in ask["text"]
    assert "承認は ✅、却下は ❌ をこのメッセージに付けてください" in ask["text"]
    assert deps.line.replies == [("RE1", "QE1", "11/23 の勤怠（21:00〜03:00 6時間）を受け付けました。"
                                               "管理者が承認したら記録し、お知らせします。")]      # OK スタンプではなく文面

    assert run(deps, clock, "2026-11-24 04:00") == 0                                # 反応が無いあいだは何もしない
    assert att_tasks(deps) == [] and rec(deps) is None

    react(deps, ask)
    run(deps, clock, "2026-11-24 09:00")
    r = rec(deps)
    assert r["hours"] == 6 and r["status"] == "記録済み" and r["event_ids"] == ["E1"]
    assert r["history"][-1]["reason"] == "承認"
    [task] = att_tasks(deps)
    assert task["hours"] == 6 and task["slack_url"] == ask_permalink(ask)
    assert [d["hours"] for d in deps.store.find_docs("payroll", "month", "==", "2026-11").values()] == [6]   # 見込みの給与に入る
    assert [t["hours"] for t in pay_tasks(deps)] == [6]
    # 依頼の投稿が、そのまま記録の親メッセージになる（チャンネルの投稿は1つ）
    assert r["slack"]["ts"] == ask["ts"]
    assert "11月の稼働：1日・6h" in current(deps, ask["ts"]) and "<@UALERT>" not in current(deps, ask["ts"])
    assert [p for p in deps.notifier.posts if not p["thread_ts"] and "11/23（月）" in p["text"]] == [ask]
    assert thread(deps, ask["ts"]) == ["承認しました。記録しました。"]
    assert pushes(deps) == [("Cgroup", "たろうさんの 11/23 の勤怠（21:00〜03:00）が承認され、記録しました。", "QE1",
                             pushes(deps)[0][3])]
    run(deps, clock, "2026-11-24 09:30")                                              # 二度は反映しない
    assert len(pushes(deps)) == 1 and len(att_tasks(deps)) == 1


def ask_permalink(ask) -> str:
    return f"https://slack.example/archives/CSTAFF/p{ask['ts'].replace('.', '')}"


def test_rejected_input_is_never_reflected(clock):
    deps = labor_deps([att(11, 23, "21:00", "03:00")], reply_ack=True)
    taro(deps)
    send(deps, clock, "2026-11-24 03:30", "E1")
    [ask] = requests(deps)
    react(deps, ask, "x")
    assert run(deps, clock, "2026-11-24 12:00") == 1
    assert pend(deps, "E1")["state"] == "rejected"
    assert rec(deps) is None and att_tasks(deps) == [] and [t["hours"] for t in pay_tasks(deps)] in ([], [0])
    assert current(deps, ask["ts"]).startswith("【却下】山田花子｜11/23（月）　21:00〜03:00　6h")
    assert thread(deps, ask["ts"]) == ["却下しました（記録していません）。"]
    assert pushes(deps) == []                                    # 却下は LINE に知らせない（要件 §2-3）
    run(deps, clock, "2026-11-25 10:00")
    assert reminders(deps, ask["ts"]) == []


def test_reactions_by_others_or_other_emoji_are_not_counted(clock):
    deps = labor_deps([att(11, 23, "21:00", "03:00")])
    send(deps, clock, "2026-11-24 03:30", "E1")
    [ask] = requests(deps)
    react(deps, ask, "white_check_mark", "USOMEONE")
    react(deps, ask, "x", "USOMEONE")
    react(deps, ask, "eyes", APPROVER)
    run(deps, clock, "2026-11-24 12:00")
    assert pend(deps, "E1")["state"] == "waiting" and rec(deps) is None
    react(deps, ask, "heavy_check_mark", APPROVER)               # ✔️ も承認
    run(deps, clock, "2026-11-24 12:30")
    assert pend(deps, "E1")["state"] == "approved" and rec(deps)["hours"] == 6


# ---------------------------------------------------------------- 期限切れ・未来の日付
def test_late_and_future_inputs_are_marked_and_replied(clock):
    deps = labor_deps([att(11, 20, "21:00", "03:00"), att(11, 28, "21:00", "02:00")], reply_ack=True)
    send(deps, clock, "2026-11-24 03:30", "E1")      # 11/20 の期限は 11/22 9:00
    send(deps, clock, "2026-11-24 03:40", "E2")      # 11/28 はまだ先
    a, b = requests(deps)
    assert "【勤怠の承認依頼（期限切れ）】たろう｜11/20（金）" in a["text"] and "（期限 11/22 09:00）" in a["text"]
    assert "【勤怠の承認依頼（未来の日付）】たろう｜11/28（土）　21:00〜02:00　5h" in b["text"]
    assert replies(deps) == [
        "11/20 の勤怠（21:00〜03:00 6時間）を受け付けました。入力の期限（11/22（日）9:00）を過ぎています。管理者が承認したら記録し、お知らせします。",
        "11/28 の勤怠（21:00〜02:00 5時間）を受け付けました。まだ先の日付です。管理者が承認したら記録し、お知らせします。"]
    assert rec(deps, "20261120") is None and rec(deps, "20261128") is None and att_tasks(deps) == []
    approve_all(deps)
    assert rec(deps, "20261120")["history"][-1]["reason"] == "承認（期限切れ）"
    assert rec(deps, "20261128")["history"][-1]["reason"] == "承認（未来の日付）"
    assert len(att_tasks(deps)) == 2


# ---------------------------------------------------------------- 訂正
def test_correction_needs_approval_and_old_record_stays_until_then(clock):
    deps = labor_deps([att(11, 23, "21:00", "03:00"), att(11, 23, "21:30", "03:00"), att(11, 23, "22:00", "03:00")],
                      reply_ack=True)
    taro(deps)
    send(deps, clock, "2026-11-24 03:30", "E1")
    approve_all(deps)
    first = requests(deps)[0]
    assert send(deps, clock, "2026-11-24 12:00", "E2")["status"] == "attendance_pending"
    ask = requests(deps)[1]
    assert ask["text"].startswith("<@UALERT> 【勤怠の承認依頼（訂正）】山田花子｜11/23（月）　21:30〜03:00　5.5h\n今の記録：21:00〜03:00 6h\n")
    assert replies(deps)[-1] == ("11/23 の勤怠の訂正（21:30〜03:00 5.5時間）を受け付けました。"
                                 "管理者が承認したら、前の記録と置き換えて、お知らせします。")
    assert rec(deps)["hours"] == 6 and att_tasks(deps)[0]["hours"] == 6               # 承認前は前の記録のまま

    react(deps, ask)
    run(deps, clock, "2026-11-24 13:00")
    r = rec(deps)
    assert r["hours"] == 5.5 and r["event_ids"] == ["E1", "E2"] and r["slack"]["ts"] == first["ts"]
    assert "訂正：21:00〜03:00 6h → 21:30〜03:00 5.5h（承認（訂正））" in thread(deps, first["ts"])
    assert current(deps, ask["ts"]).startswith("【承認済み】山田花子｜11/23（月）　21:30〜03:00　5.5h（訂正）\n前の記録：21:00〜03:00 6h")
    assert thread(deps, ask["ts"]) == ["承認しました。記録を直しました。"]
    assert pushes(deps)[-1][1] == "たろうさんの 11/23 の勤怠の訂正（21:30〜03:00）が承認され、記録を直しました。"
    assert len(att_tasks(deps)) == 1 and att_tasks(deps)[0]["hours"] == 5.5

    send(deps, clock, "2026-11-24 14:00", "E3")                                        # 却下された訂正は反映しない
    react(deps, requests(deps)[2], "x")
    run(deps, clock, "2026-11-24 14:30")
    assert rec(deps)["hours"] == 5.5 and att_tasks(deps)[0]["hours"] == 5.5


def test_new_input_withdraws_the_waiting_one_for_the_same_day(clock):
    deps = labor_deps([att(11, 23, "21:00", "03:00"), att(11, 23, "21:30", "03:00")])
    send(deps, clock, "2026-11-24 03:30", "E1")
    send(deps, clock, "2026-11-24 04:00", "E2")
    old, new = requests(deps)
    assert pend(deps, "E1")["state"] == "superseded" and pend(deps, "E1")["superseded_by"] == "E2"
    assert current(deps, old["ts"]).startswith("【取り下げ】たろう｜11/23（月）　21:00〜03:00　6h")
    assert "取り下げました" in thread(deps, old["ts"])[0]
    assert "（訂正）" not in new["text"]                               # まだ記録が無いので訂正ではない
    react(deps, old)                                                   # 取り下げた依頼の ✅ は数えない
    run(deps, clock, "2026-11-24 05:00")
    assert rec(deps) is None
    react(deps, new)
    run(deps, clock, "2026-11-24 05:30")
    assert rec(deps)["hours"] == 5.5 and rec(deps)["event_ids"] == ["E2"]
    run(deps, clock, "2026-11-25 10:00")
    assert reminders(deps, old["ts"]) == [] and reminders(deps, new["ts"]) == []


def test_older_input_arriving_late_does_not_replace_the_newer_one(clock):
    deps = labor_deps([att(11, 23, "21:30", "03:00"), att(11, 23, "21:00", "03:00"), att(11, 23, "22:00", "03:00")],
                      reply_ack=True)
    send(deps, clock, "2026-11-24 04:00", "E2")                                        # 後に送ったものが先に届いた
    assert send(deps, clock, "2026-11-24 03:30", "E1")["status"] == "attendance_superseded"
    assert replies(deps)[-1] == "11/23 の勤怠（21:00〜03:00 6時間）より後に送られた同じ日の勤怠があるため、この入力は使いません。"
    assert len(requests(deps)) == 1 and pend(deps, "E2")["state"] == "waiting"
    approve_all(deps)
    assert rec(deps)["hours"] == 5.5 and datetime.fromisoformat(rec(deps)["applied_sent_at"]) == at("2026-11-24 04:00")
    # 記録済みの入力より前に送られたものが、もっと遅れて届いた → 使わない
    assert send(deps, clock, "2026-11-24 03:00", "E0")["status"] == "attendance_superseded"
    assert len(requests(deps)) == 1 and rec(deps)["hours"] == 5.5


def test_same_content_is_not_asked_twice(clock):
    deps = labor_deps([att(11, 23, "21:00", "03:00")], reply_ack=True)
    send(deps, clock, "2026-11-24 03:30", "E1")
    r = send(deps, clock, "2026-11-24 03:45", "E2")                                    # 返事を待たずに送り直した
    assert r == {"status": "attendance_pending", "record_id": f"W-20261123-{sid(deps)}", "same_as": "E1"}
    assert len(requests(deps)) == 1
    assert replies(deps)[-1] == "11/23 の勤怠（21:00〜03:00 6時間）は、同じ内容で承認待ちです。管理者が承認したら記録し、お知らせします。"
    approve_all(deps)
    assert send(deps, clock, "2026-11-24 12:00", "E3")["status"] == "attendance_unchanged"
    assert len(requests(deps)) == 1 and rec(deps)["event_ids"] == ["E1"]
    assert replies(deps)[-1] == "11/23 の勤怠（21:00〜03:00 6時間）は、同じ内容ですでに記録されています。"


def test_resend_matching_the_record_withdraws_a_waiting_correction(clock):
    deps = labor_deps([att(11, 23, "21:00", "03:00"), att(11, 23, "21:30", "03:00"), att(11, 23, "21:00", "03:00")],
                      reply_ack=True)
    send(deps, clock, "2026-11-24 03:30", "E1")
    approve_all(deps)
    send(deps, clock, "2026-11-24 12:00", "E2")                                        # 訂正（承認待ち）
    assert send(deps, clock, "2026-11-24 12:10", "E3")["status"] == "attendance_unchanged"   # やっぱり元のまま
    assert pend(deps, "E2")["state"] == "superseded"
    assert replies(deps)[-1].endswith("同じ内容ですでに記録されています。承認待ちだった訂正は取り下げました。")
    react(deps, requests(deps)[1])
    run(deps, clock, "2026-11-24 13:00")
    assert rec(deps)["hours"] == 6


def test_resending_a_voided_record_needs_approval(clock):
    deps = labor_deps([att(11, 23, "21:00", "03:00"), att(11, 23, "21:00", "03:00")])
    send(deps, clock, "2026-11-24 03:30", "E1")
    approve_all(deps)
    gid = rec(deps)["asana"]["task_gid"]
    deps.labor.tasks[gid].update(att_manual="対象外")
    jobs.run_steps(deps, [("attendance_overrides", attendance.overrides)])
    assert send(deps, clock, "2026-11-24 12:00", "E2")["status"] == "attendance_pending"
    assert "今の記録：21:00〜03:00 6h（対象外）" in requests(deps)[1]["text"]
    assert rec(deps)["status"] == "対象外" and deps.labor.tasks[gid]["att_status"] == "対象外"
    approve_all(deps)
    assert rec(deps)["status"] == "記録済み" and deps.labor.tasks[gid]["att_status"] == "記録済み"


# ---------------------------------------------------------------- 毎日のリマインド
def test_reminder_once_a_day_in_the_thread_until_decided(clock):
    deps = labor_deps([att(11, 23, "21:00", "03:00")])
    send(deps, clock, "2026-11-24 03:30", "E1")
    [ask] = requests(deps)
    ts = ask["ts"]
    for when in ("2026-11-24 10:00", "2026-11-24 23:30", "2026-11-25 09:30"):        # 依頼した日・10時より前は送らない
        run(deps, clock, when)
    assert reminders(deps, ts) == []
    run(deps, clock, "2026-11-25 10:00")
    assert reminders(deps, ts) == ["<@UALERT> リマインド：この勤怠はまだ承認待ちです（依頼から1日）。"
                                  "スレッドの一番上の依頼に ✅ か ❌ を付けてください。"]
    for when in ("2026-11-25 10:30", "2026-11-25 18:00", "2026-11-25 23:59"):        # 同じ日に二度は送らない
        run(deps, clock, when)
    assert len(reminders(deps, ts)) == 1
    run(deps, clock, "2026-11-26 00:30")                                              # 日をまたいでも 10時までは送らない
    assert len(reminders(deps, ts)) == 1
    run(deps, clock, "2026-11-26 10:05")
    assert len(reminders(deps, ts)) == 2 and "依頼から2日" in reminders(deps, ts)[1]
    assert all(p["thread_ts"] == ts for p in deps.notifier.posts if "リマインド" in p["text"])   # スレッドに返信
    assert pend(deps, "E1")["reminders"] == 2 and pend(deps, "E1")["reminded_on"] == "2026-11-26"
    run(deps, clock, "2026-11-28 11:00")                                              # 1日抜けても、その日は1回だけ
    assert len(reminders(deps, ts)) == 3 and "依頼から4日" in reminders(deps, ts)[2]
    react(deps, ask)
    run(deps, clock, "2026-11-28 12:00")                                              # 承認後は止まる
    run(deps, clock, "2026-11-29 10:00")
    run(deps, clock, "2026-11-30 10:00")
    assert len(reminders(deps, ts)) == 3 and rec(deps)["hours"] == 6


def test_reminders_apply_to_every_kind_of_request(clock):
    deps = labor_deps([att(11, 23, "21:00", "03:00"), att(11, 20, "21:00", "03:00"), att(11, 28, "21:00", "03:00"),
                       att(11, 22, "21:00", "03:00"), att(11, 22, "22:00", "03:00")])
    send(deps, clock, "2026-11-24 03:30", "E1")          # 期限内
    send(deps, clock, "2026-11-24 03:31", "E2")          # 期限切れ
    send(deps, clock, "2026-11-24 03:32", "E3")          # 未来の日付
    send(deps, clock, "2026-11-24 03:33", "E4")          # 11/22
    react(deps, requests(deps)[3])
    run(deps, clock, "2026-11-24 04:00")
    send(deps, clock, "2026-11-24 04:10", "E5")          # 11/22 の訂正
    asks = requests(deps)
    assert len(asks) == 5 and "【勤怠の承認依頼（訂正）】" in asks[4]["text"]     # 11/22 の期限は 11/24 9:00
    run(deps, clock, "2026-11-25 10:00")
    assert [len(reminders(deps, a["ts"])) for a in asks] == [1, 1, 1, 0, 1]


def test_reminder_failure_is_retried_the_same_day(clock, monkeypatch):
    deps = labor_deps([att(11, 23, "21:00", "03:00")])
    send(deps, clock, "2026-11-24 03:30", "E1")
    real = deps.notifier.post

    def broken(channel, text, thread_ts=None, broadcast=False):
        if "リマインド" in text:
            raise RuntimeError("Slack に届かない")
        return real(channel, text, thread_ts, broadcast)

    monkeypatch.setattr(deps.notifier, "post", broken)
    clock["t"] = at("2026-11-25 10:00")
    assert not jobs.run_steps(deps, [("attendance_approvals", attendance.approvals)])   # 失敗はジョブの失敗として知らせる
    assert "reminded_on" not in pend(deps, "E1")
    monkeypatch.setattr(deps.notifier, "post", real)
    run(deps, clock, "2026-11-25 10:30")
    assert len(reminders(deps, requests(deps)[0]["ts"])) == 1


# ---------------------------------------------------------------- 給与の計算・確定との関係
def test_unapproved_attendance_is_left_out_of_payroll_and_reminder_says_so(clock):
    deps = labor_deps([att(11, 29, "21:00", "03:00"), att(11, 28, "21:00", "03:00")])
    taro(deps)
    send(deps, clock, "2026-11-29 04:00", "E0")                       # 先に 11/28 を承認しておく（給与 6h）
    send(deps, clock, "2026-11-30 04:00", "E1")
    react(deps, pend(deps, "E0"))
    run(deps, clock, "2026-11-30 05:00")
    assert rec(deps, "20261129")["hours"] == 6 and rec(deps, "20261130") is None
    ts = pend(deps, "E1")["slack"]["ts"]

    run(deps, clock, "2026-12-01 10:00")                              # 計算の前のリマインド
    assert reminders(deps, ts)[-1].endswith("\n11月分の給与は 12/2（水）17:00 に計算します。承認されていない勤怠は給与に入りません。")
    clock["t"] = at("2026-12-02 17:10")
    assert payroll.step(deps)["calculated"] == 1
    doc = next(iter(deps.store.find_docs("payroll", "month", "==", "2026-11").values()))
    assert (doc["days"], doc["hours"], doc["wage_part"]) == (1, 6, 9000)                 # 承認待ちの 6h は入らない
    assert [t["hours"] for t in pay_tasks(deps)] == [6]
    calc = next(t for t in staff_texts(deps) if "11月分の給与を計算しました" in t)
    assert "承認待ちの勤怠が1件あり、給与に入っていません。10日 09:00 の確定までに承認すれば、計算し直します。" in calc

    run(deps, clock, "2026-12-03 10:00")                              # 計算の後のリマインド
    assert reminders(deps, ts)[-1].endswith("\n11月分の給与は 12/10（木）9:00 に確定します。それまでに承認すれば給与に入ります。")
    react(deps, pend(deps, "E1"))
    run(deps, clock, "2026-12-03 11:00")                              # 計算済みの月: 承認したら計算し直す
    doc = next(iter(deps.store.find_docs("payroll", "month", "==", "2026-11").values()))
    assert (doc["days"], doc["hours"], doc["status"]) == (2, 12, "計算済み")
    assert any("山田花子さんの11月分の給与を計算し直しました（勤怠の変更）" in t for t in staff_texts(deps))


def test_waiting_request_is_closed_when_the_month_is_confirmed(clock):
    deps = labor_deps([att(11, 23, "21:00", "03:00"), att(11, 24, "21:00", "03:00")], reply_ack=True)
    send(deps, clock, "2026-11-24 03:30", "E1")
    ts = requests(deps)[0]["ts"]
    deps.store.set_doc("payroll_runs", "2026-11", {"calculated_at": "x", "confirmed_at": "y"})
    assert run(deps, clock, "2026-12-10 09:30") == 1
    assert pend(deps, "E1")["state"] == "closed"
    assert current(deps, ts).startswith("【締め切り】たろう｜11/23（月）")
    assert thread(deps, ts) == ["<@UALERT> 11月の給与が確定したため、この依頼は閉じました（記録していません）。"
                                "直すときは Asana の給与の「調整額」で直してください。"]
    react(deps, ts)                                                    # 閉じた後の ✅ は数えない
    run(deps, clock, "2026-12-10 10:00")
    run(deps, clock, "2026-12-11 10:00")
    assert rec(deps) is None and att_tasks(deps) == [] and reminders(deps, ts) == []
    # 確定した月への新しい入力は受け付けない（LINE でも知らせる）
    assert send(deps, clock, "2026-12-11 12:00", "E2")["status"] == "attendance_month_closed"
    assert replies(deps)[-1] == "11月の給与は確定済みのため、LINE からは記録・訂正できません。管理者に伝えてください。"
    assert len(requests(deps)) == 1


# ---------------------------------------------------------------- ポータルに未承認が出ない
def test_portal_sources_only_hold_approved_attendance(clock):
    """ポータル（sales/report/portal）が読むのは Asana の「Bar null 勤怠」「Bar null 給与」だけ。承認したものだけが載る。"""
    deps = labor_deps([att(11, 20, "21:00", "03:00"), att(11, 21, "21:00", "03:00"), att(11, 22, "21:00", "03:00"),
                       att(11, 28, "21:00", "03:00"), att(11, 21, "20:00", "03:00")])
    taro(deps)
    send(deps, clock, "2026-11-23 04:00", "A")                         # 11/20 期限切れ → 承認
    send(deps, clock, "2026-11-23 04:01", "B")                         # 11/21 → 承認
    send(deps, clock, "2026-11-23 04:02", "C")                         # 11/22 → 却下
    send(deps, clock, "2026-11-23 04:03", "D")                         # 11/28 未来 → 無反応
    react(deps, pend(deps, "A"))
    react(deps, pend(deps, "B"))
    react(deps, pend(deps, "C"), "x")
    run(deps, clock, "2026-11-23 05:00")
    send(deps, clock, "2026-11-23 06:00", "E")                         # 11/21 の訂正 → 無反応
    run(deps, clock, "2026-11-24 10:00")
    assert sorted((t["work_date"], t["hours"]) for t in att_tasks(deps)) == [("2026-11-20", 6), ("2026-11-21", 6)]
    assert [t["hours"] for t in pay_tasks(deps)] == [12]               # 見込みの給与も承認した 12h だけ
    assert set(waiting(deps)) == {"D", "E"}
    for t in att_tasks(deps):
        assert t["att_status"] == "記録済み"


# ---------------------------------------------------------------- 6時またぎ
def test_shift_across_6am_and_own_sales_after_approval(clock):
    deps = labor_deps([slip_msg(Entry(month=11, day=23, customer_name="顧客A", amount=30000)),
                       slip_msg(Entry(month=11, day=24, customer_name="顧客B", amount=7000)),
                       att(None, None, "22:00", "07:00")])
    taro(deps)
    handle_event(event("2026-11-24 05:30", "S1", user="U1"), "message", deps)          # 6時前に送った 11/23 の売上
    handle_event(event("2026-11-24 06:30", "S2", user="U1"), "message", deps)          # 6時過ぎに送った 11/24 の売上
    send(deps, clock, "2026-11-24 07:30", "E1")                                        # 日付なし → 11/23 のサイクル
    assert rec(deps) is None
    approve_all(deps)
    r = rec(deps)
    assert (r["business_date"], r["hours"], r["own_sales"]) == ("2026-11-23", 9, 30000)
    assert r["start"].startswith("2026-11-23T22:00") and r["end"].startswith("2026-11-24T07:00")


# ---------------------------------------------------------------- 障害とやり直し
def test_slack_failure_on_request_is_reposted_by_the_job(clock, monkeypatch):
    deps = labor_deps([att(11, 23, "21:00", "03:00")], reply_ack=True)
    taro(deps)
    real = deps.notifier.post

    def broken(channel, text, thread_ts=None, broadcast=False):
        if "【勤怠の承認依頼" in text:
            raise RuntimeError("Slack に届かない")
        return real(channel, text, thread_ts, broadcast)

    monkeypatch.setattr(deps.notifier, "post", broken)
    assert send(deps, clock, "2026-11-24 03:30", "E1")["status"] == "attendance_pending"
    assert requests(deps) == [] and len(replies(deps)) == 1 and pend(deps, "E1")["slack"] == {}
    monkeypatch.setattr(deps.notifier, "post", real)
    run(deps, clock, "2026-11-24 04:00")                                               # 30分ごとのジョブが出し直す
    assert len(requests(deps)) == 1 and len(replies(deps)) == 1
    approve_all(deps)
    assert rec(deps)["hours"] == 6


def test_retried_event_does_not_ask_or_reply_twice(clock):
    deps = labor_deps([att(11, 23, "21:00", "03:00")], reply_ack=True)
    send(deps, clock, "2026-11-24 03:30", "E1")
    line_event = deps.store.get_doc("line_events", "E1")
    line_event.pop("processed_at")                                                     # 途中で落ちて Cloud Tasks がやり直した
    deps.store.set_doc("line_events", "E1", line_event)
    assert send(deps, clock, "2026-11-24 03:30", "E1")["status"] == "attendance_pending"
    assert len(requests(deps)) == 1 and len(deps.line.replies) == 1


def test_approval_retried_after_a_crash_is_applied_once(clock):
    deps = labor_deps([att(11, 23, "21:00", "03:00")])
    send(deps, clock, "2026-11-24 03:30", "E1")
    approve_all(deps)
    ts = requests(deps)[0]["ts"]
    p = pend(deps, "E1")
    p["state"] = "waiting"                                                             # 記録の後、状態を保存する前に落ちた
    deps.store.set_doc("attendance_pending", "E1", p)
    run(deps, clock, "2026-11-24 05:00")
    assert rec(deps)["event_ids"] == ["E1"] and len(att_tasks(deps)) == 1
    assert "11月の稼働：1日・6h" in current(deps, ts)                                  # 記録の親メッセージを依頼の文面で上書きしない
    assert pushes(deps)[0][3] == pushes(deps)[-1][3]                                   # LINE は同じ retry_key（二重に届かない）


def test_line_announcements_are_batched_per_group(clock):
    days = [18, 19, 20, 21, 22, 23]
    deps = labor_deps([att(11, d, "21:00", "03:00") for d in days])
    for i, d in enumerate(days):
        send(deps, clock, f"2026-11-{d + 1} 04:00", f"E{i}")
    approve_all(deps)
    assert deps.line.push_calls == 2 and len(pushes(deps)) == 6                        # 5件 + 1件（無料枠は送信の回数で数える）
    assert [q for _, _, q, _ in pushes(deps)] == [f"QE{i}" for i in range(6)]


def test_line_failure_on_approval_is_noted_and_record_is_kept(clock):
    deps = labor_deps([att(11, 23, "21:00", "03:00")])
    send(deps, clock, "2026-11-24 03:30", "E1")
    deps.line.fail_push = True
    for p in waiting(deps).values():
        react(deps, p)
    assert run(deps, clock, "2026-11-24 04:00") == 1                                   # LINE に送れなくてもジョブは止めない
    assert rec(deps)["hours"] == 6
    assert "LINE への承認の連絡は送れませんでした（記録は済んでいます）。" in thread(deps, requests(deps)[0]["ts"])


# ---------------------------------------------------------------- 移行（前の版で作ったもの）
def test_pending_made_by_the_previous_version_is_reminded_daily_and_approved(clock):
    deps = labor_deps([att(11, 23, "21:00", "03:00")])
    taro(deps)
    s = sid(deps)
    old_ts = deps.notifier.post("CSTAFF", "<@UALERT> 【勤怠の承認依頼（期限切れ）】山田花子｜11/20（金）…")
    deps.store.set_doc("attendance_pending", "OLD1", {    # 前の版の形（record_id・permalink・reminded_on なし、reminded あり）
        "event_id": "OLD1", "state": "waiting", "staff_id": s, "business_date": "2026-11-20",
        "start": "2026-11-20T21:00:00+09:00", "end": "2026-11-21T03:00:00+09:00", "sent_at": "2026-11-23T01:00:00+00:00",
        "line_user_id": "U1", "display_name": "たろう", "group_id": "Cgroup", "quote_token": "QOLD", "text": "@null 11/20",
        "why": "期限切れ", "asked_at": "2026-11-23T01:00:05+00:00", "slack": {"channel_id": "CSTAFF", "ts": old_ts},
        "reminded": True})
    run(deps, clock, "2026-11-24 10:00")
    assert len(reminders(deps, old_ts)) == 1
    react(deps, old_ts)
    run(deps, clock, "2026-11-24 11:00")
    r = rec(deps, "20261120")
    assert r["hours"] == 6 and r["slack"]["ts"] == old_ts and att_tasks(deps)[0]["slack_url"]
    assert pushes(deps)[-1][2] == "QOLD"


def test_pending_made_by_the_previous_version_is_withdrawn_by_a_new_input(clock):
    deps = labor_deps([att(11, 23, "21:30", "03:00")])
    taro(deps)
    s = sid(deps)
    old_ts = deps.notifier.post("CSTAFF", "<@UALERT> 【勤怠の承認依頼（未来の日付）】…")
    deps.store.set_doc("attendance_pending", "OLD1", {
        "event_id": "OLD1", "state": "waiting", "staff_id": s, "business_date": "2026-11-23",
        "start": "2026-11-23T21:00:00+09:00", "end": "2026-11-24T03:00:00+09:00", "sent_at": "2026-11-22T12:00:00+00:00",
        "line_user_id": "U1", "display_name": "たろう", "group_id": "Cgroup", "quote_token": "QOLD", "text": "@null 11/23",
        "why": "未来の日付", "asked_at": "2026-11-22T12:00:05+00:00", "slack": {"channel_id": "CSTAFF", "ts": old_ts}})
    send(deps, clock, "2026-11-24 03:30", "E1")
    assert pend(deps, "OLD1")["state"] == "superseded"
    assert current(deps, old_ts).startswith("【取り下げ】山田花子｜11/23（月）　21:00〜03:00　6h（未来の日付）")
    approve_all(deps)
    assert rec(deps)["hours"] == 5.5


def test_records_made_before_the_change_stay_and_can_be_corrected(clock):
    """11/21〜22 の3件のように、前の版で承認なしに記録したものはそのまま残る（訂正は承認待ち）。"""
    deps = labor_deps([att(11, 21, "21:00", "02:00")])
    taro(deps)
    person = StaffIndex.load(deps.store).get(sid(deps))
    old = attendance.save_record(deps, person, datetime(2026, 11, 21).date(), at("2026-11-21 21:00"),
                                 at("2026-11-22 03:00"), event_id="OLDREC", line_user_id="U1", display_name="たろう",
                                 reason="LINE")
    assert old["hours"] == 6 and len(att_tasks(deps)) == 1
    stored = deps.store.get_doc("attendance", old["record_id"])          # 前の版は受け取ってすぐ記録した（11/22 の朝）
    stored["history"][-1]["at"] = "2026-11-21T18:40:00+00:00"
    deps.store.set_doc("attendance", old["record_id"], stored)
    run(deps, clock, "2026-11-24 10:00")                                               # 見回りで消えたり変わったりしない
    assert rec(deps, "20261121")["hours"] == 6 and len(att_tasks(deps)) == 1
    send(deps, clock, "2026-11-24 12:00", "E1")
    assert "（訂正・期限切れ）" in requests(deps)[0]["text"] and rec(deps, "20261121")["hours"] == 6
    approve_all(deps)
    assert rec(deps, "20261121")["hours"] == 5 and len(att_tasks(deps)) == 1


# ---------------------------------------------------------------- 同じ日の依頼が2つ残らない（レビュー1回目の指摘）
def old_pending(deps, eid, sent_utc, start, end, day="2026-11-23", user="U1"):
    """前の版が作った形の承認待ち（同じ日に2つあっても取り下げていなかった）。"""
    ts = deps.notifier.post("CSTAFF", f"<@UALERT> 【勤怠の承認依頼（期限切れ）】… {eid}")
    deps.store.set_doc("attendance_pending", eid, {
        "event_id": eid, "state": "waiting", "staff_id": sid(deps, user), "business_date": day,
        "start": start, "end": end, "sent_at": sent_utc, "line_user_id": user, "display_name": "たろう",
        "group_id": "Cgroup", "quote_token": "Q" + eid, "text": "@null " + eid, "why": "期限切れ",
        "asked_at": sent_utc, "slack": {"channel_id": "CSTAFF", "ts": ts}})
    return ts


def test_crash_before_withdrawing_the_old_request_is_finished_by_the_retry(clock, monkeypatch):
    deps = labor_deps([att(11, 23, "21:00", "03:00"), att(11, 23, "21:30", "03:00")], reply_ack=True)
    send(deps, clock, "2026-11-24 03:30", "E1")
    real = deps.store.set_doc
    failed = {"n": 0}

    def flaky(collection, doc_id, data):
        if collection == "attendance_pending" and doc_id == "E1" and data.get("state") == "superseded" and not failed["n"]:
            failed["n"] += 1
            raise RuntimeError("Firestore が一瞬つながらない")
        return real(collection, doc_id, data)

    monkeypatch.setattr(deps.store, "set_doc", flaky)
    ev = event("2026-11-24 04:00", "E2", user="U1")
    ev["replyToken"], ev["message"]["quoteToken"] = "RE2", "QE2"
    clock["t"] = at("2026-11-24 04:00")
    with pytest.raises(RuntimeError):
        handle_event(ev, "message", deps, attempt=1)                  # E2 は保存できたが、E1 の取り下げで落ちた
    assert pend(deps, "E2")["state"] == "waiting" and pend(deps, "E1")["state"] == "waiting"
    assert handle_event(ev, "message", deps, attempt=2)["status"] == "attendance_pending"   # Cloud Tasks のやり直し
    assert pend(deps, "E1")["state"] == "superseded" and pend(deps, "E2")["state"] == "waiting"
    assert len(requests(deps)) == 2 and len(replies(deps)) == 2         # E2 の依頼と返信は1回だけ


def test_approving_an_older_request_after_a_newer_one_does_not_revert_the_record(clock):
    deps = labor_deps()
    taro(deps)
    ts1 = old_pending(deps, "OLD1", "2026-11-23T18:30:00+00:00", "2026-11-23T21:00:00+09:00", "2026-11-24T03:00:00+09:00")
    ts2 = old_pending(deps, "OLD2", "2026-11-23T19:00:00+00:00", "2026-11-23T21:30:00+09:00", "2026-11-24T03:00:00+09:00")
    react(deps, ts2)
    run(deps, clock, "2026-11-24 05:00")                               # 新しい方を承認 → 古い方は取り下げ
    assert pend(deps, "OLD1")["state"] == "superseded" and pend(deps, "OLD2")["state"] == "approved"
    assert current(deps, ts1).startswith("【取り下げ】")
    react(deps, ts1)
    run(deps, clock, "2026-11-24 05:30")                               # 古い方の ✅ は数えない
    assert rec(deps)["hours"] == 5.5 and rec(deps)["event_ids"] == ["OLD2"]
    run(deps, clock, "2026-11-25 10:00")
    assert reminders(deps, ts1) == []
    # 記録に当てた入力より古い依頼が残っていた（どこかで取り下げ損ねた）→ 承認されても当てない
    ts0 = old_pending(deps, "OLD0", "2026-11-23T18:00:00+00:00", "2026-11-23T20:00:00+09:00", "2026-11-24T03:00:00+09:00")
    react(deps, ts0)
    run(deps, clock, "2026-11-25 11:00")
    assert pend(deps, "OLD0")["state"] == "superseded" and rec(deps)["hours"] == 5.5


def test_line_event_during_the_job_does_not_bring_back_a_withdrawn_request(clock, monkeypatch):
    deps = labor_deps([att(11, 23, "21:00", "03:00"), att(11, 23, "21:30", "03:00")])
    send(deps, clock, "2026-11-24 03:30", "E1")
    ts1 = requests(deps)[0]["ts"]
    react(deps, ts1)                                                   # 桝本は E1 に ✅ を付けていた
    real_find = deps.store.find_docs
    fired = {"done": False}

    def find_then_event(collection, field, op, value):
        out = real_find(collection, field, op, value)
        if collection == "attendance_pending" and field == "state" and not fired["done"]:
            fired["done"] = True                                       # ジョブが一覧を読んだ直後に、LINE の E2 が処理された
            send(deps, clock, "2026-11-25 10:00", "E2")
        return out

    monkeypatch.setattr(deps.store, "find_docs", find_then_event)
    run(deps, clock, "2026-11-25 10:00")
    assert pend(deps, "E1")["state"] == "superseded" and pend(deps, "E2")["state"] == "waiting"
    assert rec(deps) is None and reminders(deps, ts1) == []
    assert current(deps, ts1).startswith("【取り下げ】")


def test_line_announcement_left_by_a_crash_is_sent_on_the_next_run(clock, monkeypatch):
    deps = labor_deps([att(11, 23, "21:00", "03:00")])
    send(deps, clock, "2026-11-24 03:30", "E1")
    react(deps, pend(deps, "E1"))
    monkeypatch.setattr(attendance, "_announce", lambda deps: None)   # 承認を保存した後、LINE に送る前に止まった
    run(deps, clock, "2026-11-24 04:00")
    assert rec(deps)["hours"] == 6 and pushes(deps) == [] and pend(deps, "E1")["announce_pending"] is True
    monkeypatch.undo()
    monkeypatch.setattr(attendance, "_now", lambda deps: clock["t"])
    run(deps, clock, "2026-11-24 04:30")
    assert [p[1] for p in pushes(deps)] == ["たろうさんの 11/23 の勤怠（21:00〜03:00）が承認され、記録しました。"]
    assert pend(deps, "E1")["announce_pending"] is False and pend(deps, "E1")["announced_at"]
    run(deps, clock, "2026-11-24 05:00")
    assert len(pushes(deps)) == 1


def test_approval_in_the_run_at_confirmation_time_lands_before_the_confirm(clock):
    """30分ごとのジョブの順番（attendance_approvals → payroll）: 確定の回に承認したものも、確定の前に給与に入る。"""
    deps = labor_deps([att(11, 23, "21:00", "03:00")])
    taro(deps)
    send(deps, clock, "2026-11-24 03:30", "E1")
    clock["t"] = at("2026-12-02 17:10")
    assert payroll.step(deps)["calculated"] == 1
    react(deps, pend(deps, "E1"))
    clock["t"] = at("2026-12-10 09:05")
    steps = [st for st in jobs.RECONCILE_STEPS if st[0] in ("attendance_approvals", "payroll")]
    assert [name for name, _ in steps] == ["attendance_approvals", "payroll"]
    assert jobs.run_steps(deps, steps)
    doc = next(iter(deps.store.find_docs("payroll", "month", "==", "2026-11").values()))
    assert (doc["hours"], doc["status"]) == (6, "確定") and rec(deps)["hours"] == 6


# ---------------------------------------------------------------- 承認の途中で落ちたとき（レビュー2回目の指摘）
def crash_on(monkeypatch, deps, eid, **match):
    """依頼 eid を match の内容で保存するところで、1回だけ落ちる（インスタンスが止まった・Firestore の失敗）。"""
    real = deps.store.set_doc
    done = {"n": 0}

    def flaky(collection, doc_id, data):
        if (collection == "attendance_pending" and doc_id == eid and not done["n"]
                and all(data.get(k) == v for k, v in match.items())):
            done["n"] += 1
            raise RuntimeError("ここで落ちた")
        return real(collection, doc_id, data)

    monkeypatch.setattr(deps.store, "set_doc", flaky)


@pytest.mark.parametrize("after", ["correction", "reaction_removed", "rejected_later"])
def test_crash_after_recording_but_before_saving_the_approval(clock, monkeypatch, after):
    deps = labor_deps([att(11, 23, "21:00", "03:00"), att(11, 23, "21:30", "03:00")])
    send(deps, clock, "2026-11-24 03:30", "E1")
    ts = requests(deps)[0]["ts"]
    react(deps, ts)
    crash_on(monkeypatch, deps, "E1", state="approved")
    with pytest.raises(JobFailed):
        run(deps, clock, "2026-11-24 04:00")                          # 記録はできたが、承認の状態を保存する前に落ちた
    assert rec(deps)["event_ids"] == ["E1"] and pend(deps, "E1")["state"] == "waiting"
    if after == "correction":                                         # 次の回の前に訂正が届いた
        assert send(deps, clock, "2026-11-24 04:10", "E2")["status"] == "attendance_pending"
        assert "（訂正）" in requests(deps)[1]["text"]
    elif after == "reaction_removed":
        deps.notifier.reacted[ts] = {}
    else:
        react(deps, ts, "x")
    assert not current(deps, ts).startswith("【")                     # 記録の親メッセージは書き換えていない
    run(deps, clock, "2026-11-25 10:00")
    assert pend(deps, "E1")["state"] == "approved" and rec(deps)["event_ids"] == ["E1"]
    assert "11月の稼働：1日・6h" in current(deps, ts)                 # 【取り下げ】【却下】にならない
    assert reminders(deps, ts) == [] and thread(deps, ts) == ["承認しました。記録しました。"]
    assert [p[1] for p in pushes(deps)] == ["たろうさんの 11/23 の勤怠（21:00〜03:00）が承認され、記録しました。"]
    if after == "correction":
        assert pend(deps, "E2")["state"] == "waiting" and len(reminders(deps, requests(deps)[1]["ts"])) == 1


def test_crash_after_the_line_push_does_not_send_it_twice(clock, monkeypatch):
    deps = labor_deps([att(11, 23, "21:00", "03:00"), att(11, 22, "21:00", "03:00")])
    send(deps, clock, "2026-11-24 03:30", "A")
    react(deps, pend(deps, "A"))
    crash_on(monkeypatch, deps, "A", announce_pending=False)
    with pytest.raises(JobFailed):
        run(deps, clock, "2026-11-24 04:00")                          # LINE に送れた後、「送った」を保存する前に落ちた
    assert len(pushes(deps)) == 1 and pend(deps, "A")["announce_pending"] is True
    send(deps, clock, "2026-11-24 04:10", "B")
    react(deps, pend(deps, "B"))
    run(deps, clock, "2026-11-24 04:30")                              # 次の回: A は同じ組・同じ key、B は別の組
    assert [p[2] for p in pushes(deps)] == ["QA", "QB"] and deps.line.push_calls == 2
    assert pend(deps, "A")["announce_pending"] is False and pend(deps, "B")["announce_pending"] is False


def test_old_future_request_does_not_overwrite_a_newer_old_record(clock):
    """前の版: 未来の日付の依頼が残ったまま、その日の期限内の入力が直接記録された。新しい版では古い依頼を当てない。"""
    deps = labor_deps()
    taro(deps)
    ts = old_pending(deps, "OLDF", "2026-11-20T03:00:00+00:00", "2026-11-23T21:00:00+09:00", "2026-11-24T03:00:00+09:00")
    person = StaffIndex.load(deps.store).get(sid(deps))
    old = attendance.save_record(deps, person, datetime(2026, 11, 23).date(), at("2026-11-23 22:00"),
                                 at("2026-11-24 04:00"), event_id="OLDREC", line_user_id="U1", display_name="たろう",
                                 reason="LINE")
    stored = deps.store.get_doc("attendance", old["record_id"])
    stored["history"][-1]["at"] = "2026-11-23T19:05:00+00:00"           # 11/24 04:05 に記録（前の版）
    deps.store.set_doc("attendance", old["record_id"], stored)
    react(deps, ts)
    run(deps, clock, "2026-11-26 11:00")
    assert pend(deps, "OLDF")["state"] == "superseded" and current(deps, ts).startswith("【取り下げ】")
    assert rec(deps)["hours"] == 6 and rec(deps)["start"].startswith("2026-11-23T22:00")


def test_crash_before_the_slack_follow_up_is_finished_on_the_next_run(clock, monkeypatch):
    deps = labor_deps([att(11, 23, "21:00", "03:00"), att(11, 23, "21:30", "03:00")])
    send(deps, clock, "2026-11-24 03:30", "E1")
    approve_all(deps)
    send(deps, clock, "2026-11-24 12:00", "E2")
    ts2 = requests(deps)[1]["ts"]
    react(deps, ts2)
    monkeypatch.setattr(attendance, "_run_finish", lambda deps, eid, pend: None)   # 承認を保存した後、後始末の前に止まった
    run(deps, clock, "2026-11-24 12:30")
    assert pend(deps, "E2")["state"] == "approved" and pend(deps, "E2")["finish_pending"] is True
    assert current(deps, ts2).startswith("<@UALERT> 【勤怠の承認依頼（訂正）】")
    monkeypatch.undo()
    monkeypatch.setattr(attendance, "_now", lambda deps: clock["t"])
    run(deps, clock, "2026-11-24 13:00")
    assert current(deps, ts2).startswith("【承認済み】") and thread(deps, ts2) == ["承認しました。記録を直しました。"]
    assert pend(deps, "E2")["finish_pending"] is False
    run(deps, clock, "2026-11-24 13:30")
    assert thread(deps, ts2) == ["承認しました。記録を直しました。"]


def test_reminder_for_a_correction_shows_the_current_record(clock):
    deps = labor_deps([att(11, 23, "21:00", "03:00"), att(11, 23, "21:30", "03:00")])
    send(deps, clock, "2026-11-24 03:30", "E1")
    approve_all(deps)
    send(deps, clock, "2026-11-24 12:00", "E2")
    run(deps, clock, "2026-11-25 10:00")
    [r] = reminders(deps, requests(deps)[1]["ts"])
    assert r.endswith("\n今の記録：21:00〜03:00 6h（承認すると、この依頼の内容に置き換わります）")


# ---------------------------------------------------------------- レビュー3回目の指摘
def test_crash_inside_the_save_still_recalculates_payroll_and_posts_the_correction(clock, monkeypatch):
    deps = labor_deps([att(11, 23, "21:00", "03:00"), att(11, 23, "21:30", "03:00")])
    taro(deps)
    send(deps, clock, "2026-11-24 03:30", "E1")
    approve_all(deps)
    clock["t"] = at("2026-12-02 17:10")
    assert payroll.step(deps)["calculated"] == 1
    send(deps, clock, "2026-12-03 09:00", "E2")                       # 計算済みの月の訂正
    react(deps, pend(deps, "E2"))
    real_sync = attendance.sync
    hit = {"n": 0}

    def dying_sync(deps, rec, person=None, **kw):
        if not hit["n"]:
            hit["n"] += 1
            raise RuntimeError("記録を保存した直後に止まった")
        return real_sync(deps, rec, person, **kw)

    monkeypatch.setattr(attendance, "sync", dying_sync)
    with pytest.raises(JobFailed):
        run(deps, clock, "2026-12-03 10:00")
    assert rec(deps)["hours"] == 5.5 and rec(deps)["after_save"] and pend(deps, "E2")["state"] == "waiting"
    doc = next(iter(deps.store.find_docs("payroll", "month", "==", "2026-11").values()))
    assert doc["hours"] == 6                                          # まだ計算し直していない
    run(deps, clock, "2026-12-03 10:30")                              # 次の回が続きをする
    doc = next(iter(deps.store.find_docs("payroll", "month", "==", "2026-11").values()))
    assert doc["hours"] == 5.5 and doc["status"] == "計算済み" and rec(deps)["after_save"] is None
    first_ts = requests(deps)[0]["ts"]
    assert any(t.startswith("訂正：21:00〜03:00 6h → 21:30〜03:00 5.5h") for t in thread(deps, first_ts))
    assert pend(deps, "E2")["state"] == "approved" and current(deps, requests(deps)[1]["ts"]).startswith("【承認済み】")


class CountingStore:
    """読んだ文書の数を数える（Firestore の読み取りの無料枠: 1日5万）。"""

    def __init__(self, store):
        self.store, self.reads = store, 0

    def __getattr__(self, name):
        return getattr(self.store, name)

    def get_doc(self, collection, doc_id):
        self.reads += 1
        return self.store.get_doc(collection, doc_id)

    def find_docs(self, collection, field, op, value):
        out = self.store.find_docs(collection, field, op, value)
        self.reads += max(1, len(out))
        return out


def reads_with_history(clock, history: int) -> tuple[int, int]:
    deps = labor_deps([att(11, 29, "21:00", "03:00")])
    taro(deps)
    s = sid(deps)
    for i in range(history):                                          # 過去に決まった依頼（消さないので増え続ける）
        deps.store.set_doc("attendance_pending", f"H{i}", {"event_id": f"H{i}", "state": "approved", "staff_id": s,
                                                           "business_date": "2026-10-01", "sent_at": "2026-10-02T00:00:00+00:00"})
    for day in range(10, 20):                                         # 承認待ちが10件たまっている
        old_pending(deps, f"W{day}", f"2026-11-{day}T19:00:00+00:00", f"2026-11-{day}T21:00:00+09:00",
                    f"2026-11-{day + 1}T03:00:00+09:00", day=f"2026-11-{day}")
    run(deps, clock, "2026-11-20 08:30")                              # 1回目: 前の版の依頼に record_id を付ける（一度だけ）
    assert all(p.get("record_id") for p in waiting(deps).values())
    counting = CountingStore(deps.store)
    deps.store = counting
    run(deps, clock, "2026-11-20 09:00")                              # 10時前: リマインドなし
    job = counting.reads
    counting.reads = 0
    send(deps, clock, "2026-11-30 04:00", "E1")
    return job, counting.reads


def test_reads_do_not_grow_with_past_requests(clock):
    assert reads_with_history(clock, 0) == reads_with_history(clock, 300)
    job, msg = reads_with_history(clock, 300)
    assert job <= 60 and msg <= 40                                    # 30分ごと × 48回でも数千/日


def test_newer_waiting_request_is_kept_when_the_older_one_was_approved_by_the_previous_version(clock):
    """前の版: 期限外の A（10:00 送信）と訂正 B（11:00 送信）が承認待ち → 12:00 に A を承認（記録の時刻は 12:00）。"""
    deps = labor_deps()
    taro(deps)
    person = StaffIndex.load(deps.store).get(sid(deps))
    old_pending(deps, "A", "2026-11-26T01:00:00+00:00", "2026-11-23T21:00:00+09:00", "2026-11-24T03:00:00+09:00")
    a = pend(deps, "A")
    a["state"] = "approved"
    deps.store.set_doc("attendance_pending", "A", a)
    old = attendance.save_record(deps, person, datetime(2026, 11, 23).date(), at("2026-11-23 21:00"),
                                 at("2026-11-24 03:00"), event_id="A", line_user_id="U1", display_name="たろう",
                                 reason="承認（期限外）")
    stored = deps.store.get_doc("attendance", old["record_id"])
    stored.pop("applied_sent_at", None)
    stored["history"][-1]["at"] = "2026-11-26T03:00:00+00:00"
    deps.store.set_doc("attendance", old["record_id"], stored)
    ts_b = old_pending(deps, "B", "2026-11-26T02:00:00+00:00", "2026-11-23T21:30:00+09:00", "2026-11-24T03:00:00+09:00")
    run(deps, clock, "2026-11-26 13:00")
    assert pend(deps, "B")["state"] == "waiting"                      # 後から送った B は取り下げない
    react(deps, ts_b)
    run(deps, clock, "2026-11-26 13:30")
    assert rec(deps)["hours"] == 5.5 and rec(deps)["event_ids"] == ["A", "B"]


def test_line_notice_can_be_turned_off(clock):
    deps = labor_deps([att(11, 23, "21:00", "03:00")], reply_ack=True)
    deps.settings = replace(deps.settings, labor=replace(deps.settings.labor, approval_line_notice=False))
    send(deps, clock, "2026-11-24 03:30", "E1")
    assert replies(deps) == ["11/23 の勤怠（21:00〜03:00 6時間）を受け付けました。管理者が承認したら記録します。"]
    approve_all(deps)
    assert rec(deps)["hours"] == 6 and pushes(deps) == []


# ---------------------------------------------------------------- レビュー4回目の指摘（同じ内容の送り直しも「後の入力」として数える）
def test_delayed_older_input_does_not_win_over_a_resend_matching_the_record(clock):
    """E1 を承認 → 12:00 に E2（21:30）、12:10 に E3（元の 21:00）。E2 が失敗して遅れ、E3 が先に処理された。"""
    deps = labor_deps([att(11, 23, "21:00", "03:00"), att(11, 23, "21:00", "03:00"), att(11, 23, "21:30", "03:00")],
                      reply_ack=True)
    send(deps, clock, "2026-11-24 03:30", "E1")
    approve_all(deps)
    assert send(deps, clock, "2026-11-24 12:10", "E3")["status"] == "attendance_unchanged"
    assert send(deps, clock, "2026-11-24 12:00", "E2")["status"] == "attendance_superseded"     # 遅れて届いた E2
    assert len(requests(deps)) == 1 and rec(deps)["hours"] == 6
    assert replies(deps)[-1].endswith("より後に送られた同じ日の勤怠があるため、この入力は使いません。")


def test_delayed_older_input_does_not_win_over_a_resend_matching_the_waiting_request(clock):
    """E1（A, 03:30）が承認待ち → E3（A, 03:45）は同じ内容 → 遅れて E2（B, 03:40）が届いた。"""
    deps = labor_deps([att(11, 23, "21:00", "03:00"), att(11, 23, "21:00", "03:00"), att(11, 23, "21:30", "03:00")])
    send(deps, clock, "2026-11-24 03:30", "E1")
    assert send(deps, clock, "2026-11-24 03:45", "E3")["status"] == "attendance_pending"
    assert send(deps, clock, "2026-11-24 03:40", "E2")["status"] == "attendance_superseded"
    assert pend(deps, "E1")["state"] == "waiting" and len(requests(deps)) == 1
    approve_all(deps)
    r = rec(deps)
    assert r["hours"] == 6 and datetime.fromisoformat(r["applied_sent_at"]) == at("2026-11-24 03:45")


def test_resend_matching_a_stale_old_request_gets_a_new_request(clock):
    """前の版: 未来の日付の依頼（21:00）が残ったまま、22:00 の入力が直接記録された。そこに 21:00 の入力が来た。"""
    deps = labor_deps([att(11, 23, "21:00", "03:00")], reply_ack=True)
    taro(deps)
    ts_old = old_pending(deps, "OLDF", "2026-11-20T03:00:00+00:00", "2026-11-23T21:00:00+09:00", "2026-11-24T03:00:00+09:00")
    person = StaffIndex.load(deps.store).get(sid(deps))
    old = attendance.save_record(deps, person, datetime(2026, 11, 23).date(), at("2026-11-23 22:00"),
                                 at("2026-11-24 04:00"), event_id="OLDREC", line_user_id="U1", display_name="たろう",
                                 reason="LINE")
    stored = deps.store.get_doc("attendance", old["record_id"])
    stored["history"][-1]["at"] = "2026-11-23T19:05:00+00:00"
    deps.store.set_doc("attendance", old["record_id"], stored)
    assert send(deps, clock, "2026-11-25 12:00", "E1")["status"] == "attendance_pending"
    assert pend(deps, "OLDF")["state"] == "superseded" and pend(deps, "E1")["state"] == "waiting"
    new = requests(deps)[-1]
    assert "【勤怠の承認依頼（訂正・期限切れ）】" in new["text"] and current(deps, ts_old).startswith("【取り下げ】")
    run(deps, clock, "2026-11-26 10:00")
    assert len(reminders(deps, new["ts"])) == 1                      # 依頼が消えずに残り、リマインドされる
    approve_all(deps)
    assert rec(deps)["start"].startswith("2026-11-23T21:00")


def test_payroll_notice_counts_pending_corrections_separately(clock):
    deps = labor_deps([att(11, 23, "21:00", "03:00"), att(11, 23, "21:30", "03:00"), att(11, 24, "21:00", "03:00")])
    taro(deps)
    send(deps, clock, "2026-11-24 03:30", "E1")
    approve_all(deps)
    send(deps, clock, "2026-11-24 12:00", "E2")                       # 11/23 の訂正（承認待ち）
    send(deps, clock, "2026-11-25 04:00", "E3")                       # 11/24（承認待ち）
    clock["t"] = at("2026-12-02 17:10")
    payroll.step(deps)
    calc = next(t for t in staff_texts(deps) if "11月分の給与を計算しました" in t)
    assert calc.endswith("\n承認待ちの勤怠が1件あり、給与に入っていません。承認待ちの訂正が1件あり、前の記録のまま計算しています。"
                         "10日 09:00 の確定までに承認すれば、計算し直します。")
    doc = next(iter(deps.store.find_docs("payroll", "month", "==", "2026-11").values()))
    assert doc["hours"] == 6


# ---------------------------------------------------------------- レビュー5回目の指摘（失敗した後始末はやり直す・却下された後の入力も数える）
def test_slack_failure_while_withdrawing_is_retried(clock, monkeypatch):
    deps = labor_deps([att(11, 23, "21:00", "03:00"), att(11, 23, "21:30", "03:00")])
    send(deps, clock, "2026-11-24 03:30", "E1")
    ts1 = requests(deps)[0]["ts"]
    real = deps.notifier.update
    monkeypatch.setattr(deps.notifier, "update", lambda *a: (_ for _ in ()).throw(RuntimeError("Slack が落ちている")))
    send(deps, clock, "2026-11-24 04:00", "E2")
    assert pend(deps, "E1")["state"] == "superseded" and pend(deps, "E1")["finish_pending"] is True
    assert not current(deps, ts1).startswith("【取り下げ】")
    monkeypatch.setattr(deps.notifier, "update", real)
    run(deps, clock, "2026-11-24 04:30")
    assert current(deps, ts1).startswith("【取り下げ】") and pend(deps, "E1")["finish_pending"] is False
    assert len([t for t in thread(deps, ts1) if "取り下げました" in t]) == 1


def test_slack_follow_up_gives_up_after_the_limit(clock, monkeypatch):
    deps = labor_deps([att(11, 23, "21:00", "03:00")])
    send(deps, clock, "2026-11-24 03:30", "E1")
    react(deps, pend(deps, "E1"), "x")
    monkeypatch.setattr(attendance, "FINISH_TRIES", 3)
    monkeypatch.setattr(deps.notifier, "update", lambda *a: (_ for _ in ()).throw(RuntimeError("投稿が消された")))
    for when in ("2026-11-24 04:00", "2026-11-24 04:30", "2026-11-24 05:00", "2026-11-24 05:30"):
        run(deps, clock, when)
    p = pend(deps, "E1")
    assert p["state"] == "rejected" and p["finish_pending"] is False and p["finish_failed"] and p["finish_tries"] == 3


def test_payroll_recalculation_failure_after_approval_is_retried_before_confirm(clock, monkeypatch):
    deps = labor_deps([att(11, 23, "21:00", "03:00"), att(11, 24, "21:00", "03:00")])
    taro(deps)
    send(deps, clock, "2026-11-24 03:30", "E1")
    approve_all(deps)
    send(deps, clock, "2026-11-25 04:00", "E2")                       # 11/24 は承認待ちのまま計算日を迎えた
    clock["t"] = at("2026-12-02 17:10")
    payroll.step(deps)
    react(deps, pend(deps, "E2"))
    real = payroll.on_change
    hit = {"n": 0}

    def flaky(*a, **kw):
        if not hit["n"]:
            hit["n"] += 1
            raise RuntimeError("Firestore が一瞬つながらない")
        return real(*a, **kw)

    monkeypatch.setattr(payroll, "on_change", flaky)
    run(deps, clock, "2026-12-03 10:00")                              # 承認で記録できたが、給与の計算し直しに失敗
    assert rec(deps, "20261124")["after_save_pending"] is True
    doc = next(iter(deps.store.find_docs("payroll", "month", "==", "2026-11").values()))
    assert doc["hours"] == 6
    steps = [st for st in jobs.RECONCILE_STEPS if st[0] in ("attendance_approvals", "attendance_resync", "payroll")]
    clock["t"] = at("2026-12-03 10:30")
    assert jobs.run_steps(deps, steps)                                # 次の回の attendance_resync がやり直す
    doc = next(iter(deps.store.find_docs("payroll", "month", "==", "2026-11").values()))
    assert doc["hours"] == 12 and rec(deps, "20261124")["after_save_pending"] is False


def test_delayed_older_input_is_not_used_after_a_newer_one_was_rejected(clock):
    deps = labor_deps([att(11, 23, "21:00", "03:00"), att(11, 23, "21:30", "03:00"), att(11, 23, "20:00", "03:00")],
                      reply_ack=True)
    send(deps, clock, "2026-11-24 03:30", "E1")
    send(deps, clock, "2026-11-24 04:00", "E2")                       # E1 は取り下げ
    react(deps, pend(deps, "E2"), "x")
    run(deps, clock, "2026-11-24 04:30")                              # E2 は却下
    assert send(deps, clock, "2026-11-24 03:00", "E0")["status"] == "attendance_superseded"   # 失敗して遅れた E0
    assert waiting(deps) == {} and len(requests(deps)) == 2 and rec(deps) is None


# ---------------------------------------------------------------- レビュー6回目の注記
def test_request_not_yet_posted_is_closed_without_posting_when_the_month_is_confirmed(clock, monkeypatch):
    deps = labor_deps([att(11, 23, "21:00", "03:00")])
    real = deps.notifier.post

    def broken(channel, text, thread_ts=None, broadcast=False):
        if "【勤怠の承認依頼" in text:
            raise RuntimeError("Slack に届かない")
        return real(channel, text, thread_ts, broadcast)

    monkeypatch.setattr(deps.notifier, "post", broken)
    send(deps, clock, "2026-12-10 08:55", "E1")                       # 確定の直前の入力。Slack に出せなかった
    monkeypatch.setattr(deps.notifier, "post", real)
    deps.store.set_doc("payroll_runs", "2026-11", {"calculated_at": "x", "confirmed_at": "y"})
    run(deps, clock, "2026-12-10 09:30")
    assert pend(deps, "E1")["state"] == "closed" and requests(deps) == []      # 閉じる依頼をわざわざ出さない
    [notice] = [t for t in staff_texts(deps) if "確定済みのため記録しませんでした" in t]   # 閉じたことは桝本に知らせる
    assert notice.startswith("<@UALERT> 【勤怠：11月の給与は確定済みのため記録しませんでした（11/23（月）　21:00〜03:00）。")


def test_retried_older_request_is_withdrawn_when_a_newer_resend_arrived_meanwhile(clock, monkeypatch):
    deps = labor_deps([att(11, 23, "21:00", "03:00"), att(11, 23, "21:30", "03:00"), att(11, 23, "21:00", "03:00")],
                      reply_ack=True)
    send(deps, clock, "2026-11-24 03:30", "E1")                       # A（承認待ち）
    real_settle = attendance._settle
    hit = {"n": 0}

    def dying(*a, **kw):
        if not hit["n"]:
            hit["n"] += 1
            raise RuntimeError("依頼を保存した直後に止まった")
        return real_settle(*a, **kw)

    monkeypatch.setattr(attendance, "_settle", dying)
    ev = event("2026-11-24 03:40", "E2", user="U1")                   # B
    ev["replyToken"], ev["message"]["quoteToken"] = "RE2", "QE2"
    clock["t"] = at("2026-11-24 03:40")
    with pytest.raises(RuntimeError):
        handle_event(ev, "message", deps, attempt=1)
    send(deps, clock, "2026-11-24 03:45", "E3")                       # また A（同じ内容）
    assert handle_event(ev, "message", deps, attempt=2)["status"] == "attendance_superseded"
    assert pend(deps, "E2")["state"] == "superseded" and pend(deps, "E1")["state"] == "waiting"
    assert len(requests(deps)) == 1
    assert replies(deps)[-1].endswith("より後に送られた同じ日の勤怠があるため、この入力は使いません。")
    approve_all(deps)
    assert rec(deps)["hours"] == 6


# ---------------------------------------------------------------- レビュー7回目の指摘
def test_rejected_correction_says_the_record_is_unchanged(clock):
    deps = labor_deps([att(11, 23, "21:00", "03:00"), att(11, 23, "21:30", "03:00")])
    send(deps, clock, "2026-11-24 03:30", "E1")
    approve_all(deps)
    send(deps, clock, "2026-11-24 12:00", "E2")
    ts2 = requests(deps)[1]["ts"]
    react(deps, ts2, "x")
    run(deps, clock, "2026-11-24 12:30")
    assert current(deps, ts2).startswith("【却下】たろう｜11/23（月）　21:30〜03:00　5.5h（訂正）\n"
                                         "記録（この依頼では変えていません）：21:00〜03:00 6h")
    assert rec(deps)["hours"] == 6


def test_retry_after_approval_does_not_use_the_reply_token_again(clock):
    deps = labor_deps([att(11, 23, "21:00", "03:00")], reply_ack=True)
    send(deps, clock, "2026-11-24 03:30", "E1")
    line_event = deps.store.get_doc("line_events", "E1")
    line_event.pop("processed_at")                                     # 返信した後、処理済みにする前に落ちていた
    deps.store.set_doc("line_events", "E1", line_event)
    approve_all(deps)
    assert send(deps, clock, "2026-11-24 03:30", "E1")["status"] == "attendance"
    assert len(deps.line.replies) == 1                                 # OK スタンプで同じ返信トークンを使わない


def test_same_content_resend_of_a_waiting_correction_keeps_the_correction_wording(clock):
    deps = labor_deps([att(11, 23, "21:00", "03:00"), att(11, 23, "21:30", "03:00"), att(11, 23, "21:30", "03:00")],
                      reply_ack=True)
    send(deps, clock, "2026-11-24 03:30", "E1")
    approve_all(deps)
    send(deps, clock, "2026-11-24 12:00", "E2")
    send(deps, clock, "2026-11-24 12:05", "E3")
    assert replies(deps)[-1] == ("11/23 の勤怠（21:30〜03:00 5.5時間）は、同じ内容で承認待ちです。"
                                 "管理者が承認したら、前の記録と置き換えて、お知らせします。")


def test_deleted_request_post_is_posted_again(clock, monkeypatch):
    from barnull_sales.http import ExternalAPIError

    deps = labor_deps([att(11, 23, "21:00", "03:00")])
    send(deps, clock, "2026-11-24 03:30", "E1")
    gone = requests(deps)[0]["ts"]
    real = deps.notifier.reactions

    def reactions(channel, ts):
        if ts == gone:
            raise ExternalAPIError("Slack", "reactions.get", 200, "message_not_found")
        return real(channel, ts)

    monkeypatch.setattr(deps.notifier, "reactions", reactions)
    run(deps, clock, "2026-11-24 12:00")                              # 依頼の投稿が消されていた → 出し直す（ジョブは失敗しない）
    new = requests(deps)[-1]
    assert new["ts"] != gone and pend(deps, "E1")["slack"]["ts"] == new["ts"] and pend(deps, "E1")["reposted"] == 1
    assert new["text"].startswith("<@UALERT> 【勤怠の承認依頼】")
    react(deps, new)
    run(deps, clock, "2026-11-24 12:30")
    assert rec(deps)["hours"] == 6


# ---------------------------------------------------------------- レビュー8回目の指摘
def test_retry_after_the_month_was_confirmed_closes_instead_of_asking(clock, monkeypatch):
    deps = labor_deps([att(11, 30, "21:00", "03:00")], reply_ack=True)
    real_settle = attendance._settle
    hit = {"n": 0}

    def dying(*a, **kw):
        if not hit["n"]:
            hit["n"] += 1
            raise RuntimeError("依頼を保存した直後に止まった")
        return real_settle(*a, **kw)

    monkeypatch.setattr(attendance, "_settle", dying)
    ev = event("2026-12-10 08:58", "E1", user="U1")
    ev["replyToken"], ev["message"]["quoteToken"] = "RE1", "QE1"
    clock["t"] = at("2026-12-10 08:58")
    with pytest.raises(RuntimeError):
        handle_event(ev, "message", deps, attempt=1)
    deps.store.set_doc("payroll_runs", "2026-11", {"calculated_at": "x", "confirmed_at": "y"})   # 09:00 に確定
    clock["t"] = at("2026-12-10 09:02")
    assert handle_event(ev, "message", deps, attempt=2)["status"] == "attendance_month_closed"
    assert pend(deps, "E1")["state"] == "closed" and requests(deps) == []
    assert replies(deps) == ["11月の給与は確定済みのため、LINE からは記録・訂正できません。管理者に伝えてください。"]
    assert any("【勤怠：11月の給与は確定済みのため記録しませんでした" in t for t in staff_texts(deps))
    run(deps, clock, "2026-12-10 09:30")
    assert requests(deps) == [] and rec(deps, "20261130") is None


# ---------------------------------------------------------------- レビュー9回目の指摘
def test_old_waiting_request_is_withdrawn_when_a_later_old_request_was_rejected(clock):
    """前の版: 同じ日に期限外の A（10:00）と B（11:00）が承認待ち → B を却下（前の版は A を取り下げなかった）。"""
    deps = labor_deps()
    taro(deps)
    ts_a = old_pending(deps, "A", "2026-11-26T01:00:00+00:00", "2026-11-23T21:00:00+09:00", "2026-11-24T03:00:00+09:00")
    old_pending(deps, "B", "2026-11-26T02:00:00+00:00", "2026-11-23T21:30:00+09:00", "2026-11-24T03:00:00+09:00")
    b = pend(deps, "B")
    b["state"] = "rejected"
    deps.store.set_doc("attendance_pending", "B", b)
    react(deps, ts_a)
    run(deps, clock, "2026-11-26 13:00")
    assert pend(deps, "A")["state"] == "superseded" and rec(deps) is None
    assert current(deps, ts_a).startswith("【取り下げ】")


# ---------------------------------------------------------------- レビュー10回目の注記
def test_deleted_record_post_is_posted_again_when_the_record_changes(clock, monkeypatch):
    from barnull_sales.http import ExternalAPIError

    deps = labor_deps([att(11, 23, "21:00", "03:00"), att(11, 23, "21:30", "03:00")])
    send(deps, clock, "2026-11-24 03:30", "E1")
    approve_all(deps)
    gone = rec(deps)["slack"]["ts"]
    real = deps.notifier.update

    def update(channel, ts, text):
        if ts == gone:
            raise ExternalAPIError("Slack", "chat.update", 200, "message_not_found")
        return real(channel, ts, text)

    monkeypatch.setattr(deps.notifier, "update", update)
    send(deps, clock, "2026-11-24 12:00", "E2")
    approve_all(deps)                                                 # 訂正の承認: 消された親メッセージを出し直す
    r = rec(deps)
    assert r["hours"] == 5.5 and r["slack"]["ts"] != gone and not r["sync_pending"] and not r["after_save_pending"]
    assert "21:30〜03:00" in current(deps, r["slack"]["ts"])
    assert any(t.startswith("訂正：21:00〜03:00 6h → 21:30〜03:00 5.5h") for t in thread(deps, r["slack"]["ts"]))
    assert deps.labor.tasks[r["asana"]["task_gid"]]["slack_url"].endswith(r["slack"]["ts"].replace(".", ""))


# ---------------------------------------------------------------- レビュー11回目の指摘
@pytest.mark.parametrize("decision", ["white_check_mark", "x"])
def test_decided_correction_shows_the_record_as_it_was_at_decision_time(clock, decision):
    deps = labor_deps([att(11, 23, "21:00", "03:00"), att(11, 23, "21:30", "03:00")])
    send(deps, clock, "2026-11-24 03:30", "E1")
    approve_all(deps)
    send(deps, clock, "2026-11-24 12:00", "E2")                       # 訂正（承認待ち）
    gid = rec(deps)["asana"]["task_gid"]
    deps.labor.tasks[gid].update(att_manual="時間を反映", start="22:00", end="03:00")    # その間に管理者が Asana で直した
    jobs.run_steps(deps, [("attendance_overrides", attendance.overrides)])
    ts2 = requests(deps)[1]["ts"]
    react(deps, ts2, decision)
    run(deps, clock, "2026-11-24 13:00")
    if decision == "white_check_mark":
        assert current(deps, ts2).startswith("【承認済み】たろう｜11/23（月）　21:30〜03:00　5.5h（訂正）\n前の記録：22:00〜03:00 5h")
        assert rec(deps)["hours"] == 5.5
    else:
        assert current(deps, ts2).startswith("【却下】たろう｜11/23（月）　21:30〜03:00　5.5h（訂正）\n"
                                             "記録（この依頼では変えていません）：22:00〜03:00 5h")
        assert rec(deps)["hours"] == 5


# ---------------------------------------------------------------- レビュー12回目の指摘
def test_reminder_for_a_pending_correction_says_the_old_record_is_in_payroll(clock):
    deps = labor_deps([att(11, 29, "21:00", "03:00"), att(11, 29, "22:00", "03:00")])
    taro(deps)
    send(deps, clock, "2026-11-30 04:00", "E1")
    approve_all(deps)
    send(deps, clock, "2026-11-30 12:00", "E2")                       # 11/29 の訂正（承認待ち）
    ts = requests(deps)[1]["ts"]
    run(deps, clock, "2026-12-01 10:00")
    assert "\n11月分の給与は 12/2（水）17:00 に計算します。この訂正は、承認されるまで前の記録のまま給与に入ります。" in reminders(deps, ts)[-1]
    clock["t"] = at("2026-12-02 17:10")
    payroll.step(deps)
    run(deps, clock, "2026-12-03 10:00")
    assert "\n11月分の給与は 12/10（木）9:00 に確定します。それまでに承認すれば、給与を計算し直します。" in reminders(deps, ts)[-1]


def test_unposted_correction_note_is_kept_when_another_correction_is_approved(clock, monkeypatch):
    deps = labor_deps([att(11, 23, "21:00", "03:00"), att(11, 23, "21:30", "03:00"), att(11, 23, "22:00", "03:00")])
    send(deps, clock, "2026-11-24 03:30", "E1")
    approve_all(deps)
    first = rec(deps)["slack"]["ts"]
    real = deps.notifier.post

    def no_notes(channel, text, thread_ts=None, broadcast=False):
        if thread_ts == first and text.startswith("訂正："):
            raise RuntimeError("Slack に届かない")
        return real(channel, text, thread_ts, broadcast)

    monkeypatch.setattr(deps.notifier, "post", no_notes)
    send(deps, clock, "2026-11-24 12:00", "E2")
    approve_all(deps)                                                 # 1つ目の訂正: スレッドへの投稿に失敗
    send(deps, clock, "2026-11-24 13:00", "E3")
    approve_all(deps)                                                 # 2つ目の訂正: これも失敗
    monkeypatch.setattr(deps.notifier, "post", real)
    jobs.run_steps(deps, [("attendance_resync", attendance.resync)])
    notes = [t for t in thread(deps, first) if t.startswith("訂正：")]
    assert len(notes) == 1 and "訂正：21:00〜03:00 6h → 21:30〜03:00 5.5h" in notes[0] and "21:30〜03:00 5.5h → 22:00〜03:00 5h" in notes[0]
    assert rec(deps)["after_save_pending"] is False


# ---------------------------------------------------------------- レビュー13回目の指摘
def test_recovered_correction_approval_shows_the_record_it_replaced(clock, monkeypatch):
    deps = labor_deps([att(11, 23, "21:00", "03:00"), att(11, 23, "21:30", "03:00")])
    send(deps, clock, "2026-11-24 03:30", "E1")
    approve_all(deps)
    send(deps, clock, "2026-11-24 12:00", "E2")
    gid = rec(deps)["asana"]["task_gid"]
    deps.labor.tasks[gid].update(att_manual="時間を反映", start="22:00", end="03:00")
    jobs.run_steps(deps, [("attendance_overrides", attendance.overrides)])
    ts2 = requests(deps)[1]["ts"]
    react(deps, ts2)
    crash_on(monkeypatch, deps, "E2", state="approved")
    with pytest.raises(JobFailed):
        run(deps, clock, "2026-11-24 13:00")                          # 記録した後、承認を保存する前に落ちた
    run(deps, clock, "2026-11-24 13:30")
    assert current(deps, ts2).startswith("【承認済み】たろう｜11/23（月）　21:30〜03:00　5.5h（訂正）\n前の記録：22:00〜03:00 5h")


# ---------------------------------------------------------------- レビュー14回目の指摘（移行のときだけ）
def test_resend_matching_an_old_request_on_a_recorded_day_says_it_will_replace(clock):
    deps = labor_deps([att(11, 20, "22:00", "03:00")], reply_ack=True)
    taro(deps)
    person = StaffIndex.load(deps.store).get(sid(deps))
    old = attendance.save_record(deps, person, datetime(2026, 11, 20).date(), at("2026-11-20 21:00"),
                                 at("2026-11-21 03:00"), event_id="OLDREC", line_user_id="U1", display_name="たろう",
                                 reason="LINE")
    stored = deps.store.get_doc("attendance", old["record_id"])
    stored["history"][-1]["at"] = "2026-11-20T18:10:00+00:00"           # 前の版が 11/21 03:10 に記録
    deps.store.set_doc("attendance", old["record_id"], stored)
    ts_old = old_pending(deps, "OLD1", "2026-11-23T01:00:00+00:00", "2026-11-20T22:00:00+09:00",
                         "2026-11-21T03:00:00+09:00", day="2026-11-20")      # 前の版の期限外の訂正（before なし）
    assert send(deps, clock, "2026-11-24 12:00", "E1")["status"] == "attendance_pending"
    assert replies(deps)[-1] == ("11/20 の勤怠（22:00〜03:00 5時間）は、同じ内容で承認待ちです。"
                                 "管理者が承認したら、前の記録と置き換えて、お知らせします。")
    pend_old = pend(deps, "OLD1")
    pend_old["slack"] = {}                                              # 依頼の投稿が消され、出し直すとき
    deps.store.set_doc("attendance_pending", "OLD1", pend_old)
    run(deps, clock, "2026-11-24 12:30")
    reposted = next(p for p in requests(deps) if p["ts"] != ts_old)
    assert "\n今の記録：21:00〜03:00 6h\n" in reposted["text"]
