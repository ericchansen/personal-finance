from decimal import Decimal

import pytest

from importers.maintenance.rollover import Rollover, plan_close, plan_redate


def make(balance="123.45", date="2025-01-15"):
    return Rollover(
        source_account_id="acct-1",
        source_balance=Decimal(balance),
        destination_name="Vanguard Rollover IRA",
        effective_date=date,
    )


def test_close_transfers_the_whole_balance_out():
    (activity,) = plan_close(make())
    assert activity["activityType"] == "TRANSFER_OUT"
    assert activity["amount"] == pytest.approx(123.45)
    assert activity["accountId"] == "acct-1"


def test_close_is_dated_when_the_rollover_settled_not_today():
    (activity,) = plan_close(make())
    assert activity["activityDate"].startswith("2025-01-15")


def test_close_sends_a_timestamp_because_bare_dates_are_rejected():
    (activity,) = plan_close(make())
    assert activity["activityDate"] == "2025-01-15T00:00:00Z"


def test_close_names_the_destination_so_the_entry_explains_itself():
    (activity,) = plan_close(make())
    assert "Vanguard Rollover IRA" in activity["comment"]


def test_an_empty_account_needs_no_correction():
    assert plan_close(make(balance="0")) == []


def test_a_negative_balance_is_left_alone_rather_than_inverted():
    assert plan_close(make(balance="-5")) == []


def test_the_idempotency_key_pins_account_and_date_so_reruns_collapse():
    first = plan_close(make())[0]["idempotencyKey"]
    second = plan_close(make())[0]["idempotencyKey"]
    assert first == second
    assert plan_close(make(date="2025-02-15"))[0]["idempotencyKey"] != first


def test_redate_moves_a_destination_deposit_back():
    updated = plan_redate({"id": "a", "date": "2025-02-01T00:00:00Z"}, "2025-01-15")
    assert updated["activityDate"] == "2025-01-15T00:00:00Z"


def test_redate_renames_date_because_update_expects_activity_date():
    updated = plan_redate({"id": "a", "date": "2025-02-01T00:00:00Z"}, "2025-01-15")
    assert "date" not in updated


def test_redate_keeps_the_other_fields_since_a_partial_update_blanks_them():
    updated = plan_redate(
        {"id": "a", "date": "2025-02-01", "amount": 200.00, "currency": "USD"},
        "2025-01-15",
    )
    assert updated["amount"] == 200.00
    assert updated["currency"] == "USD"


def test_redate_is_a_no_op_once_the_activity_already_sits_on_the_date():
    assert plan_redate({"id": "a", "date": "2025-01-15T00:00:00Z"}, "2025-01-15") is None


def test_redate_reads_activity_date_when_search_shape_is_absent():
    assert plan_redate({"id": "a", "activityDate": "2025-01-15"}, "2025-01-15") is None


def test_redate_does_not_mutate_the_activity_it_was_given():
    original = {"id": "a", "date": "2025-02-01T00:00:00Z"}
    plan_redate(original, "2025-01-15")
    assert original == {"id": "a", "date": "2025-02-01T00:00:00Z"}
