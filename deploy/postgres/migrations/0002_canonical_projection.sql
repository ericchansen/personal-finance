BEGIN;

CREATE TABLE IF NOT EXISTS finance.canonical_accounts (
    canonical_account_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    canonical_key text NOT NULL UNIQUE CHECK (btrim(canonical_key) <> ''),
    display_name text NOT NULL CHECK (btrim(display_name) <> ''),
    account_type text,
    currency_code text CHECK (currency_code IS NULL OR currency_code ~ '^[A-Z]{3}$'),
    status text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'closed', 'excluded')),
    exclusion_decision_id uuid REFERENCES finance.durable_decisions(decision_id),
    effective_from timestamptz NOT NULL,
    effective_to timestamptz,
    observed_at timestamptz NOT NULL,
    processed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK (effective_to IS NULL OR effective_to >= effective_from),
    CHECK ((status = 'excluded') = (exclusion_decision_id IS NOT NULL)),
    CHECK (processed_at >= observed_at)
);

CREATE TABLE IF NOT EXISTS finance.canonical_transactions (
    canonical_transaction_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    canonical_account_id uuid NOT NULL REFERENCES finance.canonical_accounts(canonical_account_id),
    canonical_key text NOT NULL CHECK (btrim(canonical_key) <> ''),
    effective_at timestamptz NOT NULL,
    observed_at timestamptz NOT NULL,
    processed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    amount numeric(24, 8) NOT NULL,
    currency_code text NOT NULL CHECK (currency_code ~ '^[A-Z]{3}$'),
    description text,
    status text NOT NULL DEFAULT 'posted' CHECK (status IN ('pending', 'posted', 'suppressed', 'corrected')),
    suppression_decision_id uuid REFERENCES finance.durable_decisions(decision_id),
    corrects_transaction_id uuid REFERENCES finance.canonical_transactions(canonical_transaction_id),
    UNIQUE (canonical_account_id, canonical_key),
    CHECK ((status = 'suppressed') = (suppression_decision_id IS NOT NULL)),
    CHECK (corrects_transaction_id IS NULL OR corrects_transaction_id <> canonical_transaction_id),
    CHECK (processed_at >= observed_at)
);

CREATE TABLE IF NOT EXISTS finance.source_account_links (
    source_account_id uuid NOT NULL REFERENCES finance.source_accounts(source_account_id),
    canonical_account_id uuid NOT NULL REFERENCES finance.canonical_accounts(canonical_account_id),
    decision_id uuid NOT NULL REFERENCES finance.durable_decisions(decision_id),
    effective_from timestamptz NOT NULL,
    effective_to timestamptz,
    observed_at timestamptz NOT NULL,
    processed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (source_account_id, canonical_account_id, effective_from),
    CHECK (effective_to IS NULL OR effective_to >= effective_from),
    CHECK (processed_at >= observed_at)
);

CREATE UNIQUE INDEX IF NOT EXISTS source_account_links_one_current_idx
ON finance.source_account_links(source_account_id)
WHERE effective_to IS NULL;

CREATE OR REPLACE FUNCTION finance.validate_source_account_link_decision()
RETURNS trigger
LANGUAGE plpgsql
AS $function$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM finance.durable_decisions d
        WHERE d.decision_id = NEW.decision_id
          AND d.decision_type = 'account_mapping'
          AND d.subject_type = 'source_account'
          AND d.subject_key = NEW.source_account_id::text
          AND d.action = 'map_to:' || NEW.canonical_account_id::text
    ) THEN
        RAISE EXCEPTION 'source account link requires its matching mapping decision'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$function$;

DROP TRIGGER IF EXISTS source_account_links_require_decision
ON finance.source_account_links;
CREATE TRIGGER source_account_links_require_decision
BEFORE INSERT OR UPDATE OF source_account_id, canonical_account_id, decision_id
ON finance.source_account_links
FOR EACH ROW EXECUTE FUNCTION finance.validate_source_account_link_decision();

CREATE OR REPLACE FUNCTION finance.protect_source_account_link_history()
RETURNS trigger
LANGUAGE plpgsql
AS $function$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'finance.source_account_links is append-only history'
            USING ERRCODE = '55000';
    END IF;
    IF NEW.source_account_id IS DISTINCT FROM OLD.source_account_id
       OR NEW.canonical_account_id IS DISTINCT FROM OLD.canonical_account_id
       OR NEW.decision_id IS DISTINCT FROM OLD.decision_id
       OR NEW.effective_from IS DISTINCT FROM OLD.effective_from
       OR NEW.observed_at IS DISTINCT FROM OLD.observed_at
       OR NEW.processed_at IS DISTINCT FROM OLD.processed_at
       OR (OLD.effective_to IS NOT NULL AND NEW.effective_to IS DISTINCT FROM OLD.effective_to)
       OR (OLD.effective_to IS NULL AND NEW.effective_to IS NULL)
       OR NEW.effective_to < OLD.effective_from
    THEN
        RAISE EXCEPTION 'established source account mapping history is immutable'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$function$;

DROP TRIGGER IF EXISTS source_account_links_protect_history
ON finance.source_account_links;
CREATE TRIGGER source_account_links_protect_history
BEFORE UPDATE OR DELETE ON finance.source_account_links
FOR EACH ROW EXECUTE FUNCTION finance.protect_source_account_link_history();

CREATE TABLE IF NOT EXISTS finance.transaction_observation_links (
    transaction_observation_link_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    transaction_observation_id uuid NOT NULL
        REFERENCES finance.transaction_observations(transaction_observation_id),
    canonical_transaction_id uuid NOT NULL
        REFERENCES finance.canonical_transactions(canonical_transaction_id),
    link_method text NOT NULL CHECK (link_method IN ('exact', 'source_id', 'fuzzy', 'manual', 'correction')),
    quality_issue_id uuid REFERENCES finance.quality_issues(quality_issue_id),
    decision_id uuid REFERENCES finance.durable_decisions(decision_id),
    is_current boolean NOT NULL DEFAULT true,
    effective_at timestamptz NOT NULL,
    observed_at timestamptz NOT NULL,
    processed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (transaction_observation_id, canonical_transaction_id, effective_at),
    CHECK (link_method <> 'fuzzy' OR quality_issue_id IS NOT NULL),
    CHECK (link_method NOT IN ('manual', 'correction') OR decision_id IS NOT NULL),
    CHECK (processed_at >= observed_at)
);

CREATE UNIQUE INDEX IF NOT EXISTS transaction_observation_links_one_current_idx
ON finance.transaction_observation_links(transaction_observation_id)
WHERE is_current;

CREATE OR REPLACE FUNCTION finance.validate_fuzzy_observation_link()
RETURNS trigger
LANGUAGE plpgsql
AS $function$
BEGIN
    IF NEW.link_method = 'fuzzy' AND NOT EXISTS (
        SELECT 1
        FROM finance.quality_issues qi
        WHERE qi.quality_issue_id = NEW.quality_issue_id
          AND qi.issue_type IN ('fuzzy_duplicate', 'unresolved_duplicate')
    ) THEN
        RAISE EXCEPTION 'fuzzy observation link requires a duplicate quality issue'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$function$;

DROP TRIGGER IF EXISTS transaction_observation_links_require_issue
ON finance.transaction_observation_links;
CREATE TRIGGER transaction_observation_links_require_issue
BEFORE INSERT OR UPDATE OF link_method, quality_issue_id
ON finance.transaction_observation_links
FOR EACH ROW EXECUTE FUNCTION finance.validate_fuzzy_observation_link();

CREATE TABLE IF NOT EXISTS finance.projection_runs (
    projection_run_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    target_system text NOT NULL,
    projection_version text NOT NULL,
    run_status text NOT NULL CHECK (run_status IN ('started', 'succeeded', 'failed', 'partial')),
    cutoff_effective_at timestamptz NOT NULL,
    observed_at timestamptz NOT NULL,
    processed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    finished_at timestamptz,
    CHECK (processed_at >= observed_at),
    CHECK (finished_at IS NULL OR finished_at >= processed_at)
);

CREATE TABLE IF NOT EXISTS finance.projection_records (
    projection_record_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    projection_run_id uuid NOT NULL REFERENCES finance.projection_runs(projection_run_id),
    canonical_transaction_id uuid NOT NULL
        REFERENCES finance.canonical_transactions(canonical_transaction_id),
    target_system text NOT NULL,
    target_record_key text,
    projected_hash text NOT NULL CHECK (projected_hash ~ '^[0-9a-f]{64}$'),
    projection_status text NOT NULL CHECK (projection_status IN ('planned', 'applied', 'rejected', 'superseded')),
    is_current boolean NOT NULL DEFAULT true,
    effective_at timestamptz NOT NULL,
    observed_at timestamptz NOT NULL,
    processed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (projection_run_id, target_system, canonical_transaction_id),
    CHECK (processed_at >= observed_at)
);

CREATE UNIQUE INDEX IF NOT EXISTS projection_records_current_canonical_target_idx
ON finance.projection_records(target_system, canonical_transaction_id)
WHERE is_current;

CREATE OR REPLACE FUNCTION finance.validate_canonical_decision()
RETURNS trigger
LANGUAGE plpgsql
AS $function$
DECLARE
    decision uuid;
    required_type text;
    expected_subject text;
BEGIN
    IF TG_TABLE_NAME = 'canonical_accounts' AND NEW.status = 'excluded' THEN
        decision := NEW.exclusion_decision_id;
        required_type := 'account_exclusion';
        expected_subject := NEW.canonical_account_id::text;
    ELSIF TG_TABLE_NAME = 'canonical_transactions' AND NEW.status = 'suppressed' THEN
        decision := NEW.suppression_decision_id;
        required_type := 'transaction_suppression';
        expected_subject := NEW.canonical_transaction_id::text;
    ELSE
        RETURN NEW;
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM finance.durable_decisions d
        WHERE d.decision_id = decision
          AND d.decision_type = required_type
          AND d.subject_key = expected_subject
    ) THEN
        RAISE EXCEPTION '% requires a matching durable % decision',
            TG_TABLE_NAME, required_type USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$function$;

DROP TRIGGER IF EXISTS canonical_accounts_require_decision ON finance.canonical_accounts;
CREATE TRIGGER canonical_accounts_require_decision
BEFORE INSERT OR UPDATE OF status, exclusion_decision_id ON finance.canonical_accounts
FOR EACH ROW EXECUTE FUNCTION finance.validate_canonical_decision();

DROP TRIGGER IF EXISTS canonical_transactions_require_decision ON finance.canonical_transactions;
CREATE TRIGGER canonical_transactions_require_decision
BEFORE INSERT OR UPDATE OF status, suppression_decision_id ON finance.canonical_transactions
FOR EACH ROW EXECUTE FUNCTION finance.validate_canonical_decision();

INSERT INTO finance.schema_migrations(version, name, checksum)
VALUES (:'migration_version', :'migration_name', :'migration_checksum')
ON CONFLICT (version) DO NOTHING;

COMMIT;
