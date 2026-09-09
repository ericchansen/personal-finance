BEGIN;

ALTER TABLE finance.source_blobs
    ADD COLUMN IF NOT EXISTS source_kind text NOT NULL DEFAULT 'legacy-candidate',
    ADD COLUMN IF NOT EXISTS source_version text NOT NULL DEFAULT 'unknown';

ALTER TABLE finance.ingestion_runs
    ADD COLUMN IF NOT EXISTS parser_hash text,
    ADD COLUMN IF NOT EXISTS source_protocol text,
    ADD COLUMN IF NOT EXISTS source_version text,
    ADD COLUMN IF NOT EXISTS overlap_start timestamptz,
    ADD COLUMN IF NOT EXISTS overlap_end timestamptz,
    ADD COLUMN IF NOT EXISTS sealed_plan_hash text;

DO $do$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'ingestion_runs_parser_hash_check'
          AND conrelid = 'finance.ingestion_runs'::regclass
    ) THEN
        ALTER TABLE finance.ingestion_runs
            ADD CONSTRAINT ingestion_runs_parser_hash_check
            CHECK (parser_hash IS NULL OR parser_hash ~ '^[0-9a-f]{64}$');
    END IF;
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'ingestion_runs_plan_hash_check'
          AND conrelid = 'finance.ingestion_runs'::regclass
    ) THEN
        ALTER TABLE finance.ingestion_runs
            ADD CONSTRAINT ingestion_runs_plan_hash_check
            CHECK (
                sealed_plan_hash IS NULL
                OR sealed_plan_hash ~ '^[0-9a-f]{64}$'
            );
    END IF;
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'ingestion_runs_overlap_check'
          AND conrelid = 'finance.ingestion_runs'::regclass
    ) THEN
        ALTER TABLE finance.ingestion_runs
            ADD CONSTRAINT ingestion_runs_overlap_check
            CHECK (
                overlap_end IS NULL
                OR overlap_start IS NULL
                OR overlap_end >= overlap_start
            );
    END IF;
END;
$do$;

CREATE TABLE IF NOT EXISTS finance.shadow_authority_metadata (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    instance_id uuid NOT NULL DEFAULT gen_random_uuid() UNIQUE,
    environment_marker text NOT NULL CHECK (btrim(environment_marker) <> ''),
    authority_mode text NOT NULL DEFAULT 'shadow'
        CHECK (authority_mode = 'shadow'),
    wealthfolio_mutation_enabled boolean NOT NULL DEFAULT false
        CHECK (NOT wealthfolio_mutation_enabled),
    cutover_authorized boolean NOT NULL DEFAULT false
        CHECK (NOT cutover_authorized),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE OR REPLACE FUNCTION pg_temp.ensure_shadow_environment(
    requested_marker text
)
RETURNS void
LANGUAGE plpgsql
AS $function$
DECLARE
    existing_marker text;
BEGIN
    SELECT environment_marker
    INTO existing_marker
    FROM finance.shadow_authority_metadata
    WHERE singleton;

    IF existing_marker IS NULL THEN
        INSERT INTO finance.shadow_authority_metadata (
            singleton, environment_marker
        ) VALUES (true, requested_marker);
    ELSIF existing_marker IS DISTINCT FROM requested_marker THEN
        RAISE EXCEPTION
            'shadow environment marker mismatch'
            USING ERRCODE = '55000';
    END IF;
END;
$function$;

SELECT pg_temp.ensure_shadow_environment(:'shadow_environment');

DROP TRIGGER IF EXISTS shadow_authority_metadata_append_only
ON finance.shadow_authority_metadata;
CREATE TRIGGER shadow_authority_metadata_append_only
BEFORE UPDATE OR DELETE ON finance.shadow_authority_metadata
FOR EACH ROW EXECUTE FUNCTION finance.reject_append_only_change();

CREATE TABLE IF NOT EXISTS finance.artifact_observations (
    artifact_observation_id uuid PRIMARY KEY,
    ingestion_run_id uuid NOT NULL
        REFERENCES finance.ingestion_runs(ingestion_run_id),
    source_blob_id uuid NOT NULL
        REFERENCES finance.source_blobs(source_blob_id),
    observation_kind text NOT NULL CHECK (btrim(observation_kind) <> ''),
    source_identity_hash text NOT NULL
        CHECK (source_identity_hash ~ '^[0-9a-f]{64}$'),
    observation_hash text NOT NULL
        CHECK (observation_hash ~ '^[0-9a-f]{64}$'),
    record_index bigint NOT NULL CHECK (record_index >= 0),
    effective_at timestamptz,
    observed_at timestamptz NOT NULL,
    processed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    quality_issue_id uuid REFERENCES finance.quality_issues(quality_issue_id),
    UNIQUE (source_blob_id, observation_kind, source_identity_hash, observation_hash),
    CHECK (processed_at >= observed_at)
);

DROP TRIGGER IF EXISTS artifact_observations_append_only
ON finance.artifact_observations;
CREATE TRIGGER artifact_observations_append_only
BEFORE UPDATE OR DELETE ON finance.artifact_observations
FOR EACH ROW EXECUTE FUNCTION finance.reject_append_only_change();

CREATE TABLE IF NOT EXISTS finance.shadow_plans (
    plan_hash text PRIMARY KEY CHECK (plan_hash ~ '^[0-9a-f]{64}$'),
    input_set_hash text NOT NULL CHECK (input_set_hash ~ '^[0-9a-f]{64}$'),
    migration_set_hash text NOT NULL CHECK (migration_set_hash ~ '^[0-9a-f]{64}$'),
    starting_state_hash text NOT NULL CHECK (starting_state_hash ~ '^[0-9a-f]{64}$'),
    expected_state_hash text NOT NULL CHECK (expected_state_hash ~ '^[0-9a-f]{64}$'),
    environment_marker text NOT NULL,
    source_count bigint NOT NULL CHECK (source_count >= 0),
    observation_count bigint NOT NULL CHECK (observation_count >= 0),
    issue_count bigint NOT NULL CHECK (issue_count >= 0),
    created_at timestamptz NOT NULL
);

DROP TRIGGER IF EXISTS shadow_plans_append_only ON finance.shadow_plans;
CREATE TRIGGER shadow_plans_append_only
BEFORE UPDATE OR DELETE ON finance.shadow_plans
FOR EACH ROW EXECUTE FUNCTION finance.reject_append_only_change();

CREATE TABLE IF NOT EXISTS finance.shadow_run_events (
    shadow_run_event_id uuid PRIMARY KEY,
    plan_hash text REFERENCES finance.shadow_plans(plan_hash),
    event_type text NOT NULL CHECK (event_type IN (
        'plan-recorded', 'apply-started', 'apply-succeeded', 'apply-failed',
        'drift-recorded', 'backup-verified', 'restore-rehearsed'
    )),
    safe_counts jsonb NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(safe_counts) = 'object'),
    occurred_at timestamptz NOT NULL,
    event_hash text NOT NULL CHECK (event_hash ~ '^[0-9a-f]{64}$')
);

DROP TRIGGER IF EXISTS shadow_run_events_append_only
ON finance.shadow_run_events;
CREATE TRIGGER shadow_run_events_append_only
BEFORE UPDATE OR DELETE ON finance.shadow_run_events
FOR EACH ROW EXECUTE FUNCTION finance.reject_append_only_change();

CREATE TABLE IF NOT EXISTS finance.lineage_review_groups (
    lineage_group_id text PRIMARY KEY CHECK (btrim(lineage_group_id) <> ''),
    candidate_hash text NOT NULL CHECK (candidate_hash ~ '^[0-9a-f]{64}$'),
    audit_graph_hash text NOT NULL CHECK (audit_graph_hash ~ '^[0-9a-f]{64}$'),
    evidence_set_hash text NOT NULL CHECK (evidence_set_hash ~ '^[0-9a-f]{64}$'),
    member_count integer NOT NULL CHECK (member_count > 1),
    observed_at timestamptz NOT NULL,
    source_blob_id uuid NOT NULL REFERENCES finance.source_blobs(source_blob_id),
    quality_issue_id uuid NOT NULL REFERENCES finance.quality_issues(quality_issue_id),
    UNIQUE (candidate_hash, audit_graph_hash)
);

DROP TRIGGER IF EXISTS lineage_review_groups_append_only
ON finance.lineage_review_groups;
CREATE TRIGGER lineage_review_groups_append_only
BEFORE UPDATE OR DELETE ON finance.lineage_review_groups
FOR EACH ROW EXECUTE FUNCTION finance.reject_append_only_change();

CREATE TABLE IF NOT EXISTS finance.lineage_review_decisions (
    lineage_decision_id uuid PRIMARY KEY,
    lineage_group_id text NOT NULL
        REFERENCES finance.lineage_review_groups(lineage_group_id),
    decision_version integer NOT NULL CHECK (decision_version > 0),
    candidate_hash text NOT NULL CHECK (candidate_hash ~ '^[0-9a-f]{64}$'),
    audit_graph_hash text NOT NULL CHECK (audit_graph_hash ~ '^[0-9a-f]{64}$'),
    evidence_set_hash text NOT NULL CHECK (evidence_set_hash ~ '^[0-9a-f]{64}$'),
    outcome text NOT NULL CHECK (outcome IN (
        'distinct-economic-events', 'duplicate-economic-event',
        'transfer', 'insufficient-evidence'
    )),
    survivor_identity_hash text
        CHECK (
            survivor_identity_hash IS NULL
            OR survivor_identity_hash ~ '^[0-9a-f]{64}$'
        ),
    rationale_hash text NOT NULL CHECK (rationale_hash ~ '^[0-9a-f]{64}$'),
    decided_at timestamptz NOT NULL,
    observed_at timestamptz NOT NULL,
    UNIQUE (lineage_group_id, decision_version),
    CHECK (
        outcome <> 'duplicate-economic-event'
        OR survivor_identity_hash IS NOT NULL
    )
);

DROP TRIGGER IF EXISTS lineage_review_decisions_append_only
ON finance.lineage_review_decisions;
CREATE TRIGGER lineage_review_decisions_append_only
BEFORE UPDATE OR DELETE ON finance.lineage_review_decisions
FOR EACH ROW EXECUTE FUNCTION finance.reject_append_only_change();

CREATE OR REPLACE FUNCTION finance.validate_lineage_review_decision()
RETURNS trigger
LANGUAGE plpgsql
AS $function$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM finance.lineage_review_groups group_record
        WHERE group_record.lineage_group_id = NEW.lineage_group_id
          AND group_record.candidate_hash = NEW.candidate_hash
          AND group_record.audit_graph_hash = NEW.audit_graph_hash
          AND group_record.evidence_set_hash = NEW.evidence_set_hash
    ) THEN
        RAISE EXCEPTION
            'lineage decision is not bound to the exact reviewed evidence'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$function$;

DROP TRIGGER IF EXISTS lineage_review_decisions_require_evidence
ON finance.lineage_review_decisions;
CREATE TRIGGER lineage_review_decisions_require_evidence
BEFORE INSERT ON finance.lineage_review_decisions
FOR EACH ROW EXECUTE FUNCTION finance.validate_lineage_review_decision();

CREATE OR REPLACE VIEW finance_read.shadow_status AS
SELECT
    metadata.instance_id,
    metadata.environment_marker,
    metadata.authority_mode,
    metadata.wealthfolio_mutation_enabled,
    metadata.cutover_authorized,
    (SELECT count(*) FROM finance.schema_migrations) AS migration_count,
    (SELECT count(*) FROM finance.source_blobs) AS source_blob_count,
    (
        (SELECT count(*) FROM finance.transaction_observations)
        + (SELECT count(*) FROM finance.balance_observations)
        + (SELECT count(*) FROM finance.position_observations)
        + (SELECT count(*) FROM finance.valuation_observations)
        + (SELECT count(*) FROM finance.artifact_observations)
    ) AS observation_count,
    (
        SELECT count(*)
        FROM finance.quality_issues
        WHERE status = 'open'
    ) AS open_issue_count,
    (
        SELECT max(occurred_at)
        FROM finance.shadow_run_events
        WHERE event_type = 'apply-succeeded'
    ) AS last_success_at,
    (
        SELECT max(occurred_at)
        FROM finance.shadow_run_events
        WHERE event_type = 'apply-failed'
    ) AS last_failure_at
FROM finance.shadow_authority_metadata metadata
WHERE metadata.singleton;

CREATE OR REPLACE VIEW finance_read.shadow_source_inventory AS
SELECT
    source_kind,
    source_version,
    count(*) AS blob_count,
    count(DISTINCT content_hash) AS distinct_blob_count,
    min(observed_at) AS first_observed_at,
    max(observed_at) AS last_observed_at
FROM finance.source_blobs
GROUP BY source_kind, source_version;

CREATE OR REPLACE VIEW finance_read.shadow_artifact_observations AS
SELECT
    artifact_observation_id,
    ingestion_run_id,
    source_blob_id,
    observation_kind,
    source_identity_hash,
    observation_hash,
    record_index,
    effective_at,
    observed_at,
    processed_at,
    payload,
    quality_issue_id
FROM finance.artifact_observations;

CREATE OR REPLACE VIEW finance_read.shadow_lineage_status AS
SELECT
    group_record.lineage_group_id,
    group_record.candidate_hash,
    group_record.audit_graph_hash,
    group_record.evidence_set_hash,
    group_record.member_count,
    issue.status AS issue_status,
    latest.outcome,
    latest.decision_version,
    latest.decided_at,
    CASE
        WHEN latest.lineage_decision_id IS NULL THEN 'review-required'
        WHEN latest.outcome = 'insufficient-evidence' THEN 'review-required'
        ELSE 'evidence-bound-decision'
    END AS review_status
FROM finance.lineage_review_groups group_record
JOIN finance.quality_issues issue
  ON issue.quality_issue_id = group_record.quality_issue_id
LEFT JOIN LATERAL (
    SELECT decision.*
    FROM finance.lineage_review_decisions decision
    WHERE decision.lineage_group_id = group_record.lineage_group_id
    ORDER BY decision.decision_version DESC
    LIMIT 1
) latest ON true;

DO $do$
DECLARE
    role_name text;
BEGIN
    FOREACH role_name IN ARRAY ARRAY[
        'finance_shadow_ingest',
        'finance_shadow_backup',
        'finance_shadow_agent_readonly'
    ] LOOP
        IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = role_name) THEN
            EXECUTE format(
                'CREATE ROLE %I NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION',
                role_name
            );
        END IF;
    END LOOP;
END;
$do$;

ALTER ROLE finance_shadow_ingest SET search_path = finance, pg_catalog;
ALTER ROLE finance_shadow_agent_readonly SET default_transaction_read_only = on;
GRANT finance_readonly TO finance_shadow_agent_readonly;
GRANT finance_readonly TO finance_shadow_ingest;
GRANT pg_read_all_data TO finance_shadow_backup;

GRANT USAGE ON SCHEMA finance TO finance_shadow_ingest;
GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA finance
TO finance_shadow_ingest;
REVOKE DELETE, TRUNCATE, REFERENCES, TRIGGER
ON ALL TABLES IN SCHEMA finance
FROM finance_shadow_ingest;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA finance
TO finance_shadow_ingest;
ALTER DEFAULT PRIVILEGES IN SCHEMA finance
    GRANT SELECT, INSERT, UPDATE ON TABLES TO finance_shadow_ingest;
ALTER DEFAULT PRIVILEGES IN SCHEMA finance
    GRANT USAGE, SELECT ON SEQUENCES TO finance_shadow_ingest;

GRANT SELECT ON
    finance_read.shadow_status,
    finance_read.shadow_source_inventory,
    finance_read.shadow_artifact_observations,
    finance_read.shadow_lineage_status
TO finance_readonly;

INSERT INTO finance.schema_migrations(version, name, checksum)
VALUES (:'migration_version', :'migration_name', :'migration_checksum')
ON CONFLICT (version) DO NOTHING;

COMMIT;
