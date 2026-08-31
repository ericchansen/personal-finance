"""Parse Fidelity's positions and account-history exports.

Fidelity emits two unrelated CSVs, and both are needed:

- **Portfolio_Positions_<date>.csv** — current holdings, one row per position,
  with share counts and prices. This is what makes a retirement balance more
  than undifferentiated cash.
- **Accounts_History.csv** — transactions, capped at one rolling year per
  download, so several files are needed to build any depth of history.

Fidelity's exports are better behaved than most: every row carries both the
account name and the account number, so three files that all arrived named
``Accounts_History.csv`` can still be told apart after the fact. Ally, by
contrast, identifies the account nowhere in the file.

Two quirks matter for anything downstream.

**Not every symbol is a ticker.** Employer plans hold collective investment
trusts, which are identified by a 9-character CUSIP and have no public quote.
Asking a market data provider to price one produces a permanently stale holding,
so :func:`is_cusip` marks them for manual pricing instead.

**The money market fund is cash.** Fidelity sweeps uninvested cash into SPAXX
and reports it as a position, flagged with trailing asterisks. Treating it as a
security would invent a holding that the account does not really have.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

POSITIONS_HEADER = "Account number,Account name"
TRANSACTIONS_HEADER = "Run Date,Account"
ESPP_HEADER = "Transaction Date,Transaction Type"

# Fidelity's own cash-sweep money market funds. Held as a position in the
# export, but spendable cash in every sense that matters.
MONEY_MARKET_SYMBOLS = {"SPAXX", "FDRXX", "FZFXX", "FCASH"}


@dataclass(frozen=True)
class FidelityPosition:
    account_number: str
    account_name: str
    symbol: str
    description: str
    quantity: Decimal
    last_price: Decimal
    current_value: Decimal
    cost_basis: Decimal

    @property
    def is_cash(self) -> bool:
        return self.symbol in MONEY_MARKET_SYMBOLS

    @property
    def needs_manual_price(self) -> bool:
        """CUSIP-identified assets have no public quote to look up."""
        return is_cusip(self.symbol)


@dataclass(frozen=True)
class FidelityTransaction:
    account_number: str
    account_name: str
    run_date: date
    settlement_date: date | None
    action: str
    symbol: str
    description: str
    quantity: Decimal
    price: Decimal
    amount: Decimal


@dataclass(frozen=True)
class EsppContribution:
    """One payroll deduction into an employee stock purchase plan.

    Until the offering period closes this is **cash**, not stock: money
    withheld from pay and held by the plan until the purchase date, when it
    buys shares at a discount. Recording it as a position would invent a
    holding that does not exist yet and would misstate the discount as a gain.
    """

    plan_name: str
    transaction_date: date
    offering_period: str
    amount: Decimal


@dataclass(frozen=True)
class FidelityExport:
    source: str
    positions: list[FidelityPosition] = field(default_factory=list)
    transactions: list[FidelityTransaction] = field(default_factory=list)
    espp: list[EsppContribution] = field(default_factory=list)

    @property
    def accounts(self) -> dict[str, str]:
        """Account number to account name, from whichever table has rows."""
        found: dict[str, str] = {}
        for row in (*self.positions, *self.transactions):
            found.setdefault(row.account_number, row.account_name)
        return dict(sorted(found.items()))

    def value_of(self, account_number: str) -> Decimal:
        return sum(
            (p.current_value for p in self.positions
             if p.account_number == account_number),
            Decimal("0"),
        )

    def funding_needed(self, account_number: str) -> Decimal:
        """Cash required to buy this account's positions and hold its sweep.

        Deliberately not the same as :meth:`value_of`. The export rounds each
        position's current value to the cent, but a buy costs
        ``quantity * price`` in full precision. Funding an account from the
        rounded figure can leave it a fraction of a cent short, which reads as
        a negative balance and is reported as a data error. For example, the
        synthetic product ``3.333 * 10.01`` is ``33.36333`` even when the
        exported value is ``33.36``.
        """
        return sum(
            (
                p.current_value if p.is_cash else p.quantity * p.last_price
                for p in self.positions
                if p.account_number == account_number
            ),
            Decimal("0"),
        )

    def cash_of(self, account_number: str) -> Decimal:
        return sum(
            (p.current_value for p in self.positions
             if p.account_number == account_number and p.is_cash),
            Decimal("0"),
        )


def is_cusip(symbol: str) -> bool:
    """True for a 9-character CUSIP rather than an exchange ticker.

    Employer plan assets are frequently collective trusts with no ticker. The
    distinction is worth drawing because a CUSIP sent to a quote provider comes
    back empty, leaving the holding stuck at its import price forever.
    """
    candidate = symbol.strip().rstrip("*")
    if len(candidate) != 9 or not candidate.isalnum():
        return False
    # A ticker is letters only; a CUSIP always carries at least one digit
    # (the final character is a check digit).
    return any(c.isdigit() for c in candidate)


def _money(value: str | None) -> Decimal:
    """Read Fidelity's ``$1,234.56``/``+$12.34``/``-$0.18`` money format."""
    if value is None:
        return Decimal("0")
    text = value.strip().strip('"').replace("$", "").replace(",", "").replace("+", "")
    if not text or text in {"--", "n/a", "N/A"}:
        return Decimal("0")
    try:
        return Decimal(text)
    except InvalidOperation:
        return Decimal("0")


def _symbol(value: str | None) -> str:
    """Strip Fidelity's trailing asterisk footnote markers."""
    return (value or "").strip().rstrip("*").upper()


def _date(value: str | None) -> date | None:
    text = (value or "").strip().strip('"')
    if not text:
        return None
    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _table(text: str, header: str) -> list[dict]:
    """Return the rows of the one real table in a Fidelity export.

    Both exports bury a CSV table inside a document: blank lines above it and
    several paragraphs of legal disclaimer below. Reading the file as plain CSV
    yields the disclaimer as data, so the table is cut out first and stopped at
    the first row that no longer looks like one.
    """
    start = text.find(header)
    if start < 0:
        return []
    rows: list[dict] = []
    for row in csv.DictReader(io.StringIO(text[start:])):
        # The disclaimer is a single unquoted paragraph, so it parses as a row
        # with one populated field and the rest missing entirely.
        if row.get(header.split(",")[0]) is None:
            break
        if None in row and row.get(header.split(",")[1]) is None:
            break
        first = (row.get(header.split(",")[0]) or "").strip()
        if not first:
            break
        rows.append(row)
    return rows


def parse_positions(text: str, source: str = "") -> list[FidelityPosition]:
    out: list[FidelityPosition] = []
    for row in _table(text, POSITIONS_HEADER):
        symbol = _symbol(row.get("Symbol"))
        if not symbol:
            continue
        out.append(
            FidelityPosition(
                account_number=(row.get("Account number") or "").strip(),
                account_name=(row.get("Account name") or "").strip(),
                symbol=symbol,
                description=(row.get("Description") or "").strip(),
                quantity=_money(row.get("Quantity")),
                last_price=_money(row.get("Last price")),
                current_value=_money(row.get("Current value")),
                cost_basis=_money(row.get("Cost basis total")),
            )
        )
    return out


def parse_transactions(text: str, source: str = "") -> list[FidelityTransaction]:
    out: list[FidelityTransaction] = []
    for row in _table(text, TRANSACTIONS_HEADER):
        run = _date(row.get("Run Date"))
        if run is None:
            continue
        out.append(
            FidelityTransaction(
                account_number=(row.get("Account Number") or "").strip(),
                account_name=(row.get("Account") or "").strip(),
                run_date=run,
                settlement_date=_date(row.get("Settlement Date")),
                action=(row.get("Action") or "").strip(),
                symbol=_symbol(row.get("Symbol")),
                description=(row.get("Description") or "").strip(),
                quantity=_money(row.get("Quantity")),
                price=_money(row.get("Price ($)")),
                amount=_money(row.get("Amount ($)")),
            )
        )
    return out


def looks_like_positions(text: str) -> bool:
    return POSITIONS_HEADER in text[:4000]


def looks_like_transactions(text: str) -> bool:
    return TRANSACTIONS_HEADER in text[:4000]


def looks_like_espp(text: str) -> bool:
    return ESPP_HEADER in text[:4000]


def parse_espp(text: str, source: str = "") -> list[EsppContribution]:
    """Read stock-plan payroll contributions.

    The same table also carries rate changes, whose Quantity is a percentage
    rather than an amount ("15%"). Those describe future intent, not money
    already withheld, so only contributions are returned.
    """
    out: list[EsppContribution] = []
    for row in _table(text, ESPP_HEADER):
        kind = (row.get("Transaction Type") or "").strip().lower()
        if "contribution" not in kind or "change" in kind:
            continue
        when = _date(row.get("Transaction Date"))
        raw = (row.get("Net Proceeds") or row.get("Quantity") or "").strip()
        if when is None or "%" in raw:
            continue
        out.append(
            EsppContribution(
                plan_name=(row.get("Plan Name") or "").strip(),
                transaction_date=when,
                offering_period=(row.get("Offering period") or "").strip(),
                amount=_money(raw),
            )
        )
    return out


def parse_files(paths) -> FidelityExport:
    """Combine any mix of positions and history exports into one view.

    History downloads are capped at a rolling year, so several files routinely
    describe the same account across different windows. Duplicates are dropped
    on identity rather than assumed absent, because re-downloading an
    overlapping range is the normal way to fill a gap.
    """
    positions: list[FidelityPosition] = []
    transactions: list[FidelityTransaction] = []
    espp: list[EsppContribution] = []
    sources: list[str] = []
    for path in paths:
        text = path.read_text(encoding="utf-8-sig")
        sources.append(path.name)
        if looks_like_positions(text):
            positions.extend(parse_positions(text, path.name))
        if looks_like_transactions(text):
            transactions.extend(parse_transactions(text, path.name))
        if looks_like_espp(text):
            espp.extend(parse_espp(text, path.name))

    seen: set[tuple] = set()
    unique: list[FidelityTransaction] = []
    for txn in sorted(transactions, key=lambda t: t.run_date):
        key = (txn.account_number, txn.run_date, txn.action, txn.symbol,
               txn.quantity, txn.amount)
        if key in seen:
            continue
        seen.add(key)
        unique.append(txn)

    return FidelityExport(
        source=", ".join(sources),
        positions=positions,
        transactions=unique,
        espp=sorted(espp, key=lambda e: e.transaction_date),
    )
