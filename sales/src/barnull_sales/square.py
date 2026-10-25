"""Square: 読み取り専用の OAuth（自社で1回だけ許可）と、決済・振込の取得。

トークンは Secret Manager の square-oauth（JSON）に置く。更新したら新しい版を足し、それより古い版は破棄する
（無料枠の有効なバージョン数に収めるため）。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol
from urllib.parse import urlencode

from .http import gcp_token, request

log = logging.getLogger(__name__)
BASE = "https://connect.squareup.com"
SCOPES = ("PAYMENTS_READ", "ORDERS_READ", "PAYOUTS_READ", "MERCHANT_PROFILE_READ")


# ---------------------------------------------------------------- 決済
@dataclass
class Payment:
    id: str
    created_at: datetime
    amount: int              # total_money（円）
    status: str
    source_type: str
    fee: int | None          # processing_fee の合計（現金は None）
    refunded: int
    brand: str | None

    @property
    def method_label(self) -> str:
        if self.source_type == "CASH":
            return "現金"
        if self.source_type == "WALLET":
            return f"電子マネー（{self.brand}）" if self.brand else "電子マネー"
        if self.brand == "FELICA":
            return "交通系IC"
        return "カード"

    @property
    def fully_refunded(self) -> bool:
        return self.refunded > 0 and self.refunded >= self.amount

    @property
    def usable(self) -> bool:
        """伝票に割り当ててよい決済か（完了していて、全額返金されていない）。"""
        return self.status == "COMPLETED" and not self.fully_refunded

    def record(self, **extra) -> dict:
        """伝票（square.payments）に保存する形。"""
        return {"id": self.id, "amount": self.amount, "fee": self.fee or 0, "source_type": self.source_type,
                "paid_at": self.created_at.isoformat(), **extra}


def parse_payment(p: dict) -> Payment:
    fees = p.get("processing_fee") or []
    card = (p.get("card_details") or {}).get("card") or {}
    return Payment(
        id=p["id"], created_at=datetime.fromisoformat(p["created_at"].replace("Z", "+00:00")),
        amount=(p.get("total_money") or {}).get("amount", 0), status=p.get("status", ""),
        source_type=p.get("source_type", ""),
        fee=sum((f.get("amount_money") or {}).get("amount", 0) for f in fees) if fees else None,
        refunded=(p.get("refunded_money") or {}).get("amount", 0),
        brand=card.get("card_brand") or (p.get("wallet_details") or {}).get("brand"),
    )


class PaymentSource(Protocol):
    def list_payments(self, begin: datetime, end: datetime) -> list[Payment]: ...


class PayoutSource(Protocol):
    def list_payouts(self, begin: datetime) -> list[dict]: ...
    def list_payouts_for(self, location_id: str, begin: datetime) -> list[dict]: ...
    def payment_ids(self, payout_id: str) -> list[str]: ...


class SquareAPI:
    """決済（list_payments）と振込（list_payouts、payment_ids）。token_provider はアクセストークンを返す関数。"""

    def __init__(self, token_provider, location_id: str) -> None:
        self.token_provider = token_provider
        self.location_id = location_id

    def _pages(self, path: str, params: dict, key: str, max_pages: int = 20) -> list[dict]:
        out, cursor = [], None
        for _ in range(max_pages):
            q = {**params, **({"cursor": cursor} if cursor else {})}
            body = request("Square", "GET", f"{BASE}{path}", params=q,
                           headers={"Authorization": f"Bearer {self.token_provider()}"}).json()
            out += body.get(key, [])
            cursor = body.get("cursor")
            if not cursor:
                break
        return out

    def list_payments(self, begin: datetime, end: datetime) -> list[Payment]:
        params = {"location_id": self.location_id, "begin_time": begin.astimezone(UTC).isoformat(),
                  "end_time": end.astimezone(UTC).isoformat(), "sort_order": "ASC", "limit": 100}
        return [parse_payment(p) for p in self._pages("/v2/payments", params, "payments")]

    def list_payouts(self, begin: datetime) -> list[dict]:
        params = {"location_id": self.location_id, "begin_time": begin.astimezone(UTC).isoformat(), "limit": 100}
        return self._pages("/v2/payouts", params, "payouts", max_pages=10)

    def list_payouts_for(self, location_id: str, begin: datetime) -> list[dict]:
        """同じアカウントのほかの店舗の振込（同じ口座に入る振込の取り違えを防ぐため。読み取りだけ）。"""
        return SquareAPI(self.token_provider, location_id).list_payouts(begin)

    def payment_ids(self, payout_id: str) -> list[str]:
        entries = self._pages(f"/v2/payouts/{payout_id}/payout-entries", {"limit": 100}, "payout_entries")
        return [pid for e in entries if (pid := (e.get("type_charge_details") or {}).get("payment_id"))]


# ---------------------------------------------------------------- OAuth
def _sign(secret: str, text: str) -> str:
    return base64.urlsafe_b64encode(hmac.new(secret.encode(), text.encode(), hashlib.sha256).digest()).decode()[:22]


def make_state(secret: str) -> str:
    ts = str(int(time.time()))
    return f"{ts}.{_sign(secret, ts)}"


def check_state(secret: str, state: str, max_age: int = 600) -> bool:
    try:
        ts, sig = state.split(".", 1)
        return hmac.compare_digest(sig, _sign(secret, ts)) and 0 <= time.time() - int(ts) <= max_age
    except ValueError:
        return False


def authorize_url(app_id: str, state: str) -> str:
    return f"{BASE}/oauth2/authorize?" + urlencode({"client_id": app_id, "scope": " ".join(SCOPES),
                                                    "session": "false", "state": state})


def exchange(app_id: str, app_secret: str, *, code: str | None = None, refresh_token: str | None = None) -> dict:
    body = {"client_id": app_id, "client_secret": app_secret}
    if code:
        body |= {"grant_type": "authorization_code", "code": code}
    else:
        body |= {"grant_type": "refresh_token", "refresh_token": refresh_token}
    return request("Square", "POST", f"{BASE}/oauth2/token", json=body).json()


# ---------------------------------------------------------------- トークンの保管（Secret Manager）
class SecretStore:
    """Secret Manager の1つのシークレットを JSON として読み書きする（REST）。"""

    def __init__(self, project: str, name: str, token=gcp_token) -> None:
        self.base = "https://secretmanager.googleapis.com/v1"
        self.url = f"{self.base}/projects/{project}/secrets/{name}"
        self.token = token

    def _h(self) -> dict:
        return {"Authorization": f"Bearer {self.token()}"}

    def read(self) -> dict | None:
        r = request("Secret Manager", "GET", f"{self.url}/versions/latest:access", headers=self._h(), allow=(404,))
        if r.status_code == 404:
            return None
        return json.loads(base64.b64decode(r.json()["payload"]["data"]))

    def write(self, data: dict) -> None:
        payload = base64.b64encode(json.dumps(data).encode()).decode()
        new = request("Secret Manager", "POST", f"{self.url}:addVersion", headers=self._h(),
                      json={"payload": {"data": payload}}).json()["name"]
        new_no = int(new.rsplit("/", 1)[-1])
        versions = request("Secret Manager", "GET", f"{self.url}/versions", headers=self._h(),
                           params={"filter": "state:ENABLED"}).json().get("versions", [])
        for v in versions:   # 今足した版より古いものだけ破棄する（同時に書いた新しい版を消さないように）
            if int(v["name"].rsplit("/", 1)[-1]) < new_no:
                try:
                    request("Secret Manager", "POST", f"{self.base}/{v['name']}:destroy", headers=self._h(), json={},
                            idempotent=True)
                except Exception:
                    log.warning("古い版の破棄に失敗（次の更新で破棄する）", exc_info=True)


class OAuthTokens:
    """アクセストークンを返す。期限まで7日を切っていたら更新して保存する。"""

    def __init__(self, store: SecretStore, app_id: str, app_secret: str) -> None:
        self.store, self.app_id, self.app_secret = store, app_id, app_secret
        self._cache: dict | None = None

    def _expires(self, data: dict) -> datetime:
        return datetime.fromisoformat(data["expires_at"].replace("Z", "+00:00"))

    def __call__(self) -> str:
        data = self._cache
        if not data or self._expires(data) - datetime.now(UTC) < timedelta(days=7):
            data = self.store.read()   # 他のインスタンスが先に更新していれば、それを使う
        if not data:
            raise RuntimeError("Square の許可がまだです（運用手順書「Square の許可をやり直す」を参照）")
        if self._expires(data) - datetime.now(UTC) < timedelta(days=7):
            new = exchange(self.app_id, self.app_secret, refresh_token=data["refresh_token"])
            data = {**data, **{k: new[k] for k in ("access_token", "expires_at", "refresh_token") if new.get(k)}}
            self.store.write(data)
            log.info("Square のアクセストークンを更新した")
        self._cache = data
        return data["access_token"]
