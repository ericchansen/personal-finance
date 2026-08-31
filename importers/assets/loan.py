"""Amortize a fixed-rate loan.

Used to derive a mortgage's balance today from its origination terms, so a
current figure does not depend on having a recent statement to hand.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_HALF_UP

CENTS = Decimal("0.01")


def _round(value: Decimal) -> Decimal:
    return value.quantize(CENTS, rounding=ROUND_HALF_UP)


def monthly_payment(principal: Decimal, annual_rate: Decimal, term_months: int) -> Decimal:
    """Level principal-and-interest payment for a fully amortizing loan."""
    if term_months <= 0:
        raise ValueError("term_months must be positive")
    if annual_rate == 0:
        return _round(principal / term_months)

    rate = annual_rate / Decimal(12)
    growth = (Decimal(1) + rate) ** term_months
    return _round(principal * rate * growth / (growth - Decimal(1)))


@dataclass(frozen=True)
class LoanState:
    payments_made: int
    balance: Decimal
    principal_paid: Decimal
    interest_paid: Decimal


def payments_elapsed(first_payment: date, as_of: date) -> int:
    """Number of monthly payments due on or before ``as_of``."""
    if as_of < first_payment:
        return 0
    months = (as_of.year - first_payment.year) * 12 + (as_of.month - first_payment.month)
    if as_of.day < first_payment.day:
        months -= 1
    return max(0, months + 1)


def amortize(
    principal: Decimal,
    annual_rate: Decimal,
    term_months: int,
    payments_made: int,
    payment: Decimal | None = None,
) -> LoanState:
    """Roll the loan forward ``payments_made`` months.

    Interest accrues on the outstanding balance each month and the remainder of
    the payment reduces principal, which is why a loan's balance falls slowly at
    first. The final payment is truncated so the balance cannot go below zero.
    """
    if payments_made < 0:
        raise ValueError("payments_made cannot be negative")

    payment = payment or monthly_payment(principal, annual_rate, term_months)
    rate = annual_rate / Decimal(12)

    balance = principal
    interest_total = Decimal("0")
    principal_total = Decimal("0")
    scheduled = min(payments_made, term_months)

    for index in range(scheduled):
        if balance <= 0:
            break
        interest = _round(balance * rate)
        principal_part = payment - interest
        # The level payment is rounded to cents, so it cannot retire the
        # principal exactly. Lenders absorb the difference in the final
        # payment, which is what stops a cent or two lingering at term.
        if index == term_months - 1 or principal_part > balance:
            principal_part = balance
        balance = _round(balance - principal_part)
        interest_total += interest
        principal_total += principal_part

    return LoanState(
        payments_made=scheduled,
        balance=_round(balance),
        principal_paid=_round(principal_total),
        interest_paid=_round(interest_total),
    )


def balance_as_of(
    principal: Decimal,
    annual_rate: Decimal,
    term_months: int,
    first_payment: date,
    as_of: date,
) -> LoanState:
    return amortize(
        principal, annual_rate, term_months, payments_elapsed(first_payment, as_of)
    )
