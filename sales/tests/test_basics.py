"""営業日、LINE の判定、イベント1件の処理（起票・重複・再試行・OK スタンプ）。"""

from __future__ import annotations

import base64
import hashlib
import hmac
from datetime import date, datetime

import pytest
from helpers import JST, SIX, SeqExtractor, event, make_deps, parents, slip_msg

from barnull_sales import bizdate
from barnull_sales.extract import Entry, Parsed
from barnull_sales.line import classify_event, is_bot_mentioned, strip_mention, verify_signature
from barnull_sales.process import handle_event
from barnull_sales.testing import FakeLine
from barnull_sales.texts import esc


# ---------------------------------------------------------------- 営業日
@pytest.mark.parametrize("local, expected", [
    ("2026-10-11 05:59", date(2026, 10, 10)),
    ("2026-10-11 06:00", date(2026, 10, 11)),
    ("2026-01-01 02:00", date(2025, 12, 31)),
    ("2026-03-01 05:00", date(2026, 2, 28)),
])
def test_business_date(local, expected):
    dt = datetime.strptime(local, "%Y-%m-%d %H:%M").replace(tzinfo=JST)
    assert bizdate.business_date(dt, JST, SIX) == expected


def test_resolve_month_day_never_future():
    sent = datetime(2026, 1, 3, 1, 0, tzinfo=JST)          # 営業日は 1/2
    assert bizdate.resolve_month_day(12, 31, sent, JST, SIX) == date(2025, 12, 31)
    assert bizdate.resolve_month_day(1, 2, sent, JST, SIX) == date(2026, 1, 2)
    assert bizdate.resolve_month_day(1, 3, sent, JST, SIX) == date(2025, 1, 3)   # 営業日より先は前年
    assert bizdate.resolve_month_day(2, 30, sent, JST, SIX) is None


def test_bank_holiday_helper():
    assert bizdate.add_bank_days(date(2026, 10, 9), 1) == date(2026, 10, 13)     # 金 → 火（10/12 はスポーツの日）


# ---------------------------------------------------------------- LINE
def test_signature():
    body = b'{"events":[]}'
    sig = base64.b64encode(hmac.new(b"secret", body, hashlib.sha256).digest()).decode()
    assert verify_signature("secret", body, sig)
    assert not verify_signature("secret", body + b" ", sig)
    assert not verify_signature("secret", body, None)


def test_mention_detection():
    assert is_bot_mentioned({"text": "@null 日付：10/10", "mention": {"mentionees": [{"isSelf": True}]}}, "@null")
    assert is_bot_mentioned({"text": "@null\n日付：10/10"}, "@null")                       # 手打ち
    assert not is_bot_mentioned({"text": "@nullpo こんにちは"}, "@null")                   # 別の名前
    assert not is_bot_mentioned({"text": "@山田 よろしく", "mention": {"mentionees": [{"isSelf": False}]}}, "@null")
    assert not is_bot_mentioned({"text": "日付：10/10 金額：1000"}, "@null")


def test_classify_event():
    allowed = ("Cgroup",)
    assert classify_event(event("2026-10-10 22:00"), allowed, "@null") == "message"
    assert classify_event(event("2026-10-10 22:00", group="Cother"), allowed, "@null") == "unknown_group_message"
    chat = event("2026-10-10 22:00", text="ただの雑談")
    chat["message"].pop("mention")
    assert classify_event(chat, allowed, "@null") is None
    image = event("2026-10-10 22:00")
    image["message"] = {"type": "image", "id": "i1"}
    assert classify_event(image, allowed, "@null") is None
    one_to_one = event("2026-10-10 22:00")
    one_to_one["source"] = {"type": "user", "userId": "U1"}
    assert classify_event(one_to_one, allowed, "@null") is None
    assert classify_event({"type": "join", "source": {"type": "group", "groupId": "Cnew"}}, allowed, "@null") == "join"


def test_strip_mention():
    msg = {"text": "@null 日付：10/10", "mention": {"mentionees": [{"index": 0, "length": 5, "isSelf": True}]}}
    assert strip_mention(msg, "@null") == "日付：10/10"
    assert strip_mention({"text": "@null\n日付：10/10"}, "@null") == "日付：10/10"


def test_slack_escape():
    assert esc("<!channel> & <https://x|y>") == "&lt;!channel&gt; &amp; &lt;https://x|y&gt;"


# ---------------------------------------------------------------- 起票
def test_two_customers_make_two_slips_and_rerun_is_idempotent():
    deps = make_deps([slip_msg(Entry(month=9, day=28, customer_name="顧客A", amount=24750),
                               Entry(month=9, day=28, customer_name="顧客B", amount=57750, note="4万円は未収",
                                     unpaid_amount=40000))])
    ev = event("2026-09-29 00:20")
    r1 = handle_event(ev, "message", deps)
    assert r1["slip_ids"] == ["S-20260928-001", "S-20260928-002"]
    assert len(parents(deps)) == 2 and len(deps.tracker.tasks) == 2
    assert parents(deps)[1]["text"].startswith("【未収あり ¥40,000】")
    slip = deps.store.get_slip("S-20260928-002")
    assert slip.expected_square_amount == 17750 and slip.unpaid_status == "未収入金有"
    assert "［未収 ¥40,000］" in deps.tracker.tasks[slip.asana["task_gid"]]["name"]

    r2 = handle_event(ev, "message", deps)            # 同じイベントがもう一度届いても、何も増えない
    assert r2["status"] == "duplicate"
    assert len(parents(deps)) == 2 and len(deps.tracker.tasks) == 2
    assert deps.extractor.calls == 1


def test_retry_after_partial_failure_does_not_duplicate_or_reparse():
    deps = make_deps([slip_msg(Entry(month=10, day=5, customer_name="顧客A", amount=100000))])

    deps.tracker.fail = {"create"}                         # Asana が落ちている
    ev = event("2026-10-06 01:00", event_id="E2")
    with pytest.raises(RuntimeError):
        handle_event(ev, "message", deps)
    deps.tracker.fail = set()
    r = handle_event(ev, "message", deps, attempt=2)       # Cloud Tasks の再試行
    assert len(r["slip_ids"]) == 1
    assert len(parents(deps)) == 1 and len(deps.tracker.tasks) == 1     # Slack は1回だけ
    assert deps.extractor.calls == 1                                    # 保存した解析結果を使う


def test_last_attempt_failure_is_reported_with_text():
    deps = make_deps([slip_msg(Entry(month=10, day=5, customer_name="顧客A", amount=100000))])

    deps.tracker.fail = {"create"}
    ev = event("2026-10-06 01:00", event_id="E3", text="@null 顧客A <!channel>")
    r = handle_event(ev, "message", deps, attempt=10)
    assert r["status"] == "failed"
    text = deps.notifier.posts[-1]["text"]
    assert "【処理できなかったメッセージ】" in text and "&lt;!channel&gt;" in text and "<@UALERT>" in text
    assert handle_event(ev, "message", deps)["status"] == "duplicate"


def test_unparsed_and_unknown_target_go_to_review():
    deps = make_deps([None])
    handle_event(event("2026-10-10 22:00", event_id="E3"), "message", deps)
    assert "【要確認】解析できないメッセージ" in deps.notifier.posts[-1]["text"]
    deps = make_deps([Parsed(kind="correction", confidence=0.9, correction={
        "target_customer_name": "顧客A", "new_amount": 95000, "new_customer_name": None, "description": "訂正"})])
    handle_event(event("2026-10-10 22:00", event_id="E4"), "message", deps)
    assert "訂正（対象の伝票を決められない）" in deps.notifier.posts[-1]["text"]   # 送った人の伝票が無い


def test_missing_amount_is_flagged():
    deps = make_deps([slip_msg(Entry(month=10, day=5, customer_name="顧客A", amount=None))])
    r = handle_event(event("2026-10-06 01:00", event_id="E5"), "message", deps)
    slip = deps.store.get_slip(r["slip_ids"][0])
    assert slip.status == "要確認"
    assert any("金額が読み取れない" in p["text"] for p in deps.notifier.posts if p["thread_ts"])


def test_negative_amount_is_treated_as_unreadable():
    p = Parsed.from_json({"kind": "slip_content", "confidence": 0.9, "correction": None, "collection_report": None,
                          "cash_deposit": None, "entries": [{"month": 10, "day": 5, "customer_name": "顧客A",
                                                             "amount": -5000, "note": "", "unpaid_amount": None,
                                                             "unpaid_due_text": "", "payment_hint": ""}]})
    assert p.entries[0].amount is None
    assert Parsed.from_json(p.to_json()).entries[0].customer_name == "顧客A"


def test_customer_text_is_escaped_in_slack():
    deps = make_deps([slip_msg(Entry(month=10, day=5, customer_name="<!channel>", amount=1000, note="<@U1>"))])
    handle_event(event("2026-10-06 01:00"), "message", deps)
    assert "<!channel>" not in parents(deps)[0]["text"] and "&lt;!channel&gt;" in parents(deps)[0]["text"]


def test_join_event_posts_group_id_once():
    deps = make_deps()
    join = {"type": "join", "webhookEventId": "J1", "timestamp": 1760000000000, "source": {"type": "group", "groupId": "Cnew"}}
    handle_event(join, "join", deps)
    handle_event({**join, "webhookEventId": "J2"}, "join", deps)
    assert len([p for p in deps.notifier.posts if "Cnew" in p["text"]]) == 1


def test_worker_ignores_groups_not_allowed():
    deps = make_deps([slip_msg(Entry(month=10, day=5, customer_name="顧客A", amount=1000))])
    r = handle_event(event("2026-10-06 01:00", group="Cother"), "message", deps)   # Webhook の判定をすり抜けても
    assert r["status"] == "ignored_group" and deps.extractor.calls == 0


def test_ok_sticker_is_replied_only_when_accepted():
    deps = make_deps([slip_msg(Entry(month=10, day=5, customer_name="顧客A", amount=1))], reply_ack=True)
    deps.line = FakeLine()
    ev = event("2026-10-06 01:00", event_id="A1")
    ev["replyToken"] = "rt1"
    ev["message"]["quoteToken"] = "qt1"
    assert handle_event(ev, "message", deps)["acked"]
    assert deps.line.replies == [("rt1", "qt1")]
    deps.extractor = SeqExtractor([None])                  # 解析できないメッセージにはスタンプを返さない
    ev2 = event("2026-10-06 01:00", event_id="A2")
    ev2["replyToken"] = "rt2"
    handle_event(ev2, "message", deps)
    assert deps.line.replies == [("rt1", "qt1")]
