"""DRY_RUN の結合テスト用に「ポーリングがこれを見つけた」イベントを投入する。

ブラウザのプロファイルは常駐プロセスが使っているため、ここでは DB にコマンドを積むだけにし、
常駐プロセス（app.process_commands）が5秒以内に実行する。DRY_RUN のときだけ使える。

  uv run barnull simulate new sm 6271768 [--text "シーシャは使えますか？"]
  uv run barnull simulate guest-message ib 8797784002 --text "ありがとうございました！"
  uv run barnull simulate manual-sent sm 6271768
  uv run barnull simulate complete sm 6248079 [--exit "気になる点があります"]
  uv run barnull simulate cancel ib 8797784002
"""

from __future__ import annotations

import argparse
import json
import logging
from datetime import timedelta

from .config import Config
from .db import DB
from .models import Message, Platform, ResState
from .textutil import iso, now

log = logging.getLogger(__name__)


async def simulate(cfg: Config, args: argparse.Namespace) -> None:
    if not cfg.dry_run:
        raise SystemExit("simulate は DRY_RUN のときだけ使えます（config.toml の dry_run = true）")
    db = DB(cfg.db_path)
    cmd = {"event": args.event, "platform": args.platform, "pid": args.platform_id, "text": args.text,
           "exit": getattr(args, "exit", None), "variant": getattr(args, "variant", None),
           "kind": getattr(args, "kind", None)}
    db.kv_set(f"cmd:{now():%Y%m%d%H%M%S%f}", json.dumps(cmd, ensure_ascii=False))
    print(f"投入しました（常駐プロセスが5秒以内に実行します）: {cmd}")


async def execute(engine, cmd: dict) -> None:
    if not engine.cfg.dry_run:
        log.warning("DRY_RUN でないため simulate コマンドを無視: %s", cmd)
        return
    platform = Platform(cmd["platform"])
    pid = cmd["pid"]
    event = cmd["event"]
    db = engine.db
    log.info("simulate: %s", cmd)
    if engine.mock is not None:
        await _execute_mock(engine, platform, pid, event, cmd)
        return
    row = db.reservation_by_pid(platform.value, pid)

    if event == "new":
        d = await engine.adapter(platform).get_detail(pid)
        if cmd.get("text"):
            d.messages.append(Message(role="guest", text=cmd["text"], at=now(), index=len(d.messages)))
        if row is not None:
            # 既に取り込み済みの予約を「新しく届いた」状態に戻して通知し直す（テスト専用）
            db.update("reservations", row["id"], state=ResState.NOTIFIED.value, template_sent_at=None, slack_ts=None,
                      hold_since=None, next_remind_at=iso(now() + timedelta(minutes=engine.cfg.remind_interval_minutes)))
            db.run("DELETE FROM thread_cursor WHERE platform=? AND thread_key=?", platform.value,
                   (d.thread_id or row["thread_id"]) if platform is Platform.IB else pid)
            db.run("DELETE FROM inquiries WHERE reservation_id=?", row["id"])
            rid = row["id"]
            await engine.ensure_parent(rid, note="🧪 (simulate) 新規予約として通知し直しました")
            thread_key = (d.thread_id or row["thread_id"]) if platform is Platform.IB else pid
            if thread_key:
                # 既存の発言は処理済みにし、--text の発言だけを予約時の問い合わせとして扱う
                engine._mark_processed(platform, thread_key, d.messages[:-1] if cmd.get("text") else d.messages)
                await engine.process_guest_messages(rid, d.messages, thread_key, initial=True)
        else:
            await engine.on_new(platform, pid, trigger="simulate", detail=d)
        return

    if row is None:
        log.warning("simulate: 予約 %s %s が未登録です", platform.value, pid)
        return
    rid = row["id"]
    if event == "guest-message":
        d = await engine.adapter(platform).get_detail(pid)
        d.messages.append(Message(role="guest", text=cmd.get("text") or "ありがとうございました！", at=now(),
                                  index=len(d.messages)))
        thread_key = (d.thread_id or row["thread_id"] or f"sim-{pid}") if platform is Platform.IB else pid
        await engine.ensure_parent(rid)
        await engine.process_guest_messages(rid, d.messages, thread_key)
    elif event == "manual-sent":
        db.kv_set(f"sim_manual_sent:{rid}", "1")
        db.update("reservations", rid, next_remind_at=iso(now()))
    elif event == "complete":
        db.update("reservations", rid, state=ResState.CONFIRMED.value,
                  review_posted_at=None, exit_status=cmd.get("exit"), exit_detail=None)
        if cmd.get("exit"):
            db.kv_set(f"sim_exit:{rid}", cmd["exit"])
        await engine.ensure_parent(rid)
        await engine.complete(rid, "simulate")
    elif event == "cancel":
        await engine.ensure_parent(rid)
        await engine.close(rid, "キャンセル（シミュレーション）")


async def _execute_mock(engine, platform: Platform, pid: str, event: str, cmd: dict) -> None:
    """模擬モード: 管理画面の代わりにメモリ上の模擬データを動かす"""
    store, db = engine.mock, engine.db
    if event == "new":
        if pid in ("auto", "new"):
            pid = f"TEST-{platform.value.upper()}-{now():%H%M%S}"
        if db.reservation_by_pid(platform.value, pid):
            log.warning("simulate: %s は登録済みです。別の ID を指定してください", pid)
            return
        d = store.create(platform, pid, cmd.get("text"), cmd.get("variant"))
        await engine.on_new(platform, pid, trigger="simulate", detail=d)
        return
    if event == "fail-next":
        store.fail_next[pid] = cmd.get("kind") or "action"
        log.info("simulate: %s の次の書き込み操作を失敗させます（%s）", pid, store.fail_next[pid])
        return
    row = db.reservation_by_pid(platform.value, pid)
    if row is None:
        log.warning("simulate: 予約 %s %s が未登録です", platform.value, pid)
        return
    rid = row["id"]
    if event == "guest-message":
        store.add_message(platform, pid, "guest", cmd.get("text") or "ありがとうございました！")
        d = await engine.adapter(platform).get_detail(pid)
        thread_key = d.thread_id if platform is Platform.IB else pid
        await engine.process_guest_messages(rid, d.messages, thread_key)
    elif event == "manual-sent":
        store.mark_template_sent(platform, pid, engine.cfg.sent_marker)
        db.update("reservations", rid, next_remind_at=iso(now()))
        await engine.tick_reminders()
    elif event == "complete":
        # 利用時間はそのまま（通知に本来の時刻を出す）。complete() は時刻を見ずに利用完了の処理をする
        d = store.get(platform, pid)
        d.exit_status = cmd.get("exit") or ("問題なく退室しました" if platform is Platform.SM else None)
        if cmd.get("exit") and "問題なく" not in cmd["exit"]:
            d.exit_detail = "備品・設備に不備があります：グラスを1つ割ってしまいました（テスト）"
        db.update("reservations", rid, exit_status=d.exit_status, exit_detail=d.exit_detail)
        await engine.complete(rid, "simulate")
    elif event == "cancel":
        store.get(platform, pid).status = "キャンセル"
        await engine.close(rid, "キャンセル（シミュレーション）")
    elif event == "approve-request":
        store.get(platform, pid).status = "予約確定"
        await engine._check_platform(rid)
