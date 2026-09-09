"""PostgreSQL persistence for canonical identity resolution generations."""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

from .accepted_identity import (
    ACCEPTANCE_CONTRACT,
    AcceptedEventMapping,
    AcceptedIdentityResult,
    AcceptedRevision,
    ClaimOwnership,
    ProjectionEvidence,
    plan_accepted_identity,
)
from .domain import content_hash, require_hash, stable_id
from .identity import (
    DEFAULT_POLICY,
    PROVIDER_TOKEN_SCOPE_POLICY_VERSION,
    ConfidenceTier,
    DecisionOutcome,
    IdentityResolution,
    RelationKind,
)
from .postgres import GLOBAL_WRITER_LOCK


IDENTITY_WRITER_LOCK = "finance-canonical-identity-writer"
POLICY_NAME = "canonical-transaction-identity"


def _lock_identity_writer(connection: Any) -> None:
    if getattr(connection, "autocommit", False):
        from psycopg.pq import TransactionStatus

        if connection.info.transaction_status != TransactionStatus.INTRANS:
            raise ValueError("identity persistence requires a caller-owned transaction")
    connection.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
        (GLOBAL_WRITER_LOCK,),
    )
    gate = connection.execute(
        "SELECT migrations_blocked FROM finance.writer_gate WHERE singleton"
    ).fetchone()
    if gate is None or _cell(gate, "migrations_blocked", 0):
        raise ValueError("finance shadow writers are blocked for migration")
    connection.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
        (IDENTITY_WRITER_LOCK,),
    )


@dataclass(frozen=True, slots=True)
class PersistedIdentityGeneration:
    policy_id: str
    generation_id: str
    generation_number: int
    generation_hash: str
    inserted: bool
    claim_count: int
    event_count: int
    decision_count: int


def _persist_generation_projection_bindings(
    connection: Any,
    resolution: IdentityResolution,
    target_activity_hashes: Mapping[str, str],
    *,
    target_application: str = "wealthfolio",
) -> int:
    """Record already-applied projection identities without calling the application."""

    if target_application != "wealthfolio":
        raise ValueError("only the Wealthfolio projection contract is supported")
    _lock_identity_writer(connection)
    events = {event.canonical_event_id: event for event in resolution.canonical_events}
    unknown = set(target_activity_hashes) - set(events)
    if unknown:
        raise ValueError("projection binding references an unknown canonical event")
    for target_hash in target_activity_hashes.values():
        try:
            require_hash(target_hash)
        except ValueError as exc:
            raise ValueError("target activity identity must be a SHA-256 hash") from exc
    generation_id = _uuid("generation", resolution.generation_hash)
    for canonical_id, target_hash in sorted(target_activity_hashes.items()):
        event = events[canonical_id]
        event_id = _uuid("event", resolution.generation_hash, canonical_id)
        effective_from = datetime.combine(
            event.source_day, datetime.min.time(), tzinfo=timezone.utc
        )
        binding_hash = content_hash(
            {
                "generationHash": resolution.generation_hash,
                "canonicalEventId": canonical_id,
                "targetApplication": target_application,
                "targetActivityHash": target_hash,
            }
        )
        binding_id = _uuid("application_projection_binding", binding_hash)
        existing = connection.execute(
            """
            SELECT canonical_identity_policy_generation_id,
                   canonical_identity_event_id, canonical_id,
                   target_application, target_activity_hash,
                   binding_source, effective_from, is_active
            FROM finance.application_projection_bindings
            WHERE application_projection_binding_id = %s
            """,
            (binding_id,),
        ).fetchone()
        expected = (
            generation_id,
            event_id,
            canonical_id,
            target_application,
            target_hash,
            "policy_generation",
            effective_from,
            True,
        )
        if existing is not None:
            actual = (
                str(
                    _cell(
                        existing,
                        "canonical_identity_policy_generation_id",
                        0,
                    )
                ),
                str(_cell(existing, "canonical_identity_event_id", 1)),
                _cell(existing, "canonical_id", 2),
                _cell(existing, "target_application", 3),
                _cell(existing, "target_activity_hash", 4),
                _cell(existing, "binding_source", 5),
                _cell(existing, "effective_from", 6),
                _cell(existing, "is_active", 7),
            )
            if actual != expected:
                raise ValueError("persisted projection binding conflicts with replay")
            continue
        connection.execute(
            """
            INSERT INTO finance.application_projection_bindings (
                application_projection_binding_id,
                canonical_identity_policy_generation_id,
                canonical_identity_event_id, canonical_id,
                target_application, target_activity_hash, binding_source,
                effective_from, observed_at
            ) VALUES (
                %s, %s, %s, %s, %s, %s, 'policy_generation', %s, %s
            )
            """,
            (
                binding_id,
                generation_id,
                event_id,
                canonical_id,
                target_application,
                target_hash,
                effective_from,
                _observed_at(resolution),
            ),
        )
    return len(target_activity_hashes)


def persist_application_projection_bindings(
    connection: Any,
    resolution: IdentityResolution,
    target_activity_hashes: Mapping[str, str],
    *,
    target_application: str = "wealthfolio",
) -> int:
    """Preserve an accepted event's original binding across generation changes.

    Unaccepted diagnostic generations keep the original recording contract.
    This function records an already-applied target; it never calls Wealthfolio.
    """

    _lock_identity_writer(connection)
    generation_id = _uuid("generation", resolution.generation_hash)
    accepted = connection.execute(
        "SELECT generation_id FROM finance.accepted_identity_generations "
        "WHERE generation_id = %s",
        (generation_id,),
    ).fetchone()
    if accepted is None:
        return _persist_generation_projection_bindings(
            connection, resolution, target_activity_hashes,
            target_application=target_application,
        )
    if target_application != "wealthfolio":
        raise ValueError("only the Wealthfolio projection contract is supported")
    for target_hash in target_activity_hashes.values():
        require_hash(target_hash)
    mappings = {
        _cell(row, "canonical_id", 0): row
        for row in connection.execute(
            """
            SELECT event.canonical_id, mapping.accepted_event_id,
                   projection.target_activity_hash, binding.is_active
            FROM finance.accepted_identity_event_mappings mapping
            JOIN finance.canonical_identity_events event
              ON event.canonical_identity_event_id = mapping.generation_event_id
            LEFT JOIN finance.accepted_identity_projection_links projection
              ON projection.accepted_event_id = mapping.accepted_event_id
             AND projection.target_application = %s
            LEFT JOIN finance.application_projection_bindings binding
              ON binding.application_projection_binding_id
                    = projection.application_projection_binding_id
            WHERE mapping.generation_id = %s
            """,
            (target_application, generation_id),
        ).fetchall()
    }
    additions = {}
    for canonical_id, target_hash in sorted(target_activity_hashes.items()):
        row = mappings.get(canonical_id)
        if row is None or _cell(row, "accepted_event_id", 1) is None:
            raise ValueError("projection requires an unambiguous accepted event mapping")
        existing_target = _cell(row, "target_activity_hash", 2)
        if existing_target is not None:
            if existing_target != target_hash or not _cell(row, "is_active", 3):
                raise ValueError("accepted projection binding conflicts with existing target")
        else:
            additions[canonical_id] = target_hash
    if additions:
        _require_current_acceptance_policy(resolution)
        if accepted_current_generation_hash(connection) != resolution.generation_hash:
            raise ValueError("new projection bindings require the selected accepted generation")
        _persist_generation_projection_bindings(
            connection, resolution, additions, target_application=target_application
        )
        for canonical_id, target_hash in additions.items():
            binding = connection.execute(
                """
                SELECT application_projection_binding_id
                FROM finance.application_projection_bindings
                WHERE canonical_identity_event_id = %s
                  AND target_application = %s AND target_activity_hash = %s
                  AND is_active
                """,
                (_uuid("event", resolution.generation_hash, canonical_id),
                 target_application, target_hash),
            ).fetchone()
            _link_accepted_projection(
                connection, str(_cell(mappings[canonical_id], "accepted_event_id", 1)),
                str(_cell(binding, "application_projection_binding_id", 0)),
                target_application, target_hash,
            )
    return len(target_activity_hashes)


def _cell(row: Any, name: str, index: int) -> Any:
    if isinstance(row, Mapping):
        return row[name]
    return row[index]


def _uuid(kind: str, *parts: object) -> str:
    return stable_id(f"canonical_identity_{kind}", *parts)


def accepted_current_generation_hash(connection: Any) -> str | None:
    """Read the optimistic predecessor token; reading does not select anything."""

    row = _accepted_head(connection)
    return None if row is None else str(_cell(row, "generation_hash", 0))


def _accepted_head(connection: Any) -> Any:
    return connection.execute(
        """
        SELECT generation.generation_hash, accepted.generation_id,
               accepted.acceptance_number
        FROM finance.accepted_identity_generations accepted
        JOIN finance.canonical_identity_policy_generations generation
          ON generation.canonical_identity_policy_generation_id = accepted.generation_id
        ORDER BY accepted.acceptance_number DESC LIMIT 1
        """
    ).fetchone()


def _link_accepted_projection(
    connection: Any, accepted_id: str, binding_id: str, application: str, target: str,
) -> None:
    existing = connection.execute(
        """
        SELECT application_projection_binding_id, target_activity_hash
        FROM finance.accepted_identity_projection_links
        WHERE accepted_event_id = %s AND target_application = %s
        """,
        (accepted_id, application),
    ).fetchone()
    if existing is not None:
        if (
            str(_cell(existing, "application_projection_binding_id", 0)) != binding_id
            or _cell(existing, "target_activity_hash", 1) != target
        ):
            raise ValueError("accepted projection link conflicts with existing binding")
        return
    connection.execute(
        """
        INSERT INTO finance.accepted_identity_projection_links (
            accepted_event_id, target_application, target_activity_hash,
            application_projection_binding_id
        ) VALUES (%s, %s, %s, %s)
        """,
        (accepted_id, application, target, binding_id),
    )


def _accepted_replay(
    connection: Any, resolution: IdentityResolution, previous_hash: str | None,
) -> AcceptedIdentityResult:
    rows = connection.execute(
        """
        SELECT event.canonical_id, mapping.accepted_event_id, mapping.revision_number,
               mapping.revision_hash, mapping.outcome, mapping.prior_event_ids,
               mapping.conflict_reasons, mapping.projection_binding_ids,
               mapping.review_decision_hashes
        FROM finance.accepted_identity_event_mappings mapping
        JOIN finance.canonical_identity_events event
          ON event.canonical_identity_event_id = mapping.generation_event_id
        WHERE mapping.generation_id = %s
        ORDER BY event.canonical_id
        """,
        (_uuid("generation", resolution.generation_hash),),
    ).fetchall()
    mappings = tuple(
        AcceptedEventMapping(
            canonical_event_id=_cell(row, "canonical_id", 0),
            accepted_event_id=(
                str(_cell(row, "accepted_event_id", 1))
                if _cell(row, "accepted_event_id", 1) is not None else None
            ),
            revision_number=_cell(row, "revision_number", 2),
            revision_hash=_cell(row, "revision_hash", 3),
            outcome=_cell(row, "outcome", 4),
            prior_event_ids=tuple(str(item) for item in _cell(row, "prior_event_ids", 5)),
            conflict_reasons=tuple(_cell(row, "conflict_reasons", 6)),
            projection_binding_ids=tuple(
                str(item) for item in _cell(row, "projection_binding_ids", 7)
            ),
            review_decision_hashes=tuple(_cell(row, "review_decision_hashes", 8)),
        )
        for row in rows
    )
    if {item.canonical_event_id for item in mappings} != {
        event.canonical_event_id for event in resolution.canonical_events
    }:
        raise ValueError("accepted identity generation has incomplete mappings")
    return AcceptedIdentityResult(
        resolution.generation_hash, previous_hash, False, mappings,
    )


def _require_current_acceptance_policy(resolution: IdentityResolution) -> None:
    """Legacy policies are replayable evidence, not eligible new selections."""

    def semantics(policy):
        document = policy.document()
        # Coverage intervals are reviewed input scope, not a new matching policy.
        # Keep every actual policy rule, including the authority policy/hash.
        document["sourceAuthority"] = {
            **document["sourceAuthority"], "intervals": [],
        }
        return document

    if semantics(resolution.policy) != semantics(DEFAULT_POLICY):
        raise ValueError(
            "new acceptance requires the latest supported policy semantics; "
            "historical generations remain evidence-only"
        )


def persist_accepted_identity_resolution(
    connection: Any,
    resolution: IdentityResolution,
    *,
    expected_previous_generation_hash: str | None,
) -> AcceptedIdentityResult:
    """Persist evidence and select its accepted mappings in one caller transaction.

    Passing ``None`` bootstraps a reviewed generation; otherwise the predecessor
    must match the selected head. Replaying any already accepted generation is
    read-only and cannot rewind the head. Historical diagnostic generations are
    never silently promoted. Conflicts are local mapping outcomes, not exceptions.
    """

    _lock_identity_writer(connection)
    generation_id = _uuid("generation", resolution.generation_hash)
    replay = connection.execute(
        """
        SELECT accepted.contract_version, previous.generation_hash
        FROM finance.accepted_identity_generations accepted
        LEFT JOIN finance.canonical_identity_policy_generations previous
          ON previous.canonical_identity_policy_generation_id
                = accepted.previous_generation_id
        WHERE accepted.generation_id = %s
        """,
        (generation_id,),
    ).fetchone()
    if replay is not None:
        if _cell(replay, "contract_version", 0) != ACCEPTANCE_CONTRACT:
            raise ValueError("accepted identity contract version conflicts with replay")
        persist_identity_resolution(connection, resolution)
        return _accepted_replay(connection, resolution, _cell(replay, "generation_hash", 1))
    _require_current_acceptance_policy(resolution)
    head = _accepted_head(connection)
    previous_hash = None if head is None else _cell(head, "generation_hash", 0)
    if previous_hash != expected_previous_generation_hash:
        raise ValueError("accepted identity predecessor changed; replan from selected head")
    claim_ids = sorted(claim.claim_id for claim in resolution.claims)
    ownership = {
        _cell(row, "claim_hash", 0): ClaimOwnership(
            str(_cell(row, "accepted_event_id", 1)),
            _cell(row, "canonical_account_hash", 2),
            _cell(row, "source_canonical_account_hash", 3),
        )
        for row in connection.execute(
            """
            SELECT claim_hash, accepted_event_id, canonical_account_hash,
                   source_canonical_account_hash
            FROM finance.accepted_identity_claims WHERE claim_hash = ANY(%s::text[])
            """,
            (claim_ids,),
        ).fetchall()
    }
    accepted_ids = sorted({owner.accepted_event_id for owner in ownership.values()})
    revisions = {
        str(_cell(row, "accepted_event_id", 0)): AcceptedRevision(
            int(_cell(row, "revision_number", 1)), _cell(row, "revision_hash", 2),
        )
        for row in connection.execute(
            """
            SELECT DISTINCT ON (accepted_event_id)
                   accepted_event_id, revision_number, revision_hash
            FROM finance.accepted_identity_revisions
            WHERE accepted_event_id = ANY(%s::uuid[])
            ORDER BY accepted_event_id, revision_number DESC
            """,
            (accepted_ids,),
        ).fetchall()
    }
    binding_rows = connection.execute(
        """
        SELECT binding.application_projection_binding_id, binding.target_application,
               binding.target_activity_hash, event.canonical_account_hash,
               array_agg(DISTINCT claim.claim_hash ORDER BY claim.claim_hash) AS claim_hashes,
               link.accepted_event_id, binding.is_active
        FROM finance.application_projection_bindings binding
        JOIN finance.canonical_identity_events event
          ON event.canonical_identity_event_id = binding.canonical_identity_event_id
        JOIN finance.canonical_identity_event_members member
          ON member.canonical_identity_event_id = event.canonical_identity_event_id
         AND member.member_type = 'source_claim'
        JOIN finance.canonical_identity_source_claims claim
          ON claim.canonical_identity_source_claim_id = member.canonical_identity_source_claim_id
        LEFT JOIN finance.accepted_identity_projection_links link
          ON link.application_projection_binding_id = binding.application_projection_binding_id
        WHERE binding.is_active OR link.accepted_event_id = ANY(%s::uuid[])
        GROUP BY binding.application_projection_binding_id,
                 event.canonical_account_hash, link.accepted_event_id
        HAVING bool_or(claim.claim_hash = ANY(%s::text[]))
            OR link.accepted_event_id = ANY(%s::uuid[])
        """,
        (accepted_ids, claim_ids, accepted_ids),
    ).fetchall()
    bindings = tuple(
        ProjectionEvidence(
            binding_id=str(_cell(row, "application_projection_binding_id", 0)),
            target_application=_cell(row, "target_application", 1),
            target_activity_hash=_cell(row, "target_activity_hash", 2),
            canonical_account_hash=_cell(row, "canonical_account_hash", 3),
            claim_ids=tuple(_cell(row, "claim_hashes", 4)),
            accepted_event_id=(
                str(_cell(row, "accepted_event_id", 5))
                if _cell(row, "accepted_event_id", 5) is not None else None
            ),
            is_active=bool(_cell(row, "is_active", 6)),
        )
        for row in binding_rows
    )
    mappings = plan_accepted_identity(resolution, ownership, revisions, bindings)
    persist_identity_resolution(connection, resolution)
    connection.execute(
        """
        INSERT INTO finance.accepted_identity_generations (
            generation_id, acceptance_number, previous_generation_id, contract_version
        ) VALUES (%s, %s, %s, %s)
        """,
        (
            generation_id,
            1 if head is None else int(_cell(head, "acceptance_number", 2)) + 1,
            None if head is None else _cell(head, "generation_id", 1),
            ACCEPTANCE_CONTRACT,
        ),
    )
    events = {event.canonical_event_id: event for event in resolution.canonical_events}
    claims = {claim.claim_id: claim for claim in resolution.claims}
    for mapping in mappings:
        event = events[mapping.canonical_event_id]
        event_id = _uuid("event", resolution.generation_hash, event.canonical_event_id)
        if mapping.accepted_event_id is not None:
            if mapping.outcome == "created":
                connection.execute(
                    """
                    INSERT INTO finance.accepted_identity_events (
                        accepted_event_id, canonical_account_hash, created_generation_id
                    ) VALUES (%s, %s, %s)
                    """,
                    (mapping.accepted_event_id, event.canonical_account_hash, generation_id),
                )
            for claim_id in sorted(event.member_claim_ids):
                if claim_id not in ownership:
                    connection.execute(
                        """
                        INSERT INTO finance.accepted_identity_claims (
                            claim_hash, accepted_event_id, canonical_account_hash,
                            source_canonical_account_hash, source_claim_id
                        ) VALUES (%s, %s, %s, %s, %s)
                        """,
                        (claim_id, mapping.accepted_event_id, event.canonical_account_hash,
                         claims[claim_id].canonical_account_hash,
                         _uuid("source_claim", resolution.generation_hash, claim_id)),
                    )
            if mapping.new_revision:
                connection.execute(
                    """
                    INSERT INTO finance.accepted_identity_revisions (
                        accepted_event_id, revision_number, revision_hash,
                        generation_id, generation_event_id, canonical_account_hash
                    ) VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (mapping.accepted_event_id, mapping.revision_number,
                     mapping.revision_hash, generation_id, event_id,
                     event.canonical_account_hash),
                )
            for binding in mapping.bindings:
                _link_accepted_projection(
                    connection, mapping.accepted_event_id, binding.binding_id,
                    binding.target_application, binding.target_activity_hash,
                )
        connection.execute(
            """
            INSERT INTO finance.accepted_identity_event_mappings (
                generation_id, generation_event_id, canonical_account_hash,
                accepted_event_id, revision_number,
                revision_hash, outcome, prior_event_ids, conflict_reasons,
                projection_binding_ids, review_decision_hashes
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s::uuid[], %s::text[], %s::uuid[], %s::text[]
            )
            """,
            (generation_id, event_id, event.canonical_account_hash,
             mapping.accepted_event_id, mapping.revision_number,
             mapping.revision_hash, mapping.outcome, list(mapping.prior_event_ids),
             list(mapping.conflict_reasons), list(mapping.projection_binding_ids),
             list(mapping.review_decision_hashes)),
        )
    return AcceptedIdentityResult(
        resolution.generation_hash, previous_hash, True, mappings,
    )


HEX_64 = re.compile(r"^[0-9a-f]{64}$")


def _ingested_observation_ids(
    connection: Any, observation_ids: Iterable[str]
) -> frozenset[str]:
    """Which resolver observation ids are really ingested observation rows.

    An observation id is only a foreign key when it is a uuid *and* that row
    exists.  Canonical scope ids are content hashes of artifact rows that were
    never ingested, so most of them are neither.  Asking the database once is
    the only honest way to tell the two apart: minting a uuid from a hash would
    fabricate a reference, and assuming every uuid exists would defer the same
    violation to insert time.
    """

    candidates = sorted(
        {
            str(uuid.UUID(observation_id))
            for observation_id in observation_ids
            if _parses_as_uuid(observation_id)
        }
    )
    if not candidates:
        return frozenset()
    rows = connection.execute(
        """
        SELECT transaction_observation_id
        FROM finance.transaction_observations
        WHERE transaction_observation_id = ANY(%s::uuid[])
        """,
        (candidates,),
    ).fetchall()
    return frozenset(str(_cell(row, "transaction_observation_id", 0)) for row in rows)


def _parses_as_uuid(observation_id: str) -> bool:
    try:
        uuid.UUID(observation_id)
    except (ValueError, AttributeError, TypeError):
        return False
    return True


def _observation_identity(
    observation_id: str, ingested: frozenset[str]
) -> tuple[str | None, str | None]:
    """Return ``(transaction_observation_id, observation_identity_hash)``.

    Exactly one is not ``None``, which is what the schema requires.  A published
    identity hash is stored exactly as published; anything else is hashed so the
    stored value is still a deterministic, replay-stable reference to the same
    observation rather than an invented uuid.
    """

    if _parses_as_uuid(observation_id):
        normalized = str(uuid.UUID(observation_id))
        if normalized in ingested:
            return normalized, None
    if HEX_64.fullmatch(observation_id):
        return None, observation_id
    return None, content_hash(observation_id)


def _observed_at(resolution: IdentityResolution) -> datetime:
    return max(
        (item.observed_at for item in resolution.observations),
        default=datetime(1970, 1, 1, tzinfo=timezone.utc),
    )


def _feature_schema_hash(resolution: IdentityResolution) -> str:
    return content_hash(
        {
            "decisionFeatures": sorted(
                {
                    key
                    for decision in resolution.decisions
                    for key, _value in decision.feature_vector
                }
            ),
            "edgeFeatures": sorted(
                {
                    key
                    for edge in resolution.edges
                    for key, _value in edge.feature_vector
                }
            ),
            "proofKeys": sorted(
                {
                    key
                    for decision in resolution.decisions
                    for key, _value in decision.competing_candidate_proof
                }
            ),
        }
    )


def persist_identity_resolution(
    connection: Any,
    resolution: IdentityResolution,
) -> PersistedIdentityGeneration:
    """Append one complete resolver generation inside the caller's transaction."""

    _lock_identity_writer(connection)
    existing = connection.execute(
        """
        SELECT canonical_identity_policy_generation_id,
               generation_number,
               canonical_state_hash
        FROM finance.canonical_identity_policy_generations
        WHERE generation_hash = %s
        """,
        (resolution.generation_hash,),
    ).fetchone()
    policy_id = _uuid("policy", resolution.policy.policy_hash)
    generation_id = _uuid("generation", resolution.generation_hash)
    if existing is not None:
        if (
            str(_cell(existing, "canonical_identity_policy_generation_id", 0))
            != generation_id
            or _cell(existing, "canonical_state_hash", 2)
            != resolution.canonical_state_hash
        ):
            raise ValueError("persisted identity generation conflicts with replay")
        return PersistedIdentityGeneration(
            policy_id=policy_id,
            generation_id=generation_id,
            generation_number=int(_cell(existing, "generation_number", 1)),
            generation_hash=resolution.generation_hash,
            inserted=False,
            claim_count=len(resolution.claims),
            event_count=len(resolution.canonical_events),
            decision_count=len(resolution.decisions),
        )

    observed_at = _observed_at(resolution)
    connection.execute(
        """
        INSERT INTO finance.canonical_identity_policies (
            canonical_identity_policy_id, policy_name, policy_version,
            policy_hash, policy_document, observed_at
        ) VALUES (%s, %s, %s, %s, %s::jsonb, %s)
        ON CONFLICT (policy_hash) DO NOTHING
        """,
        (
            policy_id,
            POLICY_NAME,
            resolution.policy.version,
            resolution.policy.policy_hash,
            json.dumps(resolution.policy.document(), sort_keys=True),
            observed_at,
        ),
    )
    persisted_policy = connection.execute(
        """
        SELECT canonical_identity_policy_id, policy_name, policy_version
        FROM finance.canonical_identity_policies
        WHERE policy_hash = %s
        """,
        (resolution.policy.policy_hash,),
    ).fetchone()
    if (
        persisted_policy is None
        or str(_cell(persisted_policy, "canonical_identity_policy_id", 0)) != policy_id
        or _cell(persisted_policy, "policy_name", 1) != POLICY_NAME
        or _cell(persisted_policy, "policy_version", 2) != resolution.policy.version
    ):
        raise ValueError("persisted identity policy conflicts with resolver")

    generation_number = int(
        _cell(
            connection.execute(
                """
                SELECT COALESCE(max(generation_number), 0) + 1
                    AS generation_number
                FROM finance.canonical_identity_policy_generations
                WHERE canonical_identity_policy_id = %s
                """,
                (policy_id,),
            ).fetchone(),
            "generation_number",
            0,
        )
    )
    connection.execute(
        """
        INSERT INTO finance.canonical_identity_policy_generations (
            canonical_identity_policy_generation_id,
            canonical_identity_policy_id, policy_version, policy_hash,
            generation_number, generation_label, generation_hash,
            input_hash, canonical_state_hash, feature_schema_hash, observed_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            generation_id,
            policy_id,
            resolution.policy.version,
            resolution.policy.policy_hash,
            generation_number,
            f"{resolution.policy.version}:{resolution.generation_hash[:16]}",
            resolution.generation_hash,
            resolution.input_hash,
            resolution.canonical_state_hash,
            _feature_schema_hash(resolution),
            observed_at,
        ),
    )

    authority_policy = resolution.policy.authority_policy
    if resolution.interval_authorities:
        authority_policy_id = _uuid(
            "source_authority_policy", authority_policy.policy_hash
        )
        connection.execute(
            """
            INSERT INTO finance.canonical_identity_source_authority_policies (
                canonical_identity_source_authority_policy_id,
                authority_policy_version, authority_policy_hash,
                authority_policy_document, observed_at
            ) VALUES (%s, %s, %s, %s::jsonb, %s)
            ON CONFLICT (authority_policy_version, authority_policy_hash)
            DO NOTHING
            """,
            (
                authority_policy_id,
                authority_policy.version,
                authority_policy.policy_hash,
                json.dumps(authority_policy.document(), sort_keys=True),
                observed_at,
            ),
        )
        for verdict in resolution.interval_authorities:
            interval = verdict.interval
            evidence = interval.evidence
            connection.execute(
                """
                INSERT INTO finance.canonical_identity_authority_intervals (
                    canonical_identity_authority_interval_id,
                    canonical_identity_policy_generation_id,
                    canonical_identity_source_authority_policy_id,
                    interval_id, canonical_account_hash,
                    effective_from, effective_through, source_family,
                    source_connection_hash, source_account_hash,
                    format_strength, stable_id_support, replay_stable_ids,
                    completeness, extraction_requested_from,
                    extraction_requested_through, extracted_at,
                    freshness_as_of, trust_cutoff_day,
                    declared_source_transaction_count,
                    observed_source_transaction_count, settlement_days,
                    posting_date_tolerance_days,
                    authority_rank, coverage_proven, counts_reconciled,
                    authoritative, authority_proof, evidence_hash,
                    source_hashes, observed_at
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s::jsonb, %s, %s::jsonb, %s
                )
                """,
                (
                    _uuid(
                        "authority_interval",
                        resolution.generation_hash,
                        interval.interval_id,
                    ),
                    generation_id,
                    authority_policy_id,
                    interval.interval_id,
                    content_hash(interval.canonical_account_id),
                    interval.effective_from,
                    interval.effective_through,
                    evidence.source_family,
                    content_hash(evidence.source_connection_id),
                    content_hash(evidence.source_account_id),
                    evidence.format_strength,
                    evidence.stable_id_support,
                    evidence.replay_stable_ids,
                    evidence.completeness,
                    evidence.extraction_requested_from,
                    evidence.extraction_requested_through,
                    evidence.extracted_at,
                    evidence.freshness_as_of,
                    evidence.trust_cutoff_day,
                    evidence.source_transaction_count,
                    verdict.observed_source_transactions,
                    interval.settlement_days(),
                    interval.posting_date_tolerance_days,
                    verdict.rank,
                    verdict.proven,
                    verdict.reconciled,
                    verdict.authoritative,
                    json.dumps(dict(verdict.proof), sort_keys=True),
                    evidence.evidence_hash,
                    json.dumps(list(evidence.source_hashes)),
                    observed_at,
                ),
            )

    observation_by_id = {item.observation_id: item for item in resolution.observations}

    for scope in resolution.token_scopes:
        connection.execute(
            """
            INSERT INTO finance.canonical_identity_provider_token_scopes (
                canonical_identity_provider_token_scope_id,
                canonical_identity_policy_generation_id, scope_hash, map_hash,
                policy_version, canonical_account_hash, left_namespace_hash,
                right_namespace_hash, left_provider_id_kind,
                right_provider_id_kind, max_day_skew, decision, decided_at,
                recorded_at
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
            )
            ON CONFLICT (canonical_identity_policy_generation_id, scope_hash)
            DO NOTHING
            """,
            (
                _uuid(
                    "provider_token_scope",
                    resolution.generation_hash,
                    scope.scope_hash,
                ),
                generation_id,
                scope.scope_hash,
                scope.map_hash,
                PROVIDER_TOKEN_SCOPE_POLICY_VERSION,
                content_hash(scope.canonical_account_id),
                content_hash(scope.left.key),
                content_hash(scope.right.key),
                scope.left.provider_id_kind,
                scope.right.provider_id_kind,
                scope.max_day_skew,
                scope.decision,
                scope.decided_at,
                observed_at,
            ),
        )

    claim_db_ids = {
        claim.claim_id: _uuid(
            "source_claim", resolution.generation_hash, claim.claim_id
        )
        for claim in resolution.claims
    }
    ingested_observations = _ingested_observation_ids(
        connection, observation_by_id.keys()
    )
    for claim in resolution.claims:
        claim_observed_at = max(
            observation_by_id[item].observed_at for item in claim.observation_ids
        )
        connection.execute(
            """
            INSERT INTO finance.canonical_identity_source_claims (
                canonical_identity_source_claim_id,
                canonical_identity_policy_generation_id, claim_hash,
                source_family, canonical_account_hash, source_account_hash,
                source_connection_hash, provider_identity_hash,
                provider_id_kind, selected_observation_hash, source_hashes,
                observed_at
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s
            )
            """,
            (
                claim_db_ids[claim.claim_id],
                generation_id,
                claim.claim_id,
                claim.source_family,
                claim.canonical_account_hash,
                claim.source_account_hash,
                claim.source_connection_hash,
                claim.provider_identity_hash,
                claim.provider_id_kind,
                content_hash(claim.selected_observation_id),
                json.dumps(claim.source_hashes),
                claim_observed_at,
            ),
        )
        for observation_id in claim.observation_ids:
            transaction_observation_id, observation_identity_hash = (
                _observation_identity(observation_id, ingested_observations)
            )
            observation = observation_by_id[observation_id]
            membership_hash = content_hash(
                {
                    "generationHash": resolution.generation_hash,
                    "claimHash": claim.claim_id,
                    "observationId": observation_id,
                }
            )
            connection.execute(
                """
                INSERT INTO finance.canonical_identity_observation_memberships (
                    canonical_identity_observation_membership_id,
                    canonical_identity_policy_generation_id,
                    canonical_identity_source_claim_id,
                    transaction_observation_id, observation_identity_hash,
                    membership_hash, membership_role, observed_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    _uuid("claim_membership", membership_hash),
                    generation_id,
                    claim_db_ids[claim.claim_id],
                    transaction_observation_id,
                    observation_identity_hash,
                    membership_hash,
                    (
                        "excluded"
                        if not observation.trusted
                        else "selected"
                        if observation_id == claim.selected_observation_id
                        else "member"
                    ),
                    observation.observed_at,
                ),
            )

    for edge in resolution.edges:
        connection.execute(
            """
            INSERT INTO finance.canonical_identity_graph_edges (
                canonical_identity_graph_edge_id,
                canonical_identity_policy_generation_id,
                left_node_type, left_node_id, right_node_type, right_node_id,
                relation_kind, feature_vector, confidence_basis_points,
                automatic, competing_candidate_proof, source_hashes,
                edge_hash, observed_at
            ) VALUES (
                %s, %s, 'source_claim', %s, 'source_claim', %s,
                %s, %s::jsonb, %s, %s, %s::jsonb, %s::jsonb, %s, %s
            )
            """,
            (
                _uuid("graph_edge", resolution.generation_hash, edge.edge_hash),
                generation_id,
                claim_db_ids[edge.left_claim_id],
                claim_db_ids[edge.right_claim_id],
                edge.kind.value,
                json.dumps(dict(edge.feature_vector), sort_keys=True),
                edge.confidence_basis_points,
                edge.automatic,
                json.dumps(dict(edge.competing_candidate_proof), sort_keys=True),
                json.dumps(edge.source_hashes),
                edge.edge_hash,
                observed_at,
            ),
        )

    event_db_ids = {
        event.canonical_event_id: _uuid(
            "event",
            resolution.generation_hash,
            event.canonical_event_id,
        )
        for event in resolution.canonical_events
    }
    claim_by_id = {item.claim_id: item for item in resolution.claims}
    for event in resolution.canonical_events:
        event_observed_at = max(
            observation_by_id[item].observed_at for item in event.member_observation_ids
        )
        event_hash = content_hash(
            {
                "canonicalEventId": event.canonical_event_id,
                "canonicalAccountHash": event.canonical_account_hash,
                "selectedObservationId": event.selected_observation_id,
                "memberClaimIds": event.member_claim_ids,
                "memberObservationIds": event.member_observation_ids,
                "sourceDay": event.source_day.isoformat(),
                "signedAmount": format(event.signed_amount, "f"),
                "currency": event.currency,
                "descriptionHash": content_hash(event.description),
                "status": event.status,
                "categoryHash": content_hash(event.category),
                "trusted": event.trusted,
            }
        )
        connection.execute(
            """
            INSERT INTO finance.canonical_identity_events (
                canonical_identity_event_id,
                canonical_identity_policy_generation_id, canonical_id,
                canonical_account_hash, selected_observation_hash, event_hash,
                source_day, signed_amount, currency_code, description_hash,
                status, category_hash, trusted, observed_at
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
            )
            """,
            (
                event_db_ids[event.canonical_event_id],
                generation_id,
                event.canonical_event_id,
                event.canonical_account_hash,
                content_hash(event.selected_observation_id),
                event_hash,
                event.source_day,
                event.signed_amount,
                event.currency,
                content_hash(event.description),
                event.status,
                content_hash(event.category),
                event.trusted,
                event_observed_at,
            ),
        )
        for claim_id in event.member_claim_ids:
            member_hash = content_hash(
                {
                    "generationHash": resolution.generation_hash,
                    "canonicalEventId": event.canonical_event_id,
                    "sourceClaimId": claim_id,
                }
            )
            connection.execute(
                """
                INSERT INTO finance.canonical_identity_event_members (
                    canonical_identity_event_member_id,
                    canonical_identity_policy_generation_id,
                    canonical_identity_event_id, member_type, member_hash,
                    member_role, canonical_identity_source_claim_id, observed_at
                ) VALUES (%s, %s, %s, 'source_claim', %s, %s, %s, %s)
                """,
                (
                    _uuid("event_member", member_hash),
                    generation_id,
                    event_db_ids[event.canonical_event_id],
                    member_hash,
                    (
                        "selected"
                        if event.selected_observation_id
                        in claim_by_id[claim_id].observation_ids
                        else "member"
                    ),
                    claim_db_ids[claim_id],
                    event_observed_at,
                ),
            )
        for observation_id in event.member_observation_ids:
            transaction_observation_id, observation_identity_hash = (
                _observation_identity(observation_id, ingested_observations)
            )
            member_hash = content_hash(
                {
                    "generationHash": resolution.generation_hash,
                    "canonicalEventId": event.canonical_event_id,
                    "transactionObservationId": observation_id,
                }
            )
            connection.execute(
                """
                INSERT INTO finance.canonical_identity_event_members (
                    canonical_identity_event_member_id,
                    canonical_identity_policy_generation_id,
                    canonical_identity_event_id, member_type, member_hash,
                    member_role, transaction_observation_id,
                    observation_identity_hash, observed_at
                ) VALUES (
                    %s, %s, %s, 'transaction_observation', %s, %s, %s, %s, %s
                )
                """,
                (
                    _uuid("event_member", member_hash),
                    generation_id,
                    event_db_ids[event.canonical_event_id],
                    member_hash,
                    (
                        "selected"
                        if observation_id == event.selected_observation_id
                        else "member"
                    ),
                    transaction_observation_id,
                    observation_identity_hash,
                    observation_by_id[observation_id].observed_at,
                ),
            )

    relationship_kind = {
        RelationKind.TRANSFER: "transfer",
        RelationKind.CORRECTION: "correction",
        RelationKind.REVERSAL: "reversal",
        RelationKind.PENDING_TRANSITION: "pending",
    }
    for relationship in resolution.relationships:
        if relationship.kind not in relationship_kind:
            continue
        connection.execute(
            """
            INSERT INTO finance.canonical_identity_event_relationships (
                canonical_identity_event_relationship_id,
                canonical_identity_policy_generation_id,
                source_canonical_identity_event_id,
                target_canonical_identity_event_id,
                relationship_type, relationship_hash, observed_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (
                _uuid(
                    "event_relationship",
                    resolution.generation_hash,
                    relationship.relationship_hash,
                ),
                generation_id,
                event_db_ids[relationship.left_canonical_event_id],
                event_db_ids[relationship.right_canonical_event_id],
                relationship_kind[relationship.kind],
                relationship.relationship_hash,
                observed_at,
            ),
        )

    decision_db_ids = {}
    for decision in resolution.decisions:
        if decision.confidence_tier is ConfidenceTier.HUMAN_OVERRIDE:
            continue
        decision_id = _uuid(
            "automatic_decision",
            resolution.generation_hash,
            decision.decision_hash,
        )
        decision_db_ids[decision.decision_hash] = decision_id
        connection.execute(
            """
            INSERT INTO finance.canonical_identity_automatic_decisions (
                canonical_identity_automatic_decision_id,
                canonical_identity_policy_generation_id,
                canonical_identity_policy_id, policy_version, policy_hash,
                outcome, confidence_tier, confidence_basis_points,
                rationale_code, feature_vector, competing_candidate_proof,
                source_hashes, decision_hash, residual_classification,
                source_authority_policy_hash, observed_at
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s::jsonb, %s::jsonb, %s::jsonb, %s, %s, %s, %s
            )
            """,
            (
                decision_id,
                generation_id,
                policy_id,
                decision.policy_version,
                decision.policy_hash,
                decision.outcome.value,
                decision.confidence_tier.value,
                decision.confidence_basis_points,
                decision.rationale_code,
                json.dumps(dict(decision.feature_vector), sort_keys=True),
                json.dumps(dict(decision.competing_candidate_proof), sort_keys=True),
                json.dumps(decision.source_hashes),
                decision.decision_hash,
                decision.residual_classification,
                decision.source_authority_policy_hash,
                observed_at,
            ),
        )
        for index, canonical_id in enumerate(decision.canonical_event_ids):
            membership_hash = content_hash(
                {
                    "generationHash": resolution.generation_hash,
                    "decisionHash": decision.decision_hash,
                    "canonicalEventId": canonical_id,
                }
            )
            connection.execute(
                """
                INSERT INTO finance.canonical_identity_decision_event_memberships (
                    canonical_identity_decision_event_membership_id,
                    canonical_identity_policy_generation_id,
                    canonical_identity_automatic_decision_id,
                    canonical_identity_event_id, canonical_id,
                    membership_role, membership_hash, observed_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    _uuid("decision_event_membership", membership_hash),
                    generation_id,
                    decision_id,
                    event_db_ids[canonical_id],
                    canonical_id,
                    "subject" if index == 0 else "related",
                    membership_hash,
                    observed_at,
                ),
            )

    suppression_edges = {
        (edge.left_claim_id, edge.right_claim_id): edge
        for edge in resolution.edges
        if edge.kind is RelationKind.SOURCE_SUPPRESSED
    }
    canonical_by_claim = {
        claim_id: event.canonical_event_id
        for event in resolution.canonical_events
        for claim_id in event.member_claim_ids
    }
    for decision in resolution.decisions:
        if decision.outcome is not DecisionOutcome.SOURCE_SUPPRESSED:
            continue
        features = dict(decision.feature_vector)
        proof = dict(decision.competing_candidate_proof)
        suppressed_claim_id = features["suppressedClaimId"]
        authoritative_claim_id = features["authoritativeClaimId"]
        edge = suppression_edges.get(
            tuple(sorted((suppressed_claim_id, authoritative_claim_id)))
        )
        if edge is None:
            raise ValueError("source-suppressed decision has no matching edge")
        canonical_event_id = canonical_by_claim[authoritative_claim_id]
        connection.execute(
            """
            INSERT INTO finance.canonical_identity_source_suppressions (
                canonical_identity_source_suppression_id,
                canonical_identity_policy_generation_id,
                canonical_identity_automatic_decision_id,
                suppressed_source_claim_id, authoritative_source_claim_id,
                canonical_identity_event_id, suppressed_interval_id,
                authoritative_interval_id, authority_policy_hash,
                occurrence_index, authoritative_occurrence_count,
                suppressed_occurrence_count, feature_vector,
                competing_candidate_proof, source_hashes, edge_hash,
                observed_at
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s::jsonb, %s::jsonb, %s::jsonb, %s, %s
            )
            """,
            (
                _uuid(
                    "source_suppression",
                    resolution.generation_hash,
                    edge.edge_hash,
                ),
                generation_id,
                decision_db_ids[decision.decision_hash],
                claim_db_ids[suppressed_claim_id],
                claim_db_ids[authoritative_claim_id],
                event_db_ids[canonical_event_id],
                features["suppressedIntervalId"],
                features["authoritativeIntervalId"],
                features["authorityPolicyHash"],
                proof["occurrenceIndex"],
                proof["authoritativeCount"],
                proof["lowerCount"],
                json.dumps(features, sort_keys=True),
                json.dumps(proof, sort_keys=True),
                json.dumps(list(edge.source_hashes)),
                edge.edge_hash,
                observed_at,
            ),
        )

    override_db_ids = {}
    for override in resolution.overrides:
        override_hash = content_hash(
            {
                "overrideId": override.override_id,
                "version": override.version,
                "action": override.action,
                "claimIds": override.claim_ids,
                "rationaleHash": override.rationale_hash,
                "decidedAt": override.decided_at.isoformat(),
                "supersedesOverrideId": override.supersedes_override_id,
            }
        )
        override_id = _uuid(
            "human_override",
            resolution.generation_hash,
            override.override_id,
            override.version,
        )
        override_db_ids[override.override_id] = override_id
        supersedes_id = (
            override_db_ids.get(override.supersedes_override_id)
            if override.supersedes_override_id
            else None
        )
        if override.supersedes_override_id and supersedes_id is None:
            persisted = connection.execute(
                """
                SELECT canonical_identity_human_override_id
                FROM finance.canonical_identity_human_overrides
                WHERE override_id = %s
                ORDER BY override_version DESC
                LIMIT 1
                """,
                (override.supersedes_override_id,),
            ).fetchone()
            if persisted is None:
                raise ValueError("superseded human override is not persisted")
            supersedes_id = str(
                _cell(
                    persisted,
                    "canonical_identity_human_override_id",
                    0,
                )
            )
        connection.execute(
            """
            INSERT INTO finance.canonical_identity_human_overrides (
                canonical_identity_human_override_id,
                canonical_identity_policy_generation_id,
                override_id, override_version, override_action, claim_hashes,
                rationale_hash, override_hash, decided_at,
                supersedes_canonical_identity_human_override_id,
                override_metadata, observed_at
            ) VALUES (
                %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s,
                '{"precedence":"human"}'::jsonb, %s
            )
            """,
            (
                override_id,
                generation_id,
                override.override_id,
                override.version,
                override.action,
                json.dumps(override.claim_ids),
                override.rationale_hash,
                override_hash,
                override.decided_at,
                supersedes_id,
                override.decided_at,
            ),
        )
        human_decision = next(
            (
                decision
                for decision in resolution.decisions
                if decision.human_override_id == override.override_id
            ),
            None,
        )
        if human_decision is None:
            continue
        for index, canonical_id in enumerate(human_decision.canonical_event_ids):
            membership_hash = content_hash(
                {
                    "generationHash": resolution.generation_hash,
                    "overrideHash": override_hash,
                    "canonicalEventId": canonical_id,
                }
            )
            connection.execute(
                """
                INSERT INTO finance.canonical_identity_decision_event_memberships (
                    canonical_identity_decision_event_membership_id,
                    canonical_identity_policy_generation_id,
                    canonical_identity_human_override_id,
                    canonical_identity_event_id, canonical_id,
                    membership_role, membership_hash, observed_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    _uuid("decision_event_membership", membership_hash),
                    generation_id,
                    override_id,
                    event_db_ids[canonical_id],
                    canonical_id,
                    "subject" if index == 0 else "related",
                    membership_hash,
                    override.decided_at,
                ),
            )

    return PersistedIdentityGeneration(
        policy_id=policy_id,
        generation_id=generation_id,
        generation_number=generation_number,
        generation_hash=resolution.generation_hash,
        inserted=True,
        claim_count=len(resolution.claims),
        event_count=len(resolution.canonical_events),
        decision_count=len(resolution.decisions),
    )
