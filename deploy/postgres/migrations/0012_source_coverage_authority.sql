BEGIN;

-- Migration 0012 layers authoritative source coverage on top of the canonical
-- identity engine introduced in 0011.  Nothing in 0011 is rewritten: this
-- migration only widens the enumerations that gained members, adds a new
-- competing-candidate proof shape, records the coverage evidence that proves an
-- authority claim, and exposes safe aggregates through a new read view.

-- The 0011 proof validator accepts a fixed set of shapes.  Source-coverage
-- suppression carries its own proof, so a dedicated branch is added.  Every
-- previously valid shape stays valid.
CREATE OR REPLACE FUNCTION finance.valid_competing_candidate_proof(candidate jsonb)
RETURNS boolean
LANGUAGE sql
IMMUTABLE
AS $function$
    SELECT CASE
        WHEN candidate IS NULL OR jsonb_typeof(candidate) <> 'object' THEN false
        WHEN candidate = '{}'::jsonb THEN true
        WHEN (
                SELECT count(*)
                FROM jsonb_object_keys(candidate) AS object_key(key)
             ) = 2
             AND candidate ? 'competingCandidateCount'
             AND candidate ? 'sourceClaimCount'
             AND NOT EXISTS (
                 SELECT 1
                 FROM jsonb_each(candidate) AS entry(key, value)
                 WHERE jsonb_typeof(entry.value) <> 'number'
                    OR entry.value::text !~ '^[0-9]+$'
             )
        THEN true
        WHEN (
                SELECT count(*)
                FROM jsonb_object_keys(candidate) AS object_key(key)
             ) = 4
             AND candidate ? 'leftDegree'
             AND candidate ? 'rightDegree'
             AND candidate ? 'componentSize'
             AND candidate ? 'competingCandidateCount'
             AND NOT EXISTS (
                 SELECT 1
                 FROM jsonb_each(candidate) AS entry(key, value)
                 WHERE jsonb_typeof(entry.value) <> 'number'
                    OR entry.value::text !~ '^[0-9]+$'
             )
        THEN true
        WHEN candidate ? 'authoritativeCount'
             AND candidate ? 'lowerCount'
             AND candidate ? 'bucketParticipantCount'
             AND NOT EXISTS (
                 SELECT 1
                 FROM jsonb_each(candidate) AS entry(key, value)
                 WHERE entry.key !~ '^[a-z][A-Za-z0-9]*$'
                    OR jsonb_typeof(entry.value) <> 'number'
                    OR entry.value::text !~ '^[0-9]+$'
             )
        THEN true
        WHEN (
                SELECT count(*)
                FROM jsonb_object_keys(candidate) AS object_key(key)
             ) > 0
             AND NOT EXISTS (
                 SELECT 1
                 FROM jsonb_each(candidate) AS entry(key, value)
                 WHERE entry.key !~ '^degree:[0-9a-f]{64}$'
                    OR jsonb_typeof(entry.value) <> 'number'
                    OR entry.value::text !~ '^[1-9][0-9]*$'
             )
        THEN true
        ELSE false
    END;
$function$;

ALTER TABLE finance.canonical_identity_graph_edges
    DROP CONSTRAINT IF EXISTS canonical_identity_graph_edges_relation_kind_check;
ALTER TABLE finance.canonical_identity_graph_edges
    ADD CONSTRAINT canonical_identity_graph_edges_relation_kind_check
    CHECK (relation_kind IN (
        'duplicate-candidate', 'transfer', 'transfer-candidate',
        'correction', 'reversal', 'pending-transition',
        'mirrored-provider-error', 'mirror-candidate', 'source-suppressed'
    ));

ALTER TABLE finance.canonical_identity_automatic_decisions
    DROP CONSTRAINT IF EXISTS canonical_identity_automatic_decisions_outcome_check;
ALTER TABLE finance.canonical_identity_automatic_decisions
    ADD CONSTRAINT canonical_identity_automatic_decisions_outcome_check
    CHECK (outcome IN (
        'merge-observations', 'merge-claims', 'preserve-distinct',
        'link-transfer', 'link-correction', 'link-reversal', 'link-pending',
        'suppress-mirrored-provider-error', 'source-suppressed',
        'unresolved', 'exclude-untrusted'
    ));

ALTER TABLE finance.canonical_identity_automatic_decisions
    DROP CONSTRAINT IF EXISTS
        canonical_identity_automatic_decisions_confidence_tier_check;
ALTER TABLE finance.canonical_identity_automatic_decisions
    ADD CONSTRAINT canonical_identity_automatic_decisions_confidence_tier_check
    CHECK (confidence_tier IN (
        'human-override', 'exact-scoped-identity', 'explicit-lineage',
        'authoritative-source-coverage', 'unique-cross-source', 'review-required'
    ));

-- Every decision now carries the residual class it settled into, and the
-- authority policy hash when source coverage participated in it.
ALTER TABLE finance.canonical_identity_automatic_decisions
    ADD COLUMN IF NOT EXISTS residual_classification text;
ALTER TABLE finance.canonical_identity_automatic_decisions
    DROP CONSTRAINT IF EXISTS
        canonical_identity_automatic_decisions_residual_classification_check;
ALTER TABLE finance.canonical_identity_automatic_decisions
    ADD CONSTRAINT
        canonical_identity_automatic_decisions_residual_classification_check
    CHECK (residual_classification IS NULL OR residual_classification IN (
        'distinct', 'source-suppressed', 'transfer',
        'correction', 'reversal', 'unresolved'
    ));

ALTER TABLE finance.canonical_identity_automatic_decisions
    ADD COLUMN IF NOT EXISTS source_authority_policy_hash text;
ALTER TABLE finance.canonical_identity_automatic_decisions
    DROP CONSTRAINT IF EXISTS
        canonical_identity_automatic_decisions_source_authority_policy_hash_check;
ALTER TABLE finance.canonical_identity_automatic_decisions
    ADD CONSTRAINT
        canonical_identity_automatic_decisions_source_authority_policy_hash_check
    CHECK (
        source_authority_policy_hash IS NULL
        OR source_authority_policy_hash ~ '^[0-9a-f]{64}$'
    );

-- A source-suppressed decision must name the authority policy that proved it.
ALTER TABLE finance.canonical_identity_automatic_decisions
    DROP CONSTRAINT IF EXISTS
        canonical_identity_automatic_decisions_source_suppression_proof_check;
ALTER TABLE finance.canonical_identity_automatic_decisions
    ADD CONSTRAINT
        canonical_identity_automatic_decisions_source_suppression_proof_check
    CHECK (
        outcome <> 'source-suppressed'
        OR (
            source_authority_policy_hash IS NOT NULL
            AND confidence_tier = 'authoritative-source-coverage'
            AND residual_classification = 'source-suppressed'
        )
    );

CREATE TABLE IF NOT EXISTS finance.canonical_identity_source_authority_policies (
    canonical_identity_source_authority_policy_id uuid PRIMARY KEY
        DEFAULT gen_random_uuid(),
    authority_policy_version text NOT NULL
        CHECK (btrim(authority_policy_version) <> ''),
    authority_policy_hash text NOT NULL
        CHECK (authority_policy_hash ~ '^[0-9a-f]{64}$'),
    authority_policy_document jsonb NOT NULL
        CHECK (jsonb_typeof(authority_policy_document) = 'object'),
    observed_at timestamptz NOT NULL,
    processed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (authority_policy_version, authority_policy_hash),
    CHECK (processed_at >= observed_at)
);

CREATE TABLE IF NOT EXISTS finance.canonical_identity_authority_intervals (
    canonical_identity_authority_interval_id uuid PRIMARY KEY
        DEFAULT gen_random_uuid(),
    canonical_identity_policy_generation_id uuid NOT NULL REFERENCES
        finance.canonical_identity_policy_generations(
            canonical_identity_policy_generation_id
        ),
    canonical_identity_source_authority_policy_id uuid NOT NULL REFERENCES
        finance.canonical_identity_source_authority_policies(
            canonical_identity_source_authority_policy_id
        ),
    interval_id text NOT NULL CHECK (interval_id ~ '^[0-9a-f]{64}$'),
    canonical_account_hash text NOT NULL
        CHECK (canonical_account_hash ~ '^[0-9a-f]{64}$'),
    effective_from date NOT NULL,
    effective_through date NOT NULL,
    source_family text NOT NULL CHECK (btrim(source_family) <> ''),
    source_connection_hash text NOT NULL
        CHECK (source_connection_hash ~ '^[0-9a-f]{64}$'),
    source_account_hash text NOT NULL
        CHECK (source_account_hash ~ '^[0-9a-f]{64}$'),
    format_strength text NOT NULL CHECK (format_strength IN (
        'stable-provider-id', 'posted-observation',
        'synthetic-csv', 'legacy-export'
    )),
    stable_id_support boolean NOT NULL,
    replay_stable_ids boolean NOT NULL,
    completeness text NOT NULL
        CHECK (completeness IN ('complete', 'partial', 'unknown')),
    extraction_requested_from date NOT NULL,
    extraction_requested_through date NOT NULL,
    extracted_at timestamptz NOT NULL,
    freshness_as_of timestamptz NOT NULL,
    trust_cutoff_day date,
    declared_source_transaction_count integer NOT NULL
        CHECK (declared_source_transaction_count >= 0),
    observed_source_transaction_count integer NOT NULL
        CHECK (observed_source_transaction_count >= 0),
    settlement_days integer NOT NULL,
    authority_rank integer CHECK (authority_rank IS NULL OR authority_rank > 0),
    coverage_proven boolean NOT NULL,
    counts_reconciled boolean NOT NULL,
    authoritative boolean NOT NULL,
    authority_proof jsonb NOT NULL
        CHECK (jsonb_typeof(authority_proof) = 'object'),
    evidence_hash text NOT NULL CHECK (evidence_hash ~ '^[0-9a-f]{64}$'),
    source_hashes jsonb NOT NULL CHECK (
        finance.jsonb_sha256_array(source_hashes)
        AND jsonb_array_length(source_hashes) > 0
    ),
    observed_at timestamptz NOT NULL,
    processed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (canonical_identity_policy_generation_id, interval_id),
    UNIQUE (
        canonical_identity_policy_generation_id,
        canonical_identity_authority_interval_id
    ),
    CHECK (effective_from <= effective_through),
    CHECK (extraction_requested_from <= effective_from),
    CHECK (effective_through <= extraction_requested_through),
    CHECK (trust_cutoff_day IS NULL OR effective_through <= trust_cutoff_day),
    CHECK (freshness_as_of >= extracted_at),
    CHECK (authoritative = (coverage_proven AND counts_reconciled
        AND authority_rank IS NOT NULL)),
    CHECK (processed_at >= observed_at)
);

CREATE INDEX IF NOT EXISTS canonical_identity_authority_intervals_account_idx
    ON finance.canonical_identity_authority_intervals (
        canonical_identity_policy_generation_id,
        canonical_account_hash,
        effective_from,
        effective_through
    );

CREATE TABLE IF NOT EXISTS finance.canonical_identity_source_suppressions (
    canonical_identity_source_suppression_id uuid PRIMARY KEY
        DEFAULT gen_random_uuid(),
    canonical_identity_policy_generation_id uuid NOT NULL REFERENCES
        finance.canonical_identity_policy_generations(
            canonical_identity_policy_generation_id
        ),
    canonical_identity_automatic_decision_id uuid NOT NULL REFERENCES
        finance.canonical_identity_automatic_decisions(
            canonical_identity_automatic_decision_id
        ),
    suppressed_source_claim_id uuid NOT NULL REFERENCES
        finance.canonical_identity_source_claims(
            canonical_identity_source_claim_id
        ),
    authoritative_source_claim_id uuid NOT NULL REFERENCES
        finance.canonical_identity_source_claims(
            canonical_identity_source_claim_id
        ),
    canonical_identity_event_id uuid NOT NULL REFERENCES
        finance.canonical_identity_events(canonical_identity_event_id),
    suppressed_interval_id text NOT NULL
        CHECK (suppressed_interval_id ~ '^[0-9a-f]{64}$'),
    authoritative_interval_id text NOT NULL
        CHECK (authoritative_interval_id ~ '^[0-9a-f]{64}$'),
    authority_policy_hash text NOT NULL
        CHECK (authority_policy_hash ~ '^[0-9a-f]{64}$'),
    occurrence_index integer NOT NULL CHECK (occurrence_index >= 0),
    authoritative_occurrence_count integer NOT NULL
        CHECK (authoritative_occurrence_count > 0),
    suppressed_occurrence_count integer NOT NULL
        CHECK (suppressed_occurrence_count > 0),
    feature_vector jsonb NOT NULL
        CHECK (jsonb_typeof(feature_vector) = 'object'),
    competing_candidate_proof jsonb NOT NULL
        CHECK (finance.valid_competing_candidate_proof(competing_candidate_proof)),
    source_hashes jsonb NOT NULL CHECK (
        finance.jsonb_sha256_array(source_hashes)
        AND jsonb_array_length(source_hashes) > 0
    ),
    edge_hash text NOT NULL CHECK (edge_hash ~ '^[0-9a-f]{64}$'),
    observed_at timestamptz NOT NULL,
    processed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (canonical_identity_policy_generation_id, edge_hash),
    UNIQUE (
        canonical_identity_policy_generation_id,
        suppressed_source_claim_id
    ),
    -- Multiplicity is never destroyed: at most as many suppressed occurrences
    -- as the authoritative source proved.
    CHECK (suppressed_occurrence_count <= authoritative_occurrence_count),
    CHECK (occurrence_index < authoritative_occurrence_count),
    CHECK (suppressed_source_claim_id <> authoritative_source_claim_id),
    CHECK (suppressed_interval_id <> authoritative_interval_id),
    CHECK (processed_at >= observed_at)
);

CREATE INDEX IF NOT EXISTS canonical_identity_source_suppressions_event_idx
    ON finance.canonical_identity_source_suppressions (
        canonical_identity_policy_generation_id,
        canonical_identity_event_id
    );

-- Coverage evidence and its consequences are append-only, exactly like the
-- 0011 identity tables: a suppression decision must remain auditable forever.
DO $do$
DECLARE
    protected_table text;
BEGIN
    FOREACH protected_table IN ARRAY ARRAY[
        'canonical_identity_source_authority_policies',
        'canonical_identity_authority_intervals',
        'canonical_identity_source_suppressions'
    ] LOOP
        EXECUTE format(
            'DROP TRIGGER IF EXISTS %I ON finance.%I',
            protected_table || '_append_only',
            protected_table
        );
        EXECUTE format(
            'CREATE TRIGGER %I BEFORE UPDATE OR DELETE ON finance.%I '
            'FOR EACH ROW EXECUTE FUNCTION finance.reject_append_only_change()',
            protected_table || '_append_only',
            protected_table
        );
    END LOOP;
END;
$do$;

CREATE OR REPLACE VIEW finance_read.identity_source_authority_summary AS
WITH interval_counts AS (
    SELECT
        interval_row.canonical_identity_policy_generation_id,
        count(*) AS authority_interval_count,
        count(*) FILTER (WHERE interval_row.coverage_proven)
            AS proven_interval_count,
        count(*) FILTER (WHERE interval_row.counts_reconciled)
            AS reconciled_interval_count,
        count(*) FILTER (WHERE interval_row.authoritative)
            AS authoritative_interval_count,
        min(interval_row.effective_from) AS earliest_effective_from,
        max(interval_row.effective_through) AS latest_effective_through
    FROM finance.canonical_identity_authority_intervals interval_row
    GROUP BY interval_row.canonical_identity_policy_generation_id
),
suppression_counts AS (
    SELECT
        suppression.canonical_identity_policy_generation_id,
        count(*) AS source_suppression_count,
        count(DISTINCT suppression.canonical_identity_event_id)
            AS source_suppressed_event_count
    FROM finance.canonical_identity_source_suppressions suppression
    GROUP BY suppression.canonical_identity_policy_generation_id
),
residual_counts AS (
    SELECT
        decision.canonical_identity_policy_generation_id,
        count(*) FILTER (WHERE decision.residual_classification = 'distinct')
            AS residual_distinct_count,
        count(*) FILTER (
            WHERE decision.residual_classification = 'source-suppressed'
        ) AS residual_source_suppressed_count,
        count(*) FILTER (WHERE decision.residual_classification = 'transfer')
            AS residual_transfer_count,
        count(*) FILTER (WHERE decision.residual_classification = 'correction')
            AS residual_correction_count,
        count(*) FILTER (WHERE decision.residual_classification = 'reversal')
            AS residual_reversal_count,
        count(*) FILTER (WHERE decision.residual_classification = 'unresolved')
            AS residual_unresolved_count,
        count(*) FILTER (
            WHERE decision.rationale_code IN (
                'ambiguous-lower-source-multiplicity',
                'ambiguous-authority-description-mapping'
            )
        ) AS authority_ambiguous_group_count
    FROM finance.canonical_identity_automatic_decisions decision
    GROUP BY decision.canonical_identity_policy_generation_id
)
SELECT
    generation.canonical_identity_policy_generation_id,
    generation.policy_version,
    generation.policy_hash,
    generation.generation_hash,
    generation.canonical_state_hash,
    COALESCE(interval_counts.authority_interval_count, 0)
        AS authority_interval_count,
    COALESCE(interval_counts.proven_interval_count, 0) AS proven_interval_count,
    COALESCE(interval_counts.reconciled_interval_count, 0)
        AS reconciled_interval_count,
    COALESCE(interval_counts.authoritative_interval_count, 0)
        AS authoritative_interval_count,
    interval_counts.earliest_effective_from,
    interval_counts.latest_effective_through,
    COALESCE(suppression_counts.source_suppression_count, 0)
        AS source_suppression_count,
    COALESCE(suppression_counts.source_suppressed_event_count, 0)
        AS source_suppressed_event_count,
    COALESCE(residual_counts.residual_distinct_count, 0)
        AS residual_distinct_count,
    COALESCE(residual_counts.residual_source_suppressed_count, 0)
        AS residual_source_suppressed_count,
    COALESCE(residual_counts.residual_transfer_count, 0)
        AS residual_transfer_count,
    COALESCE(residual_counts.residual_correction_count, 0)
        AS residual_correction_count,
    COALESCE(residual_counts.residual_reversal_count, 0)
        AS residual_reversal_count,
    COALESCE(residual_counts.residual_unresolved_count, 0)
        AS residual_unresolved_count,
    COALESCE(residual_counts.authority_ambiguous_group_count, 0)
        AS authority_ambiguous_group_count,
    generation.observed_at,
    generation.processed_at
FROM finance.canonical_identity_policy_generations generation
LEFT JOIN interval_counts
  ON interval_counts.canonical_identity_policy_generation_id
        = generation.canonical_identity_policy_generation_id
LEFT JOIN suppression_counts
  ON suppression_counts.canonical_identity_policy_generation_id
        = generation.canonical_identity_policy_generation_id
LEFT JOIN residual_counts
  ON residual_counts.canonical_identity_policy_generation_id
        = generation.canonical_identity_policy_generation_id;

GRANT SELECT ON
    finance.canonical_identity_source_authority_policies,
    finance.canonical_identity_authority_intervals,
    finance.canonical_identity_source_suppressions
TO finance_shadow_ingest;

GRANT INSERT ON
    finance.canonical_identity_source_authority_policies,
    finance.canonical_identity_authority_intervals,
    finance.canonical_identity_source_suppressions
TO finance_shadow_ingest;

GRANT SELECT ON
    finance_read.identity_source_authority_summary
TO finance_readonly;

INSERT INTO finance.schema_migrations(version, name, checksum)
VALUES (:'migration_version', :'migration_name', :'migration_checksum')
ON CONFLICT (version) DO NOTHING;

COMMIT;
