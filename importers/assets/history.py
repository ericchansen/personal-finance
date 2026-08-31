"""Build valuation histories for properties and loans.

An alternative holding in Wealthfolio carries a single valuation, so a house
contributes a flat line to the net-worth chart and a mortgage another. Worse,
the asset does not exist at all before its valuation date: a household that
owned a home in 2021 shows Properties at zero until whenever the valuation
happens to be dated, and a five-year change then reads as a rise from nothing.

Both are fixed by writing a *series* of quotes against the asset rather than
one. Loans are exact -- an amortization schedule is arithmetic. Property values
are not, so the only honest thing to do between two known valuations is
interpolate, and say so.

Monthly granularity is deliberate. Daily quotes over five years would be
thousands of rows per asset to describe a number that genuinely changes twelve
times a year at most.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

try:
    from . import loan as loan_math
except ImportError:  # pragma: no cover - direct CLI execution
    import loan as loan_math  # type: ignore


@dataclass(frozen=True)
class Valuation:
    """A value that was actually observed, on the date it was observed."""

    on: date
    value: Decimal


@dataclass(frozen=True)
class LoanPhase:
    """One loan. A refinance ends one phase and begins another."""

    principal: Decimal
    annual_rate: Decimal
    term_months: int
    first_payment: date
    starts: date
    ends: date | None = None
    payoff_amount: Decimal | None = None
    payoff_date: date | None = None


def month_ends(start: date, end: date) -> list[date]:
    """Month boundaries from ``start`` to ``end`` inclusive of both."""
    if end < start:
        return []
    out = [start]
    year, month = start.year, start.month
    while True:
        month += 1
        if month > 12:
            month, year = 1, year + 1
        nxt = date(year, month, 1)
        if nxt >= end:
            break
        out.append(nxt)
    if end != start:
        out.append(end)
    return out


def interpolate(points: list[Valuation], start: date, end: date) -> list[tuple[date, Decimal]]:
    """Monthly values between observations, straight-lined in between.

    A step function would claim the value jumped on the day of an appraisal,
    which is not what happened; a straight line claims steady change, which is
    also not what happened but is the smaller lie and does not invent a moment
    of sudden gain. Before the first observation and after the last, the
    nearest known value is held flat rather than extrapolated.
    """
    if not points:
        return []
    known = sorted(points, key=lambda p: p.on)
    out: list[tuple[date, Decimal]] = []
    for day in month_ends(start, end):
        if day <= known[0].on:
            out.append((day, known[0].value))
            continue
        if day >= known[-1].on:
            out.append((day, known[-1].value))
            continue
        after = next(i for i, p in enumerate(known) if p.on > day)
        lo, hi = known[after - 1], known[after]
        span = Decimal((hi.on - lo.on).days)
        travelled = Decimal((day - lo.on).days)
        value = lo.value + (hi.value - lo.value) * travelled / span
        out.append((day, value.quantize(Decimal("0.01"))))
    return out


def loan_balances(phases: list[LoanPhase], start: date, end: date) -> list[tuple[date, Decimal]]:
    """Monthly outstanding balance across one or more loans.

    Balances **sum**, because a household can genuinely owe on two loans at
    once -- buying the next house before selling the last one is the ordinary
    way it happens, and for a few months both mortgages are real.

    A refinance is not that. It replaces one loan with another, so its phases
    must not overlap: give the old phase an ``ends`` date. Overlapping phases
    for what is really one loan would double-count it.

    Outside every phase the balance is zero, which is what makes a sold house's
    mortgage disappear rather than amortize forever.
    """
    out: list[tuple[date, Decimal]] = []
    for day in month_ends(start, end):
        balance = Decimal("0")
        for phase in phases:
            if day < phase.starts:
                continue
            if phase.ends is not None and day > phase.ends:
                continue
            state = loan_math.balance_as_of(
                phase.principal, phase.annual_rate, phase.term_months,
                phase.first_payment, day,
            )
            phase_balance = state.balance
            if phase.payoff_date == day and phase.payoff_amount is not None:
                phase_balance = phase.payoff_amount
            balance += phase_balance
        out.append((day, balance))
    return out


def owned_window(
    series: list[tuple[date, Decimal]], acquired: date, disposed: date | None
) -> list[tuple[date, Decimal]]:
    """Zero the series outside the period the thing was actually owned.

    A property sold in 2023 must go to zero, not vanish: an absent quote leaves
    the last known value standing, so the house would appear to be owned
    forever.
    """
    out = []
    for day, value in series:
        if day < acquired or (disposed is not None and day > disposed):
            out.append((day, Decimal("0")))
        else:
            out.append((day, value))
    return out


def to_quotes(asset_id: str, series: list[tuple[date, Decimal]], currency: str = "USD") -> list[dict]:
    """Shape a series for ``POST /market-data/quotes/import``.

    That endpoint's ``symbol`` field wants the asset's internal UUID, not a
    ticker; passing anything else fails with a foreign-key violation.
    """
    return [
        {
            "symbol": asset_id,
            "date": day.isoformat(),
            "open": float(value), "high": float(value),
            "low": float(value), "close": float(value),
            "volume": 0, "currency": currency, "dataSource": "MANUAL",
        }
        for day, value in series
    ]
