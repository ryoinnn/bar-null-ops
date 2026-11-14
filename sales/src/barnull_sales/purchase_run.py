"""仕入の取り込み1回分: NOS を読む → 伝票にまとめる → Asana に合わせる → Slack に知らせる。

本人の PC のタスクスケジューラから毎週月曜 9:30 に実行する（scripts/purchase/run_purchase.ps1 → `barnull-sales purchase-fetch`）。
失敗したら Slack に ★ 付きで知らせ、終了コード 2 を返す（スクリプトが PC の通知も出す）。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from .config import ROOT, Settings, keyring_value, secrets
from .purchase import NosFormatError, check_totals, fetch_range, group, parse_rows

log = logging.getLogger(__name__)

DATA_DIR = ROOT / "samples" / "purchases"      # git に入れない（.gitignore の sales/samples/）
PROFILE = DATA_DIR / "browser"
LOGIN_HELP = ("PC で `cd sales; uv run --group purchase barnull-sales purchase-set-login` を実行して NOS の ID・パスワードを登録し直すと、"
              "次から自動でログインします（パスワードを変えたときも同じ）")


@dataclass
class RunResult:
    ok: bool
    text: str


def _slack(s: Settings, env: str, text: str) -> None:
    from .slack import SlackNotifier

    token = secrets().get("SLACK_BOT_TOKEN")
    if not token:
        log.warning("Slack のトークンが無いため通知できません")
        return
    channel = s.slack_channel_test if env == "test" else s.slack_channel_prod
    SlackNotifier(token).post(channel, text)


def _save(since: date, until: date, result) -> Path:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = DATA_DIR / f"nos_{datetime.now():%Y%m%d_%H%M%S}.json"
    path.write_text(json.dumps({"since": since.isoformat(), "until": until.isoformat(), "header": result.header,
                                "rows": result.rows, "totals": result.totals.__dict__ if result.totals else None},
                               ensure_ascii=False), encoding="utf-8")
    return path


def run(s: Settings, *, env: str, today: date, since: date | None = None, headless: bool = True,
        dry_run: bool = False, notify: bool = True, from_file: Path | None = None,
        rename_drafts: bool = False) -> RunResult:
    from .purchase_asana import AsanaPurchaseTracker, sync
    from .purchase_names import suggest_names
    from .purchase_nos import LoginRequired, NosResult, fetch

    start, end = fetch_range(today, since)
    period = f"{start.month}/{start.day}〜{end.month}/{end.day}"
    tag = "［テスト］" if env == "test" else ""
    portal = (s.report_url + "#cost-purchase") if s.report_url else ""
    try:
        if from_file:
            raw = json.loads(from_file.read_text(encoding="utf-8"))
            from .purchase import Totals
            res = NosResult(raw["header"], raw["rows"], Totals(**raw["totals"]) if raw.get("totals") else None, "")
        else:
            import unicodedata

            def cred(name: str) -> str:   # 全角で登録されていても半角に直す
                return unicodedata.normalize("NFKC", keyring_value(name, s.keyring_service)).strip()

            res = fetch(PROFILE, start, end, headless=headless, account=cred("NOS_ACCOUNT"), password=cred("NOS_PASSWORD"))
            _save(start, end, res)
        lines = parse_rows(res.header, res.rows)
        vouchers = group(lines)
        mismatch = check_totals(vouchers, res.totals) if res.rows else []
        tracker = AsanaPurchaseTracker(secrets()["ASANA_PAT"], s.asana_ids, env)
        api_key = secrets().get("ANTHROPIC_API_KEY")
        st = sync(tracker, vouchers, start.isoformat(), end.isoformat(),
                  lambda items: suggest_names(items, api_key=api_key, model=s.purchase.model),
                  dry_run=dry_run, rename_drafts=rename_drafts, log=lambda m: log.info(m))
    except LoginRequired as e:
        text = f"★ {s.mention}{tag}仕入の取り込み（NOS）を止めました: {e}\n{LOGIN_HELP}"
        if notify and not dry_run:
            _slack(s, env, text)
        return RunResult(False, text)
    except NosFormatError as e:
        text = f"★ {s.mention}{tag}仕入の取り込み（NOS）を止めました: 画面の形が変わった可能性があります（{e}）"
        if notify and not dry_run:
            _slack(s, env, text)
        return RunResult(False, text)
    except Exception as e:  # noqa: BLE001  失敗は必ず知らせる
        log.exception("仕入の取り込みに失敗")
        text = f"★ {s.mention}{tag}仕入の取り込み（NOS）に失敗しました: {type(e).__name__}: {str(e)[:200]}"
        if notify and not dry_run:
            try:
                _slack(s, env, text)
            except Exception:  # noqa: BLE001
                log.exception("Slack への通知にも失敗")
        return RunResult(False, text)

    gross = sum(v.gross for v in vouchers)
    net = sum(v.net for v in vouchers)
    days = len({v.date for v in vouchers})
    out = [f"*{tag}仕入の取り込み {period}（NIGITA）*",
           f"伝票 {len(vouchers)}件・{days}日／税込 ¥{gross:,}（税抜 ¥{net:,}）",
           f"Asana: 新規 {st.created}・更新 {st.updated}・変化なし {st.unchanged}" + (f"・対象外 {st.excluded}" if st.excluded else "")]
    if st.new_items:
        names = "、".join(st.new_items[:8]) + (f" ほか{len(st.new_items) - 8}件" if len(st.new_items) > 8 else "")
        out.append(f"新しい商品 {len(st.new_items)}件（表示名は案。仕入管理の商品マスタで確認）: {names}")
    if st.renamed:
        out.append(f"表示名の案を作り直した商品 {st.renamed}件（案のままのものだけ）")
    if (st.new_items or rename_drafts) and not st.names_by_claude:
        out.append("（表示名の案は Claude Haiku を使えず、規則だけで作りました。API キーか PC の Claude Code のログインを確認）")
    problems = mismatch + st.review
    if problems:
        out[0] = f"★ {s.mention}" + out[0]
        out += [f"要確認: {p}" for p in problems]
    if portal:
        out.append(f"仕入管理: {portal}")
    text = "\n".join(out)
    if notify and not dry_run:
        _slack(s, env, text)
    return RunResult(True, text)
