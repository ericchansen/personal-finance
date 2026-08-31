"""Build current alternative holdings from facts and canonical valuations."""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Protocol

from importers.facts.loader import load_facts
from importers.facts.schema import LoanFact, PropertyFact, VehicleFact

from .decisions import DecisionError


class AssetClient(Protocol):
    def get(self, path: str): ...
    def post(self, path: str, payload: dict): ...


@dataclass(frozen=True)
class AssetPlan:
    creates: tuple[dict, ...]
    existing: tuple[str, ...]


def _metadata(value: object) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _field(row: dict, *names: str):
    return next((row[name] for name in names if name in row), None)


def _latest_valuations(path: Path) -> dict[str, tuple[str, Decimal]]:
    latest: dict[str, tuple[str, Decimal]] = {}
    with path.open(encoding="utf-8-sig", newline="") as source:
        for row in csv.DictReader(source):
            entity = row["entity_id"]
            candidate = (row["date"], Decimal(row["value"]))
            if entity not in latest or candidate[0] > latest[entity][0]:
                latest[entity] = candidate
    return latest


def build_asset_plan(
    client: AssetClient, facts_dir: Path, canonical_valuations: Path
) -> AssetPlan:
    result = load_facts(facts_dir)
    if result.errors:
        raise DecisionError(f"facts are invalid: {result.errors[0]}")
    values = _latest_valuations(canonical_valuations)
    current_rows = client.get("/alternative-holdings") or []
    current: dict[str, dict] = {}
    for row in current_rows:
        folded = str(row["name"]).casefold()
        if folded in current:
            raise DecisionError(f"duplicate alternative holding name: {row['name']}")
        current[folded] = row
    creates: list[dict] = []
    existing: list[str] = []

    assets: list[tuple[str, str, object]] = []
    loans: list[tuple[str, str, object]] = []
    for parsed in result.facts:
        fact = parsed.fact
        if isinstance(fact, PropertyFact) and fact.sale_date is None:
            assets.append((f"property:{fact.name}", "property", fact))
        elif isinstance(fact, VehicleFact):
            assets.append((f"vehicle:{fact.name}", "vehicle", fact))
        elif isinstance(fact, LoanFact) and fact.payoff_date is None:
            loans.append((f"loan:{fact.name}", "liability", fact))

    for entity_id, kind, fact in assets + loans:
        name = fact.name
        if entity_id not in values:
            raise DecisionError(f"no canonical valuation for current holding {entity_id}")
        value_date, value = values[entity_id]
        matched = current.get(name.casefold())
        if matched:
            linked_name = fact.linked_to if isinstance(fact, LoanFact) else None
            linked = current.get(linked_name.casefold()) if linked_name else None
            expected_link = str(linked.get("id")) if linked else None
            mismatches = []
            if matched["name"] != name:
                mismatches.append("name")
            if str(_field(matched, "kind") or "").casefold() != kind:
                mismatches.append("kind")
            if _metadata(matched.get("metadata")).get("canonical_entity_id") != entity_id:
                mismatches.append("canonical identity")
            if Decimal(
                str(_field(matched, "currentValue", "current_value", "marketValue") or 0)
            ) != abs(value):
                mismatches.append("value")
            actual_date = str(
                _field(matched, "valueDate", "value_date", "valuationDate") or ""
            )[:10]
            if actual_date != value_date:
                mismatches.append("value date")
            if isinstance(fact, PropertyFact):
                actual_purchase_price = _field(
                    matched, "purchasePrice", "purchase_price"
                )
                if (
                    Decimal(str(actual_purchase_price))
                    if actual_purchase_price is not None
                    else None
                ) != fact.purchase_price:
                    mismatches.append("purchase price")
                actual_purchase_date = str(
                    _field(matched, "purchaseDate", "purchase_date") or ""
                )[:10]
                if actual_purchase_date != (
                    fact.purchase_date.isoformat() if fact.purchase_date else ""
                ):
                    mismatches.append("purchase date")
            actual_link = _field(matched, "linkedAssetId", "linked_asset_id")
            if (
                linked_name
                and not linked
                or (str(actual_link) if actual_link else None) != expected_link
            ):
                mismatches.append("liability link")
            if mismatches:
                raise DecisionError(
                    f"existing holding {name} differs from canonical: {', '.join(mismatches)}"
                )
            existing.append(name)
            continue
        payload = {
            "kind": kind,
            "name": name,
            "currency": "USD",
            "currentValue": str(abs(value)),
            "valueDate": value_date,
            "metadata": {"canonical_entity_id": entity_id},
        }
        if isinstance(fact, PropertyFact):
            if fact.purchase_price is not None:
                payload["purchasePrice"] = str(fact.purchase_price)
            if fact.purchase_date:
                payload["purchaseDate"] = fact.purchase_date.isoformat()
        if isinstance(fact, LoanFact) and fact.linked_to:
            payload["_linkedName"] = fact.linked_to
        creates.append(payload)
    return AssetPlan(tuple(creates), tuple(sorted(existing)))


def apply_asset_plan(client: AssetClient, plan: AssetPlan) -> None:
    ids = {
        row["name"]: row.get("id", "")
        for row in (client.get("/alternative-holdings") or [])
    }
    for original in plan.creates:
        payload = dict(original)
        linked_name = payload.pop("_linkedName", None)
        if linked_name and ids.get(linked_name):
            payload["linkedAssetId"] = ids[linked_name]
        result = client.post("/alternative-assets", payload)
        ids[payload["name"]] = result.get("assetId") or result.get("id") or ""
