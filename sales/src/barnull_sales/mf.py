"""マネーフォワード クラウド会計: 連携口座の入出金明細（APIキー → JWT）。読み取りだけ。"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import date
from typing import Protocol

from .http import request

EXCHANGE = "https://api.biz.moneyforward.com/auth/exchange"
API = "https://api-accounting.moneyforward.com/api/v3"


@dataclass
class BankTxn:
    id: str
    date: str          # YYYY-MM-DD
    amount: int
    content: str


class BankLedgerSource(Protocol):
    def incomes(self, start: date, end: date) -> list[BankTxn]: ...


class MoneyForward:
    def __init__(self, api_key: str, office_code: str, sub_account: str) -> None:
        self.api_key, self.office_code, self.sub_account = api_key, office_code, sub_account
        self._jwt: tuple[str, float] | None = None

    def _token(self) -> str:
        if self._jwt and self._jwt[1] - time.time() > 60:
            return self._jwt[0]
        body = request("MoneyForward", "POST", EXCHANGE, headers={"Authorization": f"Bearer {self.api_key}"},
                       idempotent=True).json()
        self._jwt = (body["access_token"], time.time() + int(body.get("expires_in", 3600)))
        return self._jwt[0]

    def incomes(self, start: date, end: date) -> list[BankTxn]:
        """対象口座の入金（side = INCOME）。1ページ50件をすべてめくる。"""
        out, page, pages = [], 1, 1
        while page <= pages and page <= 40:
            body = request("MoneyForward", "GET", f"{API}/transactions",
                           headers={"Authorization": f"Bearer {self._token()}"},
                           params={"office_code": self.office_code, "start_date": start.isoformat(),
                                   "end_date": end.isoformat(), "page": page}).json()
            pages = (body.get("metadata") or {}).get("total_pages", 1)
            for t in body.get("transactions", []):
                if t.get("connected_sub_account_id") == self.sub_account and str(t.get("side")).upper() == "INCOME":
                    out.append(BankTxn(id=str(t["id"]), date=t["date"], amount=int(t["value"]),
                                       content=t.get("content") or ""))
            page += 1
        return out
