"""ワークフローのテスト（Slack・管理画面・Claude は偽物に置き換える）。依頼書 §10 のシナリオを画面なしで確認する。"""

from __future__ import annotations

import asyncio
import dataclasses
from datetime import timedelta

import pytest

from barnull import secrets
from barnull.config import load_config
from barnull.db import DB
from barnull.llm.client import Classification
from barnull.models import Detail, InqState, Kind, Message, MsgKind, Platform, ResState
from barnull.platforms.base import ActionResult
from barnull.platforms.ib import Instabase
from barnull.platforms.sm import SpaceMarket
from barnull.textutil import iso, now
from barnull.workflows.handlers import Orchestrator

A1, A2, STRANGER, BOT = "U0A51TDCCEN", "U08EHTUTLE8", "UOTHER", "UBOT"
CODE = "4321"


class FakeSlack:
    def __init__(self):
        self.posts: list[dict] = []
        self.updates: list[dict] = []
        self._n = 1000

    async def chat_postMessage(self, **kw):
        self._n += 1
        kw["ts"] = f"{self._n}.0001"
        self.posts.append(kw)
        return {"ts": kw["ts"]}

    async def chat_update(self, **kw):
        self.updates.append(kw)
        return {"ok": True}

    async def files_upload_v2(self, **kw):
        return {"ok": True}

    async def reactions_get(self, **kw):
        names = getattr(self, "pending_reactions", {}).get(kw["timestamp"], [])
        return {"message": {"reactions": [{"name": n, "users": u} for n, u in names]}}

    async def conversations_replies(self, **kw):
        return {"messages": [{"ts": kw["ts"]}, *getattr(self, "pending_replies", {}).get(kw["ts"], [])]}

    async def reactions_add(self, **kw):
        self.reactions.append((kw["timestamp"], kw["name"]))
        return {"ok": True}

    @property
    def reactions(self) -> list[tuple[str, str]]:
        if not hasattr(self, "_reactions"):
            self._reactions = []
        return self._reactions

    def texts(self) -> list[str]:
        return [p.get("text") or "" for p in self.posts]


class FakeLLM:
    def __init__(self):
        self.drafts = 0

    async def classify_reply(self, reply, open_drafts):
        if "待って" in reply or "保留" in reply:
            return Classification("HOLD", 0.95, None)
        if "否認" in reply:
            return Classification("CANCEL_OR_DENY", 0.95, None)
        if reply.startswith("【修正】"):
            return Classification("REVISION", 0.95, None)
        if reply.startswith("【曖昧】"):
            return Classification("REVISION", 0.4, None)
        return Classification("OTHER", 0.9, None)

    async def has_inquiry(self, texts):
        return any("？" in t for t in texts), "test"

    async def draft_reply(self, **kw):
        self.drafts += 1
        if "要確認" in kw["guest_text"]:
            return "お問い合わせありがとうございます！\n料金は【要確認: 延長料金】です。\nよろしくお願いいたします！"
        return "お問い合わせありがとうございます！\n可能です。\nよろしくお願いいたします！"

    async def thanks_comment(self, purpose):
        return "この度はBar nullをご利用いただき、誠にありがとうございました！"


def sm_detail(pid="6271768", **kw) -> Detail:
    d = Detail(platform=Platform.SM, platform_id=pid, status="保留中", guest_name="山田 太郎",
               start_at=now() + timedelta(days=3), end_at=now() + timedelta(days=3, hours=3),
               guest_total=33000, host_revenue=23100, people="〜15人", purpose="パーティー", can_approve=True,
               messages=[Message("guest", "[利用目的] パーティー", index=0)])
    return dataclasses.replace(d, **kw)


class FakeSM(SpaceMarket):
    def __init__(self, cfg):
        super().__init__(cfg, None)
        self.details: dict[str, Detail] = {}
        self.calls: list[tuple] = []
        self.delay = 0.0

    async def get_detail(self, pid):
        return self.details[pid]

    async def approve_and_send_template(self, pid, text, pdf, marker, dry_run, code=None):
        self.calls.append(("template", pid, text))
        await asyncio.sleep(self.delay)
        d = self.details[pid]
        d.messages += [Message("host", text, index=len(d.messages)),
                       Message("host", "", files=[self.cfg.manual_pdf.name], index=len(d.messages) + 1)]
        d.status, d.can_approve = "予約完了", False
        return ActionResult(ok=True)

    async def send_message(self, pid, text, dry_run, already_sent_check=None, guard=None):
        if guard is not None and pid in self.details:
            guard(self.details[pid].messages)
        self.calls.append(("message", pid, text))
        return ActionResult(ok=True)

    async def post_review(self, pid, stars, comment, dry_run):
        self.calls.append(("review", pid, stars, comment))
        return ActionResult(ok=True)


class FakeIB(Instabase):
    def __init__(self, cfg):
        super().__init__(cfg, None)
        self.details: dict[str, Detail] = {}
        self.calls: list[tuple] = []

    async def get_detail(self, pid, with_thread=True):
        return self.details[pid]

    async def send_template(self, pid, text, pdf, marker, dry_run, code=None):
        self.calls.append(("template", pid, text))
        return ActionResult(ok=True), "4618365"

    async def get_thread(self, tid):
        for d in self.details.values():
            if d.thread_id == tid:
                return d.messages, d.guest_id, [d.platform_id]
        return [], None, []

    async def send_message(self, tid, text, dry_run, already_sent_check=None, guard=None):
        if guard is not None:
            msgs = next((d.messages for d in self.details.values() if d.thread_id == tid), [])
            guard(msgs)
        self.calls.append(("message", tid, text))
        return ActionResult(ok=True)

    async def post_review(self, pid, stars, comment, dry_run):
        self.calls.append(("review", pid, stars, comment))
        return ActionResult(ok=True)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("BARNULL_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BARNULL_DRY_RUN", "1")  # テストは config.toml の設定にかかわらず DRY_RUN
    store = {"KEYBOX_CODE": CODE}
    monkeypatch.setattr(secrets, "get", lambda n: store[n])
    monkeypatch.setattr(secrets, "get_optional", lambda n: store.get(n))
    monkeypatch.setattr("barnull.workflows.engine.ROOT", tmp_path)
    cfg = load_config()
    db = DB(cfg.db_path)
    slack = FakeSlack()
    eng = Orchestrator(cfg, db, slack, pool=None, llm=FakeLLM())
    eng.bot_user_id = BOT
    eng.sm, eng.ib = FakeSM(cfg), FakeIB(cfg)
    return eng, slack


def confirm_ts(eng) -> str | None:
    """最後に出した「この文面で送ってよいか」の確認メッセージ"""
    row = eng.db.one("SELECT ts FROM slack_messages WHERE kind=? ORDER BY created_at DESC, ts DESC",
                     MsgKind.REVISION_CONFIRM.value)
    return row["ts"] if row else None


async def new_sm(eng, **kw):
    d = sm_detail(**kw)
    eng.sm.details[d.platform_id] = d
    rid = await eng.on_new(Platform.SM, d.platform_id)
    return rid, eng.res(rid)


# ---------- シナリオ 1: SM 予約 → スタンプ → 承認＋定型文＋PDF ----------
async def test_sm_new_and_stamp_sends_template_once(env):
    eng, slack = env
    rid, r = await new_sm(eng)
    assert r["state"] == ResState.NOTIFIED and r["slack_ts"]
    parent = slack.posts[0]
    assert "スペースマーケット｜新規予約リクエスト" in parent["text"]
    body = str(parent["attachments"])
    assert "（初回）" in body and "¥33,000" in body and "¥23,100" in body
    await eng.on_reaction(A1, r["slack_ts"], "ok")
    assert [c[0] for c in eng.sm.calls] == ["template"]
    sent = eng.sm.calls[0][2]
    assert sent.startswith("山田さま\n\nご予約ありがとうございます！") and f"【{CODE}】" in sent
    assert eng.res(rid)["state"] == ResState.CONFIRMED
    # 二重スタンプ（別の承認者）でも再送しない
    await eng.on_reaction(A2, r["slack_ts"], "+1")
    assert len(eng.sm.calls) == 1
    # 暗証番号は Slack に出ない
    assert all(CODE not in t for t in slack.texts())


async def test_concurrent_stamps_execute_once(env):
    eng, _ = env
    rid, r = await new_sm(eng)
    eng.sm.delay = 0.05
    await asyncio.gather(eng.on_reaction(A1, r["slack_ts"], "ok"), eng.on_reaction(A2, r["slack_ts"], "+1"))
    assert len(eng.sm.calls) == 1


async def test_non_approver_and_bot_are_ignored(env):
    eng, _ = env
    rid, r = await new_sm(eng)
    await eng.on_reaction(STRANGER, r["slack_ts"], "ok")
    await eng.on_reaction(BOT, r["slack_ts"], "ok")
    await eng.on_reply(STRANGER, r["slack_ts"], "待って", "1.1")
    assert eng.sm.calls == [] and eng.res(rid)["state"] == ResState.NOTIFIED


# ---------- シナリオ 2: IB 予約 → スタンプ → 送信 ----------
async def test_ib_new_and_stamp(env):
    eng, slack = env
    d = Detail(platform=Platform.IB, platform_id="8797784002", status="予約確定", guest_name="佐藤 花子",
               guest_id="99", start_at=now() + timedelta(days=5), end_at=now() + timedelta(days=5, hours=5),
               cleanup_end_at=now() + timedelta(days=5, hours=5, minutes=30), guest_total=18150, host_revenue=None)
    eng.ib.details[d.platform_id] = d
    rid = await eng.on_new(Platform.IB, d.platform_id)
    r = eng.res(rid)
    assert "インスタベース｜新規予約（確定済み）" in slack.posts[0]["text"]
    assert r["host_revenue"] == round(18150 * 0.65) and r["revenue_is_estimate"] == 1
    assert "（概算）" in str(slack.posts[0]["attachments"])
    await eng.on_reaction(A2, r["slack_ts"], "ok")
    assert eng.ib.calls[0][0] == "template" and eng.ib.calls[0][2].startswith("佐藤さま")
    assert eng.res(rid)["thread_id"] == "4618365"


# ---------- シナリオ 3: 保留 → リマインド → リマインドにスタンプ ----------
async def test_hold_reminder_then_stamp(env):
    eng, slack = env
    rid, r = await new_sm(eng)
    await eng.on_reply(A1, r["slack_ts"], "ちょっと待って", "1.2")
    r = eng.res(rid)
    assert r["state"] == ResState.ON_HOLD and r["hold_since"]
    eng.db.update("reservations", rid, next_remind_at=iso(now() - timedelta(minutes=1)))
    await eng.tick_reminders()
    rem = [p for p in slack.posts if "リマインド" in (p.get("text") or "")]
    assert rem and "保留" in rem[-1]["text"]
    rem_ts = rem[-1]["ts"]
    assert eng.db.slack_msg(rem_ts)["kind"] == MsgKind.REMINDER
    await eng.on_reaction(A2, rem_ts, "ok")
    assert eng.res(rid)["state"] == ResState.CONFIRMED and len(eng.sm.calls) == 1


# ---------- シナリオ 4: 保留中に人が手動で送信 → 検知してリマインド停止 ----------
async def test_manual_send_detected(env):
    eng, slack = env
    rid, r = await new_sm(eng)
    await eng.on_reply(A1, r["slack_ts"], "待って", "1.3")
    d = eng.sm.details["6271768"]
    d.messages += [Message("host", f"ご予約ありがとうございます！キーボックスの暗証番号は【{CODE}】です", index=1),
                   Message("host", "", files=["x.pdf"], index=2)]
    d.status, d.can_approve = "予約完了", False  # 管理画面で承認済み
    eng.db.update("reservations", rid, next_remind_at=iso(now() - timedelta(minutes=1)))
    await eng.tick_reminders()
    assert eng.res(rid)["state"] == ResState.CONFIRMED
    assert eng.sm.calls == []
    assert any("手動対応" in t for t in slack.texts())


# ---------- シナリオ 5: 問い合わせ付き予約 → 文案 → 修正文案で送信 ----------
async def test_inquiry_with_reservation_revision_sent_verbatim_after_template(env):
    eng, slack = env
    rid, r = await new_sm(eng, messages=[Message("guest", "[利用目的] 誕生日会\nケーキを持ち込めますか？", index=0)])
    drafts = eng.open_drafts(rid)
    assert len(drafts) == 1 and drafts[0]["draft_ts"]
    revised = "【修正】お問い合わせありがとうございます！ケーキの持ち込みは可能です。よろしくお願いいたします！"
    await eng.on_reply(A1, r["slack_ts"], revised, "1.4")
    # 修正文はすぐには送らず、確認のメッセージを出す
    assert eng.db.inquiry(drafts[0]["id"])["state"] == InqState.DRAFT_POSTED
    await eng.on_reaction(A2, confirm_ts(eng), "ok")
    # 定型文の前なので承認だけ受け付ける
    assert eng.db.inquiry(drafts[0]["id"])["state"] == InqState.APPROVED
    assert eng.sm.calls == []
    await eng.on_reaction(A1, r["slack_ts"], "ok")
    assert [c[0] for c in eng.sm.calls] == ["template", "message"]
    assert eng.sm.calls[1][2] == revised  # 一字一句そのまま
    assert any("以下の文面で送信しました" in t for t in slack.texts())


async def test_ambiguous_revision_asks_confirmation(env):
    eng, slack = env
    rid, r = await new_sm(eng)
    await eng.on_reaction(A1, r["slack_ts"], "ok")
    eng.sm.details["6271768"].messages.append(Message("guest", "駐車場はありますか？", index=9))
    await eng.process_guest_messages(rid, eng.sm.details["6271768"].messages, "6271768")
    await eng.on_reply(A1, r["slack_ts"], "【曖昧】駐車場は近くのコインパーキングをご利用ください", "1.5")
    assert confirm_ts(eng)
    assert [c[0] for c in eng.sm.calls] == ["template"]
    await eng.on_reaction(A2, confirm_ts(eng), "ok")
    assert eng.sm.calls[-1][0] == "message" and "コインパーキング" in eng.sm.calls[-1][2]


# ---------- シナリオ 6: 予約後のお礼メッセージ → 文案 → スタンプで送信 ----------
async def test_post_booking_thanks_message_draft_and_send(env):
    eng, slack = env
    rid, r = await new_sm(eng)
    await eng.on_reaction(A1, r["slack_ts"], "ok")
    msgs = eng.sm.details["6271768"].messages
    msgs.append(Message("guest", "ありがとうございました！", index=len(msgs)))
    await eng.process_guest_messages(rid, msgs, "6271768")
    drafts = eng.open_drafts(rid)
    assert len(drafts) == 1  # 質問でなくても必ず文案を作る
    await eng.on_reaction(A2, drafts[0]["draft_ts"], "ok")
    assert eng.sm.calls[-1] == ("message", "6271768", drafts[0]["draft_text"])
    # 同じ発言で2回目の文案を作らない
    await eng.process_guest_messages(rid, msgs, "6271768")
    assert eng.llm.drafts == 1


async def test_needs_check_draft_is_not_sent_by_stamp(env):
    eng, slack = env
    rid, r = await new_sm(eng)
    await eng.on_reaction(A1, r["slack_ts"], "ok")
    msgs = eng.sm.details["6271768"].messages
    msgs.append(Message("guest", "延長はできますか？要確認", index=len(msgs)))
    await eng.process_guest_messages(rid, msgs, "6271768")
    d = eng.open_drafts(rid)[0]
    assert d["needs_check"] == 1
    # 通知: 赤帯・見出し・確認項目・承認者へのメンション
    post = next(p for p in slack.posts if p["ts"] == d["draft_ts"])
    att = post["attachments"][0]
    assert att["color"] == "#E01E5A"
    assert att["blocks"][0]["type"] == "header" and "要確認あり" in att["blocks"][0]["text"]["text"]
    assert "延長料金" in att["blocks"][1]["text"]["text"]
    assert post["text"].startswith(f"<@{A1}>") and "【要確認】" in post["text"]
    # 親メッセージのステータスにも要確認
    assert "要確認 1件" in str(slack.updates[-1]["attachments"])
    # 誤ってスタンプ → 送信しない・🚫・理由を返す
    await eng.on_reaction(A1, d["draft_ts"], "ok")
    assert [c[0] for c in eng.sm.calls] == ["template"]
    assert (d["draft_ts"], "no_entry_sign") in slack.reactions
    refused = [t for t in slack.texts() if "スタンプでは送信しません（誤送信防止）" in t]
    assert refused and f"<@{A1}>" in refused[-1] and "延長料金" in refused[-1]
    assert eng.db.inquiry(d["id"])["state"] == InqState.DRAFT_POSTED


async def test_followup_message_supersedes_unsent_draft(env):
    eng, slack = env
    rid, r = await new_sm(eng)
    await eng.on_reaction(A1, r["slack_ts"], "ok")
    msgs = eng.sm.details["6271768"].messages
    msgs.append(Message("guest", "駐車場はありますか？", index=len(msgs)))
    await eng.process_guest_messages(rid, msgs, "6271768")
    first = eng.open_drafts(rid)[0]
    msgs.append(Message("guest", "追加でもう一つ、Wi-Fiはありますか？", index=len(msgs)))
    await eng.process_guest_messages(rid, msgs, "6271768")
    drafts = eng.open_drafts(rid)
    assert len(drafts) == 1 and drafts[0]["id"] != first["id"]
    assert "駐車場" in drafts[0]["guest_text"] and "Wi-Fi" in drafts[0]["guest_text"]
    assert eng.db.inquiry(first["id"])["state"] == InqState.SUPERSEDED
    assert (first["draft_ts"], "arrows_counterclockwise") in slack.reactions


# ---------- シナリオ 7: 利用完了 → ★5 レビュー → クローズ ----------
async def test_completion_posts_review_and_closes(env):
    eng, slack = env
    rid, r = await new_sm(eng)
    await eng.on_reaction(A1, r["slack_ts"], "ok")
    d = eng.sm.details["6271768"]
    d.exit_status = "問題なく退室しました"
    eng.db.update("reservations", rid, end_at=iso(now() - timedelta(hours=2)))
    await eng.tick_completion()
    assert eng.sm.calls[-1][0] == "review" and eng.sm.calls[-1][2] == 5
    assert eng.res(rid)["state"] == ResState.CLOSED
    assert any("🏁 利用完了" in t and "★5" in t for t in slack.texts())


async def test_problem_exit_waits_for_confirmation(env):
    eng, slack = env
    rid, r = await new_sm(eng)
    await eng.on_reaction(A1, r["slack_ts"], "ok")
    eng.sm.details["6271768"].exit_status = "気になる点があります"
    eng.db.update("reservations", rid, end_at=iso(now() - timedelta(hours=2)))
    await eng.tick_completion()
    assert eng.res(rid)["state"] == ResState.REVIEW_CONFIRM
    assert all(c[0] != "review" for c in eng.sm.calls)
    await eng.on_reply(A2, r["slack_ts"], "★4 コメント「ご利用ありがとうございました。」", "1.6")
    assert all(c[0] != "review" for c in eng.sm.calls)  # 評価の指示は確認を経てから投稿する
    confirm = eng.db.one("SELECT ts FROM slack_messages WHERE kind=? ORDER BY ts DESC", MsgKind.REVIEW_CONFIRM.value)["ts"]
    await eng.on_reaction(A1, confirm, "ok")
    assert eng.sm.calls[-1] == ("review", "6271768", 4, "ご利用ありがとうございました。")
    assert eng.res(rid)["state"] == ResState.CLOSED


# ---------- シナリオ 8: キャンセル・否認指示 ----------
async def test_cancel_detected_on_reminder(env):
    eng, slack = env
    rid, r = await new_sm(eng)
    eng.sm.details["6271768"].status = "キャンセル"
    eng.db.update("reservations", rid, next_remind_at=iso(now() - timedelta(minutes=1)))
    await eng.tick_reminders()
    assert eng.res(rid)["state"] == ResState.CLOSED
    assert any("キャンセル" in t for t in slack.texts())


async def test_deny_instruction_is_not_executed(env):
    eng, slack = env
    rid, r = await new_sm(eng)
    await eng.on_reply(A1, r["slack_ts"], "否認して", "1.7")
    assert eng.sm.calls == [] and eng.res(rid)["state"] == ResState.ON_HOLD
    assert any("自動では実行しません" in t for t in slack.texts())


async def test_repeat_count(env):
    eng, _ = env
    eng.db.run("INSERT INTO guest_history VALUES ('sm', 'old1', 'sm:name:山田太郎', ?, 0)", iso(now() - timedelta(days=30)))
    eng.db.run("INSERT INTO guest_history VALUES ('sm', 'old2', 'sm:name:山田太郎', ?, 1)", iso(now() - timedelta(days=20)))
    rid, r = await new_sm(eng)
    assert eng.repeat_label(r) == "2回目"


async def test_inquiry_only_sm(env):
    eng, slack = env
    d = sm_detail(pid="6167621", kind=Kind.INQUIRY_ONLY, start_at=None, end_at=None, status="お問い合わせ",
                  can_approve=False, messages=[Message("guest", "20名で使えますか？", index=0)])
    eng.sm.details[d.platform_id] = d
    rid = await eng.on_new(Platform.SM, d.platform_id)
    assert "【問い合わせ】" in slack.posts[0]["text"]
    drafts = eng.open_drafts(rid)
    assert len(drafts) == 1
    await eng.on_reaction(A1, drafts[0]["draft_ts"], "ok")
    assert eng.sm.calls == [("message", "6167621", drafts[0]["draft_text"])]


async def test_catch_up_processes_missed_stamp_and_reply_once(env):
    eng, slack = env
    rid, r = await new_sm(eng)
    rid2, r2 = await new_sm(eng, platform_id="6271769", guest_name="佐藤 花子")
    # 止まっている間にスタンプと返信があった（Socket Mode では届いていない）
    slack.pending_reactions = {r["slack_ts"]: [("ok", [A1]), ("eyes", [STRANGER])]}
    slack.pending_replies = {r2["slack_ts"]: [{"ts": "2000.1", "user": A2, "text": "ちょっと待って"}]}
    n = await eng.catch_up()
    assert n == 2
    assert [c[0] for c in eng.sm.calls] == ["template"]
    assert eng.res(rid)["state"] == ResState.CONFIRMED
    assert eng.res(rid2)["state"] == ResState.ON_HOLD
    # 2回目は何もしない（処理済み）
    assert await eng.catch_up() == 0
    assert len(eng.sm.calls) == 1


async def test_fenced_revision_is_sent_without_backticks_and_low_confidence_other_asks(env):
    eng, slack = env
    rid, r = await new_sm(eng)
    await eng.on_reaction(A1, r["slack_ts"], "ok")
    msgs = eng.sm.details["6271768"].messages
    msgs.append(Message("guest", "駐車場はありますか？", index=len(msgs)))
    await eng.process_guest_messages(rid, msgs, "6271768")
    # コードブロックで書いた修正文（LLM は REVISION と判定）→ 記号を外して送る
    await eng.on_reply(A1, r["slack_ts"], "```【修正】近くのコインパーキングをご利用ください。```", "1.8")
    await eng.on_reaction(A2, confirm_ts(eng), "ok")
    assert eng.sm.calls[-1] == ("message", "6271768", "【修正】近くのコインパーキングをご利用ください。")
    # 新しい問い合わせ → 判定が「雑談」でもコードブロックなら確認を出す
    msgs.append(Message("guest", "Wi-Fiはありますか？", index=len(msgs)))
    await eng.process_guest_messages(rid, msgs, "6271768")
    n = len(eng.sm.calls)
    await eng.on_reply(A1, r["slack_ts"], "```Wi-Fiはございます。```", "1.9")
    assert len(eng.sm.calls) == n
    assert confirm_ts(eng)


async def test_past_reservation_found_by_poll_is_not_announced(env):
    eng, slack = env
    d = sm_detail(pid="5000001", start_at=now() - timedelta(days=90), end_at=now() - timedelta(days=90, hours=-3))
    eng.sm.details[d.platform_id] = d
    rid = await eng.on_new(Platform.SM, d.platform_id)
    assert eng.res(rid)["state"] == ResState.CLOSED
    assert slack.posts == []
