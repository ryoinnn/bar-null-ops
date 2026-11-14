"""Asana: 勤怠（1人 × 1営業日）、スタッフ台帳（1人）、給与（1人 × 1か月）。どれも管理者だけが見られる非公開プロジェクト。

ID は asana_ids.json の "labor" に保存する（`barnull-sales asana-setup-labor --env test|prod`）。
システムが書く欄と、管理者が書く欄を分ける（台帳の条件・給与の調整額は管理者だけが書き、システムは上書きしない）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Protocol

from .asana import AsanaAPI
from .staff import KINDS

ATT_STATUS = ("記録済み", "対象外")
ATT_MANUAL = ("なし", "時間を反映", "対象外")
PAY_STATUS = ("見込み", "計算済み", "確定")
# key: (表示名, 種類, enum の選択肢)。number は (表示名, "number", 小数の桁)
FIELDS = {
    "staff_name": ("スタッフ名", "text", None),
    "work_date": ("稼働日", "date", None),
    "hours": ("稼働時間（h）", "number", 2),
    "start": ("勤務開始", "text", None),
    "end": ("勤務終了", "text", None),
    "own_sales": ("売上（本人）", "currency", None),
    "wage_amount": ("時給の額", "currency", None),
    "att_status": ("勤怠ステータス", "enum", ATT_STATUS),
    "record_id": ("勤怠記録ID", "text", None),
    "staff_id": ("スタッフID", "text", None),
    "store": ("店舗", "text", None),
    "att_manual": ("勤怠の手動上書き", "enum", ATT_MANUAL),
    "real_name": ("本名", "text", None),
    "line_name": ("LINE名", "text", None),
    "staff_kind": ("区分", "enum", KINDS),
    "joined": ("入社日", "date", None),
    "wage": ("時給", "currency", None),
    "wage_from": ("時給の適用開始日", "date", None),
    "back_rate": ("バック率（%）", "number", 1),
    "line_ids": ("LINE ID", "text", None),
    "month": ("対象月", "text", None),
    "days": ("勤務日数", "number", 0),
    "count": ("顧客組数", "number", 0),
    "wage_part": ("時給の分", "currency", None),
    "back": ("バック", "currency", None),
    "adjustment": ("調整額", "currency", None),
    "adjust_reason": ("調整の理由", "text", None),
    "pay": ("給与", "currency", None),
    "pay_status": ("給与ステータス", "enum", PAY_STATUS),
}
PROJECTS = {
    # key: (名前, 使う欄)。Slackスレッドは伝票と同じ欄（custom_fields.slack_url）を使う
    "attendance": ("Bar null 勤怠", ("staff_name", "line_name", "work_date", "hours", "start", "end", "own_sales", "wage_amount",
                                    "att_status", "att_manual", "record_id", "staff_id", "store", "slack_url")),
    "staff": ("Bar null スタッフ台帳", ("real_name", "line_name", "staff_kind", "joined", "wage", "wage_from",
                                      "back_rate", "staff_id", "line_ids")),
    "payroll": ("Bar null 給与", ("month", "staff_name", "staff_kind", "days", "hours", "own_sales", "count",
                                "wage_part", "back_rate", "back", "adjustment", "adjust_reason", "pay", "pay_status",
                                "staff_id")),
}


class LaborTracker(Protocol):
    def upsert_attendance(self, gid: str | None, name: str, values: dict) -> tuple[str, str]: ...
    def read_attendance_overrides(self, since: str | None) -> list[dict]: ...
    def reset_attendance_override(self, gid: str) -> None: ...
    def list_staff(self) -> list[dict]: ...
    def upsert_staff(self, gid: str | None, name: str, values: dict) -> tuple[str, str]: ...
    def upsert_payroll(self, gid: str | None, name: str, values: dict) -> tuple[str, str]: ...
    def read_payroll_adjustments(self, since: str | None) -> list[dict]: ...


class AsanaLaborTracker:
    """values は {欄の key: 値}（enum は選択肢の名前、date は YYYY-MM-DD、None は空にする）。"""

    def __init__(self, token: str, ids: dict, env: str) -> None:
        self.api = AsanaAPI(token)
        labor = ids["labor"]
        self.fields = {**labor["custom_fields"], "slack_url": ids["custom_fields"]["slack_url"]}
        self.projects = labor[env]

    def _gid(self, key: str) -> str:
        f = self.fields[key]
        return f["gid"] if isinstance(f, dict) else f

    def _encode(self, values: dict) -> dict:
        out = {}
        for key, v in values.items():
            f = self.fields.get(key)
            if f is None:
                continue
            kind = FIELDS[key][1] if key in FIELDS else "text"
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
        out = {"gid": task["gid"], "modified_at": task.get("modified_at")}
        for c in task.get("custom_fields", []):
            key = by_gid.get(c.get("gid"))
            if not key:
                continue
            t = c.get("resource_subtype") or c.get("type")
            if t == "enum":
                out[key] = (c.get("enum_value") or {}).get("name")
            elif t == "number":
                out[key] = c.get("number_value")
            elif t == "date":
                out[key] = (c.get("date_value") or {}).get("date")
            else:
                out[key] = c.get("text_value") or ""
        return out

    def _upsert(self, project_key: str, gid: str | None, name: str, values: dict) -> tuple[str, str]:
        data = {"name": name, "custom_fields": self._encode(values)}
        if gid:
            self.api.call("PUT", f"/tasks/{gid}", data)
            return gid, ""
        task = self.api.call("POST", "/tasks", {**data, "projects": [self.projects[project_key]]},
                             params={"opt_fields": "gid,permalink_url"})
        return task["gid"], task.get("permalink_url", "")

    def _list(self, project_key: str, since: str | None) -> list[dict]:
        params = {"project": self.projects[project_key],
                  "opt_fields": "name,modified_at,custom_fields.gid,custom_fields.resource_subtype,custom_fields.type,"
                                "custom_fields.text_value,custom_fields.number_value,custom_fields.date_value,"
                                "custom_fields.enum_value.name"}
        if since:
            params["modified_since"] = since
        return [self._decode(t) for t in self.api.list("/tasks", params)]

    def upsert_attendance(self, gid, name, values):
        return self._upsert("attendance", gid, name, values)

    def read_attendance_overrides(self, since):
        return [t for t in self._list("attendance", since) if t.get("att_manual") not in (None, "", "なし")]

    def reset_attendance_override(self, gid):
        self.api.call("PUT", f"/tasks/{gid}", {"custom_fields": self._encode({"att_manual": "なし"})})

    def list_staff(self):
        return self._list("staff", None)

    def upsert_staff(self, gid, name, values):
        return self._upsert("staff", gid, name, values)

    def upsert_payroll(self, gid, name, values):
        return self._upsert("payroll", gid, name, values)

    def read_payroll_adjustments(self, since):
        return self._list("payroll", since)


# ---------------------------------------------------------------- セットアップ
def setup(api: AsanaAPI, *, workspace: str, team_name: str, env: str, ids_path: Path, dry_run: bool) -> dict:
    """勤怠・台帳・給与のプロジェクトとカスタムフィールドを冪等に作る（非公開）。
    同じ名前のカスタムフィールドがワークスペースに既にあれば、それを使う（作り直さない）。"""
    ids = json.loads(ids_path.read_text(encoding="utf-8")) if ids_path.exists() else {}
    labor = ids.setdefault("labor", {})
    fields = labor.setdefault("custom_fields", {})

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
            if kind == "enum":   # 後から足した選択肢（例: 見込み）を追加する
                for o in opt:
                    if o not in fields[key]["options"]:
                        made = act(f"選択肢を追加: {name} に「{o}」", lambda k=key, o=o: api.call(
                            "POST", f"/custom_fields/{fields[k]['gid']}/enum_options", {"name": o}))
                        if made:
                            fields[key]["options"][o] = made["gid"]
            continue
        ex = existing.get(name)
        want = "number" if kind in ("currency", "number") else kind
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
        elif kind == "number":
            data |= {"resource_subtype": "number", "precision": opt}
        else:
            data |= {"resource_subtype": kind}
        made = act(f"カスタムフィールドを作成: {name}（{kind}）", lambda d=data: api.call(
            "POST", "/custom_fields", d, params={"opt_fields": "gid,enum_options.name,enum_options.gid"}))
        if made:
            fields[key] = ({"gid": made["gid"], "options": {o["name"]: o["gid"] for o in made["enum_options"]}}
                           if kind == "enum" else made["gid"])

    env_ids = labor.setdefault(env, {})
    suffix = "［テスト］" if env == "test" else ""
    for pkey, (pname, keys) in PROJECTS.items():
        if pkey not in env_ids:
            p = act(f"プロジェクトを作成: {pname}{suffix}（非公開）", lambda n=pname + suffix: api.call(
                "POST", "/projects", {"name": n, "team": team, "workspace": workspace, "privacy_setting": "private",
                                      "default_view": "list"}))
            if p:
                env_ids[pkey] = p["gid"]
        project = env_ids.get(pkey)
        if not project:
            continue
        attached = {s["custom_field"]["gid"] for s in api.list(f"/projects/{project}/custom_field_settings",
                                                                {"opt_fields": "custom_field.gid"})}
        for key in keys:
            f = ids["custom_fields"]["slack_url"] if key == "slack_url" else fields.get(key)
            gid = f["gid"] if isinstance(f, dict) else f
            if gid and gid not in attached:
                act(f"欄をプロジェクトに追加: {pname}{suffix} ← {FIELDS.get(key, ('Slackスレッド',))[0]}",
                    lambda g=gid, p=project: api.call("POST", f"/projects/{p}/addCustomFieldSetting",
                                                      {"custom_field": g}))
    if not dry_run:
        ids_path.write_text(json.dumps(ids, ensure_ascii=False, indent=2), encoding="utf-8")
        print("保存:", ids_path)
    return ids
