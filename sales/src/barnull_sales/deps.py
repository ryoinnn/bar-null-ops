"""処理が使う部品（設定・保存先・連携先）をまとめたもの。テストでは偽物に差し替える。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .asana import TaskTracker
    from .config import Settings
    from .extract import Extractor
    from .line import LineClient
    from .mf import BankLedgerSource
    from .pos_notify import PosClient
    from .slack import Notifier
    from .square import PaymentSource, PayoutSource
    from .store import Store


@dataclass
class Deps:
    settings: Settings
    store: Store
    notifier: Notifier
    tracker: TaskTracker
    extractor: Extractor | None = None      # Claude（メッセージの解析、振込名義の判定）
    line: LineClient | None = None          # 表示名・グループ名の取得、OK スタンプの返信
    payments: PaymentSource | None = None   # Square の決済
    payouts: PayoutSource | None = None     # Square の振込
    bank: BankLedgerSource | None = None    # マネフォの銀行明細
    pos: PosClient | None = None            # POS（福岡）へ知らせる先
