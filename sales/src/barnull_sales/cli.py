"""手元で使うコマンド（sales フォルダで `uv run barnull-sales ...`）。

  asana-setup --env test|prod [--dry-run]   Asana のプロジェクト・セクション・カスタムフィールドを作る（asana_ids.json に保存）
  asana-setup-labor --env test|prod [--dry-run]   勤怠・スタッフ台帳・給与のプロジェクトと欄を作る（非公開）
  restore-from-asana --env test|prod [--dry-run]   Firestore を失ったとき、Asana のタスクから伝票の状態を作り直す
  try "メッセージ" [--real-slack] [--real-asana] [--real-claude]
        @Bot メッセージ1通の処理を手元で試す（既定はすべて偽物。--real-* で本物を使う。Slack と Asana はテスト先）

備品・消耗品（マネフォのカード「BAR NULL」→ Asana「Bar null 備品消耗品」。本番はクラウドの reconcile ジョブが3時間ごとに実行）:
  supplies-setup --env test|prod [--dry-run]   プロジェクト・セクション・欄を作る（非公開）
  supplies-sync [--env test|prod] [--since YYYY-MM-DD] [--dry-run]   手元で1回同期する（初回の取り込みや確認用）

固定費（経営ダッシュボード → Asana「Bar null 固定費」。手元で必要なときだけ実行。定期実行はしない）:
  fixed-setup --env test|prod [--dry-run]    固定費のプロジェクト・セクション・欄を作る（非公開）
  fixed-import --env test|prod [--dry-run]   経営ダッシュボードの固定費を、金額の変わり目ごとにまとめて写す（冪等）
  fixed-show --env test|prod [--until YYYY-MM] [--months N]   月ごとの固定費を計算して表示する（確認用）

仕入（NOS → Asana「Bar null 仕入」。Playwright を使うので `uv run --group purchase barnull-sales ...`）:
  purchase-setup --env test|prod [--dry-run]   仕入のプロジェクト・セクション・欄を作る（非公開）
  purchase-login         ブラウザの画面を開き、本人が NOS にログインする（ログイン状態を PC に保存）
  purchase-set-login     自動の再ログインに使う NOS の ID・パスワードを資格情報マネージャーに登録する（本人が入力）
  purchase-fetch [--env test|prod] [--initial | --since YYYY-MM-DD] [--dry-run] [--no-slack] [--headed] [--rename-drafts]
        仕入実績一覧を読んで Asana に合わせ、Slack に知らせる（既定の env は sales.toml の [purchase] dry_run）
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import replace

from .config import config_path, secret, settings


def cmd_asana_setup(args: argparse.Namespace) -> None:
    from .asana import AsanaAPI, setup

    s = settings()
    name = s.asana_project_name_test if args.env == "test" else s.asana_project_name_prod
    setup(AsanaAPI(secret("ASANA_PAT")), workspace=s.asana_workspace, team_name=s.asana_team_name,
          project_name=name, env=args.env, ids_path=config_path().parent / "asana_ids.json", dry_run=args.dry_run,
          pos=not s.line_enabled)


def cmd_asana_setup_labor(args: argparse.Namespace) -> None:
    from .asana import AsanaAPI
    from .labor_asana import setup

    s = settings()
    setup(AsanaAPI(secret("ASANA_PAT")), workspace=s.asana_workspace, team_name=s.asana_team_name, env=args.env,
          ids_path=config_path().parent / "asana_ids.json", dry_run=args.dry_run)


def cmd_purchase_setup(args: argparse.Namespace) -> None:
    from .asana import AsanaAPI
    from .purchase_asana import setup

    s = settings()
    setup(AsanaAPI(secret("ASANA_PAT")), workspace=s.asana_workspace, team_name=s.asana_team_name, env=args.env,
          ids_path=config_path().parent / "asana_ids.json", dry_run=args.dry_run)


def cmd_supplies_setup(args: argparse.Namespace) -> None:
    from .asana import AsanaAPI
    from .supplies_asana import setup

    s = settings()
    setup(AsanaAPI(secret("ASANA_PAT")), workspace=s.asana_workspace, team_name=s.asana_team_name, env=args.env,
          ids_path=config_path().parent / "asana_ids.json", dry_run=args.dry_run)


def cmd_supplies_sync(args: argparse.Namespace) -> None:
    """マネフォのカード明細を読んで、Asana に合わせる（reconcile ジョブのステップと同じ処理を手元で動かす）。"""
    from datetime import date, timedelta

    from . import supplies
    from .mf import MoneyForward
    from .supplies_asana import AsanaSuppliesTracker

    s = settings()
    cfg = s.supplies
    env = args.env or cfg.asana_env
    start = date.fromisoformat(args.since) if args.since else date.today() - timedelta(days=cfg.lookback_days)
    end = date.today()
    mf = MoneyForward(secret("MF_API_KEY"), s.mf.office_code, s.mf.bank_subaccount)
    txns = mf.card_txns(start, end, cfg.card_subaccount)
    tracker = AsanaSuppliesTracker(secret("ASANA_PAT"), s.asana_ids, env)
    st = supplies.sync(tracker, txns, start.isoformat(), end.isoformat(), cfg, dry_run=args.dry_run, log_fn=print)
    print(f"明細 {len(txns)}件 / 新規 {st.created}（¥{st.new_amount:,}）・更新 {st.updated}・変化なし {st.unchanged}・対象外 {st.excluded}")


def cmd_fixed_setup(args: argparse.Namespace) -> None:
    from .asana import AsanaAPI
    from .fixed_asana import setup

    s = settings()
    setup(AsanaAPI(secret("ASANA_PAT")), workspace=s.asana_workspace, team_name=s.asana_team_name, env=args.env,
          ids_path=config_path().parent / "asana_ids.json", dry_run=args.dry_run)


def cmd_fixed_import(args: argparse.Namespace) -> None:
    """経営ダッシュボードの固定費を読み、金額の変わり目ごとにまとめて「Bar null 固定費」に写す（冪等。何度実行しても同じ）。"""
    from . import fixed
    from .asana import AsanaAPI
    from .fixed_asana import AsanaFixedTracker, read_dashboard

    token = secret("ASANA_PAT")
    rows = read_dashboard(AsanaAPI(token))
    plan = fixed.plan_from_dashboard(rows)
    print(f"経営ダッシュボードの固定費 {len(rows)}件（金額あり {sum(r['amount'] is not None for r in rows)}件）"
          f" → {len(plan.records)}タスク")
    if plan.unknown:
        print("知らない項目（写さない。fixed.py の _ITEMS に足す）:", "、".join(plan.unknown))
    for c in plan.conflicts:
        print("同じ月に金額が2つ（あとの値を使う）:", c)
    tracker = AsanaFixedTracker(token, settings().asana_ids, args.env)
    st = fixed.sync(tracker, plan.records, dry_run=args.dry_run)
    print(f"作成 {st.created}・更新 {st.updated}・変化なし {st.unchanged}・対象外 {st.excluded}")


def cmd_fixed_show(args: argparse.Namespace) -> None:
    """「Bar null 固定費」から、月ごとの固定費を計算して表示する（確認用。書き込まない）。"""
    from . import fixed
    from .fixed_asana import AsanaFixedTracker

    tasks = AsanaFixedTracker(secret("ASANA_PAT"), settings().asana_ids, args.env).list_tasks()
    for i in range(args.months - 1, -1, -1):
        m = fixed.add_months(args.until, -i)
        a = fixed.month_amounts(tasks, m)
        if not a:
            print(f"{m}  未設定")
            continue
        print(f"{m}  合計 ¥{sum(v['amount'] for v in a.values()):,}")
        for n, v in sorted(a.items(), key=lambda x: -x[1]["amount"]):
            print(f"    {v['category']:<8} {n:<20} ¥{v['amount']:>10,}{'（実績）' if v['kind'] == '実績' else ''}")


def cmd_purchase_login(args: argparse.Namespace) -> None:
    from .purchase_nos import login_interactive
    from .purchase_run import PROFILE

    ok = login_interactive(PROFILE)
    print("ログインを保存しました。" if ok else "ログインを確認できませんでした。もう一度実行してください。")
    sys.exit(0 if ok else 1)


def cmd_purchase_set_login(args: argparse.Namespace) -> None:
    """NOS の ID・パスワードを、本人が入力ウィンドウで資格情報マネージャーに登録する（画面にもログにも出さない）。"""
    import getpass

    import keyring

    s = settings()
    import unicodedata

    # 日本語入力がオンのままだと全角になるので、半角に直す（2026-11-15 全角で登録されてログインできなかった）
    account = unicodedata.normalize("NFKC", input("NOS のアカウント（数字）: ")).strip()
    password = unicodedata.normalize("NFKC", getpass.getpass("NOS のパスワード（表示されません。半角で）: ")).strip()
    if not account or not password:
        sys.exit("空のため登録しませんでした")
    if not (account.isascii() and password.isascii()):
        sys.exit("半角の英数字にできない文字があるため登録しませんでした。日本語入力をオフにして、もう一度入力してください")
    keyring.set_password(s.keyring_service, "NOS_ACCOUNT", account)
    keyring.set_password(s.keyring_service, "NOS_PASSWORD", password)
    print(f"資格情報マネージャー（{s.keyring_service}）に NOS_ACCOUNT・NOS_PASSWORD を登録しました。")


def cmd_purchase_fetch(args: argparse.Namespace) -> None:
    import logging
    from datetime import date
    from pathlib import Path

    from .purchase_run import run

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)   # リクエストの URL を1行ずつ出さない
    s = settings()
    env = args.env or s.purchase.asana_env
    since = (date.fromisoformat(s.purchase.initial_since) if args.initial
             else date.fromisoformat(args.since) if args.since else None)
    today = date.fromisoformat(args.today) if args.today else date.today()
    r = run(s, env=env, today=today, since=since, headless=not args.headed, dry_run=args.dry_run,
            notify=not args.no_slack, from_file=Path(args.from_file) if args.from_file else None,
            rename_drafts=args.rename_drafts)
    print(r.text)
    sys.exit(r.exit_code)


def _firestore_with_gcloud():
    """この PC の gcloud のログイン（ご本人のアカウント）で Firestore に接続する。"""
    import os
    import subprocess

    from google.cloud import firestore
    from google.oauth2.credentials import Credentials

    g = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Google", "Cloud SDK", "google-cloud-sdk", "bin", "gcloud.cmd")
    token = subprocess.run([g, "auth", "print-access-token"], capture_output=True, text=True, check=True).stdout.strip()
    return firestore.Client(project=settings().project, credentials=Credentials(token))


def cmd_restore(args: argparse.Namespace) -> None:
    from .asana import AsanaAPI
    from .restore import restore

    s = settings()
    project = s.asana_ids[args.env]["project"]
    stats = restore(AsanaAPI(secret("ASANA_PAT")), project, _firestore_with_gcloud(), args.dry_run)
    print(json.dumps(stats, ensure_ascii=False))


def cmd_report(args: argparse.Namespace) -> None:
    from datetime import date

    from .asana import AsanaAPI
    from .report import build, fetch_rows, slack_text

    s = settings()
    api = AsanaAPI(secret("ASANA_PAT"))
    project = s.asana_ids[args.env]["project"]
    today = date.fromisoformat(args.date) if args.date else date.today()
    print(slack_text(build(fetch_rows(api, project), today), s.report_url, args.env == "test"))


class _FakeExtractor:
    """--real-claude を付けないときの簡易解析（「日付：」「顧客名：」「金額：」「備考：」の行だけ読む）。"""

    def judge_payer(self, bank_text, customer_name):
        return False, 0.0

    def parse(self, text, sent_at):
        import re

        from .extract import Entry, Parsed

        if m := re.search(r"勤務開始[:：]\s*(\d{1,2}[:：]\d{2}).*?勤務終了[:：]\s*(\d{1,2}[:：]\d{2})", text, re.S):
            d = re.search(r"日付[:：]\s*(\d+)/(\d+)", text)
            return Parsed(kind="attendance", confidence=0.9, attendance={
                "month": int(d[1]) if d else None, "day": int(d[2]) if d else None,
                "start": m[1].replace("：", ":"), "end": m[2].replace("：", ":")})
        entries, cur = [], {}
        for line in text.splitlines():
            line = line.strip("・ ")
            if m := re.match(r"日付[:：]\s*(\d+)/(\d+)", line):
                if cur:
                    entries.append(cur)
                cur = {"month": int(m[1]), "day": int(m[2])}
            elif m := re.match(r"顧客名[:：]\s*(.+?)(様|さま|さん)?$", line):
                cur["customer_name"] = m[1]
            elif m := re.match(r"金額[:：]\s*([\d,]+)", line):
                cur["amount"] = int(m[1].replace(",", ""))
            elif m := re.match(r"プロモーター(?:フラグ)?[:：]\s*(.*?)(様|さま|さん)?$", line):
                cur["promoter_name"] = "" if m[1] in ("なし", "") else m[1]
            elif m := re.match(r"備考[:：]?\s*(.*)", line):
                cur["note"] = "" if m[1] in ("なし", "") else m[1]
        if cur:
            entries.append(cur)
        if not entries:
            return Parsed(kind="other", confidence=0.9)
        return Parsed(kind="slip_content", confidence=0.9,
                      entries=[Entry(month=e.get("month"), day=e.get("day"), customer_name=e.get("customer_name"),
                                     amount=e.get("amount"), note=e.get("note", ""),
                                     promoter_name=e.get("promoter_name", "")) for e in entries])


def cmd_try(args: argparse.Namespace) -> None:
    from .asana import AsanaTracker
    from .deps import Deps
    from .extract import ClaudeExtractor
    from .labor_asana import AsanaLaborTracker
    from .process import handle_event
    from .slack import SlackNotifier
    from .store import MemoryStore
    from .testing import FakeLaborTracker, FakeNotifier, FakeTracker

    s = replace(settings(), dry_run=True, allowed_group_ids=("Ctest",))
    deps = Deps(
        settings=s, store=MemoryStore(),
        extractor=(ClaudeExtractor(secret("ANTHROPIC_API_KEY"), s.model_primary, s.model_fallback, s.effort)
                   if args.real_claude else _FakeExtractor()),
        notifier=SlackNotifier(secret("SLACK_BOT_TOKEN")) if args.real_slack else FakeNotifier(),
        tracker=AsanaTracker(secret("ASANA_PAT"), s.asana_ids, "test") if args.real_asana else FakeTracker(),
        labor=(AsanaLaborTracker(secret("ASANA_PAT"), s.asana_ids, "test")
               if args.real_asana and "test" in s.asana_ids.get("labor", {}) else FakeLaborTracker()),
    )
    text = f"{s.bot_mention_name} {args.text}"
    event = {"type": "message", "webhookEventId": f"try-{int(time.time())}", "timestamp": int(time.time() * 1000),
             "source": {"type": "group", "groupId": "Ctest", "userId": "Utest"},
             "message": {"type": "text", "id": "m1", "text": text}}
    result = handle_event(event, "message", deps)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if isinstance(deps.notifier, FakeNotifier):
        for p in deps.notifier.posts:
            print("--- Slack" + (" (スレッド)" if p["thread_ts"] else ""))
            print(p["text"])
    for slip_id in result.get("slip_ids", []):
        print(json.dumps(deps.store.get_slip(slip_id).to_dict(), ensure_ascii=False, indent=2)[:1500])


def main() -> None:
    p = argparse.ArgumentParser(prog="barnull-sales")
    sub = p.add_subparsers(required=True)
    a = sub.add_parser("asana-setup")
    a.add_argument("--env", choices=["test", "prod"], required=True)
    a.add_argument("--dry-run", action="store_true")
    a.set_defaults(func=cmd_asana_setup)
    al = sub.add_parser("asana-setup-labor")
    al.add_argument("--env", choices=["test", "prod"], required=True)
    al.add_argument("--dry-run", action="store_true")
    al.set_defaults(func=cmd_asana_setup_labor)
    r = sub.add_parser("restore-from-asana")
    r.add_argument("--env", choices=["test", "prod"], required=True)
    r.add_argument("--dry-run", action="store_true")
    r.set_defaults(func=cmd_restore)
    w = sub.add_parser("report")
    w.add_argument("--env", choices=["test", "prod"], required=True)
    w.add_argument("--date", help="週報を出す日（YYYY-MM-DD。省略時は今日）")
    w.set_defaults(func=cmd_report)
    t = sub.add_parser("try")
    t.add_argument("text")
    t.add_argument("--real-slack", action="store_true")
    t.add_argument("--real-asana", action="store_true")
    t.add_argument("--real-claude", action="store_true")
    t.set_defaults(func=cmd_try)
    ps = sub.add_parser("purchase-setup")
    ps.add_argument("--env", choices=["test", "prod"], required=True)
    ps.add_argument("--dry-run", action="store_true")
    ps.set_defaults(func=cmd_purchase_setup)
    ss = sub.add_parser("supplies-setup")
    ss.add_argument("--env", choices=["test", "prod"], required=True)
    ss.add_argument("--dry-run", action="store_true")
    ss.set_defaults(func=cmd_supplies_setup)
    sy = sub.add_parser("supplies-sync")
    sy.add_argument("--env", choices=["test", "prod"], help="省略時は [supplies] dry_run に従う")
    sy.add_argument("--since", help="読む期間の始まり（YYYY-MM-DD。省略時は lookback_days 前。初回は 2026-08-01）")
    sy.add_argument("--dry-run", action="store_true", help="Asana に書かない（マネフォは読む）")
    sy.set_defaults(func=cmd_supplies_sync)
    fs = sub.add_parser("fixed-setup")
    fs.add_argument("--env", choices=["test", "prod"], required=True)
    fs.add_argument("--dry-run", action="store_true")
    fs.set_defaults(func=cmd_fixed_setup)
    fi = sub.add_parser("fixed-import")
    fi.add_argument("--env", choices=["test", "prod"], required=True)
    fi.add_argument("--dry-run", action="store_true", help="「Bar null 固定費」に書かない（経営ダッシュボードは読む）")
    fi.set_defaults(func=cmd_fixed_import)
    fw = sub.add_parser("fixed-show")
    fw.add_argument("--env", choices=["test", "prod"], required=True)
    fw.add_argument("--until", default=time.strftime("%Y-%m"), help="最後の月（YYYY-MM。既定は今月）")
    fw.add_argument("--months", type=int, default=6)
    fw.set_defaults(func=cmd_fixed_show)
    sub.add_parser("purchase-login").set_defaults(func=cmd_purchase_login)
    sub.add_parser("purchase-set-login").set_defaults(func=cmd_purchase_set_login)
    pf = sub.add_parser("purchase-fetch")
    pf.add_argument("--env", choices=["test", "prod"])
    g = pf.add_mutually_exclusive_group()
    g.add_argument("--initial", action="store_true", help="[purchase] initial_since から読む（初回の取り込み）")
    g.add_argument("--since", help="読む期間の始まり（YYYY-MM-DD）")
    pf.add_argument("--today", help="今日の日付として扱う日（試験用）")
    pf.add_argument("--dry-run", action="store_true", help="Asana・Slack に書かない（NOS は読む）")
    pf.add_argument("--no-slack", action="store_true")
    pf.add_argument("--headed", action="store_true", help="ブラウザの画面を出す")
    pf.add_argument("--rename-drafts", action="store_true", help="案（自動）のままの商品マスタの表示名を Claude で作り直す")
    pf.add_argument("--from-file", help="NOS を読まず、保存したファイル（samples/purchases/nos_*.json）を使う")
    pf.set_defaults(func=cmd_purchase_fetch)
    args = p.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args.func(args)


if __name__ == "__main__":
    main()
