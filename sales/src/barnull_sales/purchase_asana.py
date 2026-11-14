"""Asana: 仕入（1タスク = 1伝票）と商品マスタ（1タスク = 1商品）。プロジェクト「Bar null 仕入」（非公開）。

ID は asana_ids.json の "purchase" に保存する（`barnull-sales purchase-setup --env test|prod`）。
商品マスタの表示名・区分は、本人が画面（ポータル）か Asana で直す。確認状態が「確認済み」のものはシステムが上書きしない。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from .asana import AsanaAPI
from .purchase import CATEGORIES, MASTER_STATUS, SUPPLIER, VOUCHER_STATUS, Line, Voucher, category_of

SECTIONS = ("仕入", "要確認", "対象外", "商品マスタ")
# key: (表示名, 種類, enum の選択肢 / number の小数の桁)
FIELDS = {
    "record_id": ("仕入記録ID", "text", None),
    "business_date": ("営業日", "date", None),
    "supplier": ("仕入先", "text", None),
    "voucher": ("伝票番号", "text", None),
    "order_no": ("注文番号", "text", None),
    "net": ("仕入金額（税抜）", "currency", None),
    "tax": ("消費税（推定）", "currency", None),
    "gross": ("仕入金額（税込）", "currency", None),
    "qty": ("仕入数量", "number", 0),
    "items": ("品目数", "number", 0),
    "summary": ("品目の要約", "text", None),
    "p_status": ("仕入ステータス", "enum", VOUCHER_STATUS),
    "store": ("店舗", "text", None),
    "item_code": ("商品コード", "text", None),
    "raw_name": ("元の商品名", "text", None),
    "display_name": ("表示名", "text", None),
    "p_category": ("仕入区分", "enum", CATEGORIES),
    "nos_category": ("NOSカテゴリ", "text", None),
    "m_status": ("確認状態", "enum", MASTER_STATUS),
}
VOUCHER_KEYS = ("record_id", "business_date", "supplier", "voucher", "order_no", "net", "tax", "gross", "qty", "items",
                "summary", "p_status", "store")
MASTER_KEYS = ("record_id", "item_code", "raw_name", "display_name", "p_category", "nos_category", "m_status",
               "supplier", "store")   # 営業日は付けない（伝票の集計に混ざらないように）
PROJECT_NAME = "Bar null 仕入"


class PurchaseTracker(Protocol):
    def list_tasks(self) -> list[dict]: ...
    def upsert(self, gid: str | None, name: str, values: dict, notes: str | None, section: str) -> str: ...


class AsanaPurchaseTracker:
    """values は {欄の key: 値}（enum は選択肢の名前、date は YYYY-MM-DD、None は空）。"""

    def __init__(self, token: str, ids: dict, env: str) -> None:
        self.api = AsanaAPI(token)
        p = ids["purchase"]
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


# ---------------------------------------------------------------- 取り込み（冪等）
@dataclass
class SyncStats:
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    excluded: int = 0
    renamed: int = 0                                      # 表示名の案を作り直した商品の数（--rename-drafts）
    new_items: list[str] = field(default_factory=list)   # 新しい商品の表示名（案）
    review: list[str] = field(default_factory=list)      # 要確認の理由
    names_by_claude: bool = True


def _same(existing: dict, values: dict, notes: str | None, section: str) -> bool:
    if existing.get("section") != section:
        return False
    if notes is not None and (existing.get("notes") or "").strip() != notes.strip():
        return False
    for k, v in values.items():
        cur = existing.get(k)
        if (cur in (None, "") and v in (None, "")) or cur == v:
            continue
        return False
    return True


def voucher_values(v: Voucher, names: dict[str, str], store: str = "") -> dict:
    return {"record_id": v.record_id, "business_date": v.date, "supplier": SUPPLIER, "voucher": v.voucher,
            "order_no": v.order_nos, "net": v.net, "tax": v.tax, "gross": v.gross, "qty": v.qty, "items": len(v.lines),
            "summary": v.summary(names), "p_status": v.status, "store": store}


def voucher_name(v: Voucher, names: dict[str, str]) -> str:
    kind = "返品" if v.net < 0 else "仕入"
    return f"{v.date} {kind} ¥{v.gross:,}（{v.summary(names)}）"


def sync(tracker: PurchaseTracker, vouchers: list[Voucher], since: str, until: str, suggest, *,
         store: str = "", dry_run: bool = False, rename_drafts: bool = False, log=print) -> SyncStats:
    """伝票と商品マスタを Asana に合わせる。suggest({キー: 元の商品名}) -> ({キー: 表示名}, Claude を使えたか)。

    - 同じ記録ID のタスクがあれば直す（変わっていなければ触らない）。無ければ作る
    - 期間（since〜until）の伝票で、今回の NOS に無いものは「対象外」にする（削除しない）
    - 商品マスタは、新しい商品だけ作る（表示名は案）。既存の表示名・区分・確認状態は変えない
    """
    st = SyncStats()
    tasks = tracker.list_tasks()
    by_id = {t.get("record_id"): t for t in tasks if t.get("record_id")}

    # 商品マスタ
    lines: dict[str, Line] = {}
    for v in vouchers:
        for ln in v.lines:
            lines.setdefault(ln.key, ln)
    names = {t["item_code"] or t["raw_name"]: t.get("display_name") or ""
             for t in tasks if (t.get("record_id") or "").startswith("M-")}
    new = {k: ln.name for k, ln in lines.items() if k not in names}
    if new:
        suggested, st.names_by_claude = suggest(new)
        for k, ln in lines.items():
            if k not in new:
                continue
            values = {"record_id": f"M-{k}", "item_code": ln.code, "raw_name": ln.name,
                      "display_name": suggested.get(k) or ln.name, "p_category": category_of(ln.nos_category),
                      "nos_category": ln.nos_category, "m_status": "案（自動）", "supplier": SUPPLIER, "store": store}
            log(("[dry-run] " if dry_run else "") + f"商品マスタを作成: {values['display_name']}")
            if not dry_run:
                tracker.upsert(None, values["display_name"], values, None, "商品マスタ")
            names[k] = values["display_name"]
            st.new_items.append(values["display_name"])
    if rename_drafts:
        # 案（自動）のままの商品マスタの表示名を作り直す（確認済みは触らない）
        drafts = {(t["item_code"] or t["raw_name"]): t for t in tasks
                  if (t.get("record_id") or "").startswith("M-") and t.get("m_status") != "確認済み"}
        if drafts:
            suggested, ok = suggest({k: t.get("raw_name") or k for k, t in drafts.items()})
            st.names_by_claude = st.names_by_claude and ok
            for k, t in drafts.items():
                new_name = suggested.get(k)
                if not new_name or new_name == t.get("display_name"):
                    continue
                log(("[dry-run] " if dry_run else "") + f"表示名の案を作り直し: {t.get('display_name')} → {new_name}")
                if not dry_run:
                    tracker.upsert(t["gid"], new_name, {"display_name": new_name}, None, "商品マスタ")
                names[k] = new_name
                st.renamed += 1

    # 伝票
    seen = set()
    for v in vouchers:
        seen.add(v.record_id)
        values = voucher_values(v, names, store)
        notes = v.notes(names)
        section = "要確認" if v.status == "要確認" else "仕入"
        if v.unknown_categories:
            st.review.append(f"伝票 {v.voucher}: 区分の分からない NOS カテゴリ（{', '.join(v.unknown_categories)}）")
        ex = by_id.get(v.record_id)
        if ex and _same(ex, values, notes, section):
            st.unchanged += 1
            continue
        log(("[dry-run] " if dry_run else "") + ("伝票を更新: " if ex else "伝票を作成: ") + v.record_id)
        if not dry_run:
            tracker.upsert(ex["gid"] if ex else None, voucher_name(v, names), values, notes, section)
        if ex:
            st.updated += 1
        else:
            st.created += 1

    # 今回の NOS に無い伝票（期間内）は対象外にする
    for rid, t in by_id.items():
        if not rid.startswith("P-") or rid in seen:
            continue
        d = t.get("business_date") or ""
        if not (since <= d <= until) or t.get("p_status") == "対象外":
            continue
        log(("[dry-run] " if dry_run else "") + f"伝票を対象外に: {rid}")
        if not dry_run:
            tracker.upsert(t["gid"], t.get("name") or rid, {"p_status": "対象外"}, None, "対象外")
        st.excluded += 1
    return st


# ---------------------------------------------------------------- セットアップ
def setup(api: AsanaAPI, *, workspace: str, team_name: str, env: str, ids_path: Path, dry_run: bool) -> dict:
    """プロジェクト・セクション・カスタムフィールドを冪等に作る（非公開）。
    同じ名前のカスタムフィールドがワークスペースに同じ種類で既にあれば、それを使う（営業日・店舗など）。"""
    ids = json.loads(ids_path.read_text(encoding="utf-8")) if ids_path.exists() else {}
    pur = ids.setdefault("purchase", {})
    fields = pur.setdefault("custom_fields", {})

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

    env_ids = pur.setdefault(env, {})
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
        sections = env_ids.setdefault("sections", {})
        current = {s["name"]: s["gid"] for s in api.list(f"/projects/{project}/sections", {"opt_fields": "name"})}
        for sname in SECTIONS:
            if sname in current:
                sections[sname] = current[sname]
            elif sname not in sections:
                s = act(f"セクションを作成: {sname}", lambda n=sname: api.call(
                    "POST", f"/projects/{project}/sections", {"name": n}))
                if s:
                    sections[sname] = s["gid"]
    if not dry_run:
        ids_path.write_text(json.dumps(ids, ensure_ascii=False, indent=2), encoding="utf-8")
        print("保存:", ids_path)
    return ids
