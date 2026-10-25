"""Asana: 1伝票＝1タスク（伝票データの台帳）。プロジェクト・セクション・カスタムフィールドの作成（セットアップ）も持つ。

ID は asana_ids.json に保存する（秘密ではない）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Protocol

from .http import request
from .slips import Manual, Slip, Status, Unpaid
from .texts import asana_notes, asana_task_name

API = "https://app.asana.com/api/1.0"
SECTIONS = (Status.DRAFTED, Status.PAID, Status.DEPOSITED, Status.REVIEW)
STATUS_OPTIONS = (*SECTIONS, Status.CANCELLED)
UNPAID_OPTIONS = tuple(Unpaid)
MANUAL_OPTIONS = tuple(Manual)
# key: (表示名, 種類, enum の選択肢)
FIELDS = {
    "status": ("伝票ステータス", "enum", STATUS_OPTIONS),
    "unpaid": ("未収ステータス", "enum", UNPAID_OPTIONS),
    "business_date": ("営業日", "date", None),
    "customer": ("顧客名", "text", None),
    "slip_amount": ("伝票金額", "currency", None),
    "square_amount": ("Square決済額", "currency", None),
    "fee": ("決済手数料", "currency", None),
    "deposit_expected": ("入金予定額", "currency", None),
    "deposit_date": ("入金日", "date", None),
    "unpaid_remaining": ("未収残高", "currency", None),
    "sender": ("担当（LINE）", "text", None),
    "slip_id": ("伝票ID", "text", None),
    "payment_id": ("Square payment ID", "text", None),
    "slack_url": ("Slackスレッド", "text", None),
    "manual": ("手動上書き", "enum", MANUAL_OPTIONS),
}
# POS の伝票の店舗（福岡）だけが使う欄。asana-setup で、LINE を使わない店舗のときだけ作る
POS_FIELDS = {
    "customer_codes": ("顧客コード", "text", None),
    "referrals": ("紹介元", "text", None),
    "pos_visit": ("POS 伝票ID", "text", None),
    "receipts": ("受け取りの内訳", "text", None),
}


class TaskTracker(Protocol):
    def create_task(self, slip: Slip) -> tuple[str, str]: ...
    def update_task(self, slip: Slip) -> None: ...
    def read_overrides(self, since: str | None = None) -> list[dict]: ...
    def reset_override(self, task_gid: str) -> None: ...


class AsanaAPI:
    def __init__(self, token: str) -> None:
        self.headers = {"Authorization": f"Bearer {token}"}

    def call(self, method: str, path: str, data: dict | None = None, params: dict | None = None,
             idempotent: bool | None = None) -> dict | list:
        r = request("Asana", method, f"{API}{path}", headers=self.headers, params=params, idempotent=idempotent,
                    json={"data": data} if data is not None else None)
        return r.json().get("data", {})

    def list(self, path: str, params: dict) -> list[dict]:
        """ページをすべてめくって返す（100件ずつ）。"""
        out, offset = [], None
        for _ in range(200):
            q = {**params, "limit": 100, **({"offset": offset} if offset else {})}
            body = request("Asana", "GET", f"{API}{path}", headers=self.headers, params=q).json()
            out += body.get("data", [])
            offset = (body.get("next_page") or {}).get("offset")
            if not offset:
                break
        return out


class AsanaTracker:
    def __init__(self, token: str, ids: dict, env: str) -> None:
        self.api = AsanaAPI(token)
        self.fields = ids["custom_fields"]
        self.project = ids[env]["project"]
        self.sections = ids[env]["sections"]

    def _custom_fields(self, slip: Slip) -> dict:
        f = self.fields
        values: dict = {
            f["status"]["gid"]: f["status"]["options"][slip.status],
            f["unpaid"]["gid"]: f["unpaid"]["options"].get(slip.unpaid_status),
            f["customer"]: slip.customer_name or "",
            f["sender"]: slip.sender_display_name or "",
            f["slip_id"]: slip.slip_id,
        }
        if slip.business_date:
            values[f["business_date"]] = {"date": slip.business_date}
        if slip.slip_amount is not None:
            values[f["slip_amount"]] = slip.slip_amount
        if slip.unpaid_amount:
            values[f["unpaid_remaining"]] = slip.unpaid_remaining
        if slip.slack.get("permalink"):
            values[f["slack_url"]] = slip.slack["permalink"]
        if slip.payment_ids:
            values[f["square_amount"]] = slip.square.get("amount")
            values[f["fee"]] = slip.square.get("fee") or 0
            values[f["payment_id"]] = ", ".join(slip.payment_ids)
        if slip.source == "pos":
            from .texts import pos_referrals
            method = {"cash": "現金", "square": "Square", "unpaid": "未収"}
            pos_values = {
                "customer_codes": ", ".join(c.get("code", "") for c in slip.pos.get("customers") or []),
                "referrals": pos_referrals(slip),
                "pos_visit": slip.pos.get("visit_id", ""),
                "receipts": "、".join(f"{method.get(r['method'], r['method'])} {r['amount']:,}"
                                     for r in slip.pos.get("receipts") or []),
            }
            for key, value in pos_values.items():
                if f.get(key):   # 欄を作っていない店舗（Bar null）では書かない
                    values[f[key]] = value
        expected = slip.deposits.get("expected") or []
        if expected:
            values[f["deposit_expected"]] = sum(e.get("share", 0) for e in expected)
            dates = [e["confirmed"]["date"] for e in expected if e.get("confirmed")]
            if dates and len(dates) == len(expected):
                values[f["deposit_date"]] = {"date": max(dates)}
        return values

    def create_task(self, slip: Slip) -> tuple[str, str]:
        task = self.api.call("POST", "/tasks", {
            "name": asana_task_name(slip), "notes": asana_notes(slip), "projects": [self.project],
            "memberships": [{"project": self.project, "section": self.sections[slip.status]}],
            "custom_fields": self._custom_fields(slip),
        }, params={"opt_fields": "gid,permalink_url"})
        return task["gid"], task.get("permalink_url", "")

    def update_task(self, slip: Slip) -> None:
        """名前・カスタムフィールド・完了・セクションを、伝票の今の状態に合わせる（何度呼んでも同じ）。"""
        gid = slip.asana["task_gid"]
        self.api.call("PUT", f"/tasks/{gid}", {"name": asana_task_name(slip), "completed": bool(slip.closed_at),
                                                "custom_fields": self._custom_fields(slip)})
        if slip.status in self.sections:
            self.api.call("POST", f"/sections/{self.sections[slip.status]}/addTask", {"task": gid}, idempotent=True)

    def read_overrides(self, since: str | None = None) -> list[dict]:
        """「手動上書き」が「なし」以外のタスク。誰が変えたかはストーリー（変更履歴）から取る。"""
        field = self.fields["manual"]
        none_gid = field["options"].get(Manual.NONE)
        # 完了したタスクも読む（完了後のアラートを手動上書きで閉じられるように）
        params = {"project": self.project,
                  "opt_fields": "custom_fields.gid,custom_fields.enum_value.gid,custom_fields.enum_value.name,modified_at"}
        if since:
            params["modified_since"] = since
        out = []
        for t in self.api.list("/tasks", params):
            cf = next((c for c in t.get("custom_fields", []) if c["gid"] == field["gid"]), None)
            ev = (cf or {}).get("enum_value") or {}
            if not ev or ev.get("gid") == none_gid:
                continue
            by, at = None, t.get("modified_at")
            stories = self.api.list(f"/tasks/{t['gid']}/stories", {"opt_fields": "created_by.name,created_at,text"})
            for st in reversed(stories):
                if "手動上書き" in (st.get("text") or ""):
                    by, at = (st.get("created_by") or {}).get("name"), st.get("created_at")
                    break
            out.append({"task_gid": t["gid"], "value": ev.get("name"), "by": by, "at": at})
        return out

    def reset_override(self, task_gid: str) -> None:
        f = self.fields["manual"]
        self.api.call("PUT", f"/tasks/{task_gid}", {"custom_fields": {f["gid"]: f["options"][Manual.NONE]}})


# ---------------------------------------------------------------- セットアップ
def setup(api: AsanaAPI, *, workspace: str, team_name: str, project_name: str, env: str, ids_path: Path,
          dry_run: bool, pos: bool = False) -> dict:
    """プロジェクト・セクション・カスタムフィールドを冪等に作り、ID を ids_path に保存する。

    pos: POS の伝票の店舗（福岡）。POS_FIELDS も作る。カスタムフィールドはワークスペース共通なので、ids_path に
    Bar null の custom_fields を写しておけば、同じ欄をプロジェクトに足すだけになる（作り直さない）。"""
    spec = {**FIELDS, **POS_FIELDS} if pos else FIELDS
    ids = json.loads(ids_path.read_text(encoding="utf-8")) if ids_path.exists() else {}

    def act(desc: str, fn):
        print(("[dry-run] " if dry_run else "") + desc)
        return None if dry_run else fn()

    teams = api.list(f"/organizations/{workspace}/teams", {"opt_fields": "name"})
    team = next((t["gid"] for t in teams if t["name"] == team_name), None)
    if not team:
        raise SystemExit(f"チーム「{team_name}」が見つかりません")

    fields = ids.setdefault("custom_fields", {})
    for key, (name, kind, options) in spec.items():
        if key in fields:
            if kind == "enum":  # 後から足した選択肢（例: 対象外）を追加する
                for o in options:
                    if o not in fields[key]["options"]:
                        created = act(f"選択肢を追加: {name} に「{o}」",
                                      lambda k=key, o=o: api.call("POST", f"/custom_fields/{fields[k]['gid']}/enum_options",
                                                                  {"name": o}))
                        if created:
                            fields[key]["options"][o] = created["gid"]
            continue
        data: dict = {"workspace": workspace, "name": name}
        if kind == "enum":
            data |= {"resource_subtype": "enum", "enum_options": [{"name": o} for o in options]}
        elif kind == "currency":
            data |= {"resource_subtype": "number", "format": "currency", "currency_code": "JPY", "precision": 0}
        else:
            data |= {"resource_subtype": kind}
        created = act(f"カスタムフィールドを作成: {name}（{kind}）",
                      lambda d=data: api.call("POST", "/custom_fields", d,
                                              params={"opt_fields": "gid,enum_options.name,enum_options.gid"}))
        if created:
            fields[key] = ({"gid": created["gid"], "options": {o["name"]: o["gid"] for o in created["enum_options"]}}
                           if kind == "enum" else created["gid"])

    env_ids = ids.setdefault(env, {})
    if "project" not in env_ids:
        p = act(f"プロジェクトを作成: {project_name}（チーム {team_name}、非公開）",
                lambda: api.call("POST", "/projects", {"name": project_name, "team": team, "workspace": workspace,
                                                       "privacy_setting": "private", "default_view": "list"}))
        if p:
            env_ids["project"] = p["gid"]
    project = env_ids.get("project")
    if project:
        existing = api.list(f"/projects/{project}/custom_field_settings", {"opt_fields": "custom_field.gid"})
        attached = {s["custom_field"]["gid"] for s in existing}
        for key, (name, _, _) in spec.items():
            gid = fields[key]["gid"] if isinstance(fields.get(key), dict) else fields.get(key)
            if gid and gid not in attached:
                act(f"フィールドをプロジェクトに追加: {name}",
                    lambda g=gid: api.call("POST", f"/projects/{project}/addCustomFieldSetting", {"custom_field": g}))
        sections = env_ids.setdefault("sections", {})
        current = {s["name"]: s["gid"] for s in api.list(f"/projects/{project}/sections", {"opt_fields": "name"})}
        for name in SECTIONS:
            if name in current:
                sections[name] = current[name]
            elif name not in sections:
                s = act(f"セクションを作成: {name}",
                        lambda n=name: api.call("POST", f"/projects/{project}/sections", {"name": n}))
                if s:
                    sections[name] = s["gid"]
    else:
        print(f"[dry-run] セクションを作成: {', '.join(SECTIONS)}")

    if not dry_run:
        ids_path.write_text(json.dumps(ids, ensure_ascii=False, indent=2), encoding="utf-8")
        print("保存:", ids_path)
    return ids
