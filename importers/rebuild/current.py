"""Rebuild Ledger holdings and enforce durable current-balance assertions."""

from __future__ import annotations

import csv
import json
from datetime import date
from decimal import Decimal
from pathlib import Path

from importers.facts.schema import DecisionFact, ParsedFact, QuoteFact

from .decisions import DecisionError


def _rows(path: Path) -> list[dict]:
    with path.open(encoding="utf-8-sig", newline="") as source:
        return list(csv.DictReader(source))


def ledger_payloads(canonical_dir: Path, account_map: dict[str, str]) -> list[dict]:
    positions = [
        row for row in _rows(canonical_dir / "positions.csv")
        if "ledger-live-normalized" in row["source_file"]
    ]
    if len(positions) != 1:
        raise DecisionError(f"expected one canonical Ledger position, found {len(positions)}")
    position = positions[0]
    account_id = account_map.get(position["account_id"])
    if not account_id:
        raise DecisionError("Ledger canonical account is absent from the app account map")
    transactions = [
        row
        for row in _rows(canonical_dir / "transactions.csv")
        if row["source_id"].startswith("ledger:") and row["excluded"].casefold() != "true"
    ]
    if not transactions:
        raise DecisionError("canonical Ledger history is empty")
    payloads = []
    for row in transactions:
        amount = Decimal(row["amount"])
        if amount == 0:
            continue
        payloads.append({
            "accountId": account_id,
            "activityType": "TRANSFER_IN" if amount > 0 else "TRANSFER_OUT",
            "activityDate": f"{row['date']}T00:00:00Z",
            "asset": {
                "symbol": "BTC" if position["symbol"] == "SATOSHI" else position["symbol"],
                "name": "Bitcoin" if position["symbol"] == "SATOSHI" else position["symbol"],
                "kind": "SECURITY",
                "quoteMode": "MARKET",
                "quoteCcy": "USD",
                "instrumentType": "CRYPTO",
            },
            "quantity": float(abs(amount)),
            # No acquisition basis is present in raw/facts/canonical. Zero is
            # explicit rather than inventing one; acceptance reports the gap.
            "unitPrice": 0.0,
            "currency": "USD",
            "isDraft": False,
            "subtype": "external_transfer",
            "metadata": json.dumps({"flow": {"is_external": True}}),
            "comment": "Canonical Ledger history; acquisition basis unknown",
            "idempotencyKey": row["source_id"],
        })
    total = sum(
        (Decimal(row["amount"]) for row in transactions), Decimal("0")
    )
    if total != Decimal(position["quantity"]):
        raise DecisionError("Ledger history does not reconcile to canonical quantity")
    return payloads


def quote_payloads(client, facts: tuple[ParsedFact, ...]) -> list[dict]:
    """Resolve durable quote assertions to Wealthfolio's internal asset IDs."""
    assets = {
        (str(row.get("instrumentType") or ""), str(row.get("instrumentSymbol") or "")): row
        for row in client.get("/assets")
    }
    payloads = []
    for parsed in facts:
        fact = parsed.fact
        if not isinstance(fact, QuoteFact):
            continue
        asset = assets.get((fact.instrument_type, fact.symbol))
        if not asset:
            raise DecisionError(
                f"quote asset is absent: {fact.instrument_type}:{fact.symbol}"
            )
        if fact.on is None or fact.close is None:
            raise DecisionError(f"quote fact is incomplete: {parsed.fact_id}")
        value = float(fact.close)
        payloads.append({
            "symbol": asset["id"],
            "date": fact.on.isoformat(),
            "open": value,
            "high": value,
            "low": value,
            "close": value,
            "volume": 0,
            "currency": fact.currency,
            "dataSource": "MANUAL",
        })
    return payloads


def rollover_plan(
    client,
    facts: tuple[ParsedFact, ...],
    account_map: dict[str, str],
) -> tuple[list[dict], list[dict], list[tuple[str, str]]]:
    """Split source rollover deposits into a linked known balance and remainder."""
    rows = list(client.iter_activities())
    by_key = {row.get("idempotencyKey"): row for row in rows}
    creates: list[dict] = []
    updates: list[dict] = []
    links: list[tuple[str, str]] = []
    for parsed in facts:
        fact = parsed.fact
        data = parsed.data
        if not isinstance(fact, DecisionFact) or fact.kind != "rollover-reconciliation":
            continue
        source_key = str(data.get("sourceActivityId") or "")
        source_row = by_key.get(source_key)
        if not source_row:
            raise DecisionError(f"rollover source activity is absent: {source_key}")
        source_amount = Decimal(str(data.get("sourceAmount") or "0"))
        linked_amount = Decimal(str(data.get("linkedAmount") or "0"))
        remainder = source_amount - linked_amount
        outgoing_key = f"rebuild:rollover:{fact.id}:out"
        remainder_key = f"rebuild:rollover:{fact.id}:remainder"
        if outgoing_key in by_key:
            has_expected_remainder = (remainder_key in by_key) == (remainder != 0)
            if not has_expected_remainder or not source_row.get("sourceGroupId"):
                raise DecisionError(f"partial rollover reconstruction: {fact.id}")
            continue
        from_id = account_map.get(str(data.get("fromAccountId") or ""))
        to_id = account_map.get(str(data.get("toAccountId") or ""))
        if not from_id or not to_id or source_row.get("accountId") != to_id:
            raise DecisionError(f"rollover account identity mismatch: {fact.id}")
        if Decimal(str(source_row.get("amount") or "0")) != source_amount:
            raise DecisionError(f"rollover source amount mismatch: {fact.id}")
        if linked_amount <= 0 or remainder < 0 or source_row.get("sourceGroupId"):
            raise DecisionError(f"unsafe rollover reconciliation: {fact.id}")

        update = {key: value for key, value in source_row.items() if key != "date"}
        update.update({
            "activityDate": source_row["date"],
            "activityType": "TRANSFER_IN",
            "amount": float(linked_amount),
            "subtype": "internal_transfer",
            "metadata": json.dumps({"flow": {"is_external": False}}),
            "comment": fact.resolution,
            "isDraft": False,
        })
        updates.append(update)
        creates.append({
            "accountId": from_id,
            "activityType": "TRANSFER_OUT",
            "activityDate": source_row["date"],
            "amount": float(linked_amount),
            "currency": source_row["currency"],
            "subtype": "internal_transfer",
            "metadata": json.dumps({"flow": {"is_external": False}}),
            "comment": fact.resolution,
            "idempotencyKey": outgoing_key,
            "isDraft": False,
        })
        if remainder:
            creates.append({
                "accountId": to_id,
                "activityType": "DEPOSIT",
                "activityDate": source_row["date"],
                "amount": float(remainder),
                "currency": source_row["currency"],
                "comment": "Source rollover amount not represented by the opening balance",
                "idempotencyKey": remainder_key,
                "isDraft": False,
            })
        links.append((source_row["id"], outgoing_key))
    return creates, updates, links


def assertion_payloads(
    client,
    canonical_dir: Path,
    account_map: dict[str, str],
    account_types: dict[str, str],
    *,
    closed_accounts: set[str] = frozenset(),
    skip_accounts: set[str] = frozenset(),
) -> tuple[list[dict], dict[str, Decimal]]:
    latest: dict[str, tuple[str, Decimal]] = {}
    for row in _rows(canonical_dir / "valuations.csv"):
        canonical_id = row["entity_id"]
        if canonical_id not in account_map:
            continue
        candidate = (row["date"], Decimal(row["value"]))
        if canonical_id not in latest or candidate[0] > latest[canonical_id][0]:
            latest[canonical_id] = candidate
    as_of = max((when for when, _ in latest.values()), default=date.today().isoformat())
    for canonical_id in closed_accounts & account_map.keys():
        latest[canonical_id] = (as_of, Decimal("0"))
    for canonical_id in skip_accounts - closed_accounts:
        latest.pop(canonical_id, None)
    current = account_values(
        client, [{"id": account_id} for account_id in account_map.values()]
    )
    payloads = []
    differences = {}
    for canonical_id, (as_of, target) in sorted(latest.items()):
        app_id = account_map[canonical_id]
        delta = target - current.get(app_id, Decimal("0"))
        differences[canonical_id] = delta
        if abs(delta) < Decimal("0.005"):
            continue
        account_type = account_types[canonical_id]
        if delta > 0:
            kind = "TRANSFER_IN"
        elif account_type == "CREDIT_CARD":
            kind = "WITHDRAWAL"
        else:
            kind = "TRANSFER_OUT"
        payload = {
            "accountId": app_id,
            "activityType": kind,
            "activityDate": f"{as_of}T23:59:59Z",
            "amount": float(abs(delta)),
            "currency": "USD",
            "isDraft": False,
            "comment": "Canonical current-balance assertion",
            "idempotencyKey": f"rebuild:assertion:{canonical_id}:{as_of}",
        }
        if kind.startswith("TRANSFER_"):
            payload["subtype"] = "external_transfer"
            payload["metadata"] = json.dumps({"flow": {"is_external": True}})
        payloads.append(payload)
    return payloads, differences


def account_values(client, accounts: list[dict]) -> dict[str, Decimal]:
    """Return account values, reconstructing rows omitted by performance."""
    ids = [str(account["id"]) for account in accounts]
    performance = client.post("/performance/accounts/simple", {"accountIds": ids}) or []
    values = {
        str(row["accountId"]): Decimal(str(row.get("totalValue") or 0))
        for row in performance
    }
    missing = set(ids) - values.keys()
    if not missing:
        return values
    values.update({account_id: Decimal() for account_id in missing})
    inflow = {"DEPOSIT", "CREDIT", "TRANSFER_IN", "INTEREST", "DIVIDEND", "SELL"}
    outflow = {"WITHDRAWAL", "TRANSFER_OUT", "FEE", "TAX", "EXPENSE", "BUY"}
    for row in client.iter_activities():
        account_id = str(row.get("accountId") or "")
        if account_id not in missing:
            continue
        amount = Decimal(str(row.get("amount") or 0))
        if row.get("activityType") in inflow:
            values[account_id] += amount
        elif row.get("activityType") in outflow:
            values[account_id] -= amount
    return values
