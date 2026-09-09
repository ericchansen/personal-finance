"""Deterministic, read-only duplicate and lineage analysis of a verified baseline."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import shutil
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from itertools import combinations
from pathlib import Path
from typing import Any, Iterable

from importers.analytics.publication import ensure_durable_directory, fsync_directory
from importers.rebuild.safety import plan_fingerprint, validate_private_output
from importers.simplefin.application import INFLOW, OUTFLOW
from importers.simplefin.pipeline import normalize_description

from . import baseline

SCHEMA_VERSION = 1
POINTER_SCHEMA_VERSION = 1
OUTPUT_RELATIVE = Path("audit") / "duplicates"
DATE_WINDOW_DAYS = 3
TRANSFER_WINDOW_DAYS = 5
RECONCILIATION_PREFIXES = (
    "gap:",
    "rebuild:assertion:",
    "fidelity:reconcile:",
    "monarch:opening:",
)
SUPPORTED_EVIDENCE_KINDS = frozenset(
    {"raw-snapshot", "plan", "receipt", "canonical-publication"}
)
SOURCE_FAMILIES = frozenset(
    {
        "canonical",
        "extract",
        "fidelity",
        "gap",
        "ledger",
        "manual",
        "monarch",
        "rebuild",
        "simplefin",
        "vanguard",
        "vanguard-history",
    }
)
ACTIVITY_COLUMNS = (
    "activity_ref",
    "activity_id",
    "canonical_account_id",
    "source_date",
    "signed_effect",
    "scope_reason",
    "source_family",
    "source_identity",
    "description",
    "normalized_description",
    "transfer_group",
    "candidate_group_ids",
    "dependent_reason_codes",
    "lineage_status",
    "raw_json",
)
CANDIDATE_COLUMNS = (
    "candidate_id",
    "group_id",
    "left_activity_ref",
    "right_activity_ref",
    "tier",
    "classification",
    "reason_codes",
    "source_family_pair",
    "ambiguity_cardinality",
)
SAFE_REASON = re.compile(r"^[a-z0-9-]+$")
HEX_64 = re.compile(r"^[0-9a-f]{64}$")


class ForensicAuditError(RuntimeError):
    """The audit inputs or publication cannot be trusted."""


@dataclass(frozen=True)
class Activity:
    ref: str
    activity_id: str
    ledger_account_id: str
    account_id: str
    currency: str
    source_at: str
    source_date: str
    effect: Decimal | None
    scope_reason: str | None
    source_family: str
    source_identity: str
    description: str
    normalized_description: str
    transfer_group: str
    connection_id: str
    institution: str
    account_closed: bool
    reconciliation: bool
    raw: dict[str, Any]


@dataclass(frozen=True)
class Edge:
    left: str
    right: str
    tier: int
    classification: str
    reasons: tuple[str, ...]


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _stable_ref(*values: str) -> str:
    return _sha256_bytes("\x1f".join(values).encode("utf-8"))


def _money(value: Decimal) -> str:
    return format(value, "f")


def _decimal(value: Any, label: str) -> Decimal:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ForensicAuditError(f"{label} is not an exact decimal") from None
    if not amount.is_finite():
        raise ForensicAuditError(f"{label} is not finite")
    return amount


def _utc_source_date(value: Any) -> tuple[str, str]:
    raw = str(value or "").strip()
    if not raw:
        raise ForensicAuditError("activity has no source date")
    try:
        if len(raw) == 10:
            parsed = datetime.fromisoformat(raw).replace(tzinfo=timezone.utc)
        else:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                raise ValueError
            parsed = parsed.astimezone(timezone.utc)
    except ValueError:
        raise ForensicAuditError("activity source date is not timezone-safe") from None
    return parsed.isoformat().replace("+00:00", "Z"), parsed.date().isoformat()


def _metadata(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def _find_named(value: Any, names: set[str]) -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = "".join(character for character in str(key).casefold() if character.isalnum())
            if normalized in names and child not in (None, ""):
                found.append(str(child))
            found.extend(_find_named(child, names))
    elif isinstance(value, (list, tuple)):
        for child in value:
            found.extend(_find_named(child, names))
    return found


def _source_identity(row: dict[str, Any]) -> tuple[str, str]:
    key = str(
        row.get("idempotencyKey")
        or row.get("sourceId")
        or row.get("source_id")
        or ""
    ).strip()
    source = str(row.get("sourceSystem") or row.get("source") or "").strip()
    key_prefix = key.partition(":")[0].casefold() if ":" in key else ""
    source_family = re.sub(r"[^a-z0-9-]+", "-", source.casefold()).strip("-")
    family = (
        key_prefix
        if key_prefix in SOURCE_FAMILIES
        else source_family
        if source_family in SOURCE_FAMILIES
        else "unknown"
    )
    return family, key


def _cash_effect(row: dict[str, Any]) -> tuple[Decimal | None, str | None]:
    kind = str(row.get("activityType") or row.get("type") or "").upper()
    if kind not in INFLOW | OUTFLOW:
        reason = (
            "non-cash-investment-activity"
            if kind in {"BUY", "SELL"}
            else "unsupported-cash-semantics"
        )
        return None, reason
    amount = abs(_decimal(row.get("amount", 0), "activity amount"))
    if kind in INFLOW:
        return amount, None
    return -amount, None


def _account_closed(row: dict[str, Any]) -> bool:
    status = str(row.get("status") or "").casefold()
    return bool(
        row.get("isArchived")
        or row.get("archived")
        or row.get("closedAt")
        or row.get("closed")
        or status in {"closed", "archived", "inactive"}
    )


def _canonical_account_id(
    row: dict[str, Any],
    account: dict[str, Any],
    aliases: dict[str, str],
) -> str:
    ledger_id = str(row.get("accountId") or "")
    return str(
        row.get("canonicalAccountId")
        or account.get("canonicalAccountId")
        or account.get("externalId")
        or aliases.get(ledger_id)
        or ledger_id
        or ""
    )


def _normalize_activities(
    rows: list[dict[str, Any]],
    accounts: list[dict[str, Any]],
    baseline_id: str,
    canonical_aliases: dict[str, str] | None = None,
) -> list[Activity]:
    account_ids = [str(row.get("id") or "") for row in accounts]
    if "" in account_ids or len(set(account_ids)) != len(account_ids):
        raise ForensicAuditError("baseline accounts have missing or duplicate IDs")
    account_by_id = dict(zip(account_ids, accounts))
    result: list[Activity] = []
    seen_ids: set[str] = set()
    for row in rows:
        activity_id = str(row.get("id") or "")
        if not activity_id or activity_id in seen_ids:
            raise ForensicAuditError("baseline activities have missing or duplicate IDs")
        seen_ids.add(activity_id)
        raw_account_id = str(row.get("accountId") or "")
        account = account_by_id.get(raw_account_id)
        if account is None:
            raise ForensicAuditError("activity references an unknown account")
        account_id = _canonical_account_id(row, account, canonical_aliases or {})
        if not account_id:
            raise ForensicAuditError("activity has no canonical account identity")
        source_at, source_date = _utc_source_date(
            row.get("sourceDate") or row.get("date") or row.get("activityDate")
        )
        description = str(
            row.get("description")
            or row.get("comment")
            or row.get("payee")
            or ""
        )
        family, identity = _source_identity(row)
        effect, scope_reason = _cash_effect(row)
        currency = str(row.get("currency") or account.get("currency") or "").upper()
        if effect is not None and not re.fullmatch(r"[A-Z]{3}", currency):
            raise ForensicAuditError("activity has no normalized currency")
        metadata = _metadata(row.get("metadata"))
        connection = _find_named(
            (row, account, metadata),
            {"connectionid", "sourceconnectionid", "providerconnectionid"},
        )
        institution = str(
            account.get("institution")
            or account.get("group")
            or account.get("provider")
            or ""
        ).casefold()
        result.append(
            Activity(
                ref=_stable_ref(baseline_id, activity_id),
                activity_id=activity_id,
                ledger_account_id=raw_account_id,
                account_id=account_id,
                currency=currency,
                source_at=source_at,
                source_date=source_date,
                effect=effect,
                scope_reason=scope_reason,
                source_family=family,
                source_identity=identity,
                description=description,
                normalized_description=normalize_description(description),
                transfer_group=str(row.get("sourceGroupId") or ""),
                connection_id=connection[0] if connection else "",
                institution=institution,
                account_closed=_account_closed(account),
                reconciliation=identity.startswith(RECONCILIATION_PREFIXES),
                raw=row,
            )
        )
    return sorted(result, key=lambda item: item.ref)


def _days(left: Activity, right: Activity) -> int:
    return abs(
        (
            datetime.fromisoformat(left.source_date)
            - datetime.fromisoformat(right.source_date)
        ).days
    )


def _description_similar(left: str, right: str) -> bool:
    if not left or not right:
        return False
    if left.startswith(right) or right.startswith(left):
        return min(len(left), len(right)) >= 4
    left_tokens = set(left.split())
    right_tokens = set(right.split())
    if not left_tokens or not right_tokens:
        return False
    return len(left_tokens & right_tokens) / len(left_tokens | right_tokens) >= 0.5


def _add_edge(
    edges: dict[tuple[str, str], Edge],
    left: Activity,
    right: Activity,
    tier: int,
    classification: str,
    *reasons: str,
) -> None:
    if left.ref == right.ref:
        return
    key = tuple(sorted((left.ref, right.ref)))
    existing = edges.get(key)
    combined = tuple(sorted(set(reasons) | (set(existing.reasons) if existing else set())))
    if existing and existing.tier < tier:
        edges[key] = Edge(existing.left, existing.right, existing.tier, existing.classification, combined)
        return
    edges[key] = Edge(
        key[0],
        key[1],
        tier,
        "automatic-duplicate" if tier == 1 else classification,
        combined,
    )


def _detect_edges(activities: list[Activity]) -> list[Edge]:
    edges: dict[tuple[str, str], Edge] = {}
    by_identity: dict[tuple[str, str, str], list[Activity]] = defaultdict(list)
    by_exact: dict[tuple[str, str, str, Decimal, str], list[Activity]] = defaultdict(list)
    by_account_amount: dict[tuple[str, str, Decimal], list[Activity]] = defaultdict(list)
    by_abs_amount: dict[tuple[str, Decimal], list[Activity]] = defaultdict(list)
    by_transfer: dict[str, list[Activity]] = defaultdict(list)
    for activity in activities:
        if activity.effect is None:
            continue
        if activity.source_identity and activity.source_family != "unknown":
            by_identity[
                (
                    activity.account_id,
                    activity.source_family,
                    activity.source_identity,
                )
            ].append(activity)
        by_exact[
            (
                activity.account_id,
                activity.currency,
                activity.source_date,
                activity.effect,
                activity.normalized_description,
            )
        ].append(activity)
        by_account_amount[
            (activity.account_id, activity.currency, activity.effect)
        ].append(activity)
        by_abs_amount[(activity.currency, abs(activity.effect))].append(activity)
        if activity.transfer_group:
            by_transfer[activity.transfer_group].append(activity)

    for group in by_identity.values():
        for left, right in combinations(group, 2):
            _add_edge(edges, left, right, 1, "automatic-duplicate", "exact-source-identity")

    for group in by_exact.values():
        for left, right in combinations(group, 2):
            if left.source_family != right.source_family:
                _add_edge(edges, left, right, 2, "review-required", "exact-cross-source")

    for group in by_account_amount.values():
        ordered = sorted(group, key=lambda item: item.source_date)
        for left, right in combinations(ordered, 2):
            if left.source_family == right.source_family or _days(left, right) > DATE_WINDOW_DAYS:
                continue
            if (
                left.source_date == right.source_date
                and left.normalized_description == right.normalized_description
            ):
                continue
            reasons = ["bounded-date-equal-amount"]
            similar = _description_similar(
                left.normalized_description, right.normalized_description
            )
            if similar:
                reasons.append("provider-description-similar")
            _add_edge(
                edges,
                left,
                right,
                4 if similar else 3,
                "review-required",
                *reasons,
            )

    for group in by_transfer.values():
        for left, right in combinations(group, 2):
            if (
                left.account_id != right.account_id
                and left.effect != 0
                and right.effect != 0
                and left.effect == -right.effect
            ):
                _add_edge(
                    edges,
                    left,
                    right,
                    6,
                    "relationship-only",
                    "linked-transfer",
                )
            else:
                _add_edge(
                    edges,
                    left,
                    right,
                    6,
                    "review-required",
                    "source-group-conflict",
                )

    for group in by_abs_amount.values():
        ordered = sorted(group, key=lambda item: item.source_date)
        for left, right in combinations(ordered, 2):
            if left.account_id == right.account_id:
                continue
            date_distance = _days(left, right)
            if (
                date_distance <= TRANSFER_WINDOW_DAYS
                and left.effect != 0
                and right.effect != 0
                and left.effect == -right.effect
            ):
                _add_edge(
                    edges,
                    left,
                    right,
                    6,
                    "relationship-only",
                    "transfer-candidate",
                )
            if (
                date_distance <= DATE_WINDOW_DAYS
                and left.effect == right.effect
                and left.connection_id
                and left.connection_id == right.connection_id
            ):
                _add_edge(
                    edges,
                    left,
                    right,
                    5,
                    "review-required",
                    "same-connection-cross-account-mirror",
                )
            if (
                left.source_date == right.source_date
                and left.effect == right.effect
                and left.normalized_description == right.normalized_description
                and (left.account_closed or right.account_closed)
                and left.institution
                and left.institution == right.institution
            ):
                _add_edge(
                    edges,
                    left,
                    right,
                    7,
                    "review-required",
                    "closed-reissued-overlap",
                )
    return sorted(edges.values(), key=lambda edge: (edge.left, edge.right))


def _candidate_groups(
    edges: list[Edge], by_ref: dict[str, Activity]
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    neighbors: dict[str, set[str]] = defaultdict(set)
    for edge in edges:
        neighbors[edge.left].add(edge.right)
        neighbors[edge.right].add(edge.left)
    groups: list[dict[str, Any]] = []
    group_by_ref: dict[str, str] = {}
    remaining = set(neighbors)
    while remaining:
        start = min(remaining)
        stack = [start]
        members: set[str] = set()
        while stack:
            current = stack.pop()
            if current in members:
                continue
            members.add(current)
            stack.extend(neighbors[current] - members)
        remaining -= members
        member_edges = [
            edge for edge in edges if edge.left in members and edge.right in members
        ]
        family_counts = Counter(by_ref[ref].source_family for ref in members)
        non_singletons = sum(count > 1 for count in family_counts.values())
        if len(members) == 2:
            cardinality = "one-to-one"
        elif non_singletons <= 1:
            cardinality = "one-to-many"
        else:
            cardinality = "many-to-many"
        if all(
            edge.classification == "automatic-duplicate"
            for edge in member_edges
        ):
            classification = "automatic-duplicate"
        elif all(
            edge.classification == "relationship-only"
            for edge in member_edges
        ):
            classification = "relationship-only"
        else:
            classification = "review-required"
        group_id = _stable_ref("candidate-group", *sorted(members))
        for ref in members:
            group_by_ref[ref] = group_id
        groups.append(
            {
                "groupId": group_id,
                "classification": classification,
                "cardinality": cardinality,
                "ambiguityCardinality": len(members),
                "activityRefs": sorted(members),
                "sourceFamilyCounts": dict(sorted(family_counts.items())),
                "reasonCodes": sorted(
                    {reason for edge in member_edges for reason in edge.reasons}
                ),
                "edgeCount": len(member_edges),
            }
        )
    return sorted(groups, key=lambda group: group["groupId"]), group_by_ref


def _domain(publication: Path, manifest: dict[str, Any], name: str) -> Any:
    path = publication / "domains" / f"{name}.json"
    try:
        content = path.read_bytes()
        reference = manifest["domainFiles"][f"{name}.json"]
        if (
            len(content) != path.stat().st_size
            or _sha256_bytes(content) != reference["sha256"]
        ):
            raise ForensicAuditError(
                f"verified baseline domain changed while reading: {name}"
            )
        document = json.loads(content)
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ForensicAuditError(f"cannot read verified baseline domain: {name}") from exc
    if document.get("status") != "available":
        return None
    return document.get("records")


def _dependent_state(
    activity: Activity,
    assignments: dict[str, Any],
    splits: dict[str, Any],
    transfer_pairs: dict[str, Any],
    spending_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    activity_id = activity.activity_id
    linked_spending = [
        row
        for row in spending_rows
        if str(row.get("id") or row.get("activityId") or row.get("activity_id") or "")
        == activity_id
    ]
    assignment = assignments.get(activity_id, [])
    split = splits.get(activity_id, [])
    transfer = transfer_pairs.get(activity_id)
    metadata = _metadata(activity.raw.get("metadata"))
    note = (
        activity.raw.get("note")
        or activity.raw.get("userNote")
        or activity.raw.get("user_note")
    )
    direct_activity_state = {
        key: value for key, value in activity.raw.items() if key != "metadata"
    }
    spending_state = [
        {
            **{key: value for key, value in row.items() if key != "metadata"},
            "metadata": _metadata(row.get("metadata")),
        }
        for row in linked_spending
    ]
    run_ids = sorted(
        set(
            _find_named(
                (direct_activity_state, metadata, spending_state),
                {
                    "projectionid",
                    "projectionrunid",
                    "importrunid",
                    "importid",
                    "runid",
                },
            )
        )
    )
    reasons = []
    for present, reason in (
        (bool(transfer) or bool(activity.transfer_group), "transfer-counterpart"),
        (bool(assignment), "category-assignment"),
        (bool(split), "split"),
        (
            any(row.get("eventId") or row.get("event_id") for row in linked_spending),
            "spending-event-link",
        ),
        (bool(note), "user-note"),
        (metadata not in (None, "", {}), "metadata"),
        (bool(run_ids), "projection-import-run"),
    ):
        if present:
            reasons.append(reason)
    return {
        "reasonCodes": reasons,
        "wouldLoseOrCascade": bool(reasons),
        "transferCounterpart": transfer,
        "categoryAssignments": assignment,
        "splits": split,
        "spendingEventLinks": linked_spending,
        "userNote": note,
        "metadata": metadata,
        "projectionImportRunIds": run_ids,
    }


def _safe_evidence_path(root: Path, value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = root / path
    resolved = path.resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError:
        raise ForensicAuditError("evidence binding leaves --data-dir") from None
    return resolved


def _read_evidence(
    root: Path,
    source_files: list[dict[str, Any]],
    baseline_environment: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    if not HEX_64.fullmatch(baseline_environment):
        raise ForensicAuditError(
            "verified baseline lacks a valid environment fingerprint"
        )
    references = {
        str(entry["path"]): entry
        for entry in source_files
        if not entry.get("omitted")
    }
    references_by_path = {
        _safe_evidence_path(root, relative): (relative, entry)
        for relative, entry in references.items()
        if not baseline._is_downstream_output_path(Path(relative))
    }
    documents: list[dict[str, Any]] = []
    inputs: list[dict[str, Any]] = []
    for relative, entry in sorted(references.items()):
        supported_kind = entry.get("kind") in SUPPORTED_EVIDENCE_KINDS
        input_reference = {
            "path": relative,
            "kind": entry["kind"],
            "size": entry["size"],
            "sha256": entry["sha256"],
        }
        if baseline._is_downstream_output_path(Path(relative)):
            inputs.append(input_reference)
            continue
        path = _safe_evidence_path(root, relative)
        inspect_json_shape = path.suffix.casefold() == ".json"
        if not supported_kind and not inspect_json_shape:
            continue
        try:
            content = path.read_bytes()
        except OSError as exc:
            raise ForensicAuditError(
                "evidence hash changed after baseline verification"
            ) from exc
        if (
            not path.is_file()
            or len(content) != entry.get("size")
            or _sha256_bytes(content) != entry.get("sha256")
        ):
            raise ForensicAuditError("evidence hash changed after baseline verification")
        if supported_kind:
            inputs.append(input_reference)
        if inspect_json_shape:
            try:
                value = json.loads(content)
            except json.JSONDecodeError as exc:
                if supported_kind:
                    raise ForensicAuditError(
                        f"evidence JSON is invalid: {relative}"
                    ) from exc
                continue
            if isinstance(value, dict):
                documents.append(
                    {
                        "path": path,
                        "relative": relative,
                        "sha256": entry["sha256"],
                        "input": input_reference,
                        "value": value,
                    }
                )
    plans: list[dict[str, Any]] = []
    receipts: list[dict[str, Any]] = []
    for document in documents:
        value = document["value"]
        mode = str(value.get("mode") or "")
        if value.get("schemaVersion") in {1, 3} and mode in {
            "staging-apply-plan",
            "production-promotion-plan",
        }:
            environment = value.get("environmentFingerprint")
            if not isinstance(environment, str) or not HEX_64.fullmatch(environment):
                raise ForensicAuditError(
                    "SimpleFIN application plan lacks a valid environment fingerprint"
                )
            expected = plan_fingerprint(
                {key: child for key, child in value.items() if key != "planFingerprint"}
            )
            invalid_plan = (
                value.get("planFingerprint") != expected
                or not isinstance(value.get("intentFingerprint"), str)
                or "decisionEvidence" not in value
                or not isinstance(value.get("ledgerFingerprint"), str)
                or not isinstance(
                    value.get("expectedPostLedgerFingerprint"), str
                )
                or not isinstance(value.get("operations"), dict)
                or not isinstance(value.get("reconciliations"), list)
                or not isinstance(value.get("evidence"), dict)
                or (
                    value.get("schemaVersion") == 1
                    and (
                        not isinstance(value.get("portableIntent"), dict)
                        or not isinstance(value.get("ledgerAccountIds"), list)
                        or not isinstance(value.get("links"), list)
                        or not isinstance(value.get("manual"), list)
                        or not isinstance(value.get("monitors"), list)
                        or not isinstance(value.get("impact"), dict)
                        or not isinstance(value.get("spendingWindow"), dict)
                    )
                )
            )
            if invalid_plan:
                if value.get("schemaVersion") == 1:
                    continue
                raise ForensicAuditError("SimpleFIN application plan fingerprint is invalid")
            if environment == baseline_environment:
                for binding in (value.get("evidence") or {}).values():
                    if not isinstance(binding, dict):
                        raise ForensicAuditError(
                            "SimpleFIN plan evidence binding is invalid"
                        )
                    bound_path = _safe_evidence_path(
                        root, str(binding.get("path") or "")
                    )
                    source_reference = references_by_path.get(bound_path)
                    try:
                        bound_content = bound_path.read_bytes()
                    except OSError as exc:
                        raise ForensicAuditError(
                            "SimpleFIN plan evidence hash is unproved"
                        ) from exc
                    if (
                        source_reference is None
                        or len(bound_content) != source_reference[1].get("size")
                        or _sha256_bytes(bound_content) != binding.get("sha256")
                        or source_reference[1].get("sha256") != binding.get("sha256")
                    ):
                        raise ForensicAuditError(
                            "SimpleFIN plan evidence hash is unproved"
                        )
                    if not any(item["path"] == source_reference[0] for item in inputs):
                        inputs.append(
                            {
                                "path": source_reference[0],
                                "kind": source_reference[1]["kind"],
                                "size": source_reference[1]["size"],
                                "sha256": source_reference[1]["sha256"],
                            }
                        )
            plans.append(document)
            if not any(item["path"] == document["relative"] for item in inputs):
                inputs.append(document["input"])
        elif (
            value.get("schemaVersion") in {3, 5}
            and value.get("status") in {"applied", "already-applied"}
            and "applicationPlanSha256" in value
        ):
            environment = value.get("environmentFingerprint")
            if not isinstance(environment, str) or not HEX_64.fullmatch(environment):
                raise ForensicAuditError(
                    "SimpleFIN application receipt lacks a valid environment fingerprint"
                )
            if (
                mode not in {"", "staging-apply", "production-promotion"}
                or not isinstance(value.get("applicationPlanSha256"), str)
                or not HEX_64.fullmatch(value["applicationPlanSha256"])
                or not isinstance(value.get("planFingerprint"), str)
                or not isinstance(value.get("intentFingerprint"), str)
                or "decisionEvidence" not in value
                or not isinstance(value.get("preLedgerFingerprint"), str)
                or not isinstance(value.get("postLedgerFingerprint"), str)
                or not isinstance(value.get("operations"), dict)
                or not isinstance(value.get("reconciliations"), list)
            ):
                raise ForensicAuditError(
                    "SimpleFIN application receipt shape is invalid"
                )
            receipts.append(document)
            if not any(item["path"] == document["relative"] for item in inputs):
                inputs.append(document["input"])
    return sorted(inputs, key=lambda item: item["path"]), plans, receipts


def _receipt_matches_plan(
    plan_document: dict[str, Any],
    receipt_document: dict[str, Any],
) -> bool:
    plan = plan_document["value"]
    receipt = receipt_document["value"]
    return (
        receipt.get("applicationPlanSha256") == plan_document["sha256"]
        and receipt.get("planFingerprint") == plan.get("planFingerprint")
        and receipt.get("intentFingerprint") == plan.get("intentFingerprint")
        and receipt.get("environmentFingerprint") == plan.get("environmentFingerprint")
        and receipt.get("preLedgerFingerprint") == plan.get("ledgerFingerprint")
        and receipt.get("postLedgerFingerprint")
        == plan.get("expectedPostLedgerFingerprint")
        and receipt.get("decisionEvidence") == plan.get("decisionEvidence")
        and receipt.get("operations")
        == {
            key: len(value)
            for key, value in (plan.get("operations") or {}).items()
            if isinstance(value, list)
        }
        and receipt.get("reconciliations") == plan.get("reconciliations")
    )


def _matching_receipts(
    plan_document: dict[str, Any], receipts: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    plan = plan_document["value"]
    matches: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for receipt_document in receipts:
        receipt = receipt_document["value"]
        if (
            receipt.get("applicationPlanSha256") == plan_document["sha256"]
            and receipt.get("planFingerprint") == plan.get("planFingerprint")
        ):
            binding = {
                key: receipt.get(key)
                for key in (
                    "applicationPlanSha256",
                    "planFingerprint",
                    "intentFingerprint",
                    "environmentFingerprint",
                    "preLedgerFingerprint",
                    "postLedgerFingerprint",
                    "decisionEvidence",
                    "operations",
                    "reconciliations",
                )
            }
            matches[_sha256_bytes(_json_bytes(binding))].append(
                receipt_document
            )
    return [
        min(
            documents,
            key=lambda item: (
                0 if item["value"].get("status") == "applied" else 1,
                str(item["sha256"]),
            ),
        )
        for _binding_hash, documents in sorted(matches.items())
    ]


def _receipt_for_plan(
    plan_document: dict[str, Any], receipts: list[dict[str, Any]]
) -> tuple[str, dict[str, Any] | None]:
    matches = _matching_receipts(plan_document, receipts)
    exact = [
        receipt_document
        for receipt_document in matches
        if _receipt_matches_plan(plan_document, receipt_document)
    ]
    if len(matches) != len(exact):
        return "ambiguous", None
    if len(exact) == 1:
        return "proved", exact[0]
    if len(exact) > 1:
        return "ambiguous", None
    return "missing", None


def _planned_effect(payload: dict[str, Any]) -> Decimal:
    amount = abs(_decimal(payload.get("amount", 0), "planned activity amount"))
    kind = str(payload.get("activityType") or "").upper()
    if kind in INFLOW:
        return amount
    if kind in OUTFLOW:
        return -amount
    raise ForensicAuditError("planned activity has an unsupported cash activity type")


def _lineage(
    activity: Activity,
    plans: list[dict[str, Any]],
    receipts: list[dict[str, Any]],
) -> dict[str, Any]:
    matches: list[tuple[dict[str, Any], dict[str, Any] | None, str]] = []
    unique_plans = {
        str(plan_document["sha256"]): plan_document
        for plan_document in plans
    }
    for plan_document in (
        unique_plans[key] for key in sorted(unique_plans)
    ):
        plan = plan_document["value"]
        creates = (plan.get("operations") or {}).get("creates") or []
        if any(
            activity.source_identity
            and activity.source_identity == str(create.get("idempotencyKey") or "")
            and activity.ledger_account_id == str(create.get("accountId") or "")
            for create in creates
        ):
            receipt_status, receipt = _receipt_for_plan(plan_document, receipts)
            matches.append((plan_document, receipt, receipt_status))
    if not matches:
        return {
            "status": "missing",
            "applicationPlanSha256": None,
            "receiptSha256": None,
            "receiptProvenReconciliation": False,
            "reconciliation": None,
        }
    if len(matches) > 1:
        proved_matches = [match for match in matches if match[2] == "proved"]
        if len(proved_matches) == 1:
            matches = proved_matches
    if len(matches) != 1 or matches[0][2] == "ambiguous":
        return {
            "status": "ambiguous",
            "applicationPlanSha256": None,
            "receiptSha256": None,
            "receiptProvenReconciliation": False,
            "reconciliation": None,
        }
    plan_document, receipt_document, receipt_status = matches[0]
    plan = plan_document["value"]
    account_creates = [
        create
        for create in (plan.get("operations") or {}).get("creates") or []
        if str(create.get("accountId") or "") == activity.ledger_account_id
        and str(create.get("activityType") or "").upper() in INFLOW | OUTFLOW
    ]
    reconciliation_matches = [
        row
        for row in (plan.get("reconciliations") or [])
        if str(row.get("accountId") or "") == activity.ledger_account_id
        and row.get("action") == "update"
    ]
    reconciliation_valid = False
    if len(reconciliation_matches) == 1:
        change = reconciliation_matches[0]
        updated_ids = {
            str(update.get("id") or "")
            for update in (plan.get("operations") or {}).get("updates") or []
        }
        transaction_effect = sum(
            (_planned_effect(create) for create in account_creates), Decimal("0")
        )
        reconciliation_valid = (
            str(change.get("activityId") or "") in updated_ids
            and bool(account_creates)
            and transaction_effect
            == _decimal(
                change.get("transactionEffect"),
                "reconciliation transaction effect",
            )
            and _decimal(change.get("afterEffect"), "reconciliation after effect")
            == _decimal(change.get("beforeEffect"), "reconciliation before effect")
            - transaction_effect
        )
    proved = (
        receipt_status == "proved"
        and len(reconciliation_matches) == 1
        and reconciliation_valid
    )
    reconciliation = reconciliation_matches[0] if proved else None
    return {
        "status": "receipt-proven" if receipt_status == "proved" else "plan-only",
        "applicationPlanSha256": plan_document["sha256"],
        "receiptSha256": receipt_document["sha256"] if receipt_document else None,
        "reviewedPlanSha256": (plan.get("evidence") or {}).get("reviewedPlan", {}).get("sha256"),
        "snapshotSha256": (plan.get("evidence") or {}).get("snapshot", {}).get("sha256"),
        "receiptProvenReconciliation": proved,
        "reconciliationCreateBatchCardinality": len(account_creates),
        "reconciliation": (
            {
                **reconciliation,
                "beforeFingerprint": plan_fingerprint(reconciliation.get("before")),
                "afterFingerprint": plan_fingerprint(reconciliation.get("after")),
            }
            if reconciliation
            else None
        ),
    }


def _csv_bytes(rows: Iterable[dict[str, Any]], columns: tuple[str, ...]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=columns, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({column: row.get(column, "") for column in columns})
    return output.getvalue().encode("utf-8")


def _shareable_summary(
    baseline_id: str,
    input_hashes: list[str],
    activities: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    groups: list[dict[str, Any]],
    duplicate_effects: list[dict[str, Any]],
    reconciliation_effects: list[dict[str, Any]],
    lineage_evidence: dict[str, int],
) -> dict[str, Any]:
    reasons = Counter(reason for edge in edges for reason in edge["reasonCodes"])
    pairs = Counter(edge["sourceFamilyPair"] for edge in edges)
    dependencies = Counter(
        reason
        for activity in activities
        for reason in activity["dependentState"]["reasonCodes"]
    )
    lineage = Counter(activity["lineage"]["status"] for activity in activities)
    out_of_scope = Counter(
        str(activity["scopeReason"])
        for activity in activities
        if activity["scopeReason"]
    )
    summary = {
        "schemaVersion": SCHEMA_VERSION,
        "privateDetailExcluded": True,
        "baselinePublicationSha256": baseline_id,
        "inputHashes": sorted(input_hashes),
        "counts": {
            "baseline-activities": len(activities),
            "accounted-activities": len(activities),
            "cash-activities": sum(
                activity["scopeReason"] is None for activity in activities
            ),
            "out-of-scope-activities": sum(out_of_scope.values()),
            "candidate-edges": len(edges),
            "candidate-groups": len(groups),
            "automatic-duplicate-groups": sum(
                group["classification"] == "automatic-duplicate" for group in groups
            ),
            "review-required-groups": sum(
                group["classification"] == "review-required" for group in groups
            ),
            "relationship-only-groups": sum(
                group["classification"] == "relationship-only"
                for group in groups
            ),
            "duplicate-economic-effects": len(duplicate_effects),
            "receipt-proven-reconciliation-effects": len(reconciliation_effects),
            "unresolved-effect-observations": sum(
                group["ambiguityCardinality"]
                for group in groups
                if group["classification"] == "review-required"
            ),
        },
        "candidateCardinalities": dict(
            sorted(Counter(group["cardinality"] for group in groups).items())
        ),
        "reasonCodeCounts": dict(sorted(reasons.items())),
        "sourceFamilyPairCounts": dict(sorted(pairs.items())),
        "dependentStateCounts": dict(sorted(dependencies.items())),
        "lineageStatusCounts": dict(sorted(lineage.items())),
        "lineageEvidenceCounts": dict(sorted(lineage_evidence.items())),
        "outOfScopeReasonCounts": dict(sorted(out_of_scope.items())),
        "candidateGraphSha256": _sha256_bytes(_json_bytes({"edges": edges, "groups": groups})),
    }
    _validate_shareable(summary)
    return summary


def _validate_shareable(summary: dict[str, Any]) -> None:
    if set(summary) != {
        "schemaVersion",
        "privateDetailExcluded",
        "baselinePublicationSha256",
        "inputHashes",
        "counts",
        "candidateCardinalities",
        "reasonCodeCounts",
        "sourceFamilyPairCounts",
        "dependentStateCounts",
        "lineageStatusCounts",
        "lineageEvidenceCounts",
        "outOfScopeReasonCounts",
        "candidateGraphSha256",
    }:
        raise ForensicAuditError("shareable summary has an unsafe schema")
    hashes = [
        summary["baselinePublicationSha256"],
        summary["candidateGraphSha256"],
        *summary["inputHashes"],
    ]
    if not all(isinstance(value, str) and HEX_64.fullmatch(value) for value in hashes):
        raise ForensicAuditError("shareable summary contains a non-hash identity")
    for section in (
        "counts",
        "candidateCardinalities",
        "reasonCodeCounts",
        "sourceFamilyPairCounts",
        "dependentStateCounts",
        "lineageStatusCounts",
        "lineageEvidenceCounts",
        "outOfScopeReasonCounts",
    ):
        values = summary[section]
        if not isinstance(values, dict) or not all(
            SAFE_REASON.fullmatch(str(key)) and isinstance(value, int) and value >= 0
            for key, value in values.items()
        ):
            raise ForensicAuditError("shareable summary contains private or invalid values")


def _markdown(summary: dict[str, Any]) -> bytes:
    counts = summary["counts"]
    lines = [
        "# Forensic duplicate audit",
        "",
        f"Baseline: `{summary['baselinePublicationSha256']}`",
        f"Candidate graph: `{summary['candidateGraphSha256']}`",
        "",
        "| Measure | Count |",
        "|---|---:|",
    ]
    lines.extend(
        f"| {key.replace('-', ' ')} | {value} |"
        for key, value in sorted(counts.items())
    )
    for title, key in (
        ("Reason codes", "reasonCodeCounts"),
        ("Source-family pairs", "sourceFamilyPairCounts"),
        ("Candidate cardinalities", "candidateCardinalities"),
        ("Dependent state", "dependentStateCounts"),
        ("Lineage", "lineageStatusCounts"),
        ("Lineage evidence", "lineageEvidenceCounts"),
        ("Out-of-scope activity", "outOfScopeReasonCounts"),
    ):
        lines.extend(["", f"## {title}", "", "| Code | Count |", "|---|---:|"])
        lines.extend(
            f"| `{name}` | {count} |"
            for name, count in sorted(summary[key].items())
        )
        if not summary[key]:
            lines.append("| `none` | 0 |")
    lines.extend(
        [
            "",
            "Amounts, descriptions, account identities, transaction identities, and paths are intentionally excluded.",
            "",
        ]
    )
    return "\n".join(lines).encode("utf-8")


def _write_synced(path: Path, content: bytes) -> None:
    with path.open("xb") as target:
        target.write(content)
        target.flush()
        os.fsync(target.fileno())


def _atomic_write(path: Path, content: bytes) -> None:
    temporary = path.parent / f".{path.name}-{uuid.uuid4().hex}.tmp"
    try:
        _write_synced(temporary, content)
        os.replace(temporary, path)
        fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _baseline_state(root: Path, repo_root: Path) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    try:
        publication, pointer = baseline._current(root)
        baseline.verify(root, repo_root=repo_root)
        verified_publication, verified_pointer = baseline._current(root)
    except baseline.BaselineError as exc:
        raise ForensicAuditError(str(exc)) from None
    if verified_pointer != pointer or verified_publication != publication:
        raise ForensicAuditError("baseline current pointer changed during verification")
    content = (publication / "manifest.json").read_bytes()
    if _sha256_bytes(content) != pointer["publicationId"]:
        raise ForensicAuditError("baseline manifest changed during verification")
    manifest = json.loads(content)
    return publication, pointer, manifest


def _analyze(
    baseline_publication: Path,
    baseline_pointer: dict[str, Any],
    baseline_manifest: dict[str, Any],
    inputs: list[dict[str, Any]],
    plans: list[dict[str, Any]],
    receipts: list[dict[str, Any]],
) -> dict[str, Any]:
    baseline_environment = baseline_manifest.get("environmentFingerprint")
    if (
        not isinstance(baseline_environment, str)
        or not HEX_64.fullmatch(baseline_environment)
    ):
        raise ForensicAuditError(
            "verified baseline lacks a valid environment fingerprint"
        )
    ignored_plan_count = sum(
        plan["value"]["environmentFingerprint"] != baseline_environment
        for plan in plans
    )
    ignored_receipt_count = sum(
        receipt["value"]["environmentFingerprint"] != baseline_environment
        for receipt in receipts
    )
    plans = [
        plan
        for plan in plans
        if plan["value"]["environmentFingerprint"] == baseline_environment
    ]
    receipts = [
        receipt
        for receipt in receipts
        if receipt["value"]["environmentFingerprint"] == baseline_environment
    ]
    plans = [
        item
        for _digest, item in sorted(
            {str(item["sha256"]): item for item in plans}.items()
        )
    ]
    receipts = [
        item
        for _digest, item in sorted(
            {str(item["sha256"]): item for item in receipts}.items()
        )
    ]
    raw_activities = _domain(baseline_publication, baseline_manifest, "activities")
    raw_accounts = _domain(baseline_publication, baseline_manifest, "accounts")
    if not isinstance(raw_activities, list) or not isinstance(raw_accounts, list):
        raise ForensicAuditError("verified baseline lacks activities or accounts")
    canonical_aliases: dict[str, str] = {}
    for plan_document in plans:
        for assertion in plan_document["value"].get("assertions") or []:
            ledger_id = str(assertion.get("accountId") or "")
            canonical_id = str(assertion.get("canonicalAccountId") or "")
            if ledger_id and canonical_id:
                existing = canonical_aliases.setdefault(ledger_id, canonical_id)
                if existing != canonical_id:
                    raise ForensicAuditError(
                        "application plans disagree on canonical account identity"
                    )
    activities = _normalize_activities(
        raw_activities,
        raw_accounts,
        baseline_pointer["publicationId"],
        canonical_aliases,
    )
    by_ref = {activity.ref: activity for activity in activities}
    detected_edges = _detect_edges(activities)
    groups, group_by_ref = _candidate_groups(detected_edges, by_ref)
    group_map = {group["groupId"]: group for group in groups}
    dependent_domains = {
        name: _domain(baseline_publication, baseline_manifest, name)
        for name in (
            "assignments",
            "splits",
            "transfer-groups",
            "spending-activities",
        )
    }
    unavailable = sorted(
        name for name, value in dependent_domains.items() if value is None
    )
    if unavailable:
        raise ForensicAuditError(
            "dependent-state baseline domain unavailable: " + ", ".join(unavailable)
        )
    assignments = dependent_domains["assignments"]
    splits = dependent_domains["splits"]
    transfer_pairs = dependent_domains["transfer-groups"]
    spending_rows = dependent_domains["spending-activities"]
    if (
        not isinstance(assignments, dict)
        or not isinstance(splits, dict)
        or not isinstance(transfer_pairs, dict)
        or not isinstance(spending_rows, list)
    ):
        raise ForensicAuditError("verified dependent-state domains are incompatible")

    detailed_activities = []
    for activity in activities:
        dependent = _dependent_state(
            activity, assignments, splits, transfer_pairs, spending_rows
        )
        lineage = _lineage(activity, plans, receipts)
        if activity.scope_reason:
            observation_status = "out-of-scope"
        elif activity.ref in group_by_ref:
            observation_status = "candidate"
        else:
            observation_status = "no-candidate"
        detailed_activities.append(
            {
                "activityRef": activity.ref,
                "activityId": activity.activity_id,
                "canonicalAccountId": activity.account_id,
                "sourceAtUtc": activity.source_at,
                "sourceDateUtc": activity.source_date,
                "signedEffect": (
                    _money(activity.effect) if activity.effect is not None else None
                ),
                "scopeReason": activity.scope_reason,
                "sourceFamily": activity.source_family,
                "sourceIdentity": activity.source_identity,
                "description": activity.description,
                "normalizedDescription": activity.normalized_description,
                "transferGroup": activity.transfer_group,
                "reconciliation": activity.reconciliation,
                "observationStatus": observation_status,
                "candidateGroupIds": (
                    [group_by_ref[activity.ref]] if activity.ref in group_by_ref else []
                ),
                "dependentState": dependent,
                "lineage": lineage,
                "raw": activity.raw,
            }
        )

    edge_rows = []
    for edge in detected_edges:
        left = by_ref[edge.left]
        right = by_ref[edge.right]
        group_id = group_by_ref[edge.left]
        edge_rows.append(
            {
                "candidateId": _stable_ref("candidate-edge", edge.left, edge.right),
                "groupId": group_id,
                "leftActivityRef": edge.left,
                "rightActivityRef": edge.right,
                "tier": edge.tier,
                "classification": edge.classification,
                "reasonCodes": list(edge.reasons),
                "sourceFamilyPair": "-".join(
                    sorted((left.source_family, right.source_family))
                ),
                "ambiguityCardinality": group_map[group_id]["ambiguityCardinality"],
            }
        )

    duplicate_effects = []
    exact_identity_groups: dict[tuple[str, str, str], list[Activity]] = defaultdict(list)
    for activity in activities:
        if (
            activity.effect is not None
            and activity.source_identity
            and activity.source_family != "unknown"
        ):
            exact_identity_groups[
                (
                    activity.account_id,
                    activity.source_family,
                    activity.source_identity,
                )
            ].append(activity)
    for members_by_identity in exact_identity_groups.values():
        if len(members_by_identity) < 2:
            continue
        members = sorted(activity.ref for activity in members_by_identity)
        for duplicate_ref in members[1:]:
            duplicate_effects.append(
                {
                    "groupId": group_by_ref[duplicate_ref],
                    "activityRef": duplicate_ref,
                    "signedEffect": _money(by_ref[duplicate_ref].effect),
                    "reasonCode": "exact-source-identity",
                }
            )
    reconciliation_effects_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for row in detailed_activities:
        reconciliation = row["lineage"].get("reconciliation")
        if not reconciliation:
            continue
        key = (
            str(row["lineage"]["applicationPlanSha256"]),
            str(reconciliation.get("activityId") or ""),
        )
        reconciliation_effects_by_key[key] = {
            "applicationPlanSha256": key[0],
            "receiptSha256": row["lineage"]["receiptSha256"],
            **reconciliation,
        }
    reconciliation_effects = [
        reconciliation_effects_by_key[key]
        for key in sorted(reconciliation_effects_by_key)
    ]
    unresolved_effects = [
        {
            "groupId": group["groupId"],
            "activityRef": ref,
            "signedEffect": _money(by_ref[ref].effect),
            "netted": False,
        }
        for group in groups
        if group["classification"] == "review-required"
        for ref in group["activityRefs"]
    ]
    if len(detailed_activities) != len(raw_activities) or {
        row["activityId"] for row in detailed_activities
    } != {str(row["id"]) for row in raw_activities}:
        raise ForensicAuditError("not every baseline activity was accounted for")
    plan_receipt_statuses = Counter(
        _receipt_for_plan(plan, receipts)[0] for plan in plans
    )
    lineage_evidence = {
        "application-plans": len(plans),
        "receipts": len(receipts),
        "plans-proved": plan_receipt_statuses["proved"],
        "plans-missing-receipt": plan_receipt_statuses["missing"],
        "plans-ambiguous-receipt": plan_receipt_statuses["ambiguous"],
        "ignored-other-environment-application-plans": ignored_plan_count,
        "ignored-other-environment-receipts": ignored_receipt_count,
    }
    scope_reasons = Counter(
        activity.scope_reason for activity in activities if activity.scope_reason
    )
    return {
        "schemaVersion": SCHEMA_VERSION,
        "private": True,
        "readOnly": True,
        "baselinePublication": baseline_pointer,
        "inputs": inputs,
        "activities": detailed_activities,
        "candidateEdges": edge_rows,
        "candidateGroups": groups,
        "duplicateEconomicEffects": duplicate_effects,
        "receiptProvenReconciliationEffects": reconciliation_effects,
        "unresolvedEffects": unresolved_effects,
        "lineageEvidenceCounts": lineage_evidence,
        "analysisScope": {
            "cashDuplicateMatching": {
                "status": "available",
                "includedActivityCount": sum(
                    activity.effect is not None for activity in activities
                ),
                "outOfScopeActivityCount": sum(scope_reasons.values()),
                "outOfScopeReasonCounts": dict(sorted(scope_reasons.items())),
            }
        },
        "invariants": {
            "baselineActivityCount": len(raw_activities),
            "accountedActivityCount": len(detailed_activities),
            "noObservationSuppressed": True,
            "candidateCardinalitiesExplicit": True,
            "duplicateAndReconciliationEffectsSeparated": True,
            "unresolvedEffectsNetted": False,
        },
        "capabilityGaps": [
            {
                "reasonCode": "activity-note-provenance-unavailable",
                "detail": (
                    "Provider comment text is a description, not a proven user note; "
                    "the baseline has no author or edit history."
                ),
            },
            {
                "reasonCode": "cascade-preview-unavailable",
                "detail": "Wealthfolio exposes dependent reads but no authenticated delete-cascade preview.",
            },
            {
                "reasonCode": "projection-lineage-partial",
                "detail": "Projection/import lineage is limited to IDs present in captured activity state.",
            },
        ],
    }


def build(
    data_dir: str | Path,
    *,
    repo_root: str | Path,
    before_pointer: Any | None = None,
) -> dict[str, Any]:
    """Publish an audit using files only; no transport or mutation client is accepted."""
    root = Path(data_dir).resolve()
    output = validate_private_output(root / OUTPUT_RELATIVE, root, Path(repo_root))
    baseline_publication, baseline_pointer, baseline_manifest = _baseline_state(
        root, Path(repo_root)
    )
    inputs, plans, receipts = _read_evidence(
        root,
        baseline_manifest["sourceFiles"],
        baseline_manifest["environmentFingerprint"],
    )
    detail = _analyze(
        baseline_publication,
        baseline_pointer,
        baseline_manifest,
        inputs,
        plans,
        receipts,
    )
    detailed_activities = detail["activities"]
    edge_rows = detail["candidateEdges"]
    groups = detail["candidateGroups"]
    duplicate_effects = detail["duplicateEconomicEffects"]
    reconciliation_effects = detail["receiptProvenReconciliationEffects"]
    activity_csv_rows = [
        {
            "activity_ref": row["activityRef"],
            "activity_id": row["activityId"],
            "canonical_account_id": row["canonicalAccountId"],
            "source_date": row["sourceDateUtc"],
            "signed_effect": row["signedEffect"],
            "scope_reason": row["scopeReason"],
            "source_family": row["sourceFamily"],
            "source_identity": row["sourceIdentity"],
            "description": row["description"],
            "normalized_description": row["normalizedDescription"],
            "transfer_group": row["transferGroup"],
            "candidate_group_ids": "|".join(row["candidateGroupIds"]),
            "dependent_reason_codes": "|".join(row["dependentState"]["reasonCodes"]),
            "lineage_status": row["lineage"]["status"],
            "raw_json": json.dumps(row["raw"], sort_keys=True, separators=(",", ":")),
        }
        for row in detailed_activities
    ]
    candidate_csv_rows = [
        {
            "candidate_id": row["candidateId"],
            "group_id": row["groupId"],
            "left_activity_ref": row["leftActivityRef"],
            "right_activity_ref": row["rightActivityRef"],
            "tier": row["tier"],
            "classification": row["classification"],
            "reason_codes": "|".join(row["reasonCodes"]),
            "source_family_pair": row["sourceFamilyPair"],
            "ambiguity_cardinality": row["ambiguityCardinality"],
        }
        for row in edge_rows
    ]
    review = {
        "schemaVersion": SCHEMA_VERSION,
        "mode": "review-decisions-only",
        "auditGraphSha256": _sha256_bytes(
            _json_bytes({"edges": edge_rows, "groups": groups})
        ),
        "decisions": [
            {
                "decisionId": _stable_ref("review-decision", group["groupId"]),
                "candidateGroupId": group["groupId"],
                "candidateHash": _sha256_bytes(_json_bytes(group)),
                "status": "pending",
                "allowedOutcomes": [
                    "distinct-economic-events",
                    "duplicate-economic-event",
                    "transfer",
                    "insufficient-evidence",
                ],
                "rationale": None,
                "evidenceHashes": sorted(input_item["sha256"] for input_item in inputs),
            }
            for group in groups
            if group["classification"] == "review-required"
        ],
    }
    summary = _shareable_summary(
        baseline_pointer["publicationId"],
        [item["sha256"] for item in inputs],
        detailed_activities,
        edge_rows,
        groups,
        duplicate_effects,
        reconciliation_effects,
        detail["lineageEvidenceCounts"],
    )
    documents = {
        "private-audit.json": _json_bytes(detail),
        "activities.csv": _csv_bytes(activity_csv_rows, ACTIVITY_COLUMNS),
        "candidates.csv": _csv_bytes(candidate_csv_rows, CANDIDATE_COLUMNS),
        "review-decisions.json": _json_bytes(review),
        "summary.json": _json_bytes(summary),
        "summary.md": _markdown(summary),
    }
    manifest = {
        "schemaVersion": SCHEMA_VERSION,
        "private": True,
        "readOnly": True,
        "baselinePublicationSha256": baseline_pointer["publicationId"],
        "generatedAt": baseline_manifest["generatedAt"],
        "inputFiles": inputs,
        "files": {
            name: {"sha256": _sha256_bytes(content), "size": len(content)}
            for name, content in sorted(documents.items())
        },
        "counts": summary["counts"],
    }
    manifest_content = _json_bytes(manifest)
    publication_id = _sha256_bytes(manifest_content)
    publications = output / "publications"
    ensure_durable_directory(publications, fsync_directory)
    publication = publications / publication_id
    staging = publications / f".staging-{uuid.uuid4().hex}"
    staging.mkdir()
    try:
        for name, content in documents.items():
            _write_synced(staging / name, content)
        _write_synced(staging / "manifest.json", manifest_content)
        fsync_directory(staging)
        if publication.exists():
            expected = {"manifest.json", *documents}
            if (
                {path.name for path in publication.iterdir()} != expected
                or any((publication / name).read_bytes() != content for name, content in documents.items())
                or (publication / "manifest.json").read_bytes() != manifest_content
            ):
                raise ForensicAuditError("existing forensic publication is corrupt")
        else:
            os.replace(staging, publication)
            fsync_directory(publications)
        if before_pointer:
            before_pointer(publication)
        _baseline_state(root, Path(repo_root))
        pointer = {
            "schemaVersion": POINTER_SCHEMA_VERSION,
            "publicationId": publication_id,
            "manifestSha256": publication_id,
        }
        _atomic_write(output / "current.json", _json_bytes(pointer))
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    verified = verify(root, repo_root=repo_root)
    return {
        **verified,
        "outputPath": str(output),
        "summaryPath": str(publication / "summary.md"),
    }


def _current(root: Path) -> tuple[Path, dict[str, Any]]:
    output = root / OUTPUT_RELATIVE
    try:
        pointer = json.loads((output / "current.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ForensicAuditError("forensic current pointer is missing or invalid") from exc
    publication_id = pointer.get("publicationId")
    if (
        pointer.get("schemaVersion") != POINTER_SCHEMA_VERSION
        or not isinstance(publication_id, str)
        or not HEX_64.fullmatch(publication_id)
        or pointer.get("manifestSha256") != publication_id
    ):
        raise ForensicAuditError("forensic current pointer schema is invalid")
    publication = output / "publications" / publication_id
    manifest_path = publication / "manifest.json"
    if not manifest_path.is_file() or _sha256(manifest_path) != publication_id:
        raise ForensicAuditError("forensic current manifest hash mismatch")
    return publication, pointer


def verify(data_dir: str | Path, *, repo_root: str | Path) -> dict[str, Any]:
    root = Path(data_dir).resolve()
    validate_private_output(root / OUTPUT_RELATIVE, root, Path(repo_root))
    baseline_publication, baseline_pointer, baseline_manifest = _baseline_state(
        root, Path(repo_root)
    )
    publication, pointer = _current(root)
    try:
        manifest = json.loads((publication / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ForensicAuditError("forensic manifest is invalid") from exc
    files = manifest.get("files")
    if (
        manifest.get("schemaVersion") != SCHEMA_VERSION
        or manifest.get("private") is not True
        or manifest.get("readOnly") is not True
        or manifest.get("baselinePublicationSha256")
        != baseline_pointer["publicationId"]
        or not isinstance(files, dict)
        or set(files)
        != {
            "private-audit.json",
            "activities.csv",
            "candidates.csv",
            "review-decisions.json",
            "summary.json",
            "summary.md",
        }
        or {path.name for path in publication.iterdir()}
        != {"manifest.json", *files}
    ):
        raise ForensicAuditError("forensic publication schema is invalid")
    for name, reference in files.items():
        path = publication / name
        if (
            not path.is_file()
            or path.stat().st_size != reference.get("size")
            or _sha256(path) != reference.get("sha256")
        ):
            raise ForensicAuditError(f"forensic output hash mismatch: {name}")
    for source in manifest.get("inputFiles", []):
        if baseline._is_downstream_output_path(
            Path(str(source.get("path") or ""))
        ):
            continue
        path = _safe_evidence_path(root, str(source.get("path") or ""))
        if (
            not path.is_file()
            or path.stat().st_size != source.get("size")
            or _sha256(path) != source.get("sha256")
        ):
            raise ForensicAuditError("forensic input hash mismatch")
    detail = json.loads((publication / "private-audit.json").read_text(encoding="utf-8"))
    summary = json.loads((publication / "summary.json").read_text(encoding="utf-8"))
    _validate_shareable(summary)
    inputs, plans, receipts = _read_evidence(
        root,
        baseline_manifest["sourceFiles"],
        baseline_manifest["environmentFingerprint"],
    )
    if inputs != manifest.get("inputFiles"):
        raise ForensicAuditError("forensic manifest input inventory is incomplete")
    recomputed_detail = _analyze(
        baseline_publication,
        baseline_pointer,
        baseline_manifest,
        inputs,
        plans,
        receipts,
    )
    if detail != recomputed_detail:
        raise ForensicAuditError(
            "forensic semantic analysis differs from verified baseline evidence"
        )
    invariants = detail.get("invariants") or {}
    raw_activities = _domain(
        baseline_publication, baseline_manifest, "activities"
    )
    activities = detail.get("activities")
    edges = detail.get("candidateEdges")
    groups = detail.get("candidateGroups")
    duplicate_effects = detail.get("duplicateEconomicEffects")
    reconciliation_effects = detail.get("receiptProvenReconciliationEffects")
    unresolved_effects = detail.get("unresolvedEffects")
    if not all(
        isinstance(value, list)
        for value in (
            raw_activities,
            activities,
            edges,
            groups,
            duplicate_effects,
            reconciliation_effects,
            unresolved_effects,
        )
    ):
        raise ForensicAuditError("forensic private detail schema is invalid")
    raw_ids = {str(row.get("id") or "") for row in raw_activities}
    detail_ids = {str(row.get("activityId") or "") for row in activities}
    detail_refs = {str(row.get("activityRef") or "") for row in activities}
    if (
        "" in raw_ids
        or "" in detail_ids
        or "" in detail_refs
        or len(raw_ids) != len(raw_activities)
        or len(detail_ids) != len(activities)
        or len(detail_refs) != len(activities)
        or raw_ids != detail_ids
        or any(
            row.get("observationStatus")
            not in {"candidate", "no-candidate", "out-of-scope"}
            for row in activities
        )
    ):
        raise ForensicAuditError("forensic activity accounting is invalid")
    group_by_id = {
        str(group.get("groupId") or ""): group
        for group in groups
        if isinstance(group, dict)
    }
    if "" in group_by_id or len(group_by_id) != len(groups):
        raise ForensicAuditError("forensic candidate groups are invalid")
    for group_id, group in group_by_id.items():
        member_refs = group.get("activityRefs")
        if (
            not isinstance(member_refs, list)
            or len(member_refs) != group.get("ambiguityCardinality")
            or len(set(member_refs)) != len(member_refs)
            or not set(member_refs).issubset(detail_refs)
            or group.get("cardinality")
            not in {"one-to-one", "one-to-many", "many-to-many"}
        ):
            raise ForensicAuditError(
                f"forensic candidate group cardinality is invalid: {group_id}"
            )
    for edge in edges:
        group = group_by_id.get(str(edge.get("groupId") or ""))
        if (
            group is None
            or edge.get("leftActivityRef") not in group["activityRefs"]
            or edge.get("rightActivityRef") not in group["activityRefs"]
            or edge.get("classification")
            not in {
                "automatic-duplicate",
                "relationship-only",
                "review-required",
            }
            or not isinstance(edge.get("reasonCodes"), list)
        ):
            raise ForensicAuditError("forensic candidate edge is invalid")
    for row in unresolved_effects:
        if row.get("netted") is not False:
            raise ForensicAuditError("forensic unresolved effect was netted")
    expected_summary = _shareable_summary(
        baseline_pointer["publicationId"],
        [item["sha256"] for item in manifest.get("inputFiles", [])],
        activities,
        edges,
        groups,
        duplicate_effects,
        reconciliation_effects,
        detail["lineageEvidenceCounts"],
    )
    if summary != expected_summary:
        raise ForensicAuditError("forensic shareable summary does not match detail")
    with (publication / "activities.csv").open(
        encoding="utf-8", newline=""
    ) as source:
        activity_rows = list(csv.DictReader(source))
        if (
            tuple(activity_rows[0].keys()) if activity_rows else ACTIVITY_COLUMNS
        ) != ACTIVITY_COLUMNS or {
            row["activity_ref"] for row in activity_rows
        } != detail_refs:
            raise ForensicAuditError("forensic activity CSV is inconsistent")
    with (publication / "candidates.csv").open(
        encoding="utf-8", newline=""
    ) as source:
        candidate_rows = list(csv.DictReader(source))
        expected_candidate_ids = {str(row.get("candidateId") or "") for row in edges}
        if (
            tuple(candidate_rows[0].keys()) if candidate_rows else CANDIDATE_COLUMNS
        ) != CANDIDATE_COLUMNS or {
            row["candidate_id"] for row in candidate_rows
        } != expected_candidate_ids:
            raise ForensicAuditError("forensic candidate CSV is inconsistent")
    if (
        invariants.get("baselineActivityCount")
        != invariants.get("accountedActivityCount")
        or invariants.get("noObservationSuppressed") is not True
        or invariants.get("candidateCardinalitiesExplicit") is not True
        or invariants.get("duplicateAndReconciliationEffectsSeparated") is not True
        or invariants.get("unresolvedEffectsNetted") is not False
        or summary.get("counts") != manifest.get("counts")
    ):
        raise ForensicAuditError("forensic publication invariants failed")
    return {
        "verified": True,
        "publication": pointer,
        "counts": summary["counts"],
        "outputPath": str(root / OUTPUT_RELATIVE),
        "summaryPath": str(publication / "summary.md"),
    }
