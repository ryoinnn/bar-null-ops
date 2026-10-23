from datetime import datetime

from barnull.textutil import (
    JST,
    fmt_date,
    fmt_duration,
    fmt_range,
    mask_contacts,
    normalize_name,
    parse_ib_range,
    parse_slash_datetime,
    parse_sm_range,
    parse_yen,
    salutation,
    surname,
)


def test_sm_range_same_day():
    s, e = parse_sm_range("2026/10/30(金) 20:00〜23:00")
    assert s == datetime(2026, 10, 30, 20, 0, tzinfo=JST)
    assert e == datetime(2026, 10, 30, 23, 0, tzinfo=JST)


def test_sm_range_cross_midnight_with_end_date():
    s, e = parse_sm_range("2026/10/8(木) 21:00〜2026/10/9(金) 0:00")
    assert s == datetime(2026, 10, 8, 21, tzinfo=JST)
    assert e == datetime(2026, 10, 9, 0, tzinfo=JST)
    assert fmt_range(s, e) == "21:00〜翌0:00"
    assert fmt_duration(s, e) == "3時間"


def test_sm_range_midnight_start():
    s, e = parse_sm_range("2026/9/30(水) 0:00〜4:30")
    assert (s.day, s.hour, e.hour, e.minute) == (30, 0, 4, 30)
    assert fmt_duration(s, e) == "4時間30分"


def test_ib_range_and_cross_midnight_inferred():
    s, e = parse_ib_range("2026年11月01日(日) 10時00分〜15時30分")
    assert (s.hour, e.hour, e.minute) == (10, 15, 30)
    s, e = parse_ib_range("2026年05月30日(土) 23時00分〜00時00分")
    assert e == datetime(2026, 5, 31, 0, 0, tzinfo=JST)


def test_slash_datetime():
    assert parse_slash_datetime("2026/10/05 07:28") == datetime(2026, 10, 5, 7, 28, tzinfo=JST)


def test_yen():
    assert parse_yen("￥33,000") == 33000
    assert parse_yen("18,150円 (税込)") == 18150
    assert parse_yen("夜プラン（￥30,000）") == 30000
    assert parse_yen(None) is None


def test_names():
    assert normalize_name("山田　太郎様") == normalize_name("山田 太郎") == "山田太郎"
    assert surname("山田 太郎 様") == "山田"
    assert surname("ライアン 弧音") == "ライアン"
    assert surname("N T") is None
    assert surname("山田太郎") is None
    assert salutation("山田 太郎") == "山田さま\n\n"
    assert salutation(None) == ""


def test_fmt_date():
    assert fmt_date(datetime(2026, 10, 30, tzinfo=JST)) == "2026/10/30（金）"


def test_mask_contacts():
    t = mask_contacts("電話は090-1234-5678、メールは a.b@example.com です")
    assert "090" not in t and "example.com" not in t


def test_slack_to_plain():
    from barnull.textutil import slack_to_plain

    assert slack_to_plain("```お問い合わせありがとうございます！\n可能です。```") == (
        "お問い合わせありがとうございます！\n可能です。", True)
    t, fenced = slack_to_plain("詳しくは <https://example.com/a?b=1&amp;c=2|こちら> へ &lt;要確認&gt; R&amp;D")
    # 表示名付きのリンクは、表示名と URL の両方を残す（URL が消えてゲストに届かないように）
    assert t == "詳しくは こちら https://example.com/a?b=1&c=2 へ <要確認> R&D" and not fenced
    assert slack_to_plain("<@U0A51TDCCEN> 了解です")[0] == "了解です"
