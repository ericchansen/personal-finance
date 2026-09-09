"""PostgreSQL repository adapter with transactional advisory locking."""

from __future__ import annotations

import json
from contextlib import contextmanager
from typing import TYPE_CHECKING

from .domain import (
    AppStateSnapshot,
    ArtifactObservation,
    AuditEvent,
    BalanceObservation,
    CanonicalAccount,
    CanonicalTransaction,
    DurableDecision,
    FinanceState,
    IngestionRun,
    ObservationBatch,
    ObservationLink,
    PositionObservation,
    ProjectionRecord,
    ProjectionRun,
    QualityIssue,
    ReconciliationPlan,
    SourceAccount,
    SourceBlob,
    SourceConnection,
    TransactionObservation,
    ValuationObservation,
    stable_id,
)

if TYPE_CHECKING:
    from .identity import IdentityResolution
    from .identity_postgres import PersistedIdentityGeneration

GLOBAL_WRITER_LOCK = "finance-shadow-authority-writer"


def _psycopg():
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as exc:
        raise RuntimeError(
            "PostgreSQL support requires psycopg; install requirements.txt"
        ) from exc
    return psycopg, dict_row


def postgres_error_type():
    return _psycopg()[0].Error


class _PostgresUnit:
    def __init__(self, connection) -> None:
        self.connection = connection

    def _rows(self, query: str, parameters=()):
        with self.connection.cursor() as cursor:
            cursor.execute(query, parameters)
            return cursor.fetchall()

    def state(self) -> FinanceState:
        blobs = tuple(
            SourceBlob(
                id=str(row["source_blob_id"]),
                raw_locator=row["raw_locator"],
                content_hash=row["content_hash"],
                observed_at=row["observed_at"],
                processed_at=row["processed_at"],
                byte_size=row["byte_size"] or 0,
                media_type=row["media_type"] or "application/json",
                source_kind=row["source_kind"],
                source_version=row["source_version"],
            )
            for row in self._rows("SELECT * FROM finance.source_blobs")
        )
        runs = tuple(
            IngestionRun(
                id=str(row["ingestion_run_id"]),
                source_blob_id=str(row["source_blob_id"]),
                importer_name=row["importer_name"],
                importer_version=row["importer_version"],
                status=row["run_status"],
                effective_start=row["effective_start"],
                effective_end=row["effective_end"],
                observed_at=row["observed_at"],
                processed_at=row["processed_at"],
                records_seen=row["records_seen"],
                records_accepted=row["records_accepted"],
                parser_hash=row["parser_hash"],
                source_protocol=row["source_protocol"],
                source_version=row["source_version"],
                overlap_start=row["overlap_start"],
                overlap_end=row["overlap_end"],
                sealed_plan_hash=row["sealed_plan_hash"],
                admission_hash=row["admission_hash"],
            )
            for row in self._rows("SELECT * FROM finance.ingestion_runs")
        )
        connections = tuple(
            SourceConnection(
                id=str(row["source_connection_id"]),
                source_system=row["source_system"],
                connection_key=row["connection_key"],
                effective_from=row["effective_from"],
                observed_at=row["observed_at"],
                processed_at=row["processed_at"],
                status=row["status"],
            )
            for row in self._rows("SELECT * FROM finance.source_connections")
        )
        source_accounts = tuple(
            SourceAccount(
                id=str(row["source_account_id"]),
                connection_id=str(row["source_connection_id"]),
                external_id=row["external_account_id"],
                name=row["source_account_name"] or "",
                account_type=row["account_type"],
                currency=row["currency_code"] or "USD",
                effective_from=row["effective_from"],
                observed_at=row["observed_at"],
                processed_at=row["processed_at"],
                canonical_key=row["canonical_key"]
                or f"unmapped:{row['source_account_id']}",
                trust_cutoff_at=row["trust_cutoff_at"],
                mapping_effective_from=row["mapping_effective_from"],
                status=row["status"],
            )
            for row in self._rows(
                """
                SELECT sa.*, ca.canonical_key,
                       sal.effective_from AS mapping_effective_from
                FROM finance.source_accounts sa
                LEFT JOIN finance.source_account_links sal
                  ON sal.source_account_id = sa.source_account_id
                 AND sal.effective_to IS NULL
                LEFT JOIN finance.canonical_accounts ca
                  ON ca.canonical_account_id = sal.canonical_account_id
                """
            )
        )
        transactions = tuple(
            TransactionObservation(
                id=str(row["transaction_observation_id"]),
                source_account_id=str(row["source_account_id"]),
                source_transaction_id=row["source_transaction_id"],
                observation_hash=row["observation_hash"],
                effective_at=row["effective_at"],
                observed_at=row["observed_at"],
                processed_at=row["processed_at"],
                amount=row["amount"],
                currency=row["currency_code"],
                description=row["description"] or "",
                status=row["source_status"] or "posted",
                source_blob_id=str(row["source_blob_id"]),
                ingestion_run_id=str(row["ingestion_run_id"]),
                last_seen_at=row["last_seen_at"],
                sighting_order=row["sighting_order"],
                last_seen_run_id=str(row["last_seen_run_id"]),
                last_seen_order=row["last_seen_order"],
            )
            for row in self._rows("SELECT * FROM finance.transaction_observations")
        )
        balances = tuple(
            BalanceObservation(
                id=str(row["balance_observation_id"]),
                source_account_id=str(row["source_account_id"]),
                observation_hash=row["observation_hash"],
                balance_type=row["balance_type"],
                effective_at=row["effective_at"],
                observed_at=row["observed_at"],
                processed_at=row["processed_at"],
                amount=row["amount"],
                currency=row["currency_code"],
                source_blob_id=str(row["source_blob_id"]),
                ingestion_run_id=str(row["ingestion_run_id"]),
            )
            for row in self._rows("SELECT * FROM finance.balance_observations")
        )
        positions = tuple(
            PositionObservation(
                id=str(row["position_observation_id"]),
                source_account_id=str(row["source_account_id"]),
                external_position_id=row["external_position_id"],
                observation_hash=row["observation_hash"],
                instrument_key=row["instrument_key"],
                effective_at=row["effective_at"],
                observed_at=row["observed_at"],
                processed_at=row["processed_at"],
                quantity=row["quantity"],
                cost_basis=row["cost_basis"],
                currency=row["currency_code"],
                source_blob_id=str(row["source_blob_id"]),
                ingestion_run_id=str(row["ingestion_run_id"]),
            )
            for row in self._rows("SELECT * FROM finance.position_observations")
        )
        valuations = tuple(
            ValuationObservation(
                id=str(row["valuation_observation_id"]),
                source_account_id=str(row["source_account_id"]),
                observation_hash=row["observation_hash"],
                valuation_type=row["valuation_type"],
                instrument_key=row["instrument_key"],
                effective_at=row["effective_at"],
                observed_at=row["observed_at"],
                processed_at=row["processed_at"],
                value=row["value"],
                currency=row["currency_code"],
                source_blob_id=str(row["source_blob_id"]),
                ingestion_run_id=str(row["ingestion_run_id"]),
            )
            for row in self._rows("SELECT * FROM finance.valuation_observations")
        )
        artifacts = tuple(
            ArtifactObservation(
                id=str(row["artifact_observation_id"]),
                ingestion_run_id=str(row["ingestion_run_id"]),
                source_blob_id=str(row["source_blob_id"]),
                observation_kind=row["observation_kind"],
                source_identity_hash=row["source_identity_hash"],
                observation_hash=row["observation_hash"],
                record_index=row["record_index"],
                effective_at=row["effective_at"],
                observed_at=row["observed_at"],
                processed_at=row["processed_at"],
                payload=row["payload"],
                issue_id=(
                    str(row["quality_issue_id"])
                    if row["quality_issue_id"]
                    else None
                ),
            )
            for row in self._rows("SELECT * FROM finance.artifact_observations")
        )
        canonical_accounts = tuple(
            CanonicalAccount(
                id=str(row["canonical_account_id"]),
                key=row["canonical_key"],
                display_name=row["display_name"],
                account_type=row["account_type"],
                currency=row["currency_code"] or "USD",
                effective_from=row["effective_from"],
                observed_at=row["observed_at"],
                processed_at=row["processed_at"],
                status=row["status"],
            )
            for row in self._rows("SELECT * FROM finance.canonical_accounts")
        )
        canonical_transactions = tuple(
            CanonicalTransaction(
                id=str(row["canonical_transaction_id"]),
                account_id=str(row["canonical_account_id"]),
                key=row["canonical_key"],
                effective_at=row["effective_at"],
                observed_at=row["observed_at"],
                processed_at=row["processed_at"],
                amount=row["amount"],
                currency=row["currency_code"],
                description=row["description"] or "",
                status=row["status"],
                correction_of_id=(
                    str(row["corrects_transaction_id"])
                    if row["corrects_transaction_id"]
                    else None
                ),
            )
            for row in self._rows("SELECT * FROM finance.canonical_transactions")
        )
        links = tuple(
            ObservationLink(
                id=str(row["transaction_observation_link_id"]),
                observation_id=str(row["transaction_observation_id"]),
                canonical_transaction_id=str(row["canonical_transaction_id"]),
                method=row["link_method"],
                issue_id=str(row["quality_issue_id"]) if row["quality_issue_id"] else None,
                decision_id=str(row["decision_id"]) if row["decision_id"] else None,
                current=row["is_current"],
                effective_at=row["effective_at"],
                observed_at=row["observed_at"],
                processed_at=row["processed_at"],
            )
            for row in self._rows("SELECT * FROM finance.transaction_observation_links")
        )
        decisions = tuple(
            DurableDecision(
                id=str(row["decision_id"]),
                decision_type=row["decision_type"],
                subject_type=row["subject_type"],
                subject_key=row["subject_key"],
                action=row["action"],
                rationale=row["rationale"],
                decided_by=row["decided_by"],
                supersedes_id=(
                    str(row["supersedes_decision_id"])
                    if row["supersedes_decision_id"]
                    else None
                ),
                effective_at=row["effective_at"],
                observed_at=row["observed_at"],
                processed_at=row["processed_at"],
            )
            for row in self._rows("SELECT * FROM finance.durable_decisions")
        )
        issues = tuple(
            QualityIssue(
                id=str(row["quality_issue_id"]),
                issue_type=row["issue_type"],
                severity=row["severity"],
                subject_type=row["subject_type"],
                subject_key=row["subject_key"],
                details=row["details"],
                status=row["status"],
                effective_at=row["effective_at"],
                observed_at=row["observed_at"],
                processed_at=row["processed_at"],
            )
            for row in self._rows("SELECT * FROM finance.quality_issues")
        )
        snapshots = tuple(
            AppStateSnapshot(
                id=str(row["app_state_snapshot_id"]),
                app_name=row["app_name"],
                state_kind=row["state_kind"],
                external_locator=row["external_locator"],
                state_hash=row["state_hash"],
                effective_at=row["effective_at"],
                observed_at=row["observed_at"],
                processed_at=row["processed_at"],
            )
            for row in self._rows("SELECT * FROM finance.app_state_snapshots")
        )
        projection_runs = tuple(
            ProjectionRun(
                id=str(row["projection_run_id"]),
                target_system=row["target_system"],
                version=row["projection_version"],
                status=row["run_status"],
                cutoff_effective_at=row["cutoff_effective_at"],
                observed_at=row["observed_at"],
                processed_at=row["processed_at"],
            )
            for row in self._rows("SELECT * FROM finance.projection_runs")
        )
        projection_records = tuple(
            ProjectionRecord(
                id=str(row["projection_record_id"]),
                run_id=str(row["projection_run_id"]),
                canonical_transaction_id=str(row["canonical_transaction_id"]),
                target_system=row["target_system"],
                target_record_key=row["target_record_key"] or "",
                projected_hash=row["projected_hash"],
                status=row["projection_status"],
                current=row["is_current"],
                effective_at=row["effective_at"],
                observed_at=row["observed_at"],
                processed_at=row["processed_at"],
            )
            for row in self._rows("SELECT * FROM finance.projection_records")
        )
        audit_events = tuple(
            AuditEvent(
                id=str(row["audit_event_id"]),
                event_type=row["event_type"],
                actor=row["actor"],
                subject_type=row["subject_type"],
                subject_key=row["subject_key"],
                data=row["event_data"],
                effective_at=row["effective_at"],
                observed_at=row["observed_at"],
                processed_at=row["processed_at"],
            )
            for row in self._rows("SELECT * FROM finance.audit_events")
        )
        return FinanceState(
            blobs=blobs,
            runs=runs,
            connections=connections,
            source_accounts=source_accounts,
            transaction_observations=transactions,
            balance_observations=balances,
            position_observations=positions,
            valuation_observations=valuations,
            artifact_observations=artifacts,
            canonical_accounts=canonical_accounts,
            canonical_transactions=canonical_transactions,
            links=links,
            decisions=decisions,
            issues=issues,
            app_state_snapshots=snapshots,
            projection_runs=projection_runs,
            projection_records=projection_records,
            audit_events=audit_events,
        )

    def _execute(self, query: str, parameters=()) -> None:
        with self.connection.cursor() as cursor:
            cursor.execute(query, parameters)

    def append_decision(self, decision: DurableDecision) -> None:
        self._execute(
            """
            INSERT INTO finance.durable_decisions (
                decision_id, decision_type, subject_type, subject_key, action,
                rationale, decided_by, supersedes_decision_id, effective_at,
                observed_at, processed_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (decision_id) DO NOTHING
            """,
            (
                decision.id, decision.decision_type, decision.subject_type,
                decision.subject_key, decision.action, decision.rationale,
                decision.decided_by, decision.supersedes_id, decision.effective_at,
                decision.observed_at, decision.processed_at,
            ),
        )

    def record_app_state(self, snapshot: AppStateSnapshot) -> None:
        self._execute(
            """
            INSERT INTO finance.app_state_snapshots (
                app_state_snapshot_id, app_name, state_kind, external_locator,
                state_hash, effective_at, observed_at, processed_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (app_state_snapshot_id) DO NOTHING
            """,
            (
                snapshot.id, snapshot.app_name, snapshot.state_kind,
                snapshot.external_locator, snapshot.state_hash, snapshot.effective_at,
                snapshot.observed_at, snapshot.processed_at,
            ),
        )

    def apply(
        self,
        batch: ObservationBatch,
        plan: ReconciliationPlan,
        *,
        return_state: bool = True,
    ) -> FinanceState | None:
        self._execute(
            """
            INSERT INTO finance.source_blobs (
                source_blob_id, raw_locator, content_hash, media_type, byte_size,
                observed_at, processed_at, source_kind, source_version
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (source_blob_id) DO NOTHING
            """,
            (
                batch.blob.id, batch.blob.raw_locator, batch.blob.content_hash,
                batch.blob.media_type, batch.blob.byte_size,
                batch.blob.observed_at, batch.blob.processed_at,
                batch.blob.source_kind, batch.blob.source_version,
            ),
        )
        self._execute(
            """
            INSERT INTO finance.ingestion_runs (
                ingestion_run_id, source_blob_id, importer_name, importer_version,
                run_status, effective_start, effective_end, observed_at, processed_at,
                finished_at, records_seen, records_accepted, parser_hash,
                source_protocol, source_version, overlap_start, overlap_end,
                sealed_plan_hash, admission_hash
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s
            )
            ON CONFLICT (ingestion_run_id) DO NOTHING
            """,
            (
                batch.run.id, batch.run.source_blob_id, batch.run.importer_name,
                batch.run.importer_version, batch.run.status, batch.run.effective_start,
                batch.run.effective_end, batch.run.observed_at, batch.run.processed_at,
                batch.run.processed_at, batch.run.records_seen, batch.run.records_accepted,
                batch.run.parser_hash, batch.run.source_protocol,
                batch.run.source_version, batch.run.overlap_start,
                batch.run.overlap_end, batch.run.sealed_plan_hash,
                batch.run.admission_hash,
            ),
        )
        self._execute(
            """
            INSERT INTO finance.source_connections (
                source_connection_id, source_system, connection_key, effective_from,
                observed_at, processed_at, status
            ) VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (source_connection_id) DO NOTHING
            """,
            (
                batch.connection.id, batch.connection.source_system,
                batch.connection.connection_key, batch.connection.effective_from,
                batch.connection.observed_at, batch.connection.processed_at,
                batch.connection.status,
            ),
        )
        for account in batch.accounts:
            self._execute(
                """
                INSERT INTO finance.source_accounts (
                    source_account_id, source_connection_id, external_account_id,
                    source_account_name, account_type, currency_code, effective_from,
                    observed_at, processed_at, status
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (source_account_id) DO UPDATE SET
                    source_account_name = EXCLUDED.source_account_name,
                    account_type = EXCLUDED.account_type,
                    currency_code = EXCLUDED.currency_code,
                    status = CASE
                        WHEN %s THEN EXCLUDED.status
                        ELSE finance.source_accounts.status
                    END,
                    observed_at = EXCLUDED.observed_at,
                    processed_at = EXCLUDED.processed_at
                WHERE EXCLUDED.observed_at >= finance.source_accounts.observed_at
                """,
                (
                    account.id, account.connection_id, account.external_id, account.name,
                    account.account_type, account.currency, account.effective_from,
                    account.observed_at, account.processed_at, account.status,
                    account.status_provided,
                ),
            )
            self._execute(
                "SELECT source_account_id FROM finance.source_accounts "
                "WHERE source_account_id = %s FOR UPDATE",
                (account.id,),
            )
        for canonical in plan.canonical_accounts:
            self._execute(
                """
                INSERT INTO finance.canonical_accounts (
                    canonical_account_id, canonical_key, display_name, account_type,
                    currency_code, status, effective_from, observed_at, processed_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (canonical_account_id) DO NOTHING
                """,
                (
                    canonical.id, canonical.key, canonical.display_name,
                    canonical.account_type, canonical.currency, canonical.status,
                    canonical.effective_from, canonical.observed_at,
                    canonical.processed_at,
                ),
            )
        canonical_by_key = {
            row["canonical_key"]: str(row["canonical_account_id"])
            for row in self._rows(
                "SELECT canonical_account_id, canonical_key FROM finance.canonical_accounts"
            )
        }
        for account in batch.accounts:
            canonical_id = canonical_by_key[account.canonical_key]
            mapping_effective_from = (
                account.mapping_effective_from or account.effective_from
            )
            mapping_decision_id = stable_id(
                "decision", "account_mapping", account.id, canonical_id
            )
            self._execute(
                """
                INSERT INTO finance.durable_decisions (
                    decision_id, decision_type, subject_type, subject_key, action,
                    rationale, decided_by, effective_at, observed_at, processed_at
                ) VALUES (%s, 'account_mapping', 'source_account', %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (decision_id) DO NOTHING
                """,
                (
                    mapping_decision_id, account.id, f"map_to:{canonical_id}",
                    "Synthetic/private mapping supplied by ingestion configuration",
                    "ingestion-service", mapping_effective_from,
                    account.observed_at, account.processed_at,
                ),
            )
            current_mapping = self._rows(
                """
                SELECT sal.canonical_account_id, sal.effective_from, ca.canonical_key
                FROM finance.source_account_links sal
                JOIN finance.canonical_accounts ca
                  ON ca.canonical_account_id = sal.canonical_account_id
                WHERE sal.source_account_id = %s AND sal.effective_to IS NULL
                FOR UPDATE OF sal
                """,
                (account.id,),
            )
            if not current_mapping:
                self._execute(
                    """
                    INSERT INTO finance.source_account_links (
                        source_account_id, canonical_account_id, decision_id,
                        effective_from, observed_at, processed_at
                    ) VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (
                        account.id, canonical_id, mapping_decision_id,
                        mapping_effective_from, account.observed_at,
                        account.processed_at,
                    ),
                )
            elif str(current_mapping[0]["canonical_account_id"]) != canonical_id:
                if mapping_effective_from < current_mapping[0]["effective_from"]:
                    raise ValueError(
                        "source account remapping cannot precede the current mapping"
                    )
                self._execute(
                    """
                    UPDATE finance.source_account_links
                    SET effective_to = %s
                    WHERE source_account_id = %s AND effective_to IS NULL
                    """,
                    (mapping_effective_from, account.id),
                )
                self._execute(
                    """
                    INSERT INTO finance.source_account_links (
                        source_account_id, canonical_account_id, decision_id,
                        effective_from, observed_at, processed_at
                    ) VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (
                        account.id, canonical_id, mapping_decision_id,
                        mapping_effective_from, account.observed_at,
                        account.processed_at,
                    ),
                )
                remap_event_id = stable_id(
                    "audit_event",
                    "source_account_remapped",
                    account.id,
                    current_mapping[0]["canonical_key"],
                    account.canonical_key,
                    mapping_effective_from.isoformat(),
                )
                self._execute(
                    """
                    INSERT INTO finance.audit_events (
                        audit_event_id, event_type, actor, subject_type, subject_key,
                        event_data, effective_at, observed_at, processed_at
                    ) VALUES (
                        %s, 'source_account_remapped', 'ingestion-service',
                        'source_account', %s, %s::jsonb, %s, %s, %s
                    )
                    ON CONFLICT (audit_event_id) DO NOTHING
                    """,
                    (
                        remap_event_id,
                        account.id,
                        json.dumps({
                            "previousCanonicalKey": current_mapping[0]["canonical_key"],
                            "newCanonicalKey": account.canonical_key,
                            "mappingDecisionId": mapping_decision_id,
                        }),
                        mapping_effective_from,
                        account.observed_at,
                        account.processed_at,
                    ),
                )
        for decision in plan.decisions:
            self.append_decision(decision)
        for issue in plan.issues:
            self._execute(
                """
                INSERT INTO finance.quality_issues (
                    quality_issue_id, issue_type, severity, subject_type, subject_key,
                    details, status, effective_at, observed_at, processed_at
                ) VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s)
                ON CONFLICT (quality_issue_id) DO NOTHING
                """,
                (
                    issue.id, issue.issue_type, issue.severity, issue.subject_type,
                    issue.subject_key, json.dumps(issue.details), issue.status,
                    issue.effective_at, issue.observed_at, issue.processed_at,
                ),
            )
        for account in batch.accounts:
            if account.trust_cutoff_provided:
                decision_id = stable_id(
                    "decision",
                    "trust_cutoff",
                    account.id,
                    account.trust_cutoff_at or "removed",
                    batch.run.id,
                )
                self._execute(
                    """
                    UPDATE finance.source_accounts
                    SET trust_cutoff_at = %s, trust_cutoff_decision_id = %s
                    WHERE source_account_id = %s
                      AND trust_cutoff_at IS DISTINCT FROM %s
                    """,
                    (
                        account.trust_cutoff_at,
                        decision_id,
                        account.id,
                        account.trust_cutoff_at,
                    ),
                )
        issue_by_subject = {
            issue.subject_key: issue.id
            for issue in plan.issues
            if issue.issue_type == "stale_after_trust_cutoff"
        }
        for observation in batch.transactions:
            self._execute(
                """
                INSERT INTO finance.transaction_observations (
                    transaction_observation_id, ingestion_run_id, source_blob_id,
                    source_account_id, source_transaction_id, observation_hash,
                    effective_at, observed_at, processed_at, amount, currency_code,
                    description, source_status, quality_issue_id, last_seen_at,
                    sighting_order, last_seen_run_id, last_seen_order
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (source_account_id, source_transaction_id, observation_hash)
                DO UPDATE SET
                    last_seen_at = EXCLUDED.last_seen_at,
                    last_seen_run_id = EXCLUDED.last_seen_run_id,
                    last_seen_order = EXCLUDED.last_seen_order
                WHERE (
                    finance.transaction_observations.last_seen_at,
                    finance.transaction_observations.last_seen_run_id,
                    finance.transaction_observations.last_seen_order
                ) < (
                    EXCLUDED.last_seen_at,
                    EXCLUDED.last_seen_run_id,
                    EXCLUDED.last_seen_order
                )
                """,
                (
                    observation.id, observation.ingestion_run_id,
                    observation.source_blob_id, observation.source_account_id,
                    observation.source_transaction_id, observation.observation_hash,
                    observation.effective_at, observation.observed_at,
                    observation.processed_at, observation.amount, observation.currency,
                    observation.description, observation.status,
                    issue_by_subject.get(observation.id),
                    observation.last_seen_at,
                    observation.sighting_order,
                    observation.last_seen_run_id,
                    observation.last_seen_order,
                ),
            )
        for observation in batch.balances:
            self._execute(
                """
                INSERT INTO finance.balance_observations (
                    balance_observation_id, ingestion_run_id, source_blob_id,
                    source_account_id, observation_hash, balance_type, effective_at,
                    observed_at, processed_at, amount, currency_code, quality_issue_id
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (source_account_id, balance_type, effective_at, observation_hash)
                DO NOTHING
                """,
                (
                    observation.id, observation.ingestion_run_id,
                    observation.source_blob_id, observation.source_account_id,
                    observation.observation_hash, observation.balance_type,
                    observation.effective_at, observation.observed_at,
                    observation.processed_at, observation.amount,
                    observation.currency, issue_by_subject.get(observation.id),
                ),
            )
        for observation in batch.positions:
            self._execute(
                """
                INSERT INTO finance.position_observations (
                    position_observation_id, ingestion_run_id, source_blob_id,
                    source_account_id, external_position_id, observation_hash,
                    instrument_key, effective_at, observed_at, processed_at,
                    quantity, cost_basis, currency_code
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING
                """,
                (
                    observation.id, observation.ingestion_run_id,
                    observation.source_blob_id, observation.source_account_id,
                    observation.external_position_id, observation.observation_hash,
                    observation.instrument_key, observation.effective_at,
                    observation.observed_at, observation.processed_at,
                    observation.quantity, observation.cost_basis, observation.currency,
                ),
            )
        for observation in batch.valuations:
            self._execute(
                """
                INSERT INTO finance.valuation_observations (
                    valuation_observation_id, ingestion_run_id, source_blob_id,
                    source_account_id, instrument_key, observation_hash, valuation_type,
                    effective_at, observed_at, processed_at, value, currency_code
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING
                """,
                (
                    observation.id, observation.ingestion_run_id,
                    observation.source_blob_id, observation.source_account_id,
                    observation.instrument_key, observation.observation_hash,
                    observation.valuation_type, observation.effective_at,
                    observation.observed_at, observation.processed_at,
                    observation.value, observation.currency,
                ),
            )
        for observation in batch.artifacts:
            self._execute(
                """
                INSERT INTO finance.artifact_observations (
                    artifact_observation_id, ingestion_run_id, source_blob_id,
                    observation_kind, source_identity_hash, observation_hash,
                    record_index, effective_at, observed_at, processed_at,
                    payload, quality_issue_id
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s
                )
                ON CONFLICT (artifact_observation_id) DO NOTHING
                """,
                (
                    observation.id, observation.ingestion_run_id,
                    observation.source_blob_id, observation.observation_kind,
                    observation.source_identity_hash, observation.observation_hash,
                    observation.record_index, observation.effective_at,
                    observation.observed_at, observation.processed_at,
                    json.dumps(observation.payload), observation.issue_id,
                ),
            )
        for canonical in plan.canonical_transactions:
            self._execute(
                """
                INSERT INTO finance.canonical_transactions (
                    canonical_transaction_id, canonical_account_id, canonical_key,
                    effective_at, observed_at, processed_at, amount, currency_code,
                    description, status, corrects_transaction_id
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (canonical_transaction_id) DO NOTHING
                """,
                (
                    canonical.id, canonical.account_id, canonical.key,
                    canonical.effective_at, canonical.observed_at,
                    canonical.processed_at, canonical.amount, canonical.currency,
                    canonical.description, canonical.status, canonical.correction_of_id,
                ),
            )
        for mutation in plan.canonical_mutations:
            self._execute(
                """
                UPDATE finance.canonical_transactions
                SET effective_at = %s, amount = %s, currency_code = %s,
                    description = %s, status = %s
                WHERE canonical_transaction_id = %s
                """,
                (
                    mutation.effective_at, mutation.amount, mutation.currency,
                    mutation.description, mutation.status,
                    mutation.transaction_id,
                ),
            )
        for link in plan.links:
            self._execute(
                """
                INSERT INTO finance.transaction_observation_links (
                    transaction_observation_link_id, transaction_observation_id,
                    canonical_transaction_id, link_method, quality_issue_id, decision_id,
                    is_current, effective_at, observed_at, processed_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (transaction_observation_link_id) DO NOTHING
                """,
                (
                    link.id, link.observation_id, link.canonical_transaction_id,
                    link.method, link.issue_id, link.decision_id, link.current,
                    link.effective_at, link.observed_at, link.processed_at,
                ),
            )
        if plan.projection_run:
            run = plan.projection_run
            self._execute(
                """
                INSERT INTO finance.projection_runs (
                    projection_run_id, target_system, projection_version, run_status,
                    cutoff_effective_at, observed_at, processed_at, finished_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (projection_run_id) DO NOTHING
                """,
                (
                    run.id, run.target_system, run.version, run.status,
                    run.cutoff_effective_at, run.observed_at, run.processed_at,
                    run.processed_at,
                ),
            )
        for record in plan.projection_records:
            self._execute(
                """
                UPDATE finance.projection_records
                SET is_current = false, projection_status = 'superseded'
                WHERE target_system = %s AND canonical_transaction_id = %s
                  AND is_current AND projection_record_id <> %s
                """,
                (record.target_system, record.canonical_transaction_id, record.id),
            )
            self._execute(
                """
                INSERT INTO finance.projection_records (
                    projection_record_id, projection_run_id, canonical_transaction_id,
                    target_system, target_record_key, projected_hash, projection_status,
                    is_current, effective_at, observed_at, processed_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (projection_record_id) DO NOTHING
                """,
                (
                    record.id, record.run_id, record.canonical_transaction_id,
                    record.target_system, record.target_record_key,
                    record.projected_hash, record.status, record.current,
                    record.effective_at, record.observed_at, record.processed_at,
                ),
            )
        for canonical_id in plan.projection_withdrawals:
            self._execute(
                """
                UPDATE finance.projection_records
                SET is_current = false, projection_status = 'superseded'
                WHERE target_system = 'wealthfolio'
                  AND canonical_transaction_id = %s
                  AND is_current
                """,
                (canonical_id,),
            )
        for event in plan.audit_events:
            self._execute(
                """
                INSERT INTO finance.audit_events (
                    audit_event_id, event_type, actor, subject_type, subject_key,
                    event_data, effective_at, observed_at, processed_at
                ) VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s)
                ON CONFLICT (audit_event_id) DO NOTHING
                """,
                (
                    event.id, event.event_type, event.actor, event.subject_type,
                    event.subject_key, json.dumps(event.data), event.effective_at,
                    event.observed_at, event.processed_at,
                ),
            )
        return self.state() if return_state else None


class PostgresRepository:
    def __init__(self, dsn: str) -> None:
        if not dsn:
            raise ValueError("PostgreSQL DSN is required")
        self.dsn = dsn

    @contextmanager
    def unit_of_work(self, lock_key: str):
        del lock_key
        psycopg, dict_row = _psycopg()
        with psycopg.connect(
            self.dsn, row_factory=dict_row, autocommit=True
        ) as connection:
            connection.execute(
                "SELECT pg_advisory_lock(hashtextextended(%s, 0))",
                (GLOBAL_WRITER_LOCK,),
            )
            try:
                gate_exists = connection.execute(
                    """
                    SELECT to_regclass('finance.writer_gate') IS NOT NULL
                        AS gate_exists
                    """
                ).fetchone()["gate_exists"]
                if gate_exists:
                    gate = connection.execute(
                        """
                        SELECT migrations_blocked
                        FROM finance.writer_gate
                        WHERE singleton
                        """
                    ).fetchone()
                    if gate is None or gate["migrations_blocked"]:
                        raise RuntimeError(
                            "finance shadow writers are blocked for migration"
                        )
                with connection.transaction():
                    connection.execute(
                        "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"
                    )
                    yield _PostgresUnit(connection)
            finally:
                connection.execute(
                    "SELECT pg_advisory_unlock(hashtextextended(%s, 0))",
                    (GLOBAL_WRITER_LOCK,),
                )

    @contextmanager
    def read_snapshot(self):
        psycopg, dict_row = _psycopg()
        with psycopg.connect(self.dsn, row_factory=dict_row) as connection:
            with connection.transaction():
                connection.execute(
                    "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
                )
                yield _PostgresUnit(connection)

    def state(self) -> FinanceState:
        psycopg, dict_row = _psycopg()
        with psycopg.connect(self.dsn, row_factory=dict_row) as connection:
            with connection.transaction():
                connection.execute(
                    "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
                )
                return _PostgresUnit(connection).state()

    def append_decision(self, decision: DurableDecision) -> None:
        with self.unit_of_work(
            f"decision:{decision.subject_type}:{decision.subject_key}"
        ) as unit:
            unit.append_decision(decision)

    def record_app_state(self, snapshot: AppStateSnapshot) -> None:
        with self.unit_of_work(
            f"app-state:{snapshot.app_name}:{snapshot.state_kind}"
        ) as unit:
            unit.record_app_state(snapshot)

    def persist_identity_resolution(
        self, resolution: IdentityResolution
    ) -> PersistedIdentityGeneration:
        """Append a complete canonical identity generation atomically."""

        from .identity_postgres import persist_identity_resolution

        with self.unit_of_work("canonical-transaction-identity") as unit:
            return persist_identity_resolution(unit.connection, resolution)
