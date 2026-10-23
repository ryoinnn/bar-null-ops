"""外部 API の呼び出し（すべての連携先で共通）。

- 一時的な失敗（429・5xx・タイムアウト・接続失敗）は、待ってから数回やり直す
- 二重に実行されると困る POST（Slack の投稿、Asana のタスク作成など）は、送れていないと分かる失敗
  （429、接続できなかった）のときだけやり直す
- 最後まで失敗したら ExternalAPIError。どの連携先の、どの操作で、何が起きたかがメッセージで分かる
"""

from __future__ import annotations

import logging
import time

import httpx

log = logging.getLogger(__name__)
RETRY_STATUS = {429, 500, 502, 503, 504}
METADATA_TOKEN = "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token"
_client = httpx.Client(timeout=30)


class ExternalAPIError(RuntimeError):
    """連携先（Square、マネフォ、Slack、Asana、LINE、Google Cloud など）の呼び出しの失敗。"""

    def __init__(self, api: str, op: str, status: int | None, detail: str) -> None:
        self.api, self.op, self.status = api, op, status
        super().__init__(f"{api} {op}: {'HTTP ' + str(status) if status else '通信エラー'} {detail}".strip())


def request(api: str, method: str, url: str, *, idempotent: bool | None = None, attempts: int = 4,
            allow: tuple[int, ...] = (), timeout: float = 30, **kwargs) -> httpx.Response:
    """HTTP リクエスト。成功（2xx）か allow に含まれる状態コードなら Response を返す。

    idempotent: 何度送っても結果が同じか（既定は GET/PUT/DELETE なら True、POST なら False）。
    """
    if idempotent is None:
        idempotent = method.upper() != "POST"
    op = f"{method.upper()} {httpx.URL(url).path}"
    for attempt in range(1, attempts + 1):
        last = attempt == attempts
        try:
            r = _client.request(method, url, timeout=timeout, **kwargs)
        except (httpx.ConnectError, httpx.ConnectTimeout) as e:   # 送れていない: やり直してよい
            if last:
                raise ExternalAPIError(api, op, None, repr(e)) from e
            _wait(api, op, attempt, None)
            continue
        except httpx.TransportError as e:                         # 送れたか分からない（読み取りのタイムアウトなど）
            if last or not idempotent:
                raise ExternalAPIError(api, op, None, repr(e)) from e
            _wait(api, op, attempt, None)
            continue
        if r.status_code < 400 or r.status_code in allow:
            return r
        retryable = r.status_code == 429 or (idempotent and r.status_code in RETRY_STATUS)
        if last or not retryable:
            raise ExternalAPIError(api, op, r.status_code, r.text[:300])
        _wait(api, op, attempt, r)
    raise AssertionError("unreachable")


def _wait(api: str, op: str, attempt: int, r: httpx.Response | None) -> None:
    after = r.headers.get("Retry-After") if r is not None else None
    delay = min(float(after), 30) if after and after.isdigit() else min(2 ** attempt, 20)
    log.warning("%s %s を %.0f 秒後にやり直す（%s）", api, op, delay, r.status_code if r is not None else "通信エラー",
                extra={"api": api})
    time.sleep(delay)


def gcp_token() -> str:
    """Cloud Run のサービスアカウントのアクセストークン（メタデータサーバーから）。"""
    r = request("GCP metadata", "GET", METADATA_TOKEN, headers={"Metadata-Flavor": "Google"}, timeout=5)
    return r.json()["access_token"]
