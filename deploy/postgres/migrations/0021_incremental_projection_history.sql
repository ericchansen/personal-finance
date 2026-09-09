BEGIN;

CREATE VIEW finance_read.incremental_projection_history AS
SELECT run.scope_id, outbox.run_hash, outbox.operation_id,
       outbox.accepted_event_id, outbox.revision_number, outbox.operation_kind,
       qualification.source_day, qualification.description, qualification.currency_code,
       qualification.signed_amount AS source_amount,
       (outbox.operation_document->>'delta')::numeric AS planned_cash_effect,
       run.source_observed_at, run.balance_effective_at, run.receipt_hash,
       run.snapshot_hash, run.manifest_hash, run.policy_hash,
       run_event.state AS run_state, run_event.evidence AS run_evidence,
       COALESCE(attempt.state, CASE WHEN run_event.state='held' THEN 'held'
                                  ELSE 'pending' END) AS operation_state,
       attempt.processed_at AS attempt_recorded_at,
       attempt.evidence->'observed'->>'id' AS observed_activity_id,
       attempt.evidence AS operation_evidence,
       qualification.proof AS source_proof,
       outbox.operation_document->'anchorTransition' AS source_anchor_transition
FROM finance.incremental_outbox outbox
JOIN finance.incremental_qualifications qualification
  ON qualification.qualification_id=outbox.qualification_id
JOIN finance.incremental_runs run ON run.run_hash=outbox.run_hash
LEFT JOIN LATERAL (
    SELECT state,evidence,processed_at FROM finance.incremental_attempts entry
    WHERE entry.operation_id=outbox.operation_id
    ORDER BY entry.event_number DESC LIMIT 1
) attempt ON true
LEFT JOIN LATERAL (
    SELECT state,evidence FROM finance.incremental_run_events entry
    WHERE entry.run_hash=outbox.run_hash
    ORDER BY entry.event_number DESC LIMIT 1
) run_event ON true;

GRANT SELECT ON finance_read.incremental_projection_history
TO finance_readonly, finance_shadow_ingest;

INSERT INTO finance.schema_migrations(version,name,checksum)
VALUES (:'migration_version',:'migration_name',:'migration_checksum')
ON CONFLICT (version) DO NOTHING;
COMMIT;
