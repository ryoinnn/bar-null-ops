"""秘密情報（Windows 資格情報マネージャー、keyring 経由）。

値をログや画面に出さないこと。ログには `redact()` を通す。
"""

from __future__ import annotations

import re

import keyring

SERVICE = "bar-null-ops"

NAMES = (
    "SLACK_BOT_TOKEN",
    "SLACK_APP_TOKEN",
    "ANTHROPIC_API_KEY",
    "KEYBOX_CODE",
    "SM_EMAIL",
    "SM_PASSWORD",
    "IB_EMAIL",
    "IB_PASSWORD",
)


class MissingSecret(RuntimeError):
    pass


def _read(name: str) -> str | None:
    value = keyring.get_password(SERVICE, name)
    if value is None:
        return None
    from .secret_input import clean

    return clean(value) or None


def get(name: str) -> str:
    value = _read(name)
    if not value:
        raise MissingSecret(f"{name} が未登録です。`uv run barnull set-secret {name}` で登録してください")
    return value


def get_optional(name: str) -> str | None:
    return _read(name)


def set_(name: str, value: str) -> None:
    keyring.set_password(SERVICE, name, value)


def status() -> dict[str, bool]:
    return {n: bool(_read(n)) for n in NAMES}


_redact_values: list[str] = []
_FW = str.maketrans("0123456789", "０１２３４５６７８９")


def load_redactions() -> None:
    """ログ・Slack から伏せる値（暗証番号・パスワード・トークン）を読み込む。
    URL に載った形（%エンコード）や、全角数字で書かれた暗証番号も伏せる"""
    from urllib.parse import quote, quote_plus

    values: list[str] = []
    for n in NAMES:
        if n.endswith("EMAIL"):
            continue
        v = get_optional(n)
        if v and len(v) >= 3:
            values.append(v)
            for enc in (quote(v, safe=""), quote_plus(v)):
                if enc != v:
                    values.append(enc)
    _redact_values[:] = sorted(set(values), key=len, reverse=True)


def redact(text: str) -> str:
    for v in _redact_values:
        if v.isdigit():
            # 暗証番号などの数字は、予約IDなど長い数字の一部では伏せない。全角数字で書かれていても伏せる
            alt = "".join(f"[{c}{c.translate(_FW)}]" for c in v)
            text = re.sub(rf"(?<![0-9０-９]){alt}(?![0-9０-９])", "****", text)
        else:
            text = text.replace(v, "****")
    return text


def redact_obj(obj):
    """Slack の blocks・attachments のような入れ子の値の文字列をすべて伏せる"""
    if isinstance(obj, str):
        return redact(obj)
    if isinstance(obj, list):
        return [redact_obj(x) for x in obj]
    if isinstance(obj, dict):
        return {k: redact_obj(v) for k, v in obj.items()}
    return obj
