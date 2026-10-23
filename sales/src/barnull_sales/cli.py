"""手元で使うコマンド（sales フォルダで `uv run barnull-sales ...`）。

  asana-setup --env test|prod [--dry-run]   Asana のプロジェクト・セクション・カスタムフィールドを作る（asana_ids.json に保存）
  restore-from-asana --env test|prod [--dry-run]   Firestore を失ったとき、Asana のタスクから伝票の状態を作り直す
  try "メッセージ" [--real-slack] [--real-asana] [--real-claude]
        @Bot メッセージ1通の処理を手元で試す（既定はすべて偽物。--real-* で本物を使う。Slack と Asana はテスト先）
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import replace

from .config import ROOT, secret, settings


def cmd_asana_setup(args: argparse.Namespace) -> None:
    from .asana import AsanaAPI, setup

    s = settings()
    name = s.asana_project_name_test if args.env == "test" else s.asana_project_name_prod
    setup(AsanaAPI(secret("ASANA_PAT")), workspace=s.asana_workspace, team_name=s.asana_team_name,
          project_name=name, env=args.env, ids_path=ROOT / "asana_ids.json", dry_run=args.dry_run)


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
            elif m := re.match(r"備考[:：]?\s*(.*)", line):
                cur["note"] = "" if m[1] in ("なし", "") else m[1]
        if cur:
            entries.append(cur)
        if not entries:
            return Parsed(kind="other", confidence=0.9)
        return Parsed(kind="slip_content", confidence=0.9,
                      entries=[Entry(month=e.get("month"), day=e.get("day"), customer_name=e.get("customer_name"),
                                     amount=e.get("amount"), note=e.get("note", "")) for e in entries])


def cmd_try(args: argparse.Namespace) -> None:
    from .asana import AsanaTracker
    from .deps import Deps
    from .extract import ClaudeExtractor
    from .process import handle_event
    from .slack import SlackNotifier
    from .store import MemoryStore
    from .testing import FakeNotifier, FakeTracker

    s = replace(settings(), dry_run=True, allowed_group_ids=("Ctest",))
    deps = Deps(
        settings=s, store=MemoryStore(),
        extractor=(ClaudeExtractor(secret("ANTHROPIC_API_KEY"), s.model_primary, s.model_fallback, s.effort)
                   if args.real_claude else _FakeExtractor()),
        notifier=SlackNotifier(secret("SLACK_BOT_TOKEN")) if args.real_slack else FakeNotifier(),
        tracker=AsanaTracker(secret("ASANA_PAT"), s.asana_ids, "test") if args.real_asana else FakeTracker(),
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
    args = p.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args.func(args)


if __name__ == "__main__":
    main()
