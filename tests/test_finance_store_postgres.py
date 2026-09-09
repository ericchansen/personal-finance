"""Optional disposable PostgreSQL integration tests.

Set FINANCE_POSTGRES_TEST_DSN only for a migrated, disposable database.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone

import pytest
import psycopg
from psycopg.rows import dict_row

from finance_store.domain import DurableDecision, FinanceState, content_hash
from finance_store.identity import (
    observations_from_finance_state,
    resolve_identity,
)
from finance_store.identity_postgres import (
    persist_application_projection_bindings,
)
from finance_store.memory import MemoryRepository
from finance_store.postgres import PostgresRepository
from finance_store.reconcile import IngestionService, build_plan, projection_hash
from finance_store.replay import state_digest
from finance_store.simplefin import SimpleFinAdapter

pytestmark = pytest.mark.skipif(
    not os.environ.get("FINANCE_POSTGRES_TEST_DSN"),
    reason="requires the disposable PostgreSQL integration profile",
)


def test_postgres_transactional_ingest_and_replay():
    raw = json.dumps({
        "accounts": [{
            "id": "integration-account",
            "name": "Synthetic Integration Account",
            "canonical_key": "integration-canonical-account",
            "currency": "USD",
            "balance": "42.00",
            "balance-date": 1788307200,
            "transactions": [{
                "id": "integration-transaction",
                "posted": 1788220800,
                "amount": "-8.00",
                "description": "Synthetic Integration Merchant",
            }],
        }]
    }, sort_keys=True).encode()
    batch = SimpleFinAdapter("postgres-integration").parse(
        raw,
        raw_locator="external://synthetic/postgres-integration",
        observed_at=datetime(2026, 9, 2, 12, tzinfo=timezone.utc),
    )
    repository = PostgresRepository(os.environ["FINANCE_POSTGRES_TEST_DSN"])
    service = IngestionService(repository)
    first = service.ingest(batch)
    second = service.ingest(batch)
    assert first.canonical_transactions_created == 1
    assert second.replayed_observations == 1
    assert first.state_hash == second.state_hash
    state = repository.state()
    account_id = batch.accounts[0].id
    observations = [
        item
        for item in state.transaction_observations
        if item.source_account_id == account_id
    ]
    canonical_id = next(
        link.canonical_transaction_id
        for link in state.links
        if link.observation_id == observations[0].id
    )
    assert len(observations) == 1
    assert sum(
        item.canonical_transaction_id == canonical_id
        for item in state.projection_records
    ) == 1


def test_postgres_persists_and_replays_identity_generation():
    adapter = SimpleFinAdapter("postgres-identity-integration")

    def batch(amount: str, observed_at: datetime):
        raw = json.dumps(
            {
                "accounts": [
                    {
                        "id": "identity-account",
                        "name": "Synthetic Identity Account",
                        "canonical_key": "identity-canonical-account",
                        "currency": "USD",
                        "transactions": [
                            {
                                "id": "identity-transaction",
                                "posted": 1788220800,
                                "amount": amount,
                                "description": "Synthetic Identity Merchant",
                            }
                        ],
                    }
                ]
            },
            sort_keys=True,
        ).encode()
        return adapter.parse(
            raw,
            raw_locator=(
                "external://synthetic/postgres-identity/"
                f"{amount.removeprefix('-')}"
            ),
            observed_at=observed_at,
        )

    first_batch = batch(
        "-8.00", datetime(2026, 9, 2, 12, tzinfo=timezone.utc)
    )
    second_batch = batch(
        "-8.25", datetime(2026, 9, 3, 12, tzinfo=timezone.utc)
    )
    repository = PostgresRepository(os.environ["FINANCE_POSTGRES_TEST_DSN"])
    service = IngestionService(repository)
    service.ingest(first_batch)
    service.ingest(second_batch)
    state = repository.state()
    account_ids = {item.id for item in second_batch.accounts}
    relevant = FinanceState(
        connections=tuple(
            item
            for item in state.connections
            if item.id == second_batch.connection.id
        ),
        source_accounts=tuple(
            item for item in state.source_accounts if item.id in account_ids
        ),
        transaction_observations=tuple(
            item
            for item in state.transaction_observations
            if item.source_account_id in account_ids
        ),
    )
    resolution = resolve_identity(observations_from_finance_state(relevant))

    first = repository.persist_identity_resolution(resolution)
    replay = repository.persist_identity_resolution(resolution)
    canonical_id = resolution.canonical_events[0].canonical_event_id
    target_hash = content_hash("synthetic-wealthfolio-identity-activity")
    with repository.unit_of_work(
        "canonical-transaction-identity-projection"
    ) as unit:
        assert (
            persist_application_projection_bindings(
                unit.connection,
                resolution,
                {canonical_id: target_hash},
            )
            == 1
        )
    with repository.unit_of_work(
        "canonical-transaction-identity-projection-replay"
    ) as unit:
        assert (
            persist_application_projection_bindings(
                unit.connection,
                resolution,
                {canonical_id: target_hash},
            )
            == 1
        )

    assert first.inserted is True
    assert replay.inserted is False
    assert first.generation_hash == replay.generation_hash
    with psycopg.connect(
        os.environ["FINANCE_POSTGRES_TEST_DSN"], row_factory=dict_row
    ) as connection:
        summary = connection.execute(
            """
            SELECT automatic_decision_count, canonical_event_count,
                   input_hash, canonical_state_hash,
                   active_wealthfolio_binding_count
            FROM finance_read.identity_generation_summary
            WHERE generation_hash = %s
            """,
            (resolution.generation_hash,),
        ).fetchone()
    assert summary == {
        "automatic_decision_count": 1,
        "canonical_event_count": 1,
        "input_hash": resolution.input_hash,
        "canonical_state_hash": resolution.canonical_state_hash,
        "active_wealthfolio_binding_count": 1,
    }


def test_postgres_advisory_lock_serializes_concurrent_ingest():
    raw = json.dumps({
        "accounts": [{
            "id": "concurrent-account",
            "name": "Synthetic Concurrent Account",
            "canonical_key": "concurrent-canonical-account",
            "currency": "USD",
            "transactions": [{
                "id": "concurrent-transaction",
                "posted": 1788220800,
                "amount": "-3.00",
                "description": "Synthetic Concurrent Merchant",
            }],
        }]
    }, sort_keys=True).encode()
    batch = SimpleFinAdapter("postgres-concurrent").parse(
        raw,
        raw_locator="external://synthetic/postgres-concurrent",
        observed_at=datetime(2026, 9, 2, 12, tzinfo=timezone.utc),
    )
    repository = PostgresRepository(os.environ["FINANCE_POSTGRES_TEST_DSN"])
    barrier = threading.Barrier(2)
    results = []

    def ingest():
        barrier.wait()
        results.append(IngestionService(repository).ingest(batch))

    threads = [threading.Thread(target=ingest) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(item.canonical_transactions_created for item in results) == [0, 1]
    assert sorted(item.replayed_observations for item in results) == [0, 1]
    state = repository.state()
    assert sum(
        item.source_account_id == batch.accounts[0].id
        for item in state.transaction_observations
    ) == 1


def test_postgres_writer_gate_blocks_all_repository_mutations():
    dsn = os.environ["FINANCE_POSTGRES_TEST_DSN"]
    repository = PostgresRepository(dsn)
    with psycopg.connect(dsn) as connection:
        connection.execute(
            """
            UPDATE finance.writer_gate
            SET migrations_blocked = true,
                owner_token = 'synthetic-writer-gate-test',
                changed_at = clock_timestamp()
            WHERE singleton
            """
        )
        connection.commit()
    try:
        with pytest.raises(RuntimeError, match="blocked for migration"):
            repository.append_decision(
                DurableDecision(
                    id="8cdd0272-a093-55f8-9acf-5dbdf56cf951",
                    decision_type="duplicate_resolution",
                    subject_type="synthetic",
                    subject_key="writer-gate",
                    action="retain_both",
                    rationale="Synthetic writer gate verification",
                    decided_by="synthetic-test",
                    effective_at=datetime(
                        2026, 9, 2, 12, tzinfo=timezone.utc
                    ),
                    observed_at=datetime(
                        2026, 9, 2, 12, tzinfo=timezone.utc
                    ),
                    processed_at=datetime(
                        2026, 9, 2, 12, tzinfo=timezone.utc
                    ),
                )
            )
    finally:
        with psycopg.connect(dsn) as connection:
            connection.execute(
                """
                UPDATE finance.writer_gate
                SET migrations_blocked = false,
                    owner_token = NULL,
                    changed_at = clock_timestamp()
                WHERE singleton
                """
            )
            connection.commit()


def test_backup_role_is_scoped_and_cannot_read_pg_authid():
    dsn = os.environ["FINANCE_POSTGRES_TEST_DSN"]
    with psycopg.connect(dsn) as connection:
        privileges = connection.execute(
            """
            SELECT
                pg_has_role(
                    'finance_shadow_backup',
                    'pg_read_all_data',
                    'member'
                ),
                has_schema_privilege(
                    'finance_shadow_backup',
                    'finance',
                    'USAGE'
                ),
                has_schema_privilege(
                    'finance_shadow_backup',
                    'finance_read',
                    'USAGE'
                ),
                has_table_privilege(
                    'finance_shadow_backup',
                    'finance.source_blobs',
                    'SELECT'
                ),
                has_table_privilege(
                    'finance_shadow_backup',
                    'finance.source_blobs',
                    'INSERT'
                ),
                has_table_privilege(
                    'finance_shadow_backup',
                    'pg_catalog.pg_authid',
                    'SELECT'
                )
            """
        ).fetchone()
        assert privileges == (False, True, True, True, False, False)
        with connection.transaction():
            connection.execute("SET LOCAL ROLE finance_shadow_backup")
            connection.execute("SELECT count(*) FROM finance.source_blobs")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with connection.transaction():
                connection.execute("SET LOCAL ROLE finance_shadow_backup")
                connection.execute("SELECT rolpassword FROM pg_authid")


def test_postgres_interruption_rolls_back_whole_ingest():
    raw = json.dumps({
        "accounts": [{
            "id": "interrupted-account",
            "name": "Synthetic Interrupted Account",
            "canonical_key": "interrupted-canonical-account",
            "currency": "USD",
            "transactions": [{
                "id": "interrupted-transaction",
                "posted": 1788220800,
                "amount": "-4.00",
                "description": "Synthetic Interrupted Merchant",
            }],
        }]
    }, sort_keys=True).encode()
    batch = SimpleFinAdapter("postgres-interrupted").parse(
        raw,
        raw_locator="external://synthetic/postgres-interrupted",
        observed_at=datetime(2026, 9, 2, 12, tzinfo=timezone.utc),
    )
    repository = PostgresRepository(os.environ["FINANCE_POSTGRES_TEST_DSN"])
    with pytest.raises(RuntimeError, match="synthetic interruption"):
        with repository.unit_of_work("postgres-interrupted") as unit:
            unit.apply(batch, build_plan(unit.state(), batch))
            raise RuntimeError("synthetic interruption")
    assert batch.blob.id not in {item.id for item in repository.state().blobs}


def test_postgres_remap_closes_history_and_keeps_transactions_consistent():
    def remap_payload(canonical_key, transaction_id):
        return json.dumps({
            "accounts": [{
                "id": "remapped-source-account",
                "name": "Synthetic Remapped Account",
                "canonical_key": canonical_key,
                "currency": "USD",
                "transactions": [{
                    "id": transaction_id,
                    "posted": 1788220800,
                    "amount": "-5.00",
                    "description": "Synthetic Remap Merchant",
                }],
            }]
        }, sort_keys=True).encode()

    adapter = SimpleFinAdapter("postgres-remap")
    first = adapter.parse(
        remap_payload("remap-canonical-before", "remap-transaction-before"),
        raw_locator="external://synthetic/postgres-remap-before",
        observed_at=datetime(2026, 9, 1, 12, tzinfo=timezone.utc),
    )
    second = adapter.parse(
        remap_payload("remap-canonical-after", "remap-transaction-after"),
        raw_locator="external://synthetic/postgres-remap-after",
        observed_at=datetime(2026, 9, 2, 12, tzinfo=timezone.utc),
    )
    dsn = os.environ["FINANCE_POSTGRES_TEST_DSN"]
    repository = PostgresRepository(dsn)
    service = IngestionService(repository)
    service.ingest(first)
    service.ingest(second)

    with psycopg.connect(dsn, row_factory=dict_row) as connection:
        history = connection.execute(
            """
            SELECT canonical_key, is_current, decision_id
            FROM finance_read.source_account_mapping_history
            WHERE source_account_id = %s
            ORDER BY effective_from, canonical_key
            """,
            (second.accounts[0].id,),
        ).fetchall()
        transaction_account = connection.execute(
            """
            SELECT ca.canonical_key
            FROM finance.transaction_observations tro
            JOIN finance.transaction_observation_links tol
              ON tol.transaction_observation_id = tro.transaction_observation_id
             AND tol.is_current
            JOIN finance.canonical_transactions ct
              ON ct.canonical_transaction_id = tol.canonical_transaction_id
            JOIN finance.canonical_accounts ca
              ON ca.canonical_account_id = ct.canonical_account_id
            WHERE tro.source_account_id = %s
              AND tro.source_transaction_id = 'remap-transaction-after'
            """,
            (second.accounts[0].id,),
        ).fetchone()
        remap_audit_count = connection.execute(
            """
            SELECT count(*)
            FROM finance.audit_events
            WHERE event_type = 'source_account_remapped'
              AND subject_key = %s
            """,
            (second.accounts[0].id,),
        ).fetchone()["count"]

    assert [(row["canonical_key"], row["is_current"]) for row in history] == [
        ("remap-canonical-before", False),
        ("remap-canonical-after", True),
    ]
    assert all(row["decision_id"] for row in history)
    assert transaction_account["canonical_key"] == "remap-canonical-after"
    assert remap_audit_count == 1
    current_account = next(
        account
        for account in repository.state().source_accounts
        if account.id == second.accounts[0].id
    )
    assert current_account.canonical_key == "remap-canonical-after"


def test_postgres_same_batch_versions_project_final_canonical_hash():
    raw = json.dumps({
        "accounts": [{
            "id": "same-batch-account",
            "name": "Synthetic Same Batch Account",
            "canonical_key": "same-batch-canonical-account",
            "currency": "USD",
            "transactions": [
                {
                    "id": "same-batch-transaction",
                    "posted": 1788220800,
                    "amount": "-10.00",
                    "description": "Synthetic Same Batch Merchant",
                    "pending": True,
                },
                {
                    "id": "same-batch-transaction",
                    "posted": 1788307200,
                    "amount": "-11.00",
                    "description": "Synthetic Same Batch Merchant",
                    "pending": False,
                },
            ],
        }]
    }, sort_keys=True).encode()
    batch = SimpleFinAdapter("postgres-same-batch").parse(
        raw,
        raw_locator="external://synthetic/postgres-same-batch",
        observed_at=datetime(2026, 9, 2, 12, tzinfo=timezone.utc),
    )
    repository = PostgresRepository(os.environ["FINANCE_POSTGRES_TEST_DSN"])
    IngestionService(repository).ingest(batch)
    state = repository.state()
    canonical = next(
        item
        for item in state.canonical_transactions
        if item.key.startswith("same-batch-canonical-account:")
    )
    records = [
        item
        for item in state.projection_records
        if item.canonical_transaction_id == canonical.id and item.current
    ]
    assert len(records) == 1
    assert canonical.amount == -11
    assert records[0].projected_hash == projection_hash(canonical)


def test_postgres_backdated_remap_matches_neutral_rejection():
    def remap(canonical_key, observed_at, locator):
        return SimpleFinAdapter("postgres-backdated-remap").parse(
            json.dumps({
                "accounts": [{
                    "id": "backdated-remap-account",
                    "name": "Synthetic Backdated Remap Account",
                    "canonical_key": canonical_key,
                    "currency": "USD",
                    "transactions": [],
                }]
            }, sort_keys=True).encode(),
            raw_locator=locator,
            observed_at=observed_at,
        )

    repository = PostgresRepository(os.environ["FINANCE_POSTGRES_TEST_DSN"])
    service = IngestionService(repository)
    service.ingest(
        remap(
            "backdated-current",
            datetime(2026, 9, 2, 12, tzinfo=timezone.utc),
            "external://synthetic/backdated-current",
        )
    )
    with pytest.raises(
        ValueError, match="remapping cannot precede the current mapping"
    ):
        service.ingest(
            remap(
                "backdated-rejected",
                datetime(2026, 9, 1, 12, tzinfo=timezone.utc),
                "external://synthetic/backdated-rejected",
            )
        )
    state = repository.state()
    account = next(
        item
        for item in state.source_accounts
        if item.external_id == "backdated-remap-account"
    )
    assert account.canonical_key == "backdated-current"


def test_postgres_same_mapping_refresh_preserves_mapping_chronology():
    def remap(canonical_key, observed_at, locator):
        return SimpleFinAdapter("postgres-remap-parity").parse(
            json.dumps({
                "accounts": [{
                    "id": "remap-parity-account",
                    "name": "Synthetic Remap Parity Account",
                    "canonical_key": canonical_key,
                    "currency": "USD",
                    "transactions": [],
                }]
            }, sort_keys=True).encode(),
            raw_locator=locator,
            observed_at=observed_at,
        )

    repository = PostgresRepository(os.environ["FINANCE_POSTGRES_TEST_DSN"])
    service = IngestionService(repository)
    service.ingest(
        remap(
            "remap-parity-before",
            datetime(2026, 9, 1, 12, tzinfo=timezone.utc),
            "external://synthetic/remap-parity-first",
        )
    )
    service.ingest(
        remap(
            "remap-parity-before",
            datetime(2026, 9, 3, 12, tzinfo=timezone.utc),
            "external://synthetic/remap-parity-refresh",
        )
    )
    service.ingest(
        remap(
            "remap-parity-after",
            datetime(2026, 9, 2, 12, tzinfo=timezone.utc),
            "external://synthetic/remap-parity-intermediate",
        )
    )
    account = next(
        item
        for item in repository.state().source_accounts
        if item.external_id == "remap-parity-account"
    )
    assert account.canonical_key == "remap-parity-after"
    assert account.mapping_effective_from == datetime(
        2026, 9, 2, 12, tzinfo=timezone.utc
    )


def test_postgres_trust_cutoff_revision_updates_enforced_state():
    def cutoff_batch(cutoff, transactions, observed_at, locator):
        return SimpleFinAdapter("postgres-cutoff-revision").parse(
            json.dumps({
                "accounts": [{
                    "id": "cutoff-revision-account",
                    "name": "Synthetic Cutoff Revision Account",
                    "canonical_key": "cutoff-revision-canonical",
                    "currency": "USD",
                    "transactions": transactions,
                }]
            }, sort_keys=True).encode(),
            raw_locator=locator,
            observed_at=observed_at,
            trust_cutoffs={"cutoff-revision-account": cutoff},
        )

    first_cutoff = datetime(2026, 9, 1, tzinfo=timezone.utc)
    revised_cutoff = datetime(2026, 9, 3, tzinfo=timezone.utc)
    repository = PostgresRepository(os.environ["FINANCE_POSTGRES_TEST_DSN"])
    service = IngestionService(repository)
    service.ingest(
        cutoff_batch(
            first_cutoff,
            [],
            datetime(2026, 9, 1, 12, tzinfo=timezone.utc),
            "external://synthetic/cutoff-revision-first",
        )
    )
    service.ingest(
        cutoff_batch(
            revised_cutoff,
            [{
                "id": "newly-trusted-transaction",
                "posted": 1788307200,
                "amount": "-6.00",
                "description": "Synthetic Newly Trusted Merchant",
            }],
            datetime(2026, 9, 2, 12, tzinfo=timezone.utc),
            "external://synthetic/cutoff-revision-second",
        )
    )
    state = repository.state()
    account = next(
        item
        for item in state.source_accounts
        if item.external_id == "cutoff-revision-account"
    )
    assert account.trust_cutoff_at == revised_cutoff
    assert sum(
        decision.decision_type == "trust_cutoff"
        and decision.subject_key == account.id
        for decision in state.decisions
    ) == 2
    assert any(
        event.event_type == "trust_cutoff_revised"
        and event.subject_key == account.id
        for event in state.audit_events
    )
    observation = next(
        item
        for item in state.transaction_observations
        if item.source_transaction_id == "newly-trusted-transaction"
    )
    canonical_id = next(
        link.canonical_transaction_id
        for link in state.links
        if link.observation_id == observation.id
    )
    assert any(
        record.canonical_transaction_id == canonical_id and record.current
        for record in state.projection_records
    )


def test_postgres_omitted_cutoff_inherits_and_removal_is_explicit():
    def batch(cutoffs, transaction_id, observed_at, locator):
        transactions = ([{
            "id": transaction_id,
            "posted": 1788307200,
            "amount": "-7.00",
            "description": "Synthetic Cutoff Inheritance Merchant",
        }] if transaction_id else [])
        return SimpleFinAdapter("postgres-cutoff-inheritance").parse(
            json.dumps({"accounts": [{
                "id": "cutoff-inheritance-account",
                "name": "Synthetic Cutoff Inheritance Account",
                "canonical_key": "cutoff-inheritance-canonical",
                "currency": "USD",
                "transactions": transactions,
            }]}, sort_keys=True).encode(),
            raw_locator=locator,
            observed_at=observed_at,
            trust_cutoffs=cutoffs,
        )

    repository = PostgresRepository(os.environ["FINANCE_POSTGRES_TEST_DSN"])
    service = IngestionService(repository)
    cutoff = datetime(2026, 9, 1, tzinfo=timezone.utc)
    service.ingest(batch(
        {"cutoff-inheritance-account": cutoff}, None,
        datetime(2026, 9, 1, 12, tzinfo=timezone.utc),
        "external://synthetic/pg-cutoff-inheritance-first",
    ))
    service.ingest(batch(
        {}, "cutoff-inheritance-stale",
        datetime(2026, 9, 2, 12, tzinfo=timezone.utc),
        "external://synthetic/pg-cutoff-inheritance-omitted",
    ))
    inherited = next(
        item for item in repository.state().source_accounts
        if item.external_id == "cutoff-inheritance-account"
    )
    assert inherited.trust_cutoff_at == cutoff
    inherited_state = repository.state()
    stale_observation = next(
        item for item in inherited_state.transaction_observations
        if item.source_transaction_id == "cutoff-inheritance-stale"
    )
    assert any(
        issue.subject_key == stale_observation.id
        and issue.issue_type == "stale_after_trust_cutoff"
        for issue in inherited_state.issues
    )
    assert not any(
        link.observation_id == stale_observation.id
        and any(
            record.canonical_transaction_id == link.canonical_transaction_id
            and record.current
            for record in inherited_state.projection_records
        )
        for link in inherited_state.links
    )
    service.ingest(batch(
        {"cutoff-inheritance-account": None}, None,
        datetime(2026, 9, 3, 12, tzinfo=timezone.utc),
        "external://synthetic/pg-cutoff-inheritance-removed",
    ))
    state = repository.state()
    removed = next(
        item for item in state.source_accounts
        if item.external_id == "cutoff-inheritance-account"
    )
    assert removed.trust_cutoff_at is None
    assert any(
        decision.action == "clear_cutoff" and decision.subject_key == removed.id
        for decision in state.decisions
    )


def test_postgres_reobserved_versions_follow_last_sighting():
    repository = PostgresRepository(os.environ["FINANCE_POSTGRES_TEST_DSN"])
    service = IngestionService(repository)
    for amount, day in [
        ("-10.00", 1), ("-11.00", 2), ("-10.00", 3), ("-11.00", 4)
    ]:
        raw = json.dumps({"accounts": [{
            "id": "sighting-account",
            "name": "Synthetic Sighting Account",
            "canonical_key": "sighting-canonical",
            "currency": "USD",
            "transactions": [{
                "id": "sighting-transaction",
                "posted": 1788220800,
                "amount": amount,
                "description": "Synthetic Sighting Merchant",
            }],
        }]}, sort_keys=True).encode()
        service.ingest(SimpleFinAdapter("postgres-sighting").parse(
            raw,
            raw_locator=f"external://synthetic/pg-sighting-{day}",
            observed_at=datetime(2026, 9, day, 12, tzinfo=timezone.utc),
        ))
    state = repository.state()
    canonical = next(
        item for item in state.canonical_transactions
        if item.key.startswith("sighting-canonical:")
    )
    assert canonical.amount == -11
    observations = [
        item for item in state.transaction_observations
        if item.source_transaction_id == "sighting-transaction"
    ]
    assert max(observations, key=lambda item: item.last_seen_at).amount == -11


def test_postgres_source_account_refresh_matches_memory_policy():
    def batch(name, account_type, observed_at, locator):
        raw = json.dumps({"accounts": [{
            "id": "refresh-policy-account",
            "name": name,
            "account_type": account_type,
            "canonical_key": "refresh-policy-canonical",
            "currency": "USD",
            "transactions": [],
        }]}, sort_keys=True).encode()
        return SimpleFinAdapter("postgres-refresh-policy").parse(
            raw, raw_locator=locator, observed_at=observed_at
        )

    first = batch(
        "Synthetic Original Account", "unknown",
        datetime(2026, 9, 1, 12, tzinfo=timezone.utc),
        "external://synthetic/pg-refresh-first",
    )
    second = batch(
        "Synthetic Refreshed Account", "checking",
        datetime(2026, 9, 2, 12, tzinfo=timezone.utc),
        "external://synthetic/pg-refresh-second",
    )
    postgres = PostgresRepository(os.environ["FINANCE_POSTGRES_TEST_DSN"])
    memory = MemoryRepository()
    for repository in (postgres, memory):
        service = IngestionService(repository)
        service.ingest(first)
        service.ingest(second)
    pg_account = next(
        item for item in postgres.state().source_accounts
        if item.external_id == "refresh-policy-account"
    )
    memory_account = memory.state().source_accounts[0]
    assert (
        pg_account.name, pg_account.account_type, pg_account.effective_from,
        pg_account.mapping_effective_from, pg_account.observed_at,
    ) == (
        memory_account.name, memory_account.account_type, memory_account.effective_from,
        memory_account.mapping_effective_from, memory_account.observed_at,
    )


def test_postgres_stale_view_uses_subject_linked_issue_status():
    cutoff = datetime(2026, 9, 1, tzinfo=timezone.utc)
    raw = json.dumps({"accounts": [{
        "id": "stale-view-account",
        "name": "Synthetic Stale View Account",
        "canonical_key": "stale-view-canonical",
        "currency": "USD",
        "transactions": [{
            "id": "stale-view-transaction",
            "posted": 1788307200,
            "amount": "-2.00",
            "description": "Synthetic Stale View Merchant",
        }],
    }]}, sort_keys=True).encode()
    batch = SimpleFinAdapter("postgres-stale-view").parse(
        raw,
        raw_locator="external://synthetic/pg-stale-view",
        observed_at=datetime(2026, 9, 2, 12, tzinfo=timezone.utc),
        trust_cutoffs={"stale-view-account": cutoff},
    )
    repository = PostgresRepository(os.environ["FINANCE_POSTGRES_TEST_DSN"])
    IngestionService(repository).ingest(batch)
    with psycopg.connect(
        os.environ["FINANCE_POSTGRES_TEST_DSN"], row_factory=dict_row
    ) as connection:
        row = connection.execute(
            "SELECT review_status FROM finance_read.stale_trust_cutoff_issues "
            "WHERE observation_id = %s",
            (batch.transactions[0].id,),
        ).fetchone()
    assert row["review_status"] == "open"


def test_postgres_and_memory_state_hashes_normalize_decimal_scale():
    postgres = PostgresRepository(os.environ["FINANCE_POSTGRES_TEST_DSN"])
    memory = MemoryRepository(postgres.state())
    raw = json.dumps({"accounts": [{
        "id": "hash-parity-account",
        "name": "Synthetic Hash Parity Account",
        "canonical_key": "hash-parity-canonical",
        "currency": "USD",
        "transactions": [{
            "id": "hash-parity-transaction",
            "posted": 1788220800,
            "amount": "-1.20",
            "description": "Synthetic Hash Parity Merchant",
        }],
    }]}, sort_keys=True).encode()
    batch = SimpleFinAdapter("postgres-hash-parity").parse(
        raw,
        raw_locator="external://synthetic/pg-hash-parity",
        observed_at=datetime(2026, 9, 2, 12, tzinfo=timezone.utc),
    )
    memory_result = IngestionService(memory).ingest(batch)
    postgres_result = IngestionService(postgres).ingest(batch)
    assert memory_result.state_hash == postgres_result.state_hash


def test_postgres_state_reads_are_repeatable_during_concurrent_commit():
    repository = PostgresRepository(os.environ["FINANCE_POSTGRES_TEST_DSN"])
    raw = json.dumps({"accounts": [{
        "id": "snapshot-concurrent-account",
        "name": "Synthetic Snapshot Account",
        "canonical_key": "snapshot-concurrent-canonical",
        "currency": "USD",
        "transactions": [],
    }]}, sort_keys=True).encode()
    batch = SimpleFinAdapter("postgres-snapshot-concurrent").parse(
        raw,
        raw_locator="external://synthetic/pg-snapshot-concurrent",
        observed_at=datetime(2026, 9, 2, 12, tzinfo=timezone.utc),
    )
    with repository.read_snapshot() as unit:
        before = unit.state()
        thread = threading.Thread(
            target=IngestionService(repository).ingest, args=(batch,)
        )
        thread.start()
        thread.join()
        during = unit.state()
    after = repository.state()
    assert state_digest(before) == state_digest(during)
    assert len(after.source_accounts) == len(before.source_accounts) + 1


def test_postgres_cutoff_tightening_withdraws_and_clear_reprojects():
    def batch(cutoffs, transaction_id, day, locator):
        transactions = ([{
            "id": transaction_id,
            "posted": 1788307200,
            "amount": "-9.00",
            "description": "Synthetic Eligibility Merchant",
        }] if transaction_id else [])
        return SimpleFinAdapter("postgres-eligibility").parse(
            json.dumps({"accounts": [{
                "id": "eligibility-account",
                "name": "Synthetic Eligibility Account",
                "canonical_key": "eligibility-canonical",
                "currency": "USD",
                "transactions": transactions,
            }]}, sort_keys=True).encode(),
            raw_locator=locator,
            observed_at=datetime(2026, 9, day, 12, tzinfo=timezone.utc),
            trust_cutoffs=cutoffs,
        )

    repository = PostgresRepository(os.environ["FINANCE_POSTGRES_TEST_DSN"])
    service = IngestionService(repository)
    service.ingest(batch({}, "eligible", 1, "external://synthetic/pg-eligible"))
    assert any(item.current for item in repository.state().projection_records)
    service.ingest(batch(
        {"eligibility-account": datetime(2026, 9, 1, tzinfo=timezone.utc)},
        None, 2, "external://synthetic/pg-tighten",
    ))
    assert not any(
        item.current and item.target_record_key.startswith("eligibility-canonical:")
        for item in repository.state().projection_records
    )
    with psycopg.connect(os.environ["FINANCE_POSTGRES_TEST_DSN"]) as connection:
        with pytest.raises(psycopg.errors.CheckViolation):
            with connection.transaction():
                connection.execute(
                    """
                    UPDATE finance.source_accounts
                    SET trust_cutoff_at = NULL
                    WHERE external_account_id = 'eligibility-account'
                    """
                )
    service.ingest(batch(
        {"eligibility-account": None},
        None, 3, "external://synthetic/pg-relax",
    ))
    assert sum(
        item.current and item.target_record_key.startswith("eligibility-canonical:")
        for item in repository.state().projection_records
    ) == 1


def test_postgres_rejects_nonfinite_money_at_database_boundary():
    raw = json.dumps({"accounts": [{
        "id": "money-boundary-account",
        "name": "Synthetic Money Boundary Account",
        "canonical_key": "money-boundary-canonical",
        "currency": "USD",
        "transactions": [],
    }]}, sort_keys=True).encode()
    batch = SimpleFinAdapter("postgres-money-boundary").parse(
        raw,
        raw_locator="external://synthetic/pg-money-boundary",
        observed_at=datetime(2026, 9, 2, 12, tzinfo=timezone.utc),
    )
    repository = PostgresRepository(os.environ["FINANCE_POSTGRES_TEST_DSN"])
    IngestionService(repository).ingest(batch)
    with psycopg.connect(os.environ["FINANCE_POSTGRES_TEST_DSN"]) as connection:
        with pytest.raises(psycopg.errors.CheckViolation):
            with connection.transaction():
                connection.execute(
                    """
                    INSERT INTO finance.transaction_observations (
                        ingestion_run_id, source_blob_id, source_account_id,
                        source_transaction_id, observation_hash, effective_at,
                        observed_at, amount, currency_code, source_status,
                        last_seen_run_id
                    ) VALUES (%s, %s, %s, 'nonfinite', %s, %s, %s, 'NaN',
                              'USD', 'posted', %s)
                    """,
                    (
                        batch.run.id, batch.blob.id, batch.accounts[0].id,
                        "f" * 64, batch.run.observed_at, batch.run.observed_at,
                        batch.run.id,
                    ),
                )


def test_postgres_out_of_order_status_and_cutoff_revisions_match_memory():
    def batch(amount, observed_day, *, status_marker=..., cutoff_marker=...):
        account = {
            "id": "combined-parity-account",
            "name": "Synthetic Combined Parity Account",
            "canonical_key": "combined-parity-canonical",
            "currency": "USD",
            "transactions": [{
                "id": "combined-parity-transaction",
                "posted": 1788220800,
                "amount": amount,
                "description": "Synthetic Combined Parity Merchant",
            }],
        }
        if status_marker is not ...:
            account["status"] = status_marker
        cutoffs = (
            {}
            if cutoff_marker is ...
            else {"combined-parity-account": cutoff_marker}
        )
        return SimpleFinAdapter("postgres-combined-parity").parse(
            json.dumps({"accounts": [account]}, sort_keys=True).encode(),
            raw_locator=f"external://synthetic/pg-combined-{observed_day}-{amount}",
            observed_at=datetime(2026, 9, observed_day, 12, tzinfo=timezone.utc),
            trust_cutoffs=cutoffs,
        )

    postgres = PostgresRepository(os.environ["FINANCE_POSTGRES_TEST_DSN"])
    memory = MemoryRepository(postgres.state())
    batches = [
        batch("-11.00", 2, status_marker="closed", cutoff_marker=datetime(
            2026, 9, 3, tzinfo=timezone.utc
        )),
        batch("-10.00", 1),
        batch("-11.00", 3, cutoff_marker=None),
        batch("-11.00", 4, status_marker="active", cutoff_marker=datetime(
            2026, 9, 3, tzinfo=timezone.utc
        )),
    ]
    for repository in (memory, postgres):
        service = IngestionService(repository)
        for item in batches:
            service.ingest(item)
    for state in (memory.state(), postgres.state()):
        account = next(
            item for item in state.source_accounts
            if item.external_id == "combined-parity-account"
        )
        canonical = next(
            item for item in state.canonical_transactions
            if item.key.startswith("combined-parity-canonical:")
        )
        assert account.status == "active"
        assert account.trust_cutoff_at == datetime(
            2026, 9, 3, tzinfo=timezone.utc
        )
        assert canonical.amount == -11
        decisions = [
            item for item in state.decisions
            if item.decision_type == "trust_cutoff"
            and item.subject_key == account.id
        ]
        assert len(decisions) == 3


def test_postgres_immutable_evidence_and_mapping_history_reject_deletes_and_rewrites():
    raw = json.dumps({"accounts": [{
        "id": "ddl-account",
        "name": "Synthetic DDL Account",
        "canonical_key": "ddl-canonical",
        "currency": "USD",
        "transactions": [{
            "id": "ddl-transaction",
            "posted": 1788220800,
            "amount": "-1.00",
            "description": "Synthetic DDL Merchant",
        }],
    }]}, sort_keys=True).encode()
    batch = SimpleFinAdapter("postgres-ddl").parse(
        raw,
        raw_locator="external://synthetic/pg-ddl",
        observed_at=datetime(2026, 9, 2, 12, tzinfo=timezone.utc),
    )
    repository = PostgresRepository(os.environ["FINANCE_POSTGRES_TEST_DSN"])
    IngestionService(repository).ingest(batch)
    dsn = os.environ["FINANCE_POSTGRES_TEST_DSN"]
    statements = [
        (
            "DELETE FROM finance.source_blobs WHERE source_blob_id = %s",
            (batch.blob.id,),
        ),
        (
            "DELETE FROM finance.transaction_observations "
            "WHERE transaction_observation_id = %s",
            (batch.transactions[0].id,),
        ),
        (
            "DELETE FROM finance.source_account_links WHERE source_account_id = %s",
            (batch.accounts[0].id,),
        ),
        (
            "UPDATE finance.source_account_links SET source_account_id = gen_random_uuid() "
            "WHERE source_account_id = %s",
            (batch.accounts[0].id,),
        ),
    ]
    with psycopg.connect(dsn) as connection:
        for statement, parameters in statements:
            with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState):
                with connection.transaction():
                    connection.execute(statement, parameters)


def test_postgres_cutoff_and_observation_race_leaves_no_unflagged_projection():
    connection_key = "postgres-cutoff-race"
    initial = SimpleFinAdapter(connection_key).parse(
        json.dumps({"accounts": [{
            "id": "cutoff-race-account",
            "name": "Synthetic Cutoff Race Account",
            "canonical_key": "cutoff-race-canonical",
            "currency": "USD",
            "transactions": [],
        }]}, sort_keys=True).encode(),
        raw_locator="external://synthetic/pg-cutoff-race-initial",
        observed_at=datetime(2026, 9, 1, 12, tzinfo=timezone.utc),
    )
    tightening = SimpleFinAdapter(connection_key).parse(
        json.dumps({"accounts": [{
            "id": "cutoff-race-account",
            "name": "Synthetic Cutoff Race Account",
            "canonical_key": "cutoff-race-canonical",
            "currency": "USD",
            "transactions": [],
        }]}, sort_keys=True).encode(),
        raw_locator="external://synthetic/pg-cutoff-race-tighten",
        observed_at=datetime(2026, 9, 3, 12, tzinfo=timezone.utc),
        trust_cutoffs={
            "cutoff-race-account": datetime(2026, 9, 1, tzinfo=timezone.utc)
        },
    )
    observation = SimpleFinAdapter(connection_key).parse(
        json.dumps({"accounts": [{
            "id": "cutoff-race-account",
            "name": "Synthetic Cutoff Race Account",
            "canonical_key": "cutoff-race-canonical",
            "currency": "USD",
            "transactions": [{
                "id": "cutoff-race-transaction",
                "posted": 1788307200,
                "amount": "-4.00",
                "description": "Synthetic Cutoff Race Merchant",
            }],
        }]}, sort_keys=True).encode(),
        raw_locator="external://synthetic/pg-cutoff-race-observation",
        observed_at=datetime(2026, 9, 2, 12, tzinfo=timezone.utc),
    )
    repository = PostgresRepository(os.environ["FINANCE_POSTGRES_TEST_DSN"])
    IngestionService(repository).ingest(initial)
    barrier = threading.Barrier(2)
    errors = []

    def ingest(item):
        try:
            barrier.wait()
            IngestionService(repository).ingest(item)
        except Exception as exc:
            errors.append(exc)

    threads = [
        threading.Thread(target=ingest, args=(item,))
        for item in (tightening, observation)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors
    state = repository.state()
    stale = next(
        item for item in state.transaction_observations
        if item.source_transaction_id == "cutoff-race-transaction"
    )
    assert any(
        issue.subject_key == stale.id
        and issue.issue_type == "stale_after_trust_cutoff"
        for issue in state.issues
    )
    linked = next(
        link.canonical_transaction_id for link in state.links
        if link.observation_id == stale.id
    )
    assert not any(
        record.current and record.canonical_transaction_id == linked
        for record in state.projection_records
    )
