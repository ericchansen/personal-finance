"""Tests for fixed-rate loan amortization.

Figures here are synthetic round numbers chosen so the expected results can be
checked by hand.
"""

from __future__ import annotations

import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "importers" / "assets"))

import loan  # noqa: E402


# --------------------------------------------------------------------------
# payment
# --------------------------------------------------------------------------

def test_monthly_payment_matches_known_figure():
    """$100,000 at 6% over 30 years is the textbook $599.55."""
    payment = loan.monthly_payment(Decimal("100000"), Decimal("0.06"), 360)
    assert payment == Decimal("599.55")


def test_zero_rate_is_straight_line():
    assert loan.monthly_payment(Decimal("12000"), Decimal("0"), 12) == Decimal("1000.00")


def test_rejects_non_positive_term():
    with pytest.raises(ValueError):
        loan.monthly_payment(Decimal("1000"), Decimal("0.05"), 0)


# --------------------------------------------------------------------------
# elapsed payments
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "as_of,expected",
    [
        (date(2026, 4, 30), 0),   # before the first payment is due
        (date(2026, 5, 1), 1),    # first payment day
        (date(2026, 5, 31), 1),
        (date(2026, 8, 26), 4),   # May, June, July, August
        (date(2027, 5, 1), 13),
    ],
)
def test_payments_elapsed(as_of, expected):
    assert loan.payments_elapsed(date(2026, 5, 1), as_of) == expected


def test_payment_not_counted_before_its_day_of_month():
    assert loan.payments_elapsed(date(2026, 5, 15), date(2026, 6, 14)) == 1
    assert loan.payments_elapsed(date(2026, 5, 15), date(2026, 6, 15)) == 2


# --------------------------------------------------------------------------
# amortization
# --------------------------------------------------------------------------

def test_no_payments_leaves_balance_untouched():
    state = loan.amortize(Decimal("100000"), Decimal("0.06"), 360, 0)
    assert state.balance == Decimal("100000")
    assert state.interest_paid == Decimal("0")


def test_first_payment_is_mostly_interest():
    """The point of amortization: early payments barely touch principal."""
    state = loan.amortize(Decimal("100000"), Decimal("0.06"), 360, 1)
    assert state.interest_paid == Decimal("500.00")      # 100000 * 0.06/12
    assert state.principal_paid == Decimal("99.55")
    assert state.balance == Decimal("99900.45")


def test_balance_decreases_monotonically():
    balances = [
        loan.amortize(Decimal("100000"), Decimal("0.06"), 360, n).balance
        for n in range(0, 13)
    ]
    assert all(later < earlier for earlier, later in zip(balances, balances[1:]))


def test_loan_fully_repays_at_term():
    state = loan.amortize(Decimal("100000"), Decimal("0.06"), 360, 360)
    assert state.balance == Decimal("0.00")


def test_balance_never_goes_negative_past_term():
    state = loan.amortize(Decimal("100000"), Decimal("0.06"), 360, 500)
    assert state.balance == Decimal("0.00")
    assert state.payments_made == 360


def test_principal_plus_balance_equals_original():
    principal = Decimal("100000")
    state = loan.amortize(principal, Decimal("0.06"), 360, 24)
    assert state.principal_paid + state.balance == principal


def test_rejects_negative_payments():
    with pytest.raises(ValueError):
        loan.amortize(Decimal("1000"), Decimal("0.05"), 12, -1)


def test_balance_as_of_combines_elapsed_and_amortization():
    by_date = loan.balance_as_of(
        Decimal("100000"), Decimal("0.06"), 360,
        first_payment=date(2026, 5, 1), as_of=date(2026, 8, 26),
    )
    by_count = loan.amortize(Decimal("100000"), Decimal("0.06"), 360, 4)
    assert by_date.balance == by_count.balance
    assert by_date.payments_made == 4
