"""設定（sales.toml）と秘密情報（Cloud Run では Secret Manager の JSON、手元では資格情報マネージャー）。

設定はすべて sales.toml に書く。環境変数で上書きできるのは DRY_RUN と SALES_TOML（設定ファイルの場所）だけ。
店舗ごとに設定ファイルを分ける（Bar null は sales.toml、福岡 Node は stores/node/sales.toml。SALES_TOML で選ぶ）。
[store] と [line] は省ける（省くと Bar null の値。LINE を使わない店舗は [line] を書かない）。
"""

from __future__ import annotations

import json
import os
import tomllib
from dataclasses import dataclass, fields
from datetime import datetime, time
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
KEYRING_SERVICE = "barnull-sales"   # Bar null の既定（店舗ごとに [store] keyring_service で変えられる）


def _pick(cls, d: dict):
    """d のうち cls にある項目だけで cls を作る（リストはタプルにする）。"""
    names = {f.name for f in fields(cls)}
    return cls(**{k: tuple(v) if isinstance(v, list) else v for k, v in d.items() if k in names})


@dataclass(frozen=True)
class SquareConfig:
    merchant_id: str
    location_id: str
    payment_grace_min: float = 60        # 送信からこの時間たっても決済が見つからなければ「決済未確認」
    payment_retry_days: float = 7        # 見つからなくても、送信からこの日数は探し続ける
    payment_window_extra_days: int = 1   # 営業日の翌日の朝までも探す（営業日と実際の決済の日がずれることがあるため）
    split_max: int = 5                   # 割り勘とみなす決済の最大件数
    split_span_min: float = 30           # 割り勘とみなす決済の時間の幅
    orphan_grace_hours: float = 6        # 決済の営業日が終わってから「伝票なし」と知らせるまで
    orphan_check_since: str = ""         # これより前の決済は「伝票なし」として扱わない
    oauth_enabled: bool = False          # Square の許可（/oauth/square/*）を受け付けるか。許可し直すときだけ true
    # 同じ銀行口座に振り込まれる、同じアカウントのほかの店舗（location）。同じ金額の振込があれば自動で確認しない
    shared_bank_locations: tuple[str, ...] = ()
    receipt_before_min: float = 10       # POS の受け取りの時刻の何分前からの決済を探すか
    receipt_after_min: float = 60        # 何分後までの決済を探すか

    @property
    def orphan_since(self) -> datetime | None:
        return datetime.fromisoformat(self.orphan_check_since) if self.orphan_check_since else None


@dataclass(frozen=True)
class MfConfig:
    office_code: str
    bank_subaccount: str
    square_patterns: tuple[str, ...] = ()
    fetch_interval_hours: float = 3
    staff_bank_names: tuple[str, ...] = ()   # 現金を振り込む担当者の名義（カナ）。空なら現金を自動で照合しない
    cash_by_business_day: bool = False        # 現金の入金を営業日ごとの合計で照合する（福岡。Bar null は伝票ごと）


@dataclass(frozen=True)
class DepositConfig:
    grace_bizdays: int = 3
    cash_window_days: int = 14


@dataclass(frozen=True)
class FollowupConfig:
    unpaid_reminder_interval_days: float = 7
    correction_window_hours: float = 24
    event_retention_days: int = 30
    payer_match_confidence: float = 0.8


@dataclass(frozen=True)
class Settings:
    dry_run: bool
    project: str
    region: str
    tasks_queue: str
    tasks_max_attempts: int
    invoker_sa: str
    worker_url: str
    tz: ZoneInfo
    day_start: time
    bot_mention_name: str
    allowed_group_ids: tuple[str, ...]
    reply_ack: bool
    slack_channel_prod: str
    slack_channel_test: str
    slack_alert_mention: str
    slack_report_channel_prod: str
    asana_workspace: str
    asana_team_name: str
    asana_project_name_prod: str
    asana_project_name_test: str
    model_primary: str
    model_fallback: str
    effort: str
    report_url: str
    square: SquareConfig
    mf: MfConfig
    deposit: DepositConfig
    followup: FollowupConfig
    asana_ids: dict
    pos_callback_url: str = ""               # 未収の回収などを POS に知らせる先（[pos] callback_url。空なら知らせない）
    pos_callback_audience: str = ""          # その ID トークンの audience
    schedule_mode: str = "scheduler"         # 定期ジョブの起動: scheduler（Cloud Scheduler）/ tasks（Cloud Tasks で次の回を予約。無料）
    store_name: str = "Bar null"             # 画面や通知に出す店舗名
    line_enabled: bool = True                # LINE から伝票を受け付けるか（[line] が無ければ受け付けない）
    keyring_service: str = "barnull-sales"   # 手元で秘密情報を読む資格情報マネージャーの名前
    # 秘密情報を1つのシークレットにまとめる店舗（福岡。[gcp] secret）。Square のトークンも同じシークレットに入れ、
    # Cloud Run では起動時に環境変数ではなく API で読む（版を1つに保ち、Secret Manager の無料枠に収めるため）
    secret_name: str = ""

    @property
    def slack_channel(self) -> str:
        return self.slack_channel_test if self.dry_run else self.slack_channel_prod

    @property
    def slack_report_channel(self) -> str:
        if self.dry_run:
            return self.slack_channel_test
        return self.slack_report_channel_prod or self.slack_channel_prod

    @property
    def asana_env(self) -> str:
        return "test" if self.dry_run else "prod"

    @property
    def mention(self) -> str:
        """Slack で桝本をメンションする文字列（末尾に空白つき）。"""
        return f"<@{self.slack_alert_mention}> " if self.slack_alert_mention else ""


def load_settings(path: Path | None = None) -> Settings:
    path = path or config_path()
    c = tomllib.loads(path.read_text(encoding="utf-8"))
    g, b, s, a, cl = c["gcp"], c["business"], c["slack"], c["asana"], c["claude"]
    ln, st = c.get("line"), c.get("store", {})
    h, m = (int(x) for x in b["day_start"].split(":"))
    ids_path = path.parent / "asana_ids.json"
    dry = os.environ.get("DRY_RUN")
    return Settings(
        dry_run=c.get("dry_run", True) if not dry else dry.strip().lower() in ("1", "true", "yes", "on"),
        project=g["project"], region=g["region"], tasks_queue=g["tasks_queue"],
        tasks_max_attempts=int(g.get("tasks_max_attempts", 10)), invoker_sa=g["invoker_sa"], worker_url=g["worker_url"],
        tz=ZoneInfo(b["timezone"]), day_start=time(h, m),
        bot_mention_name=(ln or {}).get("bot_mention_name", ""),
        allowed_group_ids=tuple((ln or {}).get("allowed_group_ids", [])),
        reply_ack=(ln or {}).get("reply_ack", False),
        slack_channel_prod=s["channel_prod"], slack_channel_test=s["channel_test"],
        slack_alert_mention=s.get("alert_mention", ""), slack_report_channel_prod=s.get("report_channel_prod", ""),
        asana_workspace=a["workspace"], asana_team_name=a["team_name"],
        asana_project_name_prod=a["project_name_prod"], asana_project_name_test=a["project_name_test"],
        model_primary=cl["model_primary"], model_fallback=cl["model_fallback"], effort=cl["effort"],
        report_url=c.get("report", {}).get("artifact_url", ""),
        square=_pick(SquareConfig, c["square"]), mf=_pick(MfConfig, c["mf"]),
        deposit=_pick(DepositConfig, c.get("deposit", {})), followup=_pick(FollowupConfig, c.get("followup", {})),
        asana_ids=json.loads(ids_path.read_text(encoding="utf-8")) if ids_path.exists() else {},
        pos_callback_url=c.get("pos", {}).get("callback_url", ""),
        pos_callback_audience=c.get("pos", {}).get("callback_audience", ""),
        schedule_mode=c.get("schedule", {}).get("mode", "scheduler"),
        store_name=st.get("name", "Bar null"), line_enabled=ln is not None,
        keyring_service=st.get("keyring_service", KEYRING_SERVICE),
        secret_name=g.get("secret", ""),
    )


def config_path() -> Path:
    """今の店舗の設定ファイル（asana_ids.json は同じフォルダに置く）。"""
    return Path(os.environ.get("SALES_TOML", ROOT / "sales.toml"))


@lru_cache
def settings() -> Settings:
    return load_settings()


# ---------------------------------------------------------------- 秘密情報
# サービスごとに、必要なものだけを別のシークレットに入れる（公開している sales-webhook から他の鍵を読めないように）
SECRETS_BY_SERVICE = {
    "sales-config": ("SLACK_BOT_TOKEN", "ASANA_PAT", "MF_API_KEY", "ANTHROPIC_API_KEY", "SQUARE_APP_ID",
                     "SQUARE_APP_SECRET", "LINE_CHANNEL_ACCESS_TOKEN"),                     # sales-worker
    "sales-webhook-config": ("LINE_CHANNEL_SECRET", "SQUARE_APP_ID", "SQUARE_APP_SECRET"),  # sales-webhook
    "billing-guard-config": ("SLACK_BOT_TOKEN",),                                          # billing-guard
}
# 1つのシークレットにまとめる店舗（[gcp] secret）が入れる鍵。LINE は使わない。Square のトークンも同じシークレットに入る
SINGLE_SECRET_KEYS = ("SLACK_BOT_TOKEN", "ASANA_PAT", "MF_API_KEY", "ANTHROPIC_API_KEY", "SQUARE_APP_ID",
                      "SQUARE_APP_SECRET")
SQUARE_TOKEN_KEYS = ("access_token", "refresh_token", "expires_at", "merchant_id")


@lru_cache
def secrets() -> dict[str, str]:
    """Cloud Run では環境変数 SALES_CONFIG（そのサービス用のシークレットの JSON）か、[gcp] secret を API で読む。
    手元では資格情報マネージャー（店舗の名前で無ければ Bar null の名前のものを使う。Slack・Asana などは共通）。"""
    raw = os.environ.get("SALES_CONFIG")
    if raw:
        return json.loads(raw)
    s = settings()
    if s.secret_name and os.environ.get("ROLE"):
        from .square import SecretStore

        data = SecretStore(s.project, s.secret_name).read() or {}
        return {k: v for k, v in data.items() if k not in SQUARE_TOKEN_KEYS}
    try:
        import keyring  # noqa: F401  （無い環境では資格情報マネージャーを使わない）
    except ImportError:
        return {}
    names = {n for group in SECRETS_BY_SERVICE.values() for n in group}
    out = {}
    for n in sorted(names):
        v = keyring_value(n, s.keyring_service)
        if v:
            out[n] = v
    return out


def keyring_value(name: str, service: str) -> str:
    import keyring

    v = (keyring.get_password(service, name) or "").strip()
    if not v and service != KEYRING_SERVICE:
        v = (keyring.get_password(KEYRING_SERVICE, name) or "").strip()
    if not v and name == "ANTHROPIC_API_KEY":   # 売上管理用のキーが無ければ予約対応のキー（2026-10-10 本人の選択）
        v = (keyring.get_password("bar-null-ops", name) or "").strip()
    return v


def secret(name: str) -> str:
    v = secrets().get(name)
    if not v:
        raise RuntimeError(f"秘密情報 {name} がありません")
    return v
