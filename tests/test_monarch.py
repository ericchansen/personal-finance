"""Tests for the Monarch export parser.

All fixture data here is synthetic. Never copy rows from a real export into
this repository.
"""

from __future__ import annotations

import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "importers" / "monarch"))

import monarch  # noqa: E402


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------

TRANSACTIONS_CSV = """Date,Merchant,Category,Account,Original Statement,Notes,Amount,Tags,Owner,Reviewed,Id
2025-01-06,Corner Market,Groceries,Everyday Checking,CORNER MARKET #12,,-54.20,,alex,true,tx-001
2025-01-07,Employer Inc,Paychecks,Everyday Checking,DIRECT DEP,,2500.00,,alex,true,tx-002
2025-01-08,Card Payment,Credit Card Payment,Everyday Checking,PAYMENT THANK YOU,,-300.00,,alex,true,tx-003
2025-01-08,Card Payment,Credit Card Payment,Rewards Card,PAYMENT RECEIVED,,300.00,,alex,true,tx-004
2025-01-09,Bank,Interest,Rainy Day Savings,INTEREST PAID,,3.11,,alex,true,tx-005
2025-01-10,Bank,Financial Fees,Everyday Checking,MONTHLY FEE,,-5.00,,alex,true,tx-006
2025-01-11,State,Taxes,Everyday Checking,TAX PAYMENT,,-120.00,,alex,true,tx-007
2025-01-12,Coffee Bar,Restaurants,Rewards Card,COFFEE BAR,,-6.75,,jordan,false,tx-008
"""

# Rainy Day Savings goes flat after 2025-02-10 — a dead connection.
def _balances_csv() -> str:
    rows = ["Date,Balance,Account"]
    for day in range(1, 29):
        rows.append(f"2025-02-{day:02d},{1000 + day}.00,Everyday Checking")
    for day in range(1, 11):
        rows.append(f"2025-02-{day:02d},{5000 + day}.00,Rainy Day Savings")
    for day in range(11, 29):
        rows.append(f"2025-02-{day:02d},5010.00,Rainy Day Savings")
    return "\n".join(rows) + "\n"


@pytest.fixture
def txn_file(tmp_path: Path) -> Path:
    path = tmp_path / "Transactions_test.csv"
    path.write_text(TRANSACTIONS_CSV, encoding="utf-8")
    return path


@pytest.fixture
def bal_file(tmp_path: Path) -> Path:
    path = tmp_path / "Balances_test.csv"
    path.write_text(_balances_csv(), encoding="utf-8")
    return path


# --------------------------------------------------------------------------
# parsing
# --------------------------------------------------------------------------

def test_reads_all_transactions(txn_file: Path):
    txns = monarch.read_transactions(txn_file)
    assert len(txns) == 8
    assert txns[0].merchant == "Corner Market"
    assert txns[0].amount == Decimal("-54.20")
    assert txns[0].source_id == "tx-001"


def test_skips_unparseable_rows(tmp_path: Path):
    path = tmp_path / "bad.csv"
    path.write_text(
        "Date,Merchant,Category,Account,Original Statement,Notes,Amount,Tags,Owner,Reviewed,Id\n"
        "not-a-date,X,Groceries,A,,,-1.00,,,,1\n"
        "2025-01-01,Y,Groceries,A,,,not-a-number,,,,2\n"
        "2025-01-02,Z,Groceries,A,,,-2.00,,,,3\n",
        encoding="utf-8",
    )
    assert len(monarch.read_transactions(path)) == 1


@pytest.mark.parametrize(
    "raw,expected",
    [("-54.20", Decimal("-54.20")), ("$1,234.56", Decimal("1234.56")),
     ("(75.00)", Decimal("-75.00")), ("", None), ("abc", None)],
)
def test_amount_parsing(raw, expected):
    assert monarch._parse_amount(raw) == expected


# --------------------------------------------------------------------------
# activity mapping
# --------------------------------------------------------------------------

def test_activity_type_mapping(txn_file: Path):
    by_id = {t.source_id: t for t in monarch.read_transactions(txn_file)}
    assert by_id["tx-001"].activity_type == monarch.WITHDRAWAL
    assert by_id["tx-002"].activity_type == monarch.DEPOSIT
    assert by_id["tx-005"].activity_type == monarch.INTEREST
    assert by_id["tx-006"].activity_type == monarch.FEE
    assert by_id["tx-007"].activity_type == monarch.TAX


def test_transfers_are_not_income_or_spending(txn_file: Path):
    """Credit card payments move money between accounts and must not count as
    cash flow, or spending is double counted."""
    by_id = {t.source_id: t for t in monarch.read_transactions(txn_file)}
    assert by_id["tx-003"].activity_type == monarch.TRANSFER_OUT
    assert by_id["tx-004"].activity_type == monarch.TRANSFER_IN
    assert by_id["tx-003"].is_transfer and by_id["tx-004"].is_transfer
    assert not by_id["tx-001"].is_transfer


# --------------------------------------------------------------------------
# account classification
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "name,expected",
    [
        ("Everyday Checking", monarch.CASH),
        ("Rainy Day Savings", monarch.CASH),
        ("Household Venmo", monarch.CASH),
        ("Rewards Card", monarch.CREDIT),
        ("Cashback Visa (...1234)", monarch.CREDIT),
        ("Sample Discover it", monarch.CREDIT),
        ("Roth IRA", monarch.RETIREMENT),
        ("Example 401k", monarch.RETIREMENT),
        ("Sample 403b Plan", monarch.RETIREMENT),
        ("Brokerage General Investing", monarch.INVESTMENT),
        ("Individual TOD", monarch.INVESTMENT),
        ("Coinbase", monarch.INVESTMENT),
        ("Fictional Example Car", monarch.VEHICLE),
        ("Maple Street Mortgage", monarch.LOAN),
    ],
)
def test_classify_account(name, expected):
    assert monarch.classify_account(name) == expected


def test_loan_wins_over_credit_for_mortgage():
    """'Mortgage' must not be swallowed by the credit-card patterns."""
    assert monarch.classify_account("Mortgage Card Services") == monarch.LOAN


def test_overrides_force_account_type(txn_file: Path, bal_file: Path):
    """Employer-named retirement plans carry no clue in the name."""
    txns = monarch.read_transactions(txn_file)
    bals = monarch.read_balances(bal_file)

    plain = monarch.build_profiles(txns, bals)
    assert plain["Everyday Checking"].account_type == monarch.CASH

    forced = monarch.build_profiles(
        txns, bals, overrides={"everyday checking": monarch.RETIREMENT}
    )
    assert forced["Everyday Checking"].account_type == monarch.RETIREMENT
    assert forced["Everyday Checking"].type_overridden is True


# --------------------------------------------------------------------------
# trust cutoff -- the invariant that protects net worth
# --------------------------------------------------------------------------

def test_detects_forward_filled_tail(bal_file: Path):
    """The fill value equals the last real balance, so the boundary is
    genuinely ambiguous. Detection resolves it conservatively: the cutoff is
    the last date the balance *changed* (Feb 9), sacrificing one real reading
    rather than admitting any fabricated ones."""
    points = [p for p in monarch.read_balances(bal_file) if p.account == "Rainy Day Savings"]
    cutoff, stale = monarch.detect_trust_cutoff(points, min_flat_run=5)
    assert cutoff == date(2025, 2, 9)
    assert stale == 19


def test_live_account_has_no_cutoff(bal_file: Path):
    points = [p for p in monarch.read_balances(bal_file) if p.account == "Everyday Checking"]
    cutoff, stale = monarch.detect_trust_cutoff(points, min_flat_run=5)
    assert cutoff is None and stale == 0


def test_short_flat_run_is_not_treated_as_dead():
    """A balance that simply did not move for a few days is still real."""
    points = [
        monarch.BalancePoint(date(2025, 3, d), "A", Decimal("100.00")) for d in range(1, 5)
    ]
    assert monarch.detect_trust_cutoff(points, min_flat_run=30) == (None, 0)


def test_empty_input_is_safe():
    assert monarch.detect_trust_cutoff([]) == (None, 0)


def test_trusted_balances_drops_only_stale_rows(txn_file: Path, bal_file: Path):
    txns = monarch.read_transactions(txn_file)
    bals = monarch.read_balances(bal_file)
    profiles = monarch.build_profiles(txns, bals)
    profiles["Rainy Day Savings"].trust_cutoff = date(2025, 2, 10)

    kept = monarch.trusted_balances(bals, profiles)
    savings = [p for p in kept if p.account == "Rainy Day Savings"]
    checking = [p for p in kept if p.account == "Everyday Checking"]

    assert len(savings) == 10
    assert max(p.date for p in savings) == date(2025, 2, 10)
    assert len(checking) == 28  # untouched


# --------------------------------------------------------------------------
# closure detection
# --------------------------------------------------------------------------

def test_active_account_not_marked_closed(txn_file: Path, bal_file: Path):
    """Regression: closure was judged against forward-filled balance dates,
    which marked every account closed."""
    profiles = monarch.build_profiles(
        monarch.read_transactions(txn_file), monarch.read_balances(bal_file)
    )
    assert profiles["Everyday Checking"].is_closed is False
    assert profiles["Rewards Card"].is_closed is False


def test_dormant_account_marked_closed():
    txns = [
        monarch.Transaction(date(2023, 1, 5), "Old", "Shopping", "Retired Card", Decimal("-10")),
        monarch.Transaction(date(2025, 1, 5), "New", "Shopping", "Active Card", Decimal("-10")),
    ]
    profiles = monarch.build_profiles(txns, [])
    assert profiles["Retired Card"].is_closed is True
    assert profiles["Active Card"].is_closed is False


def test_data_cutoff_is_newest_transaction(txn_file: Path, bal_file: Path):
    profiles = monarch.build_profiles(
        monarch.read_transactions(txn_file), monarch.read_balances(bal_file)
    )
    assert profiles["Everyday Checking"].data_cutoff == date(2025, 1, 12)


def test_balance_only_account_is_not_closed():
    """No transactions at all means we cannot conclude it is closed."""
    points = [monarch.BalancePoint(date(2025, 2, d), "Holdings", Decimal("1.00")) for d in range(1, 5)]
    profiles = monarch.build_profiles([], points)
    assert profiles["Holdings"].is_closed is False


def test_stale_balance_only_account_flagged_for_review():
    """A dead connection and a closed account look identical when there are no
    transactions, so flag rather than decide."""
    txns = [monarch.Transaction(date(2025, 12, 1), "M", "Groceries", "Checking", Decimal("-5"))]
    points = [
        monarch.BalancePoint(date(2024, 1, d), "Old Holdings", Decimal(str(100 + d)))
        for d in range(1, 6)
    ]
    profiles = monarch.build_profiles(txns, points)
    assert profiles["Old Holdings"].needs_review is True
    assert profiles["Old Holdings"].is_closed is False


def test_current_balance_only_account_not_flagged():
    txns = [monarch.Transaction(date(2025, 12, 1), "M", "Groceries", "Checking", Decimal("-5"))]
    points = [
        monarch.BalancePoint(date(2025, 11, d), "Live Holdings", Decimal(str(100 + d)))
        for d in range(1, 6)
    ]
    profiles = monarch.build_profiles(txns, points)
    assert profiles["Live Holdings"].needs_review is False


def test_account_with_transactions_never_needs_review(txn_file: Path, bal_file: Path):
    profiles = monarch.build_profiles(
        monarch.read_transactions(txn_file), monarch.read_balances(bal_file)
    )
    assert profiles["Everyday Checking"].needs_review is False


# --------------------------------------------------------------------------
# mapping onto Wealthfolio's four account types
# --------------------------------------------------------------------------

def _profile(name: str, account_type: str) -> monarch.AccountProfile:
    return monarch.AccountProfile(name=name, account_type=account_type)


@pytest.mark.parametrize(
    "name,inferred,expected",
    [
        ("Everyday Checking", monarch.CASH, monarch.WF_CASH),
        ("Rewards Card", monarch.CREDIT, monarch.WF_CREDIT_CARD),
        ("Roth IRA", monarch.RETIREMENT, monarch.WF_SECURITIES),
        ("Brokerage", monarch.INVESTMENT, monarch.WF_SECURITIES),
        ("Coinbase", monarch.INVESTMENT, monarch.WF_CRYPTOCURRENCY),
    ],
)
def test_wealthfolio_account_type(name, inferred, expected):
    assert monarch.wealthfolio_account_type(_profile(name, inferred)) == expected


@pytest.mark.parametrize("inferred", [monarch.VEHICLE, monarch.LOAN])
def test_non_account_types_return_none(inferred):
    """Vehicles and loans are alternative assets / liabilities, not accounts.
    Returning None stops them being created as wrong-typed accounts."""
    assert monarch.wealthfolio_account_type(_profile("Something", inferred)) is None


def test_credit_cards_stay_credit_not_cash():
    """Wealthfolio's spending reports only include CASH and CREDIT_CARD, and
    collapsing cards into CASH would break liability handling."""
    assert monarch.wealthfolio_account_type(_profile("Cashback Visa", monarch.CREDIT)) == (
        monarch.WF_CREDIT_CARD
    )


# --------------------------------------------------------------------------
# account-type-aware activity resolution
#
# Regression guard: the server rejects DEPOSIT, TRANSFER_OUT and TAX on credit
# card accounts. Sending them anyway silently dropped most of an import,
# because rejections come back in the response body rather than as an error.
# --------------------------------------------------------------------------

def _txn(amount: str, category: str = "Shopping") -> monarch.Transaction:
    return monarch.Transaction(
        date=date(2025, 6, 1), merchant="M", category=category,
        account="A", amount=Decimal(amount),
    )


@pytest.mark.parametrize(
    "amount,category,expected",
    [
        ("-25.00", "Shopping", monarch.WITHDRAWAL),           # a charge
        ("25.00", "Shopping", monarch.CREDIT),                # refund, not DEPOSIT
        ("300.00", "Credit Card Payment", monarch.TRANSFER_IN),
        ("-300.00", "Credit Card Payment", monarch.WITHDRAWAL),  # not TRANSFER_OUT
        ("-40.00", "Taxes", monarch.WITHDRAWAL),              # TAX is invalid here
        ("-9.00", "Financial Fees", monarch.FEE),
        ("-3.00", "Interest", monarch.FEE),                   # interest charged
        ("3.00", "Interest", monarch.INTEREST),               # interest refunded
    ],
)
def test_credit_card_activity_types_are_supported(amount, category, expected):
    assert monarch.resolve_activity_type(_txn(amount, category), monarch.WF_CREDIT_CARD) == expected


@pytest.mark.parametrize(
    "amount,category,expected",
    [
        ("-25.00", "Shopping", monarch.WITHDRAWAL),
        ("2500.00", "Paychecks", monarch.DEPOSIT),
        ("-300.00", "Credit Card Payment", monarch.TRANSFER_OUT),
        ("-40.00", "Taxes", monarch.TAX),
        ("3.11", "Interest", monarch.INTEREST),
    ],
)
def test_cash_accounts_keep_natural_types(amount, category, expected):
    assert monarch.resolve_activity_type(_txn(amount, category), monarch.WF_CASH) == expected


def test_unsupported_types_never_reach_credit_cards():
    """Whatever the input, a card must never receive a rejected type."""
    forbidden = {monarch.DEPOSIT, monarch.TRANSFER_OUT, monarch.TAX}
    categories = ["Shopping", "Paychecks", "Credit Card Payment", "Transfer",
                  "Taxes", "Interest", "Financial Fees", "Groceries"]
    for category in categories:
        for amount in ("-10.00", "10.00"):
            resolved = monarch.resolve_activity_type(_txn(amount, category), monarch.WF_CREDIT_CARD)
            assert resolved not in forbidden, f"{category} {amount} -> {resolved}"


# --------------------------------------------------------------------------
# transfer pairing -- stops internal moves counting as income and spending
# --------------------------------------------------------------------------

def _transfer(day: int, account: str, amount: str, tid: str) -> monarch.Transaction:
    return monarch.Transaction(
        date=date(2025, 5, day), merchant="Transfer", category="Transfer",
        account=account, amount=Decimal(amount), source_id=tid,
    )


def test_pairs_matching_legs_across_accounts():
    txns = [_transfer(1, "Checking", "-500.00", "a"), _transfer(1, "Savings", "500.00", "b")]
    pairs = monarch.find_transfer_pairs(txns)
    assert len(pairs) == 1
    out, inn = pairs[0]
    assert (out.source_id, inn.source_id) == ("a", "b")


def test_pairs_legs_that_post_on_different_days():
    txns = [_transfer(1, "Checking", "-500.00", "a"), _transfer(3, "Savings", "500.00", "b")]
    assert len(monarch.find_transfer_pairs(txns)) == 1


def test_ignores_legs_too_far_apart():
    txns = [_transfer(1, "Checking", "-500.00", "a"), _transfer(20, "Savings", "500.00", "b")]
    assert monarch.find_transfer_pairs(txns) == []


def test_never_pairs_within_one_account():
    """A move inside a single account is not an internal transfer."""
    txns = [_transfer(1, "Checking", "-500.00", "a"), _transfer(1, "Checking", "500.00", "b")]
    assert monarch.find_transfer_pairs(txns) == []


def test_ignores_non_transfer_categories():
    txns = [
        monarch.Transaction(date(2025, 5, 1), "Shop", "Shopping", "Checking", Decimal("-500"), source_id="a"),
        monarch.Transaction(date(2025, 5, 1), "Job", "Paychecks", "Savings", Decimal("500"), source_id="b"),
    ]
    assert monarch.find_transfer_pairs(txns) == []


def test_each_activity_pairs_at_most_once():
    """Two identical outflows must not both claim the same inflow."""
    txns = [
        _transfer(1, "Checking", "-500.00", "a"),
        _transfer(1, "Checking", "-500.00", "b"),
        _transfer(1, "Savings", "500.00", "c"),
    ]
    pairs = monarch.find_transfer_pairs(txns)
    assert len(pairs) == 1
    assert len({id(inn) for _, inn in pairs}) == 1


def test_nearest_date_wins():
    txns = [
        _transfer(10, "Checking", "-500.00", "out"),
        _transfer(14, "Savings", "500.00", "far"),
        _transfer(11, "Savings", "500.00", "near"),
    ]
    pairs = monarch.find_transfer_pairs(txns)
    assert pairs[0][1].source_id == "near"


def test_credit_card_payment_pairs_with_checking():
    txns = [
        monarch.Transaction(date(2025, 5, 1), "Pmt", "Credit Card Payment", "Checking", Decimal("-300"), source_id="a"),
        monarch.Transaction(date(2025, 5, 2), "Pmt", "Credit Card Payment", "Rewards Card", Decimal("300"), source_id="b"),
    ]
    assert len(monarch.find_transfer_pairs(txns)) == 1


def test_pairing_is_deterministic():
    txns = [
        _transfer(1, "Checking", "-500.00", "a"),
        _transfer(2, "Savings", "500.00", "b"),
        _transfer(2, "Brokerage", "500.00", "c"),
    ]
    first = [(o.source_id, i.source_id) for o, i in monarch.find_transfer_pairs(txns)]
    second = [(o.source_id, i.source_id) for o, i in monarch.find_transfer_pairs(list(reversed(txns)))]
    assert first == second


# --------------------------------------------------------------------------
# opening balances -- without these every account reads low by its day-one value
# --------------------------------------------------------------------------

def _spend(day: int, account: str, amount: str) -> monarch.Transaction:
    return monarch.Transaction(
        date=date(2025, 3, day), merchant="M", category="Shopping",
        account=account, amount=Decimal(amount), source_id=f"{account}-{day}",
    )


def _bal(day: int, account: str, amount: str) -> monarch.BalancePoint:
    return monarch.BalancePoint(date=date(2025, 3, day), account=account, balance=Decimal(amount))


def test_opening_balance_reconciles_to_known_balance():
    """Spent 30 and ended at 70, so the account opened with 100."""
    txns = [_spend(5, "Checking", "-10.00"), _spend(6, "Checking", "-20.00")]
    bals = [_bal(6, "Checking", "70.00")]
    profiles = monarch.build_profiles(txns, bals)
    opening = monarch.compute_opening_balances(txns, bals, profiles)[0]
    assert opening.amount == Decimal("100.00")
    assert opening.target_balance == Decimal("70.00")
    assert opening.activity_sum == Decimal("-30.00")


def test_opening_balance_is_dated_before_first_transaction():
    """It must sit outside the reporting window or it counts as income."""
    txns = [_spend(5, "Checking", "-10.00")]
    bals = [_bal(6, "Checking", "90.00")]
    profiles = monarch.build_profiles(txns, bals)
    assert monarch.compute_opening_balances(txns, bals, profiles)[0].as_of == date(2025, 3, 4)


def test_opening_balance_can_be_negative():
    """A card that ends further in debt than its charges explain opened in debt."""
    txns = [_spend(5, "Rewards Card", "-40.00")]
    bals = [_bal(6, "Rewards Card", "-100.00")]
    profiles = monarch.build_profiles(txns, bals)
    assert monarch.compute_opening_balances(txns, bals, profiles)[0].amount == Decimal("-60.00")


def test_no_opening_balance_when_already_reconciled():
    txns = [_spend(5, "Checking", "-10.00")]
    bals = [_bal(6, "Checking", "-10.00")]
    profiles = monarch.build_profiles(txns, bals)
    assert monarch.compute_opening_balances(txns, bals, profiles) == []


def test_opening_balance_ignores_untrusted_balances():
    """Forward-filled padding must not become the reconciliation target."""
    txns = [_spend(5, "Checking", "-10.00")]
    bals = [_bal(d, "Checking", "90.00") for d in range(6, 9)]
    bals += [_bal(d, "Checking", "999.00") for d in range(9, 32)]  # dead connection
    profiles = monarch.build_profiles(txns, bals)
    profiles["Checking"].trust_cutoff = date(2025, 3, 8)
    opening = monarch.compute_opening_balances(txns, bals, profiles)[0]
    assert opening.target_balance == Decimal("90.00")


def test_accounts_without_balances_are_skipped():
    txns = [_spend(5, "Checking", "-10.00")]
    profiles = monarch.build_profiles(txns, [])
    assert monarch.compute_opening_balances(txns, [], profiles) == []


def test_non_account_types_get_no_opening_balance():
    """Vehicles and loans are not accounts, so they cannot take an activity."""
    bals = [_bal(6, "Fictional Example Car", "15000.00")]
    profiles = monarch.build_profiles([], bals)
    assert monarch.compute_opening_balances([], bals, profiles) == []


def test_balance_only_account_gets_full_balance_as_opening():
    """Retirement accounts arrive with a balance and no transactions."""
    bals = [_bal(6, "Roth IRA", "50000.00")]
    profiles = monarch.build_profiles([], bals)
    opening = monarch.compute_opening_balances([], bals, profiles)[0]
    assert opening.amount == Decimal("50000.00")
    assert opening.as_of == date(2025, 3, 6)


@pytest.mark.parametrize(
    "amount,wf_type,expected",
    [
        ("100", monarch.WF_CASH, monarch.DEPOSIT),
        ("-100", monarch.WF_CASH, monarch.WITHDRAWAL),
        ("100", monarch.WF_CREDIT_CARD, monarch.CREDIT),      # DEPOSIT is rejected
        ("-100", monarch.WF_CREDIT_CARD, monarch.WITHDRAWAL),
        ("100", monarch.WF_SECURITIES, monarch.DEPOSIT),
    ],
)
def test_opening_activity_type(amount, wf_type, expected):
    opening = monarch.OpeningBalance(
        account="A", amount=Decimal(amount), as_of=date(2025, 1, 1),
        target_balance=Decimal("0"), activity_sum=Decimal("0"),
    )
    assert monarch.opening_activity_type(opening, wf_type) == expected


# --------------------------------------------------------------------------
# external transfers -- the ones that genuinely cross the portfolio boundary
# --------------------------------------------------------------------------

def test_unpaired_transfers_are_reported():
    txns = [
        _transfer(1, "Checking", "-500.00", "paired-out"),
        _transfer(1, "Savings", "500.00", "paired-in"),
        _transfer(2, "Venmo", "-40.00", "to-a-friend"),
    ]
    unpaired = monarch.find_unpaired_transfers(txns)
    assert [t.source_id for t in unpaired] == ["to-a-friend"]


def test_paired_transfers_are_never_external():
    txns = [_transfer(1, "Checking", "-500.00", "a"), _transfer(1, "Savings", "500.00", "b")]
    assert monarch.find_unpaired_transfers(txns) == []


def test_non_transfers_are_not_external_candidates():
    """Ordinary spending is not a boundary crossing."""
    txns = [monarch.Transaction(date(2025, 5, 1), "Shop", "Shopping", "Checking", Decimal("-9"))]
    assert monarch.find_unpaired_transfers(txns) == []


def test_unpaired_and_paired_partition_all_transfers():
    txns = [
        _transfer(1, "Checking", "-500.00", "a"),
        _transfer(1, "Savings", "500.00", "b"),
        _transfer(2, "Venmo", "-40.00", "c"),
        _transfer(3, "Venmo", "-25.00", "d"),
    ]
    pairs = monarch.find_transfer_pairs(txns)
    unpaired = monarch.find_unpaired_transfers(txns)
    assert len(pairs) * 2 + len(unpaired) == sum(1 for t in txns if t.is_transfer)
