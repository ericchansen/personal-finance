"""Project verified private lineage decisions into canonical transaction records."""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, is_dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable

from finance_store import identity_declarations as declarations
from finance_store.domain import content_hash
from finance_store.identity import (
    DEFAULT_POLICY,
    ConfidenceTier,
    DecisionOutcome,
    DuplicateSummaryMapping,
    IdentityPolicy,
    ProviderTokenScope,
    observations_from_transaction_rows,
    resolve_identity,
)

from .model import HEX_64, SUPPRESSION_TYPES, ReviewError, stable_hash
from .workflow import (
    OUTPUT_RELATIVE,
    private_publications_exist,
    verified_state,
)

SCHEMA_VERSION = 1
COLLISION_FIELDS = (
    "date",
    "amount",
    "description",
    "category",
    "transfer_group",
    "symbol",
    "quantity",
    "price",
    "external_flow",
    "excluded",
    "exclusion_reason",
    "assignment_source",
    "assignment_rule_id",
    "assignment_confidence",
    "split_group",
)


def _row_data(row: Any) -> dict[str, Any]:
    if is_dataclass(row):
        return asdict(row)
    if isinstance(row, dict):
        return dict(row)
    raise ReviewError("canonical-observation-shape-invalid")


def _row_sort(row: dict[str, Any]) -> tuple[str, ...]:
    return tuple(str(row.get(key) or "") for key in sorted(row))


def _source_identity(source_id: str) -> str:
    marker = ":collision:"
    return source_id.rpartition(marker)[0] if marker in source_id else source_id


def source_collision_suffix(row: dict[str, Any]) -> str:
    content = tuple(row.get(key) for key in COLLISION_FIELDS)
    return hashlib.sha256(
        json.dumps(content, separators=(",", ":"), default=str).encode()
    ).hexdigest()[:16]


def _same_amount(left: Any, right: Any) -> bool:
    try:
        return Decimal(str(left)) == Decimal(str(right))
    except (InvalidOperation, ValueError):
        return False


def _observations(rows: Iterable[Any]) -> list[dict[str, Any]]:
    counters: Counter[str] = Counter()
    result = []
    for row in sorted((_row_data(item) for item in rows), key=_row_sort):
        fingerprint = stable_hash(row)
        counters[fingerprint] += 1
        ordinal = counters[fingerprint]
        result.append(
            {
                "observationId": stable_hash(
                    {
                        "kind": "canonical-source-observation",
                        "fingerprint": fingerprint,
                        "ordinal": ordinal,
                    }
                ),
                "observationFingerprint": fingerprint,
                "sourceReplayOrdinal": ordinal,
                "transaction": row,
            }
        )
    return result


def _row_observations(
    row: dict[str, Any], observations: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    source_id = _source_identity(str(row.get("source_id") or ""))
    account_id = str(row.get("account_id") or "")
    candidates = [
        item
        for item in observations
        if str(item["transaction"].get("account_id") or "") == account_id
        and str(item["transaction"].get("source_id") or "") == source_id
    ]
    if ":collision:" in str(row.get("source_id") or ""):
        suffix = str(row["source_id"]).rpartition(":collision:")[2]
        candidates = [
            item
            for item in candidates
            if source_collision_suffix(item["transaction"]) == suffix
        ]
    return candidates


def _activity_row_index(
    activity: dict[str, Any], rows: list[dict[str, Any]]
) -> int:
    source_identity = str(activity.get("sourceIdentity") or "")
    account_id = str(activity.get("canonicalAccountId") or "")
    candidates = [
        index
        for index, row in enumerate(rows)
        if _source_identity(str(row.get("source_id") or "")) == source_identity
        and str(row.get("account_id") or "") == account_id
    ]
    candidates = [
        index
        for index in candidates
        if str(rows[index].get("date") or "")
            == str(activity.get("sourceDateUtc") or "")
        and _same_amount(rows[index].get("amount"), activity.get("signedEffect"))
        and stable_hash(str(rows[index].get("description") or ""))
        == stable_hash(str(activity.get("description") or ""))
    ]
    if len(candidates) != 1:
        raise ReviewError("canonical-decision-member-unmapped")
    return candidates[0]


def _legacy_authority_claims(
    facts: Iterable[Any],
    transfer_review: dict[str, Any] | None,
) -> tuple[set[str], set[tuple[str, str]], set[str]]:
    transfer_sources: set[str] = set()
    transfer_legs: set[tuple[str, str]] = set()
    handoff_accounts: set[str] = set()
    for parsed in facts:
        fact = getattr(parsed, "fact", None)
        kind = str(getattr(fact, "kind", "") or "")
        data = getattr(parsed, "data", {})
        if kind in {"transfer", "transfer-confirmed", "transfer-rejected"}:
            source_ids = data.get("sourceIds") or data.get("affectsSourceIds") or []
            if isinstance(source_ids, list):
                transfer_sources.update(str(item) for item in source_ids)
        if kind == "duplicate-account":
            handoff_accounts.update(str(item) for item in getattr(fact, "affects", ()))
    for section in ("confirmed", "rejected"):
        for candidate in (transfer_review or {}).get(section, []):
            if not isinstance(candidate, dict):
                continue
            for leg in ("outflow", "inflow"):
                value = candidate.get(leg)
                if isinstance(value, dict):
                    transfer_legs.add(
                        (
                            str(value.get("accountId") or ""),
                            str(value.get("sourceId") or ""),
                        )
                    )
    transfer_legs.discard(("", ""))
    return transfer_sources, transfer_legs, handoff_accounts


def _empty_projection(
    canonical_rows: list[dict[str, Any]],
    observations: list[dict[str, Any]],
    duplicate_summaries: tuple[DuplicateSummaryMapping, ...] = (),
    token_scopes: tuple[ProviderTokenScope, ...] = (),
    policy: IdentityPolicy = DEFAULT_POLICY,
    source_artifact_hashes: dict[str, str] | None = None,
) -> dict[str, Any]:
    automatic = _automatic_identity_projection(
        canonical_rows,
        set(),
        duplicate_summaries,
        token_scopes,
        policy,
        source_artifact_hashes,
    )
    row_to_observations = {
        index: _row_observations(row, observations)
        for index, row in enumerate(canonical_rows)
    }
    observation_records = []
    canonical_transactions = []
    for index, row in enumerate(canonical_rows):
        canonical_id = automatic["canonicalIds"][index]
        members = row_to_observations[index]
        if not members:
            raise ReviewError("canonical-observation-unmapped")
        decision = automatic["decisionsByIndex"].get(index)
        disposition = (
            "suppressed"
            if index in automatic["suppressed"]
            else "active"
        )
        for item in members:
            observation_records.append(
                {
                    **item,
                    "canonicalTransactionId": canonical_id,
                    "disposition": disposition,
                    "decisionId": (
                        decision["decisionId"] if decision else None
                    ),
                    "decisionType": (
                        decision["decisionType"] if decision else None
                    ),
                    "linkId": None,
                }
            )
        if disposition == "suppressed":
            continue
        grouped_members = [
            member
            for member_index, member_rows in row_to_observations.items()
            if automatic["canonicalIds"][member_index] == canonical_id
            for member in member_rows
        ]
        canonical_transactions.append(
            {
                "canonicalTransactionId": canonical_id,
                "activeObservationId": members[0]["observationId"],
                "memberObservationIds": sorted(
                    item["observationId"] for item in grouped_members
                ),
                "decisionId": (
                    decision["decisionId"] if decision else None
                ),
                "decisionType": (
                    decision["decisionType"] if decision else None
                ),
                "linkId": None,
            }
        )
    return {
        "rows": [
            row
            for index, row in enumerate(canonical_rows)
            if index not in automatic["suppressed"]
        ],
        "observationRecords": sorted(
            observation_records, key=lambda item: item["observationId"]
        ),
        "canonicalTransactions": sorted(
            canonical_transactions,
            key=lambda item: item["canonicalTransactionId"],
        ),
        "decisionProjections": automatic["decisionProjections"],
        "identityScope": automatic["identityScope"],
        "review": {
            "queuePublicationId": None,
            "decisionPublicationId": None,
            "baselinePublicationId": None,
            "forensicPublicationId": None,
            "candidateGraphHash": None,
            "identityPolicy": automatic["identity"],
        },
        "sourcePaths": (),
    }


@dataclass(frozen=True, slots=True)
class CanonicalIdentityGeneration:
    """One canonical identity generation and the exact scope that produced it.

    Canonical projection and the PostgreSQL apply are two consumers of one
    generation.  If either re-derived the resolution its own way, the persisted
    evidence would drift from the published ``identityPolicy`` and PostgreSQL
    could never be promoted to durable authority.  Both therefore go through
    :func:`resolve_canonical_identity`.
    """

    scope: tuple[dict[str, Any], ...]
    observations: tuple[Any, ...]
    resolution: Any

    def scope_document(self) -> dict[str, Any]:
        rows = [dict(row) for row in self.scope]
        return {
            "schemaVersion": SCHEMA_VERSION,
            "kind": "canonical-identity-scope",
            "private": True,
            "rowCount": len(rows),
            "scopeHash": content_hash(rows),
            "rows": rows,
        }


def resolve_canonical_identity(
    rows: Iterable[Any],
    *,
    duplicate_summaries: tuple[DuplicateSummaryMapping, ...] = (),
    token_scopes: tuple[ProviderTokenScope, ...] = (),
    policy: IdentityPolicy = DEFAULT_POLICY,
    source_artifact_hashes: dict[str, str] | None = None,
) -> CanonicalIdentityGeneration:
    """Produce a canonical identity generation from an explicit row scope."""

    scope = tuple(_row_data(item) for item in rows)
    observations = observations_from_transaction_rows(
        scope,
        duplicate_summaries=duplicate_summaries,
        token_scopes=token_scopes,
        source_artifact_hashes=source_artifact_hashes,
    )
    resolution = resolve_identity(
        observations, policy=policy, token_scopes=token_scopes
    )
    return CanonicalIdentityGeneration(
        scope=scope, observations=observations, resolution=resolution
    )


def applied_identity_decisions_by_event(resolution: Any) -> dict[str, Any]:
    """Automatic identity decisions that materially project a canonical event."""
    applied_outcomes = {
        DecisionOutcome.MERGE_CLAIMS,
        DecisionOutcome.MERGE_OBSERVATIONS,
        DecisionOutcome.LINK_PENDING,
        DecisionOutcome.LINK_CORRECTION,
        DecisionOutcome.SUPPRESS_MIRROR,
        DecisionOutcome.SOURCE_SUPPRESSED,
    }
    return {
        canonical_id: decision
        for decision in resolution.decisions
        if decision.confidence_tier is not ConfidenceTier.REVIEW_REQUIRED
        and decision.outcome in applied_outcomes
        for canonical_id in decision.canonical_event_ids
    }


def _automatic_identity_projection(
    rows: list[dict[str, Any]],
    blocked_indexes: set[int],
    duplicate_summaries: tuple[DuplicateSummaryMapping, ...] = (),
    token_scopes: tuple[ProviderTokenScope, ...] = (),
    policy: IdentityPolicy = DEFAULT_POLICY,
    source_artifact_hashes: dict[str, str] | None = None,
) -> dict[str, Any]:
    eligible = [
        (index, row)
        for index, row in enumerate(rows)
        if index not in blocked_indexes
    ]
    base_ids = {
        index: stable_hash(
            {
                "kind": "canonical-transaction",
                "accountId": row.get("account_id"),
                "sourceId": row.get("source_id"),
            }
        )
        for index, row in enumerate(rows)
    }
    generation = resolve_canonical_identity(
        (row for _, row in eligible),
        duplicate_summaries=duplicate_summaries,
        token_scopes=token_scopes,
        policy=policy,
        source_artifact_hashes=source_artifact_hashes,
    )
    identity_observations = generation.observations
    resolution = generation.resolution
    by_source_hash: dict[str, list[str]] = defaultdict(list)
    for observation in identity_observations:
        by_source_hash[observation.source_hash].append(
            observation.observation_id
        )
    for values in by_source_hash.values():
        values.sort()
    observation_to_index = {}
    for index, row in eligible:
        source_hash = content_hash(row)
        candidates = by_source_hash.get(source_hash, [])
        if not candidates:
            raise ReviewError("canonical-identity-observation-unmapped")
        observation_to_index[candidates.pop(0)] = index
    if any(values for values in by_source_hash.values()):
        raise ReviewError("canonical-identity-observation-unmapped")

    canonical_ids = dict(base_ids)
    suppressed: set[int] = set()
    decisions_by_index: dict[int, dict[str, Any]] = {}
    decision_by_event = applied_identity_decisions_by_event(resolution)
    decision_projections = []
    for event in resolution.canonical_events:
        member_indexes = sorted(
            observation_to_index[observation_id]
            for observation_id in event.member_observation_ids
        )
        for index in member_indexes:
            canonical_ids[index] = event.canonical_event_id
        decision = decision_by_event.get(event.canonical_event_id)
        if decision is None or len(member_indexes) < 2:
            continue
        survivor_index = observation_to_index[event.selected_observation_id]
        rows[survivor_index]["description"] = event.description
        if event.category:
            category_rows = [
                rows[index]
                for index in member_indexes
                if str(rows[index].get("category") or "") == event.category
            ]
            category_row = min(category_rows, key=content_hash)
            for field in (
                "category",
                "category_id",
                "assignment_source",
                "assignment_rule_id",
                "assignment_confidence",
            ):
                rows[survivor_index][field] = category_row.get(field, "")
        if event.source_group_id:
            rows[survivor_index]["transfer_group"] = event.source_group_id
        suppressed.update(
            index for index in member_indexes if index != survivor_index
        )
        decision_record = {
            "decisionId": decision.decision_hash,
            "decisionType": "automatic-identity",
        }
        for index in member_indexes:
            decisions_by_index[index] = decision_record
        decision_projections.append(
            {
                **decision_record,
                "policyVersion": decision.policy_version,
                "policyHash": decision.policy_hash,
                "generationHash": decision.generation_hash,
                "confidenceTier": decision.confidence_tier.value,
                "confidenceBasisPoints": decision.confidence_basis_points,
                "rationaleCode": decision.rationale_code,
                "outcome": decision.outcome.value,
                "claimIds": list(decision.claim_ids),
                "observationIds": list(decision.observation_ids),
                "canonicalTransactionIds": [
                    event.canonical_event_id
                ],
                "memberActivityRefs": list(decision.observation_ids),
                "suppressedActivityRefs": sorted(
                    observation_id
                    for observation_id in event.member_observation_ids
                    if observation_id != event.selected_observation_id
                ),
                "linkedActivityRefs": [],
                "linkId": None,
                "featureVector": dict(decision.feature_vector),
                "competingCandidateProof": dict(
                    decision.competing_candidate_proof
                ),
                "sourceHashes": list(decision.source_hashes),
                "residualClassification": decision.residual_classification,
                "sourceAuthorityPolicyHash": decision.source_authority_policy_hash,
                "decisionHash": decision.decision_hash,
            }
        )
    report = resolution.report_document()
    return {
        "canonicalIds": canonical_ids,
        "suppressed": suppressed,
        "decisionsByIndex": decisions_by_index,
        "identityScope": generation.scope_document(),
        "decisionProjections": sorted(
            decision_projections, key=lambda item: item["decisionId"]
        ),
        "identity": {
            "policyVersion": report["policyVersion"],
            "policyHash": report["policyHash"],
            "policyDocument": report["policyDocument"],
            "generationHash": report["generationHash"],
            "canonicalStateHash": report["canonicalStateHash"],
            "automaticScopeRows": len(eligible),
            "appliedAutomaticDecisions": len(decision_projections),
            "safeAutomaticResolutions": report["counts"][
                "safeAutomaticResolutions"
            ],
            "unresolvedDuplicateGroups": report["counts"][
                "unresolvedDuplicateGroups"
            ],
            "sourceAuthority": report["sourceAuthority"],
            "residualByClass": report["residualByClass"],
            "sourceSuppressedClaims": report["counts"]["sourceSuppressedClaims"],
            "authorityCoveredClaims": report["counts"]["authorityCoveredClaims"],
            "authorityAmbiguousGroups": report["counts"][
                "authorityAmbiguousGroups"
            ],
        },
    }


def declared_duplicate_summaries(
    root: Path,
) -> tuple[DuplicateSummaryMapping, ...]:
    """Durable duplicate-summary account decisions, read from the private map."""

    try:
        return declarations.duplicate_summaries(root)
    except declarations.DeclarationError as exc:
        raise ReviewError(exc.code) from exc


def declared_token_scopes(root: Path) -> tuple[ProviderTokenScope, ...]:
    """Durable scoped provider-token decisions, read from the private map."""

    try:
        return declarations.token_scopes(root)
    except declarations.DeclarationError as exc:
        raise ReviewError(exc.code) from exc


def declared_source_authority(root: Path) -> IdentityPolicy:
    """The identity policy, carrying any durable coverage-authority intervals."""

    try:
        return declarations.source_authority(root)
    except declarations.DeclarationError as exc:
        raise ReviewError(exc.code) from exc


def declared_identity_inputs(root: Path) -> declarations.DeclaredIdentityInputs:
    """Every durable identity declaration, read exactly once per projection.

    The private shadow reads the same inputs through the same loader, so a
    declaration can never resolve a duplicate group in one producer and leave it
    unresolved in the other.
    """

    try:
        return declarations.load_declarations(root)
    except declarations.DeclarationError as exc:
        raise ReviewError(exc.code) from exc


def project(
    data_dir: str | Path,
    canonical_rows: Iterable[Any],
    source_rows: Iterable[Any],
    facts: Iterable[Any],
    *,
    repo_root: str | Path,
    legacy_transfer_review: dict[str, Any] | None = None,
    source_artifact_hashes: dict[str, str] | None = None,
) -> dict[str, Any]:
    root = Path(data_dir).resolve()
    rows = [_row_data(item) for item in canonical_rows]
    observations = _observations(source_rows)
    declared = declared_identity_inputs(root)
    duplicate_summaries = declared.duplicate_summaries
    token_scopes = declared.token_scopes
    identity_policy = declared.policy
    review_output = root / OUTPUT_RELATIVE
    if not (review_output / "current.json").is_file():
        if private_publications_exist(review_output) or private_publications_exist(
            review_output / "decisions"
        ):
            raise ReviewError("review-history-rollback")
        projection = _empty_projection(
            rows,
            observations,
            duplicate_summaries,
            token_scopes,
            identity_policy,
            source_artifact_hashes,
        )
        return _documents(projection)

    state = verified_state(root, repo_root=repo_root)
    if not state["decisions"]:
        projection = _empty_projection(
            rows,
            observations,
            duplicate_summaries,
            token_scopes,
            identity_policy,
            source_artifact_hashes,
        )
        projection["review"] = {
            "identityPolicy": projection["review"]["identityPolicy"],
            "queuePublicationId": state["queuePublicationId"],
            "decisionPublicationId": None,
            "baselinePublicationId": state["queue"]["baselinePublicationId"],
            "forensicPublicationId": state["queue"]["forensicPublicationId"],
            "candidateGraphHash": state["queue"]["candidateGraphHash"],
        }
        projection["sourcePaths"] = state["sourcePaths"]
        return _documents(projection)

    groups = {item["groupId"]: item for item in state["queue"]["groups"]}
    activities = state["activities"]
    (
        transfer_claims,
        transfer_legs,
        handoff_claims,
    ) = _legacy_authority_claims(facts, legacy_transfer_review)
    prepared_decisions = []
    blocked_indexes: set[int] = set()
    for decision in state["decisions"]:
        group = groups[decision["candidateGroupId"]]
        member_indexes: dict[str, int] = {}
        if group["reviewKind"] != "receipt-binding":
            member_indexes = {
                ref: _activity_row_index(activities[ref], rows)
                for ref in group["activityRefs"]
            }
            if (
                len(set(member_indexes.values())) != len(member_indexes)
                and decision["decisionType"] not in SUPPRESSION_TYPES
            ):
                raise ReviewError("canonical-duplicate-member-mapping")
            blocked_indexes.update(member_indexes.values())
        prepared_decisions.append((decision, group, member_indexes))

    automatic = _automatic_identity_projection(
        rows,
        blocked_indexes,
        duplicate_summaries,
        token_scopes,
        identity_policy,
        source_artifact_hashes,
    )
    canonical_ids = automatic["canonicalIds"]
    decisions_by_index: dict[int, dict[str, Any]] = automatic[
        "decisionsByIndex"
    ]
    suppressed: set[int] = automatic["suppressed"]
    links: dict[int, str] = {}
    decision_projections = list(automatic["decisionProjections"])

    for decision, group, member_indexes in prepared_decisions:
        decision_type = decision["decisionType"]
        member_source_ids = {
            str(activities[ref].get("sourceIdentity") or "")
            for ref in group["activityRefs"]
        }
        member_accounts = {
            str(activities[ref].get("canonicalAccountId") or "")
            for ref in group["activityRefs"]
        }
        if decision_type == "linked-transfer" and member_source_ids.intersection(
            transfer_claims
        ):
            raise ReviewError("duplicated-decision-authority")
        if decision_type == "linked-transfer" and any(
            (
                str(rows[index].get("account_id") or ""),
                str(rows[index].get("source_id") or ""),
            )
            in transfer_legs
            for index in member_indexes.values()
        ):
            raise ReviewError("duplicated-decision-authority")
        if decision_type in SUPPRESSION_TYPES and (
            member_source_ids.intersection(transfer_claims)
            or any(
                rows[index].get("transfer_group")
                or (
                    str(rows[index].get("account_id") or ""),
                    str(rows[index].get("source_id") or ""),
                )
                in transfer_legs
                for index in member_indexes.values()
            )
        ):
            raise ReviewError("suppression-conflicts-with-transfer-authority")
        if decision_type == "account-handoff-reissue" and member_accounts.intersection(
            handoff_claims
        ):
            raise ReviewError("duplicated-decision-authority")

        link_id = None
        if decision_type in SUPPRESSION_TYPES:
            survivor_index = member_indexes[decision["survivorActivityRef"]]
            shared_id = stable_hash(
                {
                    "kind": "canonical-transaction",
                    "decisionId": decision["decisionId"],
                }
            )
            for index in set(member_indexes.values()):
                canonical_ids[index] = shared_id
                decisions_by_index[index] = decision
                if index != survivor_index:
                    suppressed.add(index)
        elif decision_type == "linked-transfer":
            link_id = "lineage-link-" + stable_hash(
                {
                    "kind": "linked-transfer",
                    "decisionId": decision["decisionId"],
                }
            )[:24]
            if any(
                rows[index].get("transfer_group")
                for index in member_indexes.values()
            ):
                raise ReviewError("duplicated-decision-authority")
            for index in member_indexes.values():
                rows[index]["transfer_group"] = link_id
                links[index] = link_id
                decisions_by_index[index] = decision
        elif group["reviewKind"] != "receipt-binding":
            for index in member_indexes.values():
                decisions_by_index[index] = decision

        decision_projections.append(
            {
                "decisionId": decision["decisionId"],
                "candidateGroupId": decision["candidateGroupId"],
                "decisionType": decision_type,
                "canonicalTransactionIds": sorted(
                    {canonical_ids[index] for index in member_indexes.values()}
                ),
                "memberActivityRefs": group["activityRefs"],
                "suppressedActivityRefs": sorted(
                    (
                        ref
                        for ref in member_indexes
                        if ref != decision.get("survivorActivityRef")
                    )
                    if decision_type in SUPPRESSION_TYPES
                    else ()
                ),
                "linkedActivityRefs": (
                    group["activityRefs"] if decision_type == "linked-transfer" else []
                ),
                "linkId": link_id,
            }
        )

    row_to_observations = {
        index: _row_observations(row, observations)
        for index, row in enumerate(rows)
    }
    if any(not value for value in row_to_observations.values()):
        raise ReviewError("canonical-observation-unmapped")
    observation_records = []
    grouped_observations: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for index, row in enumerate(rows):
        decision = decisions_by_index.get(index)
        if index in suppressed:
            disposition = "suppressed"
        elif index in links:
            disposition = "linked"
        elif decision and decision["decisionType"] == "defer-insufficient-evidence":
            disposition = "deferred"
        else:
            disposition = "active"
        for item in row_to_observations[index]:
            record = {
                **item,
                "canonicalTransactionId": canonical_ids[index],
                "disposition": disposition,
                "decisionId": decision["decisionId"] if decision else None,
                "decisionType": decision["decisionType"] if decision else None,
                "linkId": links.get(index),
            }
            observation_records.append(record)
            grouped_observations[canonical_ids[index]].append(record)

    active_rows = [row for index, row in enumerate(rows) if index not in suppressed]
    canonical_transactions = []
    for canonical_id, members in grouped_observations.items():
        active = [item for item in members if item["disposition"] != "suppressed"]
        if len(active) != 1 and not all(
            _replay_fingerprint(item["transaction"])
            == _replay_fingerprint(active[0]["transaction"])
            for item in active[1:]
        ):
            raise ReviewError("canonical-transaction-survivor-conflict")
        selected = active[0]
        canonical_transactions.append(
            {
                "canonicalTransactionId": canonical_id,
                "activeObservationId": selected["observationId"],
                "memberObservationIds": sorted(
                    item["observationId"] for item in members
                ),
                "decisionId": selected["decisionId"],
                "decisionType": selected["decisionType"],
                "linkId": selected["linkId"],
            }
        )

    projection = {
        "rows": active_rows,
        "observationRecords": sorted(
            observation_records, key=lambda item: item["observationId"]
        ),
        "canonicalTransactions": sorted(
            canonical_transactions,
            key=lambda item: item["canonicalTransactionId"],
        ),
        "decisionProjections": sorted(
            decision_projections, key=lambda item: item["decisionId"]
        ),
        "identityScope": automatic["identityScope"],
        "review": {
            "queuePublicationId": state["queuePublicationId"],
            "decisionPublicationId": state["decisionPublicationId"],
            "baselinePublicationId": state["queue"]["baselinePublicationId"],
            "forensicPublicationId": state["queue"]["forensicPublicationId"],
            "candidateGraphHash": state["queue"]["candidateGraphHash"],
            "identityPolicy": automatic["identity"],
        },
        "sourcePaths": state["sourcePaths"],
    }
    return _documents(projection)


def _documents(projection: dict[str, Any]) -> dict[str, Any]:
    observations = projection["observationRecords"]
    dispositions = Counter(item["disposition"] for item in observations)
    observation_document = {
        "schemaVersion": SCHEMA_VERSION,
        "kind": "canonical-transaction-observations",
        "private": True,
        "observationCount": len(observations),
        "observations": observations,
        "identityScope": projection["identityScope"],
    }
    lineage_document = {
        "schemaVersion": SCHEMA_VERSION,
        "kind": "canonical-transaction-lineage",
        "private": True,
        "transactionSetHash": transaction_set_hash(projection["rows"]),
        **projection["review"],
        "counts": {
            "source-observations": len(observations),
            "canonical-transactions": len(projection["canonicalTransactions"]),
            "decision-projections": len(projection["decisionProjections"]),
            **{
                f"{name}-observations": count
                for name, count in sorted(dispositions.items())
            },
        },
        "canonicalTransactions": projection["canonicalTransactions"],
        "decisionProjections": projection["decisionProjections"],
    }
    lineage_document = bind_transaction_rows(
        lineage_document,
        observation_document,
        projection["rows"],
    )
    return {
        "rows": projection["rows"],
        "observations": observation_document,
        "lineage": lineage_document,
        "summary": {
            "schemaVersion": SCHEMA_VERSION,
            **projection["review"],
            "counts": lineage_document["counts"],
        },
        "sourcePaths": projection["sourcePaths"],
    }


def _normalized_transaction(item: Any) -> dict[str, str]:
    row = _row_data(item)
    return {
        key: (
            "true"
            if value is True
            else "false"
            if value is False
            else ""
            if value is None
            else str(value)
        )
        for key, value in sorted(row.items())
    }


def _replay_fingerprint(transaction: dict[str, Any]) -> str:
    return stable_hash(
        {
            key: value
            for key, value in transaction.items()
            if key != "source_file"
        }
    )


def transaction_set_hash(rows: Iterable[Any]) -> str:
    normalized = [_normalized_transaction(item) for item in rows]
    return stable_hash(sorted(normalized, key=_row_sort))


def bind_transaction_rows(
    lineage: dict[str, Any],
    observations: dict[str, Any],
    rows: Iterable[Any],
) -> dict[str, Any]:
    normalized_rows = [_normalized_transaction(item) for item in rows]
    rows_by_source: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    splits_by_parent: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for row in normalized_rows:
        if "account_id" not in row or "source_id" not in row:
            raise ReviewError("canonical-transaction-publication-unmapped")
        account_id = row["account_id"]
        source_id = row["source_id"]
        rows_by_source[(account_id, source_id)].append(row)
        position = source_id.find(":split:")
        while position >= 0:
            splits_by_parent[(account_id, source_id[:position])].append(row)
            position = source_id.find(":split:", position + 1)
    observations_by_id = {
        str(item["observationId"]): item
        for item in observations.get("observations", [])
        if isinstance(item, dict) and item.get("observationId")
    }
    canonical_transactions = []
    claimed: Counter[str] = Counter()
    for item in lineage.get("canonicalTransactions", []):
        active = observations_by_id.get(str(item.get("activeObservationId") or ""))
        if active is None or not isinstance(active.get("transaction"), dict):
            raise ReviewError("canonical-transaction-publication-unmapped")
        parent = active["transaction"]
        account_id = str(parent.get("account_id") or "")
        source_id = str(parent.get("source_id") or "")
        collision_id = f"{source_id}:collision:{source_collision_suffix(parent)}"
        matches = [
            *rows_by_source.get((account_id, source_id), ()),
            *rows_by_source.get((account_id, collision_id), ()),
            *splits_by_parent.get((account_id, collision_id), ()),
            *(
                row
                for row in splits_by_parent.get((account_id, source_id), ())
                if row.get("split_group")
            ),
        ]
        if not matches:
            raise ReviewError("canonical-transaction-publication-unmapped")
        fingerprints = sorted(stable_hash(row) for row in matches)
        claimed.update(fingerprints)
        canonical_transactions.append(
            {
                **item,
                "publishedTransactionFingerprints": fingerprints,
            }
        )
    expected = Counter(stable_hash(row) for row in normalized_rows)
    if claimed != expected:
        raise ReviewError("canonical-transaction-publication-unmapped")
    return {
        **lineage,
        "transactionSetHash": stable_hash(
            sorted(normalized_rows, key=_row_sort)
        ),
        "canonicalTransactions": canonical_transactions,
    }


def _validate_published_policy_document(identity: dict[str, Any]) -> None:
    """Check the published authority aggregate is internally consistent.

    The projector binds to ``identityPolicy`` and re-derives every hash from the
    published policy document rather than trusting it, so the two have to agree
    here.  The aggregate is also checked on its own terms: a safe report that
    names an interval count, a declared flag, and a set of interval ids has to
    agree with itself, or a reader cannot tell a real coverage declaration from
    a malformed one.

    ``policyDocument`` is required: the rebuild projector re-derives every hash
    from it rather than trusting the published values, so a block without it is
    one the projector will reject.
    """

    aggregate = identity["sourceAuthority"]
    intervals = aggregate.get("intervals")
    if (
        not isinstance(intervals, list)
        or not isinstance(aggregate.get("policyVersion"), str)
        or not aggregate["policyVersion"]
        or any(
            not isinstance(aggregate.get(key), str)
            or not HEX_64.fullmatch(aggregate[key])
            for key in ("policyHash", "authorityHash")
        )
        or aggregate.get("intervalCount") != len(intervals)
        or aggregate.get("declared") is not bool(intervals)
        or any(
            not isinstance(item, dict)
            or not isinstance(item.get("intervalId"), str)
            or not HEX_64.fullmatch(item["intervalId"])
            for item in intervals
        )
        or len({item["intervalId"] for item in intervals}) != len(intervals)
    ):
        raise ReviewError("canonical-identity-authority-document-invalid")
    document = identity.get("policyDocument")
    if (
        not isinstance(document, dict)
        or document.get("version") != identity["policyVersion"]
        or content_hash(document) != identity["policyHash"]
    ):
        raise ReviewError("canonical-identity-policy-invalid")
    authority = document.get("sourceAuthority")
    if (
        not isinstance(authority, dict)
        or set(authority) != {"policy", "policyHash", "intervals"}
        or not isinstance(authority["policy"], dict)
        or not isinstance(authority["intervals"], list)
        or content_hash(authority["policy"]) != authority["policyHash"]
        or aggregate["policyHash"] != authority["policyHash"]
        or aggregate["authorityHash"] != content_hash(authority)
        or aggregate["policyVersion"] != authority["policy"].get("version")
        # An interval id is the hash of the interval document, so the safe
        # aggregate cannot claim an interval the evidence does not contain.
        or sorted(content_hash(item) for item in authority["intervals"])
        != sorted(item["intervalId"] for item in intervals)
    ):
        raise ReviewError("canonical-identity-authority-document-invalid")


def _validate_automatic_identity(lineage: dict[str, Any]) -> None:
    identity = lineage.get("identityPolicy")
    if (
        not isinstance(identity, dict)
        or not isinstance(identity.get("policyVersion"), str)
        or not identity["policyVersion"]
        or not isinstance(identity.get("automaticScopeRows"), int)
        or identity["automaticScopeRows"] < 0
        or not isinstance(identity.get("safeAutomaticResolutions"), int)
        or identity["safeAutomaticResolutions"] < 0
        or not isinstance(identity.get("appliedAutomaticDecisions"), int)
        or identity["appliedAutomaticDecisions"] < 0
        or not isinstance(identity.get("unresolvedDuplicateGroups"), int)
        or identity["unresolvedDuplicateGroups"] < 0
        or not isinstance(identity.get("sourceAuthority"), dict)
        or not isinstance(identity.get("residualByClass"), dict)
        or any(
            not isinstance(identity.get(key), int) or identity[key] < 0
            for key in (
                "sourceSuppressedClaims",
                "authorityCoveredClaims",
                "authorityAmbiguousGroups",
            )
        )
        or any(
            not isinstance(identity.get(key), str)
            or not HEX_64.fullmatch(identity[key])
            for key in (
                "policyHash",
                "generationHash",
                "canonicalStateHash",
            )
        )
    ):
        raise ReviewError("canonical-identity-policy-invalid")
    _validate_published_policy_document(identity)
    automatic = [
        item
        for item in lineage["decisionProjections"]
        if isinstance(item, dict)
        and item.get("decisionType") == "automatic-identity"
    ]
    if len(automatic) != identity["appliedAutomaticDecisions"]:
        raise ReviewError("canonical-identity-policy-invalid")
    for decision in automatic:
        feature_vector = decision.get("featureVector")
        proof = decision.get("competingCandidateProof")
        source_hashes = decision.get("sourceHashes")
        claim_ids = decision.get("claimIds")
        observation_ids = decision.get("observationIds")
        canonical_ids = decision.get("canonicalTransactionIds")
        if (
            decision.get("policyVersion") != identity["policyVersion"]
            or decision.get("policyHash") != identity["policyHash"]
            or decision.get("generationHash") != identity["generationHash"]
            or decision.get("confidenceBasisPoints") != 10_000
            or decision.get("confidenceTier")
            not in {
                "exact-scoped-identity",
                "explicit-lineage",
                "unique-cross-source",
                "authoritative-source-coverage",
            }
            or not isinstance(feature_vector, dict)
            or not isinstance(proof, dict)
            or not isinstance(source_hashes, list)
            or not source_hashes
            or any(
                not isinstance(value, str) or not HEX_64.fullmatch(value)
                for value in source_hashes
            )
            or not isinstance(claim_ids, list)
            or any(
                not isinstance(value, str) or not HEX_64.fullmatch(value)
                for value in claim_ids
            )
            or not isinstance(observation_ids, list)
            or any(
                not isinstance(value, str) or not HEX_64.fullmatch(value)
                for value in observation_ids
            )
            or not isinstance(canonical_ids, list)
            or not canonical_ids
            or any(
                not isinstance(value, str) or not HEX_64.fullmatch(value)
                for value in canonical_ids
            )
            or decision.get("decisionId") != decision.get("decisionHash")
            or decision.get("residualClassification")
            not in {
                "distinct",
                "source-suppressed",
                "transfer",
                "correction",
                "reversal",
                "unresolved",
            }
            or (
                decision.get("outcome") == "source-suppressed"
                and (
                    decision.get("residualClassification") != "source-suppressed"
                    or decision.get("confidenceTier")
                    != "authoritative-source-coverage"
                    or not isinstance(
                        decision.get("sourceAuthorityPolicyHash"), str
                    )
                    or not HEX_64.fullmatch(decision["sourceAuthorityPolicyHash"])
                )
            )
            or (
                decision.get("outcome") != "source-suppressed"
                and decision.get("sourceAuthorityPolicyHash") is not None
            )
        ):
            raise ReviewError("canonical-identity-decision-invalid")
        decision_body = {
            "generationHash": decision["generationHash"],
            "policyVersion": decision["policyVersion"],
            "policyHash": decision["policyHash"],
            "outcome": decision.get("outcome"),
            "confidenceTier": decision["confidenceTier"],
            "confidenceBasisPoints": decision["confidenceBasisPoints"],
            "rationaleCode": decision.get("rationaleCode"),
            "claimIds": sorted(set(claim_ids)),
            "observationIds": sorted(set(observation_ids)),
            "canonicalEventIds": sorted(set(canonical_ids)),
            "featureVector": feature_vector,
            "competingCandidateProof": proof,
            "sourceHashes": sorted(set(source_hashes)),
            "humanOverrideId": None,
        }
        if content_hash(decision_body) != decision["decisionHash"]:
            raise ReviewError("canonical-identity-decision-invalid")


def validate_identity_scope(scope: Any) -> None:
    """Validate the published identity scope when a publication carries one.

    Absence is tolerated so that publications written before the scope existed
    still validate; consumers that need an exactly replayable generation treat
    absence as a blocker rather than as licence to guess the input set.
    """

    if scope is None:
        return
    if (
        not isinstance(scope, dict)
        or scope.get("schemaVersion") != SCHEMA_VERSION
        or scope.get("kind") != "canonical-identity-scope"
        or scope.get("private") is not True
        or not isinstance(scope.get("rows"), list)
        or any(not isinstance(item, dict) for item in scope["rows"])
        or scope.get("rowCount") != len(scope["rows"])
        or scope.get("scopeHash") != content_hash(scope["rows"])
    ):
        raise ReviewError("canonical-identity-scope-invalid")


def validate_documents(
    observations: Any,
    lineage: Any,
    transactions: list[dict[str, str]],
) -> None:
    if (
        not isinstance(observations, dict)
        or observations.get("schemaVersion") != SCHEMA_VERSION
        or observations.get("kind") != "canonical-transaction-observations"
        or observations.get("private") is not True
        or not isinstance(observations.get("observations"), list)
        or observations.get("observationCount")
        != len(observations["observations"])
    ):
        raise ReviewError("canonical-observation-publication-invalid")
    records = observations["observations"]
    if any(not isinstance(item, dict) for item in records):
        raise ReviewError("canonical-observation-publication-invalid")
    validate_identity_scope(observations.get("identityScope"))
    dispositions = Counter(item.get("disposition") for item in records)
    ids = {
        str(item.get("observationId") or "")
        for item in records
    }
    if (
        "" in ids
        or len(ids) != len(records)
        or any(
            item.get("observationFingerprint")
            != stable_hash(item.get("transaction"))
            for item in records
        )
        or any(
            disposition
            not in {"active", "deferred", "linked", "suppressed"}
            for disposition in dispositions
        )
    ):
        raise ReviewError("canonical-observation-publication-invalid")
    if (
        not isinstance(lineage, dict)
        or lineage.get("schemaVersion") != SCHEMA_VERSION
        or lineage.get("kind") != "canonical-transaction-lineage"
        or lineage.get("private") is not True
        or lineage.get("transactionSetHash")
        != transaction_set_hash(transactions)
        or not isinstance(lineage.get("canonicalTransactions"), list)
        or not isinstance(lineage.get("decisionProjections"), list)
        or bool(transactions) != bool(lineage["canonicalTransactions"])
    ):
        raise ReviewError("canonical-lineage-publication-invalid")
    _validate_automatic_identity(lineage)
    expected_counts = {
        "source-observations": len(records),
        "canonical-transactions": len(lineage["canonicalTransactions"]),
        "decision-projections": len(lineage["decisionProjections"]),
        **{
            f"{name}-observations": count
            for name, count in sorted(dispositions.items())
        },
    }
    if lineage.get("counts") != expected_counts:
        raise ReviewError("canonical-lineage-publication-invalid")
    try:
        rebound = bind_transaction_rows(lineage, observations, transactions)
    except ReviewError:
        raise ReviewError("canonical-lineage-publication-invalid") from None
    if (
        rebound["transactionSetHash"] != lineage["transactionSetHash"]
        or rebound["canonicalTransactions"]
        != lineage["canonicalTransactions"]
    ):
        raise ReviewError("canonical-lineage-publication-invalid")
    claimed = [
        str(item_id)
        for item in lineage["canonicalTransactions"]
        if isinstance(item, dict)
        for item_id in item.get("memberObservationIds", [])
    ]
    if len(claimed) != len(ids) or set(claimed) != ids:
        raise ReviewError("canonical-lineage-publication-invalid")
    observations_by_id = {
        str(item["observationId"]): item for item in records
    }
    canonical_ids: set[str] = set()
    published_fingerprints: Counter[str] = Counter()
    for item in lineage["canonicalTransactions"]:
        if not isinstance(item, dict):
            raise ReviewError("canonical-lineage-publication-invalid")
        canonical_id = str(item.get("canonicalTransactionId") or "")
        member_ids = item.get("memberObservationIds")
        active_id = str(item.get("activeObservationId") or "")
        active_replay_fingerprint = _replay_fingerprint(
            observations_by_id.get(active_id, {}).get("transaction", {})
        )
        published = item.get("publishedTransactionFingerprints")
        if (
            not HEX_64.fullmatch(canonical_id)
            or canonical_id in canonical_ids
            or not isinstance(member_ids, list)
            or len(set(member_ids)) != len(member_ids)
            or active_id not in member_ids
            or not isinstance(published, list)
            or not published
            or any(
                not isinstance(fingerprint, str)
                or not HEX_64.fullmatch(fingerprint)
                for fingerprint in published
            )
            or any(
                observations_by_id.get(str(member_id), {}).get(
                    "canonicalTransactionId"
                )
                != canonical_id
                for member_id in member_ids
            )
            or observations_by_id.get(active_id, {}).get("disposition")
            == "suppressed"
            or any(
                _replay_fingerprint(
                    observations_by_id[str(member_id)]["transaction"]
                )
                != active_replay_fingerprint
                for member_id in member_ids
                if observations_by_id[str(member_id)]["disposition"]
                != "suppressed"
            )
        ):
            raise ReviewError("canonical-lineage-publication-invalid")
        published_fingerprints.update(published)
        canonical_ids.add(canonical_id)
    if published_fingerprints != Counter(
        stable_hash(_normalized_transaction(row)) for row in transactions
    ):
        raise ReviewError("canonical-lineage-publication-invalid")
