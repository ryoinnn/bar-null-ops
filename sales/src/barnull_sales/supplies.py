"""備品・消耗品: マネフォのカード「BAR NULL」の明細 → Asana「Bar null 備品消耗品」（契約は docs/profit/contract.md §3b）。

reconcile ジョブのステップ `supplies`（3 時間に1回だけマネフォを読む）。冪等: 同じ明細（MF明細ID）から二度作らず、
毎回、期間内を全件読み直して Asana に合わせる。
- 手入力（取得元）・「手動修正＝あり」のタスクは触らない
- 「対象外」のタスクは取り込み済みに戻さない（本人の取消を尊重する）
- 区分は作るときだけ決める（本人が直したら上書きしない）
- マネフォから消えた明細（PENDING の置き換えなど）は、読んだ期間内のものだけ「対象外」にする（削除しない）
ログ・Slack には店名を出さない（件数と金額だけ）。
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from .config import SuppliesConfig
from .deps import Deps
from .mf import BankTxn

log = logging.getLogger(__name__)
ID_RE = re.compile(r"^E-(\d{8})-(\d{3})$")


def norm(text: str) -> str:
    """全角英数・記号を半角にし、空白をまとめる。"""
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", text or "")).strip()


def _hit(text: str, patterns: tuple[str, ...]) -> bool:
    t = norm(text).casefold()
    return any(norm(p).casefold() in t for p in patterns if p.strip())


def classify(cfg: SuppliesConfig, content: str) -> tuple[str, bool]:
    """(区分, 対象外にするか)"""
    if _hit(content, cfg.exclude_patterns):
        return "備品・消耗品", True
    return ("ツール・サブスク" if _hit(content, cfg.tool_patterns) else "備品・消耗品"), False


@dataclass
class Stats:
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    excluded: int = 0           # 今回「対象外」にした（マネフォから消えた・NIGITA）
    new_amount: int = 0         # 今回の新規の合計
    high: list[int] = field(default_factory=list)   # 新規のうち高額の金額


def _same(ex: dict, values: dict, section: str) -> bool:
    if ex.get("section") != section:
        return False
    return all((ex.get(k) in (None, "") and v in (None, "")) or ex.get(k) == v for k, v in values.items())


def task_name(d: str, payee: str, amount: int) -> str:
    return f"{d} {payee} ¥{amount:,}"


def sync(tracker, txns: list[BankTxn], start: str, end: str, cfg: SuppliesConfig, *, dry_run: bool = False,
         log_fn=lambda m: None) -> Stats:
    st = Stats()
    tasks = tracker.list_tasks()
    by_mf = {t["mf_id"]: t for t in tasks if t.get("mf_id")}
    seq: dict[str, int] = {}
    for t in tasks:
        m = ID_RE.match(t.get("record_id") or "")
        if m:
            seq[m.group(1)] = max(seq.get(m.group(1), 0), int(m.group(2)))
    seen: set[str] = set()
    for tx in sorted(txns, key=lambda x: (x.date, x.id)):
        seen.add(tx.id)
        payee = norm(tx.content) or "（摘要なし）"
        category, excluded = classify(cfg, tx.content)
        ex = by_mf.get(tx.id)
        if ex is None:
            key = tx.date.replace("-", "")
            seq[key] = seq.get(key, 0) + 1
            values = {"record_id": f"E-{key}-{seq[key]:03d}", "business_date": tx.date, "payee": payee,
                      "amount": tx.amount, "category": category, "source": "マネフォカード", "mf_id": tx.id,
                      "s_status": "対象外" if excluded else "取り込み済み", "manual": "なし", "store": cfg.store}
            log_fn(("[dry-run] " if dry_run else "") + f"作成: {values['record_id']}")
            if not dry_run:
                tracker.upsert(None, task_name(tx.date, payee, tx.amount), values, f"摘要: {tx.content}\nカード: BAR NULL",
                               "対象外" if excluded else "備品消耗品")
            st.created += 1
            if not excluded:
                st.new_amount += tx.amount
                if tx.amount >= cfg.high_value:
                    st.high.append(tx.amount)
            continue
        if ex.get("manual") == "あり" or ex.get("source") == "手入力":
            st.unchanged += 1
            continue
        values = {"business_date": tx.date, "payee": payee, "amount": tx.amount}
        status = ex.get("s_status") or "取り込み済み"
        if excluded and status != "対象外":
            status = "対象外"
            st.excluded += 1
        values["s_status"] = status
        section = "対象外" if status == "対象外" else "備品消耗品"
        if _same(ex, values, section):
            st.unchanged += 1
            continue
        log_fn(("[dry-run] " if dry_run else "") + f"更新: {ex.get('record_id')}")
        if not dry_run:
            tracker.upsert(ex["gid"], task_name(tx.date, payee, tx.amount), values, None, section)
        st.updated += 1
    for t in tasks if txns else []:     # マネフォに無くなった明細（読んだ期間内）は対象外にする（0件のときは読み違いかもしれないので触らない）
        mid = t.get("mf_id")
        if (not mid or mid in seen or t.get("manual") == "あり" or t.get("source") == "手入力"
                or t.get("s_status") == "対象外" or not (start <= (t.get("business_date") or "") <= end)):
            continue
        log_fn(("[dry-run] " if dry_run else "") + f"対象外に: {t.get('record_id')}")
        if not dry_run:
            tracker.upsert(t["gid"], t.get("name") or t.get("record_id") or "", {"s_status": "対象外"}, None, "対象外")
        st.excluded += 1
    return st


def summary(st: Stats, cfg: SuppliesConfig, start: str, end: str, n_txns: int) -> str | None:
    """Slack に知らせる文。変化が無ければ None。"""
    if not (st.created or st.updated or st.excluded):
        return None
    tag = "［テスト］" if cfg.dry_run else ""
    lines = [f"*{tag}備品・消耗品の取り込み {start[5:].replace('-', '/')}〜{end[5:].replace('-', '/')}（カード BAR NULL）*",
             f"明細 {n_txns}件／Asana: 新規 {st.created}（¥{st.new_amount:,}）・更新 {st.updated}・対象外 {st.excluded}"]
    if st.high:
        lines.append(f"高額（¥{cfg.high_value:,} 以上）の新規 {len(st.high)}件: "
                     + "、".join(f"¥{a:,}" for a in st.high) + "。固定資産（減価償却）にするか、必要なら確認")
    return "\n".join(lines)


def step(deps: Deps, now: datetime | None = None, force: bool = False) -> dict:
    """reconcile ジョブのステップ。[supplies] が無効・Asana のプロジェクト未作成なら何もしない。"""
    s = deps.settings
    cfg = s.supplies
    if not cfg.enabled or deps.supplies is None or not cfg.card_subaccount or deps.bank is None:
        return {"skipped": True}
    now = now or datetime.now(UTC)
    meta = deps.store.get_doc("meta", "supplies") or {}
    last = datetime.fromisoformat(meta["last_fetch"]) if meta.get("last_fetch") else None
    if not force and last and now - last < timedelta(hours=cfg.fetch_interval_hours):
        return {"skipped": "interval"}
    today = now.astimezone(s.tz).date()
    since = date.fromisoformat(cfg.backfill_from) if not last else today - timedelta(days=cfg.lookback_days)
    txns = deps.bank.card_txns(since, today, cfg.card_subaccount)
    st = sync(deps.supplies, txns, since.isoformat(), today.isoformat(), cfg, log_fn=log.info)
    deps.store.set_doc("meta", "supplies", {"last_fetch": now.isoformat(), "count": len(txns)})
    text = summary(st, cfg, since.isoformat(), today.isoformat(), len(txns))
    if text and cfg.notify:
        channel = s.slack_channel_test if cfg.dry_run else s.slack_channel_prod
        deps.notifier.post(channel, text)
    return {"txns": len(txns), "created": st.created, "updated": st.updated, "excluded": st.excluded}
