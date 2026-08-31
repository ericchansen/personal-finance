"""Parse Vanguard's combined holdings-and-transactions export.

Vanguard's Download Center emits a single CSV containing **two tables**, one
after the other, separated by a blank line and a second header row:

    Account Number,Investment Name,Symbol,Shares,Share Price,Total Value
    ...positions...

    Account Number,Trade Date,Settlement Date,Transaction Type,...
    ...transactions...

A normal CSV reader sees the second header as data and produces nonsense, so
the file is split on header rows before either table is read.

This is the only export encountered so far that carries **positions**, which
matters: every other source gives a balance, leaving a retirement account
modelled as undifferentiated cash. Share counts and symbols are what let a
projection reason about asset allocation.

One export covers every account, so unlike Ally there is no risk of losing
track of which file belongs to whom — but the account **numbers** still have to
be mapped to account names, which the file does not contain.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

HOLDINGS_HEADER = "Account Number,Investment Name"
TRANSACTIONS_HEADER = "Account Number,Trade Date"


@dataclass(frozen=True)
class Holding:
    account_number: str
    name: str
    symbol: str
    shares: Decimal
    share_price: Decimal
    total_value: Decimal


@dataclass(frozen=True)
class VanguardTransaction:
    account_number: str
    trade_date: date
    settlement_date: date | None
    kind: str            # Vanguard's own label: Dividend, Reinvestment, Buy, ...
    description: str
    symbol: str
    shares: Decimal
    share_price: Decimal
    net_amount: Decimal


@dataclass(frozen=True)
class VanguardExport:
    source: str
    holdings: list[Holding] = field(default_factory=list)
    transactions: list[VanguardTransaction] = field(default_factory=list)

    @property
    def account_numbers(self) -> list[str]:
        seen = {h.account_number for h in self.holdings}
        seen |= {t.account_number for t in self.transactions}
        return sorted(seen)

    def value_of(self, account_number: str) -> Decimal:
        return sum(
            (h.total_value for h in self.holdings if h.account_number == account_number),
            Decimal("0"),
        )

    def funding_needed(self, account_number: str, cash_symbols: set[str]) -> Decimal:
        """Cash required to buy this account's positions and hold its sweep.

        Deliberately not the same as :meth:`value_of`. The export rounds each
        holding's total value to the cent, but a buy costs
        ``shares * share_price`` in full precision. Funding from the rounded
        figure can leave an account a fraction of a cent short, which reads as
        a negative balance and is reported as a data error.
        """
        return sum(
            (
                h.total_value if h.symbol in cash_symbols else h.shares * h.share_price
                for h in self.holdings
                if h.account_number == account_number
            ),
            Decimal("0"),
        )


def _dec(value: str | None) -> Decimal:
    text = (value or "").strip().replace("$", "").replace(",", "")
    if not text:
        return Decimal("0")
    negative = text.startswith("(") and text.endswith(")")
    if negative:
        text = text[1:-1]
    try:
        amount = Decimal(text)
    except InvalidOperation:
        return Decimal("0")
    return -amount if negative else amount


def _date(value: str | None) -> date | None:
    text = (value or "").strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _split_sections(text: str) -> dict[str, list[str]]:
    """Split the file into its named tables at each header row."""
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in text.splitlines():
        if line.startswith(HOLDINGS_HEADER):
            current = "holdings"
            sections[current] = [line]
        elif line.startswith(TRANSACTIONS_HEADER):
            current = "transactions"
            sections[current] = [line]
        elif current and line.strip():
            sections[current].append(line)
    return sections


def parse(text: str, source: str = "") -> VanguardExport:
    sections = _split_sections(text)

    holdings: list[Holding] = []
    for row in csv.DictReader(io.StringIO("\n".join(sections.get("holdings", [])))):
        number = (row.get("Account Number") or "").strip()
        if not number:
            continue
        holdings.append(
            Holding(
                account_number=number,
                name=(row.get("Investment Name") or "").strip(),
                symbol=(row.get("Symbol") or "").strip(),
                shares=_dec(row.get("Shares")),
                share_price=_dec(row.get("Share Price")),
                total_value=_dec(row.get("Total Value")),
            )
        )

    transactions: list[VanguardTransaction] = []
    for row in csv.DictReader(io.StringIO("\n".join(sections.get("transactions", [])))):
        number = (row.get("Account Number") or "").strip()
        when = _date(row.get("Trade Date"))
        if not number or when is None:
            continue
        transactions.append(
            VanguardTransaction(
                account_number=number,
                trade_date=when,
                settlement_date=_date(row.get("Settlement Date")),
                kind=(row.get("Transaction Type") or "").strip(),
                description=(row.get("Transaction Description") or "").strip(),
                symbol=(row.get("Symbol") or "").strip(),
                shares=_dec(row.get("Shares")),
                share_price=_dec(row.get("Share Price")),
                net_amount=_dec(row.get("Net Amount")),
            )
        )

    return VanguardExport(source=source, holdings=holdings, transactions=transactions)


def parse_file(path: Path) -> VanguardExport:
    return parse(path.read_text(encoding="utf-8-sig", errors="replace"), source=path.name)


def looks_like_vanguard(text: str) -> bool:
    head = text.lstrip()[:400]
    return head.startswith(HOLDINGS_HEADER) or TRANSACTIONS_HEADER in text[:4000]
