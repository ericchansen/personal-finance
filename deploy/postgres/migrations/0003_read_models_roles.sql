BEGIN;

CREATE SCHEMA IF NOT EXISTS finance_read;

CREATE OR REPLACE VIEW finance_read.transaction_provenance AS
SELECT
    ct.canonical_transaction_id,
    ct.canonical_key,
    ct.status AS canonical_status,
    ct.effective_at AS canonical_effective_at,
    ct.amount AS canonical_amount,
    ct.currency_code,
    ca.canonical_account_id,
    ca.display_name AS canonical_account_name,
    tol.link_method,
    tol.is_current AS link_is_current,
    tro.transaction_observation_id,
    tro.source_transaction_id,
    tro.observation_hash,
    tro.effective_at AS source_effective_at,
    tro.observed_at AS source_observed_at,
    tro.processed_at AS source_processed_at,
    tro.amount AS source_amount,
    sa.source_account_id,
    sc.source_system,
    sb.raw_locator,
    sb.content_hash AS source_blob_hash,
    ir.ingestion_run_id,
    ir.importer_name,
    qi.quality_issue_id,
    qi.status AS quality_status
FROM finance.canonical_transactions ct
JOIN finance.canonical_accounts ca USING (canonical_account_id)
LEFT JOIN finance.transaction_observation_links tol
    ON tol.canonical_transaction_id = ct.canonical_transaction_id
LEFT JOIN finance.transaction_observations tro
    ON tro.transaction_observation_id = tol.transaction_observation_id
LEFT JOIN finance.source_accounts sa ON sa.source_account_id = tro.source_account_id
LEFT JOIN finance.source_connections sc ON sc.source_connection_id = sa.source_connection_id
LEFT JOIN finance.source_blobs sb ON sb.source_blob_id = tro.source_blob_id
LEFT JOIN finance.ingestion_runs ir ON ir.ingestion_run_id = tro.ingestion_run_id
LEFT JOIN finance.quality_issues qi ON qi.quality_issue_id = tol.quality_issue_id;

CREATE OR REPLACE VIEW finance_read.pending_history AS
WITH versions AS (
    SELECT
        tro.*,
        row_number() OVER (
            PARTITION BY tro.source_account_id, tro.source_transaction_id
            ORDER BY tro.last_seen_at DESC, tro.observed_at DESC, tro.processed_at DESC
        ) AS observation_version,
        count(*) OVER (
            PARTITION BY tro.source_account_id, tro.source_transaction_id
        ) AS version_count
    FROM finance.transaction_observations tro
)
SELECT
    v.transaction_observation_id,
    v.source_account_id,
    v.source_transaction_id,
    v.source_status,
    v.effective_at,
    v.observed_at,
    v.processed_at,
    v.last_seen_at,
    v.amount,
    v.currency_code,
    v.observation_hash,
    v.observation_version,
    v.version_count,
    tol.canonical_transaction_id,
    (tol.transaction_observation_link_id IS NULL) AS is_unlinked
FROM versions v
LEFT JOIN finance.transaction_observation_links tol
    ON tol.transaction_observation_id = v.transaction_observation_id
   AND tol.is_current
WHERE v.source_status = 'pending'
   OR v.observation_version > 1
   OR tol.transaction_observation_link_id IS NULL;

CREATE OR REPLACE VIEW finance_read.unresolved_duplicates AS
SELECT
    qi.quality_issue_id,
    qi.issue_type,
    qi.severity,
    qi.subject_type,
    qi.subject_key,
    qi.details,
    qi.effective_at,
    qi.observed_at,
    qi.processed_at
FROM finance.quality_issues qi
WHERE qi.issue_type IN ('fuzzy_duplicate', 'unresolved_duplicate')
  AND qi.status = 'open';

CREATE OR REPLACE VIEW finance_read.corrections_decisions AS
SELECT
    d.decision_id,
    d.decision_type,
    d.subject_type,
    d.subject_key,
    d.action,
    d.rationale,
    d.decided_by,
    d.supersedes_decision_id,
    d.effective_at,
    d.observed_at,
    d.processed_at
FROM finance.durable_decisions d
WHERE d.decision_type IN (
    'correction', 'duplicate_resolution', 'account_mapping',
    'account_exclusion', 'transaction_suppression', 'projection_override',
    'quality_issue_resolution', 'trust_cutoff'
);

CREATE OR REPLACE VIEW finance_read.balance_history AS
SELECT
    bo.balance_observation_id,
    bo.source_account_id,
    sc.source_system,
    bo.balance_type,
    bo.effective_at,
    bo.observed_at,
    bo.processed_at,
    bo.amount,
    bo.currency_code,
    bo.observation_hash,
    sb.raw_locator,
    sb.content_hash AS source_blob_hash,
    sa.trust_cutoff_at,
    bo.quality_issue_id
FROM finance.balance_observations bo
JOIN finance.source_accounts sa USING (source_account_id)
JOIN finance.source_connections sc USING (source_connection_id)
JOIN finance.source_blobs sb USING (source_blob_id);

CREATE OR REPLACE VIEW finance_read.stale_trust_cutoff_issues AS
SELECT
    bo.balance_observation_id AS observation_id,
    'balance'::text AS observation_type,
    bo.source_account_id,
    bo.effective_at,
    sa.trust_cutoff_at,
    bo.quality_issue_id,
    qi.status AS quality_issue_status,
    CASE
        WHEN qi.quality_issue_id IS NULL THEN 'missing_quality_issue'
        WHEN qi.issue_type <> 'stale_after_trust_cutoff' THEN 'wrong_quality_issue_type'
        ELSE qi.status
    END AS review_status
FROM finance.balance_observations bo
JOIN finance.source_accounts sa USING (source_account_id)
LEFT JOIN finance.quality_issues qi
  ON qi.quality_issue_id = bo.quality_issue_id
  OR (
      qi.issue_type = 'stale_after_trust_cutoff'
      AND qi.subject_type = 'balance_observation'
      AND qi.subject_key = bo.balance_observation_id::text
  )
WHERE sa.trust_cutoff_at IS NOT NULL
  AND bo.effective_at > sa.trust_cutoff_at
UNION ALL
SELECT
    tro.transaction_observation_id AS observation_id,
    'transaction'::text AS observation_type,
    tro.source_account_id,
    tro.effective_at,
    sa.trust_cutoff_at,
    tro.quality_issue_id,
    qi.status AS quality_issue_status,
    CASE
        WHEN qi.quality_issue_id IS NULL THEN 'missing_quality_issue'
        WHEN qi.issue_type <> 'stale_after_trust_cutoff' THEN 'wrong_quality_issue_type'
        ELSE qi.status
    END AS review_status
FROM finance.transaction_observations tro
JOIN finance.source_accounts sa USING (source_account_id)
LEFT JOIN finance.quality_issues qi
  ON qi.quality_issue_id = tro.quality_issue_id
  OR (
      qi.issue_type = 'stale_after_trust_cutoff'
      AND qi.subject_type = 'transaction_observation'
      AND qi.subject_key = tro.transaction_observation_id::text
  )
WHERE sa.trust_cutoff_at IS NOT NULL
  AND tro.effective_at > sa.trust_cutoff_at;

CREATE OR REPLACE VIEW finance_read.source_canonical_projection_comparison AS
SELECT
    tro.transaction_observation_id,
    tro.source_account_id,
    tro.source_transaction_id,
    tro.amount AS source_amount,
    tro.currency_code AS source_currency,
    tro.effective_at AS source_effective_at,
    ct.canonical_transaction_id,
    ct.amount AS canonical_amount,
    ct.currency_code AS canonical_currency,
    ct.effective_at AS canonical_effective_at,
    ct.status AS canonical_status,
    pr.projection_record_id,
    pr.projection_run_id,
    pr.target_system,
    pr.target_record_key,
    pr.projected_hash,
    pr.projection_status,
    pr.is_current AS projection_is_current,
    (tro.amount IS DISTINCT FROM ct.amount
        OR tro.currency_code IS DISTINCT FROM ct.currency_code
        OR tro.effective_at IS DISTINCT FROM ct.effective_at) AS source_canonical_differs
FROM finance.transaction_observations tro
LEFT JOIN finance.transaction_observation_links tol
    ON tol.transaction_observation_id = tro.transaction_observation_id
   AND tol.is_current
LEFT JOIN finance.canonical_transactions ct
    ON ct.canonical_transaction_id = tol.canonical_transaction_id
LEFT JOIN finance.projection_records pr
    ON pr.canonical_transaction_id = ct.canonical_transaction_id
   AND pr.is_current;

CREATE OR REPLACE VIEW finance_read.migration_status AS
SELECT version, name, checksum, applied_at
FROM finance.schema_migrations;

CREATE OR REPLACE VIEW finance_read.source_account_mapping_history AS
SELECT
    sal.source_account_id,
    sc.source_system,
    sa.external_account_id,
    sal.canonical_account_id,
    ca.canonical_key,
    sal.decision_id,
    sal.effective_from,
    sal.effective_to,
    sal.observed_at,
    sal.processed_at,
    (sal.effective_to IS NULL) AS is_current
FROM finance.source_account_links sal
JOIN finance.source_accounts sa USING (source_account_id)
JOIN finance.source_connections sc USING (source_connection_id)
JOIN finance.canonical_accounts ca USING (canonical_account_id);

DO $do$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'finance_readonly') THEN
        CREATE ROLE finance_readonly NOLOGIN;
    END IF;
END;
$do$;

ALTER ROLE finance_readonly SET default_transaction_read_only = on;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA finance_read TO finance_readonly;
GRANT SELECT ON ALL TABLES IN SCHEMA finance_read TO finance_readonly;
ALTER DEFAULT PRIVILEGES IN SCHEMA finance_read
    GRANT SELECT ON TABLES TO finance_readonly;

DO $do$
BEGIN
    EXECUTE format(
        'GRANT CONNECT ON DATABASE %I TO finance_readonly',
        current_database()
    );
END;
$do$;

INSERT INTO finance.schema_migrations(version, name, checksum)
VALUES (:'migration_version', :'migration_name', :'migration_checksum')
ON CONFLICT (version) DO NOTHING;

COMMIT;
