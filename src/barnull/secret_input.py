"""秘密情報の入力（伏せ字の入力ウィンドウ。Ctrl+V で貼り付けられる）。

Windows のコンソールの getpass は貼り付け（Ctrl+V）を文字として受け取ってしまうため、Tk のダイアログを使う。
"""

from __future__ import annotations

import re

PREFIX = {
    "SLACK_BOT_TOKEN": "xoxb-",
    "SLACK_APP_TOKEN": "xapp-",
    "ANTHROPIC_API_KEY": "sk-ant-",
}


def clean(value: str) -> str:
    value = re.sub(r"\x1b\[20[01]~", "", value)  # 端末の貼り付け開始・終了の印
    value = "".join(c for c in value if ord(c) >= 32 and ord(c) != 127)
    return value.strip()


def validate(name: str, value: str) -> str | None:
    """問題があればメッセージを返す"""
    if not value:
        return "空です"
    pre = PREFIX.get(name)
    if pre and not value.startswith(pre):
        return f"{pre} で始まっていません（貼り付ける値を確認してください）"
    if name == "KEYBOX_CODE" and not value.isdigit():
        return "数字ではありません"
    if name.endswith("_EMAIL") and "@" not in value:
        return "メールアドレスの形式ではありません"
    return None


def ask(name: str, help_text: str) -> str | None:
    """入力ウィンドウを出して値を返す。キャンセル・空なら None"""
    try:
        import tkinter as tk
    except Exception:  # noqa: BLE001
        import getpass
        return clean(getpass.getpass(f"{name}（表示されません）: ")) or None

    result: dict[str, str | None] = {"value": None}
    root = tk.Tk()
    root.title(f"barnull: {name}")
    root.attributes("-topmost", True)
    root.resizable(False, False)
    frm = tk.Frame(root, padx=16, pady=12)
    frm.pack()
    tk.Label(frm, text=name, font=("Yu Gothic UI", 11, "bold")).pack(anchor="w")
    tk.Label(frm, text=help_text, wraplength=460, justify="left").pack(anchor="w", pady=(2, 8))
    var = tk.StringVar()
    entry = tk.Entry(frm, textvariable=var, show="●", width=56)
    entry.pack(fill="x")
    msg = tk.Label(frm, text="Ctrl+V で貼り付けて「登録」。入力は伏せ字で表示されます。", fg="#555")
    msg.pack(anchor="w", pady=(6, 8))
    btns = tk.Frame(frm)
    btns.pack(anchor="e")

    def submit(_=None):
        v = clean(var.get())
        err = validate(name, v)
        if err:
            msg.config(text=f"⚠ {err}", fg="#c00")
            return
        result["value"] = v
        root.destroy()

    def skip():
        root.destroy()

    tk.Button(btns, text="飛ばす", width=10, command=skip).pack(side="right", padx=(6, 0))
    tk.Button(btns, text="登録", width=10, command=submit).pack(side="right")
    root.bind("<Return>", submit)
    root.bind("<Escape>", lambda _: skip())
    entry.focus_force()
    root.after(200, lambda: (root.lift(), entry.focus_force()))
    root.mainloop()
    return result["value"]
