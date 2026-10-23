"""Phase 0: 伝票サンプルでの Claude 抽出精度と費用の検証（使い捨てのスクリプト）。

実行（リポジトリのルートで）:
    uv run --with pillow python sales/scripts/phase0_extract_test.py

- 入力: sales/samples/cases.json と画像（git に入れない）
- API キー: Windows 資格情報マネージャー（サービス bar-null-ops の ANTHROPIC_API_KEY）
- 出力: sales/samples/phase0_results.json（顧客名を含むので git に入れない）と、画面への要約
"""

from __future__ import annotations

import base64
import io
import json
import re
import sys
import time
from pathlib import Path

import anthropic
import keyring
from PIL import Image, ImageOps

ROOT = Path(__file__).resolve().parents[2]
SAMPLES = ROOT / "sales" / "samples"
USD_JPY = 150
PRICES = {  # USD / 1M tokens（input, output）。2026-10 時点
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-sonnet-5-5": (2.00, 10.00),
}
FALLBACK_BETA = "server-side-fallback-2026-07-01"


def nullable(t: dict) -> dict:
    return {"anyOf": [t, {"type": "null"}]}


IMAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "is_slip_photo": {"type": "boolean"},
        "photo_confidence": {"type": "number"},
        "slips": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "date_month": nullable({"type": "integer"}),
                    "date_day": nullable({"type": "integer"}),
                    "customer_name": nullable({"type": "string"}),
                    "total_amount": nullable({"type": "integer"}),
                    "total_corrected": {"type": "boolean"},
                    "total_reading": {"type": "string"},
                    "confidence": {"type": "number"},
                },
                "required": ["index", "date_month", "date_day", "customer_name", "total_amount",
                             "total_corrected", "total_reading", "confidence"],
                "additionalProperties": False,
            },
        },
        "notes": {"type": "string"},
    },
    "required": ["is_slip_photo", "photo_confidence", "slips", "notes"],
    "additionalProperties": False,
}

IMAGE_SYSTEM = """あなたはバー「Bar null」の会計伝票の写真から値を読み取る担当です。
写真には、手書きの伝票（上部に「ACCOUNT」と印刷された縦長の用紙。Date・Item・Q'ty・Amount・Total の欄がある）が0枚以上写っています。
暗い照明や赤い照明、回転、斜めからの撮影、複数枚を並べた撮影があります。

読み取りのルール:
- 写っている伝票を1枚ずつ数え、読み順（伝票の上辺を上にしたときの左から右、上から下）に index を1から振る。
- 各伝票について、Date 欄の月と日（年が無ければ月日だけ）、顧客名（Date 欄の右などに書かれた「〇〇様」「〇〇さま」の〇〇。無ければ null）、Total 欄の金額を読む。
- 取り消し線で消された数字は採用しない。書き直された数字があればそれを採用し、total_corrected を true にする。
- 読めない値、桁が判別できない値は推測で埋めず null にする。
- 金額は円の整数（カンマ、¥、末尾の「-」「=」は除く）。
- total_reading には Total をどう読んだかを短く書く（例:「106,000 に取り消し線、上に 100,000」）。
- confidence は各伝票の Total を正しく読めた自信（0〜1）。
- 伝票が写っていない写真は is_slip_photo を false、slips を空にする。"""

TEXT_SCHEMA = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": ["slip_content", "correction", "unpaid_collection_report",
                                            "cash_deposit_report", "other"]},
        "confidence": {"type": "number"},
        "entries": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "month": nullable({"type": "integer"}),
                    "day": nullable({"type": "integer"}),
                    "customer_name": nullable({"type": "string"}),
                    "amount": nullable({"type": "integer"}),
                    "note": {"type": "string"},
                    "unpaid_amount": nullable({"type": "integer"}),
                    "unpaid_due_text": {"type": "string"},
                    "is_total_of_multiple_slips": {"type": "boolean"},
                    "payment_hint": {"type": "string"},
                },
                "required": ["month", "day", "customer_name", "amount", "note", "unpaid_amount",
                             "unpaid_due_text", "is_total_of_multiple_slips", "payment_hint"],
                "additionalProperties": False,
            },
        },
        "correction": nullable({
            "type": "object",
            "properties": {
                "target_customer_name": nullable({"type": "string"}),
                "new_amount": nullable({"type": "integer"}),
                "description": {"type": "string"},
            },
            "required": ["target_customer_name", "new_amount", "description"],
            "additionalProperties": False,
        }),
        "collection_report": nullable({
            "type": "object",
            "properties": {
                "customer_name": nullable({"type": "string"}),
                "amount": nullable({"type": "integer"}),
                "method": {"type": "string", "enum": ["bank_transfer", "cash", "square", "unknown"]},
            },
            "required": ["customer_name", "amount", "method"],
            "additionalProperties": False,
        }),
        "cash_deposit": nullable({
            "type": "object",
            "properties": {
                "customer_names": {"type": "array", "items": {"type": "string"}},
                "total_amount": nullable({"type": "integer"}),
                "deposit_date_text": nullable({"type": "string"}),
            },
            "required": ["customer_names", "total_amount", "deposit_date_text"],
            "additionalProperties": False,
        }),
    },
    "required": ["kind", "confidence", "entries", "correction", "collection_report", "cash_deposit"],
    "additionalProperties": False,
}

TEXT_SYSTEM = """あなたはバー「Bar null」のスタッフ用 LINE グループに投稿されたメッセージを分類し、値を取り出す担当です。

kind の決め方:
- slip_content: 会計伝票の内容（日付・顧客名・金額・備考）。1つのメッセージに複数の顧客分が並ぶことがある（顧客ごとに entries を1件）
- correction: 直前に送った伝票の内容の訂正（金額・顧客名・日付など）
- unpaid_collection_report: 未収金（後払い）を回収した、振込があった、という報告
- cash_deposit_report: 現金の売上を銀行に入金した報告（複数日分をまとめて入金した、など）
- other: 上のどれでもない（連絡、雑談など）

値のルール:
- 推測で埋めない。書かれていない値は null。
- 金額は円の整数。「4万」は 40000。
- 顧客名は敬称（様・さま・さん）を除いた名前。
- note は備考の原文（無い・「なし」なら空文字）。
- 備考に未収（後払い）の金額があれば unpaid_amount、回収予定日の記述があれば unpaid_due_text（無ければ空文字）。
- 備考などで、その金額が複数の伝票の合計だと書かれていれば is_total_of_multiple_slips を true。
- 支払方法の手がかり（カード、現金、割り勘、振込など）があれば payment_hint に短く書く（無ければ空文字）。
- kind が slip_content 以外なら entries は空。対応する correction / collection_report / cash_deposit だけを埋め、ほかは null。"""


def image_block(path: Path, long_edge: int) -> tuple[dict, tuple[int, int], int]:
    img = ImageOps.exif_transpose(Image.open(path)).convert("RGB")
    img.thumbnail((long_edge, long_edge))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    data = buf.getvalue()
    block = {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                          "data": base64.standard_b64encode(data).decode()}}
    return block, img.size, len(data)


def call(client: anthropic.Anthropic, model: str, effort: str, system: str, content: list, schema: dict):
    kwargs = dict(model=model, max_tokens=8000, system=system, messages=[{"role": "user", "content": content}],
                  output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}})
    t0 = time.time()
    if model == "claude-sonnet-5-5":
        resp = client.beta.messages.create(betas=[FALLBACK_BETA], fallbacks="default", **kwargs)
    else:
        resp = client.messages.create(**kwargs)
    elapsed = time.time() - t0
    if resp.stop_reason == "refusal":
        return None, resp.usage, elapsed, "refusal"
    text = next((b.text for b in resp.content if b.type == "text"), "")
    try:
        return json.loads(text), resp.usage, elapsed, resp.stop_reason
    except json.JSONDecodeError:
        return None, resp.usage, elapsed, f"invalid_json({resp.stop_reason})"


def cost_jpy(model: str, usage) -> float:
    pin, pout = PRICES[model]
    return (usage.input_tokens * pin + usage.output_tokens * pout) / 1e6 * USD_JPY


def norm_name(s: str | None) -> str:
    return re.sub(r"(様|さま|さん|ｻﾏ|サマ)$", "", (s or "").strip())


def reconcile(slips: list[dict], entries: list[dict]) -> list[str]:
    """画像の伝票とメッセージの内容を突き合わせ、出すべきアラートを返す（試作）。"""
    alerts: list[str] = []
    if any(s["total_amount"] is None for s in slips):
        alerts.append("伝票の金額を読み取れない")
    totals = [s["total_amount"] or 0 for s in slips]
    if len(entries) == 1:
        e = entries[0]
        if len(slips) > 1 and not e["is_total_of_multiple_slips"]:
            alerts.append(f"伝票{len(slips)}枚に対し内容は1件（合計とも書かれていない）")
        if e["amount"] is not None and sum(totals) != e["amount"]:
            alerts.append(f"金額不一致: 伝票 {sum(totals):,} / メッセージ {e['amount']:,}")
        return alerts
    if len(entries) != len(slips):
        alerts.append(f"伝票{len(slips)}枚と内容{len(entries)}件の数が合わない")
        return alerts
    remaining = list(slips)
    for e in entries:
        name = norm_name(e["customer_name"])
        hit = next((s for s in remaining if name and norm_name(s["customer_name"]) == name), None)
        hit = hit or next((s for s in remaining if s["total_amount"] == e["amount"]), None)
        if hit is None:
            alerts.append(f"{name or '?'}: 対応する伝票が見つからない")
            continue
        remaining.remove(hit)
        if hit["total_amount"] != e["amount"]:
            alerts.append(f"{name}: 金額不一致 伝票 {hit['total_amount']} / メッセージ {e['amount']}")
    return alerts


LAYOUT_SCHEMA = {
    "type": "object",
    "properties": {
        "rotate_clockwise": {"type": "string", "enum": ["0", "90", "180", "270"]},
        "slips": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"x0": {"type": "integer"}, "y0": {"type": "integer"},
                               "x1": {"type": "integer"}, "y1": {"type": "integer"}},
                "required": ["x0", "y0", "x1", "y1"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["rotate_clockwise", "slips"],
    "additionalProperties": False,
}

LAYOUT_SYSTEM = """写真に写っている手書きの会計伝票（上部に「ACCOUNT」と印刷された縦長の用紙）の位置を答えてください。
- slips: 伝票1枚ごとの外枠。写真の左上を (0,0)、右下を (1000,1000) とした座標（送られた写真の向きのまま）で x0,y0,x1,y1 を整数で返す。用紙の端まで含める。
- 並び順は、伝票の文字を正立させたときの左から右、上から下。
- rotate_clockwise: 伝票の文字を正立させるには、写真を時計回りに何度回転させればよいか。"""


def load_rgb(path: Path) -> Image.Image:
    return ImageOps.exif_transpose(Image.open(path)).convert("RGB")


def jpeg_block(img: Image.Image) -> tuple[dict, int]:
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    data = buf.getvalue()
    return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                        "data": base64.standard_b64encode(data).decode()}}, len(data)


def crop_slips(img: Image.Image, layout: dict, target_long_edge: int = 1200) -> list[Image.Image]:
    w, h = img.size
    crops = []
    for b in layout["slips"]:
        pad = 15  # 0-1000 座標での余白
        x0, x1 = sorted((b["x0"], b["x1"]))
        y0, y1 = sorted((b["y0"], b["y1"]))
        box = (max(0, (x0 - pad) * w // 1000), max(0, (y0 - pad) * h // 1000),
               min(w, (x1 + pad) * w // 1000), min(h, (y1 + pad) * h // 1000))
        if box[2] - box[0] < 20 or box[3] - box[1] < 20:
            print("  [crop] 枠が不正なので飛ばす:", b)
            continue
        c = img.crop(box)
        deg = int(layout["rotate_clockwise"])
        if deg:
            c = c.rotate(-deg, expand=True)
        scale = target_long_edge / max(c.size)
        if scale > 1:
            c = c.resize((round(c.width * scale), round(c.height * scale)), Image.Resampling.LANCZOS)
        crops.append(c)
    return crops


def run_images(client, cases, results) -> None:
    image_runs = [  # (model, effort, long_edge)
        ("claude-haiku-5-5", "medium", 1568),
        ("claude-haiku-5-5", "medium", 1568),
        ("claude-haiku-5-5", "low", 1568),
        ("claude-haiku-5-5", "medium", 1024),
        ("claude-sonnet-5-5", "medium", 1568),
    ]
    for case in cases["real"]:
        path = SAMPLES / case["image"]
        for model, effort, edge in image_runs:
            block, size, nbytes = image_block(path, edge)
            data, usage, sec, stop = call(client, model, effort, IMAGE_SYSTEM,
                                          [block, {"type": "text", "text": "この写真の伝票を読み取ってください。"}],
                                          IMAGE_SCHEMA)
            row = {"case": case["id"], "method": "whole", "model": model, "effort": effort, "edge": edge,
                   "size": size, "jpeg_kb": round(nbytes / 1024), "in": usage.input_tokens,
                   "out": usage.output_tokens, "jpy": round(cost_jpy(model, usage), 3), "sec": round(sec, 1),
                   "stop": stop, "data": data}
            results["image"].append(row)
            n = len(data["slips"]) if data else None
            totals = [s["total_amount"] for s in data["slips"]] if data else None
            print(f"[img] {case['id']} {model.split('-')[1]:6} {effort:6} {edge:4}px  slips={n} totals={totals} "
                  f"in={usage.input_tokens} out={usage.output_tokens} ¥{row['jpy']} {row['sec']}s {stop}")


def run_crops(client, cases, results) -> None:
    """2段階: 伝票の位置と向きを取り、1枚ずつ切り出して読む。"""
    for case in cases["real"]:
        img = load_rgb(SAMPLES / case["image"])
        for layout_model in ("claude-haiku-5-5", "claude-sonnet-5-5"):
            block, _ = jpeg_block(img)
            layout, usage, sec, stop = call(client, layout_model, "medium", LAYOUT_SYSTEM,
                                            [block, {"type": "text", "text": "伝票の位置を答えてください。"}],
                                            LAYOUT_SCHEMA)
            layout_jpy = cost_jpy(layout_model, usage)
            if not layout:
                print(f"[crop] {case['id']} layout={layout_model} 失敗 {stop}")
                continue
            print(f"  [layout] {case['id']} {layout_model.split('-')[1]} rot={layout['rotate_clockwise']} "
                  f"boxes={[(b['x0'], b['y0'], b['x1'], b['y1']) for b in layout['slips']]}")
            crops = crop_slips(img, layout)
            for i, c in enumerate(crops, 1):
                c.save(SAMPLES / f"crop_{case['id']}_{layout_model.split('-')[1]}_{i}.jpg", quality=90)
            for read_model in ("claude-haiku-5-5", "claude-sonnet-5-5"):
                slips, jpy, secs = [], layout_jpy, sec
                for i, c in enumerate(crops, 1):
                    b, _ = jpeg_block(c)
                    data, u, sc, st = call(client, read_model, "medium", IMAGE_SYSTEM,
                                           [b, {"type": "text", "text": "この画像は伝票1枚を切り出したものです。読み取ってください。"}],
                                           IMAGE_SCHEMA)
                    jpy += cost_jpy(read_model, u)
                    secs += sc
                    one = (data or {}).get("slips") or []
                    s0 = dict(one[0]) if one else {"date_month": None, "date_day": None, "customer_name": None,
                                                   "total_amount": None, "total_corrected": False,
                                                   "total_reading": f"失敗 {st}", "confidence": 0}
                    s0["index"] = i
                    slips.append(s0)
                row = {"case": case["id"], "method": "crop", "model": f"{layout_model}>{read_model}",
                       "effort": "medium", "edge": 1200, "rotate": layout["rotate_clockwise"],
                       "jpy": round(jpy, 3), "sec": round(secs, 1), "data": {"slips": slips}}
                results["image"].append(row)
                print(f"[crop] {case['id']} layout={layout_model.split('-')[1]:6} read={read_model.split('-')[1]:6} "
                      f"rot={layout['rotate_clockwise']:3} slips={len(slips)} "
                      f"totals={[s['total_amount'] for s in slips]} ¥{row['jpy']} {row['sec']}s")


def run_texts(client, cases, results) -> None:
    text_inputs = [(c["id"], c["sent_at"], c["message"], "slip_content") for c in cases["real"]] + \
                  [(c["id"], c["sent_at"], c["message"], c["expected_kind"]) for c in cases["synthetic"]]
    for cid, sent_at, msg, expected in text_inputs:
        user = f"送信日時: {sent_at}\nメッセージ:\n{msg}"
        data, usage, sec, stop = call(client, "claude-haiku-5-5", "low", TEXT_SYSTEM,
                                      [{"type": "text", "text": user}], TEXT_SCHEMA)
        row = {"id": cid, "expected_kind": expected, "in": usage.input_tokens, "out": usage.output_tokens,
               "jpy": round(cost_jpy("claude-haiku-5-5", usage), 4), "sec": round(sec, 1), "stop": stop, "data": data}
        results["text"].append(row)
        kind = data["kind"] if data else None
        print(f"[txt] {cid:5} expected={expected:24} got={kind!s:24} {'OK' if kind == expected else 'NG'} "
              f"entries={len(data['entries']) if data else None} ¥{row['jpy']} {row['sec']}s")


def main() -> None:
    modes = set(sys.argv[1:]) or {"image", "crop", "text"}
    cases = json.loads((SAMPLES / "cases.json").read_text(encoding="utf-8"))
    key = keyring.get_password("bar-null-ops", "ANTHROPIC_API_KEY")
    if not key:
        sys.exit("ANTHROPIC_API_KEY が資格情報マネージャーにありません")
    client = anthropic.Anthropic(api_key=key.strip(), max_retries=3, timeout=180.0)

    out = SAMPLES / "phase0_results.json"
    results: dict = json.loads(out.read_text(encoding="utf-8")) if out.exists() else {}
    results.setdefault("image", [])
    results.setdefault("text", [])
    if "image" in modes:
        results["image"] = [r for r in results["image"] if r.get("method") != "whole"]
        run_images(client, cases, results)
        out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    if "crop" in modes:
        results["image"] = [r for r in results["image"] if r.get("method") != "crop"]
        run_crops(client, cases, results)
        out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    if "text" in modes:
        results["text"] = []
        run_texts(client, cases, results)

    text_out = {r["id"]: r["data"] for r in results["text"]}
    results["reconcile"] = []
    for case in cases["real"]:
        entries = (text_out.get(case["id"]) or {}).get("entries", [])
        for r in results["image"]:
            if r["case"] == case["id"] and r["data"]:
                alerts = reconcile(r["data"]["slips"], entries)
                results["reconcile"].append({"case": case["id"], "method": r.get("method"), "model": r["model"],
                                             "effort": r["effort"], "edge": r["edge"], "alerts": alerts})
                print(f"[rec] {case['id']} {r.get('method'):5} {r['model']:38} {r['effort']:6} {r['edge']:4} "
                      f"-> {alerts or 'OK'}")
    out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print("saved:", out)


if __name__ == "__main__":
    main()
