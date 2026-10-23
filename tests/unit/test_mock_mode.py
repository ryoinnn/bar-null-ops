"""模擬モード（run --mock）＋ simulate の流れ"""

from test_engine import A1, env  # noqa: F401

from barnull.models import Platform, ResState
from barnull.platforms.mock import MockIB, MockSM, MockStore
from barnull.simulate import execute


async def test_mock_flow(env):  # noqa: F811
    eng, slack = env
    eng.mock = MockStore()
    eng.sm, eng.ib = MockSM(eng.cfg, eng.mock), MockIB(eng.cfg, eng.mock)
    await execute(eng, {"event": "new", "platform": "sm", "pid": "TEST-SM-1", "text": None})
    await execute(eng, {"event": "new", "platform": "ib", "pid": "TEST-IB-1", "text": "プロジェクターは使えますか？"})
    sm = eng.db.reservation_by_pid("sm", "TEST-SM-1")
    ib = eng.db.reservation_by_pid("ib", "TEST-IB-1")
    assert sm["state"] == ResState.NOTIFIED and ib["state"] == ResState.NOTIFIED
    assert len(eng.open_drafts(ib["id"])) == 1  # 予約時の問い合わせ
    # SM: スタンプで（模擬）送信
    await eng.on_reaction(A1, sm["slack_ts"], "ok")
    assert eng.res(sm["id"])["state"] == ResState.CONFIRMED
    assert any("(MOCK)" in t for t in slack.texts())
    # IB: 手動で送信済み → 検知
    await execute(eng, {"event": "manual-sent", "platform": "ib", "pid": "TEST-IB-1"})
    assert eng.res(ib["id"])["state"] == ResState.CONFIRMED
    # 予約後のメッセージ → 文案
    await execute(eng, {"event": "guest-message", "platform": "sm", "pid": "TEST-SM-1", "text": "ありがとうございます！"})
    assert len(eng.open_drafts(sm["id"])) == 1
    # 利用完了（問題あり）→ 確認待ち、（問題なし）→ レビューしてクローズ
    await execute(eng, {"event": "complete", "platform": "sm", "pid": "TEST-SM-1", "exit": "気になる点があります"})
    assert eng.res(sm["id"])["state"] == ResState.REVIEW_CONFIRM
    await execute(eng, {"event": "complete", "platform": "ib", "pid": "TEST-IB-1"})
    assert eng.res(ib["id"])["state"] == ResState.CLOSED
    await execute(eng, {"event": "cancel", "platform": "sm", "pid": "TEST-SM-1"})
    assert eng.res(sm["id"])["state"] == ResState.CLOSED
    assert eng.mock.get(Platform.SM, "TEST-SM-1").review_posted is None


async def test_mock_failure_retry_manual_request_and_inquiry(env):  # noqa: F811
    eng, slack = env
    eng.mock = MockStore()
    eng.sm, eng.ib = MockSM(eng.cfg, eng.mock), MockIB(eng.cfg, eng.mock)
    # 失敗（ログイン切れ）→ ERROR → エラーメッセージにスタンプで再実行
    await execute(eng, {"event": "new", "platform": "ib", "pid": "TEST-IB-9", "text": None})
    ib = eng.db.reservation_by_pid("ib", "TEST-IB-9")
    await execute(eng, {"event": "fail-next", "platform": "ib", "pid": "TEST-IB-9", "kind": "login"})
    await eng.on_reaction(A1, ib["slack_ts"], "ok")
    assert eng.res(ib["id"])["state"] == ResState.ERROR
    err = [p for p in slack.posts if "自動処理に失敗しました" in (p.get("text") or "")][-1]
    assert any("再ログインが必要" in t for t in slack.texts())
    await eng.on_reaction(A1, err["ts"], "ok")
    assert eng.res(ib["id"])["state"] == ResState.CONFIRMED
    # 承認制リクエスト → スタンプで承認して定型文を送信（2026-10-10 本人指定）
    await execute(eng, {"event": "new", "platform": "ib", "pid": "TEST-IB-R", "variant": "request"})
    req = eng.db.reservation_by_pid("ib", "TEST-IB-R")
    assert req["state"] == ResState.NOTIFIED
    parent = next(p for p in slack.posts if p.get("ts") == req["slack_ts"])
    assert "予約リクエスト（承認制）" in parent["text"] and "承認して定型文" in str(parent["attachments"])
    await eng.on_reaction(A1, req["slack_ts"], "ok")
    assert eng.res(req["id"])["state"] == ResState.CONFIRMED
    assert eng.mock.get(Platform.IB, "TEST-IB-R").status == "予約確定"
    assert any("承認し、定型文とマニュアルを送信しました" in t for t in slack.texts())
    # SM の予約前の問い合わせ
    await execute(eng, {"event": "new", "platform": "sm", "pid": "TEST-SM-Q", "variant": "inquiry",
                        "text": "20名で使えますか？"})
    q = eng.db.reservation_by_pid("sm", "TEST-SM-Q")
    assert q["kind"] == "inquiry_only" and len(eng.open_drafts(q["id"])) == 1
    assert any("【問い合わせ】" in (p.get("text") or "") for p in slack.posts)
