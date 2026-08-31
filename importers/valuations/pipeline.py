"""Build fingerprinted current-valuation plans from explicit private evidence."""

from __future__ import annotations

import csv
import hashlib
import ipaddress
import io
import json
import os
import stat
import urllib.parse
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from importers.facts.loader import load_facts
from importers.facts.schema import LoanFact, PropertyFact, VehicleFact
from importers.rebuild.safety import (
    instance_fingerprint,
    plan_fingerprint,
    validate_private_output,
)
from importers.simplefin.pipeline import load_mapping, read_snapshot


class ValuationError(RuntimeError):
    """Valuation inputs cannot produce a safe plan."""


@dataclass(frozen=True)
class DatedValue:
    entity_id: str
    on: date
    value: Decimal
    currency: str
    source_path: str
    evidence_hashes: tuple[str, ...]
    method: str


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _relative(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        raise ValuationError("valuation evidence must remain under the private data directory")


def _decimal(value: Any, context: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValuationError(f"{context} must be decimal-compatible") from None
    if not result.is_finite():
        raise ValuationError(f"{context} must be finite")
    return result


def _load_explicit_evidence(data_dir: Path, as_of: date) -> dict[str, DatedValue]:
    evidence_dir = data_dir / "valuations" / "evidence"
    candidates: dict[tuple[str, date], DatedValue] = {}
    if not evidence_dir.exists():
        return {}
    for record_path in sorted(evidence_dir.glob("*.json")):
        try:
            payload = json.loads(record_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValuationError(f"invalid valuation evidence record: {record_path.name}: {exc}") from None
        required = ("entityId", "date", "value", "currency", "method", "sourceFile")
        missing = [key for key in required if not payload.get(key)]
        if payload.get("schemaVersion") != 1 or missing:
            raise ValuationError(
                f"{record_path.name} must use schemaVersion 1 and include {', '.join(required)}"
            )
        try:
            observed = date.fromisoformat(str(payload["date"]))
        except ValueError:
            raise ValuationError(f"{record_path.name}: date must be ISO YYYY-MM-DD") from None
        if observed > as_of:
            raise ValuationError(f"{record_path.name}: future-dated evidence is not allowed")
        entity_id = str(payload["entityId"])
        if not entity_id.startswith(("property:", "vehicle:")):
            raise ValuationError(
                f"{record_path.name}: explicit evidence is only accepted for property or vehicle values"
            )
        source_file = Path(str(payload["sourceFile"]))
        if not source_file.is_absolute():
            source_file = data_dir / source_file
        _relative(source_file, data_dir)
        allowed_root = (data_dir / "raw" / "valuations").resolve()
        resolved_source = source_file.resolve()
        if (
            resolved_source == record_path.resolve()
            or (resolved_source != allowed_root and allowed_root not in resolved_source.parents)
        ):
            raise ValuationError(
                f"{record_path.name}: sourceFile must be a distinct artifact under raw/valuations"
            )
        if not source_file.is_file():
            raise ValuationError(f"{record_path.name}: sourceFile does not exist")
        if source_file.stat().st_mode & (
            stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH
        ):
            raise ValuationError(
                f"{record_path.name}: sourceFile must be an immutable read-only artifact"
            )
        value = _decimal(payload["value"], f"{record_path.name}: value")
        if value < 0:
            raise ValuationError(f"{record_path.name}: asset value cannot be negative")
        item = DatedValue(
            entity_id,
            observed,
            value,
            str(payload["currency"]),
            _relative(record_path, data_dir),
            (_sha256(record_path), _sha256(source_file)),
            str(payload["method"]),
        )
        key = (entity_id, observed)
        previous = candidates.get(key)
        if previous and previous.value != value:
            raise ValuationError(
                f"conflicting explicit values for one entity on {observed.isoformat()}"
            )
        candidates[key] = item
    latest: dict[str, DatedValue] = {}
    for item in candidates.values():
        if item.entity_id not in latest or item.on > latest[item.entity_id].on:
            latest[item.entity_id] = item
    return latest


def _fact_values(data_dir: Path, parsed_facts: tuple[Any, ...]) -> dict[str, DatedValue]:
    latest: dict[str, DatedValue] = {}
    for parsed in parsed_facts:
        fact = parsed.fact
        candidates: list[tuple[date, Decimal, str]] = []
        if isinstance(fact, PropertyFact):
            candidates.extend((item.on, item.value, item.kind) for item in fact.appraisals)
        elif (
            isinstance(fact, VehicleFact)
            and fact.current_value_date
            and fact.current_value is not None
        ):
            candidates.append((fact.current_value_date, fact.current_value, "fact-current-value"))
        for observed, value, method in candidates:
            item = DatedValue(
                parsed.fact_id,
                observed,
                value,
                "USD",
                _relative(parsed.path, data_dir),
                (_sha256(parsed.path),),
                method,
            )
            if item.entity_id not in latest or item.on > latest[item.entity_id].on:
                latest[item.entity_id] = item
    return latest


def _latest_snapshot(data_dir: Path, supplied: Path | None) -> Path:
    if supplied:
        resolved = supplied.resolve()
        _relative(resolved, data_dir)
        if not resolved.is_file():
            raise ValuationError("supplied SimpleFIN snapshot does not exist")
        return resolved
    candidates = sorted((data_dir / "raw" / "simplefin").rglob("simplefin-*.json"))
    if not candidates:
        raise ValuationError("no immutable SimpleFIN snapshot is available")
    return candidates[-1]


def _entity(
    entity_id: str,
    kind: str,
    name: str,
    linked_to: str | None,
    value: DatedValue | None,
    period: str,
    reason: str | None = None,
) -> dict[str, Any]:
    current = value is not None and value.on.strftime("%Y-%m") == period and reason is None
    return {
        "canonicalEntityId": entity_id,
        "kind": kind,
        "name": name,
        "linkedTo": linked_to,
        "status": "ready" if current else "review-needed",
        "reason": None if current else reason or "no-explicit-current-month-evidence",
        "quoteDate": value.on.isoformat() if value else None,
        "value": format(value.value, "f") if value else None,
        "currency": value.currency if value else "USD",
        "sourcePath": value.source_path if value else None,
        "method": value.method if value else None,
        "evidenceHashes": list(value.evidence_hashes) if value else [],
    }


def build_refresh_plan(
    data_dir: Path,
    *,
    as_of: date | None = None,
    snapshot_path: Path | None = None,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    """Build a non-mutating monthly refresh plan from canonical private sources."""
    data_dir = data_dir.resolve()
    as_of = as_of or date.today()
    generated_at = generated_at or datetime.now(timezone.utc)
    period = as_of.strftime("%Y-%m")
    facts = load_facts(data_dir / "facts")
    if facts.errors:
        raise ValuationError(f"facts are invalid: {facts.errors[0]}")

    explicit = _load_explicit_evidence(data_dir, as_of)
    prior = _fact_values(data_dir, facts.facts)
    assets: list[dict[str, Any]] = []
    current_loans: dict[str, LoanFact] = {}
    for parsed in facts.facts:
        fact = parsed.fact
        if isinstance(fact, PropertyFact) and fact.sale_date is None:
            value = explicit.get(parsed.fact_id)
            assets.append(
                _entity(
                    parsed.fact_id,
                    "property",
                    fact.name,
                    None,
                    value or prior.get(parsed.fact_id),
                    period,
                    None if value else "no-explicit-current-month-evidence",
                )
            )
        elif isinstance(fact, VehicleFact):
            value = explicit.get(parsed.fact_id)
            assets.append(
                _entity(
                    parsed.fact_id,
                    "vehicle",
                    fact.name,
                    None,
                    value or prior.get(parsed.fact_id),
                    period,
                    None if value else "no-explicit-current-month-evidence",
                )
            )
        elif isinstance(fact, LoanFact) and fact.payoff_date is None:
            current_loans[parsed.fact_id] = fact

    snapshot = _latest_snapshot(data_dir, snapshot_path)
    accounts, errors = read_snapshot(snapshot)
    if errors:
        raise ValuationError("latest SimpleFIN snapshot contains institution errors")
    mapping = load_mapping(data_dir)
    monitor: dict[str, list[tuple[Any, str]]] = {}
    for account in accounts:
        entry = mapping.get(account.id)
        if entry and entry.get("action") == "monitor":
            monitor.setdefault(str(entry.get("assertionAccountId") or ""), []).append(
                (account, str(entry.get("wealthfolioAlternativeAssetId") or ""))
            )

    loans: list[dict[str, Any]] = []
    for entity_id, fact in current_loans.items():
        matches = monitor.get(entity_id, [])
        value = None
        reason = None
        if len(matches) != 1:
            reason = "missing-unique-simplefin-monitor-source"
            target_id = None
        else:
            account, target_id = matches[0]
            if not target_id:
                reason = "simplefin-monitor-has-no-alternative-target"
            elif account.balance_date is None:
                reason = "simplefin-monitor-balance-has-no-date"
            elif account.balance_date > as_of:
                reason = "simplefin-monitor-balance-is-future-dated"
            elif account.balance > 0:
                reason = "simplefin-liability-balance-sign-is-positive"
            else:
                value = DatedValue(
                    entity_id,
                    account.balance_date,
                    account.balance,
                    account.currency,
                    _relative(snapshot, data_dir),
                    (_sha256(snapshot), _sha256(data_dir / "simplefin" / "account-map.json")),
                    "simplefin-monitor-balance",
                )
        loan = _entity(
            entity_id,
            "liability",
            fact.name,
            fact.linked_to or None,
            value,
            period,
            reason,
        )
        loan["wealthfolioTargetId"] = target_id
        loans.append(loan)

    entities = sorted(assets + loans, key=lambda item: item["canonicalEntityId"])
    canonical_rows = [
        {
            "date": item["quoteDate"],
            "entity_id": item["canonicalEntityId"],
            "value": item["value"],
            "currency": item["currency"],
            "source_file": item["sourcePath"],
            "observed_or_derived": "observed",
        }
        for item in entities
        if item["status"] == "ready"
    ]
    material = {
        "schemaVersion": 1,
        "mode": "plan-only",
        "period": period,
        "asOf": as_of.isoformat(),
        "generatedAt": generated_at.isoformat(),
        "sourceSnapshot": _relative(snapshot, data_dir),
        "sourceSnapshotSha256": _sha256(snapshot),
        "ready": all(item["status"] == "ready" for item in entities),
        "reviewNeeded": sum(item["status"] == "review-needed" for item in entities),
        "entities": entities,
        "canonicalRows": canonical_rows,
    }
    return {**material, "fingerprint": plan_fingerprint(material)}


def validate_refresh_plan(plan: dict[str, Any]) -> None:
    supplied = plan.get("fingerprint")
    material = {key: value for key, value in plan.items() if key != "fingerprint"}
    if supplied != plan_fingerprint(material):
        raise ValuationError("refresh plan fingerprint is invalid")


def write_refresh_outputs(
    data_dir: Path, repo_root: Path, plan: dict[str, Any]
) -> tuple[Path, Path]:
    """Write immutable private plan and canonical-schema monthly rows."""
    validate_refresh_plan(plan)
    folder = data_dir / "normalized" / "current-valuations" / plan["period"]
    validate_private_output(folder, data_dir, repo_root)
    folder.mkdir(parents=True, exist_ok=True)
    suffix = plan["fingerprint"][:16]
    plan_path = folder / f"plan-{plan['asOf']}-{suffix}.json"
    csv_path = folder / f"valuations-{plan['asOf']}-{suffix}.csv"
    plan_bytes = (json.dumps(plan, indent=2, sort_keys=True) + "\n").encode()
    output = io.StringIO(newline="")
    columns = (
        "date", "entity_id", "value", "currency", "source_file", "observed_or_derived"
    )
    writer = csv.DictWriter(output, fieldnames=columns, lineterminator="\n")
    writer.writeheader()
    writer.writerows(plan["canonicalRows"])
    csv_bytes = output.getvalue().encode()
    for path, content in ((plan_path, plan_bytes), (csv_path, csv_bytes)):
        _write_immutable_bytes(path, content)
    return plan_path, csv_path


def _write_immutable_bytes(path: Path, content: bytes) -> None:
    if path.exists():
        if path.read_bytes() != content:
            raise ValuationError(
                f"refusing to overwrite changed immutable output: {path.name}"
            )
        path.chmod(0o444)
        return
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    except FileExistsError:
        if path.read_bytes() != content:
            raise ValuationError(
                f"refusing to overwrite changed immutable output: {path.name}"
            ) from None
        path.chmod(0o444)
        return
    with os.fdopen(fd, "wb") as target:
        target.write(content)
        target.flush()
        os.fsync(target.fileno())
    path.chmod(0o444)


def write_immutable_json(path: Path, payload: dict[str, Any]) -> None:
    content = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode()
    _write_immutable_bytes(path, content)


def _holding_material(row: dict[str, Any]) -> dict[str, Any]:
    metadata = row.get("metadata")
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except json.JSONDecodeError:
            metadata = {}
    return {
        "id": str(row.get("id") or ""),
        "kind": str(row.get("kind") or ""),
        "name": str(row.get("name") or ""),
        "currency": str(row.get("currency") or ""),
        "marketValue": str(row.get("marketValue") or row.get("currentValue") or "0"),
        "valuationDate": str(
            row.get("valuationDate") or row.get("valueDate") or ""
        )[:10],
        "canonicalEntityId": (metadata or {}).get("canonical_entity_id"),
        "linkedAssetId": row.get("linkedAssetId"),
    }


def holding_fingerprint(row: dict[str, Any]) -> str:
    return plan_fingerprint(_holding_material(row))


def quote_on(client: Any, asset_id: str, on: str) -> dict[str, Any] | None:
    path = "/market-data/quotes/history?symbol=" + urllib.parse.quote(asset_id)
    response = client.get(path) or []
    rows = response.get("data", []) if isinstance(response, dict) else response
    for row in rows:
        if not isinstance(row, dict):
            continue
        observed = str(
            row.get("timestamp") or row.get("date") or row.get("day") or ""
        )[:10]
        if observed == on:
            return dict(row)
    return None


def validate_projection_target(base_url: str) -> None:
    target = urllib.parse.urlparse(base_url)
    try:
        is_loopback = (
            target.hostname == "localhost"
            or ipaddress.ip_address(target.hostname or "").is_loopback
        )
    except ValueError:
        is_loopback = False
    if (
        target.scheme not in {"http", "https"}
        or not is_loopback
        or target.port in {None, 8088}
    ):
        raise ValuationError(
            "valuation projection requires loopback staging on a non-production port"
        )


def build_projection_plan(
    client: Any,
    base_url: str,
    refresh_plan: dict[str, Any],
) -> dict[str, Any]:
    """Bind a refresh plan to exact holdings on a loopback staging instance."""
    validate_refresh_plan(refresh_plan)
    validate_projection_target(base_url)
    holdings = client.get("/alternative-holdings") or []
    by_id = {
        _holding_material(holding)["id"]: holding
        for holding in holdings
        if _holding_material(holding)["id"]
    }
    by_canonical: dict[str, list[dict[str, Any]]] = {}
    untagged_by_name_kind: dict[tuple[str, str], list[dict[str, Any]]] = {}
    tagged_by_name_kind: dict[tuple[str, str], list[dict[str, Any]]] = {}
    ids_by_name: dict[str, list[str]] = {}
    for holding in holdings:
        state = _holding_material(holding)
        entity_id = state["canonicalEntityId"]
        if entity_id:
            by_canonical.setdefault(str(entity_id), []).append(holding)
            tagged_by_name_kind.setdefault(
                (state["name"], state["kind"]), []
            ).append(holding)
        else:
            untagged_by_name_kind.setdefault(
                (state["name"], state["kind"]), []
            ).append(holding)
        ids_by_name.setdefault(state["name"], []).append(state["id"])
    comparisons = []
    blockers = [
        {
            "code": "valuation-review-needed",
            "canonicalEntityId": item["canonicalEntityId"],
            "reason": item["reason"],
        }
        for item in refresh_plan["entities"]
        if item["status"] != "ready"
    ]
    for item in refresh_plan["entities"]:
        identity_conflicts = [
            holding
            for holding in tagged_by_name_kind.get(
                (item["name"], item["kind"]), []
            )
            if _holding_material(holding)["canonicalEntityId"]
            != item["canonicalEntityId"]
        ]
        if identity_conflicts:
            blockers.append({
                "code": "holding-canonical-identity-conflict",
                "canonicalEntityId": item["canonicalEntityId"],
                "count": len(identity_conflicts),
            })
            continue
        target_id = item.get("wealthfolioTargetId")
        if target_id:
            matches = [by_id[target_id]] if target_id in by_id else []
            identity_mode = "simplefin-target-id"
        else:
            matches = by_canonical.get(item["canonicalEntityId"], [])
            identity_mode = "canonical-metadata"
        if not matches and not target_id:
            matches = untagged_by_name_kind.get((item["name"], item["kind"]), [])
            identity_mode = "exact-name-and-kind"
        if len(matches) != 1:
            blockers.append({
                "code": "holding-identity-mismatch",
                "canonicalEntityId": item["canonicalEntityId"],
                "count": len(matches),
            })
            continue
        holding = matches[0]
        state = _holding_material(holding)
        expected_kind = item["kind"]
        linked_ids = ids_by_name.get(item["linkedTo"], []) if item["linkedTo"] else []
        expected_link = linked_ids[0] if len(linked_ids) == 1 else None
        if (
            state["name"] != item["name"]
            or state["kind"] != expected_kind
            or state["currency"] != item["currency"]
            or (
                state["canonicalEntityId"]
                and state["canonicalEntityId"] != item["canonicalEntityId"]
            )
            or (item["linkedTo"] and len(linked_ids) != 1)
            or state["linkedAssetId"] != expected_link
        ):
            blockers.append({
                "code": "holding-state-mismatch",
                "canonicalEntityId": item["canonicalEntityId"],
            })
            continue
        if item["status"] != "ready":
            continue
        target_value = abs(Decimal(item["value"]))
        current_value = Decimal(state["marketValue"])
        sign = Decimal("-1") if expected_kind == "liability" else Decimal("1")
        previous_target = quote_on(client, state["id"], item["quoteDate"])
        if previous_target is not None:
            blockers.append({
                "code": "target-date-quote-already-exists",
                "canonicalEntityId": item["canonicalEntityId"],
            })
            continue
        if (
            current_value == target_value
            and state["valuationDate"] == item["quoteDate"]
        ):
            continue
        comparisons.append({
            "canonicalEntityId": item["canonicalEntityId"],
            "assetId": state["id"],
            "identityMode": identity_mode,
            "name": state["name"],
            "kind": expected_kind,
            "expectedHoldingFingerprint": holding_fingerprint(holding),
            "target": {
                "date": item["quoteDate"],
                "value": format(target_value, "f"),
                "currency": item["currency"],
            },
            "current": {
                "date": state["valuationDate"],
                "value": format(current_value, "f"),
            },
            "netWorthDelta": format(sign * (target_value - current_value), "f"),
            "evidenceHashes": item["evidenceHashes"],
        })
    material = {
        "schemaVersion": 1,
        "mode": "staging-read-only-comparison",
        "refreshFingerprint": refresh_plan["fingerprint"],
        "environmentFingerprint": instance_fingerprint(client, base_url),
        "ready": not blockers,
        "blockers": blockers,
        "comparisons": comparisons,
        "readOnly": True,
        "applySupported": False,
        "upstreamLimitation": "no-conditional-quote-create-or-exclusive-write",
    }
    return {**material, "fingerprint": plan_fingerprint(material)}
