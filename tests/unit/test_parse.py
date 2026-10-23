"""管理画面の生データの解釈（調査時に読み取ったテキストをもとにした例。氏名・電話はダミー）"""

from barnull.models import Kind, ListingRow, Message, Platform, message_keys
from barnull.platforms import parse_ib, parse_sm
from barnull.workflows.handlers import Orchestrator

SM_SIDEBAR_CONFIRMED = """受信トレイトップ
現在のステータス
時間貸し
予約完了
下記の連絡先からゲストと連絡できます。

電話：09000000000

予約詳細

日時

2026/10/30(金) 20:00〜23:00

スペース

★9月限定キャンペーン中★ ラグジュアリーな隠れ家バー ※代官山徒歩1分

あなたの収益

￥23,100

振込予定日：2026-11-30

ゲストのお支払い方法

クレジットカード

ゲストのご利用料金明細

夜プラン
￥30,000

消費税
￥3,000

合計
￥33,000

※ゲストの支払う金額には上記にサービス利用料金5%が加算されます。

予約変更リクエスト
予約をキャンセル

予約リクエストの変更は、2026/11/2(月) 23:59までとなっております。

ゲスト詳細

山田 太郎

予約ID：6271768"""

SM_MAIN = """昨日
12:24
予約リクエスト

利用目的

パーティー

[利用目的] パーティー
[利用形態] 個人利用
[人数] 〜15人
[プラン] 夜プラン
今日
0:21

ゲストが退室時の状態を記録しました

状態

気になる点があります

種類

備品・設備に不備があります

詳細

グラスを1つ割ってしまいました。

写真

ゲストから退室時のスペース状態が記録されました。"""


def sm_raw(sidebar=SM_SIDEBAR_CONFIRMED, main=SM_MAIN, buttons=("送信",)):
    return {
        "sidebar": sidebar, "main": main, "buttons": list(buttons),
        "items": [
            {"kind": "guest", "text": "[利用目的] パーティー\n[人数] 〜15人\n12:24", "files": []},
            {"kind": "system", "text": "予約ステータスが「承認」になりました", "files": []},
            {"kind": "host", "text": "既読\n0:23\n山田さま\n\nご予約ありがとうございます！\nなお、当日のキーボックスの暗証番号は【0000】となっております。", "files": []},
            {"kind": "file_host", "text": "Bar_null_スペース利用マニュアル.pdf", "files": ["Bar_null_スペース利用マニュアル.pdf"]},
            {"kind": "guest", "text": "ありがとうございます！\n9:00", "files": []},
        ],
    }


def test_parse_sm_thread():
    d = parse_sm.parse_thread(sm_raw(), "6271768")
    assert d.status == "予約完了"
    assert d.start_at.hour == 20 and d.end_at.hour == 23
    assert d.host_revenue == 23100 and d.guest_total == 33000
    assert d.plan == "夜プラン"
    assert d.guest_name == "山田 太郎"
    assert d.purpose == "パーティー" and d.people == "〜15人"
    assert d.exit_status == "気になる点があります"
    assert "グラス" in (d.exit_detail or "")
    assert d.kind is Kind.RESERVATION
    roles = [m.role for m in d.messages]
    assert roles == ["guest", "system", "host", "host", "guest"]
    assert d.messages[-1].text == "ありがとうございます！"
    assert d.messages[3].files == ["Bar_null_スペース利用マニュアル.pdf"]
    from barnull.platforms.sm import SpaceMarket
    assert SpaceMarket.template_sent(None, d, "キーボックスの暗証番号")


def test_parse_sm_pending_request_and_inquiry():
    side = SM_SIDEBAR_CONFIRMED.replace("予約完了", "保留中")
    d = parse_sm.parse_thread(sm_raw(sidebar=side, buttons=("承認", "否認", "送信")), "1")
    assert d.can_approve
    side = "受信トレイトップ\n現在のステータス\n時間貸し\n期限切れ\n有効期限が過ぎたため、\nお問い合わせは終了しました。\n予約詳細\n\n日時\n\nスペース\n\nX\n\nゲスト詳細\n\n佐藤 花子\n\n予約ID：6167621"
    d = parse_sm.parse_thread({"sidebar": side, "main": "", "items": [], "buttons": []}, "6167621")
    assert d.status == "期限切れ" and d.closed_reason == "期限切れ"
    assert d.kind is Kind.INQUIRY_ONLY or d.start_at is None


def test_parse_sm_review_flags():
    side = SM_SIDEBAR_CONFIRMED.replace("予約完了", "利用完了") + "\nレビュー\n期限：あと14日（2026年10月23日まで）"
    # 画面の文字では判定しない（「ホストレビューあり」はゲストのバッジ、「レビューの投稿」は投稿後も残る）
    d = parse_sm.parse_thread(sm_raw(sidebar=side + "\nホストレビューあり", buttons=("レビューの投稿", "送信")), "1")
    assert d.review_posted is None
    # 予約情報の API: reputation_id_by_owner（ホストのレビュー）と can_be_reviewed（書ける状態か）
    parse_sm.attach_api(d, [], None, {"reputation_id_by_owner": None, "can_be_reviewed": True,
                                      "review_expires_at": "2026-10-23T00:00:00.000+09:00"})
    assert d.review_posted is False and d.review_open is True and d.review_expires_at.day == 23
    parse_sm.attach_api(d, [], None, {"reputation_id_by_owner": 123, "can_be_reviewed": True})
    assert d.review_posted is True
    d2 = parse_sm.parse_thread(sm_raw(sidebar=side), "1")
    parse_sm.attach_api(d2, [], 200)  # レビュー画面の GET …/reputation が 200
    assert d2.review_posted is True


IB_TEXT = """ご予約の詳細 (予約ID:8797784002)
料金プラン
1時間以上プラン / 曜日: 月火水木金土日祝
無料設備
ゴミ処理 ×1個
現地サービス ×1個
利用日時	予約状況	決済状況	操作
合計
18,150円 (税込)
振込額合計(税込)
11,797円 (税込)
"""
IB_ROWS = [
    ["利用日時", "予約状況", "決済状況", "操作"],
    ["2026年11月01日(日) 10時00分〜15時30分", "予約確定", "支払い済み\nクレジットカード", "変更手続き\n予約キャンセル"],
    ["スペース利用料 (売上料率65%)", "18,150円", "6,353円", "11,797円"],
    ["予約ID", "8797784002"], ["氏名", "山田 太郎様\n電話番号認証済み"], ["フリガナ", "ヤマダ タロウ"],
    ["用途", "パーティー・飲み会 - その他のパーティー・飲み会"], ["用途詳細", "同窓会です。"], ["利用人数", "7名"],
]


def test_parse_ib_detail():
    d = parse_ib.parse_detail({"text": IB_TEXT, "rows": IB_ROWS, "guest_id": "992076153"}, "8797784002")
    assert d.status == "予約確定"
    assert (d.start_at.hour, d.end_at.hour, d.end_at.minute) == (10, 15, 30)
    assert d.guest_total == 18150 and d.host_revenue == 11797
    assert d.guest_name == "山田 太郎"
    assert d.people == "7名" and d.purpose_detail == "同窓会です。"
    assert d.options == "ゴミ処理 ×1個、現地サービス ×1個"
    assert d.guest_id == "992076153"


def test_parse_ib_thread_order_and_roles():
    raw = {
        "title": "山田 太郎様 (ID: 9000000001) : メッセージ｜インスタベース",
        "text": "予約（最新5件）\n予約確定\n\n2026年11月01日(日) 10時00分〜15時30分\n\nご注文番号：8797784002",
        "items": [  # 画面は新しい順
            {"host": True, "text": "既読\n\n2026/09/30 12:20\n\n承知いたしました！", "files": []},
            {"host": False, "text": "2026/09/30 12:19\n【自動配信メッセージ】\n予約内容が変更されました", "files": []},
            {"host": False, "text": "2026/09/30 12:04\n予約内容の変更を希望します。\n■ 現在の予約内容", "files": []},
            {"host": True, "text": "既読\n\n2026/09/21 18:14\n\n1789-Bar_null_スペース利用マニュアル.pdf",
             "files": ["1789-Bar_null_スペース利用マニュアル.pdf"]},
        ],
    }
    msgs, gid, orders = parse_ib.parse_thread(raw)
    assert [m.role for m in msgs] == ["host", "guest", "system", "host"]
    assert msgs[0].text == "" and msgs[0].files
    assert parse_ib.is_change_request(msgs[1].text)
    assert msgs[-1].text == "承知いたしました！"
    assert gid == "9000000001" and orders == ["8797784002"]


def test_parse_ib_bookings():
    rows = parse_ib.parse_bookings([{"id": "8797784002", "status": "予約確定 クレジットカード 支払い済み",
                                     "guest": "山田 太郎様", "guest_id": "1", "datetime": "x", "price": "18,150円"}])
    assert rows[0].guest_name == "山田 太郎" and rows[0].guest_id == "1"


def test_message_keys_stable_for_new_duplicate_text():
    a = [Message("guest", "ありがとうございます", index=0), Message("host", "どうも", index=1)]
    k1 = message_keys(a)
    b = [*a, Message("guest", "ありがとうございます", index=2)]
    k2 = message_keys(b)
    assert k2[0] == k1[0]
    assert k2[2] != k1[0]


def _row(pid, status="予約完了"):
    return ListingRow(Platform.SM, pid, status)


def test_touched_rows_detects_new_changed_and_bumped():
    prev = {"a": (_row("a").fingerprint, 0), "b": (_row("b").fingerprint, 1), "c": (_row("c").fingerprint, 2)}
    # c にメッセージが来て先頭へ。d は新規。b のステータスが変化
    rows = [_row("c"), _row("d"), _row("a"), _row("b", "利用完了")]
    touched = {r.platform_id for r in Orchestrator.touched_rows(rows, prev)}
    assert touched == {"c", "d", "b"}
    # 変化なし
    rows = [_row("a"), _row("b"), _row("c")]
    assert Orchestrator.touched_rows(rows, prev) == []
