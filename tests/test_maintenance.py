from decimal import Decimal

from importers.maintenance.gap_cli import plan_gap
from importers.maintenance.status_cli import days_since, partition, phantom_balances

from datetime import date


# -- gap filling ----------------------------------------------------------


def test_a_shortfall_is_a_transfer_not_a_deposit():
    # A DEPOSIT into a cash account is reported as income; a plug must not
    # claim money was earned.
    (entry,) = plan_gap("a", Decimal("75.00"), Decimal("100.00"), "2026-01-31", "n")
    assert entry["activityType"] == "TRANSFER_IN"


def test_an_overage_is_a_transfer_out_not_a_withdrawal():
    (entry,) = plan_gap("a", Decimal("125.00"), Decimal("100.00"), "2026-01-31", "n")
    assert entry["activityType"] == "TRANSFER_OUT"


def test_the_entry_carries_the_difference():
    (entry,) = plan_gap("a", Decimal("75.00"), Decimal("100.00"), "2026-01-31", "n")
    assert round(entry["amount"], 2) == 25.00


def test_the_amount_is_positive_and_the_type_carries_direction():
    (entry,) = plan_gap("a", Decimal("125.00"), Decimal("100.00"), "2026-01-31", "n")
    assert entry["amount"] > 0


def test_a_matching_balance_needs_no_entry():
    assert plan_gap("a", Decimal("100.00"), Decimal("100.00"), "2026-01-31", "n") == []


def test_sub_cent_drift_is_ignored_rather_than_recorded():
    assert plan_gap("a", Decimal("100.004"), Decimal("100.00"), "2026-01-31", "n") == []


def test_the_note_explains_what_is_missing():
    (entry,) = plan_gap("a", Decimal("1"), Decimal("2"), "2026-02-26", "Jan not covered")
    assert "Jan not covered" in entry["comment"]


def test_the_transfer_is_marked_external_since_it_has_no_second_leg():
    # Otherwise Wealthfolio reports it as a broken transfer, and a genuinely
    # broken one becomes indistinguishable from a deliberate gap fill.
    (entry,) = plan_gap("a", Decimal("1"), Decimal("2"), "2026-02-26", "n")
    assert entry["subtype"] == "external_transfer"


def test_a_growing_card_balance_is_recorded_as_spending():
    # A credit card balance only grows because the card was used, so the gap
    # is unrecorded spending. WITHDRAWAL is what cards actually take for a
    # charge -- every imported card transaction uses it -- while EXPENSE and
    # TRANSFER_OUT are both rejected outright.
    (entry,) = plan_gap("a", Decimal("-42.12"), Decimal("-80.44"),
                        "2026-08-27", "n", "CREDIT_CARD")
    assert entry["activityType"] == "WITHDRAWAL"
    assert "subtype" not in entry


def test_a_shrinking_card_balance_stays_a_transfer():
    # Paying a card down is money moving from a tracked account, not income.
    (entry,) = plan_gap("a", Decimal("-80.44"), Decimal("-42.12"),
                        "2026-08-27", "n", "CREDIT_CARD")
    assert entry["activityType"] == "TRANSFER_IN"
    assert entry["subtype"] == "external_transfer"


def test_a_cash_account_never_books_the_gap_as_spending():
    (entry,) = plan_gap("a", Decimal("100"), Decimal("40"),
                        "2026-08-27", "n", "CASH")
    assert entry["activityType"] == "TRANSFER_OUT"


def test_rerunning_produces_the_same_idempotency_key():
    first = plan_gap("a", Decimal("1"), Decimal("2"), "2026-02-26", "n")[0]
    second = plan_gap("a", Decimal("1"), Decimal("9"), "2026-02-26", "n")[0]
    assert first["idempotencyKey"] == second["idempotencyKey"]


def test_a_timestamp_is_sent_because_bare_dates_are_rejected():
    (entry,) = plan_gap("a", Decimal("1"), Decimal("2"), "2026-02-26", "n")
    assert entry["activityDate"] == "2026-02-26T00:00:00Z"


# -- staleness ------------------------------------------------------------


def row(name, stale, spending=True, activities=1, balance="0", closed=False):
    return {"name": name, "stale": stale, "spending": spending,
            "activities": activities, "balance": Decimal(balance), "last": None,
            "closed": closed}


def test_spending_accounts_are_reported_separately_from_investments():
    cash, net, fresh = partition(
        [row("card", 100), row("401k", 100, spending=False)], 45
    )
    assert [r["name"] for r in cash] == ["card"]
    assert [r["name"] for r in net] == ["401k"]


def test_closed_accounts_are_not_reported_as_needing_a_refresh():
    # The question a closed account raises is whether its balance is real,
    # not whether to go download more of it.
    cash, net, fresh = partition([row("old card", 900, closed=True)], 45)
    assert cash == [] and net == [] and fresh == []


def test_a_closed_account_with_a_balance_is_surfaced():
    rows = [row("old card", 900, balance="-25.00", closed=True)]
    assert [r["name"] for r in phantom_balances(rows)] == ["old card"]


def test_a_closed_account_at_zero_is_not_surfaced():
    assert phantom_balances([row("retired", 900, balance="0", closed=True)]) == []


def test_an_open_account_with_a_balance_is_not_a_phantom():
    assert phantom_balances([row("live card", 10, balance="-500")]) == []


def test_an_account_inside_the_threshold_is_fresh():
    cash, net, fresh = partition([row("card", 10)], 45)
    assert not cash and not net
    assert [r["name"] for r in fresh] == ["card"]


def test_an_account_that_never_reported_counts_as_stale():
    # A shell account an aggregator created and never populated is exactly the
    # gap this report exists to surface.
    cash, _, fresh = partition([row("phantom", None)], 45)
    assert [r["name"] for r in cash] == ["phantom"]
    assert fresh == []


def test_the_threshold_boundary_is_not_stale():
    _, _, fresh = partition([row("card", 45)], 45)
    assert len(fresh) == 1


def test_days_since_measures_from_the_given_day():
    assert days_since("2026-01-01", date(2026, 1, 31)) == 30


def test_days_since_tolerates_a_full_timestamp():
    assert days_since("2026-01-01T00:00:00Z", date(2026, 1, 31)) == 30


def test_days_since_returns_none_when_there_is_no_date():
    assert days_since(None, date(2026, 1, 31)) is None
    assert days_since("", date(2026, 1, 31)) is None


def test_days_since_returns_none_for_an_unparseable_date():
    assert days_since("not-a-date", date(2026, 1, 31)) is None
