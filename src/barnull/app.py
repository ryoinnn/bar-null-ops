"""常駐プロセス: Slack（Socket Mode）＋定時処理（ポーリング・リマインド・利用完了）＋見張り。"""

from __future__ import annotations

import asyncio
import ctypes
import dataclasses
import json
import logging
import sys
from datetime import timedelta
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp

from . import secrets
from .config import DATA_DIR, Config
from .db import DB
from .llm.client import LLM
from .models import Platform
from .platforms.browser import BrowserPool, purge_old_screenshots
from .textutil import JST, from_iso, iso, now
from .workflows.handlers import Orchestrator

log = logging.getLogger(__name__)

ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
# 承認者の返信として受け付けるメッセージの種類（「チャンネルにも投稿する」付き・ファイル付きの返信も含める）
REPLY_SUBTYPES = (None, "thread_broadcast", "file_share")


def prevent_sleep() -> None:
    """プロセス稼働中は Windows のスリープを抑止する（画面の消灯は妨げない）"""
    if sys.platform == "win32":
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)


class SingleInstance:
    def __init__(self, path: Path, message: str = "barnull は既に起動しています"):
        self.path = path
        self.message = message
        self.fp = None

    def __enter__(self):
        import msvcrt

        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fp = open(self.path, "a+")  # noqa: SIM115
        try:
            msvcrt.locking(self.fp.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            self.fp.close()
            raise SystemExit(self.message) from None
        return self

    def __exit__(self, *exc):
        if self.fp:
            self.fp.close()


def _spawn(coro, name: str) -> None:
    async def runner():
        try:
            await coro
        except Exception:  # noqa: BLE001
            log.exception("%s でエラー", name)
    asyncio.create_task(runner(), name=name)


async def deadman_refresh(engine: Orchestrator, fire_after_min: int) -> None:
    """停止検知: 「N分後に投稿される警告」を予約し直す。プロセスが止まると予約が消されず警告が出る。
    新しい予約を入れてから古い予約を消す（予約に失敗したときに、警告が何も予約されていない状態にしない）"""
    client, ch, db = engine.slack, engine.cfg.channel, engine.db
    old = db.kv_get("deadman_id")
    old_at = int(db.kv_get("deadman_at") or 0)
    if old and old_at and old_at <= now().timestamp():
        # 前回予約した「停止している可能性」の警告は、すでに投稿された（スリープ・ネットワーク切断などで止まっていた）
        try:
            await client.chat_postMessage(channel=ch, text=f"🟢 予約対応システムは再開しています（{now():%m/%d %H:%M}）。"
                                                         "PC のスリープやネットワークの切断で一時停止していました。"
                                                         "止まっていた間の予約・メッセージは、巡回で読み直します。")
        except Exception as e:  # noqa: BLE001
            log.warning("再開の知らせの投稿に失敗: %s", e)
    post_at = int((now() + timedelta(minutes=fire_after_min)).timestamp())
    from .slack.blocks import mention

    text = (f"{mention(engine.cfg.approvers)}\n⚠️ Bar null 予約対応システムが停止している可能性があります"
            f"（最後の生存確認: {now():%m/%d %H:%M}）。PC の電源・スリープ、タスクスケジューラの BarNullOps を確認してください。")
    try:
        resp = await client.chat_scheduleMessage(channel=ch, post_at=post_at, text=text)
    except Exception as e:  # noqa: BLE001
        log.warning("停止検知の予約投稿に失敗（前回の予約は残します）: %s", e)
        return
    db.kv_set("deadman_id", resp["scheduled_message_id"])
    db.kv_set("deadman_at", str(post_at))
    if old:
        try:
            await client.chat_deleteScheduledMessage(channel=ch, scheduled_message_id=old)
        except Exception:  # noqa: BLE001
            pass


async def deadman_cancel(engine: Orchestrator) -> None:
    old = engine.db.kv_get("deadman_id")
    if old:
        try:
            await engine.slack.chat_deleteScheduledMessage(channel=engine.cfg.channel, scheduled_message_id=old)
        except Exception:  # noqa: BLE001
            pass
        engine.db.kv_set("deadman_id", None)
        engine.db.kv_set("deadman_at", None)


async def process_commands(engine: Orchestrator) -> None:
    """`barnull simulate` が積んだコマンドを実行する（ブラウザのプロファイルを共有するため常駐側で動かす）"""
    from .simulate import execute

    rows = engine.db.all("SELECT * FROM kv WHERE key LIKE 'cmd:%' AND value IS NOT NULL ORDER BY key")
    for row in rows:
        engine.db.kv_set(row["key"], None)
        try:
            await execute(engine, json.loads(row["value"]))
        except Exception:  # noqa: BLE001
            log.exception("コマンドの実行に失敗: %s", row["value"])


def purge_all_screenshots(cfg: Config) -> None:
    """本番・DRY_RUN・模擬のすべてのスクリーンショットを保存期間で削除する"""
    for d in (DATA_DIR, DATA_DIR / "dryrun", DATA_DIR / "mock"):
        try:
            purge_old_screenshots(dataclasses.replace(cfg, data_dir=d))
        except Exception as e:  # noqa: BLE001
            log.warning("スクリーンショットの削除に失敗 %s: %s", d, e)


async def run(cfg: Config, mock: bool = False) -> None:
    if mock and not cfg.dry_run:
        raise SystemExit("--mock は DRY_RUN のときだけ使えます")
    # ブラウザのプロファイルと Slack の接続はモードをまたいで共通なので、ロックも全モード共通にする
    with SingleInstance(cfg.global_lock, "barnull は既に起動しています（本番・DRY_RUN・模擬のどれか、"
                                         "または login などのコマンドが動いています）"):
        await _run(cfg, mock)


async def _run(cfg: Config, mock: bool = False) -> None:
    db = DB(cfg.db_path)
    app = AsyncApp(token=secrets.get("SLACK_BOT_TOKEN"))
    try:
        # Slack のレート制限（429）と一時的な接続エラーは、待ってから再試行する
        # （接続エラーの再試行は既定で入っている）
        from slack_sdk.http_retry.builtin_async_handlers import AsyncRateLimitErrorRetryHandler

        app.client.retry_handlers.append(AsyncRateLimitErrorRetryHandler(max_retry_count=2))
    except Exception as e:  # noqa: BLE001
        log.warning("Slack の再試行の設定に失敗: %s", e)
    llm = LLM(cfg) if secrets.get_optional("ANTHROPIC_API_KEY") else None
    if llm is None:
        log.warning("ANTHROPIC_API_KEY が未登録のため、文案・分類・お礼文は使えません（キーワード判定と定型のお礼文で動きます）")
    pool = BrowserPool(cfg, headless=True)
    engine = Orchestrator(cfg, db, app.client, pool, llm)
    if mock:
        from .platforms.mock import MockIB, MockSM, MockStore
        from .slack import blocks

        def _load(p, pid):
            row = db.reservation_by_pid(p.value, pid)
            qs = db.all("SELECT * FROM inquiries WHERE reservation_id=? ORDER BY id", row["id"]) if row else []
            return row, qs

        # 模擬アダプタに先に差し替える（本物の管理画面を開かないように）
        engine.mock = MockStore(loader=_load, marker=cfg.sent_marker)
        engine.sm, engine.ib = MockSM(cfg, engine.mock), MockIB(cfg, engine.mock)
        # 再起動で作り直した模擬データの発言は、新着ではなく処理済みとして扱う
        for row in db.all("SELECT * FROM reservations WHERE state<>'CLOSED'"):
            try:
                pf = Platform(row["platform"])
                d = await engine.adapter(pf).get_detail(row["platform_id"])
                key = (d.thread_id or row["thread_id"]) if pf is Platform.IB else row["platform_id"]
                if key:
                    engine._mark_processed(pf, key, d.messages)
            except Exception as e:  # noqa: BLE001
                log.warning("模擬データの復元に失敗: %s", e)
        blocks.TEST_PREFIX = "🧪【テスト】"
    auth = await app.client.auth_test()
    engine.bot_user_id = auth["user_id"]
    log.info("起動: bot=%s channel=%s dry_run=%s", auth["user_id"], cfg.channel, cfg.dry_run)

    @app.event("reaction_added")
    async def _reaction(event: dict) -> None:
        item = event.get("item") or {}
        if item.get("type") != "message" or item.get("channel") != cfg.channel:
            return
        _spawn(engine.on_reaction(event.get("user"), item.get("ts"), event.get("reaction", ""),
                                  event.get("event_ts")), "reaction")

    @app.event("reaction_removed")
    async def _reaction_removed(event: dict) -> None:
        return

    @app.event("message")
    async def _message(event: dict) -> None:
        if event.get("channel") != cfg.channel or event.get("bot_id") or event.get("subtype") not in REPLY_SUBTYPES:
            return
        thread_ts = event.get("thread_ts")
        if not thread_ts or thread_ts == event.get("ts"):
            return
        _spawn(engine.on_reply(event.get("user"), thread_ts, event.get("text", ""), event.get("ts")), "reply")

    prevent_sleep()
    # 前回、処理の途中で止まったもの（EXECUTING・SENDING など）を回収する
    try:
        await engine.recover_stuck(0)
    except Exception:  # noqa: BLE001
        log.exception("途中で止まった処理の回収に失敗")

    sched = AsyncIOScheduler(timezone=JST)
    if not mock:
        sched.add_job(engine.poll, "interval", seconds=cfg.poll_interval_sec, jitter=cfg.poll_jitter_sec,
                      next_run_time=now() + timedelta(seconds=15), max_instances=1, coalesce=True, id="poll")
        sched.add_job(engine.tick_sweep, "interval", minutes=180, max_instances=1, coalesce=True, id="sweep",
                      next_run_time=now() + timedelta(minutes=15))
        sched.add_job(engine.tick_watchdog, "interval", minutes=5, max_instances=1, coalesce=True, id="watchdog")
    sched.add_job(engine.tick_reminders, "interval", seconds=60, max_instances=1, coalesce=True, id="remind")
    sched.add_job(engine.tick_completion, "interval", seconds=300, max_instances=1, coalesce=True, id="complete",
                  next_run_time=now() + timedelta(seconds=90))
    sched.add_job(engine.tick_repost, "interval", seconds=60, max_instances=1, coalesce=True, id="repost",
                  next_run_time=now() + timedelta(seconds=45))
    sched.add_job(engine.recover_stuck, "interval", minutes=5, args=[20], max_instances=1, coalesce=True, id="stuck")
    sched.add_job(process_commands, "interval", seconds=5, args=[engine], max_instances=1, coalesce=True, id="cmd")
    # 停止中・接続が切れていた間のスタンプ・返信を拾い直す（起動直後と10分ごと）
    sched.add_job(engine.catch_up, "interval", minutes=10, max_instances=1, coalesce=True, id="catch_up")
    sched.add_job(deadman_refresh, "interval", minutes=cfg.deadman_refresh_minutes,
                  args=[engine, cfg.deadman_fire_after_minutes], next_run_time=now(), id="deadman")
    sched.add_job(prevent_sleep, "interval", minutes=30, id="nosleep")
    # 秘密情報を登録し直したときも、ログ・Slack で新しい値を伏せられるように読み直す
    sched.add_job(secrets.load_redactions, "interval", minutes=10, id="redactions")
    # 日次の処理は、PC が一時的に止まっていても、その日のうちなら実行する
    sched.add_job(purge_all_screenshots, "cron", hour=4, minute=10, args=[cfg], id="purge",
                  misfire_grace_time=6 * 3600, coalesce=True)
    if not mock:
        sched.add_job(ib_review_sweep, "cron", hour=9, minute=5, args=[engine], id="review_sweep",
                      misfire_grace_time=6 * 3600, coalesce=True)
        sched.add_job(engine.recheck_unreviewed, "interval", minutes=30, next_run_time=now() + timedelta(minutes=3),
                      max_instances=1, coalesce=True, id="review_recheck")
    # 停止中に受けた「待って」「レビューしない」などを、巡回・利用完了の処理より先に反映する
    try:
        await engine.catch_up()
    except Exception:  # noqa: BLE001
        log.exception("起動時の取りこぼしの回収に失敗")
    sched.start()

    # 表示の変更を反映するため、進行中の予約の親メッセージを最新の形に更新する
    for row in db.all("SELECT id FROM reservations WHERE state<>'CLOSED' AND slack_ts IS NOT NULL"):
        try:
            await engine.refresh_parent(row["id"])
        except Exception as e:  # noqa: BLE001
            log.warning("親メッセージの更新に失敗: %s", e)

    mode = "🧪 DRY_RUN（テスト用チャンネル・最後のボタンは押さない）" if cfg.dry_run else "本番"
    if mock:
        mins = cfg.remind_interval_minutes
        mode = f"🧪 模擬モード（管理画面には触れません。リマインド間隔 {mins} 分）"
    # 異常終了→自動再起動を繰り返しているときは、起動のたびに投稿しない
    last_start, last_exit = from_iso(db.kv_get("started_at")), db.kv_get("last_exit")
    if not (last_exit == "crash" and last_start and now() - last_start < timedelta(minutes=30)):
        await engine.post(text=f"🟢 予約対応システムを起動しました（{mode}、{now():%m/%d %H:%M}）")
    db.kv_set("started_at", iso(now()))
    db.kv_set("last_exit", "running")
    handler = AsyncSocketModeHandler(app, secrets.get("SLACK_APP_TOKEN"))
    crashed: BaseException | None = None
    try:
        await handler.start_async()
    except (KeyboardInterrupt, asyncio.CancelledError):
        raise
    except Exception as e:  # noqa: BLE001
        crashed = e
        log.exception("常駐プロセスが異常終了しました")
        raise
    finally:
        sched.shutdown(wait=False)
        if crashed is None:
            await deadman_cancel(engine)
            db.kv_set("last_exit", "clean")
            try:
                await engine.post(text=f"⏹ 予約対応システムを停止しました（{now():%m/%d %H:%M}）")
            except Exception:  # noqa: BLE001
                pass
        else:
            # 異常終了のときは停止検知の予約を残す（再起動できなければ警告が出る）
            db.kv_set("last_exit", "crash")
            try:
                await engine.post(text=f"⚠️ 予約対応システムが異常終了しました（{type(crashed).__name__}: "
                                       f"{secrets.redact(str(crashed))[:200]}）。5分以内に自動で再起動を試みます。")
            except Exception:  # noqa: BLE001
                pass
        await pool.stop()


async def ib_review_sweep(engine: Orchestrator) -> None:
    """IB の「投稿前のレビュー」に残っている予約を、利用完了の処理に回す（取りこぼし対策）"""
    try:
        pending = await engine.ib.review_pending_ids()
    except Exception as e:  # noqa: BLE001
        log.warning("IB レビュー一覧の取得に失敗: %s", e)
        return
    for pid in pending:
        r = engine.db.reservation_by_pid(Platform.IB.value, pid)
        if r and r["state"] == "CONFIRMED":
            try:
                await engine.complete(r["id"], "review_sweep")
            except Exception:  # noqa: BLE001
                log.exception("IB レビューの回収に失敗 %s", pid)
