"""コマンドライン: uv run barnull <command>

  run                    常駐プロセス（Slack・ポーリング・リマインド）
  setup                  初回セットアップ（未登録の秘密情報の入力 → SM・IB へのログイン）
  set-secret NAME        資格情報マネージャーに秘密情報を登録（入力は画面に表示しない）
  secrets                登録状況の一覧（値は表示しない）
  login sm|ib            画面付きのブラウザでログインし、ログイン状態を保存（常駐プロセスは止めてから）
  capture [sm|ib]        管理画面の構造を個人情報を伏せて tests/fixtures/pages に保存
  backfill               既存の予約・ゲスト履歴・過去の返信を取り込む（Slack には投稿しない）
  simulate ...           DRY_RUN の結合テスト用に、ポーリングの検知イベントを投入
  doctor                 設定・秘密情報・Slack・ログインの確認
"""

from __future__ import annotations

import argparse
import asyncio
import sys


def _cfg(mock: bool = False, log_name: str = "barnull-cli.log"):
    import dataclasses

    from .config import load_config
    from .logsetup import setup_logging
    from .secrets import load_redactions

    cfg = load_config()
    if mock:
        # 模擬モードは本番のデータ（data/barnull.db）と分ける
        cfg = dataclasses.replace(cfg, data_dir=cfg.data_dir / "mock")
    elif cfg.dry_run:
        # DRY_RUN（テスト用チャンネル）の記録も本番と分ける。本番に切り替えたら初回の取り込みからやり直す
        cfg = dataclasses.replace(cfg, data_dir=cfg.data_dir / "dryrun")
    load_redactions()
    setup_logging(cfg, filename=log_name)
    return cfg


LOCK_HELP = ("常駐プロセス（barnull run）が動いているため実行できません（ブラウザのログイン状態を共有しているため）。\n"
             "先に常駐を止めてください:  Stop-ScheduledTask -TaskName BarNullOps\n"
             "（止めている間に5分ごとの起動確認が来ても、このコマンドの実行中は常駐は起動しません）\n"
             "終わったら:  Start-ScheduledTask -TaskName BarNullOps")


def _browser_lock(cfg):
    """ブラウザのプロファイルを使うコマンドは、常駐と同じロックを取る（同時に開くと壊れる・失敗する）"""
    from .app import SingleInstance

    return SingleInstance(cfg.global_lock, LOCK_HELP)


def cmd_set_secret(args: argparse.Namespace) -> None:
    from . import secrets
    from .secret_input import ask

    name = args.name.upper()
    if name not in secrets.NAMES:
        sys.exit(f"不明な名前です: {name}（{', '.join(secrets.NAMES)}）")
    print(f"{name} の入力ウィンドウを開きます（Ctrl+V で貼り付けられます）…")
    value = ask(name, SETUP_HELP.get(name, ""))
    if not value:
        sys.exit("登録しませんでした")
    secrets.set_(name, value)
    print(f"{name} を登録しました")


def cmd_secrets(_: argparse.Namespace) -> None:
    from . import secrets

    for name, ok in secrets.status().items():
        print(f"  {'✓' if ok else '✗'} {name}")


def cmd_login(args: argparse.Namespace) -> None:
    from .models import Platform
    from .platforms.browser import BrowserPool, LoginRequired

    cfg = _cfg()
    platform = Platform(args.platform)
    lock = _browser_lock(cfg)
    lock.__enter__()
    url = {
        Platform.SM: f"https://dashboard.spacemarket.com/{cfg.sm_host_id}/inbox",
        Platform.IB: "https://www.instabase.jp/partners/bookings",
    }[platform]

    async def main() -> None:
        pool = BrowserPool(cfg, headless=False)
        try:
            async with pool.page(platform) as page:
                await page.goto(url, wait_until="domcontentloaded")
                await page.wait_for_timeout(2000)
                if await pool._needs_login(platform, page):
                    try:
                        await pool.auto_login(platform, page)
                        print("保存済みの ID・パスワードでログインしました")
                    except (LoginRequired, Exception) as e:  # noqa: BLE001
                        print(f"自動ログインできませんでした: {e}")
                        print("開いたブラウザで手動でログインしてください（最大10分待ちます）…")
                    for _ in range(600):
                        try:
                            if not await pool._needs_login(platform, page):
                                break
                        except Exception:  # noqa: BLE001  （画面の切り替わり中）
                            pass
                        await page.wait_for_timeout(1000)
                await page.goto(url, wait_until="domcontentloaded")
                await page.wait_for_timeout(2000)
                if await pool._needs_login(platform, page):
                    sys.exit("ログインできていません")
                print(f"{platform.label}: ログイン状態を保存しました（{cfg.browser_dir / platform.value}）")
        finally:
            await pool.stop()

    try:
        asyncio.run(main())
    finally:
        lock.__exit__(None, None, None)


SETUP_HELP = {
    "SLACK_BOT_TOKEN": "Slack の Bot User OAuth Token（xoxb- で始まる。api.slack.com/apps → Bar null 予約対応 → OAuth & Permissions）",
    "SLACK_APP_TOKEN": "Slack の App-Level Token（xapp- で始まる。Basic Information → App-Level Tokens → socket）",
    "ANTHROPIC_API_KEY": "Claude API キー（sk-ant- で始まる。console.anthropic.com → API Keys）",
    "KEYBOX_CODE": "キーボックスの暗証番号",
    "SM_EMAIL": "スペースマーケットのログイン用メールアドレス",
    "SM_PASSWORD": "スペースマーケットのパスワード",
    "IB_EMAIL": "インスタベースのログイン用メールアドレス",
    "IB_PASSWORD": "インスタベースのパスワード",
}


def cmd_setup(args: argparse.Namespace) -> None:
    """未登録の秘密情報を順に入力してもらい、続けて SM・IB にログインする"""
    from . import secrets
    from .secret_input import ask

    only = [n.upper() for n in (args.names or [])]
    print("未登録の秘密情報を登録します。項目ごとに入力ウィンドウが開きます（Ctrl+V で貼り付け可、伏せ字で表示）。")
    print("今は分からない項目は「飛ばす」を押してください。\n")
    for name, ok in secrets.status().items():
        if only and name not in only:
            continue
        if ok and not only:
            print(f"  ✓ {name}（登録済み）")
            continue
        value = ask(name, SETUP_HELP[name])
        if value:
            secrets.set_(name, value)
            print(f"  ✓ {name} を登録しました")
        else:
            print(f"  - {name} は飛ばしました")
    print()
    if not args.no_login:
        for pf in ("sm", "ib"):
            ans = input(f"{pf.upper()} にログインしますか？ [Y/n]: ").strip().lower()
            if ans in ("", "y", "yes"):
                cmd_login(argparse.Namespace(platform=pf))
    print("\nセットアップが終わりました。登録状況:")
    cmd_secrets(argparse.Namespace())


def cmd_capture(args: argparse.Namespace) -> None:
    from .platforms.capture import capture

    cfg = _cfg()
    with _browser_lock(cfg):
        out = asyncio.run(capture(cfg, args.platform))
    print(f"保存先: {out}")


def cmd_run(args: argparse.Namespace) -> None:
    from .app import run

    cfg = _cfg(mock=args.mock, log_name="barnull.log")
    asyncio.run(run(cfg, mock=args.mock))


def cmd_backfill(args: argparse.Namespace) -> None:
    from .backfill import backfill

    cfg = _cfg()
    with _browser_lock(cfg):
        asyncio.run(backfill(cfg, announce_unsent=args.announce_unsent))


def cmd_simulate(args: argparse.Namespace) -> None:
    from .simulate import simulate

    cfg = _cfg(mock=args.mock)
    asyncio.run(simulate(cfg, args))


def cmd_doctor(_: argparse.Namespace) -> None:
    from .doctor import doctor

    cfg = _cfg()
    with _browser_lock(cfg):
        asyncio.run(doctor(cfg))


def main() -> None:
    if sys.stdout is not None and sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    p = argparse.ArgumentParser(prog="barnull", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("run")
    s.add_argument("--mock", action="store_true", help="模擬モード（管理画面に触れず Slack 側の流れを試す。DRY_RUN のみ）")
    s.set_defaults(func=cmd_run)
    s = sub.add_parser("set-secret")
    s.add_argument("name")
    s.set_defaults(func=cmd_set_secret)
    sub.add_parser("secrets").set_defaults(func=cmd_secrets)
    s = sub.add_parser("setup")
    s.add_argument("names", nargs="*", help="指定した項目だけ（登録済みでも）入力し直す")
    s.add_argument("--no-login", action="store_true", help="最後の SM・IB へのログインを聞かない")
    s.set_defaults(func=cmd_setup)
    s = sub.add_parser("login")
    s.add_argument("platform", choices=["sm", "ib"])
    s.set_defaults(func=cmd_login)
    s = sub.add_parser("capture")
    s.add_argument("platform", nargs="?", choices=["sm", "ib"])
    s.set_defaults(func=cmd_capture)
    s = sub.add_parser("backfill")
    s.add_argument("--announce-unsent", action="store_true",
                   help="定型文が未送信の今後の予約を Slack に承認待ちとして投稿する")
    s.set_defaults(func=cmd_backfill)
    s = sub.add_parser("simulate")
    s.add_argument("event", choices=["new", "guest-message", "manual-sent", "complete", "cancel", "fail-next",
                                     "approve-request"])
    s.add_argument("platform", choices=["sm", "ib"])
    s.add_argument("platform_id")
    s.add_argument("--text", default=None, help="ゲストの発言（new では予約時の問い合わせ、guest-message では新着）")
    s.add_argument("--exit", default=None, help="complete の退室時状態（例: 気になる点があります）")
    s.add_argument("--mock", action="store_true", help="模擬モード（run --mock）で動いているプロセスに投入する")
    s.add_argument("--variant", choices=["inquiry", "request"], default=None,
                   help="new の種類（inquiry: SM の予約前の問い合わせ、request: IB の承認制リクエスト）")
    s.add_argument("--kind", choices=["action", "login"], default=None, help="fail-next の失敗の種類")
    s.set_defaults(func=cmd_simulate)
    sub.add_parser("doctor").set_defaults(func=cmd_doctor)
    args = p.parse_args()
    try:
        args.func(args)
    except (SystemExit, KeyboardInterrupt):
        raise
    except BaseException:
        # タスクスケジューラ（pythonw）では画面に何も出ないので、起動・実行時の例外をファイルに残す
        _write_crash_log()
        raise


def _write_crash_log() -> None:
    import traceback
    from datetime import datetime

    try:
        from .config import DATA_DIR
        from .secrets import redact

        d = DATA_DIR / "logs"
        d.mkdir(parents=True, exist_ok=True)
        with (d / "startup_error.log").open("a", encoding="utf-8") as fp:
            fp.write(f"\n===== {datetime.now():%Y-%m-%d %H:%M:%S} {' '.join(sys.argv[1:])}\n")
            fp.write(redact(traceback.format_exc()))
    except Exception:  # noqa: BLE001
        pass


if __name__ == "__main__":
    main()
