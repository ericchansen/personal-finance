"""Tests for extract parsing.

All fixtures are synthetic. Never copy rows from a real export into this repo.
"""

from __future__ import annotations

import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "importers" / "extracts"))

import parsers  # noqa: E402


OFX = """OFXHEADER:100
DATA:OFXSGML
<OFX>
<BANKMSGSRSV1><STMTTRNRS><STMTRS>
<CURDEF>USD
<BANKACCTFROM><ACCTID>SYNTHETIC-CHECKING<ACCTTYPE>CHECKING</BANKACCTFROM>
<BANKTRANLIST>
<STMTTRN><TRNTYPE>DEBIT<DTPOSTED>20260115120000[-5:EST]<TRNAMT>-42.50
<FITID>TXN-0001<NAME>CORNER MARKET</STMTTRN>
<STMTTRN><TRNTYPE>CREDIT<DTPOSTED>20260116<TRNAMT>1200.00
<FITID>TXN-0002<NAME>PAYROLL</STMTTRN>
</BANKTRANLIST>
<LEDGERBAL><BALAMT>-25.00<DTASOF>20260116</LEDGERBAL>
</STMTRS></STMTTRNRS></BANKMSGSRSV1></OFX>
"""

CITI_CSV = """Status,Date,Description,Debit,Credit
Cleared,01/15/2026,CORNER MARKET,42.50,
Cleared,01/16/2026,PAYMENT THANK YOU,,-300.00
"""

ALLY_CSV = """Date, Time, Amount, Type, Description
2026-01-15, 12:30:00, -42.50, Withdrawal, CORNER MARKET
2026-01-16, 09:00:00, 1200.00, Deposit, PAYROLL
"""


# --------------------------------------------------------------------------
# OFX
# --------------------------------------------------------------------------

def test_ofx_reads_transactions_and_account_context():
    e = parsers.parse_ofx(OFX, source="t.ofx")
    assert e.format == "ofx"
    assert e.account_id == "SYNTHETIC-CHECKING"
    assert e.account_type == "CHECKING"
    assert e.balance == Decimal("-25.00")
    assert e.balance_date == date(2026, 1, 16)
    assert len(e.transactions) == 2


def test_ofx_uses_the_supplied_transaction_id():
    """FITID is the whole reason to prefer OFX: it makes re-import safe."""
    e = parsers.parse_ofx(OFX)
    assert [t.source_id for t in e.transactions] == ["TXN-0001", "TXN-0002"]
    assert all(not t.id_is_synthetic for t in e.transactions)


def test_ofx_handles_timestamp_and_timezone_in_dtposted():
    e = parsers.parse_ofx(OFX)
    assert e.transactions[0].date == date(2026, 1, 15)


def test_ofx_signs_amounts_from_the_file():
    e = parsers.parse_ofx(OFX)
    assert e.transactions[0].amount == Decimal("-42.50")
    assert e.transactions[1].amount == Decimal("1200.00")


def test_ofx_falls_back_to_memo_when_name_is_absent():
    text = OFX.replace("<NAME>CORNER MARKET", "<MEMO>MEMO ONLY")
    assert parsers.parse_ofx(text).transactions[0].description == "MEMO ONLY"


def test_ofx_synthesizes_an_id_when_fitid_is_missing():
    text = OFX.replace("<FITID>TXN-0001", "")
    t = parsers.parse_ofx(text).transactions[0]
    assert t.id_is_synthetic
    assert len(t.source_id.removesuffix(":1")) == 32
    assert t.source_id.endswith(":1")


def test_ofx_skips_unparseable_transactions():
    text = OFX.replace("<TRNAMT>-42.50", "<TRNAMT>not-a-number")
    assert len(parsers.parse_ofx(text).transactions) == 1


# --------------------------------------------------------------------------
# Citi CSV
# --------------------------------------------------------------------------

def test_citi_merges_debit_and_credit_columns():
    """A charge is a positive Debit, a payment a negative Credit; both are
    negated so money out is negative, as OFX and the rest of the pipeline
    expect."""
    e = parsers.parse_citi_csv(CITI_CSV, account_id="synthetic-card")
    assert [t.amount for t in e.transactions] == [Decimal("-42.50"), Decimal("300.00")]


def test_citi_credit_column_is_negative_in_the_file():
    """A card payment arrives as a negative Credit and must become an inflow."""
    text = "Status,Date,Description,Debit,Credit\nCleared,01/16/2026,PAYMENT,,-3000.00\n"
    assert parsers.parse_citi_csv(text).transactions[0].amount == Decimal("3000.00")


def test_citi_savings_credit_is_positive_in_the_file():
    """Regression: Citi's savings export writes Credit as positive while the
    card export writes it as negative. Negating both reconciled the cards and
    inverted savings, so Credit is treated as money in either way."""
    text = 'Status,Date,Description,Debit,Credit\nCleared,07-24-2026,"Interest Payment ",,1.57,\n'
    assert parsers.parse_citi_csv(text).transactions[0].amount == Decimal("1.57")


def test_citi_csv_sign_matches_ofx():
    """Regression: the CSV inverts the sign relative to OFX for the same
    account. Totals came out as exact mirrors, so importing the CSV unchanged
    would have flipped every transaction in the ledger."""
    csv_text = "Status,Date,Description,Debit,Credit\nCleared,01/19/2026,PARKING,11.99,\n"
    ofx_text = (
        "<OFX><STMTTRN><DTPOSTED>20260119<TRNAMT>-11.99"
        "<FITID>X1<NAME>PARKING</STMTTRN></OFX>"
    )
    from_csv = parsers.parse_citi_csv(csv_text).transactions[0].amount
    from_ofx = parsers.parse_ofx(ofx_text).transactions[0].amount
    assert from_csv == from_ofx == Decimal("-11.99")


def test_citi_negative_debit_is_a_refund():
    """A negative Debit is a refund, which must end up positive."""
    text = "Status,Date,Description,Debit,Credit\nCleared,02/12/2026,REFUND,-3.00,\n"
    assert parsers.parse_citi_csv(text).transactions[0].amount == Decimal("3.00")


def test_citi_keeps_zero_amount_transactions():
    """Regression: a waived $0.00 fee has Debit=0.00 and no Credit. Treating
    zero as absent dropped a valid synthetic row."""
    text = ("Status,Date,Description,Debit,Credit\n"
            "Cleared,11/05/2025,MEMBERSHIP FEE,0.00,\n")
    e = parsers.parse_citi_csv(text)
    assert len(e.transactions) == 1
    assert e.transactions[0].amount == Decimal("0.00")
    assert e.transactions[0].description == "MEMBERSHIP FEE"


def test_citi_parses_us_dates():
    e = parsers.parse_citi_csv(CITI_CSV, account_id="synthetic-card")
    assert e.transactions[0].date == date(2026, 1, 15)


def test_citi_savings_uses_hyphenated_dates():
    """Regression: Citi is not internally consistent. Card exports use
    MM/DD/YYYY, savings exports use MM-DD-YYYY, and rejecting the latter
    silently produced an empty parse of a valid fixture."""
    text = 'Status,Date,Description,Debit,Credit\nCleared,01-24-2026,"Interest Payment ",,1.25,\n'
    e = parsers.parse_citi_csv(text, account_id="synthetic-savings")
    assert len(e.transactions) == 1
    assert e.transactions[0].date == date(2026, 1, 24)
    assert e.transactions[0].amount == Decimal("1.25")


def test_citi_tolerates_a_trailing_empty_column():
    """Savings rows carry one more comma than the header declares."""
    text = 'Status,Date,Description,Debit,Credit\nCleared,01-24-2026,"Interest ",,1.25,\n'
    assert len(parsers.parse_citi_csv(text).transactions) == 1


def test_citi_ids_are_synthetic():
    e = parsers.parse_citi_csv(CITI_CSV, account_id="synthetic-card")
    assert all(t.id_is_synthetic for t in e.transactions)


# --------------------------------------------------------------------------
# Ally CSV
# --------------------------------------------------------------------------

def test_ally_handles_padded_headers():
    """Every Ally header after the first has a leading space."""
    e = parsers.parse_ally_csv(ALLY_CSV, account_id="ally-checking")
    assert len(e.transactions) == 2
    assert e.transactions[0].description == "CORNER MARKET"
    assert e.transactions[0].kind == "Withdrawal"


def test_ally_keeps_the_sign_of_the_amount():
    e = parsers.parse_ally_csv(ALLY_CSV, account_id="ally-checking")
    assert e.transactions[0].amount == Decimal("-42.50")
    assert e.transactions[1].amount == Decimal("1200.00")


def test_ally_carries_no_account_id_of_its_own():
    """Nothing in an Ally export identifies the account, so the caller must
    supply it. This is why download order has to be recorded."""
    e = parsers.parse_ally_csv(ALLY_CSV)
    assert e.account_id is None


# --------------------------------------------------------------------------
# synthesized ids
# --------------------------------------------------------------------------

def test_synthetic_id_is_stable_across_runs():
    args = ("acct", date(2026, 1, 15), Decimal("-42.50"), "Corner Market")
    assert parsers.synthesize_id(*args) == parsers.synthesize_id(*args)


def test_synthetic_id_ignores_description_case_and_padding():
    a = parsers.synthesize_id("acct", date(2026, 1, 15), Decimal("-1"), " Corner Market ")
    b = parsers.synthesize_id("acct", date(2026, 1, 15), Decimal("-1"), "corner market")
    assert a == b


@pytest.mark.parametrize(
    "field,value",
    [("account", "other"), ("amount", Decimal("-99")), ("description", "Elsewhere")],
)
def test_synthetic_id_varies_with_each_input(field, value):
    base = dict(account="acct", when=date(2026, 1, 15),
                amount=Decimal("-42.50"), description="Corner Market")
    changed = {**base, field if field != "amount" else "amount": value}
    assert parsers.synthesize_id(**base) != parsers.synthesize_id(**changed)


def test_identical_same_day_csv_rows_keep_replay_stable_occurrences():
    text = (
        "Date, Time, Amount, Type, Description\n"
        "01/15/2026, 08:00 AM,-5.00,DEBIT,Coffee\n"
        "01/15/2026, 09:00 AM,-5.00,DEBIT,Coffee\n"
    )

    first = parsers.parse_ally_csv(text, account_id="acct")
    replay = parsers.parse_ally_csv(text, account_id="acct")
    identifiers = [row.source_id for row in first.transactions]

    assert len(set(identifiers)) == 2
    assert identifiers[0].endswith(":1")
    assert identifiers[1].endswith(":2")
    assert identifiers == [row.source_id for row in replay.transactions]


# --------------------------------------------------------------------------
# format sniffing
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "text,expected",
    [(OFX, "ofx"), (CITI_CSV, "citi-csv"), (ALLY_CSV, "ally-csv"),
     ("", "unknown"), ("just some words\n", "unknown")],
)
def test_sniff_identifies_formats(text, expected):
    assert parsers.sniff(text) == expected


def test_sniff_ignores_the_file_extension():
    """Downloads get renamed by hand, so content is the only reliable signal."""
    assert parsers.sniff(CITI_CSV) == "citi-csv"


def test_parse_text_dispatches_on_content():
    assert parsers.parse_text(OFX, source="x").format == "ofx"
    assert parsers.parse_text(ALLY_CSV, source="x").format == "ally-csv"


def test_unknown_format_raises_with_the_source_named():
    with pytest.raises(ValueError, match="mystery.dat"):
        parsers.parse_text("nothing recognisable", source="mystery.dat")
