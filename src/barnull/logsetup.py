"""ログ設定（ファイルは data/logs/barnull.log、暗証番号・パスワード・トークンは伏せる）。"""

from __future__ import annotations

import logging
import logging.handlers
import sys

from .config import Config
from .secrets import redact


class RedactFilter(logging.Filter):
    """本文だけでなく例外のトレースバックも伏せる（Playwright の例外は fill("…") の入力値を含むことがある）"""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:  # noqa: BLE001
            return True
        record.msg = redact(msg)
        record.args = ()
        if record.exc_info:
            record.exc_text = redact(logging.Formatter().formatException(record.exc_info))
            record.exc_info = None
        elif record.exc_text:
            record.exc_text = redact(record.exc_text)
        if record.stack_info:
            record.stack_info = redact(record.stack_info)
        return True


def setup_logging(cfg: Config, verbose: bool = False, filename: str = "barnull.log") -> None:
    """常駐プロセスは barnull.log、それ以外のコマンドは別のファイル（同じファイルを開き合うと、
    Windows では日付の切り替え時にファイル名を変えられずログが失われるため）"""
    cfg.log_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    for h in list(root.handlers):
        root.removeHandler(h)
    fh = logging.handlers.TimedRotatingFileHandler(
        cfg.log_dir / filename, when="midnight", backupCount=30, encoding="utf-8", delay=True
    )
    fh.setFormatter(fmt)
    fh.addFilter(RedactFilter())
    root.addHandler(fh)
    if sys.stdout is not None:  # pythonw（タスクスケジューラ）では標準出力が無い
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        sh.addFilter(RedactFilter())
        root.addHandler(sh)
    for noisy in ("slack_bolt", "slack_sdk", "httpx", "httpx2", "asyncio", "anthropic"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    # 「実行中のためスキップ」の警告は出さないが、ジョブの例外（ERROR）は残す
    logging.getLogger("apscheduler").setLevel(logging.ERROR)
