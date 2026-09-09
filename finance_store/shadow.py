"""Sealed plan/apply and operations for the isolated PostgreSQL shadow authority."""

from __future__ import annotations

from builtins import ExceptionGroup
import hashlib
import json
import os
import re
import tempfile
from collections import Counter
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from importers.analytics.publication import ensure_durable_directory, fsync_directory
from importers.rebuild.safety import plan_fingerprint, validate_private_output

from . import canonical_identity
from .domain import FinanceState, content_hash, stable_id, utc
from .identity_postgres import persist_identity_resolution
from .memory import MemoryRepository
from .postgres import PostgresRepository, _psycopg
from .reconcile import build_plan
from .replay import export_state, state_digest
from .sources import (
    REPOSITORY_ROOT,
    SHADOW_OUTPUT,
    LineageDecision,
    SourceCatalog,
    load_source_catalog,
)

PLAN_SCHEMA_VERSION = 1
ENVIRONMENT_VARIABLE = "FINANCE_SHADOW_ENVIRONMENT"
MUTATION_INTERLOCK_VARIABLE = "FINANCE_SHADOW_MUTATIONS_ENABLED"
MUTATION_INTERLOCK_VALUE = "apply-reviewed-plan"
DEFAULT_ENVIRONMENT = "shadow-authority-v1"
_BASENAME = re.compile(r"^[A-Za-z0-9._-]+$")


class ShadowSafetyError(RuntimeError):
    """A shadow operation failed a safety or evidence precondition."""


@dataclass(frozen=True, slots=True)
class DatabaseStatus:
    instance_id: str
    environment_marker: str
    database_fingerprint: str
    migration_set_hash: str
    migration_count: int
    source_blob_count: int
    observation_count: int
    open_issue_count: int
    last_success_at: datetime | None
    last_failure_at: datetime | None
    current_user: str


def _migration_manifest() -> list[dict[str, str]]:
    migrations = REPOSITORY_ROOT / "deploy" / "postgres" / "migrations"
    output = []
    for path in sorted(migrations.glob("*.sql")):
        version, separator, raw_name = path.stem.partition("_")
        if not separator or not version.isdigit():
            raise ShadowSafetyError("invalid migration filename")
        output.append(
            {
                "version": version,
                "name": raw_name,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
    if not output:
        raise ShadowSafetyError("no migrations found")
    return output


def migration_set_hash() -> str:
    return content_hash(_migration_manifest())


def read_dsn_file(data_dir: Path, relative_path: Path) -> str:
    if relative_path.is_absolute():
        raise ShadowSafetyError("--dsn-file must be relative to --data-dir")
    path = (data_dir / relative_path).resolve(strict=True)
    validate_private_output(path, data_dir, REPOSITORY_ROOT)
    if not path.is_file() or path.is_symlink():
        raise ShadowSafetyError("DSN secret must be a regular, non-symlink file")
    value = path.read_text(encoding="utf-8").strip()
    if not value:
        raise ShadowSafetyError("DSN secret is empty")
    return value


def verify_database(dsn: str, expected_environment: str) -> DatabaseStatus:
    psycopg, dict_row = _psycopg()
    expected_migrations = _migration_manifest()
    with psycopg.connect(dsn, row_factory=dict_row) as connection:
        with connection.transaction():
            connection.execute(
                "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
            )
            metadata = connection.execute(
                """
                SELECT instance_id, environment_marker, authority_mode,
                       wealthfolio_mutation_enabled, cutover_authorized
                FROM finance.shadow_authority_metadata
                WHERE singleton
                """
            ).fetchone()
            if metadata is None:
                raise ShadowSafetyError("shadow authority marker is missing")
            if (
                metadata["environment_marker"] != expected_environment
                or metadata["authority_mode"] != "shadow"
                or metadata["wealthfolio_mutation_enabled"]
                or metadata["cutover_authorized"]
            ):
                raise ShadowSafetyError("database is not the expected shadow authority")
            role = connection.execute(
                """
                SELECT current_user AS role_name, rolsuper, rolcreatedb,
                       rolcreaterole, rolreplication,
                       pg_has_role(current_user, 'finance_shadow_ingest', 'member')
                           AS is_ingest_member
                FROM pg_roles
                WHERE rolname = current_user
                """
            ).fetchone()
            if (
                role is None
                or role["rolsuper"]
                or role["rolcreatedb"]
                or role["rolcreaterole"]
                or role["rolreplication"]
                or not role["is_ingest_member"]
            ):
                raise ShadowSafetyError(
                    "connection must use the least-privilege shadow ingest role"
                )
            applied = connection.execute(
                """
                SELECT version, name, checksum
                FROM finance.schema_migrations
                ORDER BY version::integer
                """
            ).fetchall()
            actual_migrations = [
                {
                    "version": row["version"],
                    "name": row["name"],
                    "sha256": row["checksum"],
                }
                for row in applied
            ]
            if actual_migrations != expected_migrations:
                raise ShadowSafetyError(
                    "database migrations do not exactly match the repository"
                )
            missing_protection = connection.execute(
                """
                SELECT count(*) AS count
                FROM (
                    VALUES
                        ('source_blobs'),
                        ('ingestion_runs'),
                        ('transaction_observations'),
                        ('balance_observations'),
                        ('position_observations'),
                        ('valuation_observations'),
                        ('artifact_observations'),
                        ('durable_decisions'),
                        ('audit_events'),
                        ('lineage_review_groups'),
                        ('lineage_review_group_members'),
                        ('lineage_review_decisions'),
                        ('shadow_plans'),
                        ('shadow_run_events')
                ) AS required(table_name)
                WHERE NOT EXISTS (
                    SELECT 1
                    FROM pg_trigger trigger_record
                    JOIN pg_class table_record
                      ON table_record.oid = trigger_record.tgrelid
                    JOIN pg_namespace namespace_record
                      ON namespace_record.oid = table_record.relnamespace
                    WHERE namespace_record.nspname = 'finance'
                      AND table_record.relname = required.table_name
                      AND NOT trigger_record.tgisinternal
                      AND (trigger_record.tgtype & 8) = 8
                )
                """
            ).fetchone()["count"]
            if missing_protection:
                raise ShadowSafetyError("immutable evidence protection is incomplete")
            status = connection.execute(
                "SELECT * FROM finance_read.shadow_status"
            ).fetchone()
            fingerprint = content_hash(
                {
                    "instanceId": str(metadata["instance_id"]),
                    "environment": metadata["environment_marker"],
                    "migrationSetHash": migration_set_hash(),
                }
            )
            return DatabaseStatus(
                instance_id=str(metadata["instance_id"]),
                environment_marker=metadata["environment_marker"],
                database_fingerprint=fingerprint,
                migration_set_hash=migration_set_hash(),
                migration_count=status["migration_count"],
                source_blob_count=status["source_blob_count"],
                observation_count=status["observation_count"],
                open_issue_count=status["open_issue_count"],
                last_success_at=status["last_success_at"],
                last_failure_at=status["last_failure_at"],
                current_user=role["role_name"],
            )


def _simulate(start: FinanceState, catalog: SourceCatalog) -> FinanceState:
    repository = MemoryRepository(start)
    for batch in catalog.batches:
        with repository.unit_of_work(
            f"simulate:{batch.connection.source_system}:"
            f"{batch.connection.connection_key}"
        ) as unit:
            state = unit.state()
            reconciliation = build_plan(
                state, batch, projection_target=None
            )
            unit.apply(batch, reconciliation)
    return repository.state()


def _safe_counts(
    start: FinanceState,
    end: FinanceState,
    catalog: SourceCatalog,
) -> dict[str, int]:
    start_observations = (
        len(start.transaction_observations)
        + len(start.balance_observations)
        + len(start.position_observations)
        + len(start.valuation_observations)
        + len(start.artifact_observations)
    )
    end_observations = (
        len(end.transaction_observations)
        + len(end.balance_observations)
        + len(end.position_observations)
        + len(end.valuation_observations)
        + len(end.artifact_observations)
    )
    return {
        "sourceFiles": len(catalog.files),
        "sourceBlobs": len(catalog.batches),
        "observations": catalog.observation_count,
        "newObservations": end_observations - start_observations,
        "transactionObservations": len(end.transaction_observations),
        "artifactObservations": len(end.artifact_observations),
        "canonicalTransactions": len(end.canonical_transactions),
        "openQualityIssues": sum(issue.status == "open" for issue in end.issues),
        "lineageGroups": len(catalog.lineage_groups),
        "lineageDecisions": len(catalog.lineage_decisions),
        "lineageQueueGroups": catalog.lineage_review_counts.get("queue-groups", 0),
        "lineageReviewedDecisions": catalog.lineage_review_counts.get(
            "reviewed-decisions", 0
        ),
        "lineageReadyGroups": catalog.lineage_readiness_counts.get(
            "ready-groups", 0
        ),
        "lineageRestoreEligibleGroups": catalog.lineage_readiness_counts.get(
            "restore-eligible-groups", 0
        ),
        "lineageSurgicalEligibleGroups": catalog.lineage_readiness_counts.get(
            "surgical-eligible-groups", 0
        ),
        "lineageRebuildEligibleGroups": catalog.lineage_readiness_counts.get(
            "rebuild-eligible-groups", 0
        ),
        "blockers": len(catalog.blockers),
        "gaps": len(catalog.gaps),
    }


def _plan_body(
    data_dir: Path,
    status: DatabaseStatus,
    generated_at: datetime,
    start: FinanceState,
    end: FinanceState,
    catalog: SourceCatalog,
    identity: dict[str, Any] | None,
    identity_blockers: list[str],
) -> dict[str, Any]:
    files = [asdict(item) for item in catalog.files]
    input_set_hash = content_hash(files)
    blockers = list(catalog.blockers) + identity_blockers
    counts = _safe_counts(start, end, catalog)
    counts["blockers"] = len(blockers)
    if identity is not None:
        counts["identityCanonicalEvents"] = identity["canonicalEvents"]
        counts["identityDecisions"] = identity["decisions"]
        counts["identityAuthorityIntervals"] = identity["authorityIntervals"]
    return {
        "schemaVersion": PLAN_SCHEMA_VERSION,
        "mode": "postgresql-shadow-plan",
        "ready": not blockers,
        "generatedAt": generated_at.isoformat(),
        "environmentMarker": status.environment_marker,
        "databaseFingerprint": status.database_fingerprint,
        "dataRootHash": content_hash(os.path.normcase(str(data_dir.resolve()))),
        "migrations": _migration_manifest(),
        "migrationSetHash": status.migration_set_hash,
        "startingStateHash": state_digest(start),
        "expectedStateHash": state_digest(end),
        "inputFiles": files,
        "inputSetHash": input_set_hash,
        "sourceCounts": catalog.source_counts,
        "identity": identity,
        "counts": counts,
        "blockers": blockers,
        "gaps": list(catalog.gaps),
        "policies": {
            "authorityMode": "shadow",
            "wealthfolioMutation": "prohibited",
            "sourceMutation": "prohibited",
            "fuzzyMerge": "prohibited-without-evidence-bound-decision",
            "vanishedPosted": "retain-and-open-quality-issue",
        },
    }


def _write_json(path: Path, value: dict[str, Any]) -> None:
    content = (
        json.dumps(value, indent=2, sort_keys=True, ensure_ascii=True) + "\n"
    ).encode("utf-8")
    ensure_durable_directory(path.parent, fsync_directory)
    if path.exists():
        if path.read_bytes() != content:
            raise ShadowSafetyError("existing content-addressed report differs")
        return
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def create_plan(
    data_dir: Path,
    dsn: str,
    *,
    environment: str,
    generated_at: datetime | None = None,
) -> tuple[dict[str, Any], Path]:
    generated_at = utc(generated_at or datetime.now(timezone.utc))
    status = verify_database(dsn, environment)
    catalog = load_source_catalog(data_dir, generated_at=generated_at)
    start = PostgresRepository(dsn).state()
    end = _simulate(start, catalog)
    identity, identity_blockers = canonical_identity.plan_binding(data_dir)
    body = _plan_body(
        data_dir,
        status,
        generated_at,
        start,
        end,
        catalog,
        identity,
        identity_blockers,
    )
    plan_hash = plan_fingerprint(body)
    plan = {**body, "planHash": plan_hash}
    output = validate_private_output(
        data_dir / SHADOW_OUTPUT / "plans" / f"{plan_hash}.json",
        data_dir,
        REPOSITORY_ROOT,
    )
    _write_json(output, plan)
    return plan, output


def load_plan(
    data_dir: Path,
    relative_path: Path,
    supplied_hash: str,
) -> dict[str, Any]:
    if relative_path.is_absolute():
        raise ShadowSafetyError("--plan must be relative to --data-dir")
    path = (data_dir / relative_path).resolve(strict=True)
    validate_private_output(path, data_dir, REPOSITORY_ROOT)
    try:
        plan = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ShadowSafetyError("sealed plan is missing or invalid") from exc
    actual_hash = plan_fingerprint(
        {key: value for key, value in plan.items() if key != "planHash"}
    )
    if (
        plan.get("schemaVersion") != PLAN_SCHEMA_VERSION
        or plan.get("mode") != "postgresql-shadow-plan"
        or plan.get("planHash") != actual_hash
        or supplied_hash != actual_hash
    ):
        raise ShadowSafetyError("operator plan hash does not match the sealed plan")
    return plan


def _require_apply_interlocks(plan: dict[str, Any], data_dir: Path) -> None:
    if os.environ.get(ENVIRONMENT_VARIABLE) != plan["environmentMarker"]:
        raise ShadowSafetyError(
            f"{ENVIRONMENT_VARIABLE} must exactly match the sealed environment"
        )
    if os.environ.get(MUTATION_INTERLOCK_VARIABLE) != MUTATION_INTERLOCK_VALUE:
        raise ShadowSafetyError(
            f"{MUTATION_INTERLOCK_VARIABLE} must be exactly "
            f"{MUTATION_INTERLOCK_VALUE!r}"
        )
    if plan["dataRootHash"] != content_hash(
        os.path.normcase(str(data_dir.resolve()))
    ):
        raise ShadowSafetyError("sealed plan belongs to a different external data root")
    if not plan.get("ready"):
        raise ShadowSafetyError("sealed plan has blockers")


def verify_backup(
    data_dir: Path,
    backup_basename: str | None,
    expected_observation_count: int,
    *,
    dsn: str | None = None,
) -> dict[str, Any] | None:
    if expected_observation_count == 0:
        return None
    if not backup_basename or not _BASENAME.fullmatch(backup_basename):
        raise ShadowSafetyError(
            "a verified backup basename is required before a non-empty apply"
        )
    root = data_dir / SHADOW_OUTPUT / "backups"
    dump = root / f"{backup_basename}.dump"
    digest_path = root / f"{backup_basename}.sha256"
    manifest_path = root / f"{backup_basename}.json"
    for path in (dump, digest_path, manifest_path):
        validate_private_output(path, data_dir, REPOSITORY_ROOT)
        if not path.is_file() or path.is_symlink():
            raise ShadowSafetyError("verified backup set is incomplete")
    digest = hashlib.sha256(dump.read_bytes()).hexdigest()
    expected_digest = digest_path.read_text(encoding="utf-8").split()[0]
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ShadowSafetyError("backup manifest is invalid") from exc
    if (
        digest != expected_digest
        or manifest.get("dumpSha256") != digest
        or manifest.get("observationCount") != expected_observation_count
    ):
        raise ShadowSafetyError("backup does not bind the current shadow state")
    if dsn is not None:
        psycopg, dict_row = _psycopg()
        with psycopg.connect(dsn, row_factory=dict_row) as connection:
            state_material = connection.execute(
                "SELECT finance.backup_state_manifest()::text AS state"
            ).fetchone()["state"]
        state_sha = hashlib.sha256(state_material.encode("utf-8")).hexdigest()
        if manifest.get("stateSha256") != state_sha:
            raise ShadowSafetyError(
                "backup does not bind the exact current database state"
            )
    return {
        "basenameHash": content_hash(backup_basename),
        "dumpSha256": digest,
        "observationCount": expected_observation_count,
    }


def _insert_plan_record(unit: Any, plan: dict[str, Any]) -> None:
    counts = plan["counts"]
    unit.connection.execute(
        """
        INSERT INTO finance.shadow_plans (
            plan_hash, input_set_hash, migration_set_hash, starting_state_hash,
            expected_state_hash, environment_marker, source_count,
            observation_count, issue_count, created_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (plan_hash) DO NOTHING
        """,
        (
            plan["planHash"],
            plan["inputSetHash"],
            plan["migrationSetHash"],
            plan["startingStateHash"],
            plan["expectedStateHash"],
            plan["environmentMarker"],
            counts["sourceFiles"],
            counts["observations"],
            counts["openQualityIssues"],
            datetime.fromisoformat(plan["generatedAt"]),
        ),
    )


def _insert_shadow_control(
    unit: Any,
    plan: dict[str, Any],
    catalog: SourceCatalog,
    identity: dict[str, Any] | None = None,
) -> None:
    counts = plan["counts"]
    if identity is not None:
        counts = {
            **counts,
            "identityGenerationNumber": identity["generationNumber"],
            "identityPersistedClaims": identity["claims"],
            "identityPersistedEvents": identity["canonicalEvents"],
            "identityPersistedDecisions": identity["decisions"],
        }
    _insert_plan_record(unit, plan)
    for group in catalog.lineage_groups:
        unit.connection.execute(
            """
            INSERT INTO finance.lineage_review_groups (
                lineage_group_id, candidate_hash, audit_graph_hash,
                evidence_set_hash, member_count, observed_at, source_blob_id,
                quality_issue_id
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (lineage_group_id) DO NOTHING
            """,
            (
                group.group_id,
                group.candidate_hash,
                group.audit_graph_hash,
                group.evidence_set_hash,
                group.member_count,
                group.observed_at,
                group.source_blob_id,
                group.quality_issue_id,
            ),
        )
        persisted_group = unit.connection.execute(
            """
            SELECT candidate_hash, audit_graph_hash, evidence_set_hash,
                   member_count, source_blob_id, quality_issue_id
            FROM finance.lineage_review_groups
            WHERE lineage_group_id = %s
            """,
            (group.group_id,),
        ).fetchone()
        if (
            persisted_group is None
            or persisted_group["candidate_hash"] != group.candidate_hash
            or persisted_group["audit_graph_hash"] != group.audit_graph_hash
            or persisted_group["evidence_set_hash"] != group.evidence_set_hash
            or persisted_group["member_count"] != group.member_count
            or str(persisted_group["source_blob_id"]) != group.source_blob_id
            or str(persisted_group["quality_issue_id"])
            != group.quality_issue_id
        ):
            raise ShadowSafetyError(
                "persisted lineage group differs from sealed evidence"
            )
        persisted_members = {
            row["member_identity_hash"]
            for row in unit.connection.execute(
                """
                SELECT member_identity_hash
                FROM finance.lineage_review_group_members
                WHERE lineage_group_id = %s
                """,
                (group.group_id,),
            ).fetchall()
        }
        incoming_members = set(group.member_identity_hashes)
        if (
            len(incoming_members) != group.member_count
            or len(group.member_identity_hashes) != group.member_count
        ):
            raise ShadowSafetyError(
                "sealed lineage group membership is incomplete"
            )
        group_is_decided = unit.connection.execute(
            """
            SELECT EXISTS (
                SELECT 1
                FROM finance.lineage_review_decisions
                WHERE lineage_group_id = %s
            ) AS decided
            """,
            (group.group_id,),
        ).fetchone()["decided"]
        if group_is_decided and persisted_members != incoming_members:
            raise ShadowSafetyError(
                "decided lineage group membership differs from sealed evidence"
            )
        for member_identity_hash in sorted(
            incoming_members - persisted_members
        ):
            unit.connection.execute(
                """
                INSERT INTO finance.lineage_review_group_members (
                    lineage_group_id, member_identity_hash
                )
                SELECT %s, %s
                WHERE NOT EXISTS (
                    SELECT 1
                    FROM finance.lineage_review_group_members
                    WHERE lineage_group_id = %s
                      AND member_identity_hash = %s
                )
                """,
                (
                    group.group_id,
                    member_identity_hash,
                    group.group_id,
                    member_identity_hash,
                ),
            )
        persisted_members = {
            row["member_identity_hash"]
            for row in unit.connection.execute(
                """
                SELECT member_identity_hash
                FROM finance.lineage_review_group_members
                WHERE lineage_group_id = %s
                """,
                (group.group_id,),
            ).fetchall()
        }
        if persisted_members != incoming_members:
            raise ShadowSafetyError(
                "persisted lineage members differ from sealed evidence"
            )
    for decision in catalog.lineage_decisions:
        unit.connection.execute(
            """
            INSERT INTO finance.lineage_review_decisions (
                lineage_decision_id, lineage_group_id, decision_version,
                candidate_hash, audit_graph_hash, evidence_set_hash, outcome,
                survivor_identity_hash, rationale_hash, decided_at, observed_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (lineage_decision_id) DO NOTHING
            """,
            (
                decision.decision_id,
                decision.group_id,
                decision.decision_version,
                decision.candidate_hash,
                decision.audit_graph_hash,
                decision.evidence_set_hash,
                decision.outcome,
                decision.survivor_identity_hash,
                decision.rationale_hash,
                decision.decided_at,
                decision.observed_at,
            ),
        )
        persisted_decision = unit.connection.execute(
            """
            SELECT decision_version, candidate_hash, audit_graph_hash,
                   evidence_set_hash, outcome, survivor_identity_hash,
                   rationale_hash, decided_at
            FROM finance.lineage_review_decisions
            WHERE lineage_decision_id = %s
            """,
            (decision.decision_id,),
        ).fetchone()
        if (
            persisted_decision is None
            or persisted_decision["decision_version"]
            != decision.decision_version
            or persisted_decision["candidate_hash"] != decision.candidate_hash
            or persisted_decision["audit_graph_hash"]
            != decision.audit_graph_hash
            or persisted_decision["evidence_set_hash"]
            != decision.evidence_set_hash
            or persisted_decision["outcome"] != decision.outcome
            or persisted_decision["survivor_identity_hash"]
            != decision.survivor_identity_hash
            or persisted_decision["rationale_hash"]
            != decision.rationale_hash
            or persisted_decision["decided_at"] != decision.decided_at
        ):
            raise ShadowSafetyError(
                "persisted lineage decision differs from sealed evidence"
            )
    event_data = {
        "planHash": plan["planHash"],
        "event": "apply-succeeded",
        "counts": counts,
    }
    unit.connection.execute(
        """
        INSERT INTO finance.shadow_run_events (
            shadow_run_event_id, plan_hash, event_type, safe_counts,
            occurred_at, event_hash
        ) VALUES (%s, %s, 'apply-succeeded', %s::jsonb, %s, %s)
        ON CONFLICT (shadow_run_event_id) DO NOTHING
        """,
        (
            stable_id("shadow_run_event", plan["planHash"], "apply-succeeded"),
            plan["planHash"],
            json.dumps(counts, sort_keys=True),
            datetime.now(timezone.utc),
            content_hash(event_data),
        ),
    )


def _persist_canonical_identity(
    unit: Any,
    data_dir: Path,
    plan: dict[str, Any],
) -> dict[str, Any]:
    """Persist the published identity generation inside the sealed transaction.

    The apply refuses unless the replayed generation is bit-for-bit the one the
    canonical publication declared and the plan sealed.  Any drift raises, which
    rolls back the single unit of work that also carries the source apply, so
    the ledger and its identity evidence can never disagree.
    """

    expected = plan.get("identity")
    if not isinstance(expected, dict):
        raise ShadowSafetyError("sealed plan carries no canonical identity binding")
    try:
        verified = canonical_identity.verified_resolution(data_dir)
    except canonical_identity.CanonicalIdentityError as exc:
        raise ShadowSafetyError(
            f"canonical identity is not applicable: {exc.code}"
        ) from exc
    if verified.proof != expected:
        raise ShadowSafetyError(
            "canonical identity generation changed after planning"
        )
    persisted = persist_identity_resolution(unit.connection, verified.resolution)
    if (
        persisted.generation_hash != expected["generationHash"]
        or persisted.claim_count != expected["claims"]
        or persisted.event_count != expected["canonicalEvents"]
        or persisted.decision_count != expected["decisions"]
    ):
        raise ShadowSafetyError("persisted identity generation differs from the plan")
    read_back = unit.connection.execute(
        """
        SELECT generation_number, canonical_state_hash, policy_hash
        FROM finance.canonical_identity_policy_generations
        WHERE generation_hash = %s
        """,
        (expected["generationHash"],),
    ).fetchone()
    if read_back is None:
        raise ShadowSafetyError("identity generation was not persisted")
    generation_number = int(_identity_cell(read_back, "generation_number", 0))
    if (
        _identity_cell(read_back, "canonical_state_hash", 1)
        != expected["canonicalStateHash"]
        or _identity_cell(read_back, "policy_hash", 2) != expected["policyHash"]
        or generation_number != persisted.generation_number
    ):
        raise ShadowSafetyError("persisted identity evidence differs from the plan")
    return {
        "policyVersion": expected["policyVersion"],
        "policyHash": expected["policyHash"],
        "generationHash": expected["generationHash"],
        "canonicalStateHash": expected["canonicalStateHash"],
        "identityScopeHash": expected["identityScopeHash"],
        "generationNumber": generation_number,
        "inserted": persisted.inserted,
        "claims": persisted.claim_count,
        "canonicalEvents": persisted.event_count,
        "decisions": persisted.decision_count,
    }


def _identity_cell(row: Any, name: str, index: int) -> Any:
    if isinstance(row, dict):
        return row[name]
    return row[index]


def _record_apply_failure(
    repository: PostgresRepository,
    plan: dict[str, Any],
    failure_code: str,
) -> None:
    safe_counts = {
        **plan["counts"],
        "failureCode": failure_code,
    }
    event_data = {
        "planHash": plan["planHash"],
        "event": "apply-failed",
        "counts": safe_counts,
    }
    with repository.unit_of_work(
        f"finance-shadow-apply-failure:{plan['planHash']}"
    ) as unit:
        _insert_plan_record(unit, plan)
        unit.connection.execute(
            """
            INSERT INTO finance.shadow_run_events (
                shadow_run_event_id, plan_hash, event_type, safe_counts,
                occurred_at, event_hash
            ) VALUES (%s, %s, 'apply-failed', %s::jsonb, %s, %s)
            ON CONFLICT (shadow_run_event_id) DO NOTHING
            """,
            (
                stable_id(
                    "shadow_run_event",
                    plan["planHash"],
                    "apply-failed",
                    failure_code,
                ),
                plan["planHash"],
                json.dumps(safe_counts, sort_keys=True),
                datetime.now(timezone.utc),
                content_hash(event_data),
            ),
        )


def _apply_ready_plan(
    data_dir: Path,
    repository: PostgresRepository,
    plan: dict[str, Any],
    backup_basename: str | None,
) -> dict[str, Any]:
    with repository.unit_of_work("finance-shadow-sealed-apply") as unit:
        metadata = unit.connection.execute(
            """
            SELECT instance_id, environment_marker, authority_mode,
                   wealthfolio_mutation_enabled, cutover_authorized
            FROM finance.shadow_authority_metadata
            WHERE singleton
            """
        ).fetchone()
        applied_migrations = [
            {
                "version": row["version"],
                "name": row["name"],
                "sha256": row["checksum"],
            }
            for row in unit.connection.execute(
                """
                SELECT version, name, checksum
                FROM finance.schema_migrations
                ORDER BY version::integer
                """
            ).fetchall()
        ]
        if metadata is None:
            raise ShadowSafetyError("locked shadow authority marker is missing")
        locked_fingerprint = content_hash(
            {
                "instanceId": str(metadata["instance_id"]),
                "environment": metadata["environment_marker"],
                "migrationSetHash": migration_set_hash(),
            }
        )
        if (
            metadata["environment_marker"] != plan["environmentMarker"]
            or metadata["authority_mode"] != "shadow"
            or metadata["wealthfolio_mutation_enabled"]
            or metadata["cutover_authorized"]
            or applied_migrations != plan["migrations"]
            or locked_fingerprint != plan["databaseFingerprint"]
        ):
            raise ShadowSafetyError(
                "locked database identity or migrations differ from the plan"
            )
        starting_state = unit.state()
        if state_digest(starting_state) != plan["startingStateHash"]:
            raise ShadowSafetyError("shadow database changed while apply was starting")
        observation_count = (
            len(starting_state.transaction_observations)
            + len(starting_state.balance_observations)
            + len(starting_state.position_observations)
            + len(starting_state.valuation_observations)
            + len(starting_state.artifact_observations)
        )
        backup = verify_backup(
            data_dir,
            backup_basename,
            observation_count,
            dsn=repository.dsn,
        )
        generated_at = datetime.fromisoformat(plan["generatedAt"])
        catalog = load_source_catalog(data_dir, generated_at=generated_at)
        if [asdict(item) for item in catalog.files] != plan["inputFiles"]:
            raise ShadowSafetyError("immutable input set changed after planning")
        if catalog.blockers:
            raise ShadowSafetyError(
                "source capability blockers appeared after planning"
            )
        simulated = _simulate(starting_state, catalog)
        if state_digest(simulated) != plan["expectedStateHash"]:
            raise ShadowSafetyError(
                "sealed replay does not match the planned state"
            )

        state = starting_state
        memory = MemoryRepository(starting_state)
        for batch in catalog.batches:
            with memory.unit_of_work(
                f"apply:{batch.connection.source_system}:"
                f"{batch.connection.connection_key}"
            ) as memory_unit:
                state = memory_unit.state()
                reconciliation = build_plan(
                    state, batch, projection_target=None
                )
                state = memory_unit.apply(batch, reconciliation)
            unit.apply(batch, reconciliation, return_state=False)
        persisted_state = unit.state()
        if (
            state_digest(state) != plan["expectedStateHash"]
            or state_digest(persisted_state) != plan["expectedStateHash"]
        ):
            raise ShadowSafetyError("database result differs from sealed replay")
        identity = _persist_canonical_identity(unit, data_dir, plan)
        _insert_shadow_control(unit, plan, catalog, identity)
    return {
        "applied": True,
        "planHash": plan["planHash"],
        "inputSetHash": plan["inputSetHash"],
        "stateHash": plan["expectedStateHash"],
        "counts": plan["counts"],
        "identity": identity,
        "backup": backup,
        "gaps": plan["gaps"],
    }


def apply_plan(
    data_dir: Path,
    dsn: str,
    *,
    plan_path: Path,
    supplied_hash: str,
    backup_basename: str | None = None,
) -> dict[str, Any]:
    plan = load_plan(data_dir, plan_path, supplied_hash)
    _require_apply_interlocks(plan, data_dir)
    status = verify_database(dsn, plan["environmentMarker"])
    if (
        status.database_fingerprint != plan["databaseFingerprint"]
        or status.migration_set_hash != plan["migrationSetHash"]
    ):
        raise ShadowSafetyError("sealed database identity or migrations changed")
    repository = PostgresRepository(dsn)
    try:
        return _apply_ready_plan(
            data_dir, repository, plan, backup_basename
        )
    except Exception as original:
        try:
            _record_apply_failure(repository, plan, type(original).__name__)
        except Exception as recording_error:
            errors = ExceptionGroup(
                "shadow apply and failure-status recording both failed",
                [original, recording_error],
            )
            raise ShadowSafetyError(
                "shadow apply failed and its failure status could not be recorded"
            ) from errors
        if isinstance(original, _psycopg()[0].Error):
            raise ShadowSafetyError(
                "PostgreSQL rejected the sealed shadow apply"
            ) from original
        raise


def _multiset(values: Iterator[tuple[Any, ...]]) -> Counter:
    return Counter(values)


def _latest_runs_by_blob(runs: tuple[Any, ...]) -> dict[str, Any]:
    latest: dict[str, Any] = {}
    for run in sorted(
        runs,
        key=lambda item: (
            item.processed_at,
            item.id,
        ),
    ):
        latest[run.source_blob_id] = run
    return latest


def _latest_successful_plan(
    connection: Any,
) -> dict[str, Any] | None:
    return connection.execute(
        """
        SELECT plan.plan_hash, plan.input_set_hash,
               plan.expected_state_hash, event.occurred_at
        FROM finance.shadow_run_events event
        JOIN finance.shadow_plans plan USING (plan_hash)
        WHERE event.event_type = 'apply-succeeded'
        ORDER BY event.occurred_at DESC, plan.plan_hash
        LIMIT 1
        """
    ).fetchone()


def create_drift_report(
    data_dir: Path,
    dsn: str,
    *,
    environment: str,
    generated_at: datetime | None = None,
) -> tuple[dict[str, Any], Path]:
    generated_at = utc(generated_at or datetime.now(timezone.utc))
    status = verify_database(dsn, environment)
    catalog = load_source_catalog(data_dir, generated_at=generated_at)
    repository = PostgresRepository(dsn)
    with repository.read_snapshot() as unit:
        state = unit.state()
        sealed_plan = _latest_successful_plan(unit.connection)
    input_set_hash = content_hash([asdict(item) for item in catalog.files])
    expected = _multiset(
        iter(
            (
                item.path_hash,
                item.source_kind,
                item.source_version,
                item.content_hash,
                item.parser_hash,
                item.record_count,
            )
            for item in catalog.files
        )
    )
    run_by_blob = _latest_runs_by_blob(state.runs)
    actual = _multiset(
        iter(
            (
                item.raw_locator.removeprefix("external+sha256://"),
                item.source_kind,
                item.source_version,
                item.content_hash,
                (run_by_blob[item.id].parser_hash or ""),
                run_by_blob[item.id].records_seen,
            )
            for item in state.blobs
            if (
                item.source_kind != "legacy-candidate"
                and item.id in run_by_blob
            )
        )
    )
    missing = expected - actual
    extra = actual - expected
    state_hash = state_digest(state)
    sealed_input_matches = bool(
        sealed_plan and sealed_plan["input_set_hash"] == input_set_hash
    )
    canonical_state_matches = bool(
        sealed_plan
        and sealed_input_matches
        and sealed_plan["expected_state_hash"] == state_hash
    )
    latest_lineage_decisions: dict[str, LineageDecision] = {}
    for decision in catalog.lineage_decisions:
        previous = latest_lineage_decisions.get(decision.group_id)
        if (
            previous is None
            or decision.decision_version > previous.decision_version
        ):
            latest_lineage_decisions[decision.group_id] = decision
    resolved_lineage_groups = {
        group_id
        for group_id, decision in latest_lineage_decisions.items()
        if decision.outcome != "insufficient-evidence"
    }
    stable = {
        "schemaVersion": 1,
        "mode": "postgresql-shadow-drift",
        "environmentMarker": environment,
        "databaseFingerprint": status.database_fingerprint,
        "inputSetHash": input_set_hash,
        "stateHash": state_hash,
        "missingInputBlobs": sum(missing.values()),
        "extraDatabaseBlobs": sum(extra.values()),
        "sourceBlockers": len(catalog.blockers),
        "openQualityIssues": sum(issue.status == "open" for issue in state.issues),
        "lineageGroups": len(catalog.lineage_groups),
        "lineageDecisions": len(catalog.lineage_decisions),
        "unresolvedLineageGroups": len(catalog.lineage_groups)
        - len(resolved_lineage_groups),
        "sealedPlanFound": sealed_plan is not None,
        "sealedPlanInputMatches": sealed_input_matches,
        "canonicalStateMatchesSealedPlan": canonical_state_matches,
        "wealthfolioMutation": "prohibited",
        "sourceMutation": "prohibited",
    }
    drift_hash = content_hash(stable)
    report = {
        **stable,
        "driftHash": drift_hash,
        "missing": [
            {
                "sourceKind": key[1],
                "sourceVersion": key[2],
                "contentHash": key[3],
                "pathHash": key[0],
                "parserHash": key[4],
                "recordCount": key[5],
                "count": count,
            }
            for key, count in sorted(missing.items())
        ],
        "extra": [
            {
                "sourceKind": key[1],
                "sourceVersion": key[2],
                "contentHash": key[3],
                "pathHash": key[0],
                "parserHash": key[4],
                "recordCount": key[5],
                "count": count,
            }
            for key, count in sorted(extra.items())
        ],
        "blockers": list(catalog.blockers),
        "gaps": list(catalog.gaps),
    }
    output = validate_private_output(
        data_dir / SHADOW_OUTPUT / "reports" / f"drift-{drift_hash}.json",
        data_dir,
        REPOSITORY_ROOT,
    )
    _write_json(output, report)
    return report, output


def status_document(dsn: str, environment: str) -> dict[str, Any]:
    status = verify_database(dsn, environment)
    return {
        "schemaVersion": 1,
        "mode": "shadow",
        "healthy": True,
        "environmentMarker": status.environment_marker,
        "databaseFingerprint": status.database_fingerprint,
        "migrationSetHash": status.migration_set_hash,
        "migrationCount": status.migration_count,
        "sourceBlobCount": status.source_blob_count,
        "observationCount": status.observation_count,
        "openIssueCount": status.open_issue_count,
        "lastSuccessAt": (
            status.last_success_at.isoformat() if status.last_success_at else None
        ),
        "lastFailureAt": (
            status.last_failure_at.isoformat() if status.last_failure_at else None
        ),
        "cutoverAuthorized": False,
        "wealthfolioMutationEnabled": False,
    }


def schema_document(dsn: str, environment: str) -> dict[str, Any]:
    status = verify_database(dsn, environment)
    psycopg, dict_row = _psycopg()
    with psycopg.connect(dsn, row_factory=dict_row) as connection:
        with connection.transaction():
            connection.execute(
                "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
            )
            version = connection.execute(
                "SELECT current_setting('server_version') AS server_version"
            ).fetchone()["server_version"]
            rows = connection.execute(
                """
                SELECT version, name, checksum, applied_at
                FROM finance_read.migration_status
                ORDER BY version::integer
                """
            ).fetchall()
    return {
        "schemaVersion": 1,
        "authorityMode": "shadow",
        "databaseFingerprint": status.database_fingerprint,
        "postgresVersion": version,
        "migrationSetHash": status.migration_set_hash,
        "migrations": [
            {
                "version": row["version"],
                "name": row["name"],
                "sha256": row["checksum"],
                "appliedAt": row["applied_at"].isoformat(),
            }
            for row in rows
        ],
    }


def prometheus_metrics(document: dict[str, Any]) -> str:
    values = {
        "finance_shadow_healthy": int(document["healthy"]),
        "finance_shadow_migrations": document["migrationCount"],
        "finance_shadow_source_blobs": document["sourceBlobCount"],
        "finance_shadow_observations": document["observationCount"],
        "finance_shadow_open_issues": document["openIssueCount"],
        "finance_shadow_cutover_authorized": int(
            document["cutoverAuthorized"]
        ),
        "finance_shadow_wealthfolio_mutation_enabled": int(
            document["wealthfolioMutationEnabled"]
        ),
    }
    return "\n".join(f"{key} {value}" for key, value in values.items()) + "\n"


def deterministic_export(data_dir: Path, dsn: str, relative_path: Path) -> dict[str, Any]:
    if relative_path.is_absolute():
        raise ShadowSafetyError("export path must be relative to --data-dir")
    output = (data_dir / relative_path).resolve()
    validate_private_output(output, data_dir, REPOSITORY_ROOT)
    document = export_state(PostgresRepository(dsn).state())
    _write_json(output, document)
    return {
        "stateHash": document["stateHash"],
        "collectionCount": len(document["state"]),
    }


@contextmanager
def scheduled_run_lock(data_dir: Path) -> Iterator[None]:
    lock_path = validate_private_output(
        data_dir / SHADOW_OUTPUT / "run.lock", data_dir, REPOSITORY_ROOT
    )
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+b")
    try:
        if handle.seek(0, os.SEEK_END) == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise ShadowSafetyError(
                    "another shadow scheduled run is active"
                ) from exc
        else:
            import fcntl

            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise ShadowSafetyError(
                    "another shadow scheduled run is active"
                ) from exc
        yield
    finally:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()
