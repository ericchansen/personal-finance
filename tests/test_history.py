from datetime import date
from decimal import Decimal

from importers.assets.history import (
    LoanPhase,
    Valuation,
    interpolate,
    loan_balances,
    month_ends,
    owned_window,
    to_quotes,
)


def test_month_ends_covers_both_endpoints():
    days = month_ends(date(2021, 4, 16), date(2021, 7, 20))
    assert days[0] == date(2021, 4, 16)
    assert days[-1] == date(2021, 7, 20)


def test_month_ends_of_a_single_day():
    assert month_ends(date(2021, 4, 16), date(2021, 4, 16)) == [date(2021, 4, 16)]


def test_month_ends_of_a_backwards_range_is_empty():
    assert month_ends(date(2021, 7, 1), date(2021, 4, 1)) == []


def test_month_ends_crosses_a_year_boundary():
    days = month_ends(date(2021, 11, 5), date(2022, 2, 5))
    assert date(2022, 1, 1) in days


# -- property values ------------------------------------------------------

BOUGHT = Valuation(date(2024, 1, 15), Decimal("250000"))
APPRAISED = Valuation(date(2025, 1, 15), Decimal("275000"))


def test_value_is_held_flat_before_the_first_observation():
    series = interpolate([BOUGHT, APPRAISED], date(2023, 1, 1), date(2025, 1, 15))
    assert series[0][1] == Decimal("250000")


def test_value_is_held_flat_after_the_last_observation():
    series = interpolate([BOUGHT, APPRAISED], date(2024, 1, 15), date(2025, 6, 1))
    assert series[-1][1] == Decimal("275000")


def test_value_moves_gradually_between_observations():
    series = interpolate([BOUGHT, APPRAISED], date(2024, 1, 15), date(2025, 1, 15))
    midpoints = [v for d, v in series if BOUGHT.on < d < APPRAISED.on]
    assert all(BOUGHT.value < v < APPRAISED.value for v in midpoints)
    assert midpoints == sorted(midpoints)


def test_interpolation_is_linear_at_the_halfway_point():
    a = Valuation(date(2024, 1, 1), Decimal("100000"))
    b = Valuation(date(2024, 3, 1), Decimal("200000"))
    series = dict(interpolate([a, b], date(2024, 1, 1), date(2024, 3, 1)))
    # 2024-02-01 is 31 of 60 days along.
    expected = Decimal("100000") + Decimal("100000") * Decimal(31) / Decimal(60)
    assert series[date(2024, 2, 1)] == expected.quantize(Decimal("0.01"))


def test_no_observations_yields_no_series():
    assert interpolate([], date(2024, 1, 1), date(2024, 6, 1)) == []


def test_observations_out_of_order_are_still_handled():
    series = interpolate([APPRAISED, BOUGHT], date(2024, 1, 15), date(2025, 1, 15))
    assert series[0][1] == Decimal("250000")
    assert series[-1][1] == Decimal("275000")


# -- loan balances --------------------------------------------------------

OLD_LOAN = LoanPhase(
    principal=Decimal("200000"), annual_rate=Decimal("0.04"), term_months=360,
    first_payment=date(2022, 2, 1), starts=date(2022, 1, 15), ends=date(2023, 6, 15),
)


def test_a_loan_balance_declines_over_time():
    series = loan_balances([OLD_LOAN], date(2022, 2, 1), date(2023, 6, 15))
    values = [v for _, v in series]
    assert values == sorted(values, reverse=True)
    assert values[0] < Decimal("200000")


def test_an_authoritative_payoff_overrides_amortization():
    payoff = LoanPhase(
        Decimal("100000"), Decimal("0.05"), 360, date(2024, 2, 1),
        date(2024, 1, 1), date(2024, 6, 1), Decimal("98765.43"),
        date(2024, 6, 1),
    )
    series = dict(loan_balances([payoff], date(2024, 1, 1), date(2024, 6, 1)))
    assert series[date(2024, 6, 1)] == Decimal("98765.43")


def test_the_balance_is_zero_before_the_loan_starts():
    series = dict(loan_balances([OLD_LOAN], date(2022, 1, 1), date(2022, 2, 1)))
    assert series[date(2022, 1, 1)] == Decimal("0")


def test_the_balance_is_zero_after_the_loan_ends():
    # A sold house's mortgage must disappear, not amortize forever.
    series = dict(loan_balances([OLD_LOAN], date(2023, 6, 15), date(2024, 1, 1)))
    assert series[date(2024, 1, 1)] == Decimal("0")


def test_a_refinance_switches_to_the_new_loan():
    old = LoanPhase(Decimal("300000"), Decimal("0.06"), 360,
                    date(2024, 2, 1), date(2024, 1, 15), date(2025, 4, 30))
    new = LoanPhase(Decimal("290000"), Decimal("0.05"), 360,
                    date(2025, 5, 1), date(2025, 5, 1))
    series = dict(loan_balances([old, new], date(2024, 2, 1), date(2025, 8, 1)))
    # The new loan resets the balance upward relative to the amortized old one.
    assert series[date(2025, 8, 1)] > Decimal("285000")
    assert series[date(2024, 2, 1)] < Decimal("300000")


def test_two_concurrent_loans_sum_rather_than_overwrite():
    # Buying before selling means both mortgages exist for a few months, which
    # is real and must not look like a bug. An earlier version assigned rather
    # than accumulated, so the second loan silently erased the first.
    new_loan = LoanPhase(Decimal("300000"), Decimal("0.06"), 360,
                         date(2023, 5, 1), date(2023, 4, 15))
    both = loan_balances([OLD_LOAN, new_loan], date(2023, 5, 1), date(2023, 5, 1))[0][1]
    old_only = loan_balances([OLD_LOAN], date(2023, 5, 1), date(2023, 5, 1))[0][1]
    new_only = loan_balances([new_loan], date(2023, 5, 1), date(2023, 5, 1))[0][1]
    assert both == old_only + new_only
    assert both > new_only


# -- ownership window -----------------------------------------------------


def test_a_disposed_asset_goes_to_zero_rather_than_vanishing():
    series = [(date(2023, 6, 1), Decimal("150000")),
              (date(2023, 7, 1), Decimal("150000"))]
    out = dict(owned_window(series, date(2022, 1, 15), date(2023, 6, 15)))
    assert out[date(2023, 7, 1)] == Decimal("0")


def test_an_asset_is_absent_before_it_was_acquired():
    series = [(date(2021, 1, 1), Decimal("300001"))]
    out = dict(owned_window(series, date(2021, 4, 16), None))
    assert out[date(2021, 1, 1)] == Decimal("0")


def test_a_still_owned_asset_is_left_alone():
    series = [(date(2025, 6, 1), Decimal("275000"))]
    out = dict(owned_window(series, date(2024, 1, 15), None))
    assert out[date(2025, 6, 1)] == Decimal("275000")


# -- quote shaping --------------------------------------------------------


def test_quotes_use_the_asset_uuid_as_the_symbol():
    # The import endpoint rejects a ticker with a foreign-key violation.
    quotes = to_quotes("abc-123", [(date(2024, 1, 1), Decimal("100"))])
    assert quotes[0]["symbol"] == "abc-123"


def test_a_quote_is_flat_across_open_high_low_close():
    quotes = to_quotes("abc-123", [(date(2024, 1, 1), Decimal("100"))])
    q = quotes[0]
    assert q["open"] == q["high"] == q["low"] == q["close"] == 100.0
    assert q["dataSource"] == "MANUAL"
