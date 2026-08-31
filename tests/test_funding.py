from decimal import Decimal

from importers.extracts import fidelity
from tests.test_fidelity import POSITIONS, FakePath


def test_funding_covers_the_full_precision_cost_of_a_buy():
    # 3.333 shares at 10.01 costs 33.36333, but the export rounds the
    # position's current value to 33.36. Funding from the rounded figure
    # leaves the account a fraction of a cent short, which reads as a negative
    # balance.
    export = fidelity.parse_files([FakePath("p.csv", POSITIONS)])
    assert export.value_of("SYN000002") == Decimal("33.36")
    assert export.funding_needed("SYN000002") == Decimal("3.333") * Decimal("10.01")
    assert export.funding_needed("SYN000002") > export.value_of("SYN000002")


def test_funding_never_falls_short_of_what_the_buys_will_cost():
    export = fidelity.parse_files([FakePath("p.csv", POSITIONS)])
    for number in export.accounts:
        cost = sum(
            (p.quantity * p.last_price for p in export.positions
             if p.account_number == number and not p.is_cash),
            Decimal("0"),
        )
        cash = export.cash_of(number)
        assert export.funding_needed(number) >= cost + cash


def test_funding_keeps_the_cash_sweep_at_its_reported_value():
    # The sweep is not bought, so its exported value is used as-is rather than
    # recomputed from a quantity it does not report.
    export = fidelity.parse_files([FakePath("p.csv", POSITIONS)])
    spaxx = [p for p in export.positions if p.is_cash][0]
    assert spaxx.quantity == Decimal("0")
    assert export.funding_needed("SYN000001") >= spaxx.current_value


def test_an_account_with_no_positions_needs_no_funding():
    export = fidelity.parse_files([FakePath("p.csv", POSITIONS)])
    assert export.funding_needed("NOSUCH") == Decimal("0")
