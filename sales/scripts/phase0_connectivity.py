"""Phase 0: 各 API の疎通確認（使い捨てのスクリプト）。

実行（リポジトリのルートで）:
    uv run python sales/scripts/phase0_connectivity.py [square] [mf] [asana] [slack] [--post-slack-test]

- 読み取りだけを行う。--post-slack-test を付けたときだけ、テスト用チャンネルに投稿・スレッド返信・更新を1回ずつ行う
- 秘密情報: Windows 資格情報マネージャー（サービス barnull-sales）。画面やファイルには出さない
- 詳細: sales/samples/phase0_connectivity.json（個人情報になりうる項目は除く。git に入れない）
"""

from __future__ import annotations

import json
import os
import sys
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import keyring

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "sales" / "samples" / "phase0_connectivity.json"
JST = ZoneInfo("Asia/Tokyo")
BAR_NULL = "L7SVX8VVWDQBX"
SPICE_DRINK = "LD7FS4BS4298P"
TEST_CHANNEL = "test_barnull-sales"
PROD_CHANNEL = "barnull-sales"


def secret(name: str) -> str:
    v = (keyring.get_password("barnull-sales", name) or "").strip()
    if not v:
        sys.exit(f"{name} が未登録です")
    return v


def jst(ts: str | None) -> str | None:
    if not ts:
        return None
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(JST).strftime("%Y-%m-%d %H:%M")


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def money(m: dict | None) -> int | None:
    return None if not m else m.get("amount")


def brief_error(r: httpx.Response) -> str:
    return f"HTTP {r.status_code} {r.text[:300]}"


# ---------------------------------------------------------------- Square
def check_square(out: dict) -> None:
    print("\n== Square")
    c = httpx.Client(base_url="https://connect.squareup.com",
                     headers={"Authorization": f"Bearer {secret('SQUARE_ACCESS_TOKEN')}"}, timeout=30)
    res: dict = {}
    r = c.get("/v2/locations")
    if r.status_code != 200:
        print("  locations:", brief_error(r))
        out["square"] = {"error": brief_error(r)}
        return
    locs = [{k: loc.get(k) for k in ("id", "name", "status", "country", "currency", "timezone", "merchant_id")}
            for loc in r.json().get("locations", [])]
    res["locations"] = locs
    for loc in locs:
        print(f"  location {loc['id']} {loc['name']} status={loc['status']} {loc['country']} {loc['currency']}")
    print("  Square-Version（応答ヘッダー）:", r.headers.get("square-version"))

    now = datetime.now(UTC)
    payments: list[dict] = []
    cursor = None
    for _ in range(5):
        params = {"location_id": BAR_NULL, "begin_time": iso(now - timedelta(days=45)), "sort_order": "DESC",
                  "limit": 100}
        if cursor:
            params["cursor"] = cursor
        r = c.get("/v2/payments", params=params)
        if r.status_code != 200:
            print("  payments:", brief_error(r))
            break
        body = r.json()
        payments += body.get("payments", [])
        cursor = body.get("cursor")
        if not cursor:
            break
    slim = []
    for p in payments:
        fees = sum(money(f.get("amount_money")) or 0 for f in p.get("processing_fee", []) or [])
        slim.append({
            "id": p["id"], "created_jst": jst(p.get("created_at")), "status": p.get("status"),
            "source_type": p.get("source_type"), "amount": money(p.get("amount_money")),
            "total": money(p.get("total_money")), "tip": money(p.get("tip_money")),
            "refunded": money(p.get("refunded_money")), "fee": fees if p.get("processing_fee") else None,
            "card_brand": (p.get("card_details") or {}).get("card", {}).get("card_brand"),
            "wallet": (p.get("wallet_details") or {}).get("brand"),
            "external_type": (p.get("external_details") or {}).get("type"),
            "has_cash_details": "cash_details" in p, "has_note": bool(p.get("note")),
            "has_customer": bool(p.get("customer_id")), "has_order": bool(p.get("order_id")),
            "location_id": p.get("location_id"),
        })
    res["payments_45d"] = slim
    print(f"  Bar null の決済（45日）: {len(slim)} 件")
    print("   source_type:", dict(Counter(s["source_type"] for s in slim)))
    print("   status:", dict(Counter(s["status"] for s in slim)))
    print("   1日あたり件数（営業日≒日付）:", dict(Counter((s["created_jst"] or "")[:10] for s in slim).most_common(5)))
    print("   メモあり:", sum(s["has_note"] for s in slim), " 顧客あり:", sum(s["has_customer"] for s in slim),
          " 返金あり:", sum(1 for s in slim if s["refunded"]))
    for s in slim[:8]:
        print(f"   {s['created_jst']} {s['source_type']:8} total={s['total']} fee={s['fee']} "
              f"brand={s['card_brand'] or s['wallet'] or s['external_type'] or '-'} status={s['status']}")

    r = c.get("/v2/payments", params={"location_id": SPICE_DRINK, "begin_time": iso(now - timedelta(days=90)),
                                      "limit": 10})
    res["spice_drink_payments_90d"] = len(r.json().get("payments", [])) if r.status_code == 200 else brief_error(r)
    print("  SPICE DRINK の決済（90日）:", res["spice_drink_payments_90d"])

    r = c.get("/v2/payouts", params={"location_id": BAR_NULL, "begin_time": iso(now - timedelta(days=45)),
                                     "sort_order": "DESC", "limit": 20})
    if r.status_code != 200:
        print("  payouts:", brief_error(r))
        res["payouts"] = {"error": brief_error(r)}
    else:
        payouts = r.json().get("payouts", [])
        res["payouts"] = [{k: po.get(k) for k in ("id", "status", "type", "arrival_date", "created_at")}
                          | {"amount": money(po.get("amount_money")),
                             "payout_fee": [money(f.get("amount_money")) for f in po.get("payout_fee", []) or []]}
                          for po in payouts]
        print(f"  Payouts API: 使える（{len(payouts)} 件）")
        for po in res["payouts"][:8]:
            print(f"   {po['id']} {po['status']:6} {po['type']} arrival={po['arrival_date']} amount={po['amount']} "
                  f"fee={po['payout_fee']}")
        details = []
        for po in res["payouts"][:3]:
            r = c.get(f"/v2/payouts/{po['id']}/payout-entries", params={"limit": 100})
            if r.status_code != 200:
                print("   entries:", brief_error(r))
                continue
            entries = r.json().get("payout_entries", [])
            types = Counter(e.get("type") for e in entries)
            net = sum(money(e.get("net_amount_money")) or 0 for e in entries)
            pids = [(e.get("type_charge_details") or {}).get("payment_id") for e in entries]
            linked = [p for p in pids if p]
            known = {s["id"] for s in slim}
            details.append({"payout": po["id"], "types": dict(types), "net_sum": net, "payout_amount": po["amount"],
                            "linked_payments": len(linked), "linked_in_bar_null_list": sum(p in known for p in linked)})
            print(f"   entries {po['id']}: {dict(types)} net合計={net} 振込額={po['amount']} "
                  f"決済への紐付け={len(linked)}件（うち上の決済一覧にあるもの {sum(p in known for p in linked)}）")
        res["payout_entries"] = details
    out["square"] = res


# ---------------------------------------------------------------- Money Forward
def check_mf(out: dict) -> None:
    print("\n== マネーフォワード")
    res: dict = {}
    r = httpx.post("https://api.biz.moneyforward.com/auth/exchange",
                   headers={"Authorization": f"Bearer {secret('MF_API_KEY')}"}, timeout=30)
    if r.status_code != 200:
        print("  JWT 交換:", brief_error(r))
        out["mf"] = {"exchange_error": brief_error(r)}
        return
    tok = r.json()
    print(f"  JWT 交換: OK（expires_in={tok.get('expires_in')}）")
    h = {"Authorization": f"Bearer {tok['access_token']}"}

    r = httpx.get("https://api.biz.moneyforward.com/v2/tenant/tenant_user", headers=h, timeout=30)
    tenant_code = None
    if r.status_code == 200:
        body = r.json()
        tenant_code = body.get("tenant_code") or (body.get("tenant") or {}).get("code")
        res["tenant_user_keys"] = sorted(body.keys())
        print("  tenant_user: OK keys=", res["tenant_user_keys"], " tenant_code あり:", bool(tenant_code))
    else:
        print("  tenant_user:", brief_error(r))

    base = "https://api-accounting.moneyforward.com/api/v3"
    ac = httpx.Client(base_url=base, headers=h, timeout=30)
    office_code = os.environ.get("MF_OFFICE_CODE")
    tenant_code = office_code or tenant_code
    for params in ({}, {"office_code": tenant_code} if tenant_code else None):
        if params is None:
            continue
        r = ac.get("/offices", params=params)
        print(f"  GET /offices {params and 'office_code=…' or '(指定なし)'}: HTTP {r.status_code}")
        if r.status_code == 200:
            body = r.json()
            res["offices_shape"] = {k: type(v).__name__ for k, v in body.items()} if isinstance(body, dict) else "list"
            offices = body.get("offices", [body]) if isinstance(body, dict) else body
            for o in offices[:3]:
                office_code = o.get("code") or o.get("office_code") or office_code
                print("   office keys:", sorted(o.keys())[:20])
            break
        print("   ", r.text[:200])
    office_code = office_code or tenant_code
    q = {"office_code": office_code} if office_code else {}

    r = ac.get("/accounts", params=q)
    print(f"  GET /accounts: HTTP {r.status_code}")
    if r.status_code == 200:
        body = r.json()
        accounts = body.get("accounts", body if isinstance(body, list) else [])
        banks = []
        for a in accounts:
            name = a.get("name", "")
            if "預金" in name:
                subs = [{"id": s.get("id"), "name": s.get("name")} for s in a.get("sub_accounts", []) or []]
                banks.append({"id": a.get("id"), "name": name, "sub_accounts": subs})
        res["bank_accounts"] = banks
        for b in banks:
            print(f"   {b['name']} id={b['id']} 補助科目={[s['name'] for s in b['sub_accounts']]}")
    else:
        print("   ", r.text[:300])

    today = datetime.now(JST).date()
    params = q | {"start_date": str(today - timedelta(days=45)), "end_date": str(today)}
    r = ac.get("/transactions", params=params)
    print(f"  GET /transactions（45日）: HTTP {r.status_code}")
    if r.status_code != 200:
        print("   ", r.text[:400])
        out["mf"] = res
        return
    body = r.json()
    res["transactions_top_keys"] = sorted(body.keys()) if isinstance(body, dict) else "list"
    txs = body.get("transactions", body if isinstance(body, list) else [])
    print("   応答のキー:", res["transactions_top_keys"], " 件数:", len(txs))
    if txs:
        res["transaction_fields"] = sorted(txs[0].keys())
        print("   明細の項目:", res["transaction_fields"])
    by_acct = Counter((t.get("connected_account_id"), t.get("connected_sub_account_id")) for t in txs)
    res["by_connected_account"] = {f"{a}/{s}": n for (a, s), n in by_acct.items()}
    print("   口座ごとの件数:", res["by_connected_account"])
    income = [t for t in txs if str(t.get("side", "")).upper() in ("INCOME", "IN", "DEPOSIT")]
    square_like = [t for t in income if any(k in (t.get("content") or "") for k in ("スクエア", "ｽｸｴｱ", "SQUARE", "Square"))]
    res["income_count"] = len(income)
    res["square_like"] = [{"date": t.get("date"), "value": t.get("value"), "content": t.get("content"),
                           "status": t.get("journalizing_status")} for t in square_like]
    others = Counter((t.get("content") or "")[:12] for t in income if t not in square_like)
    res["other_income_content_prefixes"] = dict(others.most_common(15))
    print(f"   入金 {len(income)} 件、うち Square らしい摘要 {len(square_like)} 件")
    for t in res["square_like"][:10]:
        print(f"    {t['date']} {t['value']} 「{t['content']}」 {t['status']}")
    print("   ほかの入金の摘要（先頭12文字と件数）:", res["other_income_content_prefixes"])
    print("   仕訳ステータス:", dict(Counter(t.get("journalizing_status") for t in txs)))
    out["mf"] = res


# ---------------------------------------------------------------- Asana
def check_asana(out: dict) -> None:
    print("\n== Asana")
    c = httpx.Client(base_url="https://app.asana.com/api/1.0",
                     headers={"Authorization": f"Bearer {secret('ASANA_PAT')}"}, timeout=30)
    res: dict = {}
    r = c.get("/users/me", params={"opt_fields": "email,workspaces.name"})
    if r.status_code != 200:
        print("  users/me:", brief_error(r))
        out["asana"] = {"error": brief_error(r)}
        return
    me = r.json()["data"]
    print("  ログイン:", me.get("email"))
    res["workspaces"] = [{"gid": w["gid"], "name": w["name"]} for w in me.get("workspaces", [])]
    for w in res["workspaces"]:
        r = c.get(f"/workspaces/{w['gid']}", params={"opt_fields": "name,is_organization,email_domains"})
        ws = r.json().get("data", {}) if r.status_code == 200 else {}
        w["is_organization"] = ws.get("is_organization")
        print(f"  workspace {w['gid']} {w['name']} organization={w['is_organization']} domains={ws.get('email_domains')}")
        if w["is_organization"]:
            r = c.get(f"/organizations/{w['gid']}/teams", params={"opt_fields": "name"})
            teams = [t["name"] for t in r.json().get("data", [])] if r.status_code == 200 else brief_error(r)
            w["teams"] = teams
            print("   teams:", teams)
        r = c.get(f"/workspaces/{w['gid']}/custom_fields", params={"limit": 5, "opt_fields": "name,type"})
        w["custom_fields_status"] = r.status_code
        print("   custom_fields の読み取り: HTTP", r.status_code, "" if r.status_code == 200 else r.text[:200])
        r = c.get("/users", params={"workspace": w["gid"], "opt_fields": "email", "limit": 100})
        emails = [u.get("email") for u in r.json().get("data", [])] if r.status_code == 200 else []
        w["okamoto_found"] = "souma.okamoto@0x0.co.jp" in emails
        print("   souma.okamoto@0x0.co.jp がメンバーにいる:", w["okamoto_found"])
    out["asana"] = res


# ---------------------------------------------------------------- Slack
def check_slack(out: dict, post: bool) -> None:
    print("\n== Slack")
    c = httpx.Client(base_url="https://slack.com/api",
                     headers={"Authorization": f"Bearer {secret('SLACK_BOT_TOKEN')}"}, timeout=30)
    res: dict = {}
    r = c.post("/auth.test").json()
    if not r.get("ok"):
        print("  auth.test:", r.get("error"))
        out["slack"] = {"error": r.get("error")}
        return
    res["auth"] = {k: r.get(k) for k in ("team", "team_id", "user", "user_id", "bot_id")}
    print("  auth.test:", res["auth"])
    chans = {}
    cursor = None
    for _ in range(10):
        q = {"types": "public_channel", "limit": 1000, "exclude_archived": "true"}
        if cursor:
            q["cursor"] = cursor
        r = c.get("/conversations.list", params=q).json()
        if not r.get("ok"):
            print("  conversations.list:", r.get("error"))
            break
        for ch in r.get("channels", []):
            if ch["name"] in (TEST_CHANNEL, PROD_CHANNEL):
                chans[ch["name"]] = {"id": ch["id"], "is_member": ch.get("is_member"), "is_private": ch.get("is_private")}
        cursor = (r.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            break
    res["channels"] = chans
    print("  チャンネル:", chans or "見つからない（非公開チャンネルの場合は一覧に出ない）")
    test = chans.get(TEST_CHANNEL)
    if post and test:
        now = datetime.now(JST).strftime("%m/%d %H:%M")
        text = f"【疎通確認】Bar null 売上管理 Bot のテスト投稿です（Phase 0、{now}）\nステータス：投稿テスト"
        r = c.post("/chat.postMessage", json={"channel": test["id"], "text": text}).json()
        print("  postMessage:", r.get("ok"), r.get("error") or "")
        if r.get("ok"):
            ts = r["ts"]
            r2 = c.post("/chat.postMessage", json={"channel": test["id"], "thread_ts": ts,
                                                   "text": "スレッド返信のテストです（伝票の進捗はこのようにスレッドに書きます）"}).json()
            r3 = c.post("/chat.update", json={"channel": test["id"], "ts": ts,
                                              "text": text.replace("ステータス：投稿テスト", "ステータス：更新テスト済み")}).json()
            r4 = c.get("/chat.getPermalink", params={"channel": test["id"], "message_ts": ts}).json()
            res["post_test"] = {"post": True, "thread": r2.get("ok"), "update": r3.get("ok"), "permalink": r4.get("ok")}
            print("  スレッド返信:", r2.get("ok"), r2.get("error") or "", " 更新:", r3.get("ok"), r3.get("error") or "",
                  " permalink:", r4.get("permalink"))
    out["slack"] = res


def main() -> None:
    args = set(sys.argv[1:])
    targets = args - {"--post-slack-test"} or {"square", "mf", "asana", "slack"}
    out: dict = json.loads(OUT.read_text(encoding="utf-8")) if OUT.exists() else {}
    out["checked_at"] = datetime.now(JST).isoformat(timespec="seconds")
    for name, fn in (("square", check_square), ("mf", check_mf), ("asana", check_asana)):
        if name in targets:
            try:
                fn(out)
            except httpx.HTTPError as e:
                print(f"  {name}: 通信エラー {e!r}")
    if "slack" in targets:
        check_slack(out, "--post-slack-test" in args)
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\nsaved:", OUT)


if __name__ == "__main__":
    main()
