"""Synthetic resolver-to-durable-identity contract tests."""

from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal

from finance_store.accepted_identity import (
    AcceptedRevision,
    ClaimOwnership,
    ProjectionEvidence,
    plan_accepted_identity,
)
from finance_store.domain import content_hash
from finance_store.identity import HumanOverride, IdentityObservation
from finance_store.identity import resolve_identity as resolver


def observation(name="A", *, family="simplefin", group="", amount="-12.34"):
    return IdentityObservation(
        observation_id=f"SYN-OBS-{name}",
        source_family=family,
        source_connection_id=f"SYN-CONNECTION-{family}",
        source_account_id="SYN-ACCOUNT",
        canonical_account_id="SYN-CANONICAL",
        provider_transaction_id=f"SYN-TXN-{name}",
        provider_id_kind="simplefin-id" if family == "simplefin" else "ofx-fitid",
        source_hash=content_hash(f"SYN-SOURCE-{name}"),
        source_day=date(2026, 1, 15),
        observed_at=datetime(2026, 1, 16, tzinfo=timezone.utc),
        signed_amount=Decimal(amount),
        currency="USD",
        description=("Synthetic statement detail" if family == "ofx" else "Synthetic merchant"),
        source_group_id=group,
    )


def resolve_identity(observations):
    """Synthetic groups represent explicit human merge decisions, not similarity."""

    result = resolver(observations)
    by_observation = {
        observation_id: claim.claim_id
        for claim in result.claims for observation_id in claim.observation_ids
    }
    groups = {}
    for item in observations:
        if item.source_group_id:
            groups.setdefault(item.source_group_id, set()).add(by_observation[item.observation_id])
    overrides = tuple(
        HumanOverride(
            override_id=f"SYN-ACCEPTED-{group}", version=1, action="merge",
            claim_ids=tuple(sorted(claim_ids)), rationale_hash=content_hash(group),
            decided_at=datetime(2026, 2, 1, tzinfo=timezone.utc),
        )
        for group, claim_ids in sorted(groups.items()) if len(claim_ids) > 1
    )
    return resolver(observations, overrides=overrides) if overrides else result


def registered(resolution):
    mappings = plan_accepted_identity(resolution, {}, {})
    by_id = {item.canonical_event_id: item for item in mappings}
    claims = {claim.claim_id: claim for claim in resolution.claims}
    owners = {
        claim: ClaimOwnership(
            by_id[event.canonical_event_id].accepted_event_id,
            event.canonical_account_hash,
            claims[claim].canonical_account_hash,
        )
        for event in resolution.canonical_events
        for claim in event.member_claim_ids
    }
    revisions = {
        item.accepted_event_id: AcceptedRevision(item.revision_number, item.revision_hash)
        for item in mappings
    }
    return mappings, owners, revisions


def test_real_resolver_membership_growth_keeps_exact_claim_address():
    a = observation(group="SYN-GROUP")
    before = resolve_identity((a,))
    initial, owners, revisions = registered(before)
    b = observation("B", family="ofx", group="SYN-GROUP")
    after = resolve_identity((b, a))
    assert len(after.canonical_events) == 1
    assert before.canonical_events[0].canonical_event_id != (
        after.canonical_events[0].canonical_event_id
    )
    mapped = plan_accepted_identity(after, owners, revisions)
    assert mapped[0].accepted_event_id == initial[0].accepted_event_id
    assert mapped[0].revision_number == 2
    assert mapped[0].new_revision
    assert mapped == plan_accepted_identity(resolve_identity((a, b)), owners, revisions)


def test_merge_and_split_are_local_explicit_outcomes_not_reinterpretations():
    a, b = observation(), observation("B", family="ofx")
    distinct = resolve_identity((a, b))
    initial, owners, revisions = registered(distinct)
    merged = resolve_identity((
        replace(a, source_group_id="SYN-GROUP"),
        replace(b, source_group_id="SYN-GROUP"),
        observation("INDEPENDENT", amount="-91.23"),
    ))
    mappings = plan_accepted_identity(merged, owners, revisions)
    conflict = next(item for item in mappings if item.outcome == "conflict")
    assert "many-existing-merge" in conflict.conflict_reasons
    assert set(conflict.prior_event_ids) == {item.accepted_event_id for item in initial}
    assert len([item for item in mappings if item.outcome == "created"]) == 1
    _, merged_owners, merged_revisions = registered(merged)
    split = plan_accepted_identity(distinct, merged_owners, merged_revisions)
    assert len(split) == 2
    assert all("one-existing-split" in item.conflict_reasons for item in split)
    assert all(item.accepted_event_id is None for item in split)


def test_account_move_cannot_steal_registered_claim():
    a = observation()
    before = resolve_identity((a,))
    _, owners, revisions = registered(before)
    after = resolve_identity((replace(a, canonical_account_id="SYN-OTHER-OWNER"),))
    assert before.claims[0].claim_id == after.claims[0].claim_id
    mapping, = plan_accepted_identity(after, owners, revisions)
    assert mapping.conflict_reasons == ("account-ownership-change",)
    assert mapping.accepted_event_id is None


def test_legacy_binding_split_or_multiple_targets_cannot_be_reused():
    a, b = observation(group="SYN-GROUP"), observation(
        "B", family="ofx", group="SYN-GROUP"
    )
    merged = resolve_identity((a, b))
    binding = ProjectionEvidence(
        binding_id="SYN-BINDING", target_application="wealthfolio",
        target_activity_hash=content_hash("SYN-TARGET"),
        canonical_account_hash=merged.canonical_events[0].canonical_account_hash,
        claim_ids=merged.canonical_events[0].member_claim_ids,
    )
    split = resolve_identity((
        replace(a, source_group_id=""), replace(b, source_group_id=""),
    ))
    mappings = plan_accepted_identity(split, {}, {}, (binding,))
    assert all("projection-binding-split" in item.conflict_reasons for item in mappings)
    other = replace(binding, binding_id="SYN-SECOND", target_activity_hash=content_hash("SYN-2"))
    mapping, = plan_accepted_identity(merged, {}, {}, (binding, other))
    assert mapping.conflict_reasons == ("multiple-projection-targets",)


def test_replay_and_unchanged_event_in_another_generation_need_no_revision():
    a = observation()
    before = resolve_identity((a,))
    initial, owners, revisions = registered(before)
    after = resolve_identity((observation("SAFE", amount="-77.00"), a))
    mappings = plan_accepted_identity(after, owners, revisions)
    continued = next(item for item in mappings if item.outcome == "continued")
    assert continued.accepted_event_id == initial[0].accepted_event_id
    assert continued.revision_number == 1
    assert not continued.new_revision


def test_reused_provider_token_in_another_namespace_is_not_continuity():
    a = observation()
    initial, owners, revisions = registered(resolve_identity((a,)))
    other = replace(a, source_connection_id="SYN-OTHER-CONNECTION")
    mapped, = plan_accepted_identity(resolve_identity((other,)), owners, revisions)
    assert mapped.outcome == "created"
    assert mapped.accepted_event_id != initial[0].accepted_event_id


def test_emitted_unresolved_events_are_not_eligible_for_acceptance():
    a = observation()
    b = replace(observation("B", family="ofx"), description=a.description)
    resolved = resolver((a, b, observation("SAFE", amount="-83.01")))
    mappings = plan_accepted_identity(resolved, {}, {})
    conflicts = [item for item in mappings if item.outcome == "conflict"]
    assert len(conflicts) == 2
    assert all("unresolved-identity-component" in item.conflict_reasons for item in conflicts)
    unresolved_hashes = {
        decision.decision_hash for decision in resolved.decisions
        if decision.residual_classification == "unresolved"
    }
    assert all(set(item.review_decision_hashes) == unresolved_hashes for item in conflicts)
    assert all(item.accepted_event_id is None for item in conflicts)
    assert len([item for item in mappings if item.outcome == "created"]) == 1
