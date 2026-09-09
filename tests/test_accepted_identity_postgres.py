"""Opt-in real PostgreSQL proof in an owned, disposable synthetic container.

FINANCE_ACCEPTED_IDENTITY_POSTGRES_TEST=1 enables this suite. It does not accept
an external DSN. The existing immutable migrations are applied with psql; no
shared shadow database, private files, collector, or application is accessed.
"""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import secrets
import subprocess
import time
import uuid

import pytest

from finance_store.domain import content_hash
from finance_store.identity_postgres import (
    accepted_current_generation_hash,
    persist_accepted_identity_resolution,
    persist_application_projection_bindings,
    persist_identity_resolution,
)
from tests.test_accepted_identity import observation, resolve_identity


pytestmark = pytest.mark.skipif(
    os.environ.get("FINANCE_ACCEPTED_IDENTITY_POSTGRES_TEST") != "1",
    reason="requires explicitly enabled owned disposable PostgreSQL container",
)
ROOT = Path(__file__).resolve().parents[1]


def docker(*args, **kwargs):
    result = subprocess.run(
        ["docker", *args], check=False, text=True, capture_output=True, **kwargs,
    )
    if result.returncode:
        raise RuntimeError(result.stderr)
    return result.stdout.strip()


@pytest.fixture(scope="module")
def database_runtime(tmp_path_factory):
    import psycopg

    name = f"finance-accepted-identity-test-{uuid.uuid4().hex[:12]}"
    password = secrets.token_hex(24)
    env = dict(os.environ, POSTGRES_PASSWORD=password)
    started = False
    try:
        docker(
            "run", "--detach", "--rm", "--name", name,
            "--label", "finance.test=accepted-identity",
            "--publish", "127.0.0.1::5432",
            "--env", "POSTGRES_PASSWORD", "--env", "POSTGRES_DB=accepted_template",
            "--mount", "type=tmpfs,destination=/var/lib/postgresql/data",
            "postgres:17.11-bookworm", env=env,
        )
        started = True
        port = int(docker("port", name, "5432/tcp").rsplit(":", 1)[1])
        for _ in range(60):
            result = subprocess.run(
                [
                    "docker", "exec", name, "pg_isready", "-q",
                    "-h", "127.0.0.1", "-U", "postgres",
                ],
                capture_output=True,
            )
            if result.returncode == 0:
                break
            time.sleep(1)
        else:
            pytest.fail("owned PostgreSQL container did not become ready")
        script_root = tmp_path_factory.mktemp("accepted-identity-runner")
        secret = script_root / "admin-password"
        secret.write_text(password, encoding="utf-8")
        docker("cp", str(secret), f"{name}:/tmp/accepted-admin-password")
        for folder in ("scripts", "migrations"):
            target = script_root / folder
            target.mkdir()
            for source in (ROOT / "deploy" / "postgres" / folder).iterdir():
                if source.is_file():
                    staged = target / source.name
                    staged.write_text(
                        source.read_text(encoding="utf-8"), encoding="utf-8", newline="\n"
                    )
                    if source.suffix == ".sh":
                        staged.chmod(0o755)
            docker("cp", str(target), f"{name}:/{folder}")
        provision = (ROOT / "deploy" / "postgres" / "scripts" / "provision-logins.sh").read_text()
        provision_sql = provision.split("<<'SQL'\n", 1)[1].split("\nSQL\n", 1)[0]
        empty_login_env = dict(os.environ, **{
            f"FINANCE_SHADOW_{role}_PASSWORD": ""
            for role in ("LOADER", "AGENT", "BACKUP")
        })
        docker(
            "exec", "-i",
            "--env", "FINANCE_SHADOW_LOADER_PASSWORD",
            "--env", "FINANCE_SHADOW_AGENT_PASSWORD",
            "--env", "FINANCE_SHADOW_BACKUP_PASSWORD",
            name, "psql", "-X", "-q", "-U", "postgres", "-d", "accepted_template",
            "-v", "ON_ERROR_STOP=1", "-v", "roles_only=true",
            input=provision_sql, env=empty_login_env,
        )
        assert docker(
            "exec", name, "psql", "-X", "-A", "-t", "-U", "postgres",
            "-d", "accepted_template", "-c",
            "SELECT count(*) FROM pg_roles WHERE rolname IN "
            "('finance_shadow_loader','finance_shadow_agent','finance_shadow_backup_login')",
        ) == "0"
        for migration in sorted((ROOT / "deploy" / "postgres" / "migrations").glob("*.sql")):
            text = migration.read_text(encoding="utf-8")
            checksum = hashlib.sha256(text.encode()).hexdigest()
            docker(
                "exec", "-i", name, "psql", "-X", "-q", "-U", "postgres",
                "-d", "accepted_template", "-v", "ON_ERROR_STOP=1",
                "-v", f"migration_version={migration.name.split('_', 1)[0]}",
                "-v", f"migration_name={migration.stem.split('_', 1)[1]}",
                "-v", f"migration_checksum={checksum}",
                "-v", "shadow_environment=synthetic-shadow-integration",
                input=text,
            )
        # Exercise the existing provisioning SQL too: it rebuilds grants after
        # restore and must not revoke INSERT on the new acceptance tables.
        login_env = dict(os.environ, **{
            f"FINANCE_SHADOW_{role}_PASSWORD": password
            for role in ("LOADER", "AGENT", "BACKUP")
        })
        docker(
            "exec", "-i",
            "--env", "FINANCE_SHADOW_LOADER_PASSWORD",
            "--env", "FINANCE_SHADOW_AGENT_PASSWORD",
            "--env", "FINANCE_SHADOW_BACKUP_PASSWORD",
            name, "psql", "-X", "-q", "-U", "postgres", "-d", "accepted_template",
            "-v", "ON_ERROR_STOP=1", "-v", "roles_only=false",
            input=provision_sql, env=login_env,
        )
        docker(
            "exec", "-i", name, "psql", "-X", "-q", "-U", "postgres",
            "-d", "accepted_template", "-v", "ON_ERROR_STOP=1",
            "--command", "CREATE DATABASE accepted_schema_verification TEMPLATE accepted_template",
        )
        docker(
            "exec", "-i", name, "psql", "-X", "-q", "-U", "postgres",
            "-d", "accepted_schema_verification", "-v", "ON_ERROR_STOP=1",
            input=(ROOT / "deploy" / "postgres" / "integration" / "schema_test.sql").read_text(),
        )
        docker(
            "exec", name, "psql", "-X", "-q", "-U", "postgres",
            "-d", "accepted_template", "-v", "ON_ERROR_STOP=1",
            "--command", "DROP DATABASE accepted_schema_verification",
        )
        settings = dict(host="127.0.0.1", port=port, user="postgres", password=password)
        with psycopg.connect(**settings, dbname="accepted_template") as connection:
            assert connection.execute(
                "SELECT count(*) FROM finance.schema_migrations"
            ).fetchone()[0] == len(list((ROOT / "deploy" / "postgres" / "migrations").glob("*.sql")))
        yield name, settings
    finally:
        if started:
            docker("rm", "--force", name)


@pytest.fixture(scope="module")
def database_server(database_runtime):
    return database_runtime[1]


@pytest.fixture
def database(database_server):
    import psycopg
    from psycopg import sql

    name = f"accepted_case_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(**database_server, dbname="postgres", autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {} TEMPLATE accepted_template").format(
            sql.Identifier(name)
        ))
        try:
            yield dict(database_server, dbname=name)
        finally:
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


@pytest.fixture
def connection(database):
    import psycopg

    with psycopg.connect(**database) as connection:
        connection.execute("SET LOCAL ROLE finance_shadow_ingest")
        yield connection


def accept(connection, resolution):
    return persist_accepted_identity_resolution(
        connection, resolution,
        expected_previous_generation_hash=accepted_current_generation_hash(connection),
    )


def counts(connection):
    return tuple(
        connection.execute(f"SELECT count(*) FROM finance.{table}").fetchone()[0]
        for table in (
            "canonical_identity_policy_generations", "accepted_identity_generations",
            "accepted_identity_events", "accepted_identity_claims",
            "accepted_identity_revisions", "accepted_identity_event_mappings",
            "application_projection_bindings", "accepted_identity_projection_links",
        )
    )


def current_by_id(connection):
    from psycopg.rows import dict_row

    with connection.cursor(row_factory=dict_row) as cursor:
        cursor.execute(
            "SELECT accepted_event_id, revision_number, signed_amount, currency_code, "
            "generation_hash, selection_generation_hash, selection_status, identity_status, "
            "disposition_generation_hash, disposition_status, disposition_event_ids, "
            "conflict_reasons, current_candidate_event_ids, target_activity_hash, "
            "source_admission_status, currency_evidence_status, is_economically_certified "
            "FROM finance_read.accepted_identity_current"
        )
        return {str(row["accepted_event_id"]): row for row in cursor.fetchall()}


@pytest.mark.parametrize("operation", ["diagnostic", "accepted", "projection"])
def test_identity_writes_honor_closed_migration_gate(database, connection, operation):
    import psycopg

    with psycopg.connect(**database) as admin:
        admin.execute(
            "UPDATE finance.writer_gate SET migrations_blocked = true, "
            "owner_token = 'synthetic-migration' WHERE singleton"
        )
    before = counts(connection)
    resolution = resolve_identity((observation(),))
    with pytest.raises(ValueError, match="blocked for migration"):
        if operation == "diagnostic":
            persist_identity_resolution(connection, resolution)
        elif operation == "accepted":
            persist_accepted_identity_resolution(
                connection, resolution, expected_previous_generation_hash=None
            )
        else:
            persist_application_projection_bindings(connection, resolution, {})
    assert counts(connection) == before


def test_identity_writes_cannot_pass_the_backup_global_lock(database):
    import psycopg
    from finance_store.postgres import GLOBAL_WRITER_LOCK

    with psycopg.connect(**database, autocommit=True) as backup:
        backup.execute(
            "SELECT pg_advisory_lock(hashtextextended(%s, 0))",
            (GLOBAL_WRITER_LOCK,),
        )
        try:
            with psycopg.connect(**database) as writer:
                writer.execute("SET LOCAL lock_timeout = '100ms'")
                with pytest.raises(psycopg.errors.LockNotAvailable):
                    persist_accepted_identity_resolution(
                        writer, resolve_identity((observation(),)),
                        expected_previous_generation_hash=None,
                    )
                writer.rollback()
                assert writer.execute(
                    "SELECT count(*) FROM finance.accepted_identity_generations"
                ).fetchone()[0] == 0
        finally:
            backup.execute(
                "SELECT pg_advisory_unlock(hashtextextended(%s, 0))",
                (GLOBAL_WRITER_LOCK,),
            )


def test_explicit_transaction_on_autocommit_connection_is_supported(database):
    import psycopg

    with psycopg.connect(**database, autocommit=True) as connection:
        with connection.transaction():
            connection.execute("SET LOCAL ROLE finance_shadow_ingest")
            result = persist_accepted_identity_resolution(
                connection, resolve_identity((observation(),)),
                expected_previous_generation_hash=None,
            )
            assert result.inserted
        assert connection.execute(
            "SELECT count(*) FROM finance.accepted_identity_events"
        ).fetchone()[0] == 1


@pytest.mark.parametrize("invalid_backup", [False, True])
def test_actual_migration_runner_releases_only_successful_owned_gates(
    database_runtime, database, invalid_backup
):
    import psycopg

    container = database_runtime[0]
    command = [
        "docker", "exec", "--env", "PGHOST=127.0.0.1",
        "--env", f"PGDATABASE={database['dbname']}", "--env", "PGUSER=postgres",
        "--env", "PGPASSWORD_FILE=/tmp/accepted-admin-password",
        "--env", "FINANCE_SHADOW_ENVIRONMENT=synthetic-shadow-integration",
    ]
    if invalid_backup:
        command.extend(["--env", "POSTGRES_MIGRATION_BACKUP=SYN-MISSING"])
    command.extend([container, "/bin/sh", "/scripts/migrate.sh"])
    result = subprocess.run(
        command, capture_output=True, text=True,
        env=dict(os.environ, PGPASSWORD=database["password"]),
    )
    with psycopg.connect(**database) as connection:
        gate = connection.execute(
            "SELECT migrations_blocked, owner_token FROM finance.writer_gate WHERE singleton"
        ).fetchone()
    if invalid_backup:
        assert result.returncode != 0
        assert "writer gate remains closed" in result.stderr
        assert gate[0] is True and gate[1].startswith("migration-")
    else:
        assert result.returncode == 0, result.stderr
        assert gate == (False, None)


def test_replay_reordering_sightings_growth_correction_and_current_history(connection):
    a = observation(group="SYN-GROUP")
    first = resolve_identity((a,))
    initial = accept(connection, first)
    stable = initial.mappings[0].accepted_event_id
    baseline = counts(connection)
    assert not accept(connection, first).inserted
    assert counts(connection) == baseline
    sighting = replace(
        a, observation_id="SYN-OBS-A-NEW", source_hash=content_hash("SYN-NEW"),
        observed_at=datetime(2026, 1, 17, tzinfo=timezone.utc),
    )
    second = resolve_identity((a, sighting))
    sighted = accept(connection, second)
    assert sighted.mappings[0].accepted_event_id == stable
    assert sighted.mappings[0].revision_number == 2
    baseline = counts(connection)
    assert not accept(connection, resolve_identity((sighting, a))).inserted
    assert counts(connection) == baseline
    b = observation("B", family="ofx", group="SYN-GROUP")
    grown = resolve_identity((a, sighting, b))
    added = accept(connection, grown)
    assert len(added.mappings) == 1
    assert added.mappings[0].accepted_event_id == stable
    assert added.mappings[0].revision_number == 3
    # Correction through the same claim, without cross-source economic guessing.
    corrected_b = replace(
        b, observation_id="SYN-CORRECTED", signed_amount=b.signed_amount - 1,
        status="reversed", observed_at=datetime(2026, 1, 18, tzinfo=timezone.utc),
        source_hash=content_hash("SYN-CORRECTION"),
    )
    correction = resolve_identity((b, corrected_b))
    revised = accept(connection, correction)
    assert revised.mappings[0].accepted_event_id == stable
    assert revised.mappings[0].revision_number == 4
    baseline = counts(connection)
    old = accept(connection, first)
    assert not old.inserted
    assert counts(connection) == baseline
    assert accepted_current_generation_hash(connection) == correction.generation_hash
    row = connection.execute(
        "SELECT accepted_event_id, revision_number, signed_amount, currency_code, status "
        "FROM finance_read.accepted_identity_current"
    ).fetchone()
    assert str(row[0]) == stable
    assert row[1:] == (4, corrected_b.signed_amount, "USD", "reversed")
    assert connection.execute(
        "SELECT count(DISTINCT generation_hash) FROM finance_read.accepted_identity_history"
    ).fetchone()[0] == 4


def test_local_merge_conflict_preserves_independent_event_and_bindings(connection):
    a, b = observation(), observation("B", family="ofx")
    first = resolve_identity((a, b))
    initial = accept(connection, first)
    targets = {event.canonical_event_id: content_hash(f"SYN-TARGET-{index}")
               for index, event in enumerate(first.canonical_events)}
    persist_application_projection_bindings(connection, first, targets)
    after = resolve_identity((
        replace(a, source_group_id="SYN-GROUP"),
        replace(b, source_group_id="SYN-GROUP"),
        observation("SAFE", amount="-79.01"),
    ))
    result = accept(connection, after)
    assert len(result.conflicts) == 1
    assert "many-existing-merge" in result.conflicts[0].conflict_reasons
    assert len(result.conflicts[0].projection_binding_ids) == 2
    assert set(result.conflicts[0].prior_event_ids) == {
        item.accepted_event_id for item in initial.mappings
    }
    assert len([item for item in result.mappings if item.outcome == "created"]) == 1
    assert dict(connection.execute(
        "SELECT selection_status, count(*) FROM finance_read.accepted_identity_current "
        "GROUP BY selection_status"
    ).fetchall()) == {"selected": 1, "retained-prior": 2}
    assert connection.execute(
        "SELECT count(*) FROM finance.accepted_identity_projection_links"
    ).fetchone()[0] == 2
    with pytest.raises(ValueError, match="unambiguous"):
        persist_application_projection_bindings(
            connection, after,
            {result.conflicts[0].canonical_event_id: next(iter(targets.values()))},
        )


def test_split_does_not_claim_either_child_and_replay_cannot_unblock(connection):
    a, b = observation(group="SYN-GROUP"), observation(
        "B", family="ofx", group="SYN-GROUP"
    )
    first = resolve_identity((a, b))
    original = accept(connection, first)
    split = resolve_identity((
        replace(a, source_group_id=""), replace(b, source_group_id=""),
        observation("SAFE", amount="-99.12"),
    ))
    result = accept(connection, split)
    assert len(result.conflicts) == 2
    assert all("one-existing-split" in item.conflict_reasons for item in result.conflicts)
    assert all(item.prior_event_ids == (original.mappings[0].accepted_event_id,)
               for item in result.conflicts)
    baseline = counts(connection)
    replay = accept(connection, split)
    assert len(replay.conflicts) == 2
    assert counts(connection) == baseline


def test_historical_binding_is_adopted_without_rewriting_then_reused(connection):
    a = observation(group="SYN-GROUP")
    historical = resolve_identity((a,))
    persist_identity_resolution(connection, historical)
    target = content_hash("SYN-KNOWN-TARGET")
    persist_application_projection_bindings(
        connection, historical, {historical.canonical_events[0].canonical_event_id: target},
    )
    old = connection.execute(
        "SELECT application_projection_binding_id, canonical_identity_event_id "
        "FROM finance.application_projection_bindings"
    ).fetchone()
    grown = resolve_identity((a, observation("B", family="ofx", group="SYN-GROUP")))
    adopted = accept(connection, grown)
    assert not adopted.conflicts
    persist_application_projection_bindings(
        connection, grown, {grown.canonical_events[0].canonical_event_id: target},
    )
    assert connection.execute(
        "SELECT application_projection_binding_id, canonical_identity_event_id "
        "FROM finance.application_projection_bindings"
    ).fetchall() == [old]
    with pytest.raises(ValueError, match="existing target"):
        persist_application_projection_bindings(
            connection, grown,
            {grown.canonical_events[0].canonical_event_id: content_hash("SYN-OTHER")},
        )
    assert str(connection.execute(
        "SELECT application_projection_binding_id FROM finance_read.accepted_identity_current"
    ).fetchone()[0]) == str(old[0])


def test_automatic_shared_provider_claim_growth_retains_identity_and_binding(connection):
    from tests.test_identity_shared_tokens import ofx_row, resolve, scope, simplefin_row

    declaration = scope(max_day_skew=1)
    first = resolve([ofx_row()], scopes=(declaration,))
    stable = accept(connection, first).mappings[0].accepted_event_id
    target = content_hash("SYN-SCOPED-TARGET")
    persist_application_projection_bindings(
        connection, first, {first.canonical_events[0].canonical_event_id: target},
    )
    grown = resolve([simplefin_row(), ofx_row()], scopes=(declaration,))
    assert len(grown.canonical_events) == 1
    assert first.canonical_events[0].canonical_event_id != grown.canonical_events[0].canonical_event_id
    result = accept(connection, grown)
    assert result.mappings[0].accepted_event_id == stable
    assert result.mappings[0].revision_number == 2
    persist_application_projection_bindings(
        connection, grown, {grown.canonical_events[0].canonical_event_id: target},
    )
    assert connection.execute(
        "SELECT count(*) FROM finance.application_projection_bindings"
    ).fetchone()[0] == 1


def test_policy_history_and_unaffected_event_revision_are_preserved(connection, monkeypatch):
    from finance_store.identity import DEFAULT_POLICY, resolve_identity as resolver

    a, b = observation(), observation("SAFE", amount="-89.12")
    first = resolver((a,))
    stable = accept(connection, first).mappings[0].accepted_event_id
    next_generation = resolver((a, b))
    result = accept(connection, next_generation)
    continued = next(item for item in result.mappings if item.accepted_event_id == stable)
    assert continued.revision_number == 1
    assert not continued.new_revision
    new_policy = replace(DEFAULT_POLICY, version="synthetic-accepted-policy-v2")
    revised = resolver((a, b), policy=new_policy)
    monkeypatch.setattr("finance_store.identity_postgres.DEFAULT_POLICY", new_policy)
    result = accept(connection, revised)
    assert next(item for item in result.mappings if item.accepted_event_id == stable).revision_number == 2
    assert {row[0] for row in connection.execute(
        "SELECT DISTINCT policy_hash FROM finance_read.accepted_identity_history"
    ).fetchall()} == {DEFAULT_POLICY.policy_hash, new_policy.policy_hash}
    assert dict(connection.execute(
        "SELECT policy_hash, policy_document FROM finance.canonical_identity_policies"
    ).fetchall()) == {
        first.policy.policy_hash: first.policy.document(),
        revised.policy.policy_hash: revised.policy.document(),
    }
    assert connection.execute(
        "SELECT count(*) FROM finance.canonical_identity_event_members "
        "WHERE member_type = 'source_claim'"
    ).fetchone()[0] == 5


def test_revised_policy_split_is_local_and_does_not_reassign_old_identity(connection, monkeypatch):
    from finance_store.identity import DEFAULT_POLICY, HumanOverride, resolve_identity as resolver

    a, b = observation(group="SYN-GROUP"), observation("B", family="ofx", group="SYN-GROUP")
    first = resolve_identity((a, b))
    original = accept(connection, first)
    claim_ids = tuple(sorted(claim.claim_id for claim in first.claims))
    revised_policy = replace(
        DEFAULT_POLICY, version=f"{DEFAULT_POLICY.version}-synthetic-revision",
    )
    preserve = HumanOverride(
        override_id="SYN-REVIEWED-PRESERVE", version=1, action="preserve-distinct",
        claim_ids=claim_ids, rationale_hash=content_hash("SYN-REVIEWED-PRESERVE"),
        decided_at=datetime(2026, 2, 2, tzinfo=timezone.utc),
    )
    revised = resolver(
        (a, b, observation("UNRELATED", amount="-93.01")),
        policy=revised_policy, overrides=(preserve,),
    )
    monkeypatch.setattr("finance_store.identity_postgres.DEFAULT_POLICY", revised_policy)
    accepted = accept(connection, revised)
    assert len(accepted.conflicts) == 2
    assert all("one-existing-split" in item.conflict_reasons for item in accepted.conflicts)
    assert all(item.prior_event_ids == (original.mappings[0].accepted_event_id,)
               for item in accepted.conflicts)
    assert len([item for item in accepted.mappings if item.outcome == "created"]) == 1
    assert {row[0] for row in connection.execute(
        "SELECT DISTINCT policy_hash FROM finance_read.accepted_identity_history"
    ).fetchall()} == {first.policy.policy_hash, revised.policy.policy_hash}
    assert connection.execute(
        "SELECT count(*) FROM finance.accepted_identity_revisions WHERE accepted_event_id = %s",
        (original.mappings[0].accepted_event_id,),
    ).fetchone()[0] == 1


@pytest.mark.parametrize("change", ["identity-version", "authority-version", "weakened-current-rules"])
def test_historical_or_unsupported_policy_cannot_bootstrap_current_acceptance(connection, change):
    from finance_store.identity import DEFAULT_POLICY, resolve_identity as resolver

    if change == "identity-version":
        policy = replace(DEFAULT_POLICY, version="synthetic-historical-identity")
    else:
        authority = replace(
            DEFAULT_POLICY.authority_policy,
            **({"version": "synthetic-historical-authority"}
               if change == "authority-version" else {"require_reconciled_source_counts": False}),
        )
        policy = replace(
            DEFAULT_POLICY,
            source_authority=replace(DEFAULT_POLICY.source_authority, policy=authority),
        )
    historical = resolver((observation(),), policy=policy)
    persist_identity_resolution(connection, historical)
    before = counts(connection)
    assert connection.execute(
        "SELECT count(*) FROM finance_read.accepted_identity_current"
    ).fetchone()[0] == 0
    with pytest.raises(ValueError, match="latest supported policy semantics"):
        accept(connection, historical)
    assert counts(connection) == before
    latest = resolver((observation(),))
    assert accept(connection, latest).inserted
    assert connection.execute(
        "SELECT DISTINCT generation_hash FROM finance_read.accepted_identity_current"
    ).fetchall() == [(latest.generation_hash,)]
    assert connection.execute(
        "SELECT count(*) FROM finance.canonical_identity_policy_generations"
    ).fetchone()[0] == 2


def test_current_policy_scope_changes_keep_both_hashes_without_changing_semantic_version(connection):
    from finance_store.identity import resolve_identity as resolver
    from tests.test_identity_source_authority import evidence, policy_with

    a = observation(family="ofx")
    first = resolver((a,))
    initial = accept(connection, first)
    policy = policy_with(evidence(
        family="ofx", strength="stable-provider-id", count=1,
        account=a.source_account_id, canonical_account=a.canonical_account_id,
        connection=a.source_connection_id,
    ))
    scoped = resolver((a,), policy=policy)
    assert scoped.policy.version == first.policy.version
    assert scoped.policy.policy_hash != first.policy.policy_hash
    result = accept(connection, scoped)
    assert not result.conflicts
    assert result.mappings[0].accepted_event_id == initial.mappings[0].accepted_event_id
    assert result.mappings[0].revision_number == 2
    assert dict(connection.execute(
        "SELECT policy_hash, policy_document FROM finance.canonical_identity_policies"
    ).fetchall()) == {
        first.policy.policy_hash: first.policy.document(),
        scoped.policy.policy_hash: scoped.policy.document(),
    }


def test_unregistered_historical_binding_split_is_not_bootstrapped(connection):
    a, b = observation(group="SYN-GROUP"), observation("B", family="ofx", group="SYN-GROUP")
    historical = resolve_identity((a, b))
    assert len(historical.canonical_events) == 1
    persist_identity_resolution(connection, historical)
    persist_application_projection_bindings(
        connection, historical,
        {historical.canonical_events[0].canonical_event_id: content_hash("SYN-OLD-TARGET")},
    )
    split = resolve_identity((replace(a, source_group_id=""), replace(b, source_group_id="")))
    result = accept(connection, split)
    assert len(result.conflicts) == 2
    assert all("projection-binding-split" in item.conflict_reasons for item in result.conflicts)
    assert all(len(item.projection_binding_ids) == 1 for item in result.conflicts)
    assert accept(connection, split).conflicts == result.conflicts
    assert connection.execute(
        "SELECT count(*) FROM finance.accepted_identity_projection_links"
    ).fetchone()[0] == 0
    assert connection.execute(
        "SELECT count(*) FROM finance.application_projection_bindings WHERE is_active"
    ).fetchone()[0] == 1


def test_legacy_projected_identity_cannot_be_absorbed_by_another_accepted_event(connection):
    a, b = observation(), observation("B", family="ofx")
    accept(connection, resolve_identity((a,)))
    historical = resolve_identity((b,))
    persist_identity_resolution(connection, historical)
    persist_application_projection_bindings(
        connection, historical,
        {historical.canonical_events[0].canonical_event_id: content_hash("SYN-OTHER-PROJECTED")},
    )
    merged = resolve_identity((
        replace(a, source_group_id="SYN-GROUP"), replace(b, source_group_id="SYN-GROUP"),
    ))
    result = accept(connection, merged)
    assert "historical-binding-identity-conflict" in result.conflicts[0].conflict_reasons


def test_account_owner_change_is_a_local_conflict(connection):
    a = observation()
    first = resolve_identity((a,))
    accept(connection, first)
    changed = resolve_identity((replace(a, canonical_account_id="SYN-OTHER-OWNER"),))
    result = accept(connection, changed)
    assert result.conflicts[0].conflict_reasons == ("account-ownership-change",)
    assert connection.execute(
        "SELECT count(*) FROM finance.accepted_identity_events"
    ).fetchone()[0] == 1


def test_unresolved_emissions_are_excluded_and_source_currency_certification_is_not_invented(connection):
    from finance_store.identity import resolve_identity as resolver

    a = observation()
    b = replace(observation("B", family="ofx"), description=a.description)
    safe = replace(observation("SAFE", amount="-84.01"), currency="EUR")
    resolved = resolver((a, b, safe))
    result = accept(connection, resolved)
    assert len(result.conflicts) == 2
    assert all(item.review_decision_hashes for item in result.conflicts)
    assert counts(connection)[2:5] == (1, 1, 1)
    assert connection.execute(
        "SELECT currency_code, source_admission_status, currency_evidence_status, "
        "is_economically_certified FROM finance_read.accepted_identity_current"
    ).fetchall() == [("EUR", "not-evaluated", "resolver-supplied-unverified", False)]
    assert accept(connection, resolved).conflicts == result.conflicts


def test_new_ambiguity_retains_prior_selected_amount_and_binding_with_review_flags(connection):
    from finance_store.identity import resolve_identity as resolver

    a = observation()
    first = resolver((a,))
    initial = accept(connection, first)
    target = content_hash("SYN-RETAINED-TARGET")
    persist_application_projection_bindings(
        connection, first, {first.canonical_events[0].canonical_event_id: target},
    )
    new_a = replace(
        a, observation_id="SYN-NEW-VERSION", signed_amount=a.signed_amount - 1,
        source_hash=content_hash("SYN-NEW-VERSION"),
        observed_at=datetime(2026, 1, 17, tzinfo=timezone.utc),
    )
    b = replace(
        observation("B", family="ofx"), description=a.description,
        signed_amount=new_a.signed_amount,
    )
    unresolved = resolver((a, new_a, b, observation("SAFE", amount="-85.01")))
    result = accept(connection, unresolved)
    assert len(result.conflicts) == 2
    row = connection.execute(
        "SELECT accepted_event_id, revision_number, signed_amount, generation_hash, "
        "selection_generation_hash, selection_status, identity_status, target_activity_hash "
        "FROM finance_read.accepted_identity_current WHERE selection_status = 'retained-prior'"
    ).fetchone()
    assert str(row[0]) == initial.mappings[0].accepted_event_id
    assert row[1:] == (
        1, a.signed_amount, first.generation_hash, unresolved.generation_hash,
        "retained-prior", "needs-review", target,
    )
    assert connection.execute(
        "SELECT count(*) FROM finance.accepted_identity_revisions WHERE accepted_event_id = %s",
        (initial.mappings[0].accepted_event_id,),
    ).fetchone()[0] == 1
    assert connection.execute(
        "SELECT count(*) FROM finance_read.accepted_identity_current WHERE selection_status = 'selected'"
    ).fetchone()[0] == 1
    affected = next(item for item in result.conflicts if item.prior_event_ids)
    with pytest.raises(ValueError, match="unambiguous"):
        persist_application_projection_bindings(
            connection, unresolved, {affected.canonical_event_id: target},
        )


@pytest.mark.parametrize("change", ["excluded", "unknown", "trust-cutoff"])
def test_source_exclusion_or_unknown_account_state_does_not_fall_back_to_trusted_prior(connection, change):
    from finance_store.identity import resolve_identity as resolver

    a = observation()
    accept(connection, resolver((a,)))
    changed = (
        replace(a, trust_cutoff_day=a.source_day.replace(day=1))
        if change == "trust-cutoff" else replace(a, account_status=change)
    )
    result = accept(connection, resolver((changed,)))
    assert len(result.conflicts) == 1
    expected = "account-state-unknown" if change == "unknown" else "source-excluded-or-untrusted"
    assert expected in result.conflicts[0].conflict_reasons
    assert connection.execute(
        "SELECT count(*) FROM finance_read.accepted_identity_current"
    ).fetchone()[0] == 0
    assert connection.execute(
        "SELECT count(*) FROM finance.accepted_identity_revisions"
    ).fetchone()[0] == 1


def test_resolver_selected_cross_account_mirror_retains_both_ownership_snapshots(connection):
    from finance_store.identity import resolve_identity as resolver

    original = observation(family="ofx")
    initial = accept(connection, resolver((original,)))
    mirror = replace(
        observation("MIRROR", family="simplefin"),
        canonical_account_id="SYN-MIRROR-ACCOUNT",
        source_account_id="SYN-MIRROR-SOURCE",
        description=original.description,
        attributes=(("provider_error_of", original.observation_id),),
    )
    resolved = resolver((original, mirror))
    assert len(resolved.canonical_events) == 1
    accepted = accept(connection, resolved)
    assert not accepted.conflicts
    assert accepted.mappings[0].accepted_event_id == initial.mappings[0].accepted_event_id
    assert connection.execute(
        "SELECT count(*) FROM finance.accepted_identity_claims "
        "WHERE source_canonical_account_hash <> canonical_account_hash"
    ).fetchone()[0] == 1
    moved = resolver((original, replace(mirror, canonical_account_id="SYN-MOVED-MIRROR")))
    assert "account-ownership-change" in accept(connection, moved).conflicts[0].conflict_reasons


def test_empty_selection_and_old_replay_do_not_delete_or_reselect_events(connection):
    first = resolve_identity((observation(),))
    original = accept(connection, first)
    empty = resolve_identity(())
    result = accept(connection, empty)
    assert result.mappings == ()
    accept(connection, first)
    assert accepted_current_generation_hash(connection) == empty.generation_hash
    rows = current_by_id(connection)
    assert set(rows) == {original.mappings[0].accepted_event_id}
    retained = rows[original.mappings[0].accepted_event_id]
    assert retained["selection_status"] == "not-observed-in-current-selection"
    assert retained["disposition_status"] == "qualified"
    assert retained["selection_generation_hash"] == empty.generation_hash
    assert retained["generation_hash"] == first.generation_hash


def test_bounded_source_omission_carries_accounts_through_empty_and_later_return(connection):
    a = observation()
    b = replace(
        observation("B", amount="-31.27"), source_account_id="SYN-SECOND-SOURCE",
        canonical_account_id="SYN-SECOND-CANONICAL",
    )
    first = resolve_identity((a, b))
    initial = accept(connection, first)
    ids = {item.canonical_event_id: item.accepted_event_id for item in initial.mappings}
    a_id = ids[first.observation_to_canonical[a.observation_id]]
    b_id = ids[first.observation_to_canonical[b.observation_id]]
    target = content_hash("SYN-OMITTED-SOURCE-TARGET")
    persist_application_projection_bindings(
        connection, first, {first.observation_to_canonical[b.observation_id]: target},
    )

    a_only = resolve_identity((a,))
    accept(connection, a_only)
    current = current_by_id(connection)
    assert set(current) == {a_id, b_id}
    assert current[a_id]["selection_status"] == "selected"
    assert current[b_id]["selection_status"] == "not-observed-in-current-selection"
    assert current[b_id]["signed_amount"] == b.signed_amount
    assert current[b_id]["disposition_generation_hash"] == first.generation_hash
    assert current[b_id]["current_candidate_event_ids"] == []
    assert current[b_id]["target_activity_hash"] == target

    empty = resolve_identity(())
    accept(connection, empty)
    current = current_by_id(connection)
    assert set(current) == {a_id, b_id}
    assert all(row["selection_status"] == "not-observed-in-current-selection"
               for row in current.values())
    assert sum(row["signed_amount"] for row in current.values()) == a.signed_amount + b.signed_amount
    assert all(row["revision_number"] == 1 for row in current.values())
    assert current[a_id]["disposition_generation_hash"] == a_only.generation_hash
    assert current[b_id]["disposition_generation_hash"] == first.generation_hash

    independent = resolve_identity((observation("C", amount="-44.19"),))
    safe = accept(connection, independent)
    c_id = safe.mappings[0].accepted_event_id
    current = current_by_id(connection)
    assert set(current) == {a_id, b_id, c_id}
    assert current[c_id]["selection_status"] == "selected"
    assert current[b_id]["signed_amount"] == b.signed_amount
    assert current[b_id]["target_activity_hash"] == target
    assert connection.execute(
        "SELECT count(*) FROM finance.accepted_identity_event_mappings"
    ).fetchone()[0] == 4  # Omission creates no artificial observation/mapping rows.

    returned = replace(
        b, observation_id="SYN-B-RETURNED", source_hash=content_hash("SYN-B-RETURNED"),
        observed_at=datetime(2026, 1, 18, tzinfo=timezone.utc),
    )
    returned_resolution = resolve_identity((b, returned))
    result = accept(connection, returned_resolution)
    assert result.mappings[0].accepted_event_id == b_id
    assert result.mappings[0].revision_number == 2
    current = current_by_id(connection)
    assert set(current) == {a_id, b_id, c_id}
    assert current[b_id]["selection_status"] == "selected"
    assert current[b_id]["target_activity_hash"] == target
    assert current[b_id]["signed_amount"] == b.signed_amount
    assert all(not row["is_economically_certified"] for row in current.values())
    assert all(row["source_admission_status"] == "not-evaluated" for row in current.values())


def test_ambiguous_disposition_survives_omission_and_does_not_become_qualified(connection):
    from finance_store.identity import resolve_identity as resolver

    a = observation()
    first = resolver((a,))
    stable = accept(connection, first).mappings[0].accepted_event_id
    b = replace(observation("B", family="ofx"), description=a.description)
    ambiguous = resolver((a, b))
    conflict = accept(connection, ambiguous)
    assert len(conflict.conflicts) == 2
    current = current_by_id(connection)[stable]
    assert current["selection_status"] == "retained-prior"
    assert current["identity_status"] == "needs-review"
    reasons = current["conflict_reasons"]

    independent = resolver((observation("SAFE", amount="-73.12"),))
    safe = accept(connection, independent).mappings[0].accepted_event_id
    current = current_by_id(connection)
    assert set(current) == {stable, safe}
    assert current[stable]["selection_status"] == "not-observed-in-current-selection"
    assert current[stable]["identity_status"] == "needs-review"
    assert current[stable]["disposition_status"] == "conflict"
    assert current[stable]["disposition_generation_hash"] == ambiguous.generation_hash
    assert current[stable]["conflict_reasons"] == reasons
    assert current[stable]["current_candidate_event_ids"] == []
    assert current[stable]["disposition_event_ids"]
    assert current[stable]["signed_amount"] == a.signed_amount
    assert current[safe]["selection_status"] == "selected"

    empty = resolver(())
    accept(connection, empty)
    current = current_by_id(connection)
    assert set(current) == {stable, safe}
    assert current[stable]["identity_status"] == "needs-review"
    assert current[stable]["conflict_reasons"] == reasons
    assert current[stable]["disposition_generation_hash"] == ambiguous.generation_hash
    assert current[stable]["selection_generation_hash"] == empty.generation_hash
    assert connection.execute(
        "SELECT count(*) FROM finance.accepted_identity_revisions WHERE accepted_event_id = %s",
        (stable,),
    ).fetchone()[0] == 1


@pytest.mark.parametrize("change", ["excluded", "trust-cutoff", "unknown-owner", "ownership-drift"])
def test_ineligible_disposition_is_not_resurrected_by_later_omission(connection, change):
    a = observation()
    b = replace(
        observation("PRESERVED", amount="-35.25"), source_account_id="SYN-INDEPENDENT-SOURCE",
        canonical_account_id="SYN-INDEPENDENT-CANONICAL",
    )
    first = resolve_identity((a, b))
    initial = accept(connection, first)
    ids = {item.canonical_event_id: item.accepted_event_id for item in initial.mappings}
    blocked_id = ids[first.observation_to_canonical[a.observation_id]]
    preserved_id = ids[first.observation_to_canonical[b.observation_id]]
    changes = {
        "excluded": {"account_status": "excluded"},
        "trust-cutoff": {"trust_cutoff_day": a.source_day.replace(day=1)},
        "unknown-owner": {"account_status": "unknown"},
        "ownership-drift": {"canonical_account_id": "SYN-DRIFTED-OWNER"},
    }
    blocked = resolve_identity((replace(a, **changes[change]),))
    result = accept(connection, blocked)
    assert len(result.conflicts) == 1
    assert result.conflicts[0].prior_event_ids == (blocked_id,)
    assert set(current_by_id(connection)) == {preserved_id}

    independent = resolve_identity((replace(
        observation("NEW-SAFE", amount="-67.23"),
        source_account_id="SYN-NEW-INDEPENDENT-SOURCE",
        canonical_account_id="SYN-NEW-INDEPENDENT-CANONICAL",
    ),))
    safe_id = accept(connection, independent).mappings[0].accepted_event_id
    current = current_by_id(connection)
    assert set(current) == {preserved_id, safe_id}
    assert current[safe_id]["selection_status"] == "selected"
    assert current[preserved_id]["selection_status"] == "not-observed-in-current-selection"
    assert current[preserved_id]["signed_amount"] == b.signed_amount

    empty = resolve_identity(())
    accept(connection, empty)
    current = current_by_id(connection)
    assert set(current) == {preserved_id, safe_id}
    assert blocked_id not in current
    assert all(row["selection_status"] == "not-observed-in-current-selection"
               for row in current.values())
    assert connection.execute(
        "SELECT count(*) FROM finance.accepted_identity_revisions WHERE accepted_event_id = %s",
        (blocked_id,),
    ).fetchone()[0] == 1
    assert connection.execute(
        "SELECT conflict_reasons FROM finance_read.accepted_identity_history "
        "WHERE generation_hash = %s",
        (blocked.generation_hash,),
    ).fetchone()[0] == list(result.conflicts[0].conflict_reasons)


def test_readonly_role_sees_sourced_current_fields_but_cannot_read_registry(connection):
    import psycopg

    resolved = resolve_identity((observation(),))
    accepted = accept(connection, resolved)
    connection.execute("SET LOCAL ROLE finance_readonly")
    row = connection.execute(
        "SELECT accepted_event_id, signed_amount, currency_code, description_hash "
        "FROM finance_read.accepted_identity_current"
    ).fetchone()
    assert str(row[0]) == accepted.mappings[0].accepted_event_id
    assert row[1:] == (
        resolved.canonical_events[0].signed_amount, "USD", content_hash("Synthetic merchant"),
    )
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with connection.transaction():
            connection.execute("SELECT * FROM finance.accepted_identity_claims")


def test_autocommit_cannot_partially_accept_a_generation(database):
    import psycopg

    with psycopg.connect(**database, autocommit=True) as connection:
        with pytest.raises(ValueError, match="caller-owned transaction"):
            accept(connection, resolve_identity((observation(),)))
        assert counts(connection) == (0,) * 8


def test_transaction_rollback_covers_generation_registry_and_projection(database):
    import psycopg

    resolution = resolve_identity((observation(),))
    with pytest.raises(RuntimeError, match="synthetic interruption"):
        with psycopg.connect(**database) as connection:
            connection.execute("SET LOCAL ROLE finance_shadow_ingest")
            accept(connection, resolution)
            persist_application_projection_bindings(
                connection, resolution,
                {resolution.canonical_events[0].canonical_event_id: content_hash("SYN-TARGET")},
            )
            raise RuntimeError("synthetic interruption")
    with psycopg.connect(**database) as connection:
        assert counts(connection) == (0,) * 8


def test_concurrent_acceptance_serializes_and_stale_predecessor_is_rejected(database):
    import psycopg

    first = resolve_identity((observation(),))

    def write(resolution, predecessor):
        with psycopg.connect(**database) as connection:
            connection.execute("SET LOCAL ROLE finance_shadow_ingest")
            return persist_accepted_identity_resolution(
                connection, resolution, expected_previous_generation_hash=predecessor,
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: write(first, None), range(2)))
    assert sorted(result.inserted for result in results) == [False, True]
    after = resolve_identity((observation(), observation("SAFE", amount="-87.21")))
    with pytest.raises(ValueError, match="predecessor changed"):
        write(after, None)
    with psycopg.connect(**database) as connection:
        assert counts(connection)[:6] == (1, 1, 1, 1, 1, 1)
    with ThreadPoolExecutor(max_workers=2) as pool:
        competing = (
            after,
            resolve_identity((observation(), observation("OTHER", amount="-17.21"))),
        )
        futures = [pool.submit(write, item, first.generation_hash) for item in competing]
        successes, conflicts = 0, 0
        for future in futures:
            try:
                assert future.result().inserted
                successes += 1
            except ValueError as error:
                assert "predecessor changed" in str(error)
                conflicts += 1
        assert (successes, conflicts) == (1, 1)


def test_database_guards_history_claim_ownership_and_projection_uniqueness(connection):
    import psycopg

    resolution = resolve_identity((observation(), observation("B", amount="-33.21")))
    accepted = accept(connection, resolution)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with connection.transaction():
            connection.execute(
                "UPDATE finance.accepted_identity_events SET accepted_event_id = accepted_event_id"
            )
    with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState):
        with connection.transaction():
            connection.execute("RESET ROLE")
            connection.execute(
                "UPDATE finance.accepted_identity_events SET accepted_event_id = accepted_event_id"
            )
    with pytest.raises(psycopg.errors.UniqueViolation):
        with connection.transaction():
            connection.execute(
                "INSERT INTO finance.accepted_identity_claims SELECT * "
                "FROM finance.accepted_identity_claims LIMIT 1"
            )
    target = content_hash("SYN-ONE-TARGET")
    event = resolution.canonical_events[0]
    persist_application_projection_bindings(connection, resolution, {event.canonical_event_id: target})
    other = next(item for item in accepted.mappings if item.canonical_event_id != event.canonical_event_id)
    with pytest.raises((psycopg.errors.CheckViolation, psycopg.errors.UniqueViolation)):
        with connection.transaction():
            connection.execute(
                """
                INSERT INTO finance.accepted_identity_projection_links
                SELECT %s, target_application, target_activity_hash, application_projection_binding_id
                FROM finance.accepted_identity_projection_links
                """,
                (other.accepted_event_id,),
            )
