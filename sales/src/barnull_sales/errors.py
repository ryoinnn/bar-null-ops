"""定期ジョブの中で、伝票1件の失敗が他の伝票の処理を止めないようにする。"""

from __future__ import annotations

import logging
from contextlib import contextmanager

from . import logs
from .http import ExternalAPIError

log = logging.getLogger(__name__)


class JobFailed(RuntimeError):
    """ジョブの一部（伝票のいくつか）が失敗した。メッセージに対象と原因が入る。

    apis: 失敗の原因になった連携先（すべて連携先の失敗なら、外部API障害として知らせる）。
    """

    def __init__(self, stage: str, errors: list[tuple[str, Exception]]) -> None:
        self.errors = errors
        self.apis = sorted({e.api for _, e in errors if isinstance(e, ExternalAPIError)})
        self.all_external = all(isinstance(e, ExternalAPIError) for _, e in errors)
        lines = [f"{key}: {e}"[:200] for key, e in errors[:3]]
        more = f" ほか{len(errors) - 3}件" if len(errors) > 3 else ""
        super().__init__(f"{stage}: {len(errors)}件失敗（{' / '.join(lines)}{more}）")


class ItemErrors:
    """for slip in ...: with errs.item(slip_id=slip.slip_id): ... と書くと、失敗を記録して次へ進む。

    item に渡した項目（slip_id、task_gid など）は、その中のログに付く。
    """

    def __init__(self, stage: str) -> None:
        self.stage = stage
        self.errors: list[tuple[str, Exception]] = []

    @contextmanager
    def item(self, **ctx: str):
        key = ",".join(f"{k}={v}" for k, v in ctx.items())
        with logs.context(stage=self.stage, **ctx):
            try:
                yield
            except Exception as e:
                log.exception("%s: %s の処理に失敗（次へ進む）", self.stage, key)
                self.errors.append((key, e))

    def raise_if_any(self) -> None:
        if self.errors:
            raise JobFailed(self.stage, self.errors)
