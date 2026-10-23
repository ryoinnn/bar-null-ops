"""2026-10-11 の点検で直した弱点の再発防止テスト（Slack・管理画面・Claude は偽物）"""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta

import pytest
from test_engine import A1, A2, CODE, STRANGER, confirm_ts, env, new_sm, sm_detail  # noqa: F401

from barnull import secrets
from barnull.llm.client import Classification, LLMTruncated
from barnull.models import Detail, InqState, Message, MsgKind, Platform, ResState, message_keys
from barnull.platforms.base import (
    ActionFailed,
    ActionResult,
    NotReady,
    is_template_text,
    review_submitted,
    same_text,
)
from barnull.platforms.ib import Instabase
from barnull.textutil import iso, now


def posts_text(slack) -> str:
    out = []
    for p in slack.posts:
        out.append(p.get("text") or "")
        for a in p.get("attachments") or []:
            for b in a.get("blocks") or []:
                t = b.get("text") or {}
                out.append(t.get("text", "") if isinstance(t, dict) else "")
    return "\n".join(out)


# ---------- スタンプ ----------
async def test_non_approval_emoji_does_nothing_and_hints_once(env):  # noqa: F811
    eng, slack = env
    rid, r = await new_sm(eng)
    await eng.on_reaction(A1, r["slack_ts"], "eyes")
    await eng.on_reaction(A2, r["slack_ts"], "thinking_face")
    assert eng.sm.calls == [] and eng.res(rid)["state"] == ResState.NOTIFIED
    assert sum("承認のスタンプとして扱いません" in t for t in slack.texts()) == 1
    await eng.on_reaction(A1, r["slack_ts"], "+1::skin-tone-3")  # 肌の色違いも承認
    assert [c[0] for c in eng.sm.calls] == ["template"]


async def test_preexisting_reaction_is_not_executed_after_inquiry_becomes_request(env):  # noqa: F811
    eng, slack = env
    d = sm_detail(pid="7000001", status="お問い合わせ", start_at=None, end_at=None, can_approve=False)
    d.kind = d.kind.INQUIRY_ONLY
    eng.sm.details[d.platform_id] = d
    rid = await eng.on_new(Platform.SM, d.platform_id)
    ts = eng.res(rid)["slack_ts"]
    # 問い合わせの段階で、承認者が「見た」の意味で ✅ を押していた（停止中で届いていない）
    slack.pending_reactions = {ts: [("white_check_mark", [A1])]}
    # 予約リクエストに切り替わる
    eng.sm.details[d.platform_id] = sm_detail(pid="7000001")
    from barnull.models import ListingRow
    await eng._handle_sm_row(ListingRow(platform=Platform.SM, platform_id="7000001", status="保留中"))
    assert eng.res(rid)["state"] == ResState.NOTIFIED
    await eng.catch_up()
    assert eng.sm.calls == []  # 切り替わる前のスタンプでは承認・送信しない


async def test_catch_up_conflict_between_stamp_and_reply_does_not_execute(env):  # noqa: F811
    eng, slack = env
    rid, r = await new_sm(eng)
    slack.pending_reactions = {r["slack_ts"]: [("ok", [A1])]}
    slack.pending_replies = {r["slack_ts"]: [{"ts": "2000.5", "user": A2, "text": "ちょっと待って"}]}
    await eng.catch_up()
    assert eng.sm.calls == []
    assert eng.res(rid)["state"] == ResState.ON_HOLD
    assert any("どちらが先か分からない" in t for t in slack.texts())
    assert await eng.catch_up() == 0  # 2回目は何もしない


# ---------- 支払い待ち・途中停止 ----------
async def test_not_ready_keeps_code_unsent_and_notifies_on_payment(env):  # noqa: F811
    eng, slack = env
    rid, r = await new_sm(eng)

    async def approve_only(pid, text, pdf, marker, dry_run, code=None):
        eng.sm.details[pid].status, eng.sm.details[pid].can_approve = "承認", False
        raise NotReady("支払い待ち", "承認しました。予約の状態が「承認」（ゲストの支払い待ち）のため、定型文（暗証番号）はまだ送りません")
    eng.sm.approve_and_send_template = approve_only
    await eng.on_reaction(A1, r["slack_ts"], "ok")
    assert eng.res(rid)["state"] == ResState.NOTIFIED and eng.db.kv_get(f"await_payment:{rid}")
    assert any("支払いが確認されたらお知らせします" in t for t in slack.texts())
    eng.sm.details["6271768"].status = "予約完了"
    await eng._check_platform(rid)
    assert any("支払いが確認され" in t and "スタンプ" in t for t in slack.texts())
    assert eng.db.kv_get(f"await_payment:{rid}") is None


async def test_recover_stuck_states_after_crash(env):  # noqa: F811
    eng, slack = env
    rid, r = await new_sm(eng)
    eng.db.update("reservations", rid, state=ResState.EXECUTING.value)
    n = await eng.recover_stuck(0)
    assert n == 1 and eng.res(rid)["state"] == ResState.ERROR
    err = eng.db.one("SELECT * FROM slack_messages WHERE kind=? ORDER BY ts DESC", MsgKind.ERROR.value)
    assert err and '"retry": "template"' in err["extra"]
    await eng.on_reaction(A1, err["ts"], "ok")  # 再実行
    assert [c[0] for c in eng.sm.calls] == ["template"]


# ---------- 手動返信・システムの送信の区別 ----------
async def test_system_template_is_not_mistaken_for_manual_reply(env):  # noqa: F811
    eng, slack = env
    rid, r = await new_sm(eng, messages=[Message("guest", "[利用目的] 誕生日会\nケーキを持ち込めますか？", index=0)])
    draft = eng.open_drafts(rid)[0]
    await eng.on_reaction(A1, r["slack_ts"], "ok")  # 先に定型文（システムがホストとして送信）
    await eng.process_guest_messages(rid, eng.sm.details["6271768"].messages, "6271768")
    assert eng.db.inquiry(draft["id"])["state"] == InqState.DRAFT_POSTED  # 手動返信とみなさない
    await eng.on_reaction(A2, draft["draft_ts"], "ok")
    assert eng.sm.calls[-1][0] == "message"


async def test_manual_reply_just_before_send_is_detected(env):  # noqa: F811
    eng, slack = env
    rid, r = await new_sm(eng)
    await eng.on_reaction(A1, r["slack_ts"], "ok")
    msgs = eng.sm.details["6271768"].messages
    msgs.append(Message("guest", "駐車場はありますか？", index=len(msgs)))
    await eng.process_guest_messages(rid, msgs, "6271768")
    draft = eng.open_drafts(rid)[0]
    # 文案にスタンプする前に、誰かが管理画面で返信した（巡回はまだ）
    msgs.append(Message("host", "近くのコインパーキングをご利用ください。", index=len(msgs)))
    await eng.on_reaction(A2, draft["draft_ts"], "ok")
    assert [c[0] for c in eng.sm.calls] == ["template"]  # 二重返信しない
    assert eng.db.inquiry(draft["id"])["state"] == InqState.SENT_MANUALLY


async def test_second_message_before_send_regenerates_draft(env):  # noqa: F811
    eng, slack = env
    rid, r = await new_sm(eng)
    await eng.on_reaction(A1, r["slack_ts"], "ok")
    msgs = eng.sm.details["6271768"].messages
    msgs.append(Message("guest", "駐車場はありますか？", index=len(msgs)))
    await eng.process_guest_messages(rid, msgs, "6271768")
    d1 = eng.open_drafts(rid)[0]
    msgs.append(Message("guest", "あとWi-Fiはありますか？", index=len(msgs)))  # まだ巡回で拾っていない
    await eng.on_reaction(A2, d1["draft_ts"], "ok")
    assert [c[0] for c in eng.sm.calls] == ["template"]
    drafts = eng.open_drafts(rid)
    assert len(drafts) == 1 and drafts[0]["id"] != d1["id"] and "Wi-Fi" in drafts[0]["guest_text"]


async def test_sm_legacy_id_keys_do_not_create_duplicate_drafts(env):  # noqa: F811
    eng, slack = env
    rid, r = await new_sm(eng)
    q = Message("guest", "駐車場はありますか？", index=1, uid="39339705")
    # 以前の版の記録（メッセージ API の ID で処理済み）
    eng._save_cursor(Platform.SM, "6271768", {"id:39339705"})
    # 今回は API が取れず ID が無い
    msgs = [Message("guest", "[利用目的] パーティー", index=0), Message("guest", q.text, index=1)]
    before = eng.llm.drafts
    await eng.process_guest_messages(rid, msgs, "6271768")
    assert eng.llm.drafts == before  # 照合できないうちは新着扱いしない
    msgs[1].uid = "39339705"
    await eng.process_guest_messages(rid, msgs, "6271768")
    assert eng.llm.drafts == before  # ID で照合できた → 処理済み
    assert any(k.startswith("h:") for k in eng._cursor(Platform.SM, "6271768"))  # 新しい形式でも記録した


# ---------- 文面の検査・表示 ----------
async def test_draft_with_external_contact_needs_check(env):  # noqa: F811
    eng, slack = env
    rid, r = await new_sm(eng)
    await eng.on_reaction(A1, r["slack_ts"], "ok")

    async def bad_draft(**kw):
        return "ご連絡ありがとうございます！詳しくは LINE（ID: barnull）でご連絡ください。"
    eng.llm.draft_reply = bad_draft
    msgs = eng.sm.details["6271768"].messages
    msgs.append(Message("guest", "詳しく相談したいです", index=len(msgs)))
    await eng.process_guest_messages(rid, msgs, "6271768")
    d = eng.open_drafts(rid)[0]
    assert d["needs_check"] == 1
    await eng.on_reaction(A2, d["draft_ts"], "ok")
    assert [c[0] for c in eng.sm.calls] == ["template"]  # スタンプでは送らない


async def test_guest_strings_are_escaped_and_code_is_redacted_in_slack(env):  # noqa: F811
    eng, slack = env
    secrets.load_redactions()
    try:
        rid, r = await new_sm(eng, guest_name="<!channel> 太郎",
                              messages=[Message("guest", f"<!here> 暗証番号は {CODE} ですか？", index=0)])
        text = posts_text(slack)
        assert "<!channel>" not in text and "<!here>" not in text
        assert "&lt;!channel&gt;" in text
        assert CODE not in text
    finally:
        secrets._redact_values.clear()


async def test_revision_requires_confirmation_and_is_checked(env):  # noqa: F811
    eng, slack = env
    rid, r = await new_sm(eng)
    await eng.on_reaction(A1, r["slack_ts"], "ok")
    msgs = eng.sm.details["6271768"].messages
    msgs.append(Message("guest", "延長はできますか？", index=len(msgs)))
    await eng.process_guest_messages(rid, msgs, "6271768")
    await eng.on_reply(A1, r["slack_ts"], "【修正】延長は【要確認: 料金】です。よろしくお願いいたします！", "3.1")
    assert [c[0] for c in eng.sm.calls] == ["template"]  # 確認前は送らない
    await eng.on_reaction(A2, confirm_ts(eng), "ok")
    assert [c[0] for c in eng.sm.calls] == ["template"]  # 【要確認】が残っているので送らない
    assert any("送信しませんでした" in t for t in slack.texts())


async def test_dismiss_drafts_by_reply(env):  # noqa: F811
    eng, slack = env
    rid, r = await new_sm(eng)
    await eng.on_reaction(A1, r["slack_ts"], "ok")
    msgs = eng.sm.details["6271768"].messages
    msgs.append(Message("guest", "ありがとうございました", index=len(msgs)))
    await eng.process_guest_messages(rid, msgs, "6271768")
    assert eng.open_drafts(rid)
    await eng.on_reply(A2, r["slack_ts"], "送らない", "3.2")
    assert eng.open_drafts(rid) == []


async def test_truncated_llm_output_is_not_used(env):  # noqa: F811
    eng, slack = env
    rid, r = await new_sm(eng)
    await eng.on_reaction(A1, r["slack_ts"], "ok")

    async def truncated(**kw):
        raise LLMTruncated("cut")
    eng.llm.draft_reply = truncated
    msgs = eng.sm.details["6271768"].messages
    msgs.append(Message("guest", "持ち込みはできますか？", index=len(msgs)))
    await eng.process_guest_messages(rid, msgs, "6271768")
    q = eng.open_drafts(rid)[0]
    assert q["state"] == InqState.NO_DRAFT and not q["draft_text"]


async def test_close_keeps_unsent_drafts_and_says_so(env):  # noqa: F811
    eng, slack = env
    rid, r = await new_sm(eng)
    await eng.on_reaction(A1, r["slack_ts"], "ok")
    msgs = eng.sm.details["6271768"].messages
    msgs.append(Message("guest", "キャンセルした場合の返金は？", index=len(msgs)))
    await eng.process_guest_messages(rid, msgs, "6271768")
    await eng.close(rid, "ゲストキャンセル")
    assert eng.open_drafts(rid)  # 黙って捨てない
    assert any("未送信の返信文案が 1 件残っています" in t for t in slack.texts())


async def test_code_placeholder_is_refused_after_use(env):  # noqa: F811
    eng, slack = env
    rid, r = await new_sm(eng)
    await eng.on_reaction(A1, r["slack_ts"], "ok")
    eng.db.update("reservations", rid, state=ResState.CLOSED.value)
    assert not eng.code_allowed(eng.res(rid))
    assert eng.outgoing_problems("暗証番号は［暗証番号］です", eng.res(rid))


# ---------- レビュー ----------
async def test_sm_review_waits_for_exit_record_then_asks(env):  # noqa: F811
    eng, slack = env
    rid, r = await new_sm(eng)
    await eng.on_reaction(A1, r["slack_ts"], "ok")
    d = eng.sm.details["6271768"]
    d.start_at, d.end_at = now() - timedelta(hours=3), now() - timedelta(hours=1)
    eng.db.update("reservations", rid, start_at=iso(d.start_at), end_at=iso(d.end_at))
    await eng.complete(rid, "exit_record")
    assert eng.res(rid)["state"] == ResState.CONFIRMED and not any(c[0] == "review" for c in eng.sm.calls)
    d.end_at = now() - timedelta(hours=4)
    eng.db.update("reservations", rid, end_at=iso(d.end_at))
    await eng.complete(rid, "exit_record")
    assert eng.res(rid)["state"] == ResState.REVIEW_CONFIRM  # 記録が無いまま自動で★5は付けない


async def test_review_reply_with_count_words_is_not_read_as_stars(env):  # noqa: F811
    eng, slack = env
    rid, r = await new_sm(eng)
    await eng.on_reaction(A1, r["slack_ts"], "ok")
    eng.db.update("reservations", rid, state=ResState.REVIEW_CONFIRM.value)
    await eng.on_reply(A1, r["slack_ts"], "写真を1つ確認しました", "4.1")
    assert not any(c[0] == "review" for c in eng.sm.calls)
    await eng.on_reply(A1, r["slack_ts"], "★4 コメント「ご利用ありがとうございました！またお待ちしております。」", "4.2")
    assert not any(c[0] == "review" for c in eng.sm.calls)  # 確認を経てから
    await eng.on_reaction(A2, eng.db.kv_get(f"review_confirm_ts:{rid}"), "ok")
    assert eng.sm.calls[-1][:3] == ("review", "6271768", 4)


async def test_review_failure_is_separate_from_template_error(env):  # noqa: F811
    eng, slack = env
    rid, r = await new_sm(eng)
    await eng.on_reaction(A1, r["slack_ts"], "ok")

    async def fail(*a, **kw):
        raise RuntimeError("boom")
    eng.sm.post_review = fail
    await eng.post_review(rid, 3, "ご利用ありがとうございました！またお待ちしております。", A1)
    assert eng.res(rid)["state"] == ResState.REVIEW_ERROR
    n = len(eng.sm.calls)
    await eng.on_reaction(A2, r["slack_ts"], "ok")  # 親へのスタンプで定型文をやり直さない
    await eng.on_reply(A2, r["slack_ts"], "待って", "4.3")
    assert len(eng.sm.calls) == n
    err = eng.db.one("SELECT * FROM slack_messages WHERE kind=? ORDER BY ts DESC", MsgKind.ERROR.value)
    assert '"stars": 3' in err["extra"]  # 再実行でも指定の評価を使う


# ---------- 送信済みの判定・その他 ----------
def test_template_detection_requires_code_and_lenient_compare():
    assert is_template_text(f"キーボックスの暗証番号は【{CODE}】です", "キーボックスの暗証番号", CODE)
    assert not is_template_text("キーボックスの暗証番号は当日お送りします", "キーボックスの暗証番号", CODE)
    assert same_text("ご予約ありがとうございます！\n\nよろしく", "ご予約ありがとうございます！ よろしく")


def test_ib_booking_start_index_fallbacks():
    from datetime import datetime

    from barnull.textutil import JST
    t = lambda h: datetime(2026, 10, 5, h, 0, tzinfo=JST)  # noqa: E731
    msgs = [Message("system", "【自動配信メッセージ】\n予約ID：111 の予約状況が「予約確定」になりました", at=t(1), index=0),
            Message("host", "キーボックスの暗証番号は【0000】", files=["m.pdf"], at=t(2), index=1),
            Message("system", "【自動配信メッセージ】\n送信した予約内容の変更リクエストが承認され、予約内容が変更されました", at=t(5), index=2)]
    assert Instabase._index_after_booking(msgs, "111") == 0
    assert Instabase._index_after_booking(msgs, "222", applied_at=t(3)) == 2   # 申込日時以降
    assert Instabase._index_after_booking(msgs, "222") == 2                    # 変更承認の自動メッセージ


def test_message_keys_are_stable_without_api_ids():
    a = [Message("guest", "こんにちは", index=0, uid="1"), Message("guest", "こんにちは", index=1)]
    b = [Message("guest", "こんにちは", index=0), Message("guest", "こんにちは", index=1, uid="2")]
    assert message_keys(a) == message_keys(b)


async def test_alert_is_recorded_only_when_posted(env):  # noqa: F811
    eng, slack = env

    async def broken(**kw):
        raise RuntimeError("slack down")
    ok = slack.chat_postMessage
    slack.chat_postMessage = broken
    await eng.alert("k1", "テスト")
    assert eng.db.one("SELECT * FROM alerts WHERE key='k1'") is None
    slack.chat_postMessage = ok
    await eng.alert("k1", "テスト")
    assert eng.db.one("SELECT * FROM alerts WHERE key='k1'") is not None


async def test_parent_post_failure_is_retried(env):  # noqa: F811
    eng, slack = env
    ok = slack.chat_postMessage

    async def broken(**kw):
        raise RuntimeError("slack down")
    slack.chat_postMessage = broken
    d = sm_detail(pid="6300001")
    eng.sm.details[d.platform_id] = d
    rid = await eng.on_new(Platform.SM, d.platform_id)
    assert eng.res(rid)["slack_ts"] is None
    slack.chat_postMessage = ok
    await eng.tick_repost()
    assert eng.res(rid)["slack_ts"]


def test_single_instance_lock(tmp_path):
    from barnull.app import SingleInstance

    with SingleInstance(tmp_path / "x.lock"):
        with pytest.raises(SystemExit):
            with SingleInstance(tmp_path / "x.lock", "使用中"):
                pass


def test_redaction_variants():
    secrets._redact_values[:] = []
    try:
        secrets._redact_values.extend(["5678", "pa ss/word", "pa%20ss%2Fword"])
        out = secrets.redact("５６７８ / 5678 / 156789 / ?p=pa%20ss%2Fword")
        assert out == "**** / **** / 156789 / ?p=****"  # 長い数字の一部は伏せない
    finally:
        secrets._redact_values.clear()


def test_stranger_cannot_dismiss(env):  # noqa: F811
    eng, _ = env
    assert not eng.is_approver(STRANGER)


def test_detail_kind_for_dated_inquiry():
    from barnull.platforms import parse_sm

    d = parse_sm.parse_thread({"status": "お問い合わせ", "fields": {"日時": "2026/12/20(日) 13:00〜18:00"}}, "1")
    assert d.kind.value == "inquiry_only"
    assert isinstance(d, Detail)


# ---------- 2回目のレビューで直したもの ----------
async def test_restamp_after_payment_wait_is_accepted(env):  # noqa: F811
    eng, slack = env
    rid, r = await new_sm(eng)
    calls = []

    async def first_not_ready(pid, text, pdf, marker, dry_run, code=None):
        calls.append(pid)
        if len(calls) == 1:
            raise NotReady("支払い待ち", "支払い待ち")
        from barnull.platforms.base import ActionResult
        return ActionResult(ok=True)
    eng.sm.approve_and_send_template = first_not_ready
    await eng.on_reaction(A1, r["slack_ts"], "ok", event_ts="100.1")
    assert eng.res(rid)["state"] == ResState.NOTIFIED
    await eng.on_reaction(A1, r["slack_ts"], "ok", event_ts="100.2")  # 同じ人が同じスタンプを付け直す
    assert len(calls) == 2 and eng.res(rid)["state"] == ResState.CONFIRMED


async def test_manual_reply_mentioning_keybox_counts_as_manual(env):  # noqa: F811
    eng, slack = env
    rid, r = await new_sm(eng)
    await eng.on_reaction(A1, r["slack_ts"], "ok")
    msgs = eng.sm.details["6271768"].messages
    msgs.append(Message("guest", "暗証番号がどこにあるか分かりません", index=len(msgs)))
    await eng.process_guest_messages(rid, msgs, "6271768")
    d = eng.open_drafts(rid)[0]
    msgs.append(Message("host", "キーボックスの暗証番号は、先ほどのメッセージに記載しております。", index=len(msgs)))
    await eng.process_guest_messages(rid, msgs, "6271768")
    assert eng.db.inquiry(d["id"])["state"] == InqState.SENT_MANUALLY


async def test_stamp_on_old_draft_message_is_refused(env):  # noqa: F811
    eng, slack = env
    rid, r = await new_sm(eng)
    await eng.on_reaction(A1, r["slack_ts"], "ok")
    msgs = eng.sm.details["6271768"].messages
    msgs.append(Message("guest", "駐車場は？", index=len(msgs)))
    await eng.process_guest_messages(rid, msgs, "6271768")
    d = eng.open_drafts(rid)[0]
    eng.db.save_slack("9999.1", eng.cfg.channel, MsgKind.DRAFT, rid, d["id"])  # 重複して出た古い文案の投稿
    await eng.on_reaction(A2, "9999.1", "ok")
    assert [c[0] for c in eng.sm.calls] == ["template"]
    assert any("古い文案" in t for t in slack.texts())


async def test_revision_replaces_approved_reply_instead_of_sending_both(env):  # noqa: F811
    eng, slack = env
    rid, r = await new_sm(eng, messages=[Message("guest", "[利用目的] 誕生日会\nケーキを持ち込めますか？", index=0)])
    d = eng.open_drafts(rid)[0]
    await eng.on_reaction(A1, d["draft_ts"], "ok")  # 定型文の前に文案を承認
    assert eng.db.inquiry(d["id"])["state"] == InqState.APPROVED
    revised = "【修正】ケーキの持ち込みは可能です。冷蔵庫もお使いいただけます。よろしくお願いいたします！"
    await eng.on_reply(A2, r["slack_ts"], revised, "5.1")
    await eng.on_reaction(A2, confirm_ts(eng), "ok")
    assert eng.db.inquiry(d["id"])["sent_text"] == revised
    await eng.on_reaction(A1, r["slack_ts"], "ok")  # 定型文 → 承認済みの返信（置き換え後の1通だけ）
    msgs = [c for c in eng.sm.calls if c[0] == "message"]
    assert len(msgs) == 1 and msgs[0][2] == revised


async def test_empty_reply_is_not_reprocessed_by_catch_up(env):  # noqa: F811
    eng, slack = env
    rid, r = await new_sm(eng)
    slack.pending_replies = {r["slack_ts"]: [{"ts": "6000.1", "user": A1, "text": "", "subtype": "file_share"}]}
    assert await eng.catch_up() == 0
    assert await eng.catch_up() == 0


async def test_template_refused_after_use_and_reminders_stop(env):  # noqa: F811
    eng, slack = env
    rid, r = await new_sm(eng)
    eng.db.update("reservations", rid, start_at=iso(now() - timedelta(hours=5)), end_at=iso(now() - timedelta(hours=2)),
                  next_remind_at=iso(now() - timedelta(minutes=1)))
    await eng.on_reaction(A1, r["slack_ts"], "ok")
    assert eng.sm.calls == [] and any("利用時間が過ぎている" in t for t in slack.texts())
    eng.sm.details["6271768"].start_at = now() - timedelta(hours=5)
    eng.sm.details["6271768"].end_at = now() - timedelta(hours=2)
    n = len(slack.posts)
    await eng.tick_reminders()
    assert not any("承認して送信" in (p.get("text") or "") for p in slack.posts[n:])


async def test_ib_past_order_in_shared_thread_does_not_take_new_message(env):  # noqa: F811
    eng, slack = env
    tid = "4618365"
    active = Detail(platform=Platform.IB, platform_id="9000000002", status="予約確定", guest_name="佐藤 花子",
                    start_at=now() + timedelta(days=5), end_at=now() + timedelta(days=5, hours=3), thread_id=tid)
    past = Detail(platform=Platform.IB, platform_id="9000000001", status="予約確定", guest_name="佐藤 花子",
                  start_at=now() - timedelta(days=60), end_at=now() - timedelta(days=60, hours=-3), thread_id=tid)
    msgs = [Message("system", "【自動配信メッセージ】\n予約ID：9000000002 の予約状況が「予約確定」になりました",
                    at=now() - timedelta(days=1), index=0),
            Message("guest", "当日プロジェクターは使えますか？", at=now() - timedelta(minutes=5), index=1)]
    active.messages = past.messages = msgs
    eng.ib.details = {active.platform_id: active, past.platform_id: past}
    rid_a = await eng.on_new(Platform.IB, active.platform_id, detail=active)
    eng._mark_processed(Platform.IB, tid, msgs[:1])
    eng.db.update("reservations", rid_a, thread_id=tid)

    async def get_thread(t):
        return msgs, None, [active.platform_id, past.platform_id]
    eng.ib.get_thread = get_thread
    await eng.on_ib_thread(tid)
    past_r = eng.db.reservation_by_pid("ib", past.platform_id)
    assert past_r["state"] == ResState.CLOSED and eng.open_drafts(past_r["id"]) == []
    assert len(eng.open_drafts(rid_a)) == 1  # 新着は進行中の予約で扱う


def test_done_state_rules():
    from barnull.models import message_key_variants
    from barnull.workflows.engine import Engine
    m_with_id = Message("guest", "ありがとう", index=0, uid="7")
    ks = message_key_variants([m_with_id])[0]
    assert Engine._done_state(m_with_id, ks, {"id:7"}) == "done"
    assert Engine._done_state(m_with_id, ks, {"id:8"}) == "new"
    m_no_id = Message("guest", "ありがとう", index=0)
    ks2 = message_key_variants([m_no_id])[0]
    assert Engine._done_state(m_no_id, ks2, {"id:8"}) == "unknown"   # 以前の ID の記録と照合できない
    h = message_keys([m_no_id])[0]
    assert Engine._done_state(m_no_id, ks2, {h}) == "done"


# ---------- レビュー投稿（2026-10-12 の本番の失敗を受けて） ----------
async def test_review_not_ready_returns_to_confirmed_and_retries_later(env):  # noqa: F811
    eng, slack = env
    rid, r = await new_sm(eng)
    await eng.on_reaction(A1, r["slack_ts"], "ok")

    async def not_ready(*a, **kw):
        raise NotReady("レビュー", "まだレビューを書ける状態になっていません")
    eng.sm.post_review = not_ready
    await eng.post_review(rid, 5, None, "system")
    assert eng.res(rid)["state"] == ResState.CONFIRMED  # エラーにせず、後で再試行
    assert eng.db.kv_get(f"review_wait:{rid}")
    assert not any("自動処理に失敗" in t for t in slack.texts())


async def test_hold_reply_after_review_failure_explains_and_no_review_closes(env):  # noqa: F811
    eng, slack = env
    rid, r = await new_sm(eng)
    await eng.on_reaction(A1, r["slack_ts"], "ok")
    eng.db.update("reservations", rid, state=ResState.REVIEW_ERROR.value)
    await eng.on_reply(A1, r["slack_ts"], "後で確認するので保留で", "7.1")
    assert any("スタンプを押すまで投稿しません" in t for t in slack.texts())
    await eng.on_reply(A1, r["slack_ts"], "レビューはしない", "7.2")
    assert eng.res(rid)["state"] == ResState.CLOSED


async def _used_sm(eng):
    """定型文を送り、利用が終わった SM の予約"""
    rid, r = await new_sm(eng)
    await eng.on_reaction(A1, r["slack_ts"], "ok")
    d = eng.sm.details["6271768"]
    d.start_at, d.end_at = now() - timedelta(hours=5), now() - timedelta(hours=2)
    d.exit_status = "問題なく退室しました"
    eng.db.update("reservations", rid, start_at=iso(d.start_at), end_at=iso(d.end_at))
    return rid, eng.res(rid)


async def test_not_ready_from_review_error_is_retried_automatically_and_gives_up_after_days(env):  # noqa: F811
    eng, slack = env
    rid, r = await _used_sm(eng)
    eng.db.update("reservations", rid, state=ResState.REVIEW_ERROR.value)
    thanks = {"n": 0}

    async def count_thanks(purpose):
        thanks["n"] += 1
        return "この度はBar nullをご利用いただき、誠にありがとうございました！"
    eng.llm.thanks_comment = count_thanks
    ready = {"v": False}
    orig = eng.sm.post_review

    async def maybe(*a, **kw):
        if not ready["v"]:
            raise NotReady("レビュー", "まだレビューを書ける状態になっていません")
        return await orig(*a, **kw)
    eng.sm.post_review = maybe
    await eng.post_review(rid, 3, None, A2, approved=True)  # 人の再実行（★3。スタンプの経路と同じ）
    assert eng.res(rid)["state"] == ResState.CONFIRMED  # 自動の再試行に回る
    await eng.complete(rid, "exit_record")
    assert thanks["n"] == 1  # お礼文は作り直さない
    ready["v"] = True
    await eng.complete(rid, "exit_record")
    assert eng.sm.calls[-1][:3] == ("review", "6271768", 3)  # 人が指定した★3のまま
    # 書ける状態にならないまま日数が過ぎたら、自動投稿をやめて知らせる
    rid2, r2 = await new_sm(eng, platform_id="6271799", guest_name="佐藤 花子")
    eng.db.update("reservations", rid2, state=ResState.CONFIRMED.value, template_sent_at=iso(now()))
    eng.db.kv_set(f"review_wait:{rid2}", iso(now() - timedelta(days=4)))
    eng.sm.post_review = maybe
    ready["v"] = False
    await eng.post_review(rid2, 5, None, "system")
    assert eng.res(rid2)["state"] == ResState.REVIEW_ERROR


async def test_hold_and_no_review_work_before_auto_post(env):  # noqa: F811
    eng, slack = env
    rid, r = await _used_sm(eng)
    await eng.on_reply(A1, r["slack_ts"], "レビューは待って", "8.1")
    assert eng.res(rid)["state"] == ResState.REVIEW_CONFIRM  # 自動投稿を止めて確認を求める
    await eng.tick_completion()
    assert not any(c[0] == "review" for c in eng.sm.calls)
    rid2, r2 = await new_sm(eng, platform_id="6271798", guest_name="佐藤 花子")
    await eng.on_reaction(A1, r2["slack_ts"], "ok")
    eng.db.update("reservations", rid2, end_at=iso(now() - timedelta(hours=1)))
    await eng.on_reply(A1, r2["slack_ts"], "レビューはしない", "8.2")
    assert eng.res(rid2)["state"] == ResState.CLOSED


async def test_review_comment_with_inner_quotes_and_old_confirm(env):  # noqa: F811
    eng, slack = env
    rid, r = await _used_sm(eng)
    eng.db.update("reservations", rid, state=ResState.REVIEW_CONFIRM.value)
    await eng.on_reply(A1, r["slack_ts"], "★4 コメント「この度は「Bar null」をご利用いただきありがとうございました。」", "9.1")
    first = eng.db.kv_get(f"review_confirm_ts:{rid}")
    await eng.on_reply(A1, r["slack_ts"], "★5 コメント「またのご利用を心よりお待ちしております。ありがとうございました。」", "9.2")
    await eng.on_reaction(A2, first, "ok")  # 古い確認メッセージ
    assert not any(c[0] == "review" for c in eng.sm.calls)
    await eng.on_reaction(A2, eng.db.kv_get(f"review_confirm_ts:{rid}"), "ok")
    assert eng.sm.calls[-1] == ("review", "6271768", 5, "またのご利用を心よりお待ちしております。ありがとうございました。")
    # コメントの中の「」で切れない
    assert "「Bar null」をご利用" in posts_text(slack)


# ---------- 2回目のレビューの指摘 ----------
async def test_hold_with_open_draft_after_use_also_stops_auto_review(env):  # noqa: F811
    eng, slack = env
    rid, r = await _used_sm(eng)
    msgs = eng.sm.details["6271768"].messages
    msgs.append(Message("guest", "グラスを1つ割ってしまいました。弁償は必要ですか？", index=len(msgs)))
    await eng.process_guest_messages(rid, msgs, "6271768")
    assert eng.open_drafts(rid)
    await eng.on_reply(A1, r["slack_ts"], "レビューは待って", "10.1")
    assert eng.res(rid)["state"] == ResState.REVIEW_CONFIRM
    await eng.tick_completion()
    assert not any(c[0] == "review" for c in eng.sm.calls)


async def test_changed_mind_to_auto_comment_does_not_reuse_human_comment(env):  # noqa: F811
    eng, slack = env
    rid, r = await _used_sm(eng)
    eng.db.update("reservations", rid, state=ResState.REVIEW_CONFIRM.value)
    await eng.on_reply(A1, r["slack_ts"], "★2 コメント「清掃が不十分な箇所がありました。次回はご注意ください。」", "11.1")

    async def fail(*a, **kw):
        raise ActionFailed("レビュー", "確認画面が出ません")
    orig = eng.sm.post_review
    eng.sm.post_review = fail
    await eng.on_reaction(A2, eng.db.kv_get(f"review_confirm_ts:{rid}"), "ok")
    assert eng.res(rid)["state"] == ResState.REVIEW_ERROR
    eng.sm.post_review = orig
    await eng.on_reply(A1, r["slack_ts"], "やっぱり★5で", "11.2")
    await eng.on_reaction(A2, eng.db.kv_get(f"review_confirm_ts:{rid}"), "ok")
    assert eng.sm.calls[-1][2] == 5 and "清掃" not in eng.sm.calls[-1][3]


async def test_approved_review_waits_and_posts_without_reasking(env):  # noqa: F811
    eng, slack = env
    rid, r = await _used_sm(eng)
    eng.sm.details["6271768"].exit_status = "気になる点があります"
    await eng.complete(rid, "exit_record")
    assert eng.res(rid)["state"] == ResState.REVIEW_CONFIRM
    await eng.on_reply(A1, r["slack_ts"], "★3 コメント「ご利用ありがとうございました。次回は片付けにご協力をお願いします。」", "12.1")
    ready = {"v": False}
    orig = eng.sm.post_review

    async def maybe(*a, **kw):
        if not ready["v"]:
            raise NotReady("レビュー", "まだ書けません")
        return await orig(*a, **kw)
    eng.sm.post_review = maybe
    await eng.on_reaction(A2, eng.db.kv_get(f"review_confirm_ts:{rid}"), "ok")
    assert eng.res(rid)["state"] == ResState.CONFIRMED
    ready["v"] = True
    n = len(slack.posts)
    await eng.complete(rid, "exit_record")
    assert eng.sm.calls[-1] == ("review", "6271768", 3, "ご利用ありがとうございました。次回は片付けにご協力をお願いします。")
    assert not any("確認してください" in (p.get("text") or "") for p in slack.posts[n:])  # 確認をやり直さない


async def test_old_error_message_does_not_override_newer_confirm(env):  # noqa: F811
    eng, slack = env
    rid, r = await _used_sm(eng)

    async def fail(*a, **kw):
        raise ActionFailed("レビュー", "失敗")
    orig = eng.sm.post_review
    eng.sm.post_review = fail
    await eng.post_review(rid, 5, None, "system")
    err = eng.db.one("SELECT ts FROM slack_messages WHERE kind=? ORDER BY ts DESC", MsgKind.ERROR.value)["ts"]
    eng.sm.post_review = orig
    await eng.on_reply(A1, r["slack_ts"], "★2 コメント「次回は片付けにご協力をお願いいたします。」", "13.1")
    await eng.on_reaction(A2, err, "ok")  # 古い失敗メッセージ
    assert not any(c[0] == "review" for c in eng.sm.calls)


async def test_five_star_words_in_revision_are_not_review_instruction(env):  # noqa: F811
    eng, slack = env
    rid, r = await _used_sm(eng)
    msgs = eng.sm.details["6271768"].messages
    msgs.append(Message("guest", "素敵なお店でした！忘れ物は見つかりますか？", index=len(msgs)))
    await eng.process_guest_messages(rid, msgs, "6271768")
    await eng.on_reply(A1, r["slack_ts"], "【修正】5つ星の評価をいただきありがとうございます！忘れ物は保管しております。", "14.1")
    assert confirm_ts(eng)  # 修正文の確認が出る
    assert eng.res(rid)["state"] == ResState.REVIEW_CONFIRM  # 念のため、レビューの自動投稿は止めて確認を求める


async def test_complete_read_failures_are_reported(env):  # noqa: F811
    eng, slack = env
    rid, r = await _used_sm(eng)

    async def broken(pid):
        raise RuntimeError("画面が読めない")
    eng.sm.get_detail = broken
    for _ in range(3):
        await eng.complete(rid, "exit_record")
    assert any("3回続けて失敗" in t for t in slack.texts())


async def test_recheck_unreviewed_lists_missed_reviews_once(env):  # noqa: F811
    eng, slack = env
    d = sm_detail(pid="6087596", status="利用完了", start_at=now() - timedelta(days=3, hours=3),
                  end_at=now() - timedelta(days=3), can_approve=False)
    eng.sm.details[d.platform_id] = d
    rid = await eng.on_new(Platform.SM, d.platform_id, trigger="seed", detail=d, silent=True)
    eng.db.update("reservations", rid, state=ResState.CLOSED.value)
    d.review_posted, d.review_open = False, True
    await eng.recheck_unreviewed()
    assert any("レビューが未投稿" in t and "6087596" in t for t in slack.texts())
    n = len(slack.posts)
    await eng.recheck_unreviewed()
    assert len(slack.posts) == n


# ---------- 3回目のレビューの指摘 ----------
async def test_review_instructions_before_use_are_followed_after_use(env):  # noqa: F811
    eng, slack = env
    rid, r = await new_sm(eng)
    await eng.on_reaction(A1, r["slack_ts"], "ok")  # 利用前（CONFIRMED）
    await eng.on_reply(A1, r["slack_ts"], "このゲストはレビューしない", "15.1")
    assert any("利用後のレビューは投稿しません" in t for t in slack.texts())
    d = eng.sm.details["6271768"]
    d.start_at, d.end_at, d.exit_status = now() - timedelta(hours=5), now() - timedelta(hours=2), "問題なく退室しました"
    eng.db.update("reservations", rid, start_at=iso(d.start_at), end_at=iso(d.end_at))
    await eng.complete(rid, "exit_record")
    assert eng.res(rid)["state"] == ResState.CLOSED and not any(c[0] == "review" for c in eng.sm.calls)

    rid2, r2 = await new_sm(eng, platform_id="6271797", guest_name="佐藤 花子")
    await eng.on_reaction(A1, r2["slack_ts"], "ok")
    await eng.on_reply(A1, r2["slack_ts"], "★３ コメント「ご利用ありがとうございました。次回もお待ちしております。」", "15.2")  # 全角数字
    d2 = eng.sm.details["6271797"]
    d2.start_at, d2.end_at, d2.exit_status = now() - timedelta(hours=5), now() - timedelta(hours=2), "問題なく退室しました"
    eng.db.update("reservations", rid2, start_at=iso(d2.start_at), end_at=iso(d2.end_at))
    await eng.complete(rid2, "exit_record")
    assert eng.res(rid2)["state"] == ResState.REVIEW_CONFIRM  # 自動では投稿せず、指定の内容で確認
    await eng.on_reaction(A2, eng.db.kv_get(f"review_confirm_ts:{rid2}"), "ok")
    assert eng.sm.calls[-1][:3] == ("review", "6271797", 3)


async def test_invalid_review_instruction_after_use_stops_auto_post(env):  # noqa: F811
    eng, slack = env
    rid, r = await _used_sm(eng)
    await eng.on_reply(A1, r["slack_ts"], "★2 コメント「汚れが目立った」", "16.1")  # 10字未満
    assert eng.res(rid)["state"] == ResState.REVIEW_CONFIRM
    await eng.tick_completion()
    assert not any(c[0] == "review" for c in eng.sm.calls)


async def test_only_latest_review_error_message_retries(env):  # noqa: F811
    eng, slack = env
    rid, r = await _used_sm(eng)

    async def fail(*a, **kw):
        raise ActionFailed("レビュー", "失敗")
    orig = eng.sm.post_review
    eng.sm.post_review = fail
    await eng.post_review(rid, 5, None, "system", from_states=(ResState.CONFIRMED,))
    e1 = eng.db.kv_get(f"review_error_ts:{rid}")
    await eng.on_reply(A1, r["slack_ts"], "★2 コメント「次回は片付けにご協力をお願いいたします。」", "17.1")
    await eng.on_reaction(A2, eng.db.kv_get(f"review_confirm_ts:{rid}"), "ok")  # これも失敗 → 新しい失敗メッセージ
    assert eng.db.kv_get(f"review_error_ts:{rid}") != e1
    eng.sm.post_review = orig
    await eng.on_reaction(A1, e1, "ok")  # 古い失敗メッセージ
    assert not any(c[0] == "review" for c in eng.sm.calls)
    await eng.on_reaction(A1, eng.db.kv_get(f"review_error_ts:{rid}"), "ok")
    assert eng.sm.calls[-1][:3] == ("review", "6271768", 2)


async def test_recheck_covers_old_misreads_skips_declined_and_retries_on_failure(env):  # noqa: F811
    eng, slack = env
    ids = []
    for pid in ("6087596", "6087597", "6087598"):
        d = sm_detail(pid=pid, status="利用完了", start_at=now() - timedelta(days=3, hours=3),
                      end_at=now() - timedelta(days=3), can_approve=False)
        eng.sm.details[pid] = d
        rid = await eng.on_new(Platform.SM, pid, trigger="seed", detail=d, silent=True)
        eng.db.update("reservations", rid, state=ResState.CLOSED.value)
        d.review_posted, d.review_open = False, True
        ids.append(rid)
    eng.db.update("reservations", ids[0], review_posted_at=iso(now()))  # 以前の版がバッジを誤読して「投稿済み」にした
    eng.db.kv_set(f"review_declined:{ids[1]}", "1")                    # 承認者が「レビューしない」と決めた
    orig = eng.sm.get_detail
    calls = {"n": 0}

    async def flaky(pid):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("一時的に読めない")
        return await orig(pid)
    eng.sm.get_detail = flaky
    await eng.recheck_unreviewed()
    assert eng.db.kv_get("review_recheck_v1") is None  # 読めない予約があったので、まだ済みにしない
    await eng.recheck_unreviewed()
    msg = next(t for t in slack.texts() if "レビューが未投稿" in t)
    assert "6087596" in msg and "6087598" in msg and "6087597" not in msg
    assert eng.db.kv_get("review_recheck_v1")


# ---------- 4回目のレビューの指摘（言い方の揺れ・読み切れない指示は安全側に） ----------
async def _finish_use(eng, pid="6271768"):
    d = eng.sm.details[pid]
    d.start_at, d.end_at, d.exit_status = now() - timedelta(hours=5), now() - timedelta(hours=2), "問題なく退室しました"
    rid = eng.db.reservation_by_pid("sm", pid)["id"]
    eng.db.update("reservations", rid, start_at=iso(d.start_at), end_at=iso(d.end_at))
    await eng.complete(rid, "exit_record")
    return rid


async def test_varied_no_review_phrases_before_use_and_before_approval(env):  # noqa: F811
    eng, slack = env
    rid, r = await new_sm(eng)  # 承認前（NOTIFIED）
    await eng.on_reply(A1, r["slack_ts"], "このゲストはレビューしなくていいです", "20.1")
    assert any("利用後のレビューは投稿しません" in t for t in slack.texts())
    await eng.on_reaction(A1, r["slack_ts"], "ok")
    await _finish_use(eng)
    assert eng.res(rid)["state"] == ResState.CLOSED and not any(c[0] == "review" for c in eng.sm.calls)


async def test_stars_without_leading_mark_before_use_is_recorded(env):  # noqa: F811
    eng, slack = env
    rid, r = await new_sm(eng)
    await eng.on_reaction(A1, r["slack_ts"], "ok")
    await eng.on_reply(A1, r["slack_ts"], "このゲストは評価★3でお願いします", "21.1")
    await _finish_use(eng)
    assert eng.res(rid)["state"] == ResState.REVIEW_CONFIRM and not any(c[0] == "review" for c in eng.sm.calls)
    assert "★3" in posts_text(slack)


async def test_star_emoji_codes_after_use_stop_auto_post(env):  # noqa: F811
    eng, slack = env
    rid, r = await _used_sm(eng)
    await eng.on_reply(A1, r["slack_ts"], ":star::star::star: でお願いします", "22.1")
    assert eng.res(rid)["state"] == ResState.REVIEW_CONFIRM
    await eng.tick_completion()
    assert not any(c[0] == "review" for c in eng.sm.calls)


async def test_review_words_with_open_draft_hold_review_and_keep_draft_flow(env):  # noqa: F811
    eng, slack = env
    rid, r = await _used_sm(eng)
    msgs = eng.sm.details["6271768"].messages
    msgs.append(Message("guest", "忘れ物はありませんでしたか？", index=len(msgs)))
    await eng.process_guest_messages(rid, msgs, "6271768")
    await eng.on_reply(A1, r["slack_ts"], "このゲストは評価★3で", "23.1")
    assert eng.res(rid)["state"] == ResState.REVIEW_CONFIRM  # 自動投稿は止まる
    await eng.tick_completion()
    assert not any(c[0] == "review" for c in eng.sm.calls)


async def test_invalid_comment_before_use_still_holds_review(env):  # noqa: F811
    eng, slack = env
    rid, r = await new_sm(eng)
    await eng.on_reaction(A1, r["slack_ts"], "ok")
    await eng.on_reply(A1, r["slack_ts"], "★2 コメント「汚れが目立った」", "24.1")
    await _finish_use(eng)
    assert eng.res(rid)["state"] == ResState.REVIEW_CONFIRM and not any(c[0] == "review" for c in eng.sm.calls)


async def test_recheck_retries_when_review_status_unknown_then_reports(env):  # noqa: F811
    eng, slack = env
    d = sm_detail(pid="6087599", status="利用完了", start_at=now() - timedelta(days=3, hours=3),
                  end_at=now() - timedelta(days=3), can_approve=False)
    eng.sm.details[d.platform_id] = d
    rid = await eng.on_new(Platform.SM, d.platform_id, trigger="seed", detail=d, silent=True)
    eng.db.update("reservations", rid, state=ResState.CLOSED.value)
    d.review_posted, d.review_open = None, None  # API から読み取れない
    for _ in range(5):
        await eng.recheck_unreviewed()
    assert eng.db.kv_get("review_recheck_v1") is None
    await eng.recheck_unreviewed()
    assert eng.db.kv_get("review_recheck_v1")
    assert any("6087599" in t and "確認できませんでした" in t for t in slack.texts())


# ---------- 5回目のレビューの指摘（自由文の扱いの作り直し） ----------
async def test_guest_facing_text_with_review_words_still_gets_revision_confirm(env):  # noqa: F811
    eng, slack = env
    rid, r = await new_sm(eng)
    await eng.on_reaction(A1, r["slack_ts"], "ok")  # 利用前・文案なし
    await eng.on_reply(A1, r["slack_ts"], "【修正】当日はお気をつけてお越しください。ご利用後にレビューをいただけますと幸いです。", "30.1")
    assert confirm_ts(eng)  # ゲストに送る文面の確認が出る（飲み込まれない）
    assert json.loads(eng.db.kv_get(f"review_pref:{rid}"))["hold"]  # 念のため自動投稿は止める


async def test_revision_after_use_in_review_confirm_is_sendable(env):  # noqa: F811
    eng, slack = env
    rid, r = await _used_sm(eng)
    eng.db.update("reservations", rid, state=ResState.REVIEW_CONFIRM.value)
    await eng.on_reply(A1, r["slack_ts"], "【修正】お忘れ物のお財布をお預かりしております。受け取り方法をご連絡ください。", "31.1")
    assert confirm_ts(eng)
    assert not any("評価が読み取れませんでした" in t for t in slack.texts())


async def test_stars_with_no_comment_or_double_negative_is_not_decline_and_decline_is_reversible(env):  # noqa: F811
    eng, slack = env
    rid, r = await _used_sm(eng)
    eng.db.update("reservations", rid, state=ResState.REVIEW_CONFIRM.value)
    await eng.on_reply(A1, r["slack_ts"], "レビューは★4、コメントなしで", "32.1")
    assert eng.res(rid)["state"] == ResState.REVIEW_CONFIRM and "★4" in posts_text(slack)
    await eng.on_reply(A1, r["slack_ts"], "レビューは止めなくていい、★5で投稿して", "32.2")
    assert eng.res(rid)["state"] == ResState.REVIEW_CONFIRM
    await eng.on_reply(A1, r["slack_ts"], "レビューはしない", "32.3")
    assert eng.res(rid)["state"] == ResState.CLOSED
    await eng.on_reply(A2, r["slack_ts"], "やっぱり★5で", "32.4")  # 期限内なら再開できる
    assert eng.res(rid)["state"] == ResState.REVIEW_CONFIRM
    await eng.on_reaction(A2, eng.db.kv_get(f"review_confirm_ts:{rid}"), "ok")
    assert eng.sm.calls[-1][:3] == ("review", "6271768", 5)


async def test_later_hold_overrides_earlier_approval(env):  # noqa: F811
    eng, slack = env
    rid, r = await new_sm(eng)
    await eng.on_reaction(A1, r["slack_ts"], "ok")
    # 承認者が★5を確認したが、まだ書けなかった（承認済みとして待っている）
    eng.db.kv_set(f"review_args:{rid}", json.dumps({"stars": 5, "comment": None, "auto": True, "approved": True}))
    await eng.on_reply(A1, r["slack_ts"], "レビューは待って", "33.1")  # 利用前の新しい指示
    await _finish_use(eng)
    assert eng.res(rid)["state"] == ResState.REVIEW_CONFIRM and not any(c[0] == "review" for c in eng.sm.calls)


async def test_instruction_during_completing_is_answered_honestly(env):  # noqa: F811
    eng, slack = env
    rid, r = await _used_sm(eng)
    eng.db.update("reservations", rid, state=ResState.COMPLETING.value)
    await eng.on_reply(A1, r["slack_ts"], "このゲストはレビューしない", "34.1")
    assert any("止められない可能性があります" in t for t in slack.texts())
    assert json.loads(eng.db.kv_get(f"review_pref:{rid}"))["decline"]


async def test_review_args_saved_before_slow_steps(env):  # noqa: F811
    eng, slack = env
    rid, r = await _used_sm(eng)
    eng.db.kv_set(f"review_args:{rid}", json.dumps({"stars": 2, "comment": "清掃が不十分な箇所がありました。", "auto": False}))
    eng.db.update("reservations", rid, state=ResState.REVIEW_CONFIRM.value)
    seen = {}

    async def slow_thanks(purpose):
        seen["args"] = json.loads(eng.db.kv_get(f"review_args:{rid}"))  # お礼文を作っている間に止まった場合に回収される内容
        return "この度はBar nullをご利用いただき、誠にありがとうございました！"
    eng.llm.thanks_comment = slow_thanks
    await eng.post_review(rid, 5, None, A2, approved=True)
    assert seen["args"]["stars"] == 5 and seen["args"]["comment"] is None  # 撤回した★2のコメントは残らない


async def test_review_with_evaluation_number_phrase(env):  # noqa: F811
    eng, slack = env
    rid, r = await _used_sm(eng)
    eng.db.update("reservations", rid, state=ResState.REVIEW_CONFIRM.value)
    await eng.on_reply(A1, r["slack_ts"], "評価は5でお願いします", "35.1")
    assert "★5" in posts_text(slack) and eng.db.kv_get(f"review_confirm_ts:{rid}")


# ---------- 6回目のレビューの指摘（指示の取りこぼし・投稿中の指示・二重投稿） ----------
LONG_COMMENT = "この度はご利用いただきありがとうございました。お部屋もきれいに使っていただき大変助かりました。またのご利用をお待ちしております。"


def _reviews(eng):
    return [c for c in eng.sm.calls if c[0] == "review"]


def _review_confirm(eng, rid) -> dict:
    ts = eng.db.kv_get(f"review_confirm_ts:{rid}")
    row = eng.db.one("SELECT extra FROM slack_messages WHERE ts=?", ts)
    return {"ts": ts, **json.loads(row["extra"] or "{}")}


async def _add_guest_draft(eng, rid):
    msgs = eng.sm.details["6271768"].messages
    msgs.append(Message("guest", "忘れ物はありませんでしたか？", index=len(msgs)))
    await eng.process_guest_messages(rid, msgs, "6271768")
    assert eng.open_drafts(rid)


async def _in_review_confirm(eng):
    rid, r = await _used_sm(eng)
    eng.sm.details["6271768"].exit_status = "気になる点があります"
    await eng.complete(rid, "exit_record")
    assert eng.res(rid)["state"] == ResState.REVIEW_CONFIRM
    return rid, r


async def _completing(eng, rid, outcome):
    """投稿の処理中（COMPLETING）の間に返信を受ける。outcome: 管理画面の操作の結果（例外を投げるか、結果を返す関数）"""
    gate = asyncio.Event()

    async def slow(pid, stars, comment, dry_run):
        await gate.wait()
        return outcome(pid, stars, comment)
    eng.sm.post_review = slow
    task = asyncio.create_task(eng.post_review(rid, 5, None, "system", from_states=(ResState.CONFIRMED,)))
    for _ in range(50):
        await asyncio.sleep(0.01)
        if eng.res(rid)["state"] == ResState.COMPLETING:
            break
    assert eng.res(rid)["state"] == ResState.COMPLETING
    return gate, task


def _raise(exc):
    def f(*a):
        raise exc
    return f


async def test_polite_star_instruction_with_open_draft_is_taken(env):  # noqa: F811
    eng, slack = env
    rid, r = await _in_review_confirm(eng)
    await _add_guest_draft(eng, rid)
    await eng.on_reply(A1, r["slack_ts"], "★3でお願いします", "60.1")
    c = _review_confirm(eng, rid)
    assert c["stars"] == 3
    await eng.on_reaction(A2, c["ts"], "ok")
    assert _reviews(eng)[-1][2] == 3


async def test_long_comment_instruction_is_taken(env):  # noqa: F811
    eng, slack = env
    rid, r = await _in_review_confirm(eng)
    await eng.on_reply(A1, r["slack_ts"], f"★4 コメント「{LONG_COMMENT}」", "61.1")
    c = _review_confirm(eng, rid)
    assert c["stars"] == 4 and c["comment"] == LONG_COMMENT
    assert not any("評価が読み取れませんでした" in t for t in slack.texts())


async def test_ambiguous_reply_keeps_earlier_decline_and_stars(env):  # noqa: F811
    eng, slack = env
    rid, r = await new_sm(eng)
    await eng.on_reaction(A1, r["slack_ts"], "ok")
    await eng.on_reply(A1, r["slack_ts"], "このゲストはレビューしない", "62.1")
    await eng.on_reply(A1, r["slack_ts"], "【修正】当日はお気をつけてお越しください。ご利用後にレビューをいただけますと幸いです。", "62.2")
    assert json.loads(eng.db.kv_get(f"review_pref:{rid}")) == {"decline": True}
    await _finish_use(eng)
    assert eng.res(rid)["state"] == ResState.CLOSED and not _reviews(eng)


async def test_ambiguous_reply_keeps_earlier_stars(env):  # noqa: F811
    eng, slack = env
    rid, r = await new_sm(eng)
    await eng.on_reaction(A1, r["slack_ts"], "ok")
    await eng.on_reply(A1, r["slack_ts"], "★2 コメント「清掃が不十分な箇所がありました。」", "63.1")
    await eng.on_reply(A1, r["slack_ts"], "【修正】当日はお気をつけてお越しください。ご利用後にレビューをいただけますと幸いです。", "63.2")
    await _finish_use(eng)
    c = _review_confirm(eng, rid)
    assert c["stars"] == 2 and c["comment"] == "清掃が不十分な箇所がありました。" and not _reviews(eng)


async def test_decline_during_posting_is_followed_when_post_fails(env):  # noqa: F811
    eng, slack = env
    rid, r = await _used_sm(eng)
    gate, task = await _completing(eng, rid, _raise(ActionFailed("レビュー", "サイトのエラー")))
    await eng.on_reply(A1, r["slack_ts"], "このゲストはレビューしない", "64.1")
    assert any("止められない可能性があります" in t for t in slack.texts())
    gate.set()
    await task
    assert eng.res(rid)["state"] == ResState.REVIEW_ERROR
    assert eng.db.kv_get(f"review_error_ts:{rid}") == "-"  # 前の内容での再実行のスタンプは出さない
    assert not eng.db.one("SELECT 1 FROM slack_messages WHERE reservation_id=? AND kind=?", rid, MsgKind.ERROR.value)
    await eng.on_reply(A1, r["slack_ts"], "レビューしない", "64.2")
    assert eng.res(rid)["state"] == ResState.CLOSED and not _reviews(eng)


async def test_stars_during_posting_become_the_retry_content(env):  # noqa: F811
    eng, slack = env
    rid, r = await _used_sm(eng)
    gate, task = await _completing(eng, rid, _raise(ActionFailed("レビュー", "サイトのエラー")))
    await eng.on_reply(A1, r["slack_ts"], "★3 コメント「次回は片付けにご協力をお願いします。」", "65.1")
    gate.set()
    await task
    row = eng.db.one("SELECT ts, extra FROM slack_messages WHERE reservation_id=? AND kind=? ORDER BY created_at DESC",
                     rid, MsgKind.ERROR.value)
    extra = json.loads(row["extra"])
    assert extra["stars"] == 3 and extra["comment"] == "次回は片付けにご協力をお願いします。"


async def test_decline_during_posting_then_not_ready_closes(env):  # noqa: F811
    eng, slack = env
    rid, r = await _used_sm(eng)
    gate, task = await _completing(eng, rid, _raise(NotReady("レビュー", "まだ書けません")))
    await eng.on_reply(A1, r["slack_ts"], "このゲストはレビューしない", "66.1")
    gate.set()
    await task
    assert eng.res(rid)["state"] == ResState.CLOSED and eng.db.kv_get(f"review_declined:{rid}")


async def test_hold_during_posting_is_recorded(env):  # noqa: F811
    eng, slack = env
    rid, r = await _used_sm(eng)
    gate, task = await _completing(eng, rid, _raise(NotReady("レビュー", "まだ書けません")))
    await eng.on_reply(A1, r["slack_ts"], "ちょっと待って", "67.1")
    assert json.loads(eng.db.kv_get(f"review_pref:{rid}")).get("hold")
    gate.set()
    await task
    del eng.sm.post_review
    await eng.complete(rid, "exit_record")
    assert eng.res(rid)["state"] == ResState.REVIEW_CONFIRM and not _reviews(eng)


async def test_old_confirm_is_not_reused_after_newer_instruction(env):  # noqa: F811
    eng, slack = env
    rid, r = await _in_review_confirm(eng)
    ts = eng.db.kv_get(f"review_confirm_ts:{rid}")
    eng.sm.post_review = _async_raise(NotReady("レビュー", "まだ書けません"))
    await eng.on_reaction(A1, ts, "ok")
    assert eng.res(rid)["state"] == ResState.CONFIRMED  # 承認済みで、書けるのを待っている
    await eng.on_reaction(A2, ts, "white_check_mark")
    assert any("確認済みです" in t for t in slack.texts())
    await eng.on_reply(A1, r["slack_ts"], "このゲストはレビューしない", "68.1")
    assert eng.res(rid)["state"] == ResState.CLOSED
    n = len(eng.sm.calls)
    await eng.on_reaction(A2, ts, "+1")
    assert len(eng.sm.calls) == n and eng.res(rid)["state"] == ResState.CLOSED


def _async_raise(exc):
    async def f(*a):
        raise exc
    return f


async def test_approved_review_waiting_is_kept(env):  # noqa: F811
    eng, slack = env
    rid, r = await _in_review_confirm(eng)
    await eng.on_reply(A1, r["slack_ts"], "★3 コメント「次回は片付けにご協力をお願いします。」", "69.1")
    eng.sm.post_review = _async_raise(NotReady("レビュー", "まだ書けません"))
    await eng.on_reaction(A2, eng.db.kv_get(f"review_confirm_ts:{rid}"), "ok")
    assert eng.res(rid)["state"] == ResState.CONFIRMED
    await eng.on_reply(A1, r["slack_ts"], "了解です", "69.2")  # レビューに関係ない返信では止めない
    assert eng.res(rid)["state"] == ResState.CONFIRMED
    await eng.on_reply(A1, r["slack_ts"], "ちょっと待って", "69.3")  # 待って → 確認し直す（内容は承認済みの★3のまま）
    c = _review_confirm(eng, rid)
    assert eng.res(rid)["state"] == ResState.REVIEW_CONFIRM
    assert c["stars"] == 3 and c["comment"] == "次回は片付けにご協力をお願いします。"


async def test_comment_only_instruction_with_draft_stops_auto_post(env):  # noqa: F811
    eng, slack = env
    rid, r = await _used_sm(eng)
    await _add_guest_draft(eng, rid)
    await eng.on_reply(A1, r["slack_ts"], "コメントは「またのご利用をお待ちしております。ありがとうございました。」で", "70.1")
    c = _review_confirm(eng, rid)
    assert eng.res(rid)["state"] == ResState.REVIEW_CONFIRM
    assert c["stars"] == 5 and c["comment"] == "またのご利用をお待ちしております。ありがとうございました。"
    await eng.complete(rid, "exit_record")
    assert not _reviews(eng)


async def test_deny_with_review_instruction_does_both(env):  # noqa: F811
    eng, slack = env
    rid, r = await new_sm(eng)
    await eng.on_reply(A1, r["slack_ts"], "否認して。レビューもなしで", "71.1")
    assert json.loads(eng.db.kv_get(f"review_pref:{rid}")) == {"decline": True}
    assert any("否認・キャンセルは自動では実行しません" in t for t in slack.texts())
    assert eng.res(rid)["state"] == ResState.ON_HOLD


async def test_review_wait_words_do_not_hold_the_booking(env):  # noqa: F811
    eng, slack = env
    rid, r = await new_sm(eng)
    await eng.on_reply(A1, r["slack_ts"], "レビューは待って", "72.1")
    assert eng.res(rid)["state"] == ResState.NOTIFIED  # 予約の承認待ちは保留にしない
    assert json.loads(eng.db.kv_get(f"review_pref:{rid}")).get("hold")


async def test_star_emoji_only_reply(env):  # noqa: F811
    eng, slack = env
    rid, r = await _in_review_confirm(eng)
    await eng.on_reply(A1, r["slack_ts"], ":star::star::star:", "73.1")
    assert _review_confirm(eng, rid)["stars"] == 3


async def test_not_yet_is_a_hold_not_a_decline(env):  # noqa: F811
    eng, slack = env
    rid, r = await _used_sm(eng)
    await eng.on_reply(A1, r["slack_ts"], "レビューはまだしないで", "74.1")
    assert eng.res(rid)["state"] == ResState.REVIEW_CONFIRM and not eng.db.kv_get(f"review_declined:{rid}")
    await eng.on_reply(A1, r["slack_ts"], "レビューは一旦止めて", "74.2")
    assert eng.res(rid)["state"] == ResState.REVIEW_CONFIRM
    assert any("スタンプを押すまで投稿しません" in t for t in slack.texts())


async def test_conflicting_stars_are_not_guessed(env):  # noqa: F811
    eng, slack = env
    rid, r = await _in_review_confirm(eng)
    before = eng.db.kv_get(f"review_confirm_ts:{rid}")
    await eng.on_reply(A1, r["slack_ts"], "レビューは★5じゃなくて★3で", "75.1")
    assert eng.db.kv_get(f"review_confirm_ts:{rid}") == before
    assert any("評価が複数書かれていて" in t for t in slack.texts())


async def test_review_instruction_on_closed_reservation_is_answered(env):  # noqa: F811
    eng, slack = env
    rid, r = await _used_sm(eng)
    await eng.complete(rid, "exit_record")
    assert eng.res(rid)["state"] == ResState.CLOSED and _reviews(eng)
    await eng.on_reply(A1, r["slack_ts"], "★3 コメント「次回は片付けにご協力をお願いします。」", "76.1")
    assert any("対応を終了しています" in t for t in slack.texts())


async def test_deny_after_use_does_not_promise_reminders(env):  # noqa: F811
    eng, slack = env
    rid, r = await _used_sm(eng)
    await eng.on_reply(A1, r["slack_ts"], "否認で", "77.1")
    assert eng.res(rid)["state"] == ResState.REVIEW_CONFIRM
    assert not any("リマインドします" in t for t in slack.texts())


async def test_invalid_comment_is_rejected_while_posting_and_when_reopening(env):  # noqa: F811
    eng, slack = env
    rid, r = await _used_sm(eng)
    gate, task = await _completing(eng, rid, _raise(NotReady("レビュー", "まだ書けません")))
    await eng.on_reply(A1, r["slack_ts"], "★2 コメント「汚れが目立った」", "78.1")
    assert any("10〜400 字" in t for t in slack.texts())
    assert json.loads(eng.db.kv_get(f"review_pref:{rid}")) == {"hold": True}
    gate.set()
    await task
    del eng.sm.post_review
    await eng.on_reply(A1, r["slack_ts"], "レビューはしない", "78.2")
    eng.db.update("reservations", rid, state=ResState.CLOSED.value)
    eng.db.kv_set(f"review_declined:{rid}", "1")
    await eng.on_reply(A1, r["slack_ts"], "★2 コメント「汚れあり」", "78.3")
    assert eng.res(rid)["state"] == ResState.CLOSED and eng.db.kv_get(f"review_declined:{rid}")


async def test_failure_after_submit_offers_no_retry_stamp(env):  # noqa: F811
    eng, slack = env
    rid, r = await _used_sm(eng)

    async def submitted_then_failed(pid, stars, comment, dry_run):
        review_submitted(True)
        raise ActionFailed("レビュー", "レビュー投稿の通信が失敗しました")
    eng.sm.post_review = submitted_then_failed
    await eng.post_review(rid, 5, None, "system", from_states=(ResState.CONFIRMED,))
    assert eng.res(rid)["state"] == ResState.REVIEW_ERROR
    assert eng.db.kv_get(f"review_error_ts:{rid}") == "-"
    assert any("投稿されたか分かりません" in t for t in slack.texts())
    # 返信で★5 → 確認メッセージ → スタンプで（管理画面を確かめてから）投稿する
    del eng.sm.post_review
    await eng.on_reply(A1, r["slack_ts"], "★5", "79.1")
    await eng.on_reaction(A2, eng.db.kv_get(f"review_confirm_ts:{rid}"), "ok")
    assert len(_reviews(eng)) == 1


async def test_rejected_by_site_after_submit_can_be_retried(env):  # noqa: F811
    eng, slack = env
    rid, r = await _used_sm(eng)

    async def rejected(pid, stars, comment, dry_run):
        review_submitted(True)
        review_submitted(False)  # サイトが受け付けなかった（投稿されていない）
        raise ActionFailed("レビュー", "レビューの投稿がサイト側で受け付けられませんでした（応答 422）")
    eng.sm.post_review = rejected
    await eng.post_review(rid, 5, None, "system", from_states=(ResState.CONFIRMED,))
    ts = eng.db.kv_get(f"review_error_ts:{rid}")
    assert ts and ts != "-"
    del eng.sm.post_review
    await eng.on_reaction(A1, ts, "ok")
    assert len(_reviews(eng)) == 1 and eng.res(rid)["state"] == ResState.CLOSED


async def test_timeout_after_submit_waits_instead_of_cancelling(env, monkeypatch):  # noqa: F811
    eng, slack = env
    rid, r = await _used_sm(eng)
    monkeypatch.setattr("barnull.workflows.handlers.REVIEW_TIMEOUT_SEC", 0.05)

    async def slow_after_submit(pid, stars, comment, dry_run):
        review_submitted(True)
        await asyncio.sleep(0.2)  # 投稿後の確認の読み直しが長引いた
        eng.sm.calls.append(("review", pid, stars, comment))
        return ActionResult(ok=True, note="投稿しました")
    eng.sm.post_review = slow_after_submit
    await eng.post_review(rid, 5, None, "system", from_states=(ResState.CONFIRMED,))
    assert eng.res(rid)["state"] == ResState.CLOSED and len(_reviews(eng)) == 1


async def test_timeout_before_submit_cancels_and_can_retry(env, monkeypatch):  # noqa: F811
    eng, slack = env
    rid, r = await _used_sm(eng)
    monkeypatch.setattr("barnull.workflows.handlers.REVIEW_TIMEOUT_SEC", 0.05)

    async def stuck(pid, stars, comment, dry_run):
        await asyncio.sleep(5)  # 画面を開けない（投稿のボタンの前）
    eng.sm.post_review = stuck
    await eng.post_review(rid, 5, None, "system", from_states=(ResState.CONFIRMED,))
    assert eng.res(rid)["state"] == ResState.REVIEW_ERROR
    ts = eng.db.kv_get(f"review_error_ts:{rid}")
    assert ts and ts != "-"


async def test_recover_stuck_after_submit_offers_no_retry(env):  # noqa: F811
    eng, slack = env
    rid, r = await _used_sm(eng)
    eng.db.update("reservations", rid, state=ResState.COMPLETING.value)
    eng.db.kv_set(f"review_args:{rid}", json.dumps({"stars": 5, "comment": "ありがとうございました。またどうぞ。", "auto": True}))
    eng.db.kv_set(f"review_submitted:{rid}", iso(now()))
    await eng.recover_stuck(0)
    assert eng.res(rid)["state"] == ResState.REVIEW_ERROR
    assert eng.db.kv_get(f"review_error_ts:{rid}") == "-"
    assert any("投稿されたか分かりません" in t for t in slack.texts())


async def test_hold_for_booking_plus_no_review(env):  # noqa: F811
    eng, slack = env
    rid, r = await new_sm(eng)
    await eng.on_reply(A1, r["slack_ts"], "ちょっと待って。レビューもしないで", "80.1")
    assert json.loads(eng.db.kv_get(f"review_pref:{rid}")) == {"decline": True}
    assert eng.res(rid)["state"] == ResState.ON_HOLD


# ---------- 7回目のレビューの指摘（ゲスト宛ての文面の取り違え・古いスタンプの競合） ----------
def _llm_says_revision(eng):
    """本物の Claude は、ゲスト宛ての敬語の文面を REVISION と判定する（偽物は【修正】で始まる時だけ）"""
    async def classify(reply, drafts):
        return Classification("REVISION", 0.9, None)
    eng.llm.classify_reply = classify


async def _guest_asks_about_review(eng, rid):
    msgs = eng.sm.details["6271768"].messages
    msgs.append(Message("guest", "レビューは書いたほうがいいですか？", index=len(msgs)))
    await eng.process_guest_messages(rid, msgs, "6271768")
    assert eng.open_drafts(rid)


@pytest.mark.parametrize("reply", [
    "ご質問ありがとうございます。レビューは不要ですのでお気遣いなく。",
    "お問い合わせありがとうございました！レビューはなしでも問題ございません！！",
    "ご質問ありがとうございます。レビューは任意ですので不要です。\nBar null スタッフ",
])
async def test_polite_guest_reply_with_review_words_is_not_a_decline(env, reply):  # noqa: F811
    eng, slack = env
    _llm_says_revision(eng)
    rid, r = await _used_sm(eng)
    await _guest_asks_about_review(eng, rid)
    await eng.on_reply(A1, r["slack_ts"], reply, "90.1")
    assert eng.res(rid)["state"] != ResState.CLOSED and not eng.db.kv_get(f"review_declined:{rid}")
    assert confirm_ts(eng)  # ゲストへの送信の確認が出る
    assert not _reviews(eng)


async def test_polite_guest_reply_with_star_words_before_use(env):  # noqa: F811
    eng, slack = env
    _llm_says_revision(eng)
    rid, r = await new_sm(eng)
    await eng.on_reaction(A1, r["slack_ts"], "ok")
    await _guest_asks_about_review(eng, rid)
    await eng.on_reply(A1, r["slack_ts"], "申し訳ございませんが、星5の評価による割引はございません。ご了承くださいませ。", "91.1")
    assert confirm_ts(eng)
    assert json.loads(eng.db.kv_get(f"review_pref:{rid}") or "{}").get("stars") in (None, 5)


async def test_decorative_star_run_is_not_a_rating(env):  # noqa: F811
    eng, slack = env
    _llm_says_revision(eng)
    rid, r = await _used_sm(eng)
    await _add_guest_draft(eng, rid)
    await eng.on_reply(A1, r["slack_ts"], "★★ご案内★★ 駐車場はございませんので、近隣のコインパーキングをご利用ください。", "92.1")
    assert confirm_ts(eng)
    row = eng.db.one("SELECT extra FROM slack_messages WHERE reservation_id=? AND kind=? ORDER BY created_at DESC",
                     rid, MsgKind.REVIEW_CONFIRM.value)
    assert row is None or json.loads(row["extra"]).get("stars") != 2


async def test_star_run_only_reply_is_a_rating(env):  # noqa: F811
    eng, slack = env
    rid, r = await _in_review_confirm(eng)
    await eng.on_reply(A1, r["slack_ts"], "★★★☆☆で", "93.1")
    assert _review_confirm(eng, rid)["stars"] == 3


async def test_closed_note_does_not_swallow_guest_thanks(env):  # noqa: F811
    eng, slack = env
    _llm_says_revision(eng)
    rid, r = await _used_sm(eng)
    await eng.complete(rid, "exit_record")
    assert eng.res(rid)["state"] == ResState.CLOSED
    msgs = eng.sm.details["6271768"].messages
    msgs.append(Message("guest", "素敵なレビューをありがとうございました！また使わせていただきます。", index=len(msgs)))
    await eng.process_guest_messages(rid, msgs, "6271768")
    await eng.on_reply(A1, r["slack_ts"], "★5のレビューありがとうございました。またのご利用をお待ちしております。", "94.1")
    assert confirm_ts(eng) and not any("対応を終了しています" in t for t in slack.texts())


async def test_quote_labelled_as_guest_reply_is_not_the_review_comment(env):  # noqa: F811
    eng, slack = env
    rid, r = await _used_sm(eng)
    await _add_guest_draft(eng, rid)
    await eng.on_reply(A1, r["slack_ts"], "レビューは★5で。返信は「お忘れ物はございませんでした。ご確認ありがとうございます。」で", "95.1")
    rows = eng.db.all("SELECT extra FROM slack_messages WHERE reservation_id=? AND kind=?", rid, MsgKind.REVIEW_CONFIRM.value)
    assert all(json.loads(x["extra"]).get("comment") != "お忘れ物はございませんでした。ご確認ありがとうございます。" for x in rows)


async def test_review_sentence_hold_words_do_not_hold_booking(env):  # noqa: F811
    eng, slack = env
    rid, r = await new_sm(eng)
    for i, reply in enumerate(["レビューは★5で、一旦止めておいて", "★4で、コメントは一旦なしで", "★5で。コメントはちょっと待って"]):
        await eng.on_reply(A1, r["slack_ts"], reply, f"96.{i}")
        assert eng.res(rid)["state"] == ResState.NOTIFIED, reply


async def test_cancellation_fee_sentence_is_not_a_deny(env):  # noqa: F811
    eng, slack = env
    rid, r = await new_sm(eng)
    await eng.on_reply(A1, r["slack_ts"], "★5で。キャンセル料の件はこちらで確認します", "97.1")
    assert eng.res(rid)["state"] == ResState.NOTIFIED
    assert not any("否認・キャンセルは自動では実行しません" in t for t in slack.texts())


async def test_deny_plus_decline_while_posting_warns_once(env):  # noqa: F811
    eng, slack = env
    rid, r = await _used_sm(eng)
    gate, task = await _completing(eng, rid, _raise(NotReady("レビュー", "まだ書けません")))
    await eng.on_reply(A1, r["slack_ts"], "待って。レビューもしないで", "98.1")
    assert sum("止められない可能性があります" in t for t in slack.texts()) == 1
    gate.set()
    await task
    assert eng.res(rid)["state"] == ResState.CLOSED


async def test_ambiguous_decline_words_while_posting_are_not_a_decline(env):  # noqa: F811
    eng, slack = env
    rid, r = await _used_sm(eng)
    gate, task = await _completing(eng, rid, _raise(NotReady("レビュー", "まだ書けません")))
    await eng.on_reply(A1, r["slack_ts"], "【修正】レビューはしなくて大丈夫です。またのご利用をお待ちしております。", "99.1")
    assert not json.loads(eng.db.kv_get(f"review_pref:{rid}")).get("decline")
    gate.set()
    await task
    assert eng.res(rid)["state"] != ResState.CLOSED


async def test_comment_only_instruction_while_posting_is_the_retry_content(env):  # noqa: F811
    eng, slack = env
    rid, r = await _used_sm(eng)
    gate, task = await _completing(eng, rid, _raise(ActionFailed("レビュー", "サイトのエラー")))
    await eng.on_reply(A1, r["slack_ts"], "コメントは「またのご利用をお待ちしております。ありがとうございました。」で", "100.1")
    gate.set()
    await task
    row = eng.db.one("SELECT extra FROM slack_messages WHERE reservation_id=? AND kind=? ORDER BY created_at DESC",
                     rid, MsgKind.ERROR.value)
    assert json.loads(row["extra"])["comment"] == "またのご利用をお待ちしております。ありがとうございました。"


async def test_stale_confirm_stamp_waiting_for_lock_does_not_post_old_content(env):  # noqa: F811
    eng, slack = env
    rid, r = await _in_review_confirm(eng)
    c1 = eng.db.kv_get(f"review_confirm_ts:{rid}")
    await eng._locks[rid].acquire()  # 巡回などが同じ予約を処理中
    task = asyncio.create_task(eng.on_reaction(A2, c1, "ok"))
    await asyncio.sleep(0.05)
    await eng.on_reply(A1, r["slack_ts"], "★3 コメント「次回は片付けにご協力をお願いします。ありがとうございました。」", "101.1")
    eng._locks[rid].release()
    await task
    assert not _reviews(eng) and eng.res(rid)["state"] == ResState.REVIEW_CONFIRM
    await eng.on_reaction(A2, eng.db.kv_get(f"review_confirm_ts:{rid}"), "ok")
    assert _reviews(eng)[-1][2] == 3


async def test_two_retry_stamps_waiting_for_lock_post_once(env):  # noqa: F811
    eng, slack = env
    rid, r = await _used_sm(eng)
    eng.sm.post_review = _async_raise(ActionFailed("レビュー", "サイトのエラー"))
    await eng.post_review(rid, 5, None, "system", from_states=(ResState.CONFIRMED,))
    e1 = eng.db.kv_get(f"review_error_ts:{rid}")

    async def submitted_then_failed(pid, stars, comment, dry_run):
        review_submitted(True)
        eng.sm.calls.append(("review", pid, stars, comment))
        raise ActionFailed("レビュー", "通信が失敗しました")
    eng.sm.post_review = submitted_then_failed
    await eng._locks[rid].acquire()
    t1 = asyncio.create_task(eng.on_reaction(A1, e1, "ok"))
    t2 = asyncio.create_task(eng.on_reaction(A2, e1, "white_check_mark"))
    await asyncio.sleep(0.05)
    eng._locks[rid].release()
    await asyncio.gather(t1, t2)
    assert len(_reviews(eng)) == 1


async def test_hold_reply_when_post_status_unknown_explains(env):  # noqa: F811
    eng, slack = env
    rid, r = await _used_sm(eng)

    async def submitted_then_failed(pid, stars, comment, dry_run):
        review_submitted(True)
        raise ActionFailed("レビュー", "通信が失敗しました")
    eng.sm.post_review = submitted_then_failed
    await eng.post_review(rid, 5, None, "system", from_states=(ResState.CONFIRMED,))
    await eng.on_reply(A1, r["slack_ts"], "ちょっと待って", "102.1")
    assert any("投稿されたか分からない状態です" in t for t in slack.texts())


async def test_starry_sky_sentence_is_not_review_related(env):  # noqa: F811
    eng, slack = env
    rid, r = await new_sm(eng)
    await eng.on_reaction(A1, r["slack_ts"], "ok")
    await eng.on_reply(A1, r["slack_ts"], "星空がきれいに見える日もございます。当日お待ちしております。", "103.1")
    assert not eng.db.kv_get(f"review_pref:{rid}")


# ---------- 8回目のレビューの指摘 ----------
async def test_polite_guest_reply_without_draft_is_not_a_decline(env):  # noqa: F811
    eng, slack = env
    _llm_says_revision(eng)
    rid, r = await _used_sm(eng)
    await eng.on_reply(A1, r["slack_ts"], "ご丁寧にありがとうございます。レビューは不要ですので、どうぞお気遣いなく。", "110.1")
    assert eng.res(rid)["state"] != ResState.CLOSED and confirm_ts(eng) and not _reviews(eng)


async def test_short_polite_instruction_without_draft_is_still_an_instruction(env):  # noqa: F811
    eng, slack = env
    rid, r = await new_sm(eng)
    await eng.on_reply(A1, r["slack_ts"], "このゲストはレビューしなくていいです", "111.1")
    assert json.loads(eng.db.kv_get(f"review_pref:{rid}")) == {"decline": True}


async def test_comment_with_colon_is_read(env):  # noqa: F811
    eng, slack = env
    rid, r = await _used_sm(eng)
    await _add_guest_draft(eng, rid)
    await eng.on_reply(A1, r["slack_ts"], "コメント：「お部屋をきれいに使っていただきありがとうございました。」", "112.1")
    c = _review_confirm(eng, rid)
    assert c["comment"] == "お部屋をきれいに使っていただきありがとうございました。"
    await eng.complete(rid, "exit_record")
    assert not _reviews(eng)


async def test_ambiguous_star_text_keeps_earlier_explicit_instruction(env):  # noqa: F811
    eng, slack = env
    _llm_says_revision(eng)
    rid, r = await new_sm(eng)
    await eng.on_reaction(A1, r["slack_ts"], "ok")
    await eng.on_reply(A1, r["slack_ts"], "★2 コメント「清掃が不十分な箇所がありました。次回はご協力をお願いします。」", "113.1")
    await _add_guest_draft(eng, rid)
    await eng.on_reply(A1, r["slack_ts"], "特にございません。お気に召しましたら、ご利用後に★5で評価いただけますと幸いです。", "113.2")
    pref = json.loads(eng.db.kv_get(f"review_pref:{rid}"))
    assert pref["stars"] == 2 and pref["comment"].startswith("清掃が不十分")


async def test_second_quote_is_not_part_of_the_comment(env):  # noqa: F811
    eng, slack = env
    rid, r = await _used_sm(eng)
    await _add_guest_draft(eng, rid)
    await eng.on_reply(A1, r["slack_ts"], "★4 コメント「お部屋をきれいに使っていただきありがとうございました。」、"
                                          "ゲストへの返信は「お忘れ物はございませんでした。」で", "114.1")
    rows = eng.db.all("SELECT extra FROM slack_messages WHERE reservation_id=? AND kind=?", rid, MsgKind.REVIEW_CONFIRM.value)
    assert all("お忘れ物" not in (json.loads(x["extra"]).get("comment") or "") for x in rows)


async def test_star_run_with_comment(env):  # noqa: F811
    eng, slack = env
    rid, r = await _in_review_confirm(eng)
    await eng.on_reply(A1, r["slack_ts"], "★★★ コメント「次回は片付けにご協力をお願いします。ありがとうございました。」", "115.1")
    c = _review_confirm(eng, rid)
    assert c["stars"] == 3 and c["comment"] == "次回は片付けにご協力をお願いします。ありがとうございました。"


async def test_no_contradictory_unreadable_note_after_review_reply(env):  # noqa: F811
    eng, slack = env
    rid, r = await _used_sm(eng)
    await eng.on_reply(A1, r["slack_ts"], "★4 コメント：「お部屋をきれいに使っていただきありがとうございました。」「追伸」", "116.1")
    assert not any("評価が読み取れませんでした" in t for t in slack.texts())


async def test_clear_instruction_on_closed_booking_with_draft_is_answered(env):  # noqa: F811
    eng, slack = env
    rid, r = await _used_sm(eng)
    await eng.complete(rid, "exit_record")
    await _add_guest_draft(eng, rid)
    await eng.on_reply(A1, r["slack_ts"], "★3でお願いします", "117.1")
    assert any("対応を終了しています" in t for t in slack.texts())


async def test_past_tense_wait_is_not_a_hold(env):  # noqa: F811
    eng, slack = env
    rid, r = await new_sm(eng)
    await eng.on_reply(A1, r["slack_ts"], "このゲストは★3で。前回は退室が遅れて、次の予約の方が外で待っていたので", "118.1")
    assert eng.res(rid)["state"] == ResState.NOTIFIED


async def test_failed_confirm_post_reverts_and_old_confirm_is_refused(env):  # noqa: F811
    eng, slack = env
    rid, r = await _in_review_confirm(eng)
    c1 = eng.db.kv_get(f"review_confirm_ts:{rid}")
    orig = eng.post

    async def post(**kw):
        if kw.get("kind") is MsgKind.REVIEW_CONFIRM:
            return None  # Slack に出せない
        return await orig(**kw)
    eng.post = post
    await eng.on_reply(A1, r["slack_ts"], "★3", "119.1")
    eng.post = orig
    assert eng.res(rid)["state"] == ResState.REVIEW_CONFIRM
    await eng.on_reaction(A2, c1, "ok")
    assert not _reviews(eng)  # 置き換えようとした古い確認からは投稿しない


async def test_auto_post_waiting_for_lock_respects_new_instruction(env):  # noqa: F811
    eng, slack = env
    rid, r = await _used_sm(eng)
    await eng._locks[rid].acquire()
    task = asyncio.create_task(eng.post_review(rid, 5, None, "system", from_states=(ResState.CONFIRMED,)))
    await asyncio.sleep(0.05)
    eng.db.kv_set(f"review_pref:{rid}", json.dumps({"decline": True}))  # 待っている間の「レビューしない」
    eng._locks[rid].release()
    assert await task is False and not _reviews(eng)


async def test_live_stamp_is_not_processed_again_by_catch_up(env):  # noqa: F811
    eng, slack = env
    rid, r = await _in_review_confirm(eng)
    ts = eng.db.kv_get(f"review_confirm_ts:{rid}")
    await eng.on_reaction(A2, ts, "ok", event_ts="120.1")
    await eng.on_reaction(A2, ts, "ok")  # catch_up が同じスタンプを拾い直す
    assert len(_reviews(eng)) == 1


# ---------- 9回目のレビューの指摘 ----------
@pytest.mark.parametrize("reply, stars", [("星は3つでお願いします", 3), ("星を4つで", 4), ("星は2で", 2)])
async def test_star_with_particles_is_read(env, reply, stars):  # noqa: F811
    eng, slack = env
    rid, r = await new_sm(eng)
    await eng.on_reaction(A1, r["slack_ts"], "ok")
    await eng.on_reply(A1, r["slack_ts"], reply, "130.1")
    assert json.loads(eng.db.kv_get(f"review_pref:{rid}"))["stars"] == stars
    await _finish_use(eng)
    assert not _reviews(eng)


async def test_rating_scale_words_are_not_stars(env):  # noqa: F811
    eng, slack = env
    _llm_says_revision(eng)
    rid, r = await _used_sm(eng)
    await _add_guest_draft(eng, rid)
    await eng.on_reply(A1, r["slack_ts"], "評価は5段階です。ご協力お願いします。", "131.1")
    rows = eng.db.all("SELECT extra FROM slack_messages WHERE reservation_id=? AND kind=?", rid, MsgKind.REVIEW_CONFIRM.value)
    assert all(json.loads(x["extra"]).get("stars") != 5 or not json.loads(x["extra"]).get("comment") for x in rows)
    assert confirm_ts(eng)
    assert Orchestrator_parse(eng, "評価4.8の人気スペースです") is None


def Orchestrator_parse(eng, text):
    return eng._parse_stars(text, text)[0]


async def test_ambiguous_guest_text_keeps_approved_review(env):  # noqa: F811
    eng, slack = env
    _llm_says_revision(eng)
    rid, r = await _in_review_confirm(eng)
    await eng.on_reply(A1, r["slack_ts"], "★2 コメント「清掃が不十分な箇所がありました。」", "132.1")
    eng.sm.post_review = _async_raise(NotReady("レビュー", "まだ書けません"))
    await eng.on_reaction(A2, eng.db.kv_get(f"review_confirm_ts:{rid}"), "ok")
    assert eng.res(rid)["state"] == ResState.CONFIRMED
    await _add_guest_draft(eng, rid)
    await eng.on_reply(A1, r["slack_ts"], "★5の評価をいただき、ありがとうございました！またのご利用をお待ちしております。", "132.2")
    c = _review_confirm(eng, rid)
    assert c["stars"] == 2 and c["comment"] == "清掃が不十分な箇所がありました。"


async def test_ambiguous_stars_kept_earlier_are_answered(env):  # noqa: F811
    eng, slack = env
    rid, r = await new_sm(eng)
    await eng.on_reaction(A1, r["slack_ts"], "ok")
    await eng.on_reply(A1, r["slack_ts"], "★2 コメント「清掃が不十分な箇所がありました。」", "133.1")
    await eng.on_reply(A1, r["slack_ts"], "★3でお願いします。退室が少し遅れていたので、ご協力をお願いいたします", "133.2")
    assert any("以前の指示（★2）のまま" in t for t in slack.texts())
    assert json.loads(eng.db.kv_get(f"review_pref:{rid}"))["stars"] == 2


async def test_no_contradictory_notes_in_review_confirm(env):  # noqa: F811
    eng, slack = env
    rid, r = await _in_review_confirm(eng)
    await eng.on_reply(A1, r["slack_ts"], "★3でお願いします。退室が少し遅れていたので、ご協力をお願いいたします", "134.1")
    assert not any("評価が読み取れませんでした" in t for t in slack.texts())


@pytest.mark.parametrize("reply", ["★★★★☆でお願いします", ":star::star::star:", "今回はレビューなしで"])
async def test_instruction_on_closed_posted_booking_is_answered(env, reply):  # noqa: F811
    eng, slack = env
    rid, r = await _used_sm(eng)
    await eng.complete(rid, "exit_record")
    await eng.on_reply(A1, r["slack_ts"], reply, "135.1")
    assert any("対応を終了しています" in t for t in slack.texts())


async def test_failed_confirm_post_records_the_stop(env):  # noqa: F811
    eng, slack = env
    rid, r = await _used_sm(eng)
    orig = eng.post

    async def post(**kw):
        if kw.get("kind") is MsgKind.REVIEW_CONFIRM:
            return None
        return await orig(**kw)
    eng.post = post
    await eng.on_reply(A1, r["slack_ts"], "ちょっと待って", "136.1")
    eng.post = orig
    assert eng.res(rid)["state"] == ResState.CONFIRMED
    await eng.complete(rid, "exit_record")
    assert not _reviews(eng) and eng.res(rid)["state"] == ResState.REVIEW_CONFIRM


async def test_failed_resume_keeps_declined_so_it_can_be_retried(env):  # noqa: F811
    eng, slack = env
    rid, r = await _used_sm(eng)
    await eng.on_reply(A1, r["slack_ts"], "レビューしない", "137.1")
    orig = eng.post

    async def post(**kw):
        if kw.get("kind") is MsgKind.REVIEW_CONFIRM:
            return None
        return await orig(**kw)
    eng.post = post
    await eng.on_reply(A1, r["slack_ts"], "★4", "137.2")
    eng.post = orig
    assert eng.res(rid)["state"] == ResState.CLOSED and eng.db.kv_get(f"review_declined:{rid}")
    await eng.on_reply(A1, r["slack_ts"], "★4", "137.3")
    assert eng.res(rid)["state"] == ResState.REVIEW_CONFIRM and _review_confirm(eng, rid)["stars"] == 4


async def test_slow_failing_confirm_does_not_revert_a_newer_one(env):  # noqa: F811
    eng, slack = env
    rid, r = await _used_sm(eng)
    orig = eng.post
    gate = asyncio.Event()
    first = {"done": False}

    async def post(**kw):
        if kw.get("kind") is MsgKind.REVIEW_CONFIRM and not first["done"]:
            first["done"] = True
            await gate.wait()
            return None  # 最初の確認は遅れて失敗
        return await orig(**kw)
    eng.post = post
    t = asyncio.create_task(eng.on_reply(A1, r["slack_ts"], "ちょっと待って", "138.1"))
    await asyncio.sleep(0.05)
    await eng.on_reply(A1, r["slack_ts"], "★3 コメント「次回は片付けにご協力をお願いします。ありがとうございました。」", "138.2")
    gate.set()
    await t
    eng.post = orig
    assert eng.res(rid)["state"] == ResState.REVIEW_CONFIRM
    assert _review_confirm(eng, rid)["stars"] == 3
    await eng.complete(rid, "exit_record")
    assert not _reviews(eng)


async def test_confirm_interrupted_by_stop_is_reissued_at_startup(env):  # noqa: F811
    eng, slack = env
    rid, r = await _used_sm(eng)
    eng.db.update("reservations", rid, state=ResState.REVIEW_CONFIRM.value)
    eng.db.kv_set(f"review_confirm_ts:{rid}", "-:abc")  # 確認メッセージを出す途中で止まった
    await eng.recover_stuck(0)
    ts = eng.db.kv_get(f"review_confirm_ts:{rid}")
    assert ts and not ts.startswith("-") and any("出し直します" in t for t in slack.texts())


# ---------- 10回目のレビューの指摘 ----------
def _fail_kind(eng, kind):
    orig = eng.post

    async def post(**kw):
        if kw.get("kind") is kind:
            return None
        return await orig(**kw)
    eng.post = post
    return orig


async def test_guest_reply_on_declined_booking_is_not_swallowed(env):  # noqa: F811
    eng, slack = env
    _llm_says_revision(eng)
    rid, r = await _used_sm(eng)
    await eng.on_reply(A1, r["slack_ts"], "レビューはしない", "140.1")
    assert eng.res(rid)["state"] == ResState.CLOSED
    await _add_guest_draft(eng, rid)
    await eng.on_reply(A1, r["slack_ts"], "【修正】評価★5をいただき、ありがとうございました！忘れ物はございませんでした。", "140.2")
    assert confirm_ts(eng)


async def test_old_error_message_cannot_retry_after_newer_confirm(env):  # noqa: F811
    eng, slack = env
    rid, r = await _used_sm(eng)
    eng.sm.post_review = _async_raise(ActionFailed("レビュー", "サイトのエラー"))
    await eng.post_review(rid, 5, None, "system", from_states=(ResState.CONFIRMED,))
    e1 = eng.db.kv_get(f"review_error_ts:{rid}")
    await eng.on_reply(A1, r["slack_ts"], "★2 コメント「清掃が不十分な箇所がありました。」", "141.1")
    orig = _fail_kind(eng, MsgKind.ERROR)  # 次の失敗メッセージは Slack に出せない
    await eng.on_reaction(A2, eng.db.kv_get(f"review_confirm_ts:{rid}"), "ok")
    eng.post = orig
    assert eng.res(rid)["state"] == ResState.REVIEW_ERROR
    del eng.sm.post_review
    await eng.on_reaction(A1, e1, "ok")  # 古い失敗メッセージ（★5）
    assert not _reviews(eng)
    await eng.tick_repost()  # 出せなかった失敗メッセージを出し直す
    e2 = eng.db.kv_get(f"review_error_ts:{rid}")
    assert e2 not in (None, "x", "-", e1)
    await eng.on_reaction(A1, e2, "ok")
    assert _reviews(eng)[-1][2] == 2


async def test_reissued_confirm_keeps_requested_content(env):  # noqa: F811
    eng, slack = env
    rid, r = await _used_sm(eng)
    orig = _fail_kind(eng, MsgKind.REVIEW_CONFIRM)
    await eng.on_reply(A1, r["slack_ts"], "★2 コメント「清掃が不十分な箇所がありました。」", "142.1")
    eng.post = orig
    # 失敗した確認は CONFIRMED に戻り、止めた記録（★2）が残る → 次の利用完了の処理で確認を出し直す
    await eng.complete(rid, "exit_record")
    c = _review_confirm(eng, rid)
    assert c["stars"] == 2 and c["comment"] == "清掃が不十分な箇所がありました。" and not _reviews(eng)


async def test_interrupted_confirm_replacement_is_reissued_with_new_content(env):  # noqa: F811
    eng, slack = env
    rid, r = await _in_review_confirm(eng)
    c1 = eng.db.kv_get(f"review_confirm_ts:{rid}")
    orig = _fail_kind(eng, MsgKind.REVIEW_CONFIRM)
    await eng.on_reply(A1, r["slack_ts"], "★1 コメント「備品が破損していました。ご注意ください。」", "143.1")
    eng.post = orig
    await eng.on_reaction(A2, c1, "ok")
    assert not _reviews(eng)  # 置き換えようとした古い確認（★5）からは投稿しない
    pend = json.loads(eng.db.kv_get(f"review_confirm_pending:{rid}"))
    pend["at"] = iso(now() - timedelta(minutes=20))
    eng.db.kv_set(f"review_confirm_pending:{rid}", json.dumps(pend))
    await eng.tick_repost()
    c = _review_confirm(eng, rid)
    assert c["stars"] == 1 and c["comment"] == "備品が破損していました。ご注意ください。"


async def test_reply_during_classification_stops_auto_post(env):  # noqa: F811
    eng, slack = env
    rid, r = await _used_sm(eng)
    gate = asyncio.Event()
    orig = eng.llm.classify_reply

    async def slow(reply, drafts):
        await gate.wait()
        return await orig(reply, drafts)
    eng.llm.classify_reply = slow
    t = asyncio.create_task(eng.on_reply(A1, r["slack_ts"], "灰皿が割れていた。確認中", "144.1"))
    await asyncio.sleep(0.05)
    await eng.complete(rid, "exit_record")  # 判定を待つ間に利用完了の処理が走る
    gate.set()
    await t
    assert not _reviews(eng) and eng.res(rid)["state"] == ResState.REVIEW_CONFIRM


async def test_catch_up_reads_declined_closed_threads(env):  # noqa: F811
    eng, slack = env
    rid, r = await _used_sm(eng)
    await eng.on_reply(A1, r["slack_ts"], "レビューはしない", "145.1")
    eng.db.run("UPDATE reservations SET updated_at=? WHERE id=?", iso(now() - timedelta(days=5)), rid)
    rows = eng.db.all("SELECT id FROM reservations WHERE slack_ts IS NOT NULL AND (state<>? OR updated_at >= ? OR "
                      "id IN (SELECT CAST(substr(key, 17) AS INTEGER) FROM kv WHERE key LIKE 'review_declined:%' "
                      "AND value IS NOT NULL))", ResState.CLOSED.value, iso(now() - timedelta(days=2)))
    assert rid in [x["id"] for x in rows]


async def test_successful_post_clears_declined(env):  # noqa: F811
    eng, slack = env
    rid, r = await _used_sm(eng)
    await eng.on_reply(A1, r["slack_ts"], "レビューはしない", "146.1")
    await eng.on_reply(A1, r["slack_ts"], "★4", "146.2")
    await eng.on_reaction(A2, eng.db.kv_get(f"review_confirm_ts:{rid}"), "ok")
    assert eng.res(rid)["state"] == ResState.CLOSED and not eng.db.kv_get(f"review_declined:{rid}")


# ---------- 11回目のレビューの指摘 ----------
async def test_reissue_after_crash_uses_pending_not_older_instruction(env):  # noqa: F811
    eng, slack = env
    rid, r = await new_sm(eng)
    await eng.on_reaction(A1, r["slack_ts"], "ok")
    await eng.on_reply(A1, r["slack_ts"], "★2 コメント「清掃が不十分な箇所がありました。」", "150.1")
    await _finish_use(eng)  # ★2 の確認が出る
    orig = _fail_kind(eng, MsgKind.REVIEW_CONFIRM)
    await eng.on_reply(A1, r["slack_ts"], "★4 コメント「きれいに使っていただきありがとうございました。」", "150.2")
    eng.post = orig
    await eng.recover_stuck(0)
    c = _review_confirm(eng, rid)
    assert c["stars"] == 4 and c["comment"] == "きれいに使っていただきありがとうございました。"


async def test_ambiguous_text_keeps_pending_content(env):  # noqa: F811
    eng, slack = env
    _llm_says_revision(eng)
    rid, r = await _in_review_confirm(eng)
    orig = _fail_kind(eng, MsgKind.REVIEW_CONFIRM)
    await eng.on_reply(A1, r["slack_ts"], "★1 コメント「備品が破損していました。ご注意ください。」", "151.1")
    eng.post = orig
    await _add_guest_draft(eng, rid)
    await eng.on_reply(A1, r["slack_ts"], "★5の評価をいただき、ありがとうございました！またのご利用をお待ちしております。", "151.2")
    assert any("今の確認メッセージの内容のまま" in t for t in slack.texts())
    pend = json.loads(eng.db.kv_get(f"review_confirm_pending:{rid}"))
    assert pend["stars"] == 1


async def test_stale_error_repost_does_not_replace_newer_error(env):  # noqa: F811
    eng, slack = env
    rid, r = await _used_sm(eng)
    eng.sm.post_review = _async_raise(ActionFailed("レビュー", "サイトのエラー"))
    orig = _fail_kind(eng, MsgKind.ERROR)
    await eng.post_review(rid, 5, None, "system", from_states=(ResState.CONFIRMED,))
    eng.post = orig
    assert eng.db.kv_get(f"repost_review_error:{rid}")
    await eng.on_reply(A1, r["slack_ts"], "★2 コメント「清掃が不十分な箇所がありました。」", "152.1")
    await eng.on_reaction(A2, eng.db.kv_get(f"review_confirm_ts:{rid}"), "ok")  # また失敗（E2 は出せた）
    e2 = eng.db.kv_get(f"review_error_ts:{rid}")
    await eng.tick_repost()
    assert eng.db.kv_get(f"review_error_ts:{rid}") == e2


async def test_declined_unclear_review_reply_is_answered(env):  # noqa: F811
    eng, slack = env
    rid, r = await _used_sm(eng)
    await eng.on_reply(A1, r["slack_ts"], "レビューはしない", "153.1")
    await eng.on_reply(A1, r["slack_ts"], "★3か★4で", "153.2")
    assert any("レビューを投稿せずに対応を終了しています" in t for t in slack.texts())


async def test_exit_problem_recorded_while_confirming_is_reported(env):  # noqa: F811
    eng, slack = env
    rid, r = await _used_sm(eng)
    eng.sm.details["6271768"].exit_status = None
    await eng.on_reply(A1, r["slack_ts"], "お疲れさまでした", "154.1")
    assert eng.res(rid)["state"] == ResState.REVIEW_CONFIRM
    eng.sm.details["6271768"].exit_status = "気になる点があります"
    await eng.complete(rid, "exit_record")
    assert any("退室時状態が「気になる点があります」と記録されました" in t for t in slack.texts())


async def test_decline_during_resume_post_keeps_declined(env):  # noqa: F811
    eng, slack = env
    rid, r = await _used_sm(eng)
    await eng.on_reply(A1, r["slack_ts"], "レビューはしない", "155.1")
    orig = eng.post
    gate = asyncio.Event()

    async def post(**kw):
        if kw.get("kind") is MsgKind.REVIEW_CONFIRM:
            await gate.wait()
        return await orig(**kw)
    eng.post = post
    t = asyncio.create_task(eng.on_reply(A1, r["slack_ts"], "★4", "155.2"))
    await asyncio.sleep(0.05)
    await eng.on_reply(A1, r["slack_ts"], "やっぱりレビューしない", "155.3")
    gate.set()
    await t
    eng.post = orig
    assert eng.res(rid)["state"] == ResState.CLOSED and eng.db.kv_get(f"review_declined:{rid}")


async def test_exit_warning_survives_failed_confirm_post(env):  # noqa: F811
    eng, slack = env
    rid, r = await _used_sm(eng)
    eng.sm.details["6271768"].exit_status = "気になる点があります"
    orig = _fail_kind(eng, MsgKind.REVIEW_CONFIRM)
    await eng.complete(rid, "exit_record")
    eng.post = orig
    assert eng.res(rid)["state"] == ResState.CONFIRMED
    await eng.complete(rid, "exit_record")
    confirms = [t for t in slack.texts() if "このメッセージにスタンプ" in t]
    assert confirms and "気になる点があります" in confirms[-1]


# ---------- 12回目のレビューの指摘 ----------
async def test_poll_warns_exit_problem_while_confirming(env):  # noqa: F811
    eng, slack = env
    rid, r = await _used_sm(eng)
    d = eng.sm.details["6271768"]
    d.exit_status = None
    await eng.on_reply(A1, r["slack_ts"], "お疲れさまでした", "160.1")
    assert eng.res(rid)["state"] == ResState.REVIEW_CONFIRM
    d.exit_status, d.exit_detail = "気になる点があります", "灰皿が割れていました"
    await eng._warn_exit_while_confirming(rid, eng.res(rid), d)  # 巡回（_handle_sm_row）から、読んだ詳細で呼ばれる
    await eng._warn_exit_while_confirming(rid, eng.res(rid), d)
    assert sum("と記録されました" in t for t in slack.texts()) == 1


async def test_pre_use_instruction_confirm_shows_exit_problem(env):  # noqa: F811
    eng, slack = env
    rid, r = await new_sm(eng)
    await eng.on_reaction(A1, r["slack_ts"], "ok")
    await eng.on_reply(A1, r["slack_ts"], "★5", "161.1")
    d = eng.sm.details["6271768"]
    d.start_at, d.end_at, d.exit_status, d.exit_detail = (now() - timedelta(hours=5), now() - timedelta(hours=2),
                                                         "気になる点があります", "備品が破損")
    eng.db.update("reservations", rid, start_at=iso(d.start_at), end_at=iso(d.end_at))
    await eng.complete(rid, "exit_record")
    confirms = [t for t in slack.texts() if "このメッセージにスタンプ" in t]
    assert confirms and "気になる点があります" in confirms[-1]


async def test_approved_waiting_review_is_rechecked_after_exit_problem(env):  # noqa: F811
    eng, slack = env
    rid, r = await _in_review_confirm(eng)
    eng.sm.post_review = _async_raise(NotReady("レビュー", "まだ書けません"))
    eng.sm.details["6271768"].exit_status = "問題なく退室しました"
    eng.db.kv_set(f"exit_warned:{rid}", "気になる点があります")
    await eng.on_reaction(A2, eng.db.kv_get(f"review_confirm_ts:{rid}"), "ok")
    assert eng.res(rid)["state"] == ResState.CONFIRMED
    del eng.sm.post_review
    eng.sm.details["6271768"].exit_status = "汚れがありました"  # 承認の後に、別の気になる点が記録された
    await eng.complete(rid, "exit_record")
    assert not _reviews(eng) and eng.res(rid)["state"] == ResState.REVIEW_CONFIRM


async def test_exit_head_kept_through_unreadable_reply(env):  # noqa: F811
    eng, slack = env
    rid, r = await _used_sm(eng)
    eng.sm.details["6271768"].exit_status = "気になる点があります"
    orig = _fail_kind(eng, MsgKind.REVIEW_CONFIRM)
    await eng.complete(rid, "exit_record")
    eng.post = orig
    await eng.on_reply(A1, r["slack_ts"], "レビューは？", "162.1")
    confirms = [t for t in slack.texts() if "このメッセージにスタンプ" in t]
    assert confirms and "気になる点があります" in confirms[-1]


async def test_header_not_duplicated_after_repeated_failures(env):  # noqa: F811
    eng, slack = env
    rid, r = await _used_sm(eng)
    eng.sm.details["6271768"].exit_status = "気になる点があります"
    orig = _fail_kind(eng, MsgKind.REVIEW_CONFIRM)
    await eng.complete(rid, "exit_record")
    await eng.on_reply(A1, r["slack_ts"], "お疲れさまでした", "163.1")
    await eng.on_reply(A1, r["slack_ts"], "ありがとう", "163.2")
    eng.post = orig
    await eng.on_reply(A1, r["slack_ts"], "了解", "163.3")
    confirms = [t for t in slack.texts() if "このメッセージにスタンプ" in t]
    assert confirms and confirms[-1].count("気になる点があります") == 1


async def test_lost_unknown_notice_is_reposted(env):  # noqa: F811
    eng, slack = env
    rid, r = await _used_sm(eng)

    async def submitted_then_failed(pid, stars, comment, dry_run):
        review_submitted(True)
        raise ActionFailed("レビュー", "通信が失敗しました")
    eng.sm.post_review = submitted_then_failed
    orig = _fail_kind(eng, MsgKind.INFO)
    await eng.post_review(rid, 5, None, "system", from_states=(ResState.CONFIRMED,))
    eng.post = orig
    assert not any("投稿されたか分かりません" in t for t in slack.texts())
    await eng.tick_repost()
    assert any("投稿されたか分かりません" in t for t in slack.texts())


# ---------- 13回目のレビューの指摘 ----------
async def test_exit_warning_retried_after_failed_post(env):  # noqa: F811
    eng, slack = env
    rid, r = await _used_sm(eng)
    d = eng.sm.details["6271768"]
    d.exit_status = None
    await eng.on_reply(A1, r["slack_ts"], "お疲れさまでした", "170.1")
    d.exit_status = "気になる点があります"
    orig = _fail_kind(eng, None)  # 通常の投稿（kind なし）を失敗させる
    await eng._warn_exit_while_confirming(rid, eng.res(rid), d)
    eng.post = orig
    assert not eng.db.kv_get(f"exit_warned:{rid}")
    await eng._warn_exit_while_confirming(rid, eng.res(rid), d)
    assert any("と記録されました" in t for t in slack.texts())


async def test_exit_problem_warned_in_review_error(env):  # noqa: F811
    eng, slack = env
    rid, r = await _used_sm(eng)
    d = eng.sm.details["6271768"]
    eng.sm.post_review = _async_raise(ActionFailed("レビュー", "サイトのエラー"))
    await eng.post_review(rid, 5, None, "system", from_states=(ResState.CONFIRMED,))
    assert eng.res(rid)["state"] == ResState.REVIEW_ERROR
    d.exit_status = "気になる点があります"
    await eng._warn_exit_while_confirming(rid, eng.res(rid), d)
    assert any("と記録されました" in t for t in slack.texts())


async def test_confirm_showing_exit_status_marks_it_warned(env):  # noqa: F811
    eng, slack = env
    rid, r = await _used_sm(eng)
    eng.sm.details["6271768"].exit_status = "気になる点があります"
    orig = _fail_kind(eng, MsgKind.REVIEW_CONFIRM)
    await eng.complete(rid, "exit_record")
    eng.post = orig
    await eng.on_reply(A1, r["slack_ts"], "★4", "171.1")
    assert eng.db.kv_get(f"exit_warned:{rid}") == "気になる点があります"
    n = len(slack.texts())
    await eng._warn_exit_while_confirming(rid, eng.res(rid), eng.sm.details["6271768"])
    assert len(slack.texts()) == n


# ---------- 14回目のレビューの指摘 ----------
async def test_no_exit_warning_when_post_status_unknown(env):  # noqa: F811
    eng, slack = env
    rid, r = await _used_sm(eng)

    async def submitted_then_failed(pid, stars, comment, dry_run):
        review_submitted(True)
        raise ActionFailed("レビュー", "通信が失敗しました")
    eng.sm.post_review = submitted_then_failed
    await eng.post_review(rid, 5, None, "system", from_states=(ResState.CONFIRMED,))
    d = eng.sm.details["6271768"]
    d.exit_status = "気になる点があります"
    await eng._warn_exit_while_confirming(rid, eng.res(rid), d)
    assert not any("と記録されました" in t for t in slack.texts())


async def test_failed_exit_warning_is_retried_by_tick_repost(env):  # noqa: F811
    eng, slack = env
    rid, r = await _used_sm(eng)
    d = eng.sm.details["6271768"]
    d.exit_status = None
    await eng.on_reply(A1, r["slack_ts"], "お疲れさまでした", "180.1")
    d.exit_status = "気になる点があります"
    orig = _fail_kind(eng, None)
    await eng._warn_exit_while_confirming(rid, eng.res(rid), d)
    eng.post = orig
    assert eng.db.kv_get(f"exit_warn_pending:{rid}")
    await eng.tick_repost()  # 巡回で行が読み直されなくても、定期処理で出し直す
    assert any("と記録されました" in t for t in slack.texts()) and not eng.db.kv_get(f"exit_warn_pending:{rid}")


# ---------- 巡回・停止の復旧の知らせ（2026-10-18） ----------
async def test_poll_recovery_is_announced_once(env):  # noqa: F811
    eng, slack = env
    eng.db.run("INSERT OR REPLACE INTO alerts (key, last_sent_at) VALUES (?, ?)", "poll_stalled:ib", iso(now()))
    await eng._notify_poll_recovered(Platform.IB)
    await eng._notify_poll_recovered(Platform.IB)
    assert sum("巡回が復旧しました" in t for t in slack.texts()) == 1
    assert eng.db.one("SELECT 1 FROM alerts WHERE key='poll_stalled:ib'") is None
    await eng._notify_poll_recovered(Platform.SM)  # 警告を出していなければ何も言わない
    assert sum("巡回が復旧しました" in t for t in slack.texts()) == 1


async def test_deadman_refresh_announces_resume_after_fired_warning(env):  # noqa: F811
    from barnull.app import deadman_refresh
    eng, slack = env
    posted = []

    async def schedule(**kw):
        return {"scheduled_message_id": f"Q{len(posted)}"}

    async def delete(**kw):
        return {}

    async def post_msg(**kw):
        posted.append(kw["text"])
        return {"ts": "1.1"}
    eng.slack.chat_scheduleMessage = schedule
    eng.slack.chat_deleteScheduledMessage = delete
    eng.slack.chat_postMessage = post_msg
    await deadman_refresh(eng, 30)
    assert not posted  # 予約しただけ
    eng.db.kv_set("deadman_at", str(int((now() - timedelta(minutes=1)).timestamp())))  # 予約した警告が投稿された（止まっていた）
    await deadman_refresh(eng, 30)
    assert len(posted) == 1 and "再開しています" in posted[0]
    await deadman_refresh(eng, 30)
    assert len(posted) == 1
