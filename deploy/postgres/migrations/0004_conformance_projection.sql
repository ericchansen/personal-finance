BEGIN;

-- Public, synthetic CandidateAdapter evidence.  The normal finance tables retain
-- normalized records; these rows preserve the source-shaped contract input and
-- derived contract projection without storing raw financial payloads.
CREATE TABLE IF NOT EXISTS finance.conformance_observations (
    observation_id text PRIMARY KEY CHECK (observation_id LIKE 'SYN-%'),
    batch_id text NOT NULL CHECK (batch_id LIKE 'SYN-%'),
    batch_sequence integer NOT NULL CHECK (batch_sequence > 0),
    observation_order integer NOT NULL CHECK (observation_order >= 0),
    kind text NOT NULL CHECK (kind IN ('account', 'balance', 'file', 'transaction')),
    source_format text NOT NULL CHECK (btrim(source_format) <> ''),
    source_account_id text,
    provider_transaction_id text,
    normalized_observation_id uuid,
    observed_at_text text NOT NULL CHECK (btrim(observed_at_text) <> ''),
    effective_date text,
    amount_minor bigint,
    currency text,
    description text,
    pending boolean,
    attributes jsonb NOT NULL DEFAULT '[]'::jsonb
        CHECK (jsonb_typeof(attributes) = 'array'),
    UNIQUE (batch_id, observation_order)
);

DROP TRIGGER IF EXISTS conformance_observations_append_only
ON finance.conformance_observations;
CREATE TRIGGER conformance_observations_append_only
BEFORE UPDATE OR DELETE ON finance.conformance_observations
FOR EACH ROW EXECUTE FUNCTION finance.reject_append_only_change();

-- Derived artifacts are explicitly mutable projections of immutable observations.
-- `active = false` preserves a superseded projection version without making
-- withdrawn canonical state appear in the current CandidateAdapter result.
CREATE TABLE IF NOT EXISTS finance.conformance_artifacts (
    artifact_kind text NOT NULL CHECK (artifact_kind IN (
        'canonical_event', 'link', 'suppression', 'decision', 'issue', 'provenance'
    )),
    artifact_id text NOT NULL CHECK (btrim(artifact_id) <> ''),
    document jsonb NOT NULL CHECK (jsonb_typeof(document) = 'object'),
    active boolean NOT NULL DEFAULT true,
    PRIMARY KEY (artifact_kind, artifact_id)
);

CREATE INDEX IF NOT EXISTS conformance_artifacts_current_idx
ON finance.conformance_artifacts(artifact_kind, artifact_id)
WHERE active;

CREATE TABLE IF NOT EXISTS finance.conformance_batch_commits (
    batch_id text PRIMARY KEY CHECK (batch_id LIKE 'SYN-%'),
    batch_sequence integer NOT NULL CHECK (batch_sequence > 0),
    sealed_hash text NOT NULL CHECK (sealed_hash ~ '^[0-9a-f]{64}$'),
    observation_count integer NOT NULL CHECK (observation_count > 0)
);

DROP TRIGGER IF EXISTS conformance_batch_commits_append_only
ON finance.conformance_batch_commits;
CREATE TRIGGER conformance_batch_commits_append_only
BEFORE UPDATE OR DELETE ON finance.conformance_batch_commits
FOR EACH ROW EXECUTE FUNCTION finance.reject_append_only_change();

CREATE OR REPLACE VIEW finance_read.conformance_observations AS
SELECT
    observation_id,
    batch_id,
    batch_sequence,
    observation_order,
    kind,
    source_format,
    source_account_id,
    provider_transaction_id,
    normalized_observation_id,
    observed_at_text,
    effective_date,
    amount_minor,
    currency,
    description,
    pending,
    attributes
FROM finance.conformance_observations;

CREATE OR REPLACE VIEW finance_read.conformance_artifacts AS
SELECT artifact_kind, artifact_id, document
FROM finance.conformance_artifacts
WHERE active;

CREATE OR REPLACE VIEW finance_read.conformance_source_canonical_projection_comparison AS
SELECT
    o.observation_id,
    o.source_account_id,
    o.provider_transaction_id,
    o.amount_minor AS source_amount_minor,
    c.document ->> 'canonical_id' AS canonical_id,
    c.document ->> 'status' AS canonical_status,
    c.document ->> 'effective_date' AS canonical_effective_date,
    c.document ->> 'amount_minor' AS canonical_amount_minor,
    c.document -> 'observation_ids' AS canonical_observation_ids,
    ct.canonical_transaction_id AS normalized_canonical_transaction_id,
    pr.projection_record_id,
    pr.target_system,
    pr.is_current AS projected
FROM finance.conformance_observations o
LEFT JOIN finance.conformance_artifacts c
  ON c.artifact_kind = 'canonical_event'
 AND c.active
 AND c.document -> 'observation_ids' ? o.observation_id
LEFT JOIN finance.transaction_observations tro
  ON tro.transaction_observation_id = o.normalized_observation_id
LEFT JOIN finance.transaction_observation_links tol
  ON tol.transaction_observation_id = tro.transaction_observation_id
 AND tol.is_current
LEFT JOIN finance.canonical_transactions ct
  ON ct.canonical_transaction_id = tol.canonical_transaction_id
LEFT JOIN finance.projection_records pr
  ON pr.canonical_transaction_id = ct.canonical_transaction_id
 AND pr.is_current;

CREATE OR REPLACE VIEW finance_read.conformance_provenance AS
SELECT
    artifact_id AS provenance_id,
    document ->> 'canonical_id' AS canonical_id,
    document -> 'observation_ids' AS observation_ids,
    document -> 'decision_ids' AS decision_ids,
    document ->> 'transformation' AS transformation
FROM finance.conformance_artifacts
WHERE artifact_kind = 'provenance'
  AND active;

CREATE OR REPLACE VIEW finance_read.conformance_pending_history AS
SELECT
    o.observation_id,
    o.source_account_id,
    o.provider_transaction_id,
    o.pending,
    c.document ->> 'canonical_id' AS canonical_id,
    c.document ->> 'status' AS canonical_status,
    c.document -> 'observation_ids' AS canonical_observation_ids
FROM finance.conformance_observations o
LEFT JOIN finance.conformance_artifacts c
  ON c.artifact_kind = 'canonical_event'
 AND c.active
 AND c.document -> 'observation_ids' ? o.observation_id
WHERE o.kind = 'transaction';

CREATE OR REPLACE VIEW finance_read.conformance_unresolved_duplicates AS
SELECT
    artifact_id AS issue_id,
    document ->> 'kind' AS issue_kind,
    document -> 'observation_ids' AS observation_ids,
    document -> 'canonical_ids' AS canonical_ids
FROM finance.conformance_artifacts
WHERE artifact_kind = 'issue'
  AND active
  AND document ->> 'status' = 'open'
  AND document ->> 'kind' = 'ambiguous_match';

CREATE OR REPLACE VIEW finance_read.conformance_corrections_decisions AS
SELECT
    decision.artifact_id AS decision_id,
    decision.document ->> 'kind' AS decision_kind,
    decision.document -> 'observation_ids' AS observation_ids,
    decision.document -> 'canonical_ids' AS canonical_ids,
    canonical.document ->> 'amount_minor' AS current_amount_minor
FROM finance.conformance_artifacts decision
LEFT JOIN finance.conformance_artifacts canonical
  ON canonical.artifact_kind = 'canonical_event'
 AND canonical.active
 AND decision.document -> 'canonical_ids' ? canonical.artifact_id
WHERE decision.artifact_kind = 'decision'
  AND decision.active;

CREATE OR REPLACE VIEW finance_read.conformance_balance_changes AS
SELECT
    decision.artifact_id AS decision_id,
    (
        SELECT observation.source_account_id
        FROM finance.conformance_observations observation
        WHERE decision.document -> 'observation_ids' ? observation.observation_id
        ORDER BY observation.batch_sequence, observation.observation_order,
                 observation.observation_id
        LIMIT 1
    ) AS account_id,
    ordered_observations.observation_ids,
    decision.document -> 'canonical_ids' AS canonical_ids,
    related_issues.issue_ids,
    trusted.last_trusted_date
FROM finance.conformance_artifacts decision
LEFT JOIN LATERAL (
    SELECT COALESCE(
        jsonb_agg(
            observation.observation_id
            ORDER BY observation.batch_sequence, observation.observation_order,
                     observation.observation_id
        ),
        '[]'::jsonb
    ) AS observation_ids
    FROM finance.conformance_observations observation
    WHERE decision.document -> 'observation_ids' ? observation.observation_id
       OR EXISTS (
            SELECT 1
            FROM finance.conformance_artifacts canonical
            WHERE canonical.artifact_kind = 'canonical_event'
              AND canonical.active
              AND decision.document -> 'canonical_ids' ? canonical.artifact_id
              AND canonical.document -> 'observation_ids' ? observation.observation_id
       )
) ordered_observations ON true
LEFT JOIN LATERAL (
    SELECT COALESCE(
        jsonb_agg(issue.artifact_id ORDER BY issue.artifact_id),
        '[]'::jsonb
    ) AS issue_ids
    FROM finance.conformance_artifacts issue
    WHERE issue.artifact_kind = 'issue'
      AND issue.active
      AND issue.document ->> 'kind' IN ('stale_balance', 'trust_cutoff')
      AND EXISTS (
          SELECT 1
          FROM jsonb_array_elements_text(issue.document -> 'observation_ids')
               AS issue_observation(observation_id)
          WHERE decision.document -> 'observation_ids'
                ? issue_observation.observation_id
      )
) related_issues ON true
LEFT JOIN LATERAL (
    SELECT max(canonical.document ->> 'effective_date') AS last_trusted_date
    FROM finance.conformance_artifacts canonical
    WHERE canonical.artifact_kind = 'canonical_event'
      AND canonical.active
      AND canonical.document ->> 'kind' = 'balance'
      AND decision.document -> 'canonical_ids' ? canonical.artifact_id
) trusted ON true
WHERE decision.artifact_kind = 'decision'
  AND decision.active
  AND decision.document ->> 'kind' = 'trust-cutoff';

CREATE OR REPLACE VIEW finance_read.conformance_stale_trust_issues AS
SELECT
    artifact_id AS issue_id,
    document ->> 'kind' AS issue_kind,
    document -> 'observation_ids' AS observation_ids
FROM finance.conformance_artifacts
WHERE artifact_kind = 'issue'
  AND active
  AND document ->> 'kind' IN ('stale_balance', 'trust_cutoff');

CREATE OR REPLACE VIEW finance_read.conformance_replay_equality AS
SELECT
    batch.batch_id,
    batch.batch_sequence,
    batch.sealed_hash,
    batch.observation_count,
    observations.observation_ids,
    canonicals.canonical_ids
FROM finance.conformance_batch_commits batch
LEFT JOIN LATERAL (
    SELECT COALESCE(
        jsonb_agg(
            observation.observation_id
            ORDER BY observation.observation_order, observation.observation_id
        ),
        '[]'::jsonb
    ) AS observation_ids
    FROM finance.conformance_observations observation
    WHERE observation.batch_id = batch.batch_id
) observations ON true
LEFT JOIN LATERAL (
    SELECT COALESCE(
        jsonb_agg(canonical.artifact_id ORDER BY canonical.artifact_id),
        '[]'::jsonb
    ) AS canonical_ids
    FROM finance.conformance_artifacts canonical
    WHERE canonical.artifact_kind = 'canonical_event'
      AND canonical.active
      AND EXISTS (
          SELECT 1
          FROM finance.conformance_observations observation
          WHERE observation.batch_id = batch.batch_id
            AND canonical.document -> 'observation_ids' ? observation.observation_id
      )
) canonicals ON true;

GRANT SELECT ON finance_read.conformance_observations,
    finance_read.conformance_artifacts,
    finance_read.conformance_source_canonical_projection_comparison,
    finance_read.conformance_provenance,
    finance_read.conformance_pending_history,
    finance_read.conformance_unresolved_duplicates,
    finance_read.conformance_corrections_decisions,
    finance_read.conformance_balance_changes,
    finance_read.conformance_stale_trust_issues,
    finance_read.conformance_replay_equality
TO finance_readonly;

INSERT INTO finance.schema_migrations(version, name, checksum)
VALUES (:'migration_version', :'migration_name', :'migration_checksum')
ON CONFLICT (version) DO NOTHING;

COMMIT;
