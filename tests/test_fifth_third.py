from decimal import Decimal

from importers.extracts import parsers

FIFTH_THIRD = '''Date,Description,"Check Number",Amount
03/02/2026,"WEB INITIATED PAYMENT AT EXAMPLE AUTOPAY 030226",,-543.52
02/27/2026,"WEB INITIATED PAYMENT AT EXAMPLE P2P 022726",,250.00
04/20/2026,"EARLY PAY: EXAMPLE PAYMENTS 042126",,27.96
01/15/2026,"CHECK PAID",1234,-80.00
'''


def test_fifth_third_is_not_mistaken_for_ally():
    # Both headers carry Amount and Description; only one has a check number.
    assert parsers.sniff(FIFTH_THIRD) == "fifth-third-csv"


def test_quotes_around_the_column_name_do_not_defeat_the_sniff():
    # The real export quotes "Check Number"; an unquoted variant must work too.
    unquoted = FIFTH_THIRD.replace('"Check Number"', "Check Number")
    assert parsers.sniff(unquoted) == "fifth-third-csv"
    assert parsers.sniff(FIFTH_THIRD) == "fifth-third-csv"


def test_every_row_is_read():
    assert len(parsers.parse_text(FIFTH_THIRD, source="EXPORT.CSV").transactions) == 4


def test_the_format_is_reported_honestly():
    assert parsers.parse_text(FIFTH_THIRD, source="x").format == "fifth-third-csv"


def test_money_out_stays_negative():
    txns = parsers.parse_text(FIFTH_THIRD, source="x").transactions
    payment = [t for t in txns if "AUTOPAY" in t.description][0]
    assert payment.amount == Decimal("-543.52")


def test_money_in_stays_positive():
    txns = parsers.parse_text(FIFTH_THIRD, source="x").transactions
    p2p = [t for t in txns if "P2P" in t.description][0]
    assert p2p.amount == Decimal("250.00")


def test_a_check_number_is_kept_in_the_description():
    txns = parsers.parse_text(FIFTH_THIRD, source="x").transactions
    check = [t for t in txns if "CHECK PAID" in t.description][0]
    assert "check 1234" in check.description


def test_a_blank_check_number_adds_nothing():
    txns = parsers.parse_text(FIFTH_THIRD, source="x").transactions
    assert all("check " not in t.description for t in txns if "CHECK PAID" not in t.description)


def test_ids_are_synthetic_because_the_format_supplies_none():
    txns = parsers.parse_text(FIFTH_THIRD, source="x").transactions
    assert all(t.id_is_synthetic for t in txns)
    assert len({t.source_id for t in txns}) == 4


def test_ids_are_stable_across_runs():
    first = parsers.parse_text(FIFTH_THIRD, source="x", account_id="acct")
    second = parsers.parse_text(FIFTH_THIRD, source="x", account_id="acct")
    assert [t.source_id for t in first.transactions] == [
        t.source_id for t in second.transactions
    ]


def test_rows_are_not_assumed_to_be_in_date_order():
    # The real export groups by statement period, so row order is not date order.
    dates = [t.date for t in parsers.parse_text(FIFTH_THIRD, source="x").transactions]
    assert dates != sorted(dates)


def test_ally_still_sniffs_as_ally():
    ally = "Date, Time, Amount, Type, Description\n2026-01-02, 10:00, -5.00, Withdrawal, COFFEE\n"
    assert parsers.sniff(ally) == "ally-csv"
