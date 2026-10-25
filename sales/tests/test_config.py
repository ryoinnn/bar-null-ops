"""店舗ごとの設定（2026-11 福岡 Node の追加）。Bar null の設定の読み方は変えない。"""

from __future__ import annotations

import os
from datetime import time
from pathlib import Path

from barnull_sales.config import load_settings

ROOT = Path(__file__).resolve().parents[1]
os.environ.pop("DRY_RUN", None)


def test_bar_null_settings_are_unchanged():
    s = load_settings(ROOT / "sales.toml")
    assert s.store_name == "Bar null" and s.line_enabled and s.keyring_service == "barnull-sales"
    assert s.day_start == time(6, 0) and s.bot_mention_name == "@null"
    assert s.square.location_id == "L7SVX8VVWDQBX" and s.project == "barnull-sales"


def test_store_without_line(tmp_path):
    text = (ROOT / "tests" / "sales.test.toml").read_text(encoding="utf-8")
    start, end = text.index("[line]"), text.index("[slack]")
    store = text[:start] + text[end:]
    store = store.replace('day_start = "06:00"', 'day_start = "09:00"')
    store += '\n[store]\nname = "Node"\nkeyring_service = "node-sales"\n'
    path = tmp_path / "sales.toml"
    path.write_text(store, encoding="utf-8")
    s = load_settings(path)
    assert s.store_name == "Node" and not s.line_enabled and s.keyring_service == "node-sales"
    assert s.bot_mention_name == "" and s.allowed_group_ids == () and s.day_start == time(9, 0)
    assert s.asana_ids == {}   # asana_ids.json は設定ファイルと同じフォルダから読む（無ければ空）
