"""Build private household analytics without teaching Wealthfolio fake history."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import shutil
import uuid
from calendar import monthrange
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable

from importers.assets.loan import balance_as_of
from importers.facts.loader import load_facts
from importers.facts.schema import LoanFact, PropertyFact, VehicleFact
from importers.normalized.builder import (
    ACCOUNT_COLUMNS,
    TRANSACTION_COLUMNS,
    BuildError,
    verify_publication,
)

from .publication import ensure_durable_directory, fsync_directory

SCHEMA_VERSION = 1
OUTPUT_NAMES = (
    "monthly-analytics.csv",
    "investment-performance.csv",
    "metadata-review.json",
    "reporting-portfolios.json",
)
MONTHLY_COLUMNS = (
    "month",
    "as_of",
    "net_worth",
    "investable_assets",
    "liabilities",
    "property_equity",
    "cash_flow",
    "cash_flow_status",
    "investment_value",
    "property_value",
    "other_assets",
    "missing_entity_count",
    "data_quality",
)
PERFORMANCE_COLUMNS = (
    "account_id",
    "start_date",
    "end_date",
    "start_value",
    "end_value",
    "external_inflows",
    "external_outflows",
    "investment_gain",
    "modified_dietz_return",
    "status",
    "reason",
)
POSITION_COLUMNS = (
    "as_of",
    "account_id",
    "symbol",
    "quantity",
    "price",
    "market_value",
    "basis_per_unit",
    "source_file",
)
VALUATION_COLUMNS = (
    "date",
    "entity_id",
    "value",
    "currency",
    "source_file",
    "observed_or_derived",
)

INVESTMENT_KINDS = {"SECURITIES", "CRYPTOCURRENCY"}
CASH_KINDS = {"CASH"}
LIABILITY_KINDS = {"CREDIT_CARD", "credit_card", "LIABILITY", "liability"}
METADATA_FIELDS = {
    "asset": ("assetClass", "region", "sector"),
    "account": ("owner", "taxBucket", "retirement", "investable"),
}


class AnalyticsError(RuntimeError):
    """Canonical inputs cannot produce trustworthy analytics."""


@dataclass(frozen=True)
class MonthlyRow:
    month: str
    as_of: str
    net_worth: str
    investable_assets: str
    liabilities: str
    property_equity: str
    cash_flow: str
    cash_flow_status: str
    investment_value: str
    property_value: str
    other_assets: str
    missing_entity_count: int
    data_quality: str


@dataclass(frozen=True)
class PerformanceRow:
    account_id: str
    start_date: str
    end_date: str
    start_value: str
    end_value: str
    external_inflows: str
    external_outflows: str
    investment_gain: str
    modified_dietz_return: str
    status: str
    reason: str


@dataclass(frozen=True)
class Documents:
    files: dict[str, bytes]
    sources: tuple[Path, ...]
    summary: dict[str, Any]


def _money(value: Decimal | None) -> str:
    return "" if value is None else format(value.quantize(Decimal("0.01")), "f")


def _ratio(value: Decimal | None) -> str:
    return "" if value is None else format(value.quantize(Decimal("0.00000001")), "f")


def _decimal(value: str, context: str) -> Decimal:
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise AnalyticsError(f"{context}: invalid decimal") from exc
    if not parsed.is_finite():
        raise AnalyticsError(f"{context}: decimal must be finite")
    return parsed


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_csv(path: Path, expected: tuple[str, ...]) -> list[dict[str, str]]:
    try:
        with path.open(encoding="utf-8", newline="") as source:
            reader = csv.DictReader(source)
            if tuple(reader.fieldnames or ()) != expected:
                raise AnalyticsError(f"{path.name}: columns do not match canonical schema")
            return list(reader)
    except OSError as exc:
        raise AnalyticsError(f"cannot read {path}") from exc


def _load_canonical(root: Path) -> tuple[dict[str, list[dict[str, str]]], Path]:
    canonical = root / "normalized" / "canonical"
    manifest_path = canonical / "manifest.json"
    try:
        verify_publication(root)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except BuildError as exc:
        raise AnalyticsError(f"canonical verification failed: {exc}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise AnalyticsError("canonical manifest is missing or invalid") from exc

    schemas = {
        "accounts.csv": ACCOUNT_COLUMNS,
        "transactions.csv": TRANSACTION_COLUMNS,
        "positions.csv": POSITION_COLUMNS,
        "valuations.csv": VALUATION_COLUMNS,
    }
    rows: dict[str, list[dict[str, str]]] = {}
    for name, columns in schemas.items():
        path = canonical / name
        if not path.exists() or _sha256(path) != manifest.get("dataFiles", {}).get(name):
            raise AnalyticsError(f"canonical data hash mismatch: {name}")
        rows[name] = _read_csv(path, columns)
    return rows, manifest_path


def _month_ends(start: date, end: date) -> list[date]:
    if end < start:
        return []
    current = date(start.year, start.month, min(monthrange(start.year, start.month)[1], end.day))
    current = max(current, start)
    result: list[date] = []
    while current <= end:
        last = date(current.year, current.month, monthrange(current.year, current.month)[1])
        result.append(min(last, end))
        if current.month == 12:
            current = date(current.year + 1, 1, 1)
        else:
            current = date(current.year, current.month + 1, 1)
    return result


def _same_month_observations(
    points: dict[str, list[tuple[date, Decimal]]],
) -> dict[tuple[str, str], tuple[date, Decimal]]:
    result: dict[tuple[str, str], tuple[date, Decimal]] = {}
    for entity_id, observations in points.items():
        for candidate in observations:
            key = (entity_id, candidate[0].strftime("%Y-%m"))
            if key not in result or candidate[0] > result[key][0]:
                result[key] = candidate
    return result


def _interpolate(points: list[tuple[date, Decimal]], when: date) -> Decimal | None:
    if not points:
        return None
    ordered = sorted(points)
    if when < ordered[0][0]:
        return None
    if when == ordered[0][0]:
        return ordered[0][1]
    if when > ordered[-1][0]:
        return None
    if when == ordered[-1][0]:
        return ordered[-1][1]
    for (lo_date, lo_value), (hi_date, hi_value) in zip(ordered, ordered[1:]):
        if lo_date <= when <= hi_date:
            travelled = Decimal((when - lo_date).days)
            span = Decimal((hi_date - lo_date).days)
            return lo_value + (hi_value - lo_value) * travelled / span
    return None


def _active(account: dict[str, str], when: date) -> bool:
    opened = date.fromisoformat(account["opened"]) if account["opened"] else None
    closed = date.fromisoformat(account["closed"]) if account["closed"] else None
    return (opened is None or when >= opened) and (closed is None or when <= closed)


def _active_during_month(account: dict[str, str], when: date) -> bool:
    month_start = date(when.year, when.month, 1)
    opened = date.fromisoformat(account["opened"]) if account["opened"] else None
    closed = date.fromisoformat(account["closed"]) if account["closed"] else None
    return (opened is None or opened <= when) and (
        closed is None or closed >= month_start
    )


def _entity_points(
    valuations: list[dict[str, str]],
) -> dict[str, list[tuple[date, Decimal]]]:
    unique: dict[tuple[str, date], Decimal] = {}
    for row in valuations:
        entity_id = row["entity_id"]
        when = date.fromisoformat(row["date"])
        value = _decimal(row["value"], entity_id)
        key = (entity_id, when)
        if key in unique and unique[key] != value:
            raise AnalyticsError(
                f"conflicting valuations for {entity_id} on {when.isoformat()}"
            )
        unique[key] = value
    result: dict[str, list[tuple[date, Decimal]]] = defaultdict(list)
    for (entity_id, when), value in unique.items():
        result[entity_id].append((when, value))
    for observations in result.values():
        observations.sort()
    return result


def _modeled_entities(root: Path, valuations: list[dict[str, str]]):
    loaded = load_facts(root / "facts")
    if loaded.errors:
        raise AnalyticsError("facts are invalid:\n" + "\n".join(str(issue) for issue in loaded.errors))
    points = _entity_points(valuations)
    properties: dict[str, PropertyFact] = {}
    loans: dict[str, LoanFact] = {}
    vehicles: dict[str, VehicleFact] = {}
    sources = {parsed.path for parsed in loaded.facts}
    for parsed in loaded.facts:
        fact = parsed.fact
        if isinstance(fact, PropertyFact):
            properties[f"property:{fact.name}"] = fact
        elif isinstance(fact, LoanFact):
            loans[f"loan:{fact.name}"] = fact
        elif isinstance(fact, VehicleFact):
            vehicles[f"vehicle:{fact.name}"] = fact
    return properties, loans, vehicles, points, sources


def _property_value(
    entity_id: str, fact: PropertyFact, points: dict[str, list[tuple[date, Decimal]]], when: date
) -> Decimal | None:
    if fact.purchase_date and when < fact.purchase_date:
        return Decimal("0")
    if fact.sale_date and when >= fact.sale_date:
        return Decimal("0")
    return _interpolate(points.get(entity_id, []), when)


def _loan_value(fact: LoanFact, when: date) -> Decimal | None:
    if fact.origination_date and when < fact.origination_date:
        return Decimal("0")
    if fact.payoff_date and when >= fact.payoff_date:
        return Decimal("0")
    if (
        fact.principal is None
        or fact.annual_rate is None
        or fact.term_months is None
        or fact.first_payment is None
    ):
        return None
    return -balance_as_of(
        fact.principal, fact.annual_rate, fact.term_months, fact.first_payment, when
    ).balance


def _vehicle_value(
    entity_id: str, fact: VehicleFact, points: dict[str, list[tuple[date, Decimal]]], when: date
) -> Decimal | None:
    if fact.purchase_date and when < fact.purchase_date:
        return Decimal("0")
    return _interpolate(points.get(entity_id, []), when)


def _monthly_cash_flow(
    transactions: list[dict[str, str]], accounts: dict[str, dict[str, str]]
) -> tuple[dict[str, Decimal], dict[str, set[str]]]:
    result: dict[str, Decimal] = defaultdict(Decimal)
    evidence: dict[str, set[str]] = defaultdict(set)
    for row in transactions:
        account = accounts.get(row["account_id"])
        if (
            not account
            or account["excluded"] == "true"
            or row["excluded"] == "true"
            or account["kind"] not in CASH_KINDS | {"CREDIT_CARD", "credit_card"}
        ):
            continue
        month = row["date"][:7]
        evidence[month].add(row["account_id"])
        # transaction_kind is the structural classification computed by
        # importers/normalized/builder.py; using it here (instead of
        # transfer_group/symbol/external_flow/category heuristics) correctly
        # excludes transfers, card/loan payments, saving, reconciliation, and
        # investment activity from spending regardless of category labels.
        if row["transaction_kind"] not in {
            "income", "expense", "refund", "reimbursement"
        }:
            continue
        result[month] += _decimal(row["amount"], "transaction amount")
    return result, evidence


def _monthly_rows(root: Path, rows: dict[str, list[dict[str, str]]]) -> list[MonthlyRow]:
    accounts = {row["account_id"]: row for row in rows["accounts.csv"]}
    valuations = rows["valuations.csv"]
    properties, loans, vehicles, points, _ = _modeled_entities(root, valuations)
    observations = _same_month_observations(points)
    cash_flow, transaction_evidence = _monthly_cash_flow(
        rows["transactions.csv"], accounts
    )
    dated = [date.fromisoformat(row["date"]) for row in valuations]
    dated.extend(date.fromisoformat(row["as_of"]) for row in rows["positions.csv"])
    dated.extend(
        date.fromisoformat(f"{month}-01")
        for month in transaction_evidence
    )
    if not dated:
        return []
    start = min(dated)
    end = max(dated)
    result: list[MonthlyRow] = []

    for when in _month_ends(start, end):
        month = when.strftime("%Y-%m")
        missing: set[str] = set()
        investment = Decimal("0")
        cash = Decimal("0")
        liabilities = Decimal("0")
        other_account_assets = Decimal("0")
        account_total = Decimal("0")

        for account_id, account in accounts.items():
            if account["excluded"] == "true" or not _active(account, when):
                continue
            if account_id in loans:
                value = _loan_value(loans[account_id], when)
            else:
                observed = observations.get((account_id, month))
                value = observed[1] if observed and observed[0] <= when else None
            if value is None:
                missing.add(account_id)
                continue
            account_total += value
            kind = account["kind"]
            if kind in INVESTMENT_KINDS:
                investment += value
            elif kind in CASH_KINDS:
                cash += value
            elif kind in LIABILITY_KINDS:
                liabilities += value
            else:
                other_account_assets += value

        property_value = Decimal("0")
        property_loans = Decimal("0")
        linked_loan_ids = set()
        for entity_id, fact in properties.items():
            value = _property_value(entity_id, fact, points, when)
            if value is None:
                missing.add(entity_id)
            else:
                property_value += value
            for loan_id, loan in loans.items():
                if loan.linked_to == fact.name:
                    linked_loan_ids.add(loan_id)
                    loan_value = _loan_value(loan, when)
                    if loan_value is None:
                        missing.add(loan_id)
                    else:
                        property_loans += loan_value

        vehicle_value = Decimal("0")
        for entity_id, fact in vehicles.items():
            value = _vehicle_value(entity_id, fact, points, when)
            if value is None:
                missing.add(entity_id)
            else:
                vehicle_value += value

        # Loan accounts already contribute to account_total. Entity assets do not.
        net_worth = (
            None
            if missing
            else account_total + property_value + vehicle_value
        )
        active_investable = {
            account_id
            for account_id, account in accounts.items()
            if account["excluded"] != "true"
            and _active(account, when)
            and account["kind"] in INVESTMENT_KINDS | CASH_KINDS
        }
        investable = None if missing & active_investable else investment + cash
        active_liabilities = {
            account_id
            for account_id, account in accounts.items()
            if account["excluded"] != "true"
            and _active(account, when)
            and account["kind"] in LIABILITY_KINDS
        }
        liability_total = None if missing & active_liabilities else liabilities
        property_missing = bool(missing & (set(properties) | linked_loan_ids))
        property_equity = None if property_missing else property_value + property_loans
        active_other_accounts = {
            account_id
            for account_id, account in accounts.items()
            if account["excluded"] != "true"
            and _active(account, when)
            and account["kind"] not in INVESTMENT_KINDS | CASH_KINDS | LIABILITY_KINDS
        }
        other_missing = bool(
            missing
            & (
                set(vehicles)
                | active_other_accounts
            )
        )
        active_transaction_accounts = {
            account_id
            for account_id, account in accounts.items()
            if account["excluded"] != "true"
            and _active_during_month(account, when)
            and account["kind"] in CASH_KINDS | {"CREDIT_CARD", "credit_card"}
        }
        cash_flow_available = bool(active_transaction_accounts) and (
            active_transaction_accounts <= transaction_evidence.get(month, set())
        )
        result.append(
            MonthlyRow(
                month,
                when.isoformat(),
                _money(net_worth),
                _money(investable),
                _money(liability_total),
                _money(property_equity),
                _money(cash_flow.get(month, Decimal("0")))
                if cash_flow_available
                else "",
                "available" if cash_flow_available else "unavailable",
                _money(None if missing & active_investable else investment),
                _money(None if property_missing else property_value),
                _money(None if other_missing else vehicle_value + other_account_assets),
                len(missing),
                "complete" if not missing and cash_flow_available else "partial",
            )
        )
    return result


def _performance_rows(rows: dict[str, list[dict[str, str]]]) -> list[PerformanceRow]:
    accounts = {row["account_id"]: row for row in rows["accounts.csv"]}
    observed_rows = [
        row
        for row in rows["valuations.csv"]
        if row["observed_or_derived"] == "observed"
    ]
    observed_points = _entity_points(observed_rows)
    observations = {
        entity_id: points
        for entity_id, points in observed_points.items()
        if accounts.get(entity_id, {}).get("kind") in INVESTMENT_KINDS
    }
    transactions: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows["transactions.csv"]:
        transactions[row["account_id"]].append(row)

    result: list[PerformanceRow] = []
    for account_id in sorted(
        key for key, account in accounts.items()
        if account["excluded"] != "true" and account["kind"] in INVESTMENT_KINDS
    ):
        points = sorted(set(observations.get(account_id, [])))
        if len(points) < 2:
            result.append(
                PerformanceRow(
                    account_id,
                    points[0][0].isoformat() if points else "",
                    "",
                    _money(points[0][1]) if points else "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "unavailable",
                    "At least two source-backed account valuations are required.",
                )
            )
            continue
        for (start_date, start_value), (end_date, end_value) in zip(points, points[1:]):
            period_days = Decimal((end_date - start_date).days)
            if period_days <= 0:
                continue
            flows: list[tuple[date, Decimal]] = []
            for row in transactions.get(account_id, []):
                when = date.fromisoformat(row["date"])
                if (
                    start_date < when <= end_date
                    and row["external_flow"] == "true"
                    and row["excluded"] != "true"
                ):
                    amount = _decimal(row["amount"], "external flow")
                    if amount == 0 and row["quantity"] and row["price"]:
                        amount = _decimal(
                            row["quantity"], "external in-kind quantity"
                        ) * _decimal(row["price"], "external in-kind price")
                    flows.append((when, amount))
            net_flow = sum((amount for _, amount in flows), Decimal("0"))
            inflows = sum((amount for _, amount in flows if amount > 0), Decimal("0"))
            outflows = -sum((amount for _, amount in flows if amount < 0), Decimal("0"))
            gain = end_value - start_value - net_flow
            denominator = start_value
            for flow_date, amount in flows:
                remaining = Decimal((end_date - flow_date).days)
                denominator += amount * remaining / period_days
            return_value = gain / denominator if denominator > 0 else None
            result.append(
                PerformanceRow(
                    account_id,
                    start_date.isoformat(),
                    end_date.isoformat(),
                    _money(start_value),
                    _money(end_value),
                    _money(inflows),
                    _money(outflows),
                    _money(gain),
                    _ratio(return_value),
                    "supported" if return_value is not None else "unavailable",
                    (
                        "Source-backed endpoint valuations with canonical external-flow labels."
                        if return_value is not None
                        else "Modified Dietz denominator is zero or negative."
                    ),
                )
            )
    return result


def _metadata(
    root: Path, rows: dict[str, list[dict[str, str]]]
) -> tuple[dict[str, Any], Path | None, tuple[Path, ...]]:
    path = root / "plans" / "analytics-metadata.json"
    payload: dict[str, Any] = {"schemaVersion": 1, "entries": []}
    if path.exists():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise AnalyticsError("plans/analytics-metadata.json is invalid") from exc
    if payload.get("schemaVersion") != 1 or not isinstance(payload.get("entries"), list):
        raise AnalyticsError("analytics metadata schema is unsupported")

    entries: dict[str, dict[str, Any]] = {}
    citation_paths: set[Path] = set()
    for entry in payload["entries"]:
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
            raise AnalyticsError("analytics metadata entries require stable string ids")
        if entry["id"] in entries:
            raise AnalyticsError(f"duplicate analytics metadata id: {entry['id']}")
        entry_type = entry.get("type")
        if entry_type not in METADATA_FIELDS:
            raise AnalyticsError(f"analytics metadata {entry['id']}: unsupported type")
        values = entry.get("values") or {}
        citations = entry.get("citations") or []
        if set(values) - set(METADATA_FIELDS[entry_type]):
            raise AnalyticsError(f"analytics metadata {entry['id']}: unknown field")
        if values and (entry.get("reviewed") is not True or not citations):
            raise AnalyticsError(
                f"analytics metadata {entry['id']}: classifications require review and citations"
            )
        normalized_citations = []
        for citation in citations:
            if not isinstance(citation, dict) or not citation.get("source") or not (
                citation.get("url") or citation.get("sourcePath")
            ):
                raise AnalyticsError(
                    f"analytics metadata {entry['id']}: citations require source and URL or path"
                )
            normalized = dict(citation)
            if citation.get("url") and not str(citation["url"]).startswith("https://"):
                raise AnalyticsError(
                    f"analytics metadata {entry['id']}: citation URLs must use HTTPS"
                )
            if citation.get("sourcePath"):
                source_path = (root / str(citation["sourcePath"])).resolve()
                try:
                    source_path.relative_to(root.resolve())
                except ValueError as exc:
                    raise AnalyticsError(
                        f"analytics metadata {entry['id']}: citation path leaves data directory"
                    ) from exc
                if not source_path.is_file():
                    raise AnalyticsError(
                        f"analytics metadata {entry['id']}: cited source file does not exist"
                    )
                citation_paths.add(source_path)
                normalized["sha256"] = _sha256(source_path)
            normalized_citations.append(normalized)
        entries[entry["id"]] = {**entry, "citations": normalized_citations}

    expected: list[tuple[str, str]] = []
    for account in rows["accounts.csv"]:
        expected.append((f"account:{account['account_id']}", "account"))
    symbols = {
        row["symbol"]
        for name in ("transactions.csv", "positions.csv")
        for row in rows[name]
        if row["symbol"]
    }
    expected.extend((f"asset:{symbol}", "asset") for symbol in symbols)
    items = []
    for stable_id, item_type in sorted(expected):
        entry = entries.get(stable_id, {})
        values = entry.get("values") or {}
        items.append(
            {
                "id": stable_id,
                "type": item_type,
                "reviewed": entry.get("reviewed") is True,
                "values": values,
                "missingFields": [field for field in METADATA_FIELDS[item_type] if field not in values],
                "citations": entry.get("citations") or [],
            }
        )
    return {
        "schemaVersion": 1,
        "private": True,
        "method": "reviewed-only",
        "items": items,
        "summary": {
            "itemCount": len(items),
            "reviewedCount": sum(item["reviewed"] for item in items),
            "unresolvedFieldCount": sum(len(item["missingFields"]) for item in items),
        },
    }, path if path.exists() else None, tuple(sorted(citation_paths))


def _portfolio_plan(metadata_review: dict[str, Any]) -> dict[str, Any]:
    accounts = [item for item in metadata_review["items"] if item["type"] == "account"]
    definitions = [
        ("household-investable", "Household - Investable", "investable", True),
        ("retirement", "Retirement", "retirement", True),
        ("non-retirement", "Non-retirement", "retirement", False),
        ("taxable", "Taxable", "taxBucket", "taxable"),
        ("tax-deferred", "Tax-deferred", "taxBucket", "tax-deferred"),
        ("tax-free", "Tax-free", "taxBucket", "tax-free"),
    ]
    portfolios = [
        {
            "id": portfolio_id,
            "name": name,
            "accountIds": [
                item["id"].removeprefix("account:")
                for item in accounts
                if item["reviewed"] and item["values"].get(field) == expected
            ],
            "selector": {"field": field, "equals": expected},
        }
        for portfolio_id, name, field, expected in definitions
    ]
    portfolios.extend(
        {
            "id": portfolio_id,
            "name": name,
            "accountIds": [],
            "selector": {"field": "owner", "equals": None},
            "needsReview": True,
        }
        for portfolio_id, name in (
            ("owner-a", "Owner A"),
            ("owner-b", "Owner B"),
            ("owner-joint", "Joint"),
        )
    )
    owners = sorted(
        {
            str(item["values"]["owner"])
            for item in accounts
            if item["reviewed"] and item["values"].get("owner")
        }
    )
    for owner in owners:
        portfolios.append(
            {
                "id": f"owner-{hashlib.sha256(owner.encode()).hexdigest()[:12]}",
                "name": f"Owner - {owner}",
                "accountIds": [
                    item["id"].removeprefix("account:")
                    for item in accounts
                    if item["reviewed"] and item["values"].get("owner") == owner
                ],
                "selector": {"field": "owner", "equals": owner},
            }
        )
    return {
        "schemaVersion": 1,
        "private": True,
        "mode": "proposal-only",
        "productionMutation": False,
        "portfolios": portfolios,
        "accountsNeedingReview": [
            item["id"].removeprefix("account:")
            for item in accounts
            if item["missingFields"]
        ],
    }


def _csv_bytes(rows: Iterable[Any], columns: tuple[str, ...]) -> bytes:
    target = io.StringIO(newline="")
    writer = csv.DictWriter(target, fieldnames=columns, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow(asdict(row))
    return target.getvalue().encode("utf-8")


def _json_bytes(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _documents(data_dir: str | Path) -> Documents:
    root = Path(data_dir)
    rows, canonical_manifest = _load_canonical(root)
    monthly = _monthly_rows(root, rows)
    performance = _performance_rows(rows)
    metadata, metadata_path, citation_paths = _metadata(root, rows)
    portfolios = _portfolio_plan(metadata)
    files = {
        "monthly-analytics.csv": _csv_bytes(monthly, MONTHLY_COLUMNS),
        "investment-performance.csv": _csv_bytes(performance, PERFORMANCE_COLUMNS),
        "metadata-review.json": _json_bytes(metadata),
        "reporting-portfolios.json": _json_bytes(portfolios),
    }
    fact_sources = sorted((root / "facts").rglob("*.json"))
    sources = tuple(
        [canonical_manifest, *fact_sources]
        + ([metadata_path] if metadata_path else [])
        + list(citation_paths)
    )
    summary = {
        "schemaVersion": SCHEMA_VERSION,
        "rowCounts": {
            "monthlyAnalytics": len(monthly),
            "investmentPerformance": len(performance),
            "metadataReviewItems": metadata["summary"]["itemCount"],
            "proposedPortfolios": len(portfolios["portfolios"]),
        },
        "coverage": {
            "completeNetWorthMonths": sum(bool(row.net_worth) for row in monthly),
            "completePropertyEquityMonths": sum(bool(row.property_equity) for row in monthly),
            "availableCashFlowMonths": sum(
                row.cash_flow_status == "available" for row in monthly
            ),
            "supportedPerformancePeriods": sum(row.status == "supported" for row in performance),
        },
        "limitations": [
            "Account values are used only in months containing a source-backed valuation; missing months are never forward-filled.",
            "Property values are linearly interpolated only between cited observations and are zero outside the ownership window.",
            "Fixed-rate loan balances are amortized from canonical terms and are zero after payoff.",
            "Cash flow is available only when every active cash and credit-card account has transaction evidence in that month; absent evidence is never treated as zero.",
            "Investment performance is emitted only between source-backed valuation endpoints.",
        ],
    }
    return Documents(files, sources, summary)


def plan(data_dir: str | Path) -> dict[str, Any]:
    return _documents(data_dir).summary


def _atomic_write(path: Path, content: bytes) -> None:
    temporary = path.parent / f".{path.name}-{uuid.uuid4().hex}.tmp"
    try:
        with temporary.open("xb") as target:
            target.write(content)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
        fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _write_synced(path: Path, content: bytes) -> None:
    with path.open("xb") as target:
        target.write(content)
        target.flush()
        os.fsync(target.fileno())


def _current_publication(root: Path) -> tuple[Path, dict[str, Any]]:
    analytics = root / "normalized" / "analytics"
    pointer_path = analytics / "current.json"
    try:
        pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AnalyticsError("analytics current pointer is missing or invalid") from exc
    publication_id = pointer.get("publicationId")
    manifest_hash = pointer.get("manifestSha256")
    if (
        pointer.get("schemaVersion") != 1
        or not isinstance(publication_id, str)
        or len(publication_id) != 64
        or not isinstance(manifest_hash, str)
        or manifest_hash != publication_id
    ):
        raise AnalyticsError("analytics current pointer schema is unsupported")
    publication = analytics / "publications" / publication_id
    manifest_path = publication / "manifest.json"
    if not manifest_path.is_file() or _sha256(manifest_path) != manifest_hash:
        raise AnalyticsError("analytics current publication manifest hash mismatch")
    return publication, pointer


def build(data_dir: str | Path) -> dict[str, Any]:
    root = Path(data_dir)
    documents = _documents(root)
    output = root / "normalized" / "analytics"
    publications = output / "publications"
    ensure_durable_directory(publications, fsync_directory)
    source_files = [
        {
            "path": str(path.relative_to(root)).replace("\\", "/"),
            "sha256": _sha256(path),
        }
        for path in documents.sources
    ]
    manifest = {
        **documents.summary,
        "private": True,
        "sourceFiles": source_files,
        "dataFiles": {
            name: hashlib.sha256(content).hexdigest()
            for name, content in sorted(documents.files.items())
        },
    }
    manifest_content = _json_bytes(manifest)
    publication_id = hashlib.sha256(manifest_content).hexdigest()
    publication = publications / publication_id
    staging = publications / f".staging-{uuid.uuid4().hex}"
    staging.mkdir()
    try:
        for name, content in documents.files.items():
            _write_synced(staging / name, content)
        _write_synced(staging / "manifest.json", manifest_content)
        fsync_directory(staging)
        if publication.exists():
            if (publication / "manifest.json").read_bytes() != manifest_content:
                raise AnalyticsError("analytics publication identifier collision")
        else:
            os.replace(staging, publication)
            fsync_directory(publications)
        pointer = {
            "schemaVersion": 1,
            "publicationId": publication_id,
            "manifestSha256": publication_id,
        }
        _atomic_write(output / "current.json", _json_bytes(pointer))
        return {**manifest, "publication": pointer}
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def verify(data_dir: str | Path) -> dict[str, Any]:
    root = Path(data_dir)
    output, pointer = _current_publication(root)
    try:
        manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AnalyticsError("analytics manifest is missing or invalid") from exc
    if manifest.get("schemaVersion") != SCHEMA_VERSION:
        raise AnalyticsError("analytics manifest schema is unsupported")
    documents = _documents(root)
    expected_sources = [
        {
            "path": str(path.relative_to(root)).replace("\\", "/"),
            "sha256": _sha256(path),
        }
        for path in documents.sources
    ]
    if manifest.get("sourceFiles") != expected_sources:
        raise AnalyticsError("analytics source manifest differs from recomputed sources")
    for source in expected_sources:
        path = root / source["path"]
        if not path.exists() or _sha256(path) != source["sha256"]:
            raise AnalyticsError(f"analytics source hash mismatch: {source['path']}")
    expected_data_files = {
        name: hashlib.sha256(content).hexdigest()
        for name, content in sorted(documents.files.items())
    }
    if manifest.get("dataFiles") != expected_data_files:
        raise AnalyticsError("analytics data manifest differs from recomputed outputs")
    for name, content in documents.files.items():
        path = output / name
        if not path.exists() or path.read_bytes() != content:
            raise AnalyticsError(f"analytics output differs from recomputed data: {name}")
        if hashlib.sha256(content).hexdigest() != manifest.get("dataFiles", {}).get(name):
            raise AnalyticsError(f"analytics data hash mismatch: {name}")
    if documents.summary != {
        key: manifest.get(key)
        for key in ("schemaVersion", "rowCounts", "coverage", "limitations")
    }:
        raise AnalyticsError("analytics manifest summary differs from recomputed data")
    return {"verified": True, "publication": pointer, **documents.summary}
