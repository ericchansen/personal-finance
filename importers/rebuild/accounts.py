"""Reconcile fresh-instance accounts to durable canonical identities."""

from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass
from pathlib import Path

from importers.facts.loader import load_facts
from importers.facts.schema import AccountFact
from importers.normalized.builder import (
    ACCOUNT_COLUMNS,
    BuildError,
    verify_publication,
)
from importers.simplefin.client import parse_accounts

from .decisions import DecisionError


WEALTHFOLIO_TYPES = {"CASH", "CREDIT_CARD", "SECURITIES", "CRYPTOCURRENCY"}


@dataclass(frozen=True)
class CanonicalAccount:
    account_id: str
    name: str
    kind: str
    closed: bool
    excluded: bool
    tracking_mode: str = "TRANSACTIONS"


@dataclass(frozen=True)
class AccountPlan:
    updates: tuple[tuple[str, dict], ...]
    creates: tuple[dict, ...]
    canonical_to_existing: dict[str, str]


def load_canonical_accounts(path: Path) -> dict[str, CanonicalAccount]:
    try:
        verify_publication(path.parent.parent.parent)
    except (BuildError, OSError, json.JSONDecodeError) as exc:
        raise DecisionError(f"canonical verification failed: {exc}") from exc

    result = {}
    with path.open(encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        if tuple(reader.fieldnames or ()) != ACCOUNT_COLUMNS:
            raise DecisionError("accounts.csv columns do not match canonical schema")
        for row in reader:
            tracking_mode = row["tracking_mode"].strip()
            if tracking_mode not in {"TRANSACTIONS", "HOLDINGS"}:
                raise DecisionError("accounts.csv contains invalid tracking_mode")
            if row["kind"] not in WEALTHFOLIO_TYPES:
                continue
            result[row["account_id"]] = CanonicalAccount(
                row["account_id"],
                row["name"],
                row["kind"],
                bool(row["closed"]),
                row["excluded"].casefold() == "true",
                tracking_mode,
            )
    return result


def _last4(value: str | None) -> str:
    digits = re.sub(r"\D", "", value or "")
    return digits[-4:] if len(digits) >= 4 else ""


def _tokens(value: str) -> set[str]:
    return {
        token
        for token in re.sub(r"[^a-z0-9]+", " ", value.casefold()).split()
        if len(token) >= 3
    }


def build_aliases(data_dir: Path, canonical: dict[str, CanonicalAccount]) -> dict[str, str]:
    aliases = {row.name.casefold(): row.account_id for row in canonical.values()}
    monarch = json.loads(
        (data_dir / "normalized" / "monarch-account-map.json").read_text(encoding="utf-8")
    )
    aliases.update(
        {
            str(name).casefold(): account_id
            for name, account_id in monarch.items()
            if account_id in canonical
        }
    )

    mapping = json.loads(
        (data_dir / "simplefin" / "account-map.json").read_text(encoding="utf-8")
    )["accounts"]
    snapshot = sorted((data_dir / "raw" / "simplefin").rglob("simplefin-*.json"))[-1]
    source_accounts, errors = parse_accounts(json.loads(snapshot.read_text(encoding="utf-8")))
    if errors:
        raise DecisionError("latest SimpleFIN snapshot contains institution errors")
    for source in source_accounts:
        target = mapping.get(source.id, {}).get("assertionAccountId")
        if target in canonical:
            aliases[source.name.casefold()] = target

    facts = load_facts(data_dir / "facts")
    by_last4: dict[str, list[str]] = {}
    for parsed in facts.facts:
        if isinstance(parsed.fact, AccountFact):
            suffix = _last4(parsed.fact.masked_number)
            if suffix and parsed.fact.id in canonical:
                by_last4.setdefault(suffix, []).append(parsed.fact.id)
    for relative in ("extracts/fidelity/mapping.json", "extracts/vanguard/mapping.json"):
        document = json.loads((data_dir / relative).read_text(encoding="utf-8"))
        for number, spec in document.get("accounts", {}).items():
            matches = by_last4.get(_last4(number), [])
            if len(matches) == 1:
                aliases[str(spec["name"]).casefold()] = matches[0]
                continue
            source_tokens = _tokens(str(spec["name"]))
            scored = [
                (len(source_tokens & _tokens(row.name)), row.account_id)
                for row in canonical.values()
            ]
            best = max((score for score, _ in scored), default=0)
            best_ids = [account_id for score, account_id in scored if score == best]
            if best >= 2 and len(best_ids) == 1:
                aliases[str(spec["name"]).casefold()] = best_ids[0]

    fidelity = json.loads(
        (data_dir / "extracts" / "fidelity" / "mapping.json").read_text(encoding="utf-8")
    )
    espp = fidelity.get("espp", {}).get("name")
    espp_matches = [
        row.account_id
        for row in canonical.values()
        if "espp" in row.name.casefold()
    ]
    if espp and len(espp_matches) == 1:
        aliases[str(espp).casefold()] = espp_matches[0]
    return aliases


def build_account_plan(
    existing: list[dict],
    canonical: dict[str, CanonicalAccount],
    aliases: dict[str, str],
) -> AccountPlan:
    mapped: dict[str, str] = {}
    updates = []
    for account in existing:
        canonical_id = aliases.get(str(account.get("name") or "").casefold())
        if canonical_id is None:
            raise DecisionError(f"existing account has no durable identity: {account.get('name')}")
        if canonical_id in mapped:
            raise DecisionError(f"multiple app accounts map to {canonical_id}")
        target = canonical[canonical_id]
        mapped[canonical_id] = account["id"]
        desired = {
            "name": target.name,
            "accountType": target.kind,
            "currency": account.get("currency", "USD"),
            "isActive": not target.closed and not target.excluded,
            "isDefault": account.get("isDefault", False),
            "group": account.get("group"),
            "trackingMode": target.tracking_mode,
        }
        if any(account.get(key) != value for key, value in desired.items()):
            updates.append((account["id"], desired))

    creates = []
    for canonical_id, target in canonical.items():
        if canonical_id in mapped or target.excluded:
            continue
        creates.append(
            {
                "_canonicalId": canonical_id,
                "name": target.name,
                "account_type": target.kind,
                "group": "Crypto" if target.kind == "CRYPTOCURRENCY" else None,
                "is_active": not target.closed,
                "tracking_mode": target.tracking_mode,
            }
        )
    return AccountPlan(tuple(updates), tuple(creates), mapped)


def apply_account_plan(client, plan: AccountPlan) -> dict[str, str]:
    mapping = dict(plan.canonical_to_existing)
    for account_id, fields in plan.updates:
        client.update_account(account_id, **fields)
    for item in plan.creates:
        payload = dict(item)
        canonical_id = payload.pop("_canonicalId")
        created = client.create_account(**payload)
        mapping[canonical_id] = created["id"]
    return mapping
