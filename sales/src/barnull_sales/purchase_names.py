"""商品名の標準化（新しい商品の表示名の案を Claude Haiku で作る）。渡すのは商品名と NOS カテゴリだけ（価格・数量は渡さない）。

使うモデルは sales.toml の [purchase] model（Haiku の最新版。2026-11-15 本人指定）。呼び方は次の順:
1. Claude API（資格情報マネージャーの ANTHROPIC_API_KEY）
2. この PC の Claude Code（`claude -p`。本人の Claude アカウントのログインを使う）
3. どちらも使えなければ、規則で作る（Slack にその旨を書く）
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile

from .purchase import rule_name

log = logging.getLogger(__name__)

SYSTEM = """あなたはバーの仕入管理の補助です。酒屋の発注サイトの商品名を、店の人が一目で分かる短い表示名に直します。
規則:
- メーカー名・容量・度数・入数・容器（ペット・瓶・缶・パック）・「※」「☆」などの記号は落とす
- 全角の英数字は半角にする。銘柄・種類が分かる最短の名前にする（年数・ラベル名・フレーバーなど区別に要るものは残す）
- 例: 「アイリスオーヤマ とうもろこしのひげ茶 １．５Ｌ」→「ひげ茶」
- 例: 「クエルボ１８００アネホ ４０°７５０ｍｌ」→「クエルボ1800アネホ」
- 例: 「ジョニー ウォーカー 黒ラベル １２年 ４０°７００ｍｌ」→「ジョニーウォーカー黒ラベル12年」
- 例: 「甲２０°鏡月グリーン ７００ｍｌ」→「鏡月グリーン20度」（度数違いの同じ銘柄が一覧にあるときだけ度数を残す）
入力の id ごとに表示名を返す。"""

SCHEMA = {
    "type": "object",
    "properties": {"items": {"type": "array", "items": {
        "type": "object", "properties": {"id": {"type": "string"}, "name": {"type": "string"}},
        "required": ["id", "name"], "additionalProperties": False}}},
    "required": ["items"], "additionalProperties": False,
}


def _via_api(payload: list[dict], api_key: str, model: str) -> dict[str, str]:
    import anthropic

    client = anthropic.Anthropic(api_key=api_key, max_retries=3, timeout=120.0)
    resp = client.messages.create(
        model=model, max_tokens=4000, system=SYSTEM,
        messages=[{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}})
    raw = next((b.text for b in resp.content if b.type == "text"), "")
    return {it["id"]: it["name"].strip() for it in json.loads(raw).get("items", [])}


def _via_claude_code(payload: list[dict], model: str) -> dict[str, str]:
    exe = shutil.which("claude") or os.path.join(os.environ.get("USERPROFILE", ""), ".local", "bin", "claude.exe")
    if not os.path.exists(exe):
        raise RuntimeError("claude コマンドが見つかりません")
    env = {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")}
    prompt = SYSTEM + "\n\n入力:\n" + json.dumps(payload, ensure_ascii=False)
    with tempfile.TemporaryDirectory() as cwd:   # プロジェクトの設定（CLAUDE.md 等）を読ませない
        r = subprocess.run([exe, "-p", prompt, "--model", model, "--output-format", "json", "--tools", "",
                            "--no-session-persistence", "--json-schema", json.dumps(SCHEMA)],
                           cwd=cwd, env=env, capture_output=True, text=True, encoding="utf-8", timeout=300)
    out = json.loads(r.stdout or "{}")
    if out.get("is_error"):
        raise RuntimeError(f"Claude Code: {str(out.get('result'))[:200]}")
    data = out.get("structured_output") or json.loads(out.get("result") or "{}")
    return {it["id"]: str(it["name"]).strip() for it in data.get("items", [])}


def suggest_names(items: dict[str, str], *, api_key: str | None, model: str,
                  categories: dict[str, str] | None = None) -> tuple[dict[str, str], bool]:
    """{キー: 元の商品名} → ({キー: 表示名の案}, Claude を使えたか)。使えなければ規則で作る。"""
    if not items:
        return {}, True
    fallback = {k: rule_name(v) for k, v in items.items()}
    ids = list(items)
    payload = [{"id": str(i), "name": items[k], **({"category": categories[k]} if categories and k in categories else {})}
               for i, k in enumerate(ids)]
    got: dict[str, str] | None = None
    if api_key:
        try:
            got = _via_api(payload, api_key, model)
        except Exception as e:  # noqa: BLE001  名前の案は無くても取り込みは続ける
            log.warning("表示名の案を Claude API で作れませんでした: %s", type(e).__name__)
    if got is None:
        try:
            got = _via_claude_code(payload, model)
        except Exception as e:  # noqa: BLE001
            log.warning("表示名の案を Claude Code で作れませんでした: %s", str(e)[:200])
    if got is None:
        return fallback, False
    return {k: (got.get(str(i)) or fallback[k])[:60] for i, k in enumerate(ids)}, True
