"""ワークフローの中核。状態遷移・Slack 投稿・外部操作の呼び出しをここに集約する。

外部への書き込み（承認・送信・添付・レビュー）は必ず
  1) DB の比較更新で状態を「実行中」にしてから（同時実行の防止）
  2) 操作の直前に管理画面を読み、実施済みなら飛ばす（各 platform モジュール側）
の順で行う。プロセスが途中で止まって「実行中」のまま残ったものは、起動時に回収して再実行を依頼する。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sqlite3
from collections import defaultdict
from datetime import timedelta
from pathlib import Path
from typing import Any

from slack_sdk.web.async_client import AsyncWebClient

from .. import secrets
from ..config import ROOT, Config
from ..db import DB
from ..llm.client import LLM, LLMRefused, LLMTruncated
from ..models import (
    ACTIVE_RES_STATES,
    OPEN_INQ_STATES,
    UNSENT_INQ_STATES,
    Detail,
    InqState,
    Kind,
    Message,
    MsgKind,
    Platform,
    ResState,
    message_key_variants,
    message_keys,
)
from ..platforms.base import (
    SM_PENDING,
    ActionFailed,
    NotReady,
    SendSkipped,
    is_template_text,
    norm_text,
    same_text,
)
from ..platforms.browser import BrowserPool, LoginRequired
from ..platforms.ib import Instabase
from ..platforms.parse_ib import is_change_request
from ..platforms.sm import SpaceMarket
from ..slack import blocks as B
from ..textutil import from_iso, iso, mask_contacts, normalize_name, now, salutation

log = logging.getLogger(__name__)

PLACEHOLDER_CODE = "［暗証番号］"
SM_REVIEW_MIN, SM_REVIEW_MAX = 10, 400
DEFAULT_THANKS = "この度はBar nullをご利用いただき、誠にありがとうございました！またのご利用を心よりお待ちしております。"
EMERGENCY_PHONE = "09098670069"   # マニュアルの緊急連絡先（文案に書いてよい唯一の電話番号）
TEMPLATE_TIMEOUT_SEC = 600        # 承認＋定型文＋PDF の操作全体の制限時間（固まってロックを握り続けないように）
SEND_TIMEOUT_SEC = 300
REVIEW_TIMEOUT_SEC = 300
LEGACY_DEFER_MAX = 6              # 古い形式の記録と照合できない SM の発言を、新着扱いにせず待つ巡回の回数

_EXTERNAL = re.compile(r"(LINE|ライン|PayPay|ペイペイ|口座|振込|振り込み|[\w.+-]+@[\w-]+\.[\w.-]+)", re.I)
_PHONE_LIKE = re.compile(r"(?<!\d)0\d{1,4}-?\d{1,4}-?\d{3,4}(?!\d)")


class Engine:
    def __init__(self, cfg: Config, db: DB, slack: AsyncWebClient, pool: BrowserPool, llm: LLM | None):
        self.cfg = cfg
        self.db = db
        self.slack = slack
        self.pool = pool
        self._llm = llm
        self.sm = SpaceMarket(cfg, pool)
        self.ib = Instabase(cfg, pool)
        self.bot_user_id: str | None = None
        self.mock = None  # 模擬モードでは MockStore
        self._locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._drafting: set[int] = set()  # 文案を作成中の問い合わせ（出し直しの処理と重ならないように）

    # =================================================================
    # 共通
    # =================================================================
    @property
    def llm(self) -> LLM | None:
        """Claude API。キーが後から登録された場合もここで使えるようになる"""
        if self._llm is None and secrets.get_optional("ANTHROPIC_API_KEY"):
            try:
                self._llm = LLM(self.cfg)
                log.info("Claude API キーを検出しました。文案・分類を有効にします")
            except Exception as e:  # noqa: BLE001
                log.warning("Claude API の初期化に失敗: %s", e)
        return self._llm

    def adapter(self, platform: Platform | str):
        return self.sm if Platform(platform) is Platform.SM else self.ib

    def _view(self, r: dict) -> dict:
        """Slack 表示用に、DB の列以外の情報（承認制リクエストとして届いたか）を足す"""
        v = dict(r)
        v["was_request"] = bool(self.db.kv_get(f"ib_request:{r['id']}"))
        return v

    def res(self, rid: int) -> dict:
        row = self.db.reservation(rid)
        if row is None:
            raise KeyError(rid)
        return dict(row)

    def dashboard_url(self, platform: Platform, pid: str) -> str:
        return self.sm.thread_url(pid) if platform is Platform.SM else self.ib.booking_url(pid)

    def is_approver(self, user: str | None) -> bool:
        return bool(user) and user in self.cfg.approvers and user != self.bot_user_id

    async def post(self, *, text: str, thread_ts: str | None = None, blocks: list | None = None,
                   attachments: list | None = None, kind: MsgKind = MsgKind.INFO, rid: int | None = None,
                   iid: int | None = None, extra: dict | None = None, mention: bool = False) -> str | None:
        if mention:
            text = f"{B.mention(self.cfg.approvers)}\n{text}"
            if blocks:
                blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": B.mention(self.cfg.approvers)}}, *blocks]
        # 本文だけでなく blocks・attachments の中も伏せる（ゲストが書いた暗証番号などが Slack に残らないように）
        text, blocks, attachments = secrets.redact_obj([text, blocks, attachments])
        try:
            resp = await self.slack.chat_postMessage(
                channel=self.cfg.channel, text=text, thread_ts=thread_ts,
                blocks=blocks, attachments=attachments, unfurl_links=False, unfurl_media=False,
            )
        except Exception as e:  # noqa: BLE001
            log.error("Slack 投稿に失敗: %s", e)
            return None
        ts = resp["ts"]
        if kind is not MsgKind.INFO or rid or iid:
            self.db.save_slack(ts, self.cfg.channel, kind, rid, iid, extra)
        return ts

    async def react(self, ts: str, name: str) -> None:
        try:
            await self.slack.reactions_add(channel=self.cfg.channel, timestamp=ts, name=name)
        except Exception as e:  # noqa: BLE001
            log.debug("リアクションを付けられません: %s", e)

    async def upload(self, path: str, thread_ts: str | None, title: str) -> None:
        try:
            await self.slack.files_upload_v2(channel=self.cfg.channel, thread_ts=thread_ts, file=path, title=title)
        except Exception as e:  # noqa: BLE001
            log.warning("スクリーンショットのアップロードに失敗: %s", e)

    async def alert(self, key: str, text: str, thread_ts: str | None = None, mention: bool = True) -> None:
        """同じ警告は alert_dedup_minutes に1回まで（投稿に成功したときだけ記録する）"""
        row = self.db.one("SELECT last_sent_at FROM alerts WHERE key=?", key)
        if row and now() - from_iso(row["last_sent_at"]) < timedelta(minutes=self.cfg.alert_dedup_minutes):
            return
        ts = await self.post(text=f"⚠️ {text}", thread_ts=thread_ts, mention=mention)
        if ts:
            self.db.run("INSERT OR REPLACE INTO alerts (key, last_sent_at) VALUES (?, ?)", key, iso(now()))

    def open_drafts(self, rid: int) -> list[dict]:
        qs = ",".join("?" for _ in UNSENT_INQ_STATES)
        return [dict(r) for r in self.db.all(
            f"SELECT * FROM inquiries WHERE reservation_id=? AND state IN ({qs}) ORDER BY id", rid, *UNSENT_INQ_STATES)]

    def repeat_label(self, r: dict) -> str:
        if not r["guest_key"]:
            return "初回"
        n = self.db.one(
            "SELECT COUNT(*) AS n FROM guest_history WHERE platform=? AND guest_key=? AND platform_id<>? AND cancelled=0 "
            "AND (start_at IS NULL OR ? IS NULL OR start_at < ?)",
            r["platform"], r["guest_key"], r["platform_id"], r["start_at"], r["start_at"],
        )["n"]
        return "初回" if n == 0 else f"{n + 1}回目"

    async def refresh_parent(self, rid: int) -> None:
        r = self.res(rid)
        if not r["slack_ts"]:
            return
        closed_label = None
        if r["state"] == ResState.CLOSED:
            st = r["platform_status"] or ""
            closed_label = f"✖️ {st}" if any(w in st for w in ("キャンセル", "否認", "期限切れ", "非承認")) else "🏁 対応完了"
        drafts = self.open_drafts(rid)
        pending = [d for d in drafts if d["state"] != InqState.APPROVED]
        msg = B.parent_message(self._view(r), self.repeat_label(r), len(pending), closed_label,
                               needs_check=sum(1 for d in pending if d["needs_check"]),
                               approved_waiting=len(drafts) - len(pending))
        if r["kind"] == Kind.INQUIRY_ONLY and r["state"] != ResState.CLOSED:
            n = len(pending)
            msg["attachments"][0]["blocks"][3] = {"type": "section", "text": {"type": "mrkdwn", "text":
                f"*ステータス：{'💬 返信待ちあり（' + str(n) + '件）' if n else '✅ 返信済み'}*"}}
        text, attachments = secrets.redact_obj([msg["text"], msg["attachments"]])
        try:
            await self.slack.chat_update(channel=self.cfg.channel, ts=r["slack_ts"], text=text, attachments=attachments)
        except Exception as e:  # noqa: BLE001
            log.warning("親メッセージの更新に失敗: %s", e)

    async def ensure_parent(self, rid: int, note: str | None = "（システム導入前からの予約です）") -> str | None:
        """親メッセージが無ければ作る（導入時に取り込んだ予約で、後から通知が必要になった場合）。
        新規予約の通知の投稿に失敗していた予約は、注記なし・メンション付きで出し直す"""
        r = self.res(rid)
        if r["slack_ts"]:
            return r["slack_ts"]
        repost = bool(self.db.kv_get(f"repost_parent:{rid}"))
        if repost:
            note = None
        msg = B.parent_message(self._view(r), self.repeat_label(r), len(self.open_drafts(rid)))
        ts = await self.post(text=msg["text"], attachments=msg["attachments"], kind=MsgKind.PARENT, rid=rid,
                             mention=repost)
        if ts is None:
            self.db.kv_set(f"repost_parent:{rid}", "1")  # 投稿に失敗した。あとで投稿し直す
            return None
        self.db.update("reservations", rid, slack_ts=ts)
        self.db.kv_set(f"repost_parent:{rid}", None)
        if note:
            await self.post(text=note, thread_ts=ts)
        return ts

    async def post_error(self, rid: int, step: str, message: str, screenshots: list[str] | None,
                         retry: str | None, iid: int | None = None, extra: dict | None = None) -> str | None:
        r = self.res(rid)
        shots = [s for s in (screenshots or []) if s]
        lines = [f"⚠️ 自動処理に失敗しました：{step}", f"内容：{secrets.redact(message)[:1000]}"]
        if retry:
            lines.append("🔁 このメッセージにスタンプ → 管理画面の状態を確認してから再実行します（送信済みの部分は飛ばします）")
        if shots and not self.cfg.upload_screenshots:
            # 画面には暗証番号やゲストの電話番号が写るので、Slack には上げず PC に残す
            lines.append(f"🖼 失敗時の画面は PC に保存しました：{Path(shots[-1]).name}")
        if retry == "review":
            self.db.kv_set(f"review_error_ts:{rid}", "x")  # 投稿できるまで、前の失敗メッセージからの再実行は受け付けない
            self.db.kv_set(f"repost_review_error:{rid}", None)  # 出し直し待ちの古い失敗は、この新しい失敗に置き換わる
        ts = await self.post(text="\n".join(lines), thread_ts=r["slack_ts"], kind=MsgKind.ERROR if retry else MsgKind.INFO,
                             rid=rid, iid=iid, extra={"retry": retry, **(extra or {})}, mention=True)
        if retry == "review":
            if ts:
                self.db.kv_set(f"review_error_ts:{rid}", ts)  # レビューの再実行は最新の失敗メッセージだけ
            else:
                # Slack に出せなかった → 後で出し直す（REVIEW_ERROR のまま、見えない状態にしない）
                self.db.kv_set(f"repost_review_error:{rid}", json.dumps(
                    {"step": step, "message": message, "extra": extra}, ensure_ascii=False))
        if self.cfg.upload_screenshots:
            for p in shots[-1:]:
                await self.upload(p, ts and r["slack_ts"], "失敗時の画面")
        return ts

    async def review_notice(self, rid: int, step: str, message: str, screenshots: list[str] | None = None) -> None:
        """再実行のないレビューの失敗の知らせ。Slack に出せなければ、後で出し直す（REVIEW_ERROR を見えない状態にしない）"""
        ts = await self.post_error(rid, step, message, screenshots, retry=None)
        if not ts:
            self.db.kv_set(f"repost_review_error:{rid}", json.dumps(
                {"step": step, "message": message, "notice": True}, ensure_ascii=False))

    async def review_failed(self, rid: int, step: str, message: str, screenshots: list[str] | None,
                            stars: int | None, comment: str | None, shown: str | None = None) -> None:
        """レビュー投稿の失敗を知らせる（状態は REVIEW_ERROR にしてから呼ぶ）。再実行で投稿する内容を必ず示す。
        投稿の途中に受けた指示（review_pref）があれば、前の内容での再実行は出さず、指示に合わせる。
        shown: 表示するコメント（自動で作ったお礼文。再実行では作り直さずに使う）"""
        self.db.kv_set(f"repost_review_error:{rid}", None)  # 出し直し待ちの古い失敗は、この新しい失敗に置き換わる
        pref = json.loads(self.db.kv_get(f"review_pref:{rid}") or "{}")
        unknown = bool(self.db.kv_get(f"review_submitted:{rid}"))  # 投稿の最後のボタンを押した後に止まった
        if unknown or pref.get("decline"):
            self.db.kv_set(f"review_error_ts:{rid}", "-")  # 以前の失敗メッセージからの再実行も受け付けない
            if unknown:
                tail = ("。投稿の操作の後に止まったため、投稿されたか分かりません（二重に投稿しないよう、再実行のスタンプは受け付けません）。"
                        "管理画面で確認し、投稿されていなければ、このスレッドに"
                        + ("「レビューしない」と返信してください（投稿せずに終了します）" if pref.get("decline") else
                           "「★5」のように返信してください（確認のメッセージが出ます。投稿済みと分かれば投稿しません）"))
            else:
                tail = ("。投稿の途中に「レビューしない」の指示を受けています。管理画面で投稿されていないことを確認し、"
                        "このスレッドに「レビューしない」と返信してください（投稿せずに終了します）")
            await self.review_notice(rid, step, message + tail, screenshots)
            return
        if pref.get("stars") or pref.get("comment"):
            stars, comment, shown = int(pref.get("stars") or stars or self.cfg.review_stars), pref.get("comment"), None
            message += "。再実行では、投稿の途中に受けた指示の内容で投稿します"
        elif pref.get("hold"):
            message += "。投稿の途中に、レビューを待つよう指示を受けています。内容を確かめてから再実行してください"
        stars = int(stars or self.cfg.review_stars)
        what = f"★{stars}「{shown or comment or 'お礼のコメントは自動で作成'}」"
        await self.post_error(rid, step, f"{message}（再実行で投稿する内容: {what}）", screenshots, retry="review",
                              extra={"stars": stars, "comment": comment})

    @staticmethod
    def _code_pattern(code: str) -> re.Pattern:
        """暗証番号の書き方の揺れ（全角数字、数字の間の空白）も含めて見つける"""
        fw = str.maketrans("0123456789", "０１２３４５６７８９")
        digits = r"[\s　]?".join(f"[{c}{c.translate(fw)}]" for c in code)
        return re.compile(rf"(?<![0-9０-９]){digits}(?![0-9０-９])")

    def mask_code(self, text: str) -> str:
        code = secrets.get_optional("KEYBOX_CODE")
        if not code or not text:
            return text
        return self._code_pattern(code).sub(PLACEHOLDER_CODE, text)

    def has_code(self, text: str) -> bool:
        """文面に暗証番号（伏せ字か、番号そのもの）が入っているか"""
        if PLACEHOLDER_CODE in (text or ""):
            return True
        code = secrets.get_optional("KEYBOX_CODE")
        return bool(code and text and self._code_pattern(code).search(text))

    def code(self) -> str:
        if self.mock is not None:
            return secrets.get_optional("KEYBOX_CODE") or "0000"
        return secrets.get("KEYBOX_CODE")

    def code_allowed(self, r: dict) -> bool:
        """返信に暗証番号を入れてよい予約か（定型文の送信後で、利用が終わっておらず、キャンセル等でない）"""
        end = from_iso(r["cleanup_end_at"]) or from_iso(r["end_at"])
        return (r["kind"] == Kind.RESERVATION and bool(r["template_sent_at"]) and r["state"] == ResState.CONFIRMED
                and (end is None or end > now()))

    def outgoing_problems(self, text: str, r: dict, human: bool = False, check_code: bool = True) -> list[str]:
        """ゲストに送る前に、文面に残っていてはいけないものを調べる（LLM の判定に頼らない最後の確認）。
        human=True（承認者が書いた修正文・スタンプで承認済みの文案）は、要確認の印と伏せ字の残りだけを見る"""
        out = []
        if re.search(r"[【\[［]\s*要確認", text or ""):
            out.append("【要確認】の印が残っています")
        for ph in ("［メールアドレス］", "［電話番号］"):
            if ph in (text or ""):
                out.append(f"伏せ字「{ph}」が残っています")
        if check_code and self.has_code(text) and not self.code_allowed(r):
            out.append("暗証番号は、定型文の送信後で利用前の予約にしか送れません")
        if not human:
            phones = [p for p in _PHONE_LIKE.findall(norm_text(text)) if p.replace("-", "") != EMERGENCY_PHONE]
            if phones:
                out.append("緊急連絡先以外の電話番号が書かれています")
            if _EXTERNAL.search(text or ""):
                out.append("予約サイトの外での連絡・支払いに触れています（LINE・メール・振込など）")
        return out

    # =================================================================
    # 予約の登録
    # =================================================================
    def guest_key(self, platform: Platform, d: Detail) -> str | None:
        if platform is Platform.IB and d.guest_id:
            return f"ib:id:{d.guest_id}"
        if d.guest_name:
            return f"{platform.value}:name:{normalize_name(d.guest_name)}"
        return None

    def _detail_cols(self, platform: Platform, d: Detail) -> dict[str, Any]:
        share = self.cfg.sm_host_share if platform is Platform.SM else self.cfg.ib_host_share
        revenue, estimate = d.host_revenue, 0
        if revenue is None and d.guest_total is not None:
            revenue, estimate = int(round(d.guest_total * share)), 1
        cols = dict(
            guest_name=d.guest_name, guest_key=self.guest_key(platform, d),
            start_at=iso(d.start_at), end_at=iso(d.end_at), prep_start_at=iso(d.prep_start_at),
            cleanup_end_at=iso(d.cleanup_end_at), people=d.people, purpose=d.purpose,
            purpose_detail=d.purpose_detail, options=d.options, plan=d.plan, guest_total=d.guest_total,
            host_revenue=revenue, revenue_is_estimate=estimate, platform_status=d.status,
        )
        if d.thread_id:
            cols["thread_id"] = d.thread_id
        if d.exit_status:
            cols["exit_status"] = d.exit_status
            cols["exit_detail"] = d.exit_detail
        # 取れなかった値で上書きしない
        return {k: v for k, v in cols.items() if v is not None}

    def _history(self, platform: Platform, pid: str, d: Detail, cancelled: bool = False) -> None:
        key = self.guest_key(platform, d)
        if not key:
            return
        self.db.run(
            "INSERT INTO guest_history (platform, platform_id, guest_key, start_at, cancelled) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(platform, platform_id) DO UPDATE SET guest_key=excluded.guest_key, start_at=excluded.start_at, "
            "cancelled=excluded.cancelled",
            platform.value, pid, key, iso(d.start_at), int(cancelled),
        )

    def template_sent_in(self, platform: Platform, d: Detail) -> bool:
        if self.cfg.dry_run:
            r = self.db.reservation_by_pid(platform.value, d.platform_id)
            if r is not None and self.db.kv_get(f"sim_manual_sent:{r['id']}"):
                return True
        code = secrets.get_optional("KEYBOX_CODE")
        if platform is Platform.SM:
            return self.sm.template_sent(d, self.cfg.sent_marker, code)
        since = Instabase._index_after_booking(d.messages, d.platform_id, d.applied_at)
        return Instabase.template_sent(d.messages, self.cfg.sent_marker, since, code)

    @staticmethod
    def needs_approval(platform: Platform, d: Detail) -> bool:
        """承認待ちのリクエスト（定型文が送られていても、承認しないと期限切れになる）"""
        if platform is Platform.SM:
            return d.can_approve or (d.status or "").strip() in SM_PENDING
        return "リクエスト" in (d.status or "")

    async def on_new(self, platform: Platform, pid: str, trigger: str = "poll", detail: Detail | None = None,
                     silent: bool = False) -> int | None:
        """新しい予約・問い合わせを登録して通知する（F1）。silent=True は取り込みのみ（Slack に出さない）"""
        if self.db.reservation_by_pid(platform.value, pid):
            return None
        d = detail or await self.adapter(platform).get_detail(pid)
        kind = d.kind
        past = kind is Kind.RESERVATION and d.end_at is not None and d.end_at <= now()
        if d.closed_reason or past:
            # 利用時間が過ぎた予約は「新しい予約」ではない（一覧の表示範囲が変わって現れた過去の予約など）。記録だけする
            state = ResState.CLOSED
        elif kind is Kind.INQUIRY_ONLY:
            state = ResState.CONFIRMED
        elif self.template_sent_in(platform, d) and not self.needs_approval(platform, d):
            state = ResState.CONFIRMED
        else:
            state = ResState.NOTIFIED
        ts_now = iso(now())
        try:
            rid = self.db.insert(
                "reservations", platform=platform.value, platform_id=pid, kind=kind.value, state=state.value,
                dashboard_url=self.dashboard_url(platform, pid), created_at=ts_now, updated_at=ts_now,
                next_remind_at=iso(now() + timedelta(minutes=self.cfg.remind_interval_minutes)),
                template_sent_at=ts_now if (state is ResState.CONFIRMED and kind is Kind.RESERVATION) else None,
                **self._detail_cols(platform, d),
            )
        except sqlite3.IntegrityError:
            return None  # 同時に別の経路で登録された
        if kind is Kind.RESERVATION:
            self._history(platform, pid, d, cancelled=state is ResState.CLOSED)
        self.db.log("register", reservation_id=rid, trigger=trigger, result=state.value)
        if platform is Platform.IB and "リクエスト" in (d.status or ""):
            self.db.kv_set(f"ib_request:{rid}", "1")  # 承認制リクエストとして届いた（見出しと案内に使う）
        thread_key = d.thread_id if platform is Platform.IB else pid
        if silent:
            if thread_key:
                self._mark_processed(platform, thread_key, d.messages)
            return rid
        if state is ResState.CLOSED:
            if not trigger.startswith("thread:"):
                # スレッドから見つけた過去の予約は登録だけする（新着は、そのスレッドの対象の予約で処理する）
                await self._closed_thread_messages(rid, d, thread_key, trigger)
            return rid

        r = self.res(rid)
        msg = B.parent_message(self._view(r), self.repeat_label(r))
        ts = await self.post(text=msg["text"], attachments=msg["attachments"], kind=MsgKind.PARENT, rid=rid, mention=True)
        if ts is None:
            self.db.kv_set(f"repost_parent:{rid}", "1")
        self.db.update("reservations", rid, slack_ts=ts)
        await self._link_prior_inquiry(rid)
        if thread_key:
            await self.process_guest_messages(rid, d.messages, thread_key, initial=kind is Kind.RESERVATION)
        await self.refresh_parent(rid)
        return rid

    async def _closed_thread_messages(self, rid: int, d: Detail, thread_key: str | None, trigger: str) -> None:
        """過去・キャンセル済みとして登録した予約のスレッドに、最近（7日以内）ゲストの発言があれば知らせる。
        それより古い発言は処理済みにする（一覧の表示範囲が変わって昔の予約が現れたときに騒がない）"""
        if not thread_key:
            return
        if d.platform is Platform.IB and self.db.one(
                "SELECT id FROM reservations WHERE platform='ib' AND thread_id=? AND state<>? AND id<>?",
                thread_key, ResState.CLOSED.value, rid):
            return  # 同じスレッドに進行中の予約がある（新着はそちらで処理する）
        recent_from = now() - timedelta(days=7)
        recent = {m.index for m in d.messages if m.role == "guest" and m.at and m.at >= recent_from}
        self._mark_processed(d.platform, thread_key, d.messages, only={m.index for m in d.messages} - recent)
        if not recent or trigger == "seed":
            self._mark_processed(d.platform, thread_key, d.messages)
            return
        await self.ensure_parent(rid, note="ℹ️ 利用済み・キャンセル済みの予約ですが、ゲストから最近のメッセージがあります。")
        await self.process_guest_messages(rid, d.messages, thread_key)

    async def _link_prior_inquiry(self, rid: int) -> None:
        """同じゲストの最近の問い合わせがあれば、相互にリンクを貼る"""
        r = self.res(rid)
        if r["kind"] != Kind.RESERVATION or not r["guest_key"] or not r["slack_ts"]:
            return
        prior = self.db.one(
            "SELECT * FROM reservations WHERE kind=? AND guest_key=? AND id<>? AND slack_ts IS NOT NULL "
            "AND created_at >= ? ORDER BY id DESC",
            Kind.INQUIRY_ONLY.value, r["guest_key"], rid, iso(now() - timedelta(days=30)),
        )
        if not prior:
            return
        await self.post(text=f"📎 このゲストの問い合わせ（{prior['platform_id']}）から予約に至りました。", thread_ts=prior["slack_ts"])
        await self.post(text=f"📎 このゲストは事前に問い合わせをしています（問い合わせ {prior['platform_id']} のスレッド参照）。",
                        thread_ts=r["slack_ts"])

    # =================================================================
    # ゲストのメッセージ（F4）
    # =================================================================
    def _cursor(self, platform: Platform, thread_key: str) -> set[str]:
        row = self.db.one("SELECT last_guest_key FROM thread_cursor WHERE platform=? AND thread_key=?",
                          platform.value, thread_key)
        return set(json.loads(row["last_guest_key"])) if row and row["last_guest_key"] else set()

    def _save_cursor(self, platform: Platform, thread_key: str, keys: set[str]) -> None:
        ordered = sorted(keys)[-1500:]
        self.db.run("INSERT OR REPLACE INTO thread_cursor (platform, thread_key, last_guest_key) VALUES (?, ?, ?)",
                    platform.value, thread_key, json.dumps(sorted(ordered)))

    def _mark_processed(self, platform: Platform, thread_key: str, messages: list[Message],
                        only: set[int] | None = None) -> None:
        """ゲストの発言を処理済みにする（すべての形式のキーで記録）。only を渡すと、その index の発言だけ"""
        variants = message_key_variants(messages)
        done = self._cursor(platform, thread_key)
        for m in messages:
            if m.role == "guest" and (only is None or m.index in only):
                done |= variants[m.index]
        self._save_cursor(platform, thread_key, done)

    @staticmethod
    def _done_state(m: Message, ks: set[str], done: set[str]) -> str:
        """発言が処理済みか: "done" / "new" / "unknown"（以前の記録と照合できない）。
        ID（id:）か画面の時刻＋本文（at:）のどちらかが記録と一致すれば処理済み。これらが無い発言だけ
        本文＋出現順（h:）で照合する（h: だけで照合すると、画面に出る発言の範囲がずれたときに、
        同じ文面の新着を処理済みと取り違えて見落とすため）"""
        precise = {k for k in ks if not k.startswith("h:")}
        if precise & done:
            return "done"
        has_id = any(k.startswith("id:") for k in done)
        has_precise = has_id or any(k.startswith("at:") for k in done)
        if not precise:
            if ks & done:
                return "done"
            return "unknown" if has_id else "new"
        if not m.uid and has_id:
            return "unknown"  # 以前は API の ID だけで記録していた発言かもしれない
        if not has_precise and ks & done:
            return "done"     # 本文＋出現順だけの古い記録
        return "new"

    def _source_key(self, m: Message, ks: set[str], stable: str) -> str:
        """文案の元の発言のキー（重複防止の一意キーに使うので、一番確かな形式にする）"""
        if m.uid:
            return f"id:{m.uid}"
        ats = sorted(k for k in ks if k.startswith("at:"))
        return ats[0] if ats else stable

    def _thread_reservation_ids(self, platform: Platform, thread_key: str) -> list[int]:
        if platform is Platform.IB:
            rows = self.db.all("SELECT id FROM reservations WHERE platform='ib' AND thread_id=?", thread_key)
        else:
            rows = self.db.all("SELECT id FROM reservations WHERE platform='sm' AND platform_id=?", thread_key)
        return [r["id"] for r in rows]

    def _system_sent_texts(self, platform: Platform, thread_key: str) -> list[str]:
        """このスレッドでシステムが送った返信の文面（人の手動返信と区別する）"""
        ids = self._thread_reservation_ids(platform, thread_key)
        if not ids:
            return []
        qs = ",".join("?" for _ in ids)
        rows = self.db.all(f"SELECT sent_text FROM inquiries WHERE reservation_id IN ({qs}) AND state=? "
                           "AND sent_text IS NOT NULL", *ids, InqState.SENT.value)
        code = secrets.get_optional("KEYBOX_CODE")
        out = []
        for row in rows:
            t = row["sent_text"]
            out.append(t)
            if code and PLACEHOLDER_CODE in t:
                out.append(t.replace(PLACEHOLDER_CODE, code))
        return out

    def _is_system_host(self, m: Message, sent_texts: list[str]) -> bool:
        """システムが送ったホスト発言か（定型文・マニュアルの PDF・システムが送った返信）"""
        if m.role != "host":
            return False
        if is_template_text(m.text, self.cfg.sent_marker, secrets.get_optional("KEYBOX_CODE")):
            return True
        stem = self.cfg.manual_pdf.stem
        if not (m.text or "").strip() and m.files and all(stem in f for f in m.files):
            return True
        return any(same_text(m.text, t) for t in sent_texts)

    async def process_guest_messages(self, rid: int, messages: list[Message], thread_key: str,
                                     initial: bool = False) -> None:
        r = self.res(rid)
        platform = Platform(r["platform"])
        stable = message_keys(messages)
        variants = message_key_variants(messages)
        key_index = {k: i for i, ks in variants.items() for k in ks}
        done = self._cursor(platform, thread_key)
        sent_texts = self._system_sent_texts(platform, thread_key)
        # 「人が管理画面で返信した」とみなすのは、システムが送っていないホストの発言だけ
        last_manual = max((m.index for m in messages if m.role == "host" and not self._is_system_host(m, sent_texts)),
                          default=-1)

        # 人が管理画面で返信済みの文案は止める
        for inq in self.open_drafts(rid):
            idx = key_index.get(inq["source_key"].split("|", 1)[-1])
            if idx is not None and last_manual > idx and inq["state"] != InqState.APPROVED:
                if self.db.transition("inquiries", inq["id"], OPEN_INQ_STATES, InqState.SENT_MANUALLY):
                    await self.post(text="✋ 管理画面でホストの返信を確認しました。この文案は送信しません。",
                                    thread_ts=inq["draft_ts"] or r["slack_ts"])

        guests = [m for m in messages if m.role == "guest"]
        states = {m.index: self._done_state(m, variants[m.index], done) for m in guests}
        # 処理済みの発言は、すべての形式のキーで記録し直す（次から、どの読み方でも照合できるように）
        upgraded = set(done)
        for m in guests:
            if states[m.index] == "done":
                upgraded |= variants[m.index]
        new = [m for m in guests if states[m.index] == "new"]
        unknown = [m for m in guests if states[m.index] == "unknown"]
        if unknown:
            # 以前の記録（メッセージ API の ID）と照合できない発言は、ID が取れる巡回まで新着扱いを待つ（回数に上限あり）
            kv = f"legacy_defer:{platform.value}:{thread_key}"
            n = int(self.db.kv_get(kv) or 0)
            if n < LEGACY_DEFER_MAX:
                self.db.kv_set(kv, str(n + 1))
            else:
                new = sorted(new + unknown, key=lambda m: m.index)
        if not new:
            if upgraded != done:
                self._save_cursor(platform, thread_key, upgraded)
            await self.refresh_parent(rid)
            return
        for m in new:
            upgraded |= variants[m.index]
        self._save_cursor(platform, thread_key, upgraded)
        unanswered = [m for m in new if m.index > last_manual]
        if not unanswered:
            return
        if initial:
            texts = [r["purpose_detail"] or "", *[m.text for m in unanswered]]
            if self.llm is None:
                has, reason = any("？" in t or "?" in t for t in texts), "キーワード判定"
            else:
                try:
                    has, reason = await self.llm.has_inquiry(texts)
                except Exception as e:  # noqa: BLE001
                    log.warning("問い合わせ判定に失敗: %s", e)
                    has, reason = True, "判定に失敗したため念のため文案を作成"
            self.db.log("inquiry_check", reservation_id=rid, result=str(has), detail=reason)
            if not has:
                # 返信は不要と判定しても、予約時に書かれたメッセージ（定型の [利用目的] 欄以外）は見えるようにする
                free = [m.text for m in unanswered if m.text and not m.text.lstrip().startswith("[利用目的]")]
                if free and r["slack_ts"]:
                    await self.post(text="💬 予約時のゲストのメッセージ（返信は不要と判定しました）\n"
                                         + B.quote(mask_contacts("\n\n".join(free))[:1500]), thread_ts=r["slack_ts"])
                return
        targets = unanswered
        if not initial:
            # 未送信の文案があるところに追加のメッセージが来たら、古い文案を置き換えて1つの文案にまとめる
            drafts_now = self.open_drafts(rid)
            stale = [d for d in drafts_now if d["state"] in OPEN_INQ_STATES]
            approved = any(d["state"] == InqState.APPROVED for d in drafts_now)
            if stale and not approved:
                # まとめる対象は、置き換える文案が答えようとしていた発言と、今回の新着
                targets = [Message("guest", d["guest_text"], at=from_iso(d["received_at"]))
                           for d in stale if d["guest_text"]] + unanswered
                for d in stale:
                    if self.db.transition("inquiries", d["id"], OPEN_INQ_STATES, InqState.SUPERSEDED) and d["draft_ts"]:
                        await self.react(d["draft_ts"], "arrows_counterclockwise")
                await self.post(text="↪️ ゲストから追加のメッセージが届いたため、未送信の文案を、"
                                     "まとめて答える新しい文案に置き換えます（前の文案は送信しません）。",
                                thread_ts=r["slack_ts"])
        last = unanswered[-1]
        source_key = f"{platform.value}:{thread_key}|{self._source_key(last, variants[last.index], stable[last.index])}"
        await self.create_inquiry(rid, targets, source_key, messages)

    def _conversation(self, messages: list[Message]) -> str:
        role = {"host": "ホスト", "guest": "ゲスト", "system": "システム"}
        code = secrets.get_optional("KEYBOX_CODE")
        out = []
        for m in messages[-40:]:
            t = m.text + (f"（添付: {', '.join(m.files)}）" if m.files else "")
            if code:
                t = self.mask_code(t)
            out.append(f"{role[m.role]}: {mask_contacts(t)}")
        return "\n".join(out) or "（なし）"

    def _reservation_info(self, r: dict) -> str:
        start, end = from_iso(r["start_at"]), from_iso(r["end_at"])
        sent = "はい" if self.code_allowed(r) else "いいえ"
        lines = [
            f"プラットフォーム: {Platform(r['platform']).label}",
            f"種類: {'予約前の問い合わせ' if r['kind'] == Kind.INQUIRY_ONLY else '予約'}",
            f"利用日時: {start:%Y/%m/%d %H:%M}〜{end:%m/%d %H:%M}" if start and end else "利用日時: 未定",
            f"人数: {r['people'] or '不明'}", f"利用目的: {r['purpose'] or '不明'}",
            f"用途詳細: {mask_contacts(r['purpose_detail'] or '') or 'なし'}", f"オプション: {r['options'] or 'なし'}",
            f"予約の状態: {r['platform_status'] or '不明'}", f"定型文送信済み: {sent}",
        ]
        return "\n".join(lines)

    async def create_inquiry(self, rid: int, msgs: list[Message], source_key: str, all_messages: list[Message]) -> int | None:
        await self.ensure_parent(rid, note=None if self.db.kv_get(f"repost_parent:{rid}") else "（システム導入前からの予約です）")
        guest_text = "\n\n".join(m.text for m in msgs if m.text)
        received = msgs[-1].at or now()
        try:
            iid = self.db.insert(
                "inquiries", reservation_id=rid, source_key=source_key, guest_text=guest_text,
                received_at=iso(received), state=InqState.DRAFT_POSTED.value,
                next_remind_at=iso(now() + timedelta(minutes=self.cfg.remind_interval_minutes)),
                wait_for_template=int(self.res(rid)["kind"] == Kind.RESERVATION and not self.res(rid)["template_sent_at"]),
                created_at=iso(now()), updated_at=iso(now()),
            )
        except sqlite3.IntegrityError:  # 同じ発言で2つ目の文案を作らない
            return None
        await self.produce_draft(iid, all_messages)
        return iid

    async def produce_draft(self, iid: int, all_messages: list[Message] | None = None) -> None:
        """文案を作って Slack に出す。文案が既にあれば投稿だけする（投稿に失敗した・途中で止まった場合の出し直し）。
        同じ問い合わせの文案を同時に2つ作らない"""
        if iid in self._drafting:
            return
        self._drafting.add(iid)
        try:
            await self._produce_draft(iid, all_messages)
        finally:
            self._drafting.discard(iid)

    async def _produce_draft(self, iid: int, all_messages: list[Message] | None = None) -> None:
        inq = dict(self.db.inquiry(iid))
        rid = inq["reservation_id"]
        r = self.res(rid)
        guest_text = inq["guest_text"] or ""
        received = from_iso(inq["received_at"])
        note = None
        if any(is_change_request(t) for t in guest_text.split("\n\n")):
            note = f"⚠️ 予約内容の変更リクエストです。<{r['dashboard_url']}|管理画面>で承諾／拒否してください（自動では回答しません）"
        draft, reason = inq["draft_text"], None
        if not draft and inq["state"] != InqState.NO_DRAFT:
            if self.llm is None:
                reason = "Claude API キーが未登録"
            else:
                try:
                    conversation = self._conversation(all_messages) if all_messages else "（取得していません）"
                    approved = [d["sent_text"] for d in self.open_drafts(rid)
                                if d["state"] == InqState.APPROVED and d["sent_text"]]
                    if approved:
                        conversation += "\n" + "\n".join(
                            f"ホスト（承認済み・定型文の後に送信予定。この内容には重ねて答えない）: {t}" for t in approved)
                    draft = await self.llm.draft_reply(reservation_info=self._reservation_info(r),
                                                       conversation=conversation, guest_text=guest_text)
                    draft = self.mask_code(draft)
                except LLMRefused:
                    reason = "Claude が応答を拒否"
                except LLMTruncated:
                    reason = "文案が長くなりすぎて途中で切れた"
                except Exception as e:  # noqa: BLE001
                    log.exception("文案の作成に失敗")
                    reason = f"エラー: {type(e).__name__}"
        elif not draft:
            reason = "前回、文案を作れませんでした"
        latest = self.db.inquiry(iid)
        if latest is None or latest["draft_ts"] or latest["state"] not in [s.value for s in OPEN_INQ_STATES]:
            return  # 待っている間に別の経路で投稿された・送信や置き換えが済んだ
        if draft:
            checks = B.extract_checks(draft) + [p for p in self.outgoing_problems(draft, r) if "要確認" not in p]
            needs = bool(checks)
            msg = B.draft_message(guest_text, received, draft, needs, note, checks=checks)
            ts = await self.post(text=msg["text"], attachments=msg["attachments"], thread_ts=r["slack_ts"],
                                 kind=MsgKind.DRAFT, rid=rid, iid=iid, mention=True)
            self.db.update("inquiries", iid, draft_text=draft, draft_ts=ts, needs_check=int(needs))
        else:
            msg = B.no_draft_message(guest_text, received, reason or "不明")
            ts = await self.post(text=msg["text"], blocks=msg["blocks"], thread_ts=r["slack_ts"], kind=MsgKind.DRAFT,
                                 rid=rid, iid=iid, mention=True)
            self.db.update("inquiries", iid, draft_ts=ts, state=InqState.NO_DRAFT.value)
            if note and ts:
                await self.post(text=note, thread_ts=r["slack_ts"])
        self.db.log("draft", reservation_id=rid, inquiry_id=iid,
                    result=("posted" if draft else "no_draft") + ("" if ts else "_post_failed"))
        await self.refresh_parent(rid)

    # =================================================================
    # F2: 承認スタンプ → 定型文とマニュアル
    # =================================================================
    def template_text(self, r: dict) -> str:
        return (self.cfg.template_reservation
                .replace("{SALUTATION}", salutation(r["guest_name"]))
                .replace("{KEYBOX_CODE}", self.code()))

    async def execute_template(self, rid: int, actor: str, trigger: str) -> None:
        async with self._locks[rid]:
            r = self.res(rid)
            if r["kind"] != Kind.RESERVATION:
                return
            end = from_iso(r["cleanup_end_at"]) or from_iso(r["end_at"])
            if end and end <= now() and r["state"] in (ResState.NOTIFIED, ResState.ON_HOLD, ResState.ERROR, ResState.MANUAL):
                await self.post(text="⛔ 利用時間が過ぎているため、定型文（暗証番号）は送りません。必要なら管理画面で対応してください。",
                                thread_ts=r["slack_ts"])
                return
            if not self.db.transition("reservations", rid,
                                      (ResState.NOTIFIED, ResState.ON_HOLD, ResState.ERROR, ResState.MANUAL),
                                      ResState.EXECUTING, approved_by=actor):
                log.info("execute_template: 状態が %s のため実行しない", r["state"])
                return
            await self.refresh_parent(rid)
            platform = Platform(r["platform"])
            self.db.log("template_start", reservation_id=rid, trigger=trigger, actor=actor)
            try:
                text = self.template_text(r)
                code = self.code()
                if platform is Platform.SM:
                    result = await asyncio.wait_for(self.sm.approve_and_send_template(
                        r["platform_id"], text, self.cfg.manual_pdf, self.cfg.sent_marker, self.cfg.dry_run, code=code),
                        timeout=TEMPLATE_TIMEOUT_SEC)
                else:
                    result, tid = await asyncio.wait_for(self.ib.send_template(
                        r["platform_id"], text, self.cfg.manual_pdf, self.cfg.sent_marker, self.cfg.dry_run, code=code),
                        timeout=TEMPLATE_TIMEOUT_SEC)
                    if tid:
                        self.db.update("reservations", rid, thread_id=tid)
            except NotReady as e:
                # 承認はできたが支払い待ちなど。暗証番号は送らず、支払いの確認後にもう一度スタンプしてもらう
                self.db.transition("reservations", rid, (ResState.EXECUTING,), ResState.NOTIFIED,
                                   next_remind_at=iso(now() + timedelta(minutes=self.cfg.remind_interval_minutes)))
                self.db.kv_set(f"await_payment:{rid}", "1")
                self.db.log("template", reservation_id=rid, actor=actor, result="not_ready", detail=e.message,
                            screenshots=e.screenshots)
                await self.post(text=f"⏳ {e.message}。支払いが確認されたらお知らせします"
                                     "（そのとき親メッセージにスタンプで、定型文とマニュアルを送ります）。",
                                thread_ts=r["slack_ts"])
                await self.refresh_parent(rid)
                return
            except LoginRequired as e:
                self.db.transition("reservations", rid, (ResState.EXECUTING,), ResState.ERROR)
                self.db.log("template", reservation_id=rid, actor=actor, result="login_required", detail=str(e))
                await self.post_error(rid, "ログイン", str(e), None, retry="template")
                await self.alert(f"login:{platform.value}", f"{platform.label}への再ログインが必要です（{e}）。"
                                 "手順書（docs/runbook.md の「ログイン切れ」）に沿ってログインしてください")
                await self.refresh_parent(rid)
                return
            except ActionFailed as e:
                self.db.transition("reservations", rid, (ResState.EXECUTING,), ResState.ERROR)
                self.db.log("template", reservation_id=rid, actor=actor, result="failed", detail=str(e),
                            screenshots=e.screenshots)
                if "のため送信しません" in e.message:
                    await self.close(rid, None, note=e.message)
                else:
                    await self.post_error(rid, e.step, e.message, e.screenshots, retry="template")
                await self.refresh_parent(rid)
                return
            except TimeoutError:
                log.error("template timeout rid=%s", rid)
                self.db.transition("reservations", rid, (ResState.EXECUTING,), ResState.ERROR)
                self.db.log("template", reservation_id=rid, actor=actor, result="timeout")
                await self.post_error(rid, "定型文の送信", f"管理画面の操作が {TEMPLATE_TIMEOUT_SEC // 60} 分以内に終わりませんでした",
                                      None, retry="template")
                await self.refresh_parent(rid)
                return
            except Exception as e:  # noqa: BLE001
                log.exception("template failed")
                self.db.transition("reservations", rid, (ResState.EXECUTING,), ResState.ERROR)
                self.db.log("template", reservation_id=rid, actor=actor, result="error", detail=repr(e))
                await self.post_error(rid, "定型文の送信", repr(e), None, retry="template")
                await self.refresh_parent(rid)
                return

            self.db.transition("reservations", rid, (ResState.EXECUTING,), ResState.CONFIRMED,
                               template_sent_at=iso(now()), hold_since=None, next_remind_at=None)
            self.db.kv_set(f"await_payment:{rid}", None)
            self.db.log("template", reservation_id=rid, actor=actor, result="dry_run" if result.dry_run else "ok",
                        detail=result.note, screenshots=result.screenshots)
            who = f"<@{actor}>" if actor and actor.startswith("U") else actor
            approved = "承認しました" in result.note
            if result.skipped:
                text = f"✅ {'承認しました。' if approved else ''}{result.note.replace('承認しました。', '')}。自動送信はしていません。"
            elif approved:
                text = f"✅ 承認し、定型文とマニュアルを送信しました（スタンプ: {who}、{now():%H:%M}）"
            else:
                text = f"✅ 定型文とマニュアルを送信しました（スタンプ: {who}、{now():%H:%M}）"
            if result.dry_run:
                text = f"🧪 (DRY_RUN) {result.note}\n{text}（実際には送信していません）"
            await self.post(text=text, thread_ts=r["slack_ts"])
        await self.refresh_parent(rid)
        await self.flush_approved(rid)

    async def flush_approved(self, rid: int) -> None:
        """承認済みで定型文待ちだった問い合わせ返信を送る（定型文が送られた後に呼ぶ）"""
        if not self.res(rid)["template_sent_at"]:
            return
        for inq in self.open_drafts(rid):
            if inq["state"] == InqState.APPROVED:
                await self.send_inquiry(inq["id"], inq["sent_text"], inq["sent_by"], "after_template",
                                        revised=inq["draft_text"] != inq["sent_text"])

    # =================================================================
    # F5: 文案の送信
    # =================================================================
    def _thread_key(self, r: dict) -> str | None:
        return r["thread_id"] if r["platform"] == Platform.IB.value else r["platform_id"]

    def _send_guard(self, r: dict, inq: dict, final: str):
        """送信直前のスレッドを見て、送るべきでなければ SendSkipped を投げる関数を作る。
        - この返信が答えるゲストの発言より後に、同じ文面がある → 送信済み
        - その後に、システムが送っていないホストの発言がある → 管理画面で返信済み
        - その後に、まだ処理していないゲストの発言がある → 新着を含めて文案を作り直す"""
        platform = Platform(r["platform"])
        thread_key = self._thread_key(r) or ""
        src = (inq["source_key"] or "").split("|", 1)[-1]
        created = from_iso(inq["created_at"])
        sent_texts = self._system_sent_texts(platform, thread_key) if thread_key else []
        done = self._cursor(platform, thread_key) if thread_key else set()

        def guard(messages: list[Message]) -> None:
            variants = message_key_variants(messages)
            key_index = {k: i for i, ks in variants.items() for k in ks}
            src_idx = key_index.get(src)
            if src_idx is None:
                # 承認者が書いた返信（元のゲスト発言が無い）: 作成後の同じ文面だけを「送信済み」とみなす
                recent = [m for m in messages if m.role == "host"
                          and (m.at is None or created is None or m.at >= created - timedelta(minutes=2))]
                if any(same_text(m.text, final) for m in recent[-5:]):
                    raise SendSkipped("already_sent", "同じ文面が既に送信されていました")
                return
            after = [m for m in messages if m.index > src_idx]
            if any(m.role == "host" and same_text(m.text, final) for m in after):
                raise SendSkipped("already_sent", "同じ文面が既に送信されていました")
            if any(m.role == "host" and not self._is_system_host(m, sent_texts) for m in after):
                raise SendSkipped("manual_reply", "管理画面で、このメッセージへのホストの返信を確認しました")
            if any(m.role == "guest" and not (variants[m.index] & done) for m in after):
                err = SendSkipped("new_guest_message", "送信の直前に、ゲストから新しいメッセージが届いていました")
                err.messages = messages  # type: ignore[attr-defined]
                raise err
        return guard

    async def send_inquiry(self, iid: int, text: str, actor: str, trigger: str, revised: bool) -> None:
        inq = dict(self.db.inquiry(iid))
        rid = inq["reservation_id"]
        async with self._locks[rid]:
            r = self.res(rid)
            inq = dict(self.db.inquiry(iid))
            who = f"<@{actor}>" if actor and actor.startswith("U") else actor
            thread = r["slack_ts"]
            if inq["state"] not in UNSENT_INQ_STATES:
                log.info("send_inquiry: 状態が %s のため送信しない", inq["state"])
                return
            # 文案そのものの検査（外部連絡先など）は文案の作成時に済んでいる（要確認なら スタンプでは送らない）。
            # ここでは、どの経路でも残っていてはいけない印・伏せ字を見る
            problems = self.outgoing_problems(text, r, human=True, check_code=False)
            if problems:
                await self.post(text=f"🚫 送信しませんでした（{who}）：" + "、".join(problems)
                                     + "。直した全文をこのスレッドに返信してください。", thread_ts=thread)
                return
            if r["kind"] == Kind.RESERVATION and not r["template_sent_at"] and r["state"] != ResState.CLOSED:
                was_approved = inq["state"] == InqState.APPROVED
                froms = (*OPEN_INQ_STATES, InqState.APPROVED) if revised else OPEN_INQ_STATES
                if self.db.transition("inquiries", iid, froms, InqState.APPROVED, sent_text=text, sent_by=actor):
                    msg = (f"👌 承認済みの返信を、この修正文に置き換えました（{who}）。定型文とマニュアルの送信後に送ります。"
                           if was_approved else
                           f"👌 承認を受け付けました（{who}）。定型文とマニュアルの送信後に、この返信を送ります。")
                    await self.post(text=msg, thread_ts=thread)
                else:
                    await self.post(text="ℹ️ この返信は、すでに承認済みか処理済みです（何もしていません）。", thread_ts=thread)
                return
            final = text
            if self.has_code(final):
                if not self.code_allowed(r):
                    await self.post(text="⚠️ 暗証番号を含む返信は、定型文の送信後で利用前の予約にしか送れません。文面を直してください。",
                                    thread_ts=thread)
                    return
                final = final.replace(PLACEHOLDER_CODE, self.code())
            prev_state = inq["state"]
            if not self.db.transition("inquiries", iid, UNSENT_INQ_STATES, InqState.SENDING, sent_text=text, sent_by=actor):
                return
            platform = Platform(r["platform"])
            guard = self._send_guard(r, inq, final)
            try:
                if platform is Platform.SM:
                    result = await asyncio.wait_for(
                        self.sm.send_message(r["platform_id"], final, self.cfg.dry_run, guard=guard), timeout=SEND_TIMEOUT_SEC)
                else:
                    tid = r["thread_id"]
                    if not tid:
                        d = await self.ib.get_detail(r["platform_id"])
                        tid = d.thread_id
                        if tid:
                            self.db.update("reservations", rid, thread_id=tid)
                    if not tid:
                        raise ActionFailed("スレッド", "メッセージのスレッドが見つかりません")
                    result = await asyncio.wait_for(
                        self.ib.send_message(tid, final, self.cfg.dry_run, guard=guard), timeout=SEND_TIMEOUT_SEC)
            except SendSkipped as e:
                await self._send_skipped(rid, iid, prev_state, e, thread)
                return
            except (TimeoutError, ActionFailed, LoginRequired, Exception) as e:  # noqa: BLE001
                self.db.transition("inquiries", iid, (InqState.SENDING,), InqState.ERROR)
                shots = getattr(e, "screenshots", None)
                self.db.log("send_inquiry", reservation_id=rid, inquiry_id=iid, actor=actor, result="failed",
                            detail=repr(e), screenshots=shots)
                msg = "管理画面の操作が時間内に終わりませんでした" if isinstance(e, asyncio.TimeoutError) else getattr(e, "message", repr(e))
                await self.post_error(rid, getattr(e, "step", "返信の送信"), msg, shots, retry="inquiry", iid=iid)
                if isinstance(e, LoginRequired):
                    await self.alert(f"login:{platform.value}", f"{platform.label}への再ログインが必要です（{e}）")
                await self.refresh_parent(rid)
                return
            self.db.transition("inquiries", iid, (InqState.SENDING,), InqState.SENT, sent_at=iso(now()))
            self.db.log("send_inquiry", reservation_id=rid, inquiry_id=iid, actor=actor,
                        result="dry_run" if result.dry_run else ("skipped" if result.skipped else "ok"), detail=result.note)
            prefix = "🧪 (DRY_RUN) 実際には送信していません。" if result.dry_run else ""
            if result.skipped:
                body = f"✅ {result.note}（自動送信はしていません）"
            elif revised:
                body = f"📤 以下の文面で送信しました（{who}、{now():%H:%M}）\n```{B.esc(text)}```"
            else:
                body = f"📤 文案を送信しました（スタンプ: {who}、{now():%H:%M}）"
            await self.post(text=prefix + body, thread_ts=thread)
            if not result.dry_run and not result.skipped:
                self._remember_reply(r, inq["guest_text"] or "", text, revised)
        await self.refresh_parent(rid)

    async def _send_skipped(self, rid: int, iid: int, prev_state: str, e: SendSkipped, thread: str | None) -> None:
        self.db.log("send_inquiry", reservation_id=rid, inquiry_id=iid, result=f"skipped:{e.code}", detail=e.message)
        if e.code == "already_sent":
            self.db.transition("inquiries", iid, (InqState.SENDING,), InqState.SENT, sent_at=iso(now()))
            await self.post(text=f"✅ {e.message}（自動送信はしていません）", thread_ts=thread)
        elif e.code == "manual_reply":
            self.db.transition("inquiries", iid, (InqState.SENDING,), InqState.SENT_MANUALLY)
            await self.post(text=f"✋ {e.message}。この返信は送信しませんでした（送る必要があれば、修正文として返信してください）。",
                            thread_ts=thread)
        else:
            back = prev_state if prev_state in (s.value for s in OPEN_INQ_STATES) else InqState.DRAFT_POSTED.value
            self.db.transition("inquiries", iid, (InqState.SENDING,), back)
            await self.post(text=f"↪️ {e.message}。この文案の送信を止め、新しいメッセージも含めて文案を作り直します。",
                            thread_ts=thread)
            r = self.res(rid)
            key = self._thread_key(r)
            msgs = getattr(e, "messages", None)
            if key and msgs:
                await self.process_guest_messages(rid, msgs, key)
        await self.refresh_parent(rid)

    def _remember_reply(self, r: dict, guest_text: str, sent: str, revised: bool) -> None:
        """送った返信をナレッジに追記する（暗証番号・連絡先・ゲスト名は伏せる）。
        承認者の修正文は「過去に修正して送った回答」として別ファイルに残す（手で書いた FAQ には混ぜない）"""
        code = secrets.get_optional("KEYBOX_CODE")

        def clean(t: str) -> str:
            t = mask_contacts(t or "")
            if code:
                t = re.sub(rf"(?<!\d){re.escape(code)}(?!\d)", PLACEHOLDER_CODE, t)
            for part in [r["guest_name"] or "", *re.split(r"[\s　]+", r["guest_name"] or "")]:
                if len(part) >= 2:
                    t = t.replace(part, "◯◯")
            return t

        try:
            d = ROOT / "knowledge" / "past_replies"
            d.mkdir(parents=True, exist_ok=True)
            f = d / f"sent_{now():%Y%m}.md"
            with f.open("a", encoding="utf-8") as fp:
                fp.write(f"## {now():%Y-%m-%d} {Platform(r['platform']).label}\nゲスト: {clean(guest_text)[:800]}\n"
                         f"ホスト: {clean(sent)}\n\n")
            if revised:
                learned = ROOT / "knowledge" / "faq_learned.md"
                with learned.open("a", encoding="utf-8") as fp:
                    fp.write(f"\n## Q. {clean(guest_text)[:200].replace(chr(10), ' ')}\n"
                             f"A. {clean(sent).replace(chr(10), ' ')}（{now():%Y-%m-%d}、承認者の修正文）\n")
        except OSError as e:
            log.warning("ナレッジへの追記に失敗: %s", e)

    async def dismiss_drafts(self, rid: int, user: str) -> int:
        """未送信の文案を「送らない」にする（承認者の指示）"""
        n = 0
        for d in self.open_drafts(rid):
            if self.db.transition("inquiries", d["id"], UNSENT_INQ_STATES, InqState.SUPERSEDED):
                n += 1
                if d["draft_ts"]:
                    await self.react(d["draft_ts"], "heavy_minus_sign")
        r = self.res(rid)
        await self.post(text=(f"🗑 未送信の文案 {n} 件を送らないことにしました（<@{user}>）。" if n
                              else "未送信の文案はありません。"), thread_ts=r["slack_ts"])
        await self.refresh_parent(rid)
        return n

    # =================================================================
    # 途中で止まった処理の回収
    # =================================================================
    async def recover_stuck(self, older_than_minutes: int = 0) -> int:
        """EXECUTING / COMPLETING / SENDING のまま残ったもの（プロセスの停止・固まり）を ERROR にして、
        管理画面を確認してからの再実行を依頼する。older_than_minutes=0 は起動時（実行中の処理は無い）"""
        cutoff = iso(now() - timedelta(minutes=older_than_minutes))
        n = 0
        why = "処理の途中でシステムが停止しました。送信できたか分からないため、管理画面で確認してください"
        if older_than_minutes == 0:
            # 以前の版はレビューの失敗も ERROR にしていた（定型文は送信済み）。REVIEW_ERROR に直す
            self.db.run("UPDATE reservations SET state=? WHERE state=? AND kind=? AND template_sent_at IS NOT NULL",
                        ResState.REVIEW_ERROR.value, ResState.ERROR.value, Kind.RESERVATION.value)
            # レビューの確認メッセージを出す途中で止まった（確認メッセージが無い）→ 出し直す
            if hasattr(self, "reissue_stuck_confirms"):
                await self.reissue_stuck_confirms(0)
        for row in self.db.all("SELECT * FROM reservations WHERE state IN (?, ?) AND updated_at <= ?",
                               ResState.EXECUTING.value, ResState.COMPLETING.value, cutoff):
            rid = row["id"]
            if self._locks[rid].locked():
                continue
            if row["state"] == ResState.EXECUTING:
                if self.db.transition("reservations", rid, (ResState.EXECUTING,), ResState.ERROR):
                    await self.post_error(rid, "定型文の送信", why, None, retry="template")
            elif self.db.transition("reservations", rid, (ResState.COMPLETING,), ResState.REVIEW_ERROR):
                args = json.loads(self.db.kv_get(f"review_args:{rid}") or "{}")
                await self.review_failed(rid, "レビュー投稿", why, None, args.get("stars"),
                                         None if args.get("auto") else args.get("comment"), shown=args.get("comment"))
            n += 1
            await self.refresh_parent(rid)
        for q in self.db.all("SELECT * FROM inquiries WHERE state=? AND updated_at <= ?", InqState.SENDING.value, cutoff):
            if self._locks[q["reservation_id"]].locked():
                continue
            if self.db.transition("inquiries", q["id"], (InqState.SENDING,), InqState.ERROR):
                await self.post_error(q["reservation_id"], "返信の送信", why, None, retry="inquiry", iid=q["id"])
                n += 1
                await self.refresh_parent(q["reservation_id"])
        if n:
            log.warning("途中で止まった処理を %d 件回収しました", n)
        return n

    async def recheck_unreviewed(self) -> None:
        """以前の版は、ゲストのバッジ「ホストレビューあり」を投稿済みと読んでいた。導入時に閉じた過去の SM 予約で、
        まだレビューを書けて未投稿のものを一度だけ読み直し、一覧で知らせる（自動では投稿しない）"""
        if self.db.kv_get("review_recheck_v1"):
            return
        # 投稿済みかは API で判定するので、DB の review_posted_at では絞らない（以前の版の誤読で入っていることがある）
        rows = self.db.all("SELECT * FROM reservations WHERE platform='sm' AND kind=? AND state=? "
                           "AND end_at IS NOT NULL AND end_at >= ? AND end_at <= ?", Kind.RESERVATION.value,
                           ResState.CLOSED.value, iso(now() - timedelta(days=14)), iso(now()))
        posted_by_system = {r["reservation_id"] for r in self.db.all(
            "SELECT reservation_id FROM action_log WHERE action='review' AND result='ok'")}
        todo, unknown, failed = [], [], 0
        for row in rows:
            if any(w in (row["platform_status"] or "") for w in ("キャンセル", "否認", "期限切れ", "非承認")):
                continue
            if row["id"] in posted_by_system or self.db.kv_get(f"review_declined:{row['id']}"):
                continue  # システムが投稿した、または承認者が「レビューしない」と決めた
            try:
                d = await self.sm.get_detail(row["platform_id"])
            except Exception as e:  # noqa: BLE001
                log.warning("レビューの読み直しに失敗 %s: %s", row["platform_id"], e)
                failed += 1
                unknown.append(f"・<{row['dashboard_url']}|{B.esc(row['guest_name'] or row['platform_id'])} 様 "
                               f"{row['platform_id']}>（画面を読めませんでした）")
                continue
            label = f"<{row['dashboard_url']}|{B.esc(row['guest_name'] or row['platform_id'])} 様 {row['platform_id']}>"
            if d.review_posted is None or d.review_open is None:
                failed += 1  # 投稿済みか・書けるかを API から読み取れなかった
                unknown.append(f"・{label}（投稿済みか確認できませんでした）")
                continue
            if d.review_posted is False and d.review_open:
                due = f"（期限 {d.review_expires_at:%m/%d %H:%M}）" if d.review_expires_at else ""
                todo.append(f"・{label}{due}")
        tries = int(self.db.kv_get("review_recheck_tries") or 0) + 1
        self.db.kv_set("review_recheck_tries", str(tries))
        if failed and tries < 6:
            return  # 読めなかった予約がある。後でもう一度（済みの印は付けない）
        todo += unknown  # 何度試しても確認できなかったものは、確認できなかったとして知らせる
        if todo:
            ts = await self.post(text="📋 スペースマーケット：レビューが未投稿で、まだ書ける過去の予約です（導入時の確認で漏れていました。"
                                      "自動では投稿しません。必要なら管理画面で投稿してください）\n" + "\n".join(todo), mention=True)
            if not ts:
                return  # 投稿に失敗した。後でもう一度
        self.db.kv_set("review_recheck_v1", iso(now()))

    # =================================================================
    # クローズ
    # =================================================================
    async def close(self, rid: int, status: str | None, note: str | None = None) -> None:
        r = self.res(rid)
        if status:
            self.db.update("reservations", rid, platform_status=status)
        if not self.db.transition("reservations", rid, ACTIVE_RES_STATES, ResState.CLOSED, next_remind_at=None):
            return
        cancelled = any(w in (status or "") for w in ("キャンセル", "否認", "期限切れ", "非承認"))
        if cancelled:
            self.db.run("UPDATE guest_history SET cancelled=1 WHERE platform=? AND platform_id=?", r["platform"], r["platform_id"])
        self.db.kv_set(f"await_payment:{rid}", None)
        drafts = self.open_drafts(rid)
        for d in drafts:
            # 定型文待ちの承認済み返信は、定型文が送られないので元に戻す（送るか送らないかを人が決める）
            if d["state"] == InqState.APPROVED:
                self.db.transition("inquiries", d["id"], (InqState.APPROVED,), InqState.DRAFT_POSTED)
        self.db.log("close", reservation_id=rid, result=status or "", detail=note)
        if r["slack_ts"]:
            text = note or (f"✖️ 予約が「{status}」になりました。対応を終了します。" if status else "🏁 対応を終了しました。")
            if drafts:
                text += (f"\n💬 未送信の返信文案が {len(drafts)} 件残っています。必要なら文案にスタンプ（または修正文を返信）で"
                         "送信、不要ならスレッドに「送らない」と返信してください。")
            await self.post(text=text, thread_ts=r["slack_ts"], mention=bool(drafts))
            await self.refresh_parent(rid)
