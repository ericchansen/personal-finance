"""Durable addresses for accepted resolver output, not another identity resolver.

Continuity requires an exact, previously registered source claim. Economic
similarity is deliberately irrelevant here. Acceptance order is explicit; it
is not inferred from policy rank, generation number, or observation timestamps.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Mapping

from .domain import content_hash, stable_id
from .identity import CanonicalEvent, IdentityResolution


ACCEPTANCE_CONTRACT = "accepted-identity-v1"


@dataclass(frozen=True, slots=True)
class ClaimOwnership:
    accepted_event_id: str
    canonical_account_hash: str
    source_canonical_account_hash: str | None = None


@dataclass(frozen=True, slots=True)
class AcceptedRevision:
    revision_number: int
    revision_hash: str


@dataclass(frozen=True, slots=True)
class ProjectionEvidence:
    binding_id: str
    target_application: str
    target_activity_hash: str
    canonical_account_hash: str
    claim_ids: tuple[str, ...]
    accepted_event_id: str | None = None
    is_active: bool = True


@dataclass(frozen=True, slots=True)
class AcceptedEventMapping:
    canonical_event_id: str
    accepted_event_id: str | None
    revision_number: int | None
    revision_hash: str | None
    outcome: str
    prior_event_ids: tuple[str, ...]
    conflict_reasons: tuple[str, ...] = ()
    projection_binding_ids: tuple[str, ...] = ()
    review_decision_hashes: tuple[str, ...] = ()
    new_revision: bool = False
    bindings: tuple[ProjectionEvidence, ...] = ()


@dataclass(frozen=True, slots=True)
class AcceptedIdentityResult:
    generation_hash: str
    previous_generation_hash: str | None
    inserted: bool
    mappings: tuple[AcceptedEventMapping, ...]

    @property
    def conflicts(self) -> tuple[AcceptedEventMapping, ...]:
        return tuple(item for item in self.mappings if item.outcome == "conflict")


def revision_hash(event: CanonicalEvent, resolution: IdentityResolution) -> str:
    """Hash selected fields and evidence without changing their interpretation."""

    claims = {claim.claim_id: claim for claim in resolution.claims}
    return content_hash(
        {
            "contract": ACCEPTANCE_CONTRACT,
            "policyHash": resolution.policy.policy_hash,
            "accountHash": event.canonical_account_hash,
            "selectedObservation": event.selected_observation_id,
            "claims": [
                {
                    "id": claim_id,
                    "observations": sorted(claims[claim_id].observation_ids),
                    "sourceHashes": sorted(claims[claim_id].source_hashes),
                }
                for claim_id in sorted(event.member_claim_ids)
            ],
            "observations": sorted(event.member_observation_ids),
            "sourceDay": event.source_day.isoformat(),
            "amount": format(event.signed_amount, "f"),
            "currency": event.currency,
            "descriptionHash": content_hash(event.description),
            "status": event.status,
            "categoryHash": content_hash(event.category),
            "trusted": event.trusted,
        }
    )


def plan_accepted_identity(
    resolution: IdentityResolution,
    ownership: Mapping[str, ClaimOwnership],
    revisions: Mapping[str, AcceptedRevision],
    bindings: tuple[ProjectionEvidence, ...] = (),
) -> tuple[AcceptedEventMapping, ...]:
    """Map exact claim continuity, quarantining the entire ambiguous component.

    A conflict never registers new claims or selects a revision. Thus neither a
    merge nor a split can steal a prior identity or its application binding.
    Other events in the same accepted generation can still advance.
    """

    events = sorted(resolution.canonical_events, key=lambda event: event.canonical_event_id)
    claims = {claim.claim_id: claim for claim in resolution.claims}
    observations = {item.observation_id: item for item in resolution.observations}
    unresolved = tuple(
        decision for decision in resolution.decisions
        if decision.residual_classification == "unresolved"
    )
    review_hashes: dict[str, tuple[str, ...]] = {}
    prior: dict[str, tuple[str, ...]] = {}
    owners_to_events: dict[str, set[str]] = defaultdict(set)
    reasons: dict[str, set[str]] = defaultdict(set)
    candidates: dict[str, str] = {}
    event_bindings: dict[str, list[ProjectionEvidence]] = defaultdict(list)
    for event in events:
        key = event.canonical_event_id
        if not event.member_claim_ids:
            raise ValueError("accepted event requires resolver source claims")
        review_hashes[key] = tuple(sorted(
            decision.decision_hash for decision in unresolved
            if key in decision.canonical_event_ids
            or set(decision.claim_ids).intersection(event.member_claim_ids)
            or set(decision.observation_ids).intersection(event.member_observation_ids)
        ))
        if review_hashes[key]:
            reasons[key].add("unresolved-identity-component")
        if not event.trusted or event.status == "excluded":
            reasons[key].add("source-excluded-or-untrusted")
        if observations[event.selected_observation_id].account_status == "unknown":
            reasons[key].add("account-state-unknown")
        prior[key] = tuple(sorted({
            ownership[claim_id].accepted_event_id
            for claim_id in event.member_claim_ids
            if claim_id in ownership
        }))
        for owner in prior[key]:
            owners_to_events[owner].add(key)
        if len(prior[key]) > 1:
            reasons[key].add("many-existing-merge")
        for claim_id in event.member_claim_ids:
            if (
                claim_id in ownership
                and (
                    ownership[claim_id].canonical_account_hash != event.canonical_account_hash
                    or (
                        ownership[claim_id].source_canonical_account_hash
                        or ownership[claim_id].canonical_account_hash
                    ) != claims[claim_id].canonical_account_hash
                )
            ):
                reasons[key].add("account-ownership-change")
        # A first acceptance is reproducible, but not invariant to which evidence
        # was accepted first. Later generations use the registry, never this seed.
        candidates[key] = (
            prior[key][0] if len(prior[key]) == 1 else stable_id(
                "accepted_identity_event",
                event.canonical_account_hash,
                min(event.member_claim_ids),
            )
        )
    for event_keys in owners_to_events.values():
        if len(event_keys) > 1:
            for key in event_keys:
                reasons[key].add("one-existing-split")

    for binding in bindings:
        matching = [
            event for event in events
            if set(binding.claim_ids).intersection(event.member_claim_ids)
            or binding.accepted_event_id in prior[event.canonical_event_id]
        ]
        for event in matching:
            key = event.canonical_event_id
            event_bindings[key].append(binding)
            if len(matching) > 1:
                reasons[key].add("projection-binding-split")
            if not binding.is_active:
                reasons[key].add("inactive-projection-binding")
            if binding.canonical_account_hash != event.canonical_account_hash:
                reasons[key].add("account-ownership-change")
            if (
                binding.accepted_event_id is not None
                and binding.accepted_event_id != candidates[key]
            ):
                reasons[key].add("projection-binding-owner-conflict")
            if binding.accepted_event_id is None and not set(binding.claim_ids).issubset(
                event.member_claim_ids
            ):
                reasons[key].add("unmapped-historical-binding-members")
            if (
                binding.accepted_event_id is None
                and prior[key]
                and not any(
                    claim_id in ownership
                    and ownership[claim_id].accepted_event_id == candidates[key]
                    for claim_id in binding.claim_ids
                )
            ):
                reasons[key].add("historical-binding-identity-conflict")

    result = []
    for event in events:
        key = event.canonical_event_id
        attached = tuple(sorted(event_bindings[key], key=lambda item: item.binding_id))
        by_application: dict[str, set[str]] = defaultdict(set)
        for binding in attached:
            by_application[binding.target_application].add(binding.target_activity_hash)
        if any(len(targets) > 1 for targets in by_application.values()):
            reasons[key].add("multiple-projection-targets")
        if reasons[key]:
            result.append(AcceptedEventMapping(
                canonical_event_id=key,
                accepted_event_id=None,
                revision_number=None,
                revision_hash=None,
                outcome="conflict",
                prior_event_ids=prior[key],
                conflict_reasons=tuple(sorted(reasons[key])),
                projection_binding_ids=tuple(item.binding_id for item in attached),
                review_decision_hashes=review_hashes[key],
            ))
            continue
        accepted_id = candidates[key]
        fingerprint = revision_hash(event, resolution)
        current = revisions.get(accepted_id)
        changed = current is None or current.revision_hash != fingerprint
        number = 1 if current is None else current.revision_number + int(changed)
        result.append(AcceptedEventMapping(
            canonical_event_id=key,
            accepted_event_id=accepted_id,
            revision_number=number,
            revision_hash=fingerprint,
            outcome="created" if not prior[key] else "continued",
            prior_event_ids=prior[key],
            projection_binding_ids=tuple(item.binding_id for item in attached),
            review_decision_hashes=review_hashes[key],
            new_revision=changed,
            bindings=attached,
        ))
    return tuple(result)
