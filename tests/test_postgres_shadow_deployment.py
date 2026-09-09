import json
import os
from pathlib import Path

import psycopg
import pytest

import finance_store.cli as shadow_cli
from finance_store.domain import content_hash
from finance_store.shadow import (
    MUTATION_INTERLOCK_VALUE,
    ShadowSafetyError,
    _require_apply_interlocks,
    load_plan,
    scheduled_run_lock,
)
from importers.rebuild.safety import plan_fingerprint


ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy" / "postgres"


def text(relative: str) -> str:
    return (DEPLOY / relative).read_text(encoding="utf-8")


def test_compose_projects_are_digest_pinned_private_and_disjoint():
    production = text("compose.yml")
    integration = text("compose.integration.yml")
    rehearsal = text("compose.rehearsal.yml")
    tls = text("compose.tls.yml")
    hba = text("tls/pg_hba.conf")

    assert production.startswith(
        "name: personal-finance-postgres-shadow-authority\n"
    )
    assert integration.startswith(
        "name: personal-finance-postgres-shadow-authority-integration\n"
    )
    assert rehearsal.startswith(
        "name: personal-finance-postgres-shadow-authority-restore-rehearsal\n"
    )
    assert "127.0.0.1" in production
    assert "127.0.0.1" in integration
    assert "POSTGRES_PASSWORD_FILE:" in production
    assert "POSTGRES_PASSWORD:" not in production
    database_service = production.split("  migrate:", 1)[0]
    assert "/backups" not in database_service
    assert "finance_shadow_backup_login" in production
    assert (
        "personal-finance-postgres-shadow-authority-data" in production
    )
    digest = (
        "postgres:17.11-bookworm@sha256:"
        "051f7b7b3abdd564d5d1bd1e8c4b9c1b6e77087d1dd22020ede611c096a272e0"
    )
    assert digest in production
    assert digest in integration
    assert digest in rehearsal
    assert "hba_file=/run/postgres-tls/pg_hba.conf" in tls
    assert "install -o postgres -g postgres -m 0600" in tls
    assert "hostnossl all all 0.0.0.0/0 reject" in hba
    assert "hostssl all all 0.0.0.0/0 scram-sha-256" in hba


def test_migrations_backups_and_scheduler_fail_closed():
    migration = text("scripts/migrate.sh")
    backup_role_migration = text(
        "migrations/0010_backup_role_scope.sql"
    )
    backup = text("scripts/backup.sh")
    backup_state = text("scripts/backup-state.sh")
    restore = text("scripts/restore-rehearsal.sh")
    scheduler = text("install-shadow-task.ps1")

    assert "pg_advisory_lock" in migration
    assert "finance-shadow-authority-writer" in migration
    assert "finance.writer_gate" in migration
    assert "POSTGRES_MIGRATION_BACKUP" in migration
    assert "WHERE singleton AND migrations_blocked" in migration
    assert "owner_token = CASE WHEN '$gate_owner_supported' = 't'" in migration
    assert "THEN '$gate_token' ELSE 'legacy-migration-bootstrap' END" in migration
    assert "migration no longer owns the writer gate" in migration
    assert "writer gate remains closed for recovery" in migration
    assert "trap cleanup EXIT HUP INT TERM" in migration
    assert "gate_armed=false" in migration
    assert "differs from the immutable migration file" in migration
    assert "legacy_crlf_checksum" in migration
    assert "applied_checksum IS NOT DISTINCT FROM legacy_crlf_checksum" in migration
    assert "AND backup_is_verified" in migration
    assert "SET checksum = requested_checksum" in migration
    assert "pg_dump" in backup and "sha256sum" in backup
    assert "--schema=finance" in backup
    assert "--schema=finance_read" in backup
    assert "finance.backup_state_manifest()" in backup_state
    assert "CREATE ROLE finance_shadow_backup" in backup_role_migration
    assert "REVOKE pg_read_all_data" in backup_role_migration
    assert "REVOKE pg_read_all_data" in text("scripts/provision-logins.sh")
    assert "pg_restore" in restore and "verify-backup.sh" in restore
    assert restore.index("FINANCE_SHADOW_ROLES_ONLY=true") < restore.index(
        "POSTGRES_MIGRATION_BACKUP="
    )
    assert "Restored migration did not release its writer gate" in restore
    assert "SupportsShouldProcess" in scheduler
    assert "MultipleInstances IgnoreNew" in scheduler
    assert "Register-ScheduledTask" in scheduler


def test_shadow_loader_has_no_collector_or_wealthfolio_mutation_client():
    sources = (ROOT / "finance_store" / "sources.py").read_text(encoding="utf-8")
    shadow = (ROOT / "finance_store" / "shadow.py").read_text(encoding="utf-8")
    combined = sources + shadow

    assert "fetch_snapshot" not in combined
    assert "claim_access_url" not in combined
    assert "WealthfolioClient" not in combined
    assert "wealthfolio_client" not in combined


def test_apply_requires_both_exact_interlocks(tmp_path, monkeypatch):
    plan = {
        "environmentMarker": "synthetic-shadow",
        "dataRootHash": content_hash(
            os.path.normcase(str(tmp_path.resolve()))
        ),
        "ready": True,
    }
    with pytest.raises(ShadowSafetyError, match="FINANCE_SHADOW_ENVIRONMENT"):
        _require_apply_interlocks(plan, tmp_path)
    monkeypatch.setenv("FINANCE_SHADOW_ENVIRONMENT", "synthetic-shadow")
    monkeypatch.setenv("FINANCE_SHADOW_MUTATIONS_ENABLED", "true")
    with pytest.raises(
        ShadowSafetyError, match="FINANCE_SHADOW_MUTATIONS_ENABLED"
    ):
        _require_apply_interlocks(plan, tmp_path)
    monkeypatch.setenv(
        "FINANCE_SHADOW_MUTATIONS_ENABLED", MUTATION_INTERLOCK_VALUE
    )
    _require_apply_interlocks(plan, tmp_path)


def test_plan_hash_and_data_root_are_bound(tmp_path):
    root = tmp_path / "private"
    path = root / "postgres-shadow" / "plans" / "plan.json"
    path.parent.mkdir(parents=True)
    body = {
        "schemaVersion": 1,
        "mode": "postgresql-shadow-plan",
        "ready": True,
        "dataRootHash": content_hash(os.path.normcase(str(root.resolve()))),
    }
    plan = {**body, "planHash": plan_fingerprint(body)}
    path.write_text(json.dumps(plan), encoding="utf-8")

    assert load_plan(
        root, path.relative_to(root), plan["planHash"]
    ) == plan
    with pytest.raises(ShadowSafetyError, match="plan hash"):
        load_plan(root, path.relative_to(root), "0" * 64)


def test_scheduled_lock_rejects_overlap(tmp_path):
    with scheduled_run_lock(tmp_path):
        with pytest.raises(ShadowSafetyError, match="another shadow"):
            with scheduled_run_lock(tmp_path):
                pass


def test_cli_hides_postgres_error_details(tmp_path, monkeypatch):
    monkeypatch.setattr(
        shadow_cli,
        "read_dsn_file",
        lambda *_args, **_kwargs: "postgresql://synthetic",
    )

    def reject_apply(*_args, **_kwargs):
        raise psycopg.errors.CheckViolation(
            "synthetic private database detail"
        )

    monkeypatch.setattr(shadow_cli, "apply_plan", reject_apply)
    with pytest.raises(SystemExit) as error:
        shadow_cli.main(
            [
                "--data-dir",
                str(tmp_path),
                "apply",
                "--plan",
                "postgres-shadow/plans/synthetic.json",
                "--plan-hash",
                "f" * 64,
            ]
        )
    assert str(error.value) == (
        "shadow operation refused: PostgreSQL rejected the request"
    )
    assert "private database detail" not in str(error.value)
