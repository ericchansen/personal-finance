from __future__ import annotations

from datetime import datetime, timezone
from dataclasses import replace
from decimal import Decimal

from finance_store.domain import content_hash, stable_id
from finance_store.identity import IdentityObservation, resolve_identity
from finance_store.identity_postgres import (
    IDENTITY_WRITER_LOCK,
    POLICY_NAME,
    persist_application_projection_bindings,
    persist_identity_resolution,
)
from finance_store.postgres import GLOBAL_WRITER_LOCK


class Cursor:
    def __init__(self, row=None, rows=None):
        self.row = row
        self.rows = list(rows) if rows is not None else ([] if row is None else [row])

    def fetchone(self):
        return self.row

    def fetchall(self):
        return list(self.rows)


class RecordingConnection:
    def __init__(self, resolution, *, existing=False, ingested=()):
        self.resolution = resolution
        self.existing = existing
        self.ingested = tuple(ingested)
        self.calls = []
        self.binding = None

    def execute(self, query, parameters=()):
        normalized = " ".join(query.split())
        self.calls.append((normalized, parameters))
        if "FROM finance.writer_gate" in normalized:
            return Cursor((False,))
        if "FROM finance.transaction_observations" in normalized:
            requested = set(parameters[0]) if parameters else set()
            return Cursor(
                rows=[(item,) for item in self.ingested if item in requested]
            )
        if "WHERE generation_hash = %s" in normalized:
            if self.existing:
                return Cursor(
                    (
                        stable_id(
                            "canonical_identity_generation",
                            self.resolution.generation_hash,
                        ),
                        1,
                        self.resolution.canonical_state_hash,
                    )
                )
            return Cursor(None)
        if "WHERE policy_hash = %s" in normalized:
            return Cursor(
                (
                    stable_id(
                        "canonical_identity_policy",
                        self.resolution.policy.policy_hash,
                    ),
                    POLICY_NAME,
                    self.resolution.policy.version,
                )
            )
        if "SELECT COALESCE(max(generation_number)" in normalized:
            return Cursor((1,))
        if (
            "FROM finance.application_projection_bindings" in normalized
            and "WHERE application_projection_binding_id = %s" in normalized
        ):
            return Cursor(self.binding)
        if "INSERT INTO finance.application_projection_bindings" in normalized:
            self.binding = (
                parameters[1],
                parameters[2],
                parameters[3],
                parameters[4],
                parameters[5],
                "policy_generation",
                parameters[6],
                True,
            )
        return Cursor()


def _observation(name: str, family: str, provider_id: str):
    return IdentityObservation(
        observation_id=stable_id("synthetic-observation", name),
        source_family=family,
        source_connection_id=f"SYN-CONNECTION-{family}",
        source_account_id="SYN-ACCOUNT",
        canonical_account_id="SYN-CANONICAL-ACCOUNT",
        provider_transaction_id=provider_id,
        provider_id_kind=(
            "simplefin-id" if family == "simplefin" else "scoped-provider-id"
        ),
        source_hash=content_hash(name),
        source_day=datetime(2026, 1, 15).date(),
        observed_at=datetime(2026, 1, 16, tzinfo=timezone.utc),
        signed_amount=Decimal("-12.34"),
        currency="USD",
        description="Synthetic Merchant",
    )


def test_persistence_writes_complete_generation_in_dependency_order():
    resolution = resolve_identity(
        (
            _observation("left", "monarch", "SYN-LEFT"),
            _observation("right", "simplefin", "SYN-RIGHT"),
        )
    )
    connection = RecordingConnection(resolution)

    persisted = persist_identity_resolution(connection, resolution)
    statements = "\n".join(query for query, _parameters in connection.calls)

    assert persisted.inserted is True
    assert persisted.generation_number == 1
    assert persisted.claim_count == 2
    assert persisted.event_count == 2
    for table in (
        "canonical_identity_policies",
        "canonical_identity_policy_generations",
        "canonical_identity_source_claims",
        "canonical_identity_observation_memberships",
        "canonical_identity_graph_edges",
        "canonical_identity_events",
        "canonical_identity_event_members",
        "canonical_identity_automatic_decisions",
        "canonical_identity_decision_event_memberships",
    ):
        assert f"INSERT INTO finance.{table}" in statements


def test_persistence_replay_returns_existing_generation_without_writes():
    resolution = resolve_identity((_observation("single", "simplefin", "SYN-SINGLE"),))
    connection = RecordingConnection(resolution, existing=True)

    persisted = persist_identity_resolution(connection, resolution)

    assert persisted.inserted is False
    assert len(connection.calls) == 4
    assert connection.calls[0][1] == (GLOBAL_WRITER_LOCK,)
    assert "FROM finance.writer_gate" in connection.calls[1][0]
    assert connection.calls[2][1] == (IDENTITY_WRITER_LOCK,)
    assert not any("INSERT INTO" in query for query, _parameters in connection.calls)


def test_projection_binding_records_hashes_without_application_mutation():
    resolution = resolve_identity(
        (_observation("projection", "simplefin", "SYN-PROJECTION"),)
    )
    connection = RecordingConnection(resolution)
    persist_identity_resolution(connection, resolution)
    event_id = resolution.canonical_events[0].canonical_event_id

    count = persist_application_projection_bindings(
        connection,
        resolution,
        {event_id: content_hash("SYN-WEALTHFOLIO-ACTIVITY")},
    )
    replay_count = persist_application_projection_bindings(
        connection,
        resolution,
        {event_id: content_hash("SYN-WEALTHFOLIO-ACTIVITY")},
    )

    assert count == 1
    assert replay_count == 1
    inserts = [
        (query, parameters)
        for query, parameters in connection.calls
        if "INSERT INTO finance.application_projection_bindings" in query
    ]
    assert len(inserts) == 1
    query, parameters = inserts[0]
    assert "'policy_generation'" in query
    assert parameters[4] == "wealthfolio"
    assert parameters[5] == content_hash("SYN-WEALTHFOLIO-ACTIVITY")

def test_source_authority_rows_are_persisted_with_decision_classification():
    from tests.test_identity_source_authority import evidence, policy_with

    policy = policy_with(
        evidence(
            family="ofx",
            strength="stable-provider-id",
            count=1,
            account="SYN-ACCOUNT",
            canonical_account="SYN-CANONICAL-ACCOUNT",
        ),
        evidence(
            family="monarch",
            strength="legacy-export",
            count=1,
            account="SYN-ACCOUNT",
            canonical_account="SYN-CANONICAL-ACCOUNT",
            stable_id_support=False,
            replay_stable_ids=False,
        ),
    )
    resolution = resolve_identity(
        (
            _observation("authoritative", "ofx", "SYN-FITID"),
            _observation("lower", "monarch", "SYN-MONARCH"),
        ),
        policy=policy,
    )
    assert resolution.interval_authorities
    connection = RecordingConnection(resolution)

    persist_identity_resolution(connection, resolution)
    statements = "\n".join(query for query, _parameters in connection.calls)

    for table in (
        "canonical_identity_source_authority_policies",
        "canonical_identity_authority_intervals",
        "canonical_identity_source_suppressions",
    ):
        assert f"INSERT INTO finance.{table}" in statements

    intervals = [
        parameters
        for query, parameters in connection.calls
        if "INSERT INTO finance.canonical_identity_authority_intervals" in query
    ]
    assert len(intervals) == len(resolution.interval_authorities)
    assert all("SYN-" not in str(value) for row in intervals for value in row)

    suppression_rows = [
        parameters
        for query, parameters in connection.calls
        if "INSERT INTO finance.canonical_identity_source_suppressions" in query
    ]
    assert len(suppression_rows) == 1
    assert suppression_rows[0][9] == 0
    assert suppression_rows[0][10] == 1
    assert suppression_rows[0][11] == 1

    decision_rows = [
        parameters
        for query, parameters in connection.calls
        if "INSERT INTO finance.canonical_identity_automatic_decisions" in query
    ]
    classifications = {row[13] for row in decision_rows}
    assert "source-suppressed" in classifications
    assert all(row[13] is not None for row in decision_rows)
    suppressed = [row for row in decision_rows if row[13] == "source-suppressed"]
    assert len(suppressed) == 1
    assert suppressed[0][6] == "authoritative-source-coverage"
    assert suppressed[0][14] == policy.authority_policy.policy_hash
    assert all(
        row[14] is None for row in decision_rows if row[13] != "source-suppressed"
    )


def test_declared_posting_window_is_persisted_on_the_interval_row():
    from tests.test_identity_source_authority import evidence, policy_with

    qfx = evidence(
        family="qfx",
        strength="stable-provider-id",
        count=1,
        account="SYN-ACCOUNT",
        canonical_account="SYN-CANONICAL-ACCOUNT",
    )
    qfx["posting_date_tolerance_days"] = 2
    policy = policy_with(
        qfx,
        evidence(
            family="monarch",
            strength="legacy-export",
            count=1,
            account="SYN-ACCOUNT",
            canonical_account="SYN-CANONICAL-ACCOUNT",
            stable_id_support=False,
            replay_stable_ids=False,
        ),
    )
    resolution = resolve_identity(
        (
            _observation("authoritative", "qfx", "SYN-FITID"),
            _observation("lower", "monarch", "SYN-MONARCH"),
        ),
        policy=policy,
    )
    connection = RecordingConnection(resolution)

    persist_identity_resolution(connection, resolution)

    intervals = [
        parameters
        for query, parameters in connection.calls
        if "INSERT INTO finance.canonical_identity_authority_intervals" in query
    ]
    assert len(intervals) == len(resolution.interval_authorities)
    windows = sorted(row[22] for row in intervals)
    assert windows == [0, 2]
    for query, _parameters in connection.calls:
        if "canonical_identity_authority_intervals" in query:
            assert "posting_date_tolerance_days" in query
    assert all("SYN-" not in str(value) for row in intervals for value in row)


def _hashed_observation(name: str, family: str, provider_id: str):
    """A canonical-scope observation: identified by a content hash, not a uuid."""

    base = _observation(name, family, provider_id)
    return replace(base, observation_id=content_hash(("canonical-scope", name)))


def _bindings(connection, table: str):
    """Every observation row written to ``table``, as ``(uuid, identity hash)``.

    Positions are read from each statement's own column and VALUES lists so the
    test asserts on what the writer says it binds, rather than on a hand-counted
    offset. Literal values in the VALUES list consume a column but no parameter.
    """

    rows = []
    for query, parameters in connection.calls:
        if f"INSERT INTO finance.{table}" not in query:
            continue
        head, _, tail = query.partition(") VALUES (")
        columns = [column.strip() for column in head.split("(", 1)[1].split(",")]
        placeholders = [value.strip() for value in tail.split(")", 1)[0].split(",")]
        if "observation_identity_hash" not in columns:
            continue  # a source-claim member: no observation identity at all
        bound = {}
        parameter_index = 0
        for column, placeholder in zip(columns, placeholders, strict=True):
            if placeholder.startswith("%s"):
                bound[column] = parameters[parameter_index]
                parameter_index += 1
        rows.append(
            (
                bound["transaction_observation_id"],
                bound["observation_identity_hash"],
            )
        )
    return rows


MEMBERSHIPS = "canonical_identity_observation_memberships"
EVENT_MEMBERS = "canonical_identity_event_members"


def test_hash_identified_observations_persist_as_published_identity_hashes():
    left = _hashed_observation("left", "monarch", "SYN-LEFT")
    right = _hashed_observation("right", "simplefin", "SYN-RIGHT")
    resolution = resolve_identity((left, right))
    connection = RecordingConnection(resolution)

    persist_identity_resolution(connection, resolution)

    published = {left.observation_id, right.observation_id}
    for table in (MEMBERSHIPS, EVENT_MEMBERS):
        rows = _bindings(connection, table)
        assert rows, table
        # No uuid is invented, and the stored hash is the published one verbatim.
        assert all(observation_uuid is None for observation_uuid, _hash in rows)
        assert {stored for _uuid, stored in rows} == published


def test_uuid_observations_bind_the_foreign_key_only_when_the_row_exists():
    left = _observation("left", "monarch", "SYN-LEFT")
    right = _observation("right", "simplefin", "SYN-RIGHT")
    resolution = resolve_identity((left, right))
    connection = RecordingConnection(resolution, ingested=(left.observation_id,))

    persist_identity_resolution(connection, resolution)

    for table in (MEMBERSHIPS, EVENT_MEMBERS):
        rows = _bindings(connection, table)
        bound = {observation_uuid for observation_uuid, _hash in rows if observation_uuid}
        # The ingested row is a real foreign key.
        assert bound == {left.observation_id}
        # The uuid that is not in finance.transaction_observations would have been
        # a dangling reference, so it is recorded as an identity instead.
        hashed = [stored for observation_uuid, stored in rows if observation_uuid is None]
        assert hashed == [content_hash(right.observation_id)]
        assert all(value is None or len(value) == 64 for _uuid, value in rows if value)


def test_absent_uuid_rows_never_become_foreign_keys():
    resolution = resolve_identity(
        (
            _observation("left", "monarch", "SYN-LEFT"),
            _observation("right", "simplefin", "SYN-RIGHT"),
        )
    )
    connection = RecordingConnection(resolution)

    persist_identity_resolution(connection, resolution)

    for table in (MEMBERSHIPS, EVENT_MEMBERS):
        rows = _bindings(connection, table)
        assert rows, table
        assert all(observation_uuid is None for observation_uuid, _hash in rows)
        assert all(stored for _uuid, stored in rows)


def test_exactly_one_identity_is_bound_on_every_observation_row():
    resolution = resolve_identity(
        (
            _observation("left", "monarch", "SYN-LEFT"),
            _hashed_observation("right", "simplefin", "SYN-RIGHT"),
        )
    )
    connection = RecordingConnection(
        resolution, ingested=(stable_id("synthetic-observation", "left"),)
    )

    persist_identity_resolution(connection, resolution)

    for table in (MEMBERSHIPS, EVENT_MEMBERS):
        rows = _bindings(connection, table)
        assert rows, table
        for observation_uuid, stored in rows:
            assert (observation_uuid is None) != (stored is None)


def test_existence_is_probed_once_for_uuid_ids_only():
    left = _observation("left", "monarch", "SYN-LEFT")
    right = _hashed_observation("right", "simplefin", "SYN-RIGHT")
    resolution = resolve_identity((left, right))
    connection = RecordingConnection(resolution)

    persist_identity_resolution(connection, resolution)

    probes = [
        parameters
        for query, parameters in connection.calls
        if "FROM finance.transaction_observations" in query
    ]
    # One batched read, inside the caller's transaction, and only the uuid-shaped
    # identifiers are asked about.
    assert len(probes) == 1
    assert probes[0][0] == [left.observation_id]
    assert not any("COMMIT" in query for query, _parameters in connection.calls)


def test_hash_identified_persistence_is_replay_stable():
    observations = (
        _hashed_observation("left", "monarch", "SYN-LEFT"),
        _hashed_observation("right", "simplefin", "SYN-RIGHT"),
    )
    first = RecordingConnection(resolve_identity(observations))
    persist_identity_resolution(first, first.resolution)
    second = RecordingConnection(resolve_identity(tuple(reversed(observations))))
    persist_identity_resolution(second, second.resolution)

    assert first.calls == second.calls


def test_no_observation_identifier_is_written_without_a_probe():
    """A dangling foreign key must never be preferred over a recorded identity."""

    resolution = resolve_identity((_observation("single", "simplefin", "SYN-SINGLE"),))
    connection = RecordingConnection(resolution)

    persist_identity_resolution(connection, resolution)

    queries = [query for query, _parameters in connection.calls]
    probe = next(
        index
        for index, query in enumerate(queries)
        if "FROM finance.transaction_observations" in query
    )
    membership = next(
        index
        for index, query in enumerate(queries)
        if "INSERT INTO finance.canonical_identity_observation_memberships" in query
    )
    assert probe < membership
