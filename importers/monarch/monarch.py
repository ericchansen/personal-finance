"""Parse and classify a Monarch Money CSV export.

Pure functions over the two files Monarch produces:

    Transactions_*.csv  Date, Merchant, Category, Account, Original Statement,
                        Notes, Amount, Tags, Owner, Reviewed, Id
    Balances_*.csv      Date, Balance, Account

Nothing here talks to a network or a database, so it is cheap to test with
synthetic fixtures.

The important idea in this module is the **trust cutoff**. When an aggregator
loses its connection to an institution it often keeps emitting the last known
balance forever. Those rows look like real history but are not, and importing
them silently corrupts net worth. `detect_trust_cutoff` finds the date after
which an account's balance stops actually moving so the caller can discard the
forward-filled tail.
"""

from __future__ import annotations

import csv
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

# --- Wealthfolio activity types we emit -------------------------------------
DEPOSIT = "DEPOSIT"
WITHDRAWAL = "WITHDRAWAL"
TRANSFER_IN = "TRANSFER_IN"
TRANSFER_OUT = "TRANSFER_OUT"
INTEREST = "INTEREST"
FEE = "FEE"
TAX = "TAX"

# --- Account classification -------------------------------------------------
CASH = "CASH"
CREDIT = "CREDIT"
INVESTMENT = "INVESTMENT"
RETIREMENT = "RETIREMENT"
VEHICLE = "VEHICLE"
LOAN = "LOAN"

# Wealthfolio recognises exactly four account types. Anything else has to be
# modelled through the alternative-assets API instead of as an account.
WF_SECURITIES = "SECURITIES"
WF_CASH = "CASH"
WF_CREDIT_CARD = "CREDIT_CARD"
WF_CRYPTOCURRENCY = "CRYPTOCURRENCY"

# Only CASH and CREDIT_CARD accounts feed Wealthfolio's spending/cash-flow
# reports, which is why credit cards must not be flattened into CASH.
WEALTHFOLIO_ACCOUNT_TYPE = {
    CASH: WF_CASH,
    CREDIT: WF_CREDIT_CARD,
    INVESTMENT: WF_SECURITIES,
    RETIREMENT: WF_SECURITIES,
}

# Types with no account equivalent; these belong to the alternative-assets and
# liability surfaces and are deliberately excluded from account creation.
NON_ACCOUNT_TYPES = frozenset({VEHICLE, LOAN})

CRYPTO_PATTERN = re.compile(r"coinbase|kraken|binance|gemini|\bcrypto\b|ledger|metamask", re.I)

# Ordered: the first pattern that matches wins.
ACCOUNT_TYPE_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (LOAN, re.compile(r"\bmortgage|\bloan\b|\bheloc\b", re.I)),
    (VEHICLE, re.compile(r"\bcivic\b|\bcamry\b|\bvehicle\b|\bcar\b|\btruck\b", re.I)),
    (RETIREMENT, re.compile(r"\bira\b|\b401\s*k\b|\b403\s*b\b|\bretirement\b|\bpension\b|supplemental", re.I)),
    (INVESTMENT, re.compile(r"brokerage|\btod\b|investing|coinbase|robinhood|webull|\bcrypto\b", re.I)),
    (CREDIT, re.compile(r"\bcard\b|\bvisa\b|mastercard|\bamex\b|discover it|freedom|double cash|skymiles|\bcredit\b", re.I)),
    (CASH, re.compile(r"checking|savings|\bvenmo\b|\bcash\b|\bpaypal\b", re.I)),
]

# Monarch categories that represent movement between accounts rather than
# genuine income or spending. These must not count as cash flow.
TRANSFER_CATEGORIES = {
    "transfer",
    "credit card payment",
    "balance adjustments",
}

CATEGORY_ACTIVITY_OVERRIDES = {
    "interest": INTEREST,
    "financial fees": FEE,
    "taxes": TAX,
}

# An account with no activity for longer than this before the export stopped
# carrying real data is treated as closed.
CLOSED_AFTER_DAYS = 180


def _norm(value: str | None) -> str:
    return (value or "").strip()


def _parse_date(value: str) -> date | None:
    text = _norm(value)
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _parse_amount(value: str) -> Decimal | None:
    text = _norm(value).replace("$", "").replace(",", "")
    if not text:
        return None
    negative = text.startswith("(") and text.endswith(")")
    if negative:
        text = text[1:-1]
    try:
        amount = Decimal(text)
    except InvalidOperation:
        return None
    return -amount if negative else amount


@dataclass(frozen=True)
class Transaction:
    date: date
    merchant: str
    category: str
    account: str
    amount: Decimal
    notes: str = ""
    tags: str = ""
    owner: str = ""
    source_id: str = ""

    @property
    def is_transfer(self) -> bool:
        return self.category.strip().lower() in TRANSFER_CATEGORIES

    @property
    def activity_type(self) -> str:
        """Type implied by the category and sign, ignoring the account.

        Use :func:`resolve_activity_type` when the destination account type is
        known; credit cards reject several of these.
        """
        override = CATEGORY_ACTIVITY_OVERRIDES.get(self.category.strip().lower())
        if override is not None:
            return override
        if self.is_transfer:
            return TRANSFER_IN if self.amount > 0 else TRANSFER_OUT
        return DEPOSIT if self.amount > 0 else WITHDRAWAL


# Wealthfolio refuses several activity types on credit-card accounts, because a
# card is a liability rather than a cash pot. Verified against a live server:
# DEPOSIT, TRANSFER_OUT and TAX are rejected; WITHDRAWAL, CREDIT, TRANSFER_IN,
# INTEREST and FEE are accepted.
CREDIT_CARD_SUBSTITUTIONS = {
    DEPOSIT: CREDIT,          # refund or statement credit
    TRANSFER_OUT: WITHDRAWAL,  # money leaving a card is a charge
    TAX: WITHDRAWAL,           # TAX is not a valid card activity
}


def resolve_activity_type(txn: Transaction, wf_account_type: str | None) -> str:
    """Activity type valid for the account the transaction will land in.

    The naive mapping is correct for cash accounts but produces types the
    server rejects on credit cards, which silently drops a large share of an
    import when the biggest accounts are cards.
    """
    base = txn.activity_type
    if wf_account_type != WF_CREDIT_CARD:
        return base
    # Interest *charged* on a card is an outflow; the INTEREST type adds cash.
    if base == INTEREST and txn.amount < 0:
        return FEE
    return CREDIT_CARD_SUBSTITUTIONS.get(base, base)


@dataclass(frozen=True)
class BalancePoint:
    date: date
    account: str
    balance: Decimal


@dataclass(frozen=True)
class OpeningBalance:
    """A correction that makes an account's computed balance match reality."""

    account: str
    amount: Decimal          # signed: positive is an inflow
    as_of: date
    target_balance: Decimal  # the known balance we are reconciling to
    activity_sum: Decimal    # net effect of the imported transactions


def compute_opening_balances(
    transactions: list[Transaction],
    balances: list[BalancePoint],
    profiles: dict[str, AccountProfile],
) -> list[OpeningBalance]:
    """Derive the opening balance each account needs to reconcile.

    Importing transactions alone gives an account the *net change* over the
    imported window, not its actual balance, so every account reads low (or
    negative) by whatever it held on day one.

    For each account: ``opening = last trusted balance - sum of transactions up
    to that date``. The balance is taken at the trust cutoff, because anything
    after it is forward-filled padding rather than a real reading.

    The result is dated the day before the account's first transaction. That
    placement matters: an opening balance posted as a deposit counts as income
    in cash-flow reports, so it must sit outside the periods being analysed.
    """
    signed_totals: dict[str, Decimal] = defaultdict(Decimal)
    earliest: dict[str, date] = {}

    cutoffs = {
        name: (profile.trust_cutoff or profile.last_balance)
        for name, profile in profiles.items()
    }

    for txn in transactions:
        cutoff = cutoffs.get(txn.account)
        if cutoff is not None and txn.date > cutoff:
            continue
        signed_totals[txn.account] += txn.amount
        if txn.account not in earliest or txn.date < earliest[txn.account]:
            earliest[txn.account] = txn.date

    latest_balance: dict[str, BalancePoint] = {}
    for point in sorted(balances, key=lambda p: p.date):
        cutoff = cutoffs.get(point.account)
        if cutoff is not None and point.date > cutoff:
            continue
        latest_balance[point.account] = point

    openings: list[OpeningBalance] = []
    for name, profile in profiles.items():
        if wealthfolio_account_type(profile) is None:
            continue
        point = latest_balance.get(name)
        if point is None:
            continue

        activity_sum = signed_totals.get(name, Decimal("0"))
        opening = point.balance - activity_sum
        if abs(opening) < Decimal("0.01"):
            continue

        first = earliest.get(name)
        as_of = (first - timedelta(days=1)) if first else point.date
        openings.append(
            OpeningBalance(
                account=name,
                amount=opening,
                as_of=as_of,
                target_balance=point.balance,
                activity_sum=activity_sum,
            )
        )

    return sorted(openings, key=lambda o: o.account)


def opening_activity_type(opening: OpeningBalance, wf_account_type: str) -> str:
    """Activity type that moves an account toward its target balance.

    An inflow raises a cash balance and pays down a card; an outflow does the
    reverse. Credit cards reject DEPOSIT, so an inflow there is a CREDIT.
    """
    if opening.amount > 0:
        return CREDIT if wf_account_type == WF_CREDIT_CARD else DEPOSIT
    return WITHDRAWAL


@dataclass
class AccountProfile:
    """What we can infer about one Monarch account."""

    name: str
    account_type: str = CASH
    first_txn: date | None = None
    last_txn: date | None = None
    txn_count: int = 0
    first_balance: date | None = None
    last_balance: date | None = None
    balance_count: int = 0
    trust_cutoff: date | None = None
    stale_balance_rows: int = 0
    owners: set[str] = field(default_factory=set)
    # Latest date at which the export as a whole still held real data. Set by
    # build_profiles so closure can be judged against live data, not against
    # forward-filled padding.
    data_cutoff: date | None = None
    type_overridden: bool = False

    @property
    def is_closed(self) -> bool:
        """True when activity stopped well before the export stopped being real.

        Judged against ``data_cutoff`` rather than the last balance row: a dead
        aggregator connection forward-fills balances to the export date, so
        comparing against that would mark every account closed.
        """
        if self.last_txn is None or self.data_cutoff is None:
            return False
        return (self.data_cutoff - self.last_txn).days > CLOSED_AFTER_DAYS

    @property
    def dormant_days(self) -> int:
        if self.last_txn is None or self.data_cutoff is None:
            return 0
        return max(0, (self.data_cutoff - self.last_txn).days)

    @property
    def needs_review(self) -> bool:
        """Balance-only account whose data went stale long before the export did.

        With no transactions there is no way to tell a closed account from one
        whose aggregator connection simply died. Both look identical. Closing
        it automatically risks erasing a real asset from net worth, which is far
        worse than carrying a dead account, so these are surfaced for a human to
        confirm instead of being decided here.
        """
        if self.txn_count > 0 or self.data_cutoff is None:
            return False
        last_known = self.trust_cutoff or self.last_balance
        if last_known is None:
            return False
        return (self.data_cutoff - last_known).days > CLOSED_AFTER_DAYS


def classify_account(name: str) -> str:
    for account_type, pattern in ACCOUNT_TYPE_PATTERNS:
        if pattern.search(name):
            return account_type
    return CASH


def read_transactions(path: Path) -> list[Transaction]:
    rows: list[Transaction] = []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for raw in csv.DictReader(handle):
            when = _parse_date(raw.get("Date", ""))
            amount = _parse_amount(raw.get("Amount", ""))
            if when is None or amount is None:
                continue
            rows.append(
                Transaction(
                    date=when,
                    merchant=_norm(raw.get("Merchant")),
                    category=_norm(raw.get("Category")),
                    account=_norm(raw.get("Account")),
                    amount=amount,
                    notes=_norm(raw.get("Notes")),
                    tags=_norm(raw.get("Tags")),
                    owner=_norm(raw.get("Owner")),
                    source_id=_norm(raw.get("Id")),
                )
            )
    return rows


def read_balances(path: Path) -> list[BalancePoint]:
    rows: list[BalancePoint] = []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for raw in csv.DictReader(handle):
            when = _parse_date(raw.get("Date", ""))
            amount = _parse_amount(raw.get("Balance", ""))
            if when is None or amount is None:
                continue
            rows.append(
                BalancePoint(date=when, account=_norm(raw.get("Account")), balance=amount)
            )
    return rows


def detect_trust_cutoff(
    points: list[BalancePoint], min_flat_run: int = 30
) -> tuple[date | None, int]:
    """Find the last date an account's balance genuinely moved.

    Returns ``(cutoff, stale_row_count)``. A trailing run of at least
    ``min_flat_run`` identical balances is treated as forward-filled padding
    from a dead connection, and the cutoff is the last date *before* that run.

    A cutoff of ``None`` means every point looks live and all of them can be
    trusted.

    Deliberately conservative. Forward-filling repeats the last real balance,
    so the final genuine reading is indistinguishable from the padding that
    follows it. Rather than guess, the cutoff falls on the last date the
    balance actually *changed*. That discards one real data point in exchange
    for never admitting a fabricated one, which is the right trade when the
    output feeds a net-worth figure.
    """
    if not points:
        return None, 0

    ordered = sorted(points, key=lambda p: p.date)
    final = ordered[-1].balance

    flat_from = len(ordered)
    for index in range(len(ordered) - 1, -1, -1):
        if ordered[index].balance != final:
            break
        flat_from = index

    stale_count = len(ordered) - flat_from
    if stale_count < min_flat_run or flat_from == 0:
        return None, 0

    return ordered[flat_from - 1].date, stale_count


def build_profiles(
    transactions: list[Transaction],
    balances: list[BalancePoint],
    overrides: dict[str, str] | None = None,
) -> dict[str, AccountProfile]:
    """Summarise every account seen in either file.

    ``overrides`` maps an account name to a forced account type, for accounts
    whose name carries no clue about what they are — an employer-named 401(k)
    being the usual case. Because those names are personal data, the override
    map belongs in the external data directory, never in this repository.
    """
    profiles: dict[str, AccountProfile] = {}
    overrides = {k.strip().lower(): v for k, v in (overrides or {}).items()}

    by_account_txn: dict[str, list[Transaction]] = defaultdict(list)
    for txn in transactions:
        by_account_txn[txn.account].append(txn)

    by_account_bal: dict[str, list[BalancePoint]] = defaultdict(list)
    for point in balances:
        by_account_bal[point.account].append(point)

    for name in sorted(set(by_account_txn) | set(by_account_bal)):
        forced = overrides.get(name.strip().lower())
        profile = AccountProfile(
            name=name,
            account_type=forced or classify_account(name),
            type_overridden=forced is not None,
        )

        txns = by_account_txn.get(name, [])
        if txns:
            dates = sorted(t.date for t in txns)
            profile.first_txn, profile.last_txn = dates[0], dates[-1]
            profile.txn_count = len(txns)
            profile.owners = {t.owner for t in txns if t.owner}

        points = by_account_bal.get(name, [])
        if points:
            dates = sorted(p.date for p in points)
            profile.first_balance, profile.last_balance = dates[0], dates[-1]
            profile.balance_count = len(points)
            profile.trust_cutoff, profile.stale_balance_rows = detect_trust_cutoff(points)

        profiles[name] = profile

    data_cutoff = infer_data_cutoff(profiles)
    for profile in profiles.values():
        profile.data_cutoff = data_cutoff

    return profiles


def infer_data_cutoff(profiles: dict[str, AccountProfile]) -> date | None:
    """The latest date the export as a whole still contained real data.

    Taken as the newest transaction across all accounts: transactions are never
    forward-filled, so the most recent one marks where genuine history ends.
    """
    dates = [p.last_txn for p in profiles.values() if p.last_txn is not None]
    return max(dates) if dates else None


def trusted_balances(
    balances: list[BalancePoint], profiles: dict[str, AccountProfile]
) -> list[BalancePoint]:
    """Drop balance points that fall after their account's trust cutoff."""
    kept: list[BalancePoint] = []
    for point in balances:
        profile = profiles.get(point.account)
        cutoff = profile.trust_cutoff if profile else None
        if cutoff is None or point.date <= cutoff:
            kept.append(point)
    return kept


def category_summary(transactions: list[Transaction]) -> list[tuple[str, int]]:
    return Counter(t.category for t in transactions if t.category).most_common()


def find_transfer_pairs(
    transactions: list[Transaction], max_days: int = 5
) -> list[tuple[Transaction, Transaction]]:
    """Match each outgoing transfer to the incoming side in another account.

    Aggregator exports record both legs of an internal transfer independently.
    Left unlinked, one leg counts as spending and the other as income, which
    inflates both sides of a cash-flow report. Wealthfolio nets them out only
    once the two activities are linked as a pair.

    Returns ``(outflow, inflow)`` tuples. Matching requires the same magnitude,
    opposite signs, different accounts, and dates within ``max_days`` — the two
    legs rarely post on the same day. Each activity is used at most once, and
    the nearest date wins so repeated equal transfers pair sensibly.
    """
    outflows = [t for t in transactions if t.is_transfer and t.amount < 0]
    inflows = [t for t in transactions if t.is_transfer and t.amount > 0]

    by_amount: dict[Decimal, list[Transaction]] = defaultdict(list)
    for txn in inflows:
        by_amount[abs(txn.amount)].append(txn)

    used: set[int] = set()
    pairs: list[tuple[Transaction, Transaction]] = []

    # Deterministic order so a rerun produces the same pairing.
    for out in sorted(outflows, key=lambda t: (t.date, t.source_id)):
        candidates = [
            txn
            for txn in by_amount.get(abs(out.amount), [])
            if id(txn) not in used
            and txn.account != out.account
            and abs((txn.date - out.date).days) <= max_days
        ]
        if not candidates:
            continue
        best = min(candidates, key=lambda t: (abs((t.date - out.date).days), t.source_id))
        used.add(id(best))
        pairs.append((out, best))

    return pairs


def find_unpaired_transfers(
    transactions: list[Transaction], max_days: int = 5
) -> list[Transaction]:
    """Transfer-category rows with no matching leg in another tracked account.

    These are real flows that cross the tracked-account boundary — paying a
    friend over Venmo, or moving cash to an institution absent from the export.
    They cannot be paired, and left unmarked Wealthfolio reports them as
    incomplete transfers and mis-attributes the flow when computing returns.

    Marking them external is the documented resolution, and it says something
    different from "unmatched": it asserts the money genuinely left or entered
    the portfolio.
    """
    pairs = find_transfer_pairs(transactions, max_days=max_days)
    paired = {id(txn) for pair in pairs for txn in pair}
    return [t for t in transactions if t.is_transfer and id(t) not in paired]


def wealthfolio_account_type(profile: AccountProfile) -> str | None:
    """Map an inferred type onto one of Wealthfolio's four account types.

    Returns ``None`` for things Wealthfolio does not model as an account —
    vehicles and loans — so callers route them to the alternative-assets and
    liability surfaces instead of silently creating a wrong-typed account.
    """
    if profile.account_type in NON_ACCOUNT_TYPES:
        return None
    if profile.account_type == INVESTMENT and CRYPTO_PATTERN.search(profile.name):
        return WF_CRYPTOCURRENCY
    return WEALTHFOLIO_ACCOUNT_TYPE.get(profile.account_type, WF_SECURITIES)


# Retirement accounts map to SECURITIES like any other brokerage, so the group
# label is the only thing preserving the distinction in the UI.
DEFAULT_GROUPS = {
    CASH: "Cash",
    CREDIT: "Credit Cards",
    INVESTMENT: "Investments",
    RETIREMENT: "Retirement",
}


def default_group(profile: AccountProfile) -> str | None:
    if profile.account_type == INVESTMENT and CRYPTO_PATTERN.search(profile.name):
        return "Crypto"
    return DEFAULT_GROUPS.get(profile.account_type)
