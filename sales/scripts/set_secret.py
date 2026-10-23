"""売上管理の秘密情報を、この PC の Windows 資格情報マネージャー（サービス名 barnull-sales）に登録する。

入力は伏せ字のウィンドウで受け取り、画面やログには出さない。
登録・変更したら `uv run python sales/infra/setup.py secrets` で Secret Manager に反映する。

    uv run python sales/scripts/set_secret.py            # 未登録のものを順に聞く
    uv run python sales/scripts/set_secret.py NAME ...   # 指定したものを登録し直す
    uv run python sales/scripts/set_secret.py --status   # 登録状況だけ表示
"""

from __future__ import annotations

import sys

import keyring
from barnull.secret_input import ask, clean

SERVICE = "barnull-sales"

NAMES: dict[str, tuple[str, str]] = {  # 名前: (先頭の文字列, 説明)
    "SQUARE_APP_ID": ("sq0idp-", "Square Developer Console → アプリ → OAuth（Production）の Application ID"),
    "SQUARE_APP_SECRET": ("sq0csp-", "Square Developer Console → アプリ → OAuth（Production）の Application secret"),
    "MF_API_KEY": ("mf_api_", "マネーフォワード クラウド アプリポータルで発行した APIキー"),
    "ASANA_PAT": ("", "Asana（0x0.co.jp のアカウント）の開発者コンソールで発行した Personal Access Token"),
    "SLACK_BOT_TOKEN": ("xoxb-", "Slack App「Bar null 売上管理」の Bot User OAuth Token"),
    "ANTHROPIC_API_KEY": ("sk-ant-", "売上管理用の Claude API キー（予約対応と分けると費用を測りやすい）"),
    "LINE_CHANNEL_SECRET": ("", "LINE Developers → Messaging API チャネル → チャネル基本設定 → チャネルシークレット"),
    "LINE_CHANNEL_ACCESS_TOKEN": ("", "LINE Developers → Messaging API 設定 → チャネルアクセストークン（長期）"),
}


def is_set(name: str) -> bool:
    return bool(clean(keyring.get_password(SERVICE, name) or ""))


def register(name: str) -> None:
    prefix, help_text = NAMES[name]
    while True:
        value = ask(name, help_text)
        if not value:
            print(f"  {name}: 登録しませんでした")
            return
        if prefix and not value.startswith(prefix):
            print(f"  {name}: {prefix} で始まっていません。貼り付けた値を確認してください")
            continue
        keyring.set_password(SERVICE, name, value)
        print(f"  {name}: 登録しました")
        return


def main() -> None:
    args = [a.upper() for a in sys.argv[1:]]
    if args == ["--STATUS"]:
        for n in NAMES:
            print(f"  {'✓' if is_set(n) else '✗'} {n}")
        return
    unknown = [a for a in args if a not in NAMES]
    if unknown:
        sys.exit(f"不明な名前です: {', '.join(unknown)}（{', '.join(NAMES)}）")
    targets = args or [n for n in NAMES if not is_set(n)]
    if not targets:
        print("すべて登録済みです（登録し直すときは名前を指定）")
    for name in targets:
        print(f"{name} の入力ウィンドウを開きます（Ctrl+V で貼り付け。空のまま閉じると飛ばします）…")
        register(name)


if __name__ == "__main__":
    main()
