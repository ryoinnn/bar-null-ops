"""Cloud Tasks への投入（sales-webhook → sales-worker）。REST で呼び、Webhook の起動を軽くする。"""

from __future__ import annotations

import base64
import json
import re

from .http import gcp_token, request


def task_id(event_id: str) -> str:
    """タスク名に使える文字だけにする（同じイベントの再送は同じ名前になり、Cloud Tasks が重複を弾く）。"""
    return re.sub(r"[^A-Za-z0-9_-]", "_", event_id)[:500]


def task_name_for(event: dict, kind: str) -> str:
    """タスク名。同じ名前のタスクは Cloud Tasks が弾く（約1時間）。

    処理対象のメッセージはイベントごと。グループへの参加・対象外のグループのメッセージはグループごとにして、
    知らないグループから大量に送られても、キューと worker が埋まらないようにする。
    """
    if kind in ("join", "unknown_group_message"):
        return task_id(f"{kind}_{(event.get('source') or {}).get('groupId', '')}")
    return task_id(event.get("webhookEventId", ""))


def enqueue(*, project: str, region: str, queue: str, worker_url: str, invoker_sa: str, event: dict,
            kind: str) -> str:
    """戻り値: "created" / "duplicate"。"""
    parent = f"projects/{project}/locations/{region}/queues/{queue}"
    body = base64.b64encode(json.dumps({"event": event, "kind": kind}, ensure_ascii=False).encode()).decode()
    task = {
        "name": f"{parent}/tasks/{task_name_for(event, kind)}",
        "httpRequest": {
            "httpMethod": "POST",
            "url": f"{worker_url}/tasks/process-event",
            "headers": {"Content-Type": "application/json"},
            "body": body,
            "oidcToken": {"serviceAccountEmail": invoker_sa, "audience": worker_url},
        },
    }
    # タスク名で重複が弾かれるので、やり直しても二重にならない
    r = request("Cloud Tasks", "POST", f"https://cloudtasks.googleapis.com/v2/{parent}/tasks", idempotent=True,
                headers={"Authorization": f"Bearer {gcp_token()}"}, json={"task": task}, allow=(409,), timeout=10,
                attempts=2)
    return "duplicate" if r.status_code == 409 else "created"
