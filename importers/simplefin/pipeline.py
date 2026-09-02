"""Safe SimpleFIN snapshot, normalization, and import planning.

This module never writes to Wealthfolio.  Raw and derived files contain private
financial data and are therefore written only below ``FINANCE_DATA``.
"""

from __future__ import annotations

import json
import os
import re
import unicodedata
import urllib.request
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable

try:
    from .client import FETCH_TIMEOUT, SimpleFinAccount, build_url, parse_accounts, _request
except ImportError:  # pragma: no cover - supports ``python cli.py``.
    from client import FETCH_TIMEOUT, SimpleFinAccount, build_url, parse_accounts, _request

MAX_HISTORY_DAYS = 90
DEFAULT_HISTORY_DAYS = 45
MAX_REQUESTS_PER_DAY = 24
CORPORATE_CARD_DECISION = "employer-corporate-card"
DORMANT_ACCOUNT_DECISION = "dormant-zero-balance-account"
DUPLICATE_SUMMARY_DECISION = "aggregator-account-summary"
INFLOW = {"DEPOSIT", "CREDIT", "TRANSFER_IN", "INTEREST", "DIVIDEND"}
OUTFLOW = {"WITHDRAWAL", "TRANSFER_OUT", "FEE", "TAX", "EXPENSE"}


class PipelineError(RuntimeError):
    pass


@dataclass(frozen=True)
class ExistingTransaction:
    account_id: str
    posted: date
    amount: Decimal
    description: str
    source_id: str | None = None


def normalize_description(value: str) -> str:
    """Normalize conservatively: case, punctuation, and whitespace only."""
    value = unicodedata.normalize("NFKC", value).casefold()
    return " ".join(re.sub(r"[^\w]+", " ", value).split())


def overlap_key(
    account_id: str, posted: date, amount: Decimal, description: str
) -> tuple[str, date, Decimal, str]:
    return (
        account_id,
        posted,
        amount.normalize(),
        normalize_description(description),
    )


def load_mapping(data_dir: Path) -> dict[str, dict[str, str]]:
    """Load the private, source-id keyed account map."""
    path = data_dir / "simplefin" / "account-map.json"
    if not path.exists():
        raise PipelineError(f"missing private account mapping: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PipelineError(f"could not read private account mapping: {exc}") from None
    accounts = payload.get("accounts")
    if payload.get("version") != 1 or not isinstance(accounts, dict):
        raise PipelineError("account-map.json must have version 1 and an accounts object")
    return accounts


def _snapshot_path(data_dir: Path, now: datetime) -> Path:
    folder = data_dir / "raw" / "simplefin" / now.date().isoformat()
    folder.mkdir(parents=True, exist_ok=True)
    # Fixed, exclusively-created slots enforce the limit even when callers race.
    # Reservations remain after transport failures because those may consume
    # Bridge quota too.
    reserved = False
    for slot in range(1, MAX_REQUESTS_PER_DAY + 1):
        reservation = folder / f"request-{slot:02d}"
        try:
            fd = os.open(reservation, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        except FileExistsError:
            continue
        os.close(fd)
        reserved = True
        break
    if not reserved:
        raise PipelineError("daily SimpleFIN request limit reached; no request was made")
    stamp = now.strftime("%H%M%S-%f")
    return folder / f"simplefin-{stamp}.json"


def fetch_snapshot(
    data_dir: Path,
    access_url: str,
    *,
    days: int = MAX_HISTORY_DAYS,
    now: datetime | None = None,
    opener=urllib.request.urlopen,
) -> tuple[Path, dict[str, Any]]:
    """Fetch once and persist the unmodified JSON response with exclusive create."""
    if not 1 <= days <= MAX_HISTORY_DAYS:
        raise PipelineError(f"days must be between 1 and {MAX_HISTORY_DAYS}")
    now = now or datetime.now(timezone.utc)
    path = _snapshot_path(data_dir, now)
    request = _request(
        build_url(
            access_url,
            # The protocol's bounds are inclusive, so 90 days starts 89 days
            # before today.
            start=now.date() - timedelta(days=days - 1),
            pending=True,
        )
    )
    try:
        with opener(request, timeout=FETCH_TIMEOUT) as response:
            body = response.read()
        payload = json.loads(body.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 - report transport/JSON uniformly
        raise PipelineError(f"SimpleFIN fetch failed: {exc}") from None
    if not isinstance(payload, dict):
        raise PipelineError("SimpleFIN response must be a JSON object")

    # O_EXCL makes snapshots append-only even when clocks collide or jobs race.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(body)
            output.flush()
            os.fsync(output.fileno())
    except Exception:
        path.unlink(missing_ok=True)
        raise
    path.chmod(0o444)
    return path, payload


def read_snapshot(path: Path) -> tuple[list[SimpleFinAccount], list[str]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PipelineError(f"could not read snapshot: {exc}") from None
    return parse_accounts(payload)


def existing_from_activities(rows: Iterable[dict[str, Any]]) -> list[ExistingTransaction]:
    """Convert read-only Wealthfolio activities into overlap candidates."""
    found: list[ExistingTransaction] = []
    for row in rows:
        raw_date = str(row.get("date") or row.get("activityDate") or "")[:10]
        try:
            posted = date.fromisoformat(raw_date)
            amount = Decimal(str(row.get("amount") or "0"))
        except (ValueError, InvalidOperation):
            continue
        if row.get("activityType") in OUTFLOW:
            amount = -abs(amount)
        elif row.get("activityType") in INFLOW:
            amount = abs(amount)
        else:
            continue
        key = str(row.get("idempotencyKey") or "")
        source_id = (
            key.split(":", 2)[2]
            if key.startswith(("extract:", "simplefin:")) and key.count(":") >= 2
            else None
        )
        found.append(
            ExistingTransaction(
                account_id=str(row.get("accountId") or ""),
                posted=posted,
                amount=amount,
                description=str(row.get("comment") or row.get("description") or ""),
                source_id=source_id,
            )
        )
    return found


def balances_from_activities(rows: Iterable[dict[str, Any]]) -> dict[str, Decimal]:
    balances: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    for txn in existing_from_activities(rows):
        balances[txn.account_id] += txn.amount
    return dict(balances)


def _transaction_semantics(account: SimpleFinAccount) -> list[tuple[Any, ...]]:
    return sorted(
        (
            transaction.posted,
            transaction.amount.normalize(),
            normalize_description(transaction.description),
            transaction.pending,
        )
        for transaction in account.transactions
    )


def exclusion_error(
    account: SimpleFinAccount,
    entry: dict[str, str],
    accounts_by_id: dict[str, SimpleFinAccount],
    mapping: dict[str, dict[str, str]],
) -> str | None:
    decision = entry.get("decision")
    if decision == CORPORATE_CARD_DECISION:
        return None
    if decision == DORMANT_ACCOUNT_DECISION:
        if account.balance != 0 or account.transactions:
            return "excluded-account-not-dormant"
        return None
    if decision != DUPLICATE_SUMMARY_DECISION:
        return "invalid-exclusion-decision"

    duplicate_id = entry.get("duplicateOfSourceAccountId")
    duplicate = accounts_by_id.get(duplicate_id or "")
    if duplicate is None or duplicate is account:
        return "invalid-duplicate-source"
    duplicate_mapping = mapping.get(duplicate.id, {})
    if (
        duplicate_mapping.get("action", "import") != "import"
        or not duplicate_mapping.get("wealthfolioAccountId")
    ):
        return "invalid-duplicate-target"
    if (
        account.currency != duplicate.currency
        or account.balance != duplicate.balance
        or account.balance_date != duplicate.balance_date
        or _transaction_semantics(account) != _transaction_semantics(duplicate)
    ):
        return "duplicate-summary-mismatch"
    return None


def build_plan(
    accounts: Iterable[SimpleFinAccount],
    errors: Iterable[str],
    mapping: dict[str, dict[str, str]],
    existing: Iterable[ExistingTransaction] = (),
    ledger_balances: dict[str, Decimal] | None = None,
    known_account_ids: set[str] | None = None,
    *,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    """Create a reviewable plan; no returned operation can mutate a balance."""
    generated_at = generated_at or datetime.now(timezone.utc)
    accounts = list(accounts)
    accounts_by_id = {account.id: account for account in accounts}
    existing = list(existing)
    ledger_balances = ledger_balances or {}
    by_source: dict[tuple[str, str], list[ExistingTransaction]] = defaultdict(list)
    by_overlap: dict[tuple[str, date, Decimal, str], list[ExistingTransaction]] = defaultdict(list)
    for old in existing:
        if old.source_id:
            by_source[(old.account_id, old.source_id)].append(old)
        by_overlap[overlap_key(old.account_id, old.posted, old.amount, old.description)].append(old)

    institution_errors = [str(error) for error in errors]
    advisories = [
        error
        for error in institution_errors
        if "exceeds recommended range" in error.casefold()
    ]
    actionable_errors = [
        error for error in institution_errors if error not in advisories
    ]
    blockers = [
        {"code": "institution-error", "message": error}
        for error in actionable_errors
    ]
    account_plans: list[dict[str, Any]] = []
    totals: dict[str, int] = defaultdict(int)

    for account in accounts:
        entry = mapping.get(account.id)
        base = {
            "sourceAccountId": account.id,
            "institution": account.org,
            "sourceName": account.name,
            "transactionCount": len(account.transactions),
        }
        if entry is None:
            blockers.append({"code": "missing-mapping", "sourceAccountId": account.id})
            account_plans.append({**base, "status": "blocked", "transactions": []})
            continue
        action = entry.get("action", "import")
        if action == "exclude":
            decision = entry.get("decision")
            error = exclusion_error(account, entry, accounts_by_id, mapping)
            if error:
                blockers.append({
                    "code": error,
                    "sourceAccountId": account.id,
                })
                account_plans.append({**base, "status": "blocked", "transactions": []})
            else:
                account_plans.append({
                    **base,
                    "status": "excluded",
                    "decision": decision,
                    **(
                        {
                            "duplicateOfSourceAccountId": entry[
                                "duplicateOfSourceAccountId"
                            ]
                        }
                        if decision == DUPLICATE_SUMMARY_DECISION
                        else {}
                    ),
                    "transactions": [],
                })
            continue

        if action == "monitor":
            target = entry.get("wealthfolioAlternativeAssetId")
            if not target:
                blockers.append({
                    "code": "missing-monitor-target",
                    "sourceAccountId": account.id,
                })
                account_plans.append({**base, "status": "blocked", "transactions": []})
                continue
            if known_account_ids is not None and target not in known_account_ids:
                blockers.append({
                    "code": "unknown-monitor-target",
                    "sourceAccountId": account.id,
                })
                account_plans.append({**base, "status": "blocked", "transactions": []})
                continue
            ledger = ledger_balances.get(target)
            drift = account.balance - ledger if ledger is not None else None
            account_plans.append({
                **base,
                "status": "monitored",
                "wealthfolioAlternativeAssetId": target,
                "assertionAccountId": entry.get("assertionAccountId", account.id),
                "sourceBalance": format(account.balance, "f"),
                "balanceDate": (
                    account.balance_date.isoformat() if account.balance_date else None
                ),
                "ledgerBalance": format(ledger, "f") if ledger is not None else None,
                "drift": format(drift, "f") if drift is not None else None,
                "balanceAction": "report-only",
                # Alternative liabilities do not accept account activities.
                "transactions": [],
            })
            continue

        if action == "observe":
            assertion_id = entry.get("assertionAccountId")
            if not assertion_id:
                blockers.append({
                    "code": "missing-observation-id",
                    "sourceAccountId": account.id,
                })
                account_plans.append({**base, "status": "blocked", "transactions": []})
                continue
            account_plans.append({
                **base,
                "status": "observed",
                "assertionAccountId": assertion_id,
                "sourceBalance": format(account.balance, "f"),
                "balanceDate": (
                    account.balance_date.isoformat() if account.balance_date else None
                ),
                "ledgerBalance": None,
                "drift": None,
                "balanceAction": "report-only",
                # The source is retained and asserted, but has no app target yet.
                "transactions": [],
            })
            continue

        if action != "import":
            blockers.append({
                "code": "unknown-mapping-action",
                "sourceAccountId": account.id,
            })
            account_plans.append({**base, "status": "blocked", "transactions": []})
            continue

        target = entry.get("wealthfolioAccountId")
        if not target:
            blockers.append({"code": "missing-target", "sourceAccountId": account.id})
            account_plans.append({**base, "status": "blocked", "transactions": []})
            continue
        if known_account_ids is not None and target not in known_account_ids:
            blockers.append({"code": "unknown-target", "sourceAccountId": account.id})
            account_plans.append({**base, "status": "blocked", "transactions": []})
            continue

        incoming_overlap_counts: dict[tuple[str, date, Decimal, str], int] = defaultdict(int)
        for txn in account.transactions:
            incoming_overlap_counts[
                overlap_key(target, txn.posted, txn.amount, txn.description)
            ] += 1

        transactions: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        for txn in account.transactions:
            status = "planned"
            reason = None
            key = overlap_key(target, txn.posted, txn.amount, txn.description)
            if txn.pending:
                status, reason = "skipped", "pending"
            elif not txn.id:
                status, reason = "review", "missing-source-id"
            elif txn.id in seen_ids:
                status, reason = "skipped", "duplicate-in-snapshot"
            elif by_source.get((target, txn.id)):
                status, reason = "skipped", "duplicate-source-id"
            elif by_overlap.get(key):
                if len(by_overlap[key]) > 1 or incoming_overlap_counts[key] > 1:
                    status, reason = "review", "ambiguous-overlap"
                else:
                    status, reason = "skipped", "duplicate-overlap"
            seen_ids.add(txn.id)
            totals[status] += 1
            transactions.append({
                "status": status,
                "reason": reason,
                "sourceId": txn.id,
                "date": txn.posted.isoformat(),
                "amount": format(txn.amount, "f"),
                "description": txn.description,
                "normalizedDescription": normalize_description(txn.description),
                "idempotencyKey": f"simplefin:{target}:{txn.id}" if txn.id else None,
            })

        ledger = ledger_balances.get(target)
        drift = account.balance - ledger if ledger is not None else None
        account_plans.append({
            **base,
            "status": "mapped",
            "wealthfolioAccountId": target,
            "assertionAccountId": entry.get("assertionAccountId", target),
            "sourceBalance": format(account.balance, "f"),
            "balanceDate": account.balance_date.isoformat() if account.balance_date else None,
            "ledgerBalance": format(ledger, "f") if ledger is not None else None,
            "drift": format(drift, "f") if drift is not None else None,
            "balanceAction": "report-only",
            "transactions": transactions,
        })

    return {
        "schemaVersion": 1,
        "mode": "plan-only",
        "generatedAt": generated_at.isoformat(),
        "ready": not blockers,
        "institutionErrors": institution_errors,
        "advisories": advisories,
        "blockers": blockers,
        "totals": dict(totals),
        "accounts": account_plans,
    }


def write_plan(data_dir: Path, plan: dict[str, Any], snapshot: Path) -> tuple[Path, Path]:
    """Persist the plan and machine-checkable balance assertions externally."""
    stamp = datetime.fromisoformat(plan["generatedAt"]).strftime("%Y-%m-%d-%H%M%S-%f")
    folder = data_dir / "normalized" / "simplefin"
    folder.mkdir(parents=True, exist_ok=True)
    plan_path = folder / f"plan-{stamp}.json"
    document = {**plan, "snapshot": str(snapshot)}
    plan_path.write_text(json.dumps(document, indent=2), encoding="utf-8")

    assertions = {
        "schemaVersion": 1,
        "generatedAt": plan["generatedAt"],
        "sourceSnapshot": str(snapshot),
        # Canonical balance-snapshot shape consumed by
        # ``facts cli.py verify-assertions``.
        "balances": [
            {
                "accountId": account["assertionAccountId"],
                "date": account["balanceDate"],
                "balance": account["sourceBalance"],
                "source": "simplefin",
                "ledgerBalance": account["ledgerBalance"],
                "drift": account["drift"],
                "action": "report-only",
            }
            for account in plan["accounts"]
            if account.get("status") in {"mapped", "monitored", "observed"}
        ],
        "institutionErrors": plan["institutionErrors"],
    }
    assertion_path = folder / f"assertions-{stamp}.json"
    assertion_path.write_text(json.dumps(assertions, indent=2), encoding="utf-8")
    return plan_path, assertion_path
