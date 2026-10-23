"""Slack のスタンプ・返信、リマインド、利用完了、ポーリング（Engine の続き）。"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import sqlite3
import unicodedata
import uuid
from datetime import timedelta

from ..llm.client import Classification
from ..models import (
    ACTIVE_RES_STATES,
    OPEN_INQ_STATES,
    UNSENT_INQ_STATES,
    InqState,
    Kind,
    ListingRow,
    MsgKind,
    Platform,
    ResState,
)
from ..platforms.base import IB_SEND_OK, REVIEW_SUBMIT_HOOK, SM_SEND_OK, ActionFailed, NotReady
from ..platforms.browser import LoginRequired
from ..slack import blocks as B
from ..textutil import from_iso, iso, now, slack_to_plain
from .engine import DEFAULT_THANKS, REVIEW_TIMEOUT_SEC, SM_REVIEW_MAX, SM_REVIEW_MIN, Engine

log = logging.getLogger(__name__)

HOLD_WORDS = re.compile(r"(待って|まって|保留|確認します|確認する|ストップ|止めて|とめて|ちょっと待|一旦)")
CANCEL_WORDS = re.compile(r"(否認|キャンセル|断って|お断り|拒否)")
NO_REVIEW_WORDS = re.compile(r"レビュー.{0,8}?(しない|しなく|不要|いらない|要らない|なし|無し|やめ|止め|書かな|投稿しな|控え|不可|NG)")
# 「まだ」「一旦」「後で」など、今は投稿しないだけの言い方（投稿しないと決めたのではない）→ 保留として扱う
REVIEW_LATER = re.compile(r"まだ|一旦|いったん|今は|いまは|今のところ|後で|あとで|後ほど|のちほど|しばらく|待って|まって|待ち|保留|"
                          r"ストップ|止め|とめて|とめと")
# コメントだけの指示（「コメントは「…」で」）
COMMENT_LEAD = r"コメント\s*(?:は|を)?\s*[:：、,，]?\s*[「『]"
# レビュー・評価・星に触れた返信（Slack の :star: などの絵文字コードも含む）
REVIEW_RELATED = re.compile(r"レビュー|評価|[★☆]|星\s*(?:は|を|の数は?)?\s*[1-5]|[1-5]\s*つ星|星の数|:star|:glowing_star|:dizzy|"
                            + COMMENT_LEAD)
# 返信の、レビュー以外の文で行う保留・否認（命令の形だけ。「キャンセル料は…」「確認します」では行わない）
REST_HOLD = re.compile(r"(?:待って|まって|止めて|とめて)(?=ください|ほしい|欲しい|おいて|[。！!、,\s]|$)|保留(して|で|に)|ストップ")
REST_DENY = re.compile(r"否認(して|で|する|お願い)|お断り(して|で|する)|断って|拒否(して|で|する)|キャンセル(して|で|する|お願い)")
# ゲスト宛ての文面らしさ（敬語）。文末だけでなく文中も見る（「…お気遣いなく。」「…くださいませ♪」、署名つきなど）
POLITE = re.compile(r"です|ます|ません|ください|ございま|いたしま|おりま|ませ|ご了承|お待ち")
# 指示の文の区切り（「、」では切らない。「レビューは★5で、一旦止めておいて」は1つの指示）
SENTENCE_SPLIT = re.compile(r"[。.!！?？\n]+")
DISMISS_WORDS = re.compile(r"^\s*(送らない|送らなくて(いい|良い|大丈夫)(です)?|返信(は)?不要(です)?|対応(は)?不要(です)?|"
                           r"不要(です)?|スキップ)\s*[。！!]?\s*$")
# レビューの評価は「★3」「星3」「3つ星」の形だけを読む（メモの「1つ」「2点」を評価と取り違えない）
STARS = re.compile(r"[★☆]\s*([1-5])(?![0-9.])|星\s*(?:は|を|の数は?)?\s*([1-5])(?![0-9.]|段|点)|([1-5])\s*つ星"
                   r"|評価\s*(?:は|を)?\s*([1-5])(?![0-9.]|段|点)")
# 「レビューは止めなくていい」「コメントなし」のような、投稿しない指示ではない言い方
NOT_DECLINE = re.compile(r"(止め|やめ)な(い|く)|コメント\s*(なし|無し|不要)")
# はっきりしたレビューの指示の書き出し（短い返信でこう始まるものだけを指示として扱う）
REVIEW_LEAD = re.compile(r"^\s*(★|☆|星\s*(?:は|を|の数は?)?\s*[1-5]|[1-5]\s*つ星|レビュー|評価|" + COMMENT_LEAD + ")")
# 「★★★」「★★★☆☆」の形の評価（★1つだけは飾りのことがあるので読まない）
STAR_RUN = re.compile(r"^\s*(★{1,5})(☆{0,4})(?=\s*(?:$|[。！!、,]|で|に|コメント|[「『]))")
STAR_EMOJI = re.compile(r":(?:star|star2|glowing_star):")
INSTRUCTION_MAX = 60
GUEST_TEXT_MIN = 20  # 敬語でこれより長い返信は、ゲストに送る文面とみなす（★N の指示のコメント「…」は数えない）
# コメントの書き出し（「コメント」か評価の直後）。この後の「…」をコメントとして読む
COMMENT_AT = re.compile(r"(?:コメント\s*(?:は|を)?\s*[:：、,，]?|[★☆]\s*[1-5１-５]|星\s*[1-5１-５]|[1-5１-５]\s*つ星|"
                        r"評価\s*(?:は|を)?\s*[1-5１-５]|^\s*★{2,5}☆{0,3})\s*(?:で|、|,|，|:|：)?\s*(?=[「『])")


def balanced_quote(text: str, start: int) -> tuple[str | None, int]:
    """text[start] の「/『から、対応する閉じ括弧までの中身と、その次の位置（入れ子の「」も可）"""
    pairs = {"「": "」", "『": "』"}
    stack: list[str] = []
    for i in range(start, len(text)):
        ch = text[i]
        if ch in pairs:
            stack.append(pairs[ch])
        elif stack and ch == stack[-1]:
            stack.pop()
            if not stack:
                return text[start + 1:i], i + 1
    return None, start


def guest_like(norm: str, core: str, lead: bool, given: bool, drafts_open: bool) -> bool:
    """ゲストに送る文面らしいか（敬語）。敬語の長い返信、文案があるときの指示の形でない敬語の返信"""
    unquoted = re.sub(r"[「『].*?[」』]", "", norm, flags=re.S)
    if not POLITE.search(unquoted):
        return False
    return len(core.strip()) > GUEST_TEXT_MIN or (drafts_open and not (lead and given))
REVIEW_WAIT_DAYS = 3  # レビューを書ける状態になるのを待つ日数（過ぎたら自動投稿をやめて知らせる）
UNKNOWN_NOTE = ("ℹ️ このレビューは、投稿されたか分からない状態です（自動では何もしません）。管理画面で確認し、"
                "投稿されていなければ「★5」のように、投稿しない場合は「レビューしない」と返信してください。")
REVIEW_SUBMIT_GRACE_SEC = 180  # 投稿の最後のボタンを押した後は、時間切れでも途中で止めずにこれだけ待つ
SM_TOP_RECHECK = 2  # 受信トレイの上位の行は、表示が変わらなくても毎回読み直す（一番上の行への2通目を拾うため）
EXIT_WAIT_HOURS = 3  # SM: 退室時の記録を待つ時間（それでも無ければ、レビューを投稿する前に確認を求める）


class Orchestrator(Engine):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # 同じスタンプ・返信を二重に処理しない（常駐のイベントと catch_up が重なった場合）
        self._handled: set[str] = set()
        self._review_notes = 0  # レビューの指示に返事をした回数（同じ返信に「評価が読み取れませんでした」を重ねないため）

    def _first_time(self, key: str) -> bool:
        if key in self._handled:
            return False
        self._handled.add(key)
        if len(self._handled) > 5000:
            self._handled = set(list(self._handled)[-2000:])
        return True

    @staticmethod
    def _base_emoji(name: str) -> str:
        return (name or "").split("::")[0]  # 肌の色の違い（::skin-tone-2）は同じ絵文字

    def is_approve_reaction(self, name: str) -> bool:
        return self._base_emoji(name) in self.cfg.approve_reactions

    # =================================================================
    # Slack: スタンプ
    # =================================================================
    async def on_reaction(self, user: str, item_ts: str, reaction: str, event_ts: str | None = None) -> None:
        """event_ts: Slack のイベントの時刻（付け直したスタンプも別の操作として受け付ける）。catch_up からは None"""
        if not self.is_approver(user):
            return
        if not self._first_time(f"rxev:{event_ts}" if event_ts else f"reaction:{reaction}@{item_ts}|{user}"):
            return
        if event_ts:
            # この後 catch_up が同じスタンプを拾っても、もう一度は処理しない（付け直したスタンプは event_ts が違うので受け付ける）
            self._handled.add(f"reaction:{reaction}@{item_ts}|{user}")
        msg = self.db.slack_msg(item_ts)
        if msg is None:
            return
        kind, rid, iid = MsgKind(msg["kind"]), msg["reservation_id"], msg["inquiry_id"]
        trigger = f"reaction:{reaction}@{item_ts}"
        self.db.log("reaction", reservation_id=rid, inquiry_id=iid, trigger=trigger, actor=user, result=kind.value)
        extra = json.loads(msg["extra"] or "{}")

        if not self.is_approve_reaction(reaction):
            # 承認以外のスタンプ（👀 🤔 など）では何もしない。操作待ちのメッセージなら、一度だけ案内する
            if self._actionable(msg) and not self.db.kv_get(f"rx_hint:{item_ts}"):
                self.db.kv_set(f"rx_hint:{item_ts}", "1")
                thread = self.res(rid)["slack_ts"] if rid else None
                await self.post(text=f"ℹ️ :{self._base_emoji(reaction)}: は承認のスタンプとして扱いません（何もしていません）。"
                                     "承認・送信するには ✅ を押してください。", thread_ts=thread)
            return

        if kind in (MsgKind.PARENT, MsgKind.REMINDER) and rid:
            r = self.res(rid)
            if kind is MsgKind.REMINDER and extra.get("target") == "drafts":
                return
            if r["kind"] == Kind.RESERVATION and r["state"] in (ResState.NOTIFIED, ResState.ON_HOLD, ResState.ERROR,
                                                                 ResState.MANUAL):
                await self.execute_template(rid, user, trigger)
            return
        if kind is MsgKind.DRAFT and iid:
            inq = dict(self.db.inquiry(iid))
            if inq["state"] not in OPEN_INQ_STATES:
                return
            r = self.res(inq["reservation_id"])
            if inq["draft_ts"] and inq["draft_ts"] != item_ts:
                await self.post(text="ℹ️ このメッセージは古い文案です（送信していません）。最新の文案にスタンプしてください。",
                                thread_ts=r["slack_ts"])
                return
            if inq["state"] == InqState.NO_DRAFT or not inq["draft_text"]:
                await self.post(text="この問い合わせには文案がありません。送る文面をこのスレッドに返信してください。",
                                thread_ts=r["slack_ts"])
                return
            checks = B.extract_checks(inq["draft_text"]) + [
                p for p in self.outgoing_problems(inq["draft_text"], r) if "要確認" not in p]
            if inq["needs_check"] or checks:
                self.db.log("stamp_refused_needs_check", reservation_id=rid, inquiry_id=iid, actor=user, trigger=trigger)
                await self.react(item_ts, "no_entry_sign")
                await self.post(text=B.needs_check_refused(user, inq["draft_text"], checks), thread_ts=r["slack_ts"])
                return
            await self.send_inquiry(iid, inq["draft_text"], user, trigger, revised=False)
            return
        if kind is MsgKind.REVISION_CONFIRM:
            text = extra.get("text")
            target = extra.get("inquiry_id")
            if not text:
                return
            if target:
                q = self.db.inquiry(int(target))
                if q is None or q["state"] not in UNSENT_INQ_STATES:
                    r = self.res(rid) if rid else None
                    await self.post(text="ℹ️ この修正文の対象だった文案は、すでに送信済みか、新しい文案に置き換わっています。"
                                         "送る場合は、もう一度修正文を返信してください。", thread_ts=r["slack_ts"] if r else None)
                    return
                await self.send_inquiry(int(target), text, user, trigger, revised=True)
            elif rid:
                try:
                    iid2 = self.db.insert("inquiries", reservation_id=rid, source_key=f"manual:{item_ts}", guest_text="",
                                          state=InqState.DRAFT_POSTED.value, draft_text=text, draft_ts=item_ts,
                                          created_at=iso(now()), updated_at=iso(now()))
                except sqlite3.IntegrityError:
                    iid2 = self.db.one("SELECT id FROM inquiries WHERE source_key=?", f"manual:{item_ts}")["id"]
                await self.send_inquiry(iid2, text, user, trigger, revised=True)
            return
        if kind is MsgKind.ERROR and rid:
            retry = extra.get("retry")
            if retry == "template":
                await self.execute_template(rid, user, trigger)
            elif retry == "inquiry" and iid:
                inq = dict(self.db.inquiry(iid))
                await self.send_inquiry(iid, inq["sent_text"] or inq["draft_text"], user, trigger,
                                        revised=(inq["sent_text"] or "") != (inq["draft_text"] or ""))
            elif retry == "review":
                r = self.res(rid)
                latest = self.db.kv_get(f"review_error_ts:{rid}")
                if r["state"] != ResState.REVIEW_ERROR or (latest and latest != item_ts):
                    await self.post(text="ℹ️ この失敗メッセージからは再実行しません（その後に確認メッセージが出ているか、状態が変わっています）。"
                                         "最新の確認メッセージにスタンプしてください。", thread_ts=r["slack_ts"])
                    return
                # 承認者が指定した評価・コメントは、再実行でも同じものを使う
                if not await self.post_review(rid, int(extra.get("stars") or self.cfg.review_stars), extra.get("comment"), user,
                                              approved=True, from_states=(ResState.REVIEW_ERROR,),
                                              expect=("review_error_ts", item_ts)):
                    r = self.res(rid)
                    await self.post(text=f"ℹ️ レビューを投稿できる状態ではありません（現在：{B.STATUS_LABEL[ResState(r['state'])]}）。"
                                         "管理画面で確認してください。", thread_ts=r["slack_ts"])
            return
        if kind is MsgKind.REVIEW_CONFIRM and rid:
            r = self.res(rid)
            latest = self.db.kv_get(f"review_confirm_ts:{rid}")
            if latest and latest != item_ts:
                await self.post(text="ℹ️ これは古い確認メッセージです（投稿していません）。最新の確認メッセージにスタンプしてください。",
                                thread_ts=r["slack_ts"])
                return
            if r["state"] != ResState.REVIEW_CONFIRM:
                # 確認待ちではない（受付済みで書けるのを待っている、その後に別の指示を受けた、投稿中・終了済み）
                args = json.loads(self.db.kv_get(f"review_args:{rid}") or "{}")
                if r["state"] == ResState.CONFIRMED and args.get("approved"):
                    note = "ℹ️ 確認済みです。レビューを書ける状態になったら、確認した内容で投稿します。"
                else:
                    note = (f"ℹ️ この確認メッセージからは投稿しません（現在：{B.STATUS_LABEL[ResState(r['state'])]}）。"
                            "最新のメッセージを確認してください。")
                await self.post(text=note, thread_ts=r["slack_ts"])
                return
            if not await self.post_review(rid, int(extra.get("stars") or self.cfg.review_stars), extra.get("comment"), user,
                                          approved=True, from_states=(ResState.REVIEW_CONFIRM,),
                                          expect=("review_confirm_ts", item_ts)):
                r = self.res(rid)
                await self.post(text=f"ℹ️ この確認メッセージからは投稿しません（現在：{B.STATUS_LABEL[ResState(r['state'])]}）。",
                                thread_ts=r["slack_ts"])

    async def snapshot_reactions(self, ts: str | None) -> None:
        """今付いているスタンプを「処理済み」として記録する（メッセージの意味が変わる前に押されたスタンプを、
        変わった後に承認として実行しないように。例: 問い合わせが予約リクエストに切り替わったとき）"""
        if not ts:
            return
        try:
            resp = await self.slack.reactions_get(channel=self.cfg.channel, timestamp=ts)
        except Exception as e:  # noqa: BLE001
            log.warning("reactions.get に失敗: %s", e)
            return
        for rx in (resp.get("message") or {}).get("reactions", []):
            for user in rx.get("users", []):
                trig = f"reaction:{rx['name']}@{ts}"
                self.db.log("reaction", trigger=trig, actor=user, result="preexisting")
                self._handled.add(f"{trig}|{user}")

    # =================================================================
    # Slack: スレッドへの返信（F3・F5）
    # =================================================================
    async def on_reply(self, user: str, thread_ts: str, text: str, ts: str) -> None:
        if not self.is_approver(user):
            return
        if not self._first_time(f"reply@{ts}|{user}"):
            return
        root = self.db.slack_msg(thread_ts)
        if root is None or MsgKind(root["kind"]) is not MsgKind.PARENT or not root["reservation_id"]:
            return
        rid = root["reservation_id"]
        r = self.res(rid)
        # 本文の無い返信（ファイルだけ等）も記録する（catch_up が何度も拾い直さないように）
        self.db.log("reply", reservation_id=rid, trigger=f"reply@{ts}", actor=user, detail=(text or "")[:500],
                    result=None if (text or "").strip() else "ignored_empty")
        if not (text or "").strip():
            return
        raw = text
        # Slack の書式（コードブロックの ``` やリンク表記、&amp; 等）を外して、ゲストに送る平文にする
        text, fenced = slack_to_plain(text)
        if not text and not REVIEW_RELATED.search(raw):
            return
        if DISMISS_WORDS.search(text) and self.open_drafts(rid):
            await self.dismiss_drafts(rid, user)
            return
        drafts = [d for d in self.open_drafts(rid) if d["state"] != InqState.APPROVED]
        review_wait = self._review_phase(r)
        reviewable = r["kind"] == Kind.RESERVATION and (
            r["state"] != ResState.CLOSED or bool(self.db.kv_get(f"review_declined:{rid}")))
        before = (self.db.kv_get(f"review_confirm_ts:{rid}"), self.db.kv_get(f"review_pref:{rid}"), r["state"],
                  self._review_notes)
        if reviewable and await self._review_instruction(rid, text, user, raw):
            await self._also_hold_or_deny(rid, user, text, thread_ts)
            return
        # レビューの確認・記録をしたか（したなら、下の「評価が読み取れませんでした」は出さない）
        review_touched = before != (self.db.kv_get(f"review_confirm_ts:{rid}"), self.db.kv_get(f"review_pref:{rid}"),
                                    self.res(rid)["state"], self._review_notes)
        norm = unicodedata.normalize("NFKC", text)
        stars_c, _ = self._parse_stars(norm, unicodedata.normalize("NFKC", raw))
        given_c = stars_c is not None or bool(re.search(COMMENT_LEAD, norm))
        core_c = re.sub(r"[「『].*[」』]", "「」", norm, flags=re.S) if given_c else norm
        if (r["kind"] == Kind.RESERVATION and not reviewable
                and (REVIEW_LEAD.match(norm) or (stars_c is not None and not norm.strip())
                     or (NO_REVIEW_WORDS.search(norm) and len(norm.strip()) <= GUEST_TEXT_MIN))
                and len(core_c.strip()) <= INSTRUCTION_MAX and not guest_like(norm, core_c, True, given_c, bool(drafts))
                and (given_c or NO_REVIEW_WORDS.search(norm))):
            # 対応を終えた予約（投稿済み・期限切れ・キャンセル）へのレビューの指示 → 黙って無視しない
            await self.post(text="ℹ️ この予約は対応を終了しています（レビューは投稿済み、または期限切れ・キャンセル）。"
                                 f"レビューについては何もしていません。<{r['dashboard_url']}|管理画面>で確認してください。",
                            thread_ts=r["slack_ts"])
            return
        if not text:
            return
        if (self._review_phase(r) and r["state"] == ResState.CONFIRMED and not drafts and not review_touched
                and not json.loads(self.db.kv_get(f"review_args:{rid}") or "{}").get("approved")
                and not self.db.kv_get(f"review_pref:{rid}")):
            # 判定を待つ間に自動投稿されないよう、先に止めた記録を残す（判定の後に確認を出す）
            self._set_review_pref(rid, {"hold": True})

        c = await self._classify(text, drafts)
        log.info("reply classified: %s %.2f", c.intent, c.confidence)
        # 判定の間に、利用完了の処理（レビューの自動投稿）で状態が変わっているかもしれない
        r = self.res(rid)
        review_wait = self._review_phase(r)
        # 承認者が確認した内容で、書ける状態になるのを待っている → レビューに関係ない返信では止めない
        approved_wait = r["state"] == ResState.CONFIRMED and bool(
            json.loads(self.db.kv_get(f"review_args:{rid}") or "{}").get("approved"))
        if (review_wait and not drafts and not approved_wait and not review_touched and r["state"] == ResState.CONFIRMED
                and c.intent not in ("HOLD", "CANCEL_OR_DENY")):
            # 利用後（★5の自動投稿の前）に承認者の返信があった → 念のため自動投稿は止め、確認を求める
            await self._ask_review_confirm(rid, r, "⏸ 返信があったため、レビューの自動投稿を止めました。\n",
                                           from_states=(ResState.CONFIRMED,))
        if (review_wait and not drafts and not approved_wait and not review_touched
                and c.intent not in ("HOLD", "CANCEL_OR_DENY", "REVISION")):
            await self.post(text="評価が読み取れませんでした。「★3 コメント「…」」のように返信してください"
                                 "（「レビューしない」で投稿せずに終了）。ゲストに送る文面なら、敬語の全文で返信してください。",
                            thread_ts=r["slack_ts"])
            return
        # 未送信の文案があるのに「メモ・雑談」かどうか自信が無い、またはコードブロックで書かれた返信は、
        # 黙って無視せず「この文面で送ってよいか」を確認する（返信漏れ防止）
        if c.intent == "OTHER" and drafts and (fenced or (c.confidence < 0.7 and len(text) >= 10)):
            c = Classification("REVISION", 0.5, None)
        if c.intent == "HOLD":
            await self._hold(rid, user)
        elif c.intent == "CANCEL_OR_DENY":
            await self._deny(rid, user, thread_ts)
        elif c.intent == "REVISION":
            target = None
            approved = [d for d in self.open_drafts(rid) if d["state"] == InqState.APPROVED]
            if len(drafts) == 1:
                target = drafts[0]["id"]
            elif c.target_draft_id and any(d["id"] == c.target_draft_id for d in drafts):
                target = c.target_draft_id
            elif not drafts and len(approved) == 1:
                target = approved[0]["id"]  # 承認済み（定型文待ち）の返信の置き換え（両方送られないように）
            # 修正文は、送る前に必ず確認する（スタッフ向けのメモや指示を、誤ってゲストに送らないように）
            if target and not drafts:
                head = ("✏️ 承認済み（定型文の後に送る予定）の返信を、この文面に置き換えます。"
                        "よければ、このメッセージにスタンプを押してください。")
            elif target:
                head = "✏️ この文面で、文案の代わりにゲストへ送信します。よければ、このメッセージにスタンプを押してください。"
            elif drafts:
                head = "❓ どの文案への修正か判断できません。この文面を新しい返信としてゲストに送ってよければ、スタンプを押してください。"
            else:
                head = "❓ 未送信の文案がありません。この文面をそのままゲストに送ってよければ、このメッセージにスタンプを押してください。"
            body = f"{head}\n```{B.esc(text)}```"
            problems = self.outgoing_problems(text, r, human=True)
            if problems:
                body += "\n⚠️ " + "、".join(problems) + "（このままでは送信できません。直した全文を返信してください）"
            await self.post(text=body, thread_ts=thread_ts, kind=MsgKind.REVISION_CONFIRM, rid=rid, iid=target,
                            extra={"text": text, "inquiry_id": target})
        # OTHER は何もしない

    async def _classify(self, text: str, drafts: list[dict]) -> Classification:
        if self.llm is not None:
            try:
                return await self.llm.classify_reply(text, drafts)
            except Exception as e:  # noqa: BLE001
                log.warning("分類に失敗。キーワードで判定: %s", e)
                await self.alert("llm:classify", f"Claude API の呼び出しに失敗しています（{type(e).__name__}）。"
                                 "返信の判定はキーワードで代用しています。API キー・残高・障害情報を確認してください。",
                                 mention=False)
        if CANCEL_WORDS.search(text) and len(text) < 40:
            return Classification("CANCEL_OR_DENY", 0.6, None)
        if HOLD_WORDS.search(text) and len(text) < 40:
            return Classification("HOLD", 0.6, None)
        if len(text) >= 20 and re.search(r"(です|ます|ください|ませ)", text):
            return Classification("REVISION", 0.5, None)
        return Classification("OTHER", 0.5, None)

    async def _deny(self, rid: int, user: str, thread_ts: str) -> None:
        """否認・キャンセルの返信: 自動では実行せず、保留にして管理画面での操作を案内する"""
        r = self.res(rid)
        review_side = r["state"] == ResState.COMPLETING or (r["state"] == ResState.CONFIRMED and self._review_phase(r))
        held = await self._hold(rid, user, quiet=True)
        tail = "" if review_side else ("この予約は保留にしました（リマインドします）。" if held else "")
        await self.post(text=f"🙅 否認・キャンセルは自動では実行しません。<{r['dashboard_url']}|管理画面>で操作をお願いします。{tail}",
                        thread_ts=thread_ts)

    async def _also_hold_or_deny(self, rid: int, user: str, text: str, thread_ts: str) -> None:
        """レビューの指示と一緒に書かれた「待って」「否認」も行う（例:「否認して。レビューもなしで」）。
        レビューに触れた部分（「レビューは待って」など）とコメント「…」の中は見ない"""
        body = unicodedata.normalize("NFKC", re.sub(r"「.*」|『.*』", "", text, flags=re.S))
        rest = "。".join(part for part in SENTENCE_SPLIT.split(body)
                        if part.strip() and not re.search(r"コメント|お礼文|評価", part) and not REVIEW_RELATED.search(part))
        if REST_DENY.search(rest):
            await self._deny(rid, user, thread_ts)
        elif REST_HOLD.search(rest) and self.res(rid)["state"] not in (ResState.COMPLETING, ResState.REVIEW_CONFIRM,
                                                                        ResState.REVIEW_ERROR):
            await self._hold(rid, user)  # 投稿中・確認待ちは、レビューの指示の方で返事済み

    async def _hold(self, rid: int, user: str, quiet: bool = False) -> bool:
        r = self.res(rid)
        nxt = iso(now() + timedelta(minutes=self.cfg.remind_interval_minutes))
        changed = False
        if r["kind"] == Kind.RESERVATION and self.db.transition(
                "reservations", rid, (ResState.NOTIFIED, ResState.ERROR), ResState.ON_HOLD,
                hold_since=iso(now()), next_remind_at=nxt):
            changed = True
        for d in self.open_drafts(rid):
            if self.db.transition("inquiries", d["id"], (InqState.DRAFT_POSTED, InqState.NO_DRAFT, InqState.ERROR),
                                  InqState.ON_HOLD, hold_since=iso(now()), next_remind_at=nxt):
                changed = True
        if r["kind"] == Kind.RESERVATION and r["state"] == ResState.COMPLETING:
            # レビューの投稿中の「待って」→ 記録し、投稿されなかった場合は確認を求める（止められない可能性は正直に伝える）
            cur = json.loads(self.db.kv_get(f"review_pref:{rid}") or "{}")
            if not cur.get("decline"):
                self._set_review_pref(rid, {**cur, "hold": True})
            await self.post(text=f"⚠️ 今まさにレビューを投稿しているところのため、止められない可能性があります（<@{user}>）。"
                                 "結果のメッセージを確認してください。"
                                 + ("「レビューしない」の指示を受けているため、投稿されなかった場合はそのまま終了します。" if cur.get("decline")
                                    else "投稿されなかった場合は、確認を求めます。"), thread_ts=r["slack_ts"])
            await self.refresh_parent(rid)
            return True
        if self._review_phase(r) and r["state"] == ResState.CONFIRMED:
            # 利用後（レビューの自動投稿の前）の「待って」→ 文案の保留とは別に、自動投稿を止めて確認を求める
            if changed and not quiet:
                await self.post(text=f"⏸ 未送信の文案を保留にしました（<@{user}>）。", thread_ts=r["slack_ts"])
            await self._ask_review_confirm(rid, r, f"⏸ レビューの自動投稿を止めました（<@{user}>）。\n")
            await self.refresh_parent(rid)
            return True
        if not quiet:
            if changed:
                mins = self.cfg.remind_interval_minutes
                every = f"{mins // 60}時間" if mins % 60 == 0 else f"{mins}分"
                await self.post(text=f"⏸ 保留にしました（<@{user}>）。{every}ごとにリマインドします。"
                                     "スタンプで承認・送信すると止まります。", thread_ts=r["slack_ts"])
            elif self._review_unknown(rid, r):
                await self.post(text=UNKNOWN_NOTE, thread_ts=r["slack_ts"])
            elif r["state"] in (ResState.REVIEW_CONFIRM, ResState.REVIEW_ERROR):
                await self.post(text="⏸ レビューは投稿していません。⚠️ のメッセージにスタンプを押すまで投稿しません"
                                     "（「レビューしない」と返信すれば、投稿せずに終了します）。", thread_ts=r["slack_ts"])
            else:
                await self.post(text=f"ℹ️ 保留にできるもの（承認待ちの予約・未送信の文案）がありません"
                                     f"（現在：{B.STATUS_LABEL[ResState(r['state'])]}）。", thread_ts=r["slack_ts"])
        await self.refresh_parent(rid)
        return changed

    # =================================================================
    # リマインド（F3・F5）
    # =================================================================
    async def tick_reminders(self) -> None:
        due = iso(now())
        rows = self.db.all(
            "SELECT * FROM reservations WHERE kind=? AND state IN (?, ?, ?) AND next_remind_at IS NOT NULL AND next_remind_at<=?",
            Kind.RESERVATION.value, ResState.NOTIFIED.value, ResState.ON_HOLD.value, ResState.ERROR.value, due)
        for row in rows:
            try:
                await self._remind_reservation(dict(row))
            except Exception:  # noqa: BLE001
                log.exception("リマインドに失敗 rid=%s", row["id"])
        rows = self.db.all(
            "SELECT reservation_id, MIN(next_remind_at) AS due, MIN(COALESCE(hold_since, created_at)) AS since, COUNT(*) AS n "
            "FROM inquiries WHERE state IN (?, ?, ?, ?) AND next_remind_at IS NOT NULL GROUP BY reservation_id HAVING due<=?",
            *OPEN_INQ_STATES, due)
        for row in rows:
            try:
                await self._remind_drafts(row["reservation_id"], from_iso(row["since"]), row["n"])
            except Exception:  # noqa: BLE001
                log.exception("文案のリマインドに失敗 rid=%s", row["reservation_id"])

    async def _check_platform(self, rid: int) -> bool:
        """管理画面を読み、キャンセル・手動対応・新着を反映する。リマインドを続けるなら True"""
        r = self.res(rid)
        platform = Platform(r["platform"])
        if platform is Platform.IB and r["platform_id"].startswith("thread-"):
            # 予約に紐づかない問い合わせ: スレッドだけを読む（手動返信・新着の検知）
            if r["thread_id"]:
                try:
                    msgs, _, _ = await self.ib.get_thread(r["thread_id"])
                    await self.process_guest_messages(rid, msgs, r["thread_id"])
                except LoginRequired as e:
                    await self.alert(f"login:{platform.value}", f"{platform.label}への再ログインが必要です（{e}）")
                except Exception as e:  # noqa: BLE001
                    log.warning("スレッドの確認に失敗: %s", e)
            return True
        try:
            d = await self.adapter(platform).get_detail(r["platform_id"])
        except LoginRequired as e:
            await self.alert(f"login:{platform.value}", f"{platform.label}への再ログインが必要です（{e}）")
            return True
        except Exception as e:  # noqa: BLE001
            log.warning("状態確認に失敗: %s", e)
            return True
        self.db.update("reservations", rid, **self._detail_cols(platform, d))
        dl = re.search(r"期限[:：]\s*あと[^\n]+", d.raw_status_text or "")
        self.db.kv_set(f"deadline:{rid}", dl.group(0) if dl else None)
        thread_key = (d.thread_id or r["thread_id"]) if platform is Platform.IB else r["platform_id"]
        # 手動返信の検知と新着は、キャンセル済みでも見る（キャンセルと同時に届いたメッセージを捨てない）
        if thread_key and d.messages:
            await self.process_guest_messages(self._thread_target(rid, platform, thread_key), d.messages, thread_key)
        if d.closed_reason:
            await self.close(rid, d.status)
            return False
        r = self.res(rid)
        if (r["kind"] == Kind.RESERVATION and r["state"] in (ResState.NOTIFIED, ResState.ON_HOLD, ResState.ERROR)
                and self.template_sent_in(platform, d) and not self.needs_approval(platform, d)):
            if self.db.transition("reservations", rid, (ResState.NOTIFIED, ResState.ON_HOLD, ResState.ERROR),
                                  ResState.CONFIRMED, template_sent_at=iso(now()), next_remind_at=None):
                self.db.kv_set(f"await_payment:{rid}", None)
                await self.post(text="✅ 管理画面で、予約の確定と定型文・マニュアルの送信済みを確認しました（手動対応）。"
                                     "自動送信はせず、リマインドを止めます。", thread_ts=r["slack_ts"])
                await self.refresh_parent(rid)
                await self.flush_approved(rid)
            return False
        ok_status = SM_SEND_OK if platform is Platform.SM else IB_SEND_OK
        if self.db.kv_get(f"await_payment:{rid}") and (d.status or "").strip() in ok_status:
            self.db.kv_set(f"await_payment:{rid}", None)
            await self.post(text="💳 ゲストの支払いが確認され、予約が確定しました。"
                                 "親メッセージにスタンプで、定型文とマニュアルを送ります。", thread_ts=r["slack_ts"], mention=True)
        if platform is Platform.IB and "リクエスト" not in d.status and r["state"] == ResState.MANUAL:
            if self.db.transition("reservations", rid, (ResState.MANUAL,), ResState.NOTIFIED,
                                  next_remind_at=iso(now() + timedelta(minutes=self.cfg.remind_interval_minutes))):
                await self.post(text="✅ インスタベースで予約リクエストが承認されました。"
                                     "親メッセージにスタンプで、定型文とマニュアルを送信します。",
                                thread_ts=r["slack_ts"], mention=True)
                await self.refresh_parent(rid)
        return True

    def _thread_target(self, rid: int, platform: Platform, thread_key: str) -> int:
        """IB のスレッドはゲストの予約で共有される。閉じた予約から読んだ発言は、同じスレッドの進行中の予約で処理する"""
        if platform is not Platform.IB or self.res(rid)["state"] != ResState.CLOSED:
            return rid
        rows = [dict(x) for x in self.db.all("SELECT * FROM reservations WHERE platform='ib' AND thread_id=? AND state<>?",
                                             thread_key, ResState.CLOSED.value)]
        if not rows:
            return rid
        return min(rows, key=lambda c: abs((from_iso(c["start_at"]) or now()) - now()))["id"]

    def _remind_minutes(self, deadline: str | None) -> int:
        """承認の期限が近いときは、リマインドの間隔を縮める"""
        base = self.cfg.remind_interval_minutes
        m = re.search(r"あと\s*(\d+)\s*(日|時間|分)", deadline or "")
        if not m:
            return base
        left = int(m.group(1)) * {"日": 1440, "時間": 60, "分": 1}[m.group(2)]
        return max(30, min(60, left // 2)) if left <= base else base

    async def _remind_reservation(self, r: dict) -> None:
        rid = r["id"]
        if not await self._check_platform(rid):
            return
        r = self.res(rid)
        if r["state"] not in (ResState.NOTIFIED, ResState.ON_HOLD, ResState.ERROR):
            return
        end = from_iso(r["cleanup_end_at"]) or from_iso(r["end_at"])
        if end and end <= now():
            # 利用後に暗証番号を送るよう促さない（利用時間を過ぎた警告は tick_completion が出す）
            self.db.update("reservations", rid, next_remind_at=None)
            return
        since = from_iso(r["hold_since"] or r["created_at"])
        hours = (now() - since).total_seconds() / 3600
        if r["state"] == ResState.ON_HOLD:
            what = "保留"
        elif r["state"] == ResState.ERROR:
            what = "送信失敗のまま"
        else:
            what = "支払い待ち" if self.db.kv_get(f"await_payment:{rid}") else "承認待ち"
        text = B.reminder(r, hours, what)
        deadline = self.db.kv_get(f"deadline:{rid}")
        if deadline:
            text += f"\n⌛ 承認{deadline}"
        self.db.update("reservations", rid, next_remind_at=iso(now() + timedelta(minutes=self._remind_minutes(deadline))))
        await self.post(text=text, thread_ts=r["slack_ts"], kind=MsgKind.REMINDER, rid=rid,
                        extra={"target": "reservation"}, mention=True)

    async def _remind_drafts(self, rid: int, since, n: int) -> None:
        # キャンセル済み・利用済みの予約でも、未送信の文案があればリマインドする（戻り値で止めない）
        await self._check_platform(rid)
        drafts = [d for d in self.open_drafts(rid) if d["state"] in OPEN_INQ_STATES]
        nxt = iso(now() + timedelta(minutes=self.cfg.remind_interval_minutes))
        for d in drafts:
            self.db.update("inquiries", d["id"], next_remind_at=nxt)  # 投稿に失敗しても毎分繰り返さない
        if not drafts:
            return
        r = self.res(rid)
        hours = (now() - since).total_seconds() / 3600 if since else 0
        await self.post(text=B.draft_reminder(hours, len(drafts)), thread_ts=r["slack_ts"], kind=MsgKind.REMINDER,
                        rid=rid, extra={"target": "drafts"}, mention=True)

    # =================================================================
    # 利用完了とレビュー（F6・F7）
    # =================================================================
    async def tick_completion(self) -> None:
        grace = timedelta(minutes=self.cfg.completion_grace_minutes)
        for row in self.db.all("SELECT * FROM reservations WHERE kind=? AND state=?", Kind.RESERVATION.value,
                               ResState.CONFIRMED.value):
            end = from_iso(row["cleanup_end_at"]) or from_iso(row["end_at"])
            if end and now() >= end + grace:
                try:
                    await self.complete(row["id"], "time")
                except Exception:  # noqa: BLE001
                    log.exception("利用完了の処理に失敗 rid=%s", row["id"])
        # 承認されないまま利用時間を過ぎた予約
        for row in self.db.all("SELECT * FROM reservations WHERE kind=? AND state IN (?, ?, ?)", Kind.RESERVATION.value,
                               ResState.NOTIFIED.value, ResState.ON_HOLD.value, ResState.ERROR.value):
            end = from_iso(row["end_at"])
            if end and now() >= end and not row["deadline_warned"]:
                try:
                    self.db.update("reservations", row["id"], deadline_warned=iso(now()))
                    if await self._check_platform(row["id"]):
                        await self.alert(f"overdue:{row['id']}", "定型文・マニュアルを送らないまま利用時間を過ぎました。"
                                         "管理画面の状況を確認してください。", thread_ts=row["slack_ts"])
                except Exception:  # noqa: BLE001
                    log.exception("期限切れの確認に失敗 rid=%s", row["id"])

    async def complete(self, rid: int, trigger: str) -> None:
        r = self.res(rid)
        if r["state"] == ResState.REVIEW_CONFIRM and trigger == "exit_record" and r["platform"] == Platform.SM.value:
            await self._warn_exit_while_confirming(rid, r)
            return
        if r["state"] != ResState.CONFIRMED:
            return
        if trigger == "time":
            # 退室時の記録を待っている間は、30分に1回だけ確認する（毎回、管理画面を開かない）
            last = from_iso(self.db.kv_get(f"complete_checked:{rid}"))
            if last and now() - last < timedelta(minutes=30):
                return
            self.db.kv_set(f"complete_checked:{rid}", iso(now()))
        await self.ensure_parent(rid)
        r = self.res(rid)
        platform = Platform(r["platform"])
        exit_status, exit_detail = r["exit_status"], r["exit_detail"]
        try:
            d = await self.adapter(platform).get_detail(r["platform_id"])
        except LoginRequired as e:
            await self.alert(f"login:{platform.value}", f"{platform.label}への再ログインが必要です（{e}）")
            return
        except Exception as e:  # noqa: BLE001
            log.warning("利用完了の確認に失敗（次回やり直します）: %s", e)
            n = int(self.db.kv_get(f"complete_fail:{rid}") or 0) + 1
            self.db.kv_set(f"complete_fail:{rid}", str(n))
            if n >= 3:
                await self.alert(f"complete_fail:{rid}", f"利用完了・レビューの確認のため管理画面を読もうとしましたが、{n}回続けて失敗しています"
                                 f"（{type(e).__name__}）。<{r['dashboard_url']}|管理画面>で確認してください。", thread_ts=r["slack_ts"])
            return
        self.db.kv_set(f"complete_fail:{rid}", None)
        if self.res(rid)["state"] != ResState.CONFIRMED:
            return  # 読んでいる間に、承認者の指示などで状態が変わった
        self.db.update("reservations", rid, **self._detail_cols(platform, d))
        if d.closed_reason:
            await self.close(rid, d.status)
            return
        pref = json.loads(self.db.kv_get(f"review_pref:{rid}") or "{}")
        if pref.get("decline"):
            if self.db.transition("reservations", rid, (ResState.CONFIRMED,), ResState.CLOSED):
                self.db.kv_set(f"review_declined:{rid}", "1")
                await self.post(text=B.completion(r, d.exit_status, None, None) + "\n（指示のとおり、レビューは投稿していません）",
                                thread_ts=r["slack_ts"])
                await self.refresh_parent(rid)
            return
        args = json.loads(self.db.kv_get(f"review_args:{rid}") or "{}")
        exit_head = ""
        if platform is Platform.SM:
            st = d.exit_status or exit_status
            if st and "問題なく" not in st and self.db.kv_get(f"exit_warned:{rid}") != st:
                dt = d.exit_detail or exit_detail
                exit_head = (f"⚠️ 利用が終わりましたが、退室時状態が「{B.esc(st)}」です。\n" + (f"{B.quote(dt)}\n" if dt else ""))
        if pref.get("hold"):
            head = pref.get("head") or "📝 利用前・利用中に受けた指示のとおり、レビューの内容を確認してください。\n"
            if exit_head and exit_head not in head:
                head = exit_head + head
            if await self._ask_review_confirm(rid, r, head, pref.get("stars"), pref.get("comment"),
                                              from_states=(ResState.CONFIRMED,)) and exit_head:
                self.db.kv_set(f"exit_warned:{rid}", d.exit_status or exit_status)
            return
        if args.get("approved") and exit_head:
            # 承認した後に、退室時状態に気になる点が記録された → そのまま投稿せず、もう一度確認する
            if await self._ask_review_confirm(rid, r, exit_head + "承認済みの内容で投稿する前に、もう一度確認してください。\n",
                                              from_states=(ResState.CONFIRMED,)):
                self.db.kv_set(f"exit_warned:{rid}", d.exit_status or exit_status)
            return
        if args.get("approved"):
            # 承認者が確認した評価・コメント（まだ書けなかったので待っていた）で投稿する
            await self.post_review(rid, int(args.get("stars") or self.cfg.review_stars), None if args.get("auto") else args.get("comment"),
                                   "system", approved=True, from_states=(ResState.CONFIRMED,))
            return
        if platform is Platform.SM:
            exit_status, exit_detail = d.exit_status or exit_status, d.exit_detail or exit_detail
            if self.cfg.dry_run and self.db.kv_get(f"sim_exit:{rid}"):
                exit_status = self.db.kv_get(f"sim_exit:{rid}")
                self.db.kv_set(f"sim_exit:{rid}", None)
            if d.review_posted:
                self.db.transition("reservations", rid, (ResState.CONFIRMED,), ResState.CLOSED,
                                   review_posted_at=iso(now()))
                await self.post(text=B.completion(r, exit_status, None, None) + "\n（レビューは管理画面で投稿済みでした）",
                                thread_ts=r["slack_ts"])
                await self.refresh_parent(rid)
                return
            if not exit_status:
                # 退室時の記録が無い（まだ記録していない、または画面の変更で読めない）。少し待ち、それでも無ければ確認を求める
                end = from_iso(r["cleanup_end_at"]) or from_iso(r["end_at"])
                if end and now() < end + timedelta(hours=EXIT_WAIT_HOURS) and trigger != "simulate":
                    return
                await self._ask_review_confirm(rid, r, "⚠️ 利用が終わりましたが、退室時の状態の記録が見つかりません。\n",
                                               from_states=(ResState.CONFIRMED,))
                return
            if "問題なく" not in exit_status:
                head = (f"⚠️ 利用が終わりましたが、退室時状態が「{B.esc(exit_status)}」です。\n"
                        + (f"{B.quote(exit_detail)}\n" if exit_detail else ""))
                if await self._ask_review_confirm(rid, r, head, from_states=(ResState.CONFIRMED,)):
                    self.db.kv_set(f"exit_warned:{rid}", exit_status)  # 確認に載せたので、巡回で重ねて知らせない
                return
        await self.post_review(rid, self.cfg.review_stars, None, "system", from_states=(ResState.CONFIRMED,))

    async def _warn_exit_while_confirming(self, rid: int, r: dict, d=None) -> None:
        """レビューの確認待ちの間に、SM の退室時状態に「気になる点」が記録された → スレッドに知らせる（1回だけ）。
        d: 巡回で読んだ予約の詳細（無ければ読む）"""
        if d is None:
            try:
                d = await self.adapter(Platform.SM).get_detail(r["platform_id"])
            except Exception as e:  # noqa: BLE001
                log.warning("退室時状態の確認に失敗: %s", e)
                return
        self.db.update("reservations", rid, **self._detail_cols(Platform.SM, d))
        status = d.exit_status or ""
        if not status or "問題なく" in status or self.db.kv_get(f"exit_warned:{rid}") == status:
            self.db.kv_set(f"exit_warn_pending:{rid}", None)
            return
        r = self.res(rid)
        if r["state"] not in (ResState.REVIEW_CONFIRM, ResState.REVIEW_ERROR) or d.review_posted or self._review_unknown(rid, r):
            # 確認・再実行のメッセージが無い（投稿済み、または投稿されたか分からない状態。その知らせで管理画面の確認を求めている）
            self.db.kv_set(f"exit_warn_pending:{rid}", None)
            return
        ts = await self.post(text=f"⚠️ 退室時状態が「{B.esc(status)}」と記録されました。\n"
                                  + (f"{B.quote(d.exit_detail)}\n" if d.exit_detail else "")
                                  + "スタンプする前に、レビューの確認・再実行のメッセージの内容（評価・コメント）を見直してください"
                                  "（変える場合は「★3 コメント「…」」のように返信）。", thread_ts=r["slack_ts"], mention=True)
        if ts:
            self.db.kv_set(f"exit_warned:{rid}", status)  # 知らせられたときだけ
            self.db.kv_set(f"exit_warn_pending:{rid}", None)
        else:
            self.db.kv_set(f"exit_warn_pending:{rid}", "1")  # 出せなかった → 定期処理（tick_repost）でもう一度

    def _review_phase(self, r: dict) -> bool:
        """レビューの段階か（確認待ち・失敗、または利用が終わって自動投稿を待っている）"""
        if r["state"] in (ResState.REVIEW_CONFIRM, ResState.REVIEW_ERROR):
            return True
        end = from_iso(r["cleanup_end_at"]) or from_iso(r["end_at"])
        return r["state"] == ResState.CONFIRMED and r["kind"] == Kind.RESERVATION and bool(end) and end <= now()

    async def _ask_review_confirm(self, rid: int, r: dict, head: str, stars: int | None = None,
                                  comment: str | None = None,
                                  from_states: tuple = (ResState.CONFIRMED, ResState.REVIEW_ERROR, ResState.REVIEW_CONFIRM)) -> bool:
        """レビューを投稿する前に、承認者の確認を求める（このメッセージへのスタンプで投稿）。確認を出せたら True。
        表示した評価・コメントと、スタンプで投稿する内容は必ず同じ（extra に入れる）。
        評価・コメントの指定が無ければ、それまでの指示（review_pref）か、承認済みで書けるのを待っている内容を引き継ぐ。
        「レビューしない」と指示されている利用後の予約は、確認を出さずに投稿せずに終える"""
        head_in = head
        state0 = ResState(self.res(rid)["state"])
        pending = json.loads(self.db.kv_get(f"review_confirm_pending:{rid}") or "{}")
        reissue = bool(pending) and state0 is ResState.REVIEW_CONFIRM and stars is None and comment is None and (
            self.db.kv_get(f"review_confirm_ts:{rid}") or "").startswith("-")
        if reissue:
            # 途中で止まった・出せなかった確認の出し直し: 出そうとしていた内容・元の状態・見出しを使う
            stars, comment = pending.get("stars"), pending.get("comment")
            head = (pending.get("head") or "") + head
        pref0 = json.loads(self.db.kv_get(f"review_pref:{rid}") or "{}")
        if not reissue and pref0.get("head") and pref0["head"] not in head:
            head = pref0["head"] + head  # 前に出せなかった確認の見出し（退室時状態の警告など）
        if stars is None:
            pref = json.loads(self.db.kv_get(f"review_pref:{rid}") or "{}")
            args = json.loads(self.db.kv_get(f"review_args:{rid}") or "{}")
            state = ResState(self.res(rid)["state"])
            if comment is None and pref.get("decline") and ResState.CONFIRMED in from_states and state is ResState.CONFIRMED:
                if self.db.transition("reservations", rid, (ResState.CONFIRMED,), ResState.CLOSED):
                    self.db.kv_set(f"review_declined:{rid}", "1")
                    await self.post(text="🏁 「レビューしない」の指示のとおり、レビューは投稿せずに対応を終了しました。"
                                         "やっぱり投稿する場合は、期限内に「★5」のように返信すれば再開します。", thread_ts=r["slack_ts"])
                    await self.refresh_parent(rid)
                return False
            # 今出ている確認・失敗のメッセージの内容 → 利用前などの指示 → 承認済みで待っている内容 の順に引き継ぐ
            for src in (self._shown_review(rid, state), pref, args if args.get("approved") else {}):
                if src.get("stars"):
                    stars = int(src["stars"])
                    if comment is None and not src.get("auto"):
                        comment = src.get("comment")
                    break
        prior = ResState(self.res(rid)["state"])
        prior_saved = ResState(pending.get("prior") or prior.value) if reissue else prior
        prev_ts = self.db.kv_get(f"review_confirm_ts:{rid}")
        asked = (stars, comment)
        if not self.db.transition("reservations", rid, from_states, ResState.REVIEW_CONFIRM):
            return False
        # 新しい確認を出すので、それまでの確認メッセージへのスタンプは受け付けない（投稿の通信の間も）。
        # 出すたびの印にして、同時に出した別の確認と取り違えない
        token = f"-:{uuid.uuid4().hex}"
        self.db.kv_set(f"review_confirm_ts:{rid}", token)
        stars = stars or self.cfg.review_stars
        self.db.kv_set(f"review_confirm_pending:{rid}", json.dumps(
            {"stars": stars, "comment": comment, "prior": prior_saved.value, "at": iso(now()),
             "head": (pending.get("head") if reissue else head) or "",
             "prev_ts": pending.get("prev_ts") if pending else (prev_ts if prev_ts and not prev_ts.startswith("-") else None)},
            ensure_ascii=False))
        what = f"★{stars}・" + (f"コメント「{B.esc(comment)}」" if comment else "お礼のコメントは自動で作成")
        text = (head + f"<{r['dashboard_url']}|管理画面>で内容を確認してください。\n"
                f"✅ このメッセージにスタンプ → {what}でレビューを投稿（投稿後は編集できません）\n"
                "✏️ 評価やコメントを変える場合 → スレッドに「★3 コメント「…」」のように返信（「レビューしない」で投稿せず終了）")
        ts = await self.post(text=text, thread_ts=r["slack_ts"], kind=MsgKind.REVIEW_CONFIRM, rid=rid, mention=True,
                             extra={"stars": stars, "comment": comment})
        if self.db.kv_get(f"review_confirm_ts:{rid}") != token:
            return bool(ts)  # その間に、別の確認が出された（その確認が最新。こちらの結果で上書きしない）
        if not ts and self.res(rid)["state"] != ResState.REVIEW_CONFIRM:
            # 投稿の通信の間に「レビューしない」などで状態が変わった（その指示が優先。出し直さない）
            self.db.kv_set(f"review_confirm_pending:{rid}", None)
            log.warning("レビューの確認メッセージを投稿できませんでしたが、その間に状態が変わりました rid=%s", rid)
            return False
        if not ts:
            # Slack に出せなかった。自動投稿を止めた記録を残してから元の状態に戻し、知らせる
            if prior is ResState.CONFIRMED:
                cur = json.loads(self.db.kv_get(f"review_pref:{rid}") or "{}")
                if not cur.get("decline"):
                    new = {**cur, "hold": True, "head": cur.get("head") or head_in}
                    if asked != (None, None):
                        new.update(stars=asked[0] if asked[0] is not None else cur.get("stars"), comment=asked[1])
                    self._set_review_pref(rid, new)
            if prior is ResState.REVIEW_CONFIRM:
                # 置き換えようとした前の確認メッセージは無効のまま（古い内容で投稿しない）。
                # 出そうとした内容は記録してあるので、定期処理（tick_repost）が後で出し直す
                pass
            else:
                self.db.transition("reservations", rid, (ResState.REVIEW_CONFIRM,), prior)
                self.db.kv_set(f"review_confirm_pending:{rid}", None)
            log.warning("レビューの確認メッセージを投稿できませんでした rid=%s", rid)
            await self.alert(f"review_confirm:{rid}", "レビューの確認メッセージを Slack に投稿できませんでした。レビューは投稿していません。"
                             + ("少し後に自動で出し直します。" if prior in (ResState.REVIEW_CONFIRM, ResState.CONFIRMED)
                                else "スレッドに「★5」のように返信し直してください。"),
                             thread_ts=r["slack_ts"])
            return False
        self.db.kv_set(f"review_confirm_ts:{rid}", ts)
        st = self.res(rid)["exit_status"]
        if st and f"退室時状態が「{B.esc(st)}」" in text:
            self.db.kv_set(f"exit_warned:{rid}", st)  # この確認で退室時状態を知らせた（巡回で重ねて知らせない）
        self.db.kv_set(f"review_confirm_pending:{rid}", None)
        if self.res(rid)["state"] != ResState.REVIEW_CONFIRM:
            return True  # 投稿の通信の間に「レビューしない」などで状態が変わった（その指示が優先）
        self.db.kv_set(f"repost_review_error:{rid}", None)  # 出せなかった失敗メッセージは、もう出し直さない
        if self.db.kv_get(f"review_error_ts:{rid}") not in (None, "-"):
            self.db.kv_set(f"review_error_ts:{rid}", "x")  # 前の失敗メッセージからの再実行は受け付けない（新しい確認に置き換わった）
        self._review_notes += 1
        # 確認を出したので、それまでの指示・承認は済み（このメッセージへのスタンプで改めて確認する）
        self.db.kv_set(f"review_pref:{rid}", None)
        args = json.loads(self.db.kv_get(f"review_args:{rid}") or "{}")
        if args.get("approved"):
            args["approved"] = False
            self.db.kv_set(f"review_args:{rid}", json.dumps(args, ensure_ascii=False))
        if prior_saved is ResState.CLOSED:
            self.db.kv_set(f"review_declined:{rid}", None)  # 「レビューしない」から再開した
        await self.refresh_parent(rid)
        return True

    async def reissue_stuck_confirms(self, min_age_minutes: int = 0) -> None:
        """確認メッセージを出す途中で止まった・出せなかった予約（REVIEW_CONFIRM で確認が無い）に、出し直す"""
        for row in self.db.all("SELECT * FROM reservations WHERE state=?", ResState.REVIEW_CONFIRM.value):
            rid = row["id"]
            ts = self.db.kv_get(f"review_confirm_ts:{rid}") or ""
            if not ts.startswith("-") or self._locks[rid].locked():
                continue
            pending = json.loads(self.db.kv_get(f"review_confirm_pending:{rid}") or "{}")
            at = from_iso(pending.get("at"))
            if min_age_minutes and at and now() - at < timedelta(minutes=min_age_minutes):
                continue  # まだ投稿の通信の途中かもしれない
            await self._ask_review_confirm(rid, dict(row), "⚠️ 確認メッセージを出す途中でシステムが止まったため、出し直します。\n",
                                           from_states=(ResState.REVIEW_CONFIRM,))

    def _review_unknown(self, rid: int, r: dict) -> bool:
        """投稿の操作の後に止まり、投稿されたか分からない（再実行のスタンプを受け付けていない）"""
        return r["state"] == ResState.REVIEW_ERROR and self.db.kv_get(f"review_error_ts:{rid}") == "-"

    async def _note_unchanged(self, r: dict, stars: int | None, comment: str | None, kept: str) -> None:
        self._review_notes += 1
        await self.post(text=f"ℹ️ 返信に{self._review_what(stars, comment)}とありますが、ゲストに送る文面かもしれないため、"
                             f"レビューは{kept}にしています。変える場合は「★3 コメント「…」」の形だけで返信してください。",
                        thread_ts=r["slack_ts"])

    def _shown_review(self, rid: int, state: ResState) -> dict:
        """今出ている確認（REVIEW_CONFIRM）・失敗（REVIEW_ERROR）のメッセージに載せた評価・コメント"""
        key = {ResState.REVIEW_CONFIRM: "review_confirm_ts", ResState.REVIEW_ERROR: "review_error_ts"}.get(state)
        # （確認を出し直している最中は "-"。読めないので、他の記録から引き継ぐ）
        ts = self.db.kv_get(f"{key}:{rid}") if key else None
        if state is ResState.REVIEW_CONFIRM and ts and ts.startswith("-"):
            pending = json.loads(self.db.kv_get(f"review_confirm_pending:{rid}") or "{}")
            return {"stars": pending.get("stars"), "comment": pending.get("comment")} if pending else {}
        if state is ResState.REVIEW_ERROR and ts == "x":
            saved = json.loads(self.db.kv_get(f"repost_review_error:{rid}") or "{}")
            return dict(saved.get("extra") or {})
        row = self.db.slack_msg(ts) if ts and not ts.startswith("-") and ts != "x" else None
        return json.loads(row["extra"] or "{}") if row else {}

    @staticmethod
    def _parse_stars(norm: str, raw: str) -> tuple[int | None, bool]:
        """返信から評価（1〜5）を読む。戻り値は (評価, 違う評価が複数書かれているか)。
        「★3」「星3」「3つ星」「評価3」「★★★」「★★★☆☆」、絵文字だけの返信（:star::star::star:）を読む"""
        vals = {int(next(g for g in mm.groups() if g)) for mm in STARS.finditer(norm)}
        mm = STAR_RUN.match(norm) if not vals else None  # 先頭の「★★★」「★★★☆☆で」（★★ご案内★★ の飾りは読まない）
        if mm and (len(mm.group(1)) >= 2 or len(mm.group(1)) + len(mm.group(2)) == 5):
            vals = {len(mm.group(1))}
        if not vals:
            n = len(STAR_EMOJI.findall(raw or ""))
            if 2 <= n <= 5 and not re.sub(r":[a-z0-9_+'\-]+:", "", raw or "").strip():
                vals = {n}
        if len(vals) > 1:
            return None, True
        return (next(iter(vals)) if vals else None), False

    @staticmethod
    def _review_what(stars: int | None, comment: str | None) -> str:
        return (f"★{stars}・" if stars else "") + (f"コメント「{B.esc(comment)}」" if comment else "お礼のコメントは自動で作成")

    def _set_review_pref(self, rid: int, pref: dict) -> None:
        """利用前・利用中・投稿中の指示を記録する。以前に承認された内容（review_args.approved）より新しい指示を優先する
        （自動では投稿しない）。評価の指定が無い「待って」は、承認済みの評価・コメントを確認メッセージに引き継ぐ"""
        old = json.loads(self.db.kv_get(f"review_pref:{rid}") or "{}")
        if old.get("head") and "head" not in pref and not pref.get("decline"):
            pref = {**pref, "head": old["head"]}  # 前に出せなかった確認の見出し（退室時状態の警告など）は残す
        args = json.loads(self.db.kv_get(f"review_args:{rid}") or "{}")
        if args.get("approved"):
            if pref.get("hold") and not pref.get("stars") and args.get("stars"):
                pref = {**pref, "stars": int(args["stars"])}
                if pref.get("comment") is None and not args.get("auto"):
                    pref["comment"] = args.get("comment")
            args["approved"] = False
            self.db.kv_set(f"review_args:{rid}", json.dumps(args, ensure_ascii=False))
        self.db.kv_set(f"review_pref:{rid}", json.dumps(pref, ensure_ascii=False))

    async def _hold_review(self, rid: int, r: dict, user: str, why: str, unreadable: bool = False,
                           stars: int | None = None, comment: str | None = None, maybe_decline: bool = False,
                           ack: bool = False, tentative: bool = False) -> None:
        """レビューの自動投稿を止める（安全側。取り消せる操作）。
        stars/comment: 返信から読み取れた評価・コメント（はっきりした指示ではないので、確認メッセージに載せて確かめる）
        それまでのはっきりした指示（「レビューしない」、★N）は、評価もコメントも読み取れない返信では上書きしない
        ack: 「まだ」「一旦」などの保留の指示（確認待ちなら、投稿していないことを返事する）
        tentative: はっきりしない返信（ゲスト宛ての文面かもしれない）。読んだ評価は、それまでの指示・確認を置き換えない"""
        hint = ("「★3 コメント「…」」のように返信するか、確認のメッセージにスタンプしてください"
                "（「レビューしない」で投稿せずに終了）。" if unreadable else "")
        if maybe_decline:
            hint += "投稿しない場合は「レビューしない」とだけ返信してください。"
        given = stars is not None or comment is not None
        state = ResState(r["state"])
        self._review_notes += 1  # レビューの扱いとして処理した（同じ返信に「評価が読み取れませんでした」を重ねない）
        if state in (ResState.REVIEW_CONFIRM, ResState.REVIEW_ERROR):
            # すでに承認者の確認待ち（自動では投稿しない）
            if given and tentative and self._shown_review(rid, state).get("stars"):
                await self._note_unchanged(r, stars, comment, "今の確認メッセージの内容のまま")
            elif given:
                await self._ask_review_confirm(rid, r, f"📝 返信から{self._review_what(stars, comment)}を読み取りました（<@{user}>）。"
                                                       "この内容でよければ、このメッセージにスタンプしてください。\n", stars, comment)
            elif (unreadable or maybe_decline or ack) and self._review_unknown(rid, r):
                await self.post(text=UNKNOWN_NOTE, thread_ts=r["slack_ts"])
            elif unreadable or maybe_decline:
                await self.post(text=f"ℹ️ {why}ため、レビューは変えていません（自動では投稿しません）。{hint}", thread_ts=r["slack_ts"])
            elif ack:
                await self.post(text=f"⏸ レビューは投稿していません。確認のメッセージにスタンプを押すまで投稿しません（<@{user}>）。",
                                thread_ts=r["slack_ts"])
            return
        cur = json.loads(self.db.kv_get(f"review_pref:{rid}") or "{}")
        args = json.loads(self.db.kv_get(f"review_args:{rid}") or "{}")
        approved = bool(args.get("approved") and args.get("stars"))
        keep = (not given or tentative) and bool(cur.get("decline") or cur.get("stars") or (tentative and approved))
        if keep and tentative:
            if approved and not cur:
                self._set_review_pref(rid, {"hold": True})  # 承認済みの評価・コメントを引き継いで、確認を求める（先に記録）
            if given:
                before = "「レビューしない」" if cur.get("decline") else f"★{cur.get('stars') or args.get('stars')}"
                await self._note_unchanged(r, stars, comment, f"以前の指示（{before}）のまま")
            stars = comment = None  # それまでのはっきりした指示・承認済みの内容（の評価・コメント）のまま確認する
            given = False
        if not keep:
            new = {"hold": True}
            if given:
                new.update(stars=stars if stars is not None else cur.get("stars"), comment=comment)
            self._set_review_pref(rid, new)
        if state is ResState.CONFIRMED and self._review_phase(r):
            await self._ask_review_confirm(rid, r, f"⏸ {why}ため、レビューの自動投稿を止めました（<@{user}>）。{hint}\n",
                                           stars, comment, from_states=(ResState.CONFIRMED,))
        elif not keep:
            await self.post(text=f"⏸ {why}ため、利用後のレビューは自動で投稿せず、確認を求めます（<@{user}>）。{hint}",
                            thread_ts=r["slack_ts"])
        elif unreadable or maybe_decline or ack:
            before = "「レビューしない」" if cur.get("decline") else f"★{cur['stars']}"
            await self.post(text=f"ℹ️ 以前の指示（{before}）のままです（<@{user}>）。{hint}", thread_ts=r["slack_ts"])

    async def _review_instruction(self, rid: int, text: str, user: str, raw: str | None = None) -> bool:
        """レビュー・評価・星に触れた返信を扱う。True なら処理済み（通常の返信としては扱わない）。
        - 取り消せない操作（投稿）は、自由文から直接は行わない。★N は確認メッセージ（スタンプで投稿）か、利用後に確認を出す記録
        - はっきりした指示（短く、★/星N/レビュー/評価/コメント「 で始まる、または ★N・「レビューしない」を含む）だけを指示として扱う
          ・「レビューしない」等 → 利用後は投稿せずに終了（期限内なら ★N の返信で再開できる）、利用前は記録
          ・「まだ」「一旦」「後で」等 → 保留（投稿しないと決めたのではない）
          ・★N・コメント「…」→ 確認メッセージ、または利用後に確認を出す記録
          ・違う評価が複数ある、読み取れない → 自動投稿を止めて、書き方を案内する
        - それ以外（ゲスト宛ての文面にレビューの語が入っているだけ等）→ 自動投稿だけ止め、通常の返信として扱う（False）"""
        r = self.res(rid)
        norm = unicodedata.normalize("NFKC", text or "")  # 全角数字（★３）も読む
        raw_norm = unicodedata.normalize("NFKC", raw or "")
        if not REVIEW_RELATED.search(norm + "\n" + raw_norm):
            return False
        state = ResState(r["state"])
        in_phase = self._review_phase(r)
        stars, conflict = self._parse_stars(norm, raw_norm)
        lead = bool(REVIEW_LEAD.match(norm)) or (stars is not None and not norm.strip())  # ★の絵文字だけの返信も指示
        # 「まだ」「一旦」は、レビューに触れた文の中だけを見る（「ちょっと待って。レビューもしないで」の「待って」は予約の保留）
        later = (any(REVIEW_LATER.search(part) for part in re.split(r"[。.!！?？\n]+", norm) if REVIEW_RELATED.search(part))
                 and stars is None and not NOT_DECLINE.search(norm))
        decline = (bool(NO_REVIEW_WORDS.search(norm)) and stars is None and not conflict and not later
                   and not NOT_DECLINE.search(norm))
        # コメントは最初の「から最後の」まで（コメントの中の「」で切れないように）。『』も可
        # コメントは「コメント」か評価の直後の「…」だけ（「返信は「…」で」のような、別の文面の引用は読まない）
        # （元の文字のまま読む。全角の★３や、全角の空白・読点も可）
        comment, span = None, None
        c = COMMENT_AT.search(text or "")
        if c and (stars is not None or re.search(COMMENT_LEAD, norm)):
            body, end = balanced_quote(text, c.end())
            if body is not None:
                comment, span = body.strip(), (c.end(), end)
        rest_text = (text or "")[:span[0]] + (text or "")[span[1]:] if span else (text or "")
        other_quote = bool(re.search(r"[「『]", rest_text))  # コメントのほかに、別の文面の引用がある
        given = stars is not None or comment is not None
        drafts_open = bool([d for d in self.open_drafts(rid) if d["state"] != InqState.APPROVED])
        # ★/評価/コメント で始まる指示のコメント「…」は長さに数えない（コメントは10〜400字まで書ける）
        core = unicodedata.normalize("NFKC", (text or "")[:span[0]] + "「」" + (text or "")[span[1]:]) if (lead and span) else norm
        # はっきりした指示: 短く（ゲスト宛ての長い文面ではなく）、【修正】で始まらず、
        # ★/レビュー/評価 で始まるか、★N・「レビューしない」系の語を含むもの。
        # 文案がある時の敬語の文は修正文とみなす（★/評価/コメント で始まり、評価かコメントを含むものは除く）
        unambiguous = (len(core.strip()) <= INSTRUCTION_MAX and not norm.lstrip().startswith("【")
                       and bool(lead or stars is not None or decline or conflict
                                or (later and NO_REVIEW_WORDS.search(norm)))
                       and not guest_like(norm, core, lead, given, drafts_open)
                       and not (other_quote and given))
        bad = comment is not None and not (SM_REVIEW_MIN <= len(comment) <= SM_REVIEW_MAX)
        bad_msg = (f"⚠️ レビューのコメントは {SM_REVIEW_MIN}〜{SM_REVIEW_MAX} 字にしてください（今は {len(comment)} 字）。"
                   "投稿していません。") if bad else ""

        if state is ResState.CLOSED:
            # 「レビューしない」で閉じた予約: 期限内に ★N（コメント）の返信があれば、確認メッセージで再開する
            if unambiguous and given and not decline:
                if bad:
                    await self.post(text=bad_msg, thread_ts=r["slack_ts"])
                    return True
                await self._ask_review_confirm(rid, r, f"📝 レビューを再開します（<@{user}>）。\n", stars, comment,
                                               from_states=(ResState.CLOSED,))
                return True
            if given:
                await self._note_unchanged(r, stars, comment, "「レビューしない」で終了したまま")
            elif unambiguous or (lead and not guest_like(norm, core, lead, given, drafts_open)):
                self._review_notes += 1
                await self.post(text="ℹ️ この予約は、レビューを投稿せずに対応を終了しています"
                                     + ("（評価が複数書かれていて、どれか分かりませんでした）" if conflict else "")
                                     + "。投稿する場合は、期限内に「★5」のように返信してください。", thread_ts=r["slack_ts"])
                return True
            return False  # ゲスト宛ての文面かもしれない → 通常の返信として扱う（送信の確認を出す）
        if state is ResState.COMPLETING:
            # 投稿の途中。止められない可能性があることを正直に伝え、投稿されなかった場合は指示に従う
            cur = json.loads(self.db.kv_get(f"review_pref:{rid}") or "{}")
            if decline and unambiguous:
                new = {"decline": True}
            elif given and not bad and (unambiguous or not (
                    cur.get("decline") or cur.get("stars")
                    or json.loads(self.db.kv_get(f"review_args:{rid}") or "{}").get("approved"))):
                new = {"hold": True, "stars": stars if stars is not None else cur.get("stars"), "comment": comment}
            elif cur.get("decline") or cur.get("stars"):
                new = None  # それまでのはっきりした指示は、評価もコメントも読み取れない返信では上書きしない
            else:
                new = {"hold": True}
            if new is not None:
                self._set_review_pref(rid, new)
            await self.post(text=f"⚠️ 今まさにレビューを投稿しているところのため、止められない可能性があります（<@{user}>）。"
                                 "結果のメッセージを確認してください。投稿されなかった場合は、この指示に従います。"
                                 + (f"\n{bad_msg}" if bad else ""), thread_ts=r["slack_ts"])
            return unambiguous
        if not unambiguous:
            # ゲスト宛ての文面などにレビューの語が入っているだけかもしれない。自動投稿は止め、通常の返信として扱う
            # （読み取れた評価・コメントは確認メッセージに載せる。「レビューしない」系なら、指示の書き方を案内する）
            await self._hold_review(rid, r, user, "レビューに触れた返信があった", stars=None if bad else stars,
                                    comment=None if bad else comment, maybe_decline=decline or later, tentative=True)
            return False
        if bad:
            await self._hold_review(rid, r, user, "レビューの指示を受け付けられなかった")
            await self.post(text=bad_msg, thread_ts=r["slack_ts"])
            return True
        if conflict:
            await self._hold_review(rid, r, user, "評価が複数書かれていて、どれか分からなかった", unreadable=True)
            return True
        if decline:
            if in_phase or state in (ResState.REVIEW_CONFIRM, ResState.REVIEW_ERROR):
                if self.db.transition("reservations", rid, (ResState.REVIEW_CONFIRM, ResState.REVIEW_ERROR, ResState.CONFIRMED),
                                      ResState.CLOSED):
                    self.db.kv_set(f"review_declined:{rid}", "1")
                    self.db.kv_set(f"review_confirm_pending:{rid}", None)
                    await self.post(text=f"🏁 レビューは投稿せずに対応を終了しました（<@{user}>）。"
                                         "やっぱり投稿する場合は、期限内に「★5」のように返信すれば再開します。", thread_ts=r["slack_ts"])
                    await self.refresh_parent(rid)
            else:
                self._set_review_pref(rid, {"decline": True})
                await self.post(text=f"🛑 利用後のレビューは投稿しません（<@{user}>）。やっぱり投稿する場合は「★5」のように返信してください。",
                                thread_ts=r["slack_ts"])
            return True
        if later and not given:
            await self._hold_review(rid, r, user, "レビューを待つよう指示があった", ack=True)
            return True
        if not given:
            await self._hold_review(rid, r, user, "レビューの指示を読み取れなかった", unreadable=True)
            return True
        if in_phase or state in (ResState.REVIEW_CONFIRM, ResState.REVIEW_ERROR):
            await self._ask_review_confirm(rid, r, f"📝 レビューの内容を受け付けました（<@{user}>）。\n", stars, comment)
            return True
        cur = json.loads(self.db.kv_get(f"review_pref:{rid}") or "{}")
        s2 = stars if stars is not None else cur.get("stars")
        self._set_review_pref(rid, {"hold": True, "stars": s2, "comment": comment})
        await self.post(text=f"📝 利用後に、{self._review_what(s2, comment)}で投稿してよいかの確認を出します"
                             f"（<@{user}>。自動では投稿しません）。", thread_ts=r["slack_ts"])
        return True

    @staticmethod
    async def _await_review(task: asyncio.Future, submitted: dict):
        """管理画面の操作を時間内に終える。投稿の最後のボタンを押した後なら、途中で止めずに終わるまで待つ
        （止めると、投稿されたか分からないまま再実行することになり、二重に投稿しかねない）"""
        try:
            return await asyncio.wait_for(asyncio.shield(task), timeout=REVIEW_TIMEOUT_SEC)
        except TimeoutError:
            if not submitted["sent"]:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
                raise
            log.warning("レビューの投稿操作の後で時間がかかっています。終わるまで待ちます")
            return await asyncio.wait_for(task, timeout=REVIEW_SUBMIT_GRACE_SEC)

    async def post_review(self, rid: int, stars: int, comment: str | None, actor: str, approved: bool = False,
                          from_states: tuple = (ResState.CONFIRMED, ResState.REVIEW_CONFIRM, ResState.REVIEW_ERROR),
                          expect: tuple[str, str] | None = None) -> bool:
        """投稿の処理を始めたら True（成否は Slack に出す）、状態が合わず始めなかったら False。
        approved: 承認者がこの評価・コメントを確認済み（まだ書けずに待つ場合も、その内容で後から投稿する）
        from_states: 自動の経路（利用完了の処理）は CONFIRMED からだけ（承認者が止めた・指示した後に上書きしない）
        expect: (kv の名前, ts) スタンプされたメッセージが、ロックを取った時点でもまだ最新か（新しい確認・失敗に置き換わっていないか）"""
        if comment and not (SM_REVIEW_MIN <= len(comment) <= SM_REVIEW_MAX):
            r = self.res(rid)
            await self.post(text=f"⚠️ レビューのコメントは {SM_REVIEW_MIN}〜{SM_REVIEW_MAX} 字にしてください（今は {len(comment)} 字）。"
                                 "投稿していません。", thread_ts=r["slack_ts"])
            return True
        async with self._locks[rid]:
            r = self.res(rid)
            if expect is not None and self.db.kv_get(f"{expect[0]}:{rid}") not in (None, expect[1]):
                return False  # 待っている間に、新しい確認・失敗のメッセージに置き換わった（古い内容で投稿しない）
            if actor == "system" and tuple(from_states) == (ResState.CONFIRMED,):
                # 利用完了の処理（自動）。待っている間に承認者の指示があった、または承認が取り消された → 次の処理で指示に従う
                if self.db.kv_get(f"review_pref:{rid}"):
                    return False
                if approved and not json.loads(self.db.kv_get(f"review_args:{rid}") or "{}").get("approved"):
                    return False
            if not self.db.transition("reservations", rid, from_states, ResState.COMPLETING):
                return False
            saved = json.loads(self.db.kv_get(f"review_args:{rid}") or "{}")
            self.db.kv_set(f"review_args:{rid}", json.dumps(
                {"stars": stars, "comment": comment or (saved.get("comment") if saved.get("auto") else None),
                 "auto": not comment, "approved": approved}, ensure_ascii=False))
            if approved:
                self.db.kv_set(f"review_pref:{rid}", None)  # 承認者が確認したので、以前の指示は済んだ
            await self.refresh_parent(rid)
            platform = Platform(r["platform"])
            auto = not comment
            if not comment and saved.get("auto") and saved.get("comment"):
                comment = saved["comment"]  # 前回、自動で作ったお礼文（再試行のたびに作り直さない。人が書いたコメントは使い回さない）
            if not comment:
                comment = DEFAULT_THANKS
                if self.llm is not None:
                    try:
                        comment = await self.llm.thanks_comment(r["purpose"]) or DEFAULT_THANKS
                    except Exception as e:  # noqa: BLE001
                        log.warning("お礼文の作成に失敗。定型文を使う: %s", e)
                if not (SM_REVIEW_MIN <= len(comment) <= SM_REVIEW_MAX):
                    comment = DEFAULT_THANKS
            # 途中で止まって回収されたときや再試行でも、同じ評価・コメントで投稿できるように残す
            self.db.kv_set(f"review_args:{rid}", json.dumps(
                {"stars": stars, "comment": comment, "auto": auto, "approved": approved or bool(saved.get("approved") and saved.get("stars") == stars)},
                ensure_ascii=False))
            submitted = {"sent": False}

            def on_submit(sent: bool) -> None:
                submitted["sent"] = sent
                self.db.kv_set(f"review_submitted:{rid}", iso(now()) if sent else None)

            self.db.kv_set(f"review_submitted:{rid}", None)
            token = REVIEW_SUBMIT_HOOK.set(on_submit)
            try:
                task = asyncio.ensure_future(
                    self.adapter(platform).post_review(r["platform_id"], stars, comment, self.cfg.dry_run))
            finally:
                REVIEW_SUBMIT_HOOK.reset(token)
            try:
                result = await self._await_review(task, submitted)
            except NotReady as e:
                # まだレビューを書けない（利用直後など）。CONFIRMED に戻して、利用完了の処理で後でもう一度試す
                # （人が指定した評価・コメントは review_args に残っている）。待ちすぎたら止めて知らせる
                self.db.log("review", reservation_id=rid, actor=actor, result="not_ready", detail=e.message)
                first = from_iso(self.db.kv_get(f"review_wait:{rid}"))
                if first is None:
                    self.db.kv_set(f"review_wait:{rid}", iso(now()))
                pref = json.loads(self.db.kv_get(f"review_pref:{rid}") or "{}")
                if pref.get("decline"):
                    # 書けない（投稿されていない）ので、投稿の途中に受けた「レビューしない」の指示のとおりに終える
                    if self.db.transition("reservations", rid, (ResState.COMPLETING,), ResState.CLOSED):
                        self.db.kv_set(f"review_declined:{rid}", "1")
                        await self.post(text="🏁 「レビューしない」の指示のとおり、レビューは投稿せずに対応を終了しました。",
                                        thread_ts=r["slack_ts"])
                elif first is not None and now() - first > timedelta(days=REVIEW_WAIT_DAYS):
                    self.db.transition("reservations", rid, (ResState.COMPLETING,), ResState.REVIEW_ERROR)
                    await self.review_failed(rid, "レビュー投稿", f"{e.message}。{REVIEW_WAIT_DAYS}日以上この状態のため、自動での投稿をやめました",
                                             None, stars, comment)
                else:
                    self.db.transition("reservations", rid, (ResState.COMPLETING,), ResState.CONFIRMED)
                    if pref:
                        await self.post(text=f"⏳ {e.message}。投稿の途中に受けた指示があるため、書ける状態になったら指示に従います。",
                                        thread_ts=r["slack_ts"])
                    elif actor and actor.startswith("U"):
                        await self.post(text=f"⏳ {e.message}。書ける状態になったら自動で投稿します。", thread_ts=r["slack_ts"])
                await self.refresh_parent(rid)
                return True
            except (TimeoutError, ActionFailed, LoginRequired, Exception) as e:  # noqa: BLE001
                self.db.transition("reservations", rid, (ResState.COMPLETING,), ResState.REVIEW_ERROR)
                self.db.log("review", reservation_id=rid, actor=actor, result="failed", detail=repr(e),
                            screenshots=getattr(e, "screenshots", None))
                msg = "管理画面の操作が時間内に終わりませんでした" if isinstance(e, asyncio.TimeoutError) else getattr(e, "message", repr(e))
                await self.review_failed(rid, getattr(e, "step", "レビュー投稿"), msg, getattr(e, "screenshots", None),
                                         stars, comment)
                await self.refresh_parent(rid)
                return True
            self.db.transition("reservations", rid, (ResState.COMPLETING,), ResState.CLOSED,
                               review_posted_at=iso(now()), review_comment=comment)
            self.db.kv_set(f"review_submitted:{rid}", None)
            self.db.kv_set(f"review_declined:{rid}", None)
            self.db.log("review", reservation_id=rid, actor=actor, result="dry_run" if result.dry_run else "ok",
                        detail=result.note, screenshots=result.screenshots)
            text = B.completion(r, r["exit_status"], None if result.skipped else stars, None if result.skipped else comment)
            if result.skipped:
                text += "\n（レビューは投稿済みでした）"
            if result.dry_run:
                text = f"🧪 (DRY_RUN) 実際には投稿していません。\n{text}"
            await self.post(text=text, thread_ts=r["slack_ts"])
        await self.refresh_parent(rid)
        return True

    # =================================================================
    # ポーリング
    # =================================================================
    @staticmethod
    def touched_rows(rows: list[ListingRow], prev: dict[str, tuple[str, int]]) -> list[ListingRow]:
        """新しい行、表示が変わった行、上に移動した行（＝メッセージ更新の可能性）を返す"""
        touched: dict[str, ListingRow] = {}
        for row in rows:
            if row.platform_id not in prev or prev[row.platform_id][0] != row.fingerprint:
                touched[row.platform_id] = row
        # 前回の順番を保ったまま並んでいる末尾部分より前の行は「上に移動した」
        known = [row for row in rows if row.platform_id in prev]
        last = 10**9
        cut = len(known)
        for i in range(len(known) - 1, -1, -1):
            pos = prev[known[i].platform_id][1]
            if pos < last:
                last = pos
                cut = i
            else:
                break
        for row in known[:cut]:
            touched[row.platform_id] = row
        return [r for r in rows if r.platform_id in touched]

    def _snapshots(self, platform: Platform) -> dict[str, tuple[str, int]]:
        return {r["platform_id"]: (r["fingerprint"], r["position"]) for r in self.db.all(
            "SELECT * FROM listing_snapshots WHERE platform=?", platform.value)}

    async def _row_failed(self, platform: Platform, pid: str, e: Exception) -> None:
        """行ごとの失敗を数え、3回続いたら知らせる（巡回全体は止めないが、黙って見逃さない）"""
        key = f"rowfail:{platform.value}:{pid}"
        n = int(self.db.kv_get(key) or 0) + 1
        self.db.kv_set(key, str(n))
        if n >= 3:
            r = self.db.reservation_by_pid(platform.value, pid)
            url = r["dashboard_url"] if r else self.dashboard_url(platform, pid)
            await self.alert(key, f"{platform.label}の予約 {pid} の画面を{n}回続けて読めません（{type(e).__name__}）。"
                             f"<{url}|管理画面>で直接確認してください（新しい予約やメッセージを見落としている可能性があります）")

    def _row_ok(self, platform: Platform, pid: str) -> None:
        key = f"rowfail:{platform.value}:{pid}"
        if self.db.kv_get(key):
            self.db.kv_set(key, None)

    def _save_snapshots(self, platform: Platform, rows: list[ListingRow], skip: set[str] | None = None) -> None:
        """skip: 処理に失敗した行（前回のスナップショットを残し、次回また処理する）"""
        for i, row in enumerate(rows):
            if skip and row.platform_id in skip:
                continue
            self.db.run("INSERT OR REPLACE INTO listing_snapshots (platform, platform_id, fingerprint, position, seen_at) "
                        "VALUES (?, ?, ?, ?, ?)", platform.value, row.platform_id, row.fingerprint, i, iso(now()))

    async def poll(self) -> None:
        for platform in Platform:
            try:
                if platform is Platform.SM:
                    await self._poll_sm()
                else:
                    await self._poll_ib()
                self.db.kv_set(f"poll_ok:{platform.value}", iso(now()))
                self.db.kv_set(f"poll_fail:{platform.value}", "0")
                await self._notify_poll_recovered(platform)
            except LoginRequired as e:
                await self.alert(f"login:{platform.value}", f"{platform.label}への再ログインが必要です（{e}）。"
                                 "手順書（docs/runbook.md の「ログイン切れ」）に沿ってログインしてください")
            except Exception as e:  # noqa: BLE001
                log.exception("poll %s failed", platform.value)
                fails = int(self.db.kv_get(f"poll_fail:{platform.value}") or 0) + 1
                self.db.kv_set(f"poll_fail:{platform.value}", str(fails))
                if "Executable doesn't exist" in str(e) or "playwright install" in str(e):
                    await self.alert("browser_missing", "管理画面を開くブラウザ（Chromium）が見つかりません。"
                                     "`.venv\\Scripts\\python -m playwright install chromium` を実行してください")
                elif fails >= 3:
                    await self.alert(f"pollfail:{platform.value}", f"{platform.label}の巡回が{fails}回続けて失敗しています"
                                     f"（{type(e).__name__}: {str(e)[:200]}）。画面の仕様変更の可能性があります")

    async def _poll_sm(self) -> None:
        rows = await self.sm.list_inbox()
        if not rows:
            raise RuntimeError("受信トレイの一覧が空です（画面の変更かログイン切れの可能性）")
        prev = self._snapshots(Platform.SM)
        failed: set[str] = set()
        if not prev:
            await self.seed(Platform.SM, rows)
        else:
            touched = self.touched_rows(rows, prev)
            ids = {r.platform_id for r in touched}
            # 上位の行は表示が変わらなくても読み直す（一番上の行に届いた2通目は、並びも表示も変わらないため）
            for row in rows[:SM_TOP_RECHECK]:
                if row.platform_id in ids:
                    continue
                ex = self.db.reservation_by_pid(Platform.SM.value, row.platform_id)
                if ex is None or ex["state"] != ResState.CLOSED or from_iso(ex["updated_at"]) > now() - timedelta(days=3):
                    touched.append(row)
            for row in touched:
                try:
                    await self._handle_sm_row(row)
                    self._row_ok(Platform.SM, row.platform_id)
                except LoginRequired:
                    raise
                except Exception as e:  # noqa: BLE001
                    log.exception("SM %s の処理に失敗", row.platform_id)
                    failed.add(row.platform_id)
                    await self._row_failed(Platform.SM, row.platform_id, e)
        self._save_snapshots(Platform.SM, rows, skip=failed)

    async def _handle_sm_row(self, row: ListingRow) -> None:
        existing = self.db.reservation_by_pid(Platform.SM.value, row.platform_id)
        if existing is None:
            await self.on_new(Platform.SM, row.platform_id, trigger="poll")
            return
        rid = existing["id"]
        async with self._locks[rid]:
            d = await self.sm.get_detail(row.platform_id)
        r = self.res(rid)
        old_end, old_status = r["end_at"], r["platform_status"]
        if d.change_request or (old_end and d.end_at and old_end != d.end_at.isoformat()):
            await self.ensure_parent(rid)
        self.db.update("reservations", rid, **self._detail_cols(Platform.SM, d))
        r = self.res(rid)
        if d.closed_reason and r["state"] != ResState.CLOSED:
            # キャンセルと同時に届いたメッセージも拾ってから閉じる
            await self.process_guest_messages(rid, d.messages, row.platform_id)
            await self.close(rid, d.status)
            return
        # 問い合わせが予約リクエストに切り替わった（ステータスが「お問い合わせ」でなくなった）
        if r["kind"] == Kind.INQUIRY_ONLY and d.kind is Kind.RESERVATION and not d.closed_reason:
            # 切り替わる前に親メッセージに押されていたスタンプを、承認として実行しないように記録しておく
            await self.snapshot_reactions(r["slack_ts"])
            self.db.update("reservations", rid, kind=Kind.RESERVATION.value, state=ResState.NOTIFIED.value,
                           next_remind_at=iso(now() + timedelta(minutes=self.cfg.remind_interval_minutes)))
            self._history(Platform.SM, row.platform_id, d)
            await self.post(text="📅 問い合わせが予約リクエストに切り替わりました。親メッセージにスタンプで承認・定型文の送信をします。",
                            thread_ts=r["slack_ts"], mention=True)
            r = self.res(rid)
        if old_status and d.status and old_status != d.status and r["slack_ts"]:
            if d.status in SM_SEND_OK:
                if self.db.kv_get(f"await_payment:{rid}"):
                    self.db.kv_set(f"await_payment:{rid}", None)
                    await self.post(text="💳 ゲストの支払いが確認され、予約が完了しました。"
                                         "親メッセージにスタンプで、定型文とマニュアルを送ります。",
                                    thread_ts=r["slack_ts"], mention=True)
                else:
                    await self.post(text="💳 ゲストの支払いが確認され、予約が完了しました。", thread_ts=r["slack_ts"])
        if old_end and r["end_at"] and old_end != r["end_at"] and r["slack_ts"]:
            await self.post(text=f"⏱ 利用時間が変わりました：{B.time_band(r)}", thread_ts=r["slack_ts"])
        if d.change_request and r["slack_ts"]:
            await self.alert(f"change:{rid}", f"ゲストから予約変更リクエストが届いています。<{r['dashboard_url']}|管理画面>で"
                             "承諾／拒否してください（自動では回答しません）。", thread_ts=r["slack_ts"])
        if (r["state"] in (ResState.NOTIFIED, ResState.ON_HOLD, ResState.ERROR) and self.template_sent_in(Platform.SM, d)
                and not self.needs_approval(Platform.SM, d)):
            await self._check_platform(rid)
        await self.process_guest_messages(rid, d.messages, row.platform_id)
        if d.exit_status and self.res(rid)["state"] == ResState.CONFIRMED:
            await self.complete(rid, "exit_record")
        elif d.exit_status and self.res(rid)["state"] in (ResState.REVIEW_CONFIRM, ResState.REVIEW_ERROR):
            await self._warn_exit_while_confirming(rid, self.res(rid), d)

    async def _poll_ib(self) -> None:
        rows = await self.ib.list_bookings()
        if not rows:
            raise RuntimeError("予約一覧が空です（画面の変更かログイン切れの可能性）")
        prev = self._snapshots(Platform.IB)
        seeding = not prev
        failed: set[str] = set()
        if seeding:
            await self.seed(Platform.IB, rows)
        else:
            for row in self.touched_rows(rows, prev):
                try:
                    existing = self.db.reservation_by_pid(Platform.IB.value, row.platform_id)
                    if existing is None:
                        await self.on_new(Platform.IB, row.platform_id, trigger="poll")
                    elif existing["state"] != ResState.CLOSED:
                        await self._check_platform(existing["id"])
                    self._row_ok(Platform.IB, row.platform_id)
                except LoginRequired:
                    raise
                except Exception as e:  # noqa: BLE001
                    log.exception("IB %s の処理に失敗", row.platform_id)
                    failed.add(row.platform_id)
                    await self._row_failed(Platform.IB, row.platform_id, e)
        self._save_snapshots(Platform.IB, rows, skip=failed)
        # 新着メッセージ（未返信のみの一覧）
        tids, unread = await self.ib.unreplied_threads()
        last_unread = int(self.db.kv_get("ib_unread") or 0)
        self.db.kv_set("ib_unread", str(unread))
        for tid in tids:
            try:
                await self.on_ib_thread(tid, seeding=seeding)
                self._row_ok(Platform.IB, f"thread-{tid}")
            except LoginRequired:
                raise
            except Exception as e:  # noqa: BLE001
                log.exception("IB スレッド %s の処理に失敗", tid)
                await self._row_failed(Platform.IB, f"thread-{tid}", e)
        if unread > last_unread and not tids:
            log.info("IB 未読が増えたが未返信一覧は空")

    async def on_ib_thread(self, tid: str, seeding: bool = False) -> None:
        msgs, gid, orders = await self.ib.get_thread(tid)
        cands = [dict(r) for r in self.db.all("SELECT * FROM reservations WHERE platform=? AND thread_id=?", "ib", tid)]
        for pid in orders:
            r = self.db.reservation_by_pid("ib", pid)
            if r is None and not seeding:
                await self.on_new(Platform.IB, pid, trigger=f"thread:{tid}")
                r = self.db.reservation_by_pid("ib", pid)
            if r is not None and r["id"] not in {c["id"] for c in cands}:
                cands.append(dict(r))
        real = [c for c in cands if not c["platform_id"].startswith("thread-")]
        pseudo = [c for c in cands if c["platform_id"].startswith("thread-")]
        if real:
            cands = real
        if not cands:
            if seeding:
                self._mark_processed(Platform.IB, tid, msgs)
                return
            # 予約に紐づかない問い合わせ
            pid = f"thread-{tid}"
            ts_now = iso(now())
            rid = self.db.insert("reservations", platform="ib", platform_id=pid, kind=Kind.INQUIRY_ONLY.value,
                                 state=ResState.CONFIRMED.value, thread_id=tid, guest_name=None,
                                 guest_key=f"ib:id:{gid}" if gid else None,
                                 dashboard_url=f"https://www.instabase.jp/partners/inbox/{tid}",
                                 created_at=ts_now, updated_at=ts_now)
            r = self.res(rid)
            msg = B.parent_message(r, "初回")
            ts = await self.post(text=msg["text"], attachments=msg["attachments"], kind=MsgKind.PARENT, rid=rid, mention=True)
            if ts is None:
                self.db.kv_set(f"repost_parent:{rid}", "1")
            self.db.update("reservations", rid, slack_ts=ts)
            cands = [self.res(rid)]
        for c in cands:
            if c["thread_id"] != tid:
                self.db.update("reservations", c["id"], thread_id=tid)
        active = [c for c in cands if c["state"] in ACTIVE_RES_STATES and c["state"] != ResState.CLOSED]
        pool = active or cands
        target = min(pool, key=lambda c: abs((from_iso(c["start_at"]) or now()) - now()))
        if real and pseudo:
            # 予約前の問い合わせから予約に至った。問い合わせ用の仮の記録の未送信の文案は予約に引き継ぎ、仮の記録は閉じる
            for c in pseudo:
                self.db.run(f"UPDATE inquiries SET reservation_id=? WHERE reservation_id=? AND state IN "
                            f"({','.join('?' for _ in UNSENT_INQ_STATES)})", target["id"], c["id"], *UNSENT_INQ_STATES)
                if c["state"] != ResState.CLOSED:
                    await self.close(c["id"], None, note="📎 このゲストの予約が見つかりました。以後のメッセージは予約の通知のスレッドで扱います。")
        if seeding:
            self._mark_processed(Platform.IB, tid, msgs)
            return
        await self.process_guest_messages(target["id"], msgs, tid)

    # =================================================================
    # 定期的な点検
    # =================================================================
    async def tick_repost(self) -> None:
        """Slack への投稿に失敗した親メッセージ・文案・レビューの確認や失敗のメッセージを出し直す。文案の作成中に止まったものは作り直す"""
        try:
            await self.reissue_stuck_confirms(min_age_minutes=10)
        except Exception:  # noqa: BLE001
            log.exception("レビューの確認メッセージの出し直しに失敗")
        for row in self.db.all("SELECT key FROM kv WHERE key LIKE 'exit_warn_pending:%' AND value IS NOT NULL"):
            rid = int(row["key"].split(":", 1)[1])
            try:
                r = self.db.reservation(rid)
                if r and r["state"] in (ResState.REVIEW_CONFIRM.value, ResState.REVIEW_ERROR.value):
                    await self._warn_exit_while_confirming(rid, dict(r))  # 退室時状態の知らせを出し直す（詳細は読み直す）
                else:
                    self.db.kv_set(row["key"], None)
            except Exception:  # noqa: BLE001
                log.exception("退室時状態の知らせの出し直しに失敗 rid=%s", rid)
        for row in self.db.all("SELECT key, value FROM kv WHERE key LIKE 'repost_review_error:%' AND value IS NOT NULL"):
            rid = int(row["key"].split(":", 1)[1])
            try:
                saved = json.loads(row["value"])
                self.db.kv_set(row["key"], None)
                if saved.get("notice"):
                    # 再実行のない知らせ（投稿されたか分からない・「レビューしない」の指示あり）
                    if (self.db.reservation(rid) and self.res(rid)["state"] == ResState.REVIEW_ERROR
                            and self.db.kv_get(f"review_error_ts:{rid}") == "-"):
                        await self.review_notice(rid, saved["step"], saved["message"])
                elif (self.db.reservation(rid) and self.res(rid)["state"] == ResState.REVIEW_ERROR
                        and self.db.kv_get(f"review_error_ts:{rid}") == "x"):
                    await self.post_error(rid, saved["step"], saved["message"], None, retry="review", extra=saved.get("extra"))
            except Exception:  # noqa: BLE001
                log.exception("レビューの失敗メッセージの出し直しに失敗 rid=%s", rid)
        for row in self.db.all("SELECT key FROM kv WHERE key LIKE 'repost_parent:%' AND value IS NOT NULL"):
            rid = int(row["key"].split(":", 1)[1])
            try:
                if self.db.reservation(rid) and await self.ensure_parent(rid, note=None):
                    await self.refresh_parent(rid)
            except Exception:  # noqa: BLE001
                log.exception("親メッセージの出し直しに失敗 rid=%s", rid)
        # 文案の作成（Claude の呼び出し）は最長で数分かかるので、作成中のものは飛ばす。2日より前のものは対象にしない
        cutoff = iso(now() - timedelta(minutes=10))
        oldest = iso(now() - timedelta(days=2))
        qs = ",".join("?" for _ in OPEN_INQ_STATES)
        for q in self.db.all(f"SELECT id, reservation_id FROM inquiries WHERE state IN ({qs}) AND draft_ts IS NULL "
                             "AND source_key NOT LIKE 'manual:%' AND created_at <= ? AND created_at >= ?",
                             *OPEN_INQ_STATES, cutoff, oldest):
            if q["id"] in self._drafting or self._locks[q["reservation_id"]].locked():
                continue
            try:
                await self.produce_draft(q["id"])
            except Exception:  # noqa: BLE001
                log.exception("文案の出し直しに失敗 iid=%s", q["id"])

    async def tick_sweep(self) -> None:
        """一覧の1ページ目から外れた予約のキャンセル・変更を見逃さないよう、進行中の予約を直接読み直す"""
        sm_listed = {r["platform_id"] for r in self.db.all("SELECT platform_id FROM listing_snapshots WHERE platform='sm' "
                                                            "AND seen_at >= ?", iso(now() - timedelta(hours=1)))}
        rows = self.db.all("SELECT * FROM reservations WHERE kind=? AND state IN (?, ?, ?, ?) AND "
                           "(end_at IS NULL OR end_at >= ?)", Kind.RESERVATION.value, ResState.NOTIFIED.value,
                           ResState.ON_HOLD.value, ResState.CONFIRMED.value, ResState.MANUAL.value,
                           iso(now() - timedelta(days=1)))
        for row in rows:
            if row["platform"] == Platform.SM.value and row["platform_id"] in sm_listed:
                continue  # SM の受信トレイは更新順なので、一覧に出ている予約は巡回で拾える
            if self._locks[row["id"]].locked():
                continue
            try:
                await self._check_platform(row["id"])
            except Exception:  # noqa: BLE001
                log.exception("予約の点検に失敗 rid=%s", row["id"])

    async def _notify_poll_recovered(self, platform: Platform) -> None:
        """「巡回が成功していません」を知らせた後に巡回が成功した → 復旧を知らせる（警告だけが残って、止まったままに見えないように）"""
        key = f"poll_stalled:{platform.value}"
        row = self.db.one("SELECT last_sent_at FROM alerts WHERE key=?", key)
        if row is None:
            return
        ts = await self.post(text=f"✅ {platform.label}の巡回が復旧しました（{now():%m/%d %H:%M}）。"
                                  "止まっていた間の新しい予約・メッセージも、巡回で読み直しています。", mention=False)
        if ts:
            self.db.run("DELETE FROM alerts WHERE key=?", key)  # 次に止まったときは、すぐに知らせる

    async def tick_watchdog(self) -> None:
        """巡回が長く成功していなければ知らせる（プロセスは動いているが、管理画面を読めていない状態）"""
        started = from_iso(self.db.kv_get("started_at"))
        if not started or now() - started < timedelta(minutes=25):
            return
        for platform in Platform:
            ok = from_iso(self.db.kv_get(f"poll_ok:{platform.value}"))
            if ok is None or now() - ok > timedelta(minutes=25):
                when = f"{ok:%m/%d %H:%M}" if ok else "起動後一度も"
                await self.alert(f"poll_stalled:{platform.value}", f"{platform.label}の巡回が {when} から成功していません。"
                                 "新しい予約・メッセージを検知できていない可能性があります（ログ: data/logs/barnull.log）")

    # =================================================================
    # 初回取り込み（Slack には要対応の一覧だけを出す）
    # =================================================================
    async def seed(self, platform: Platform, rows: list[ListingRow]) -> None:
        log.info("%s: 初回の取り込み（%d 行）", platform.value, len(rows))
        todo: list[str] = []
        for row in rows:
            if self.db.reservation_by_pid(platform.value, row.platform_id):
                continue
            try:
                d = await self.adapter(platform).get_detail(row.platform_id)
            except Exception as e:  # noqa: BLE001
                log.warning("seed %s %s: %s", platform.value, row.platform_id, e)
                continue
            future = d.end_at is not None and d.end_at > now()
            rid = await self.on_new(platform, row.platform_id, trigger="seed", detail=d, silent=True)
            if rid is None:
                continue
            r = self.res(rid)
            label = f"{platform.label} {row.platform_id}（{d.status or '状態不明'}）"
            url = r["dashboard_url"]
            if r["state"] == ResState.NOTIFIED and future:
                await self.ensure_parent(rid)
                todo.append(f"・<{url}|{label}> 定型文・マニュアルが未送信です（親メッセージを作成しました。スタンプで送信できます）")
            elif not future and d.kind is Kind.RESERVATION and r["state"] != ResState.CLOSED:
                # 導入前に利用が終わった予約は自動でレビューしない
                self.db.update("reservations", rid, state=ResState.CLOSED.value)
                if d.review_posted is False:
                    todo.append(f"・<{url}|{label}> レビューが未投稿です")
            last_host = max((m.index for m in d.messages if m.role == "host"), default=-1)
            if any(m.role == "guest" and m.index > last_host for m in d.messages) and not d.closed_reason:
                todo.append(f"・<{url}|{label}> ゲストのメッセージに未返信の可能性があります")
            if d.change_request:
                todo.append(f"・<{url}|{label}> 予約変更リクエストが回答待ちです")
        if todo:
            await self.post(text=f"📋 {platform.label}：システム導入時点で対応が必要な可能性があるもの（自動では処理しません）\n"
                                 + "\n".join(todo), mention=True)

    # =================================================================
    # Slack の取りこぼしの回収（PC・プロセスが止まっていた間のスタンプ・返信）
    # =================================================================
    def _seen_events(self, since) -> set[str]:
        rows = self.db.all("SELECT trigger, actor FROM action_log WHERE action IN ('reaction', 'reply') AND at >= ?",
                           iso(since))
        return {f"{r['trigger']}|{r['actor']}" for r in rows}

    def _actionable(self, m) -> bool:
        """そのメッセージへのスタンプが、今も意味を持つか（API 呼び出しを減らすための絞り込み）"""
        kind = MsgKind(m["kind"])
        r = self.db.reservation(m["reservation_id"]) if m["reservation_id"] else None
        q = self.db.inquiry(m["inquiry_id"]) if m["inquiry_id"] else None
        if kind in (MsgKind.PARENT, MsgKind.REMINDER):
            return bool(r) and r["state"] in (ResState.NOTIFIED, ResState.ON_HOLD, ResState.ERROR, ResState.MANUAL)
        if kind is MsgKind.DRAFT:
            return bool(q) and q["state"] in OPEN_INQ_STATES
        if kind is MsgKind.ERROR:
            return (bool(q) and q["state"] == InqState.ERROR) or (
                bool(r) and r["state"] in (ResState.ERROR, ResState.REVIEW_ERROR))
        if kind is MsgKind.REVIEW_CONFIRM:
            return bool(r) and r["state"] == ResState.REVIEW_CONFIRM
        if kind is MsgKind.REVISION_CONFIRM:
            if q is not None:
                return q["state"] in UNSENT_INQ_STATES
            return from_iso(m["created_at"]) > now() - timedelta(days=2)
        return False

    async def catch_up(self, days: int = 14) -> int:
        """スタンプと返信を Slack から読み直し、未処理の承認者の操作を実行する。処理した件数を返す。
        停止中に、同じ予約へスタンプと返信（「待って」など）の両方があった場合は、どちらが先か分からないので
        スタンプは実行せずに確認を求める"""
        since = now() - timedelta(days=days)
        seen = self._seen_events(since)
        done = 0

        # 1) スレッドへの返信を集める
        replies: dict[int, list[tuple[str, dict]]] = {}
        parents = self.db.all("SELECT * FROM reservations WHERE slack_ts IS NOT NULL AND (state<>? OR updated_at >= ? OR "
                              "((end_at IS NULL OR end_at >= ?) AND id IN (SELECT CAST(substr(key, 17) AS INTEGER) FROM kv "
                              "WHERE key LIKE 'review_declined:%' AND value IS NOT NULL)))",
                              ResState.CLOSED.value, iso(now() - timedelta(days=2)), iso(since))
        for r in parents:
            try:
                resp = await self.slack.conversations_replies(channel=self.cfg.channel, ts=r["slack_ts"],
                                                              oldest=str(since.timestamp()), limit=200)
            except Exception as e:  # noqa: BLE001
                log.warning("conversations.replies に失敗: %s", e)
                continue
            for msg in (resp.get("messages") or [])[1:]:
                user, ts = msg.get("user"), msg.get("ts")
                if msg.get("bot_id") or msg.get("subtype") not in (None, "thread_broadcast", "file_share"):
                    continue
                if not self.is_approver(user) or f"reply@{ts}|{user}" in seen:
                    continue
                if not (msg.get("text") or "").strip():
                    continue  # ファイルだけの投稿など（操作ではない）
                replies.setdefault(r["id"], []).append((user, msg))

        # 2) スタンプを集める
        stamps: list[tuple[dict, str, str]] = []
        rows = self.db.all("SELECT * FROM slack_messages WHERE channel=? AND created_at >= ? AND kind IN (?, ?, ?, ?, ?, ?)",
                           self.cfg.channel, iso(since), MsgKind.PARENT.value, MsgKind.REMINDER.value,
                           MsgKind.DRAFT.value, MsgKind.ERROR.value, MsgKind.REVIEW_CONFIRM.value,
                           MsgKind.REVISION_CONFIRM.value)
        for m in rows:
            if not self._actionable(m):
                continue
            try:
                resp = await self.slack.reactions_get(channel=self.cfg.channel, timestamp=m["ts"])
            except Exception as e:  # noqa: BLE001
                log.warning("reactions.get に失敗: %s", e)
                continue
            for rx in (resp.get("message") or {}).get("reactions", []):
                for user in rx.get("users", []):
                    if f"reaction:{rx['name']}@{m['ts']}|{user}" in seen or not self.is_approver(user):
                        continue
                    stamps.append((dict(m), user, rx["name"]))

        # 3) 同じ予約にスタンプと返信の両方がある → スタンプは実行しない
        conflict = {m["reservation_id"] for m, _, name in stamps if self.is_approve_reaction(name)} & set(replies)
        for rid in conflict:
            for m, user, name in [s for s in stamps if s[0]["reservation_id"] == rid]:
                self.db.log("reaction", reservation_id=rid, trigger=f"reaction:{name}@{m['ts']}", actor=user,
                            result="skipped_conflict")
            stamps = [s for s in stamps if s[0]["reservation_id"] != rid]
            await self.post(text="⚠️ 停止中に、この予約へのスタンプと返信の両方がありました。どちらが先か分からないため、"
                                 "スタンプは実行していません。返信を処理したあとの状態を確認して、必要ならもう一度スタンプを押してください。",
                            thread_ts=self.res(rid)["slack_ts"], mention=True)

        # 4) 返信 → スタンプの順に処理する
        for rid, items in replies.items():
            r = self.res(rid)
            for user, msg in sorted(items, key=lambda x: float(x[1].get("ts") or 0)):
                try:
                    log.info("取りこぼした返信を処理: %s", msg.get("ts"))
                    await self.on_reply(user, r["slack_ts"], msg.get("text", ""), msg.get("ts"))
                    done += 1
                except Exception:  # noqa: BLE001
                    log.exception("取りこぼした返信の処理に失敗")
        for m, user, name in stamps:
            try:
                fresh = self.db.slack_msg(m["ts"])
                if fresh is None or not self._actionable(fresh):
                    self.db.log("reaction", reservation_id=m["reservation_id"], trigger=f"reaction:{name}@{m['ts']}",
                                actor=user, result="stale")
                    continue
                log.info("取りこぼしたスタンプを処理: %s %s", name, m["ts"])
                await self.on_reaction(user, m["ts"], name)
                done += 1
            except Exception:  # noqa: BLE001
                log.exception("取りこぼしたスタンプの処理に失敗")
        if done:
            await self.post(text=f"🔄 停止中などで受け取れていなかった Slack の操作を {done} 件処理しました。")
        return done
