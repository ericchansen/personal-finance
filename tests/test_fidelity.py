from decimal import Decimal

from importers.extracts import fidelity

POSITIONS = """\ufeffAccount number,Account name,Symbol,Description,Quantity,Last price,\
Last price change,Current value,Today's gain/loss dollar,Today's gain/loss percent,\
Total gain/loss dollar,Total gain/loss percent,Percent of account,Cost basis total,\
Average cost basis,Type
SYN000001,Individual - TOD,SPAXX**,HELD IN MONEY MARKET,,,,$100.00,,,,,10.00%,,,Cash,
SYN000001,Individual - TOD,ACME,ACME CORP COM,8,$22.97,-$0.50,"$1,234.56",-$4.00,\
-1.00%,-$20.00,-2.00%,90.00%,$160.00,$20.00,Cash,
SYN000002,EXAMPLE 401K PLAN,SYNTH0001,SYNTHETIC COLLECTIVE TRUST,3.333,$10.01,+$0.10,\
"$33.36",$0.00,0.00%,+$3.36,+11.20%,100.00%,$30.00,$9.0009,,

"The data and information in this spreadsheet is provided to you solely for your use."

"Date downloaded Jan-02-2026 1:00 p.m ET"
"""

HISTORY = """\ufeff

Run Date,Account,Account Number,Action,Symbol,Description,Type,Price ($),Quantity,\
Commission ($),Fees ($),Accrued Interest ($),Amount ($),Settlement Date
01/02/2026,EXAMPLE 401K PLAN,SYN000002,Contributions,,SYNTHETIC COLLECTIVE TRUST,,,2.5,,,,25.00,
01/02/2026,Individual - TOD,SYN000001,DIVIDEND RECEIVED MONEY MARKET (SPAXX) (Cash),\
SPAXX,MONEY MARKET,Cash,"",0,"","","",0.78,""
01/01/2026,Individual - TOD,SYN000001,YOU BOUGHT ACME CORP (ACME) (Cash),ACME,\
ACME CORP,Cash,20.00,8,"","","","-160.00",01/02/2026

"The data and information in this spreadsheet is provided to you solely for your use."
"""

ESPP = """\ufeffTransaction Date,Transaction Type,Plan Name,Offering period,Quantity,\
Net Proceeds
2026-01-02,Payroll contribution,EXAMPLE ESPP PLAN,Jan-01-2026 to Mar-31-2026,\
"$100.00","$100.00"
2026-01-16,Payroll contribution,EXAMPLE ESPP PLAN,Jan-01-2026 to Mar-31-2026,\
"$250.00","$250.00"
2026-01-31,Contribution change,EXAMPLE ESPP PLAN,Apr-01-2026 to Jun-30-2026,10%,10%
"""


# -- positions ------------------------------------------------------------


def test_positions_stop_before_the_legal_disclaimer():
    assert len(fidelity.parse_positions(POSITIONS)) == 3


def test_money_market_is_recognised_as_cash_not_a_holding():
    spaxx = fidelity.parse_positions(POSITIONS)[0]
    assert spaxx.is_cash
    assert spaxx.current_value == Decimal("100.00")


def test_footnote_asterisks_are_stripped_from_the_symbol():
    assert fidelity.parse_positions(POSITIONS)[0].symbol == "SPAXX"


def test_thousands_separators_and_dollar_signs_are_read():
    acme = fidelity.parse_positions(POSITIONS)[1]
    assert acme.current_value == Decimal("1234.56")
    assert acme.quantity == Decimal("8")


def test_a_collective_trust_is_flagged_for_manual_pricing():
    trust = fidelity.parse_positions(POSITIONS)[2]
    assert trust.needs_manual_price
    assert trust.quantity == Decimal("3.333")


def test_an_ordinary_ticker_is_not_flagged_for_manual_pricing():
    assert not fidelity.parse_positions(POSITIONS)[1].needs_manual_price


def test_cusip_detection_requires_nine_characters_with_a_digit():
    assert fidelity.is_cusip("SYNTH0001")
    assert not fidelity.is_cusip("NVDA")
    assert not fidelity.is_cusip("ABCDEFGHI")  # nine letters, still a ticker shape
    assert not fidelity.is_cusip("12345678")  # eight characters


def test_cusip_detection_ignores_footnote_markers():
    assert fidelity.is_cusip("SYNTH0001**")


# -- history --------------------------------------------------------------


def test_history_skips_the_blank_lines_above_the_header():
    assert len(fidelity.parse_transactions(HISTORY)) == 3


def test_history_rows_identify_their_own_account():
    rows = fidelity.parse_transactions(HISTORY)
    assert {r.account_number for r in rows} == {"SYN000001", "SYN000002"}


def test_negative_amounts_keep_their_sign():
    bought = [r for r in fidelity.parse_transactions(HISTORY) if "BOUGHT" in r.action][0]
    assert bought.amount == Decimal("-160.00")


def test_quoted_empty_fields_read_as_zero_not_an_error():
    dividend = [
        r for r in fidelity.parse_transactions(HISTORY) if "DIVIDEND" in r.action
    ][0]
    assert dividend.price == Decimal("0")
    assert dividend.amount == Decimal("0.78")


def test_settlement_date_is_optional():
    rows = fidelity.parse_transactions(HISTORY)
    assert rows[0].settlement_date is None
    assert any(r.settlement_date is not None for r in rows)


# -- espp -----------------------------------------------------------------


def test_espp_reads_payroll_contributions():
    rows = fidelity.parse_espp(ESPP)
    assert [r.amount for r in rows] == [Decimal("100.00"), Decimal("250.00")]


def test_espp_ignores_rate_changes_which_are_percentages_not_money():
    assert all("change" not in r.plan_name.lower() for r in fidelity.parse_espp(ESPP))
    assert len(fidelity.parse_espp(ESPP)) == 2


# -- combining ------------------------------------------------------------


class FakePath:
    def __init__(self, name, text):
        self.name = name
        self._text = text

    def read_text(self, encoding="utf-8"):
        return self._text


def test_files_are_routed_by_shape_not_by_filename():
    export = fidelity.parse_files(
        [
            FakePath("Accounts_History.csv", HISTORY),
            FakePath("Accounts_History (1).csv", POSITIONS),
            FakePath("Transaction History.csv", ESPP),
        ]
    )
    assert len(export.positions) == 3
    assert len(export.transactions) == 3
    assert len(export.espp) == 2


def test_overlapping_history_downloads_do_not_double_count():
    export = fidelity.parse_files(
        [FakePath("a.csv", HISTORY), FakePath("b.csv", HISTORY)]
    )
    assert len(export.transactions) == 3


def test_account_value_sums_positions_including_the_cash_sweep():
    export = fidelity.parse_files([FakePath("p.csv", POSITIONS)])
    assert export.value_of("SYN000001") == Decimal("1334.56")
    assert export.cash_of("SYN000001") == Decimal("100.00")


def test_accounts_are_discovered_from_either_table():
    export = fidelity.parse_files(
        [FakePath("p.csv", POSITIONS), FakePath("h.csv", HISTORY)]
    )
    assert export.accounts == {
        "SYN000001": "Individual - TOD",
        "SYN000002": "EXAMPLE 401K PLAN",
    }
