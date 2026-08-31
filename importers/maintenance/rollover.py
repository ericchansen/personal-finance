"""Close an account that was rolled over into another one.

A 401k rollover leaves two records of the same money: the old employer plan,
frozen at whatever balance the aggregator last saw, and the new IRA holding the
real positions. Deleting the old account would erase the years it was real, so
instead its balance is transferred out on the day the rollover settled.

The destination's opening entry is re-dated to the same day. Without that the
money leaves one account months before it arrives in the other, and the net
worth chart shows a hole that never existed.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

KEY_PREFIX = "rollover"


@dataclass(frozen=True)
class Rollover:
    source_account_id: str
    source_balance: Decimal
    destination_name: str
    effective_date: str  # ISO date, the day the rollover settled
    currency: str = "USD"


def _timestamp(day: str) -> str:
    """Wealthfolio's activity endpoints reject bare dates."""
    return f"{day}T00:00:00Z"


def plan_close(rollover: Rollover) -> list[dict]:
    """Return the activities that empty the source account.

    An account already at (or below) zero needs no entry; returning an empty
    list keeps the caller's re-run idempotent rather than stacking corrections.
    """
    if rollover.source_balance <= Decimal("0"):
        return []
    return [
        {
            "accountId": rollover.source_account_id,
            "activityType": "TRANSFER_OUT",
            "activityDate": _timestamp(rollover.effective_date),
            "amount": float(rollover.source_balance),
            "currency": rollover.currency,
            "isDraft": False,
            "comment": f"Rolled over to {rollover.destination_name}",
            "idempotencyKey": (
                f"{KEY_PREFIX}:{rollover.source_account_id}:{rollover.effective_date}"
            ),
        }
    ]


def plan_redate(activity: dict, effective_date: str) -> dict | None:
    """Move a destination activity back to the rollover date.

    Returns None when the activity already sits on that date, so a repeated run
    sends nothing.

    Search results carry ``date``; updates expect ``activityDate``. The whole
    record is echoed back because a partial update blanks the omitted fields.
    """
    current = str(activity.get("date") or activity.get("activityDate") or "")
    if current.startswith(effective_date):
        return None
    updated = dict(activity)
    updated.pop("date", None)
    updated["activityDate"] = _timestamp(effective_date)
    return updated
