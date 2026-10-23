"""設定ファイル（config.toml）の読み込み。"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / "data"
DEFAULT_APPROVE_REACTIONS = ("white_check_mark", "heavy_check_mark", "ballot_box_with_check", "ok", "ok_hand", "+1",
                             "thumbsup", "o", "accept", "approved", "sumi")


@dataclass(frozen=True)
class Config:
    dry_run: bool
    channel_prod: str
    channel_test: str
    approvers: tuple[str, ...]
    poll_interval_sec: int
    poll_jitter_sec: int
    remind_interval_minutes: int
    completion_grace_minutes: int
    deadman_refresh_minutes: int
    deadman_fire_after_minutes: int
    alert_dedup_minutes: int
    screenshot_retention_days: int
    sm_host_id: str
    sm_host_share: float
    ib_host_share: float
    llm_model: str
    effort_classify: str
    effort_draft: str
    effort_thanks: str
    use_fallbacks: bool
    review_stars: int
    manual_pdf: Path
    template_reservation: str
    sent_marker: str
    data_dir: Path = field(default=DATA_DIR)
    approve_reactions: tuple[str, ...] = DEFAULT_APPROVE_REACTIONS
    upload_screenshots: bool = False

    @property
    def channel(self) -> str:
        return self.channel_test if self.dry_run else self.channel_prod

    @property
    def db_path(self) -> Path:
        return self.data_dir / "barnull.db"

    @property
    def screenshot_dir(self) -> Path:
        return self.data_dir / "screenshots"

    @property
    def browser_dir(self) -> Path:
        # ログイン状態（ブラウザのプロファイル）は、本番・DRY_RUN・模擬で共通
        return DATA_DIR / "browser"

    @property
    def global_lock(self) -> Path:
        """常駐（全モード）と、ブラウザを使うコマンド（login, capture, backfill, doctor）で共通の多重起動ロック。
        ブラウザのプロファイルと Slack の接続を共有しているため、同時に動かせるのは1つだけ"""
        return DATA_DIR / "barnull.lock"

    @property
    def log_dir(self) -> Path:
        return self.data_dir / "logs"


def load_config(path: Path | None = None) -> Config:
    path = path or Path(os.environ.get("BARNULL_CONFIG", ROOT / "config.toml"))
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    s, sch, pf, llm, tpl = raw["slack"], raw["schedule"], raw["platforms"], raw["llm"], raw["template"]
    dry_run = raw.get("dry_run", True)
    if os.environ.get("BARNULL_DRY_RUN") is not None:
        dry_run = os.environ["BARNULL_DRY_RUN"].lower() in ("1", "true", "yes")
    data_dir = Path(os.environ.get("BARNULL_DATA_DIR", DATA_DIR))
    return Config(
        dry_run=dry_run,
        channel_prod=s["channel_prod"],
        channel_test=s["channel_test"],
        approvers=tuple(s["approvers"]),
        poll_interval_sec=sch["poll_interval_sec"],
        poll_jitter_sec=sch["poll_jitter_sec"],
        remind_interval_minutes=int(
            os.environ.get("BARNULL_REMIND_MINUTES", sch["remind_interval_minutes"])
        ),
        completion_grace_minutes=sch["completion_grace_minutes"],
        deadman_refresh_minutes=sch["deadman_refresh_minutes"],
        deadman_fire_after_minutes=sch["deadman_fire_after_minutes"],
        alert_dedup_minutes=sch["alert_dedup_minutes"],
        screenshot_retention_days=sch["screenshot_retention_days"],
        sm_host_id=pf["sm"]["host_id"],
        sm_host_share=pf["sm"]["host_share"],
        ib_host_share=pf["ib"]["host_share"],
        llm_model=llm["model"],
        effort_classify=llm["effort_classify"],
        effort_draft=llm["effort_draft"],
        effort_thanks=llm["effort_thanks"],
        use_fallbacks=llm["use_fallbacks"],
        review_stars=raw.get("review", {}).get("stars", 5),
        manual_pdf=ROOT / tpl["manual_pdf"],
        template_reservation=tpl["reservation"],
        sent_marker=tpl["sent_marker"],
        data_dir=data_dir,
        approve_reactions=tuple(s.get("approve_reactions") or DEFAULT_APPROVE_REACTIONS),
        upload_screenshots=bool(s.get("upload_screenshots", False)),
    )
