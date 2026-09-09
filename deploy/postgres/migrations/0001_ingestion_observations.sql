BEGIN;

CREATE SCHEMA IF NOT EXISTS finance;

CREATE TABLE IF NOT EXISTS finance.schema_migrations (
    version text PRIMARY KEY CHECK (version ~ '^[0-9]+$'),
    name text NOT NULL,
    checksum text NOT NULL CHECK (checksum ~ '^[0-9a-f]{64}$'),
    applied_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE OR REPLACE FUNCTION finance.reject_immutable_columns()
RETURNS trigger
LANGUAGE plpgsql
AS $function$
DECLARE
    column_name text;
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION '%.% is immutable evidence', TG_TABLE_SCHEMA, TG_TABLE_NAME
            USING ERRCODE = '55000';
    END IF;
    FOREACH column_name IN ARRAY TG_ARGV LOOP
        IF to_jsonb(NEW) -> column_name IS DISTINCT FROM to_jsonb(OLD) -> column_name THEN
            RAISE EXCEPTION '% is immutable on %.%', column_name, TG_TABLE_SCHEMA, TG_TABLE_NAME
                USING ERRCODE = '55000';
        END IF;

    END LOOP;
    RETURN NEW;
END;
$function$;

CREATE OR REPLACE FUNCTION finance.reject_append_only_change()
RETURNS trigger
LANGUAGE plpgsql
AS $function$
BEGIN
    RAISE EXCEPTION '%.% is append-only', TG_TABLE_SCHEMA, TG_TABLE_NAME
        USING ERRCODE = '55000';
END;
$function$;

CREATE TABLE IF NOT EXISTS finance.durable_decisions (
    decision_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    decision_type text NOT NULL CHECK (decision_type IN (
        'account_mapping', 'account_exclusion', 'transaction_suppression',
        'duplicate_resolution', 'correction', 'trust_cutoff', 'projection_override',
        'quality_issue_resolution'
    )),
    subject_type text NOT NULL,
    subject_key text NOT NULL CHECK (btrim(subject_key) <> ''),
    action text NOT NULL CHECK (btrim(action) <> ''),
    rationale text NOT NULL CHECK (btrim(rationale) <> ''),
    decided_by text NOT NULL CHECK (btrim(decided_by) <> ''),
    supersedes_decision_id uuid REFERENCES finance.durable_decisions(decision_id),
    effective_at timestamptz NOT NULL,
    observed_at timestamptz NOT NULL,
    processed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK (processed_at >= observed_at),
    CHECK (supersedes_decision_id IS NULL OR supersedes_decision_id <> decision_id)
);

DROP TRIGGER IF EXISTS durable_decisions_append_only ON finance.durable_decisions;
CREATE TRIGGER durable_decisions_append_only
BEFORE UPDATE OR DELETE ON finance.durable_decisions
FOR EACH ROW EXECUTE FUNCTION finance.reject_append_only_change();

CREATE TABLE IF NOT EXISTS finance.source_blobs (
    source_blob_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    raw_locator text NOT NULL CHECK (btrim(raw_locator) <> ''),
    content_hash text NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    hash_algorithm text NOT NULL DEFAULT 'sha256' CHECK (hash_algorithm = 'sha256'),
    media_type text,
    byte_size bigint CHECK (byte_size IS NULL OR byte_size >= 0),
    source_effective_start timestamptz,
    source_effective_end timestamptz,
    observed_at timestamptz NOT NULL,
    processed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (raw_locator, content_hash),
    CHECK (source_effective_end IS NULL OR source_effective_start IS NULL
        OR source_effective_end >= source_effective_start),
    CHECK (processed_at >= observed_at)
);

DROP TRIGGER IF EXISTS source_blobs_immutable_identity ON finance.source_blobs;
CREATE TRIGGER source_blobs_immutable_identity
BEFORE UPDATE OR DELETE ON finance.source_blobs
FOR EACH ROW EXECUTE FUNCTION finance.reject_immutable_columns(
    'raw_locator', 'content_hash', 'hash_algorithm', 'byte_size'
);

CREATE TABLE IF NOT EXISTS finance.ingestion_runs (
    ingestion_run_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    source_blob_id uuid NOT NULL REFERENCES finance.source_blobs(source_blob_id),
    importer_name text NOT NULL,
    importer_version text NOT NULL,
    run_status text NOT NULL CHECK (run_status IN ('started', 'succeeded', 'failed', 'partial')),
    effective_start timestamptz,
    effective_end timestamptz,
    observed_at timestamptz NOT NULL,
    processed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    finished_at timestamptz,
    records_seen bigint NOT NULL DEFAULT 0 CHECK (records_seen >= 0),
    records_accepted bigint NOT NULL DEFAULT 0 CHECK (records_accepted >= 0),
    CHECK (effective_end IS NULL OR effective_start IS NULL OR effective_end >= effective_start),
    CHECK (processed_at >= observed_at),
    CHECK (finished_at IS NULL OR finished_at >= processed_at),
    CHECK (records_accepted <= records_seen)
);

CREATE TABLE IF NOT EXISTS finance.source_connections (
    source_connection_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    source_system text NOT NULL CHECK (btrim(source_system) <> ''),
    connection_key text NOT NULL CHECK (btrim(connection_key) <> ''),
    secret_reference text,
    status text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'paused', 'retired')),
    effective_from timestamptz NOT NULL,
    effective_to timestamptz,
    observed_at timestamptz NOT NULL,
    processed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (source_system, connection_key),
    CHECK (effective_to IS NULL OR effective_to >= effective_from),
    CHECK (processed_at >= observed_at)
);

CREATE TABLE IF NOT EXISTS finance.source_accounts (
    source_account_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    source_connection_id uuid NOT NULL REFERENCES finance.source_connections(source_connection_id),
    external_account_id text NOT NULL CHECK (btrim(external_account_id) <> ''),
    source_account_name text,
    account_type text,
    currency_code text CHECK (currency_code IS NULL OR currency_code ~ '^[A-Z]{3}$'),
    status text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'closed', 'unknown')),
    trust_cutoff_at timestamptz,
    trust_cutoff_decision_id uuid REFERENCES finance.durable_decisions(decision_id),
    effective_from timestamptz NOT NULL,
    effective_to timestamptz,
    observed_at timestamptz NOT NULL,
    processed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (source_connection_id, external_account_id),
    CHECK (effective_to IS NULL OR effective_to >= effective_from),
    CHECK (processed_at >= observed_at)
);

CREATE OR REPLACE FUNCTION finance.validate_trust_cutoff_decision()
RETURNS trigger
LANGUAGE plpgsql
AS $function$
BEGIN
    IF TG_OP = 'UPDATE'
       AND OLD.trust_cutoff_at IS NOT NULL
       AND NEW.trust_cutoff_at IS NULL
       AND NOT EXISTS (
           SELECT 1
           FROM finance.durable_decisions d
           WHERE d.decision_id = NEW.trust_cutoff_decision_id
             AND d.decision_type = 'trust_cutoff'
             AND d.subject_type = 'source_account'
             AND d.subject_key = NEW.source_account_id::text
             AND d.action = 'clear_cutoff'
       )
    THEN
        RAISE EXCEPTION 'clearing trust cutoff requires a matching clear decision'
            USING ERRCODE = '23514';
    END IF;

    IF NEW.trust_cutoff_at IS NOT NULL AND NOT EXISTS (
        SELECT 1
        FROM finance.durable_decisions d
        WHERE d.decision_id = NEW.trust_cutoff_decision_id
          AND d.decision_type = 'trust_cutoff'
          AND d.subject_type = 'source_account'
          AND d.subject_key = NEW.source_account_id::text
          AND d.action = 'set_cutoff'
    ) THEN
        RAISE EXCEPTION 'trust cutoff requires a matching durable decision'
            USING ERRCODE = '23514';
    END IF;

    IF NEW.trust_cutoff_at IS NOT NULL AND EXISTS (
        SELECT 1
        FROM finance.balance_observations bo
        WHERE bo.source_account_id = NEW.source_account_id
          AND bo.effective_at > NEW.trust_cutoff_at
          AND NOT EXISTS (
              SELECT 1
              FROM finance.quality_issues qi
              WHERE qi.issue_type = 'stale_after_trust_cutoff'
                AND qi.subject_type = 'balance_observation'
                AND qi.subject_key = bo.balance_observation_id::text
          )
    ) THEN
        RAISE EXCEPTION 'trust cutoff would leave stale balances without matching issues'
            USING ERRCODE = '23514';
    END IF;

    IF NEW.trust_cutoff_at IS NOT NULL AND EXISTS (
        SELECT 1
        FROM finance.transaction_observations tro
        WHERE tro.source_account_id = NEW.source_account_id
          AND tro.effective_at > NEW.trust_cutoff_at
          AND NOT EXISTS (
              SELECT 1
              FROM finance.quality_issues qi
              WHERE qi.issue_type = 'stale_after_trust_cutoff'
                AND qi.subject_type = 'transaction_observation'
                AND qi.subject_key = tro.transaction_observation_id::text
          )
    ) THEN
        RAISE EXCEPTION 'trust cutoff would leave stale transactions without matching issues'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$function$;

DROP TRIGGER IF EXISTS source_accounts_require_trust_decision ON finance.source_accounts;
CREATE TRIGGER source_accounts_require_trust_decision
BEFORE INSERT OR UPDATE OF trust_cutoff_at, trust_cutoff_decision_id
ON finance.source_accounts
FOR EACH ROW EXECUTE FUNCTION finance.validate_trust_cutoff_decision();

CREATE TABLE IF NOT EXISTS finance.quality_issues (
    quality_issue_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    issue_type text NOT NULL CHECK (issue_type IN (
        'fuzzy_duplicate', 'unresolved_duplicate', 'stale_after_trust_cutoff',
        'identity_conflict', 'invalid_observation', 'projection_mismatch',
        'missing_history', 'other'
    )),
    severity text NOT NULL CHECK (severity IN ('info', 'warning', 'error')),
    subject_type text NOT NULL,
    subject_key text NOT NULL CHECK (btrim(subject_key) <> ''),
    details jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(details) = 'object'),
    status text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'resolved', 'dismissed')),
    resolution_decision_id uuid REFERENCES finance.durable_decisions(decision_id),
    effective_at timestamptz NOT NULL,
    observed_at timestamptz NOT NULL,
    processed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK ((status = 'open' AND resolution_decision_id IS NULL)
        OR (status IN ('resolved', 'dismissed') AND resolution_decision_id IS NOT NULL)),
    CHECK (processed_at >= observed_at)
);

CREATE TABLE IF NOT EXISTS finance.transaction_observations (
    transaction_observation_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    ingestion_run_id uuid NOT NULL REFERENCES finance.ingestion_runs(ingestion_run_id),
    source_blob_id uuid NOT NULL REFERENCES finance.source_blobs(source_blob_id),
    source_account_id uuid NOT NULL REFERENCES finance.source_accounts(source_account_id),
    source_transaction_id text NOT NULL CHECK (btrim(source_transaction_id) <> ''),
    observation_hash text NOT NULL CHECK (observation_hash ~ '^[0-9a-f]{64}$'),
    effective_at timestamptz NOT NULL,
    posted_at timestamptz,
    observed_at timestamptz NOT NULL,
    processed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    amount numeric(24, 8) NOT NULL,
    currency_code text NOT NULL CHECK (currency_code ~ '^[A-Z]{3}$'),
    description text,
    source_status text NOT NULL CHECK (source_status IN ('pending', 'posted')),
    quality_issue_id uuid REFERENCES finance.quality_issues(quality_issue_id),
    last_seen_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    sighting_order integer NOT NULL DEFAULT 0 CHECK (sighting_order >= 0),
    last_seen_run_id uuid NOT NULL REFERENCES finance.ingestion_runs(ingestion_run_id),
    last_seen_order integer NOT NULL DEFAULT 0 CHECK (last_seen_order >= 0),
    UNIQUE (source_account_id, source_transaction_id, observation_hash),
    CHECK (processed_at >= observed_at),
    CHECK (last_seen_at >= observed_at),
    CHECK (amount NOT IN ('NaN'::numeric, 'Infinity'::numeric, '-Infinity'::numeric))
);

CREATE INDEX IF NOT EXISTS transaction_observations_source_identity_idx
ON finance.transaction_observations(
    source_account_id, source_transaction_id, last_seen_at DESC,
    last_seen_run_id DESC, last_seen_order DESC
);

CREATE OR REPLACE FUNCTION finance.validate_transaction_trust_cutoff()
RETURNS trigger
LANGUAGE plpgsql
AS $function$
DECLARE
    cutoff timestamptz;
BEGIN
    SELECT trust_cutoff_at INTO cutoff
    FROM finance.source_accounts
    WHERE source_account_id = NEW.source_account_id;

    IF cutoff IS NOT NULL AND NEW.effective_at > cutoff AND NOT EXISTS (
        SELECT 1
        FROM finance.quality_issues qi
        WHERE qi.quality_issue_id = NEW.quality_issue_id
          AND qi.issue_type = 'stale_after_trust_cutoff'
          AND qi.subject_type = 'transaction_observation'
          AND qi.subject_key = NEW.transaction_observation_id::text
    ) THEN
        RAISE EXCEPTION 'post-cutoff transaction requires a matching stale quality issue'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$function$;

DROP TRIGGER IF EXISTS transaction_observations_require_stale_issue
ON finance.transaction_observations;
CREATE TRIGGER transaction_observations_require_stale_issue
BEFORE INSERT OR UPDATE OF source_account_id, effective_at, quality_issue_id
ON finance.transaction_observations
FOR EACH ROW EXECUTE FUNCTION finance.validate_transaction_trust_cutoff();

CREATE TABLE IF NOT EXISTS finance.balance_observations (
    balance_observation_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    ingestion_run_id uuid NOT NULL REFERENCES finance.ingestion_runs(ingestion_run_id),
    source_blob_id uuid NOT NULL REFERENCES finance.source_blobs(source_blob_id),
    source_account_id uuid NOT NULL REFERENCES finance.source_accounts(source_account_id),
    observation_hash text NOT NULL CHECK (observation_hash ~ '^[0-9a-f]{64}$'),
    balance_type text NOT NULL CHECK (balance_type IN ('current', 'available', 'ledger', 'market')),
    effective_at timestamptz NOT NULL,
    observed_at timestamptz NOT NULL,
    processed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    amount numeric(24, 8) NOT NULL,
    currency_code text NOT NULL CHECK (currency_code ~ '^[A-Z]{3}$'),
    quality_issue_id uuid REFERENCES finance.quality_issues(quality_issue_id),
    UNIQUE (source_account_id, balance_type, effective_at, observation_hash),
    CHECK (processed_at >= observed_at),
    CHECK (amount NOT IN ('NaN'::numeric, 'Infinity'::numeric, '-Infinity'::numeric))
);

CREATE OR REPLACE FUNCTION finance.validate_balance_trust_cutoff()
RETURNS trigger
LANGUAGE plpgsql
AS $function$
DECLARE
    cutoff timestamptz;
BEGIN
    SELECT trust_cutoff_at INTO cutoff
    FROM finance.source_accounts
    WHERE source_account_id = NEW.source_account_id;

    IF cutoff IS NOT NULL AND NEW.effective_at > cutoff AND NOT EXISTS (
        SELECT 1
        FROM finance.quality_issues qi
        WHERE qi.quality_issue_id = NEW.quality_issue_id
          AND qi.issue_type = 'stale_after_trust_cutoff'
          AND qi.subject_type = 'balance_observation'
          AND qi.subject_key = NEW.balance_observation_id::text
    ) THEN
        RAISE EXCEPTION 'post-cutoff balance requires a matching stale quality issue'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$function$;

DROP TRIGGER IF EXISTS balance_observations_require_stale_issue
ON finance.balance_observations;
CREATE TRIGGER balance_observations_require_stale_issue
BEFORE INSERT OR UPDATE OF source_account_id, effective_at, quality_issue_id
ON finance.balance_observations
FOR EACH ROW EXECUTE FUNCTION finance.validate_balance_trust_cutoff();

CREATE TABLE IF NOT EXISTS finance.position_observations (
    position_observation_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    ingestion_run_id uuid NOT NULL REFERENCES finance.ingestion_runs(ingestion_run_id),
    source_blob_id uuid NOT NULL REFERENCES finance.source_blobs(source_blob_id),
    source_account_id uuid NOT NULL REFERENCES finance.source_accounts(source_account_id),
    external_position_id text NOT NULL CHECK (btrim(external_position_id) <> ''),
    observation_hash text NOT NULL CHECK (observation_hash ~ '^[0-9a-f]{64}$'),
    instrument_key text NOT NULL CHECK (btrim(instrument_key) <> ''),
    effective_at timestamptz NOT NULL,
    observed_at timestamptz NOT NULL,
    processed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    quantity numeric(30, 12) NOT NULL,
    cost_basis numeric(24, 8),
    currency_code text CHECK (currency_code IS NULL OR currency_code ~ '^[A-Z]{3}$'),
    quality_issue_id uuid REFERENCES finance.quality_issues(quality_issue_id),
    UNIQUE (source_account_id, external_position_id, effective_at, observation_hash),
    CHECK (processed_at >= observed_at)
);

CREATE TABLE IF NOT EXISTS finance.valuation_observations (
    valuation_observation_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    ingestion_run_id uuid NOT NULL REFERENCES finance.ingestion_runs(ingestion_run_id),
    source_blob_id uuid NOT NULL REFERENCES finance.source_blobs(source_blob_id),
    source_account_id uuid NOT NULL REFERENCES finance.source_accounts(source_account_id),
    instrument_key text,
    observation_hash text NOT NULL CHECK (observation_hash ~ '^[0-9a-f]{64}$'),
    valuation_type text NOT NULL CHECK (valuation_type IN ('account', 'position', 'price')),
    effective_at timestamptz NOT NULL,
    observed_at timestamptz NOT NULL,
    processed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    value numeric(24, 8) NOT NULL,
    currency_code text NOT NULL CHECK (currency_code ~ '^[A-Z]{3}$'),
    quality_issue_id uuid REFERENCES finance.quality_issues(quality_issue_id),
    UNIQUE (source_account_id, valuation_type, instrument_key, effective_at, observation_hash),
    CHECK (processed_at >= observed_at)
);

DO $do$
DECLARE
    table_name text;
BEGIN
    FOREACH table_name IN ARRAY ARRAY[
        'transaction_observations', 'balance_observations',
        'position_observations', 'valuation_observations'
    ] LOOP
        EXECUTE format('DROP TRIGGER IF EXISTS %I ON finance.%I',
            table_name || '_immutable_evidence', table_name);
        EXECUTE format(
            'CREATE TRIGGER %I BEFORE UPDATE OR DELETE ON finance.%I FOR EACH ROW '
            'EXECUTE FUNCTION finance.reject_immutable_columns'
            '(''observation_hash'', ''source_blob_id'', ''ingestion_run_id'', '
            '''source_account_id'', ''source_transaction_id'', '
            '''external_position_id'', ''instrument_key'', ''balance_type'', '
            '''valuation_type'', ''effective_at'', ''posted_at'', ''observed_at'', '
            '''processed_at'', ''amount'', ''currency_code'', ''description'', '
            '''source_status'', ''quantity'', ''cost_basis'', ''value'', '
            '''quality_issue_id'', ''sighting_order'')',
            table_name || '_immutable_evidence', table_name
        );
    END LOOP;
END;
$do$;

CREATE TABLE IF NOT EXISTS finance.app_state_snapshots (
    app_state_snapshot_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    app_name text NOT NULL,
    state_kind text NOT NULL,
    external_locator text NOT NULL CHECK (btrim(external_locator) <> ''),
    state_hash text NOT NULL CHECK (state_hash ~ '^[0-9a-f]{64}$'),
    effective_at timestamptz NOT NULL,
    observed_at timestamptz NOT NULL,
    processed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (app_name, state_kind, state_hash),
    CHECK (processed_at >= observed_at)
);

DROP TRIGGER IF EXISTS app_state_snapshots_immutable_evidence ON finance.app_state_snapshots;
CREATE TRIGGER app_state_snapshots_immutable_evidence
BEFORE UPDATE OR DELETE ON finance.app_state_snapshots
FOR EACH ROW EXECUTE FUNCTION finance.reject_immutable_columns('external_locator', 'state_hash');

CREATE TABLE IF NOT EXISTS finance.audit_events (
    audit_event_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    event_type text NOT NULL,
    actor text NOT NULL,
    subject_type text NOT NULL,
    subject_key text NOT NULL,
    event_data jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(event_data) = 'object'),
    effective_at timestamptz NOT NULL,
    observed_at timestamptz NOT NULL,
    processed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK (processed_at >= observed_at)
);

DROP TRIGGER IF EXISTS audit_events_append_only ON finance.audit_events;
CREATE TRIGGER audit_events_append_only
BEFORE UPDATE OR DELETE ON finance.audit_events
FOR EACH ROW EXECUTE FUNCTION finance.reject_append_only_change();

INSERT INTO finance.schema_migrations(version, name, checksum)
VALUES (:'migration_version', :'migration_name', :'migration_checksum')
ON CONFLICT (version) DO NOTHING;

COMMIT;
