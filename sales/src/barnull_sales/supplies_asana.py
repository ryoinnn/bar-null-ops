"""Asana: 備品・消耗品（1タスク = マネフォのカード明細1件）。プロジェクト「Bar null 備品消耗品」（非公開）。

ID は asana_ids.json の "supplies" に保存する（`barnull-sales supplies-setup --env test|prod`）。契約は docs/profit/contract.md §3b。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Protocol

from .asana import AsanaAPI

SECTIONS = ("備品消耗品", "対象外")
CATEGORIES = ("備品・消耗品", "ツール・サブスク", "その他")
SOURCES = ("マネフォカード", "手入力")
STATUSES = ("取り込み済み", "対象外")
MANUAL = ("なし", "あり")
PROJECT_NAME = "Bar null 備品消耗品"
# key: (表示名, 種類, enum の選択肢)
FIELDS = {
    "record_id": ("備品記録ID", "text", None),
    "business_date": ("営業日", "date", None),
    "payee": ("支払先", "text", None),
    "amount": ("金額（税込）", "currency", None),
    "category": ("備品区分", "enum", CATEGORIES),
    "source": ("取得元", "enum", SOURCES),
    "mf_id": ("MF明細ID", "text", None),
    "s_status": ("備品ステータス", "enum", STATUSES),
    "manual": ("手動修正", "enum", MANUAL),
    "store": ("店舗", "text", None),
}


class SuppliesTracker(Protocol):
    def list_tasks(self) -> list[dict]: ...
    def upsert(self, gid: str | None, name: str, values: dict, notes: str | None, section: str) -> str: ...


class AsanaSuppliesTracker:
    """values は {欄の key: 値}（enum は選択肢の名前、date は YYYY-MM-DD、None は空）。"""

    def __init__(self, token: str, ids: dict, env: str) -> None:
        self.api = AsanaAPI(token)
        p = ids["supplies"]
        self.fields = p["custom_fields"]
        self.project = p[env]["project"]
        self.sections = p[env]["sections"]

    def _gid(self, key: str) -> str:
        f = self.fields[key]
        return f["gid"] if isinstance(f, dict) else f

    def _encode(self, values: dict) -> dict:
        out = {}
        for key, v in values.items():
            f = self.fields.get(key)
            if f is None:
                continue
            kind = FIELDS[key][1]
            if kind == "enum":
                out[f["gid"]] = f["options"].get(v) if v else None
            elif kind == "date":
                out[f] = {"date": v} if v else None
            elif kind == "text":
                out[f] = v or ""
            else:
                out[f] = v
        return out

    def _decode(self, task: dict) -> dict:
        by_gid = {self._gid(k): k for k in self.fields}
        sec_by_gid = {g: n for n, g in self.sections.items()}
        section = next((sec_by_gid.get((m.get("section") or {}).get("gid")) for m in task.get("memberships") or []
                        if (m.get("project") or {}).get("gid") == self.project), None)
        out = {"gid": task["gid"], "name": task.get("name", ""), "notes": task.get("notes", ""), "section": section}
        for c in task.get("custom_fields", []):
            key = by_gid.get(c.get("gid"))
            if not key:
                continue
            t = c.get("resource_subtype") or c.get("type")
            if t == "enum":
                out[key] = (c.get("enum_value") or {}).get("name")
            elif t == "number":
                n = c.get("number_value")
                out[key] = int(n) if isinstance(n, float) and n.is_integer() else n
            elif t == "date":
                out[key] = (c.get("date_value") or {}).get("date")
            else:
                out[key] = c.get("text_value") or ""
        return out

    def list_tasks(self) -> list[dict]:
        params = {"project": self.project,
                  "opt_fields": "name,notes,memberships.project.gid,memberships.section.gid,custom_fields.gid,"
                                "custom_fields.resource_subtype,custom_fields.type,custom_fields.text_value,"
                                "custom_fields.number_value,custom_fields.date_value,custom_fields.enum_value.name"}
        return [self._decode(t) for t in self.api.list("/tasks", params)]

    def upsert(self, gid, name, values, notes, section):
        data: dict = {"name": name, "custom_fields": self._encode(values)}
        if notes is not None:
            data["notes"] = notes
        if gid:
            self.api.call("PUT", f"/tasks/{gid}", data)
            self.api.call("POST", f"/sections/{self.sections[section]}/addTask", {"task": gid}, idempotent=True)
            return gid
        task = self.api.call("POST", "/tasks", {
            **data, "projects": [self.project],
            "memberships": [{"project": self.project, "section": self.sections[section]}]},
            params={"opt_fields": "gid"})
        return task["gid"]


def setup(api: AsanaAPI, *, workspace: str, team_name: str, env: str, ids_path: Path, dry_run: bool) -> dict:
    """プロジェクト・セクション・カスタムフィールドを冪等に作る（非公開）。同名・同種の欄が既にあれば使う（営業日・店舗など）。"""
    ids = json.loads(ids_path.read_text(encoding="utf-8")) if ids_path.exists() else {}
    sup = ids.setdefault("supplies", {})
    fields = sup.setdefault("custom_fields", {})

    def act(desc: str, fn):
        print(("[dry-run] " if dry_run else "") + desc)
        return None if dry_run else fn()

    teams = api.list(f"/organizations/{workspace}/teams", {"opt_fields": "name"})
    team = next((t["gid"] for t in teams if t["name"] == team_name), None)
    if not team:
        raise SystemExit(f"チーム「{team_name}」が見つかりません")
    existing = {f["name"]: f for f in api.list(f"/workspaces/{workspace}/custom_fields",
                                                {"opt_fields": "name,resource_subtype,enum_options.name"})}
    for key, (name, kind, opt) in FIELDS.items():
        if key in fields:
            continue
        ex = existing.get(name)
        want = "number" if kind == "currency" else kind
        if ex and ex.get("resource_subtype") == want:
            if kind == "enum":
                options = {o["name"]: o["gid"] for o in ex.get("enum_options") or []}
                for o in opt:
                    if o not in options:
                        made = act(f"選択肢を追加: {name} に「{o}」", lambda g=ex["gid"], o=o: api.call(
                            "POST", f"/custom_fields/{g}/enum_options", {"name": o}))
                        if made:
                            options[o] = made["gid"]
                fields[key] = {"gid": ex["gid"], "options": options}
            else:
                fields[key] = ex["gid"]
            print(f"既にある欄を使う: {name}")
            continue
        if ex:
            raise SystemExit(f"同じ名前の欄「{name}」が別の種類（{ex.get('resource_subtype')}）で既にあります")
        data: dict = {"workspace": workspace, "name": name}
        if kind == "enum":
            data |= {"resource_subtype": "enum", "enum_options": [{"name": o} for o in opt]}
        elif kind == "currency":
            data |= {"resource_subtype": "number", "format": "currency", "currency_code": "JPY", "precision": 0}
        else:
            data |= {"resource_subtype": kind}
        made = act(f"カスタムフィールドを作成: {name}（{kind}）", lambda d=data: api.call(
            "POST", "/custom_fields", d, params={"opt_fields": "gid,enum_options.name,enum_options.gid"}))
        if made:
            fields[key] = ({"gid": made["gid"], "options": {o["name"]: o["gid"] for o in made["enum_options"]}}
                           if kind == "enum" else made["gid"])

    env_ids = sup.setdefault(env, {})
    name = PROJECT_NAME + ("［テスト］" if env == "test" else "")
    if "project" not in env_ids:
        p = act(f"プロジェクトを作成: {name}（非公開）", lambda: api.call(
            "POST", "/projects", {"name": name, "team": team, "workspace": workspace, "privacy_setting": "private",
                                  "default_view": "list"}))
        if p:
            env_ids["project"] = p["gid"]
    project = env_ids.get("project")
    if project:
        attached = {s["custom_field"]["gid"] for s in api.list(f"/projects/{project}/custom_field_settings",
                                                                {"opt_fields": "custom_field.gid"})}
        for key, (fname, _, _) in FIELDS.items():
            f = fields.get(key)
            gid = f["gid"] if isinstance(f, dict) else f
            if gid and gid not in attached:
                act(f"欄をプロジェクトに追加: {fname}", lambda g=gid: api.call(
                    "POST", f"/projects/{project}/addCustomFieldSetting", {"custom_field": g}))
        secs = env_ids.setdefault("sections", {})
        have = {s["name"]: s["gid"] for s in api.list(f"/projects/{project}/sections", {"opt_fields": "name"})}
        for sname in SECTIONS:
            if sname in secs:
                continue
            if sname in have:
                secs[sname] = have[sname]
                continue
            s = act(f"セクションを作成: {sname}", lambda n=sname: api.call(
                "POST", f"/projects/{project}/sections", {"name": n}))
            if s:
                secs[sname] = s["gid"]
    if not dry_run:
        ids_path.write_text(json.dumps(ids, ensure_ascii=False, indent=2), encoding="utf-8")
        print("保存:", ids_path)
    return ids
