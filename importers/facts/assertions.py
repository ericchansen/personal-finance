"""Load balance snapshots and compare them with durable balance assertions."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

try:
    from .schema import AssertionFact, LoadResult, ValidationIssue
except ImportError:  # pragma: no cover - lets ``python cli.py`` work.
    from schema import AssertionFact, LoadResult, ValidationIssue  # type: ignore


@dataclass(frozen=True)
class SnapshotBalance:
    account_id: str
    on: date
    balance: Decimal
    source: str


@dataclass(frozen=True)
class AssertionMismatch:
    account_id: str
    on: date
    expected: Decimal
    actual: Decimal


@dataclass(frozen=True)
class VerificationResult:
    checked: int = 0
    mismatches: tuple[AssertionMismatch, ...] = ()
    missing: tuple[SnapshotBalance, ...] = ()
    errors: tuple[ValidationIssue, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.errors and not self.mismatches and not self.missing


def load_balance_snapshot(path: str | Path) -> tuple[tuple[SnapshotBalance, ...], tuple[ValidationIssue, ...]]:
    """Read a non-mutating JSON balance snapshot.

    The canonical shape is ``{"date": "YYYY-MM-DD", "balances": [...]}``.
    An entry may override ``date`` and must carry ``accountId``, ``balance``,
    and ``source``. Money is converted directly to :class:`Decimal`.
    """
    snapshot_path = Path(path)
    issues: list[ValidationIssue] = []
    try:
        payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return (), (ValidationIssue(str(snapshot_path), f"invalid snapshot: {exc}"),)

    if not isinstance(payload, dict) or not isinstance(payload.get("balances"), list):
        return (), (
            ValidationIssue(str(snapshot_path), "snapshot must contain a balances list"),
        )

    default_date = payload.get("date")
    balances: list[SnapshotBalance] = []
    seen: dict[tuple[str, date], Decimal] = {}
    for index, raw in enumerate(payload["balances"]):
        field = f"balances[{index}]"
        if not isinstance(raw, dict):
            issues.append(ValidationIssue(str(snapshot_path), "balance must be an object", "snapshot", field))
            continue
        account_id = raw.get("accountId")
        source = raw.get("source")
        on = _date(raw.get("date", default_date))
        amount = _decimal(raw.get("balance"))
        if not isinstance(account_id, str) or not account_id.strip():
            issues.append(ValidationIssue(str(snapshot_path), "accountId is required", "snapshot", f"{field}.accountId"))
        if not isinstance(source, str) or not source.strip():
            issues.append(ValidationIssue(str(snapshot_path), "source is required", "snapshot", f"{field}.source"))
        if on is None:
            issues.append(ValidationIssue(str(snapshot_path), "date must be ISO YYYY-MM-DD", "snapshot", f"{field}.date"))
        if amount is None:
            issues.append(ValidationIssue(str(snapshot_path), "balance must be decimal-compatible", "snapshot", f"{field}.balance"))
        if not account_id or not source or on is None or amount is None:
            continue
        key = (account_id, on)
        previous = seen.get(key)
        if previous is not None and previous != amount:
            issues.append(
                ValidationIssue(
                    str(snapshot_path),
                    "conflicting balances for the same account and date",
                    "snapshot",
                    field,
                )
            )
            continue
        seen[key] = amount
        balances.append(SnapshotBalance(account_id, on, amount, source))
    return tuple(balances), tuple(issues)


def verify_assertions(
    facts: LoadResult,
    snapshot: tuple[SnapshotBalance, ...],
    snapshot_errors: tuple[ValidationIssue, ...] = (),
) -> VerificationResult:
    """Compare assertions on dates represented by the supplied snapshot."""
    errors = tuple(facts.errors) + tuple(snapshot_errors)
    if errors:
        return VerificationResult(errors=errors)

    actual = {(item.account_id, item.on): item for item in snapshot}
    snapshot_dates = {item.on for item in snapshot}
    assertions_by_key = {
        (parsed.fact.account_id, parsed.fact.on): parsed.fact
        for parsed in facts.facts
        if isinstance(parsed.fact, AssertionFact) and parsed.fact.on in snapshot_dates
    }
    if snapshot and not assertions_by_key:
        return VerificationResult(
            errors=(
                ValidationIssue(
                    "<snapshot>",
                    "no assertions exist for the snapshot date(s)",
                    "assertion",
                ),
            )
        )
    mismatches: list[AssertionMismatch] = []
    missing: list[SnapshotBalance] = []
    checked = 0
    # A snapshot is source-scoped: SimpleFIN legitimately does not contain a
    # Coinbase screenshot assertion from the same day. Verify every balance
    # the source DID provide rather than requiring it to reproduce assertions
    # established by unrelated sources.
    for key, balance in actual.items():
        assertion = assertions_by_key.get(key)
        if assertion is None:
            missing.append(balance)
            continue
        checked += 1
        if balance.balance != assertion.balance:
            mismatches.append(
                AssertionMismatch(
                    assertion.account_id,
                    assertion.on,
                    assertion.balance,  # type: ignore[arg-type]
                    balance.balance,
                )
            )
    return VerificationResult(checked, tuple(mismatches), tuple(missing), errors)


def _date(value: Any) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _decimal(value: Any) -> Decimal | None:
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return amount if amount.is_finite() else None
