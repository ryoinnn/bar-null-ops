"""伝票のプロモーターフラグ（contract.md §2-2）。名前はすべて架空。"""

from __future__ import annotations

from helpers import event, make_deps, parents, slip_msg, threads

from barnull_sales.asana import FIELDS
from barnull_sales.extract import Entry, Parsed
from barnull_sales.process import handle_event
from barnull_sales.restore import task_to_slip
from barnull_sales.texts import asana_notes


def test_promoter_is_kept_on_slip_and_shown_in_slack():
    deps = make_deps([slip_msg(Entry(month=10, day=5, customer_name="顧客A", amount=50000, promoter_name=" 紹介太郎 "))])
    slip = deps.store.get_slip(handle_event(event("2026-10-05 23:00"), "message", deps)["slip_ids"][0])
    assert slip.promoter_name == "紹介太郎"
    assert "プロモーター：紹介太郎" in parents(deps)[0]["text"]
    assert "プロモーター: 紹介太郎" in asana_notes(slip)


def test_no_promoter_line_means_empty():
    deps = make_deps([slip_msg(Entry(month=10, day=5, customer_name="顧客A", amount=50000))])
    slip = deps.store.get_slip(handle_event(event("2026-10-05 23:00"), "message", deps)["slip_ids"][0])
    assert slip.promoter_name == "" and "プロモーター" not in parents(deps)[0]["text"]


def test_old_saved_parse_without_promoter_still_loads():
    p = Parsed.from_json({"kind": "slip_content", "confidence": 0.9, "correction": None, "collection_report": None,
                          "cash_deposit": None, "entries": [{"month": 10, "day": 5, "customer_name": "顧客A",
                                                             "amount": 1000, "note": "", "unpaid_amount": None,
                                                             "unpaid_due_text": "", "payment_hint": ""}]})
    assert p.entries[0].promoter_name == ""


def test_correction_sets_and_clears_promoter():
    def corr(promoter):
        return Parsed(kind="correction", confidence=0.9, correction={
            "target_customer_name": "顧客A", "new_amount": None, "new_customer_name": None,
            "new_promoter_name": promoter, "description": "訂正"})

    deps = make_deps([slip_msg(Entry(month=10, day=5, customer_name="顧客A", amount=50000)),
                      corr("紹介花子"), corr("")])
    sid = handle_event(event("2026-10-05 23:00", "E1"), "message", deps)["slip_ids"][0]
    assert handle_event(event("2026-10-05 23:10", "E2"), "message", deps)["status"] == "corrected"
    slip = deps.store.get_slip(sid)
    assert slip.promoter_name == "紹介花子"
    assert any("プロモーター なし → 紹介花子" in t for t in threads(deps, slip))
    handle_event(event("2026-10-05 23:20", "E3"), "message", deps)
    assert deps.store.get_slip(sid).promoter_name == ""


def test_restore_reads_promoter_field():
    name = FIELDS["promoter"][0]
    task = {"gid": "1", "name": "x", "notes": "", "completed": True, "completed_at": "2026-10-06T00:00:00Z",
            "custom_fields": [{"name": "伝票ID", "resource_subtype": "text", "text_value": "S-20261005-001"},
                              {"name": name, "resource_subtype": "text", "text_value": "紹介太郎"}]}
    assert task_to_slip(task).promoter_name == "紹介太郎"
