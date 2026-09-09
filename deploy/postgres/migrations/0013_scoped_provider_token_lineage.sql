BEGIN;

-- Migration 0013 records scoped shared provider-token lineage.  Nothing in 0011
-- or 0012 is rewritten: the relation kind, outcome and confidence tier a scoped
-- token link uses are already admissible, so this migration only adds the proof
-- shape that carries the namespace counts and the declared settlement skew, and
-- the durable scope declaration those decisions are bound to.

-- A shared provider token proves nothing on its own; it is only lineage inside
-- a declared scope.  The proof therefore records how many claims each declared
-- namespace contributed to the token bucket, so a reviewer can see that the
-- occurrence mapping was one-to-one rather than assumed.
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
        WHEN (
                SELECT count(*)
                FROM jsonb_object_keys(candidate) AS object_key(key)
             ) = 5
             AND candidate ? 'leftNamespaceCount'
             AND candidate ? 'rightNamespaceCount'
             AND candidate ? 'sharedTokenBucketCount'
             AND candidate ? 'dateDistanceDays'
             AND candidate ? 'maxDaySkewDays'
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

-- The durable operator declaration itself.  Both namespaces are stored hashed:
-- a source account identifier is provider data and never lands in a read model.
-- The row is the audit trail proving why two writers were ever allowed to share
-- a token, and it is scoped to exactly one canonical account.
CREATE TABLE IF NOT EXISTS finance.canonical_identity_provider_token_scopes (
    canonical_identity_provider_token_scope_id uuid PRIMARY KEY
        DEFAULT gen_random_uuid(),
    canonical_identity_policy_generation_id uuid NOT NULL REFERENCES
        finance.canonical_identity_policy_generations(
            canonical_identity_policy_generation_id
        ),
    scope_hash text NOT NULL CHECK (scope_hash ~ '^[0-9a-f]{64}$'),
    map_hash text NOT NULL CHECK (map_hash ~ '^[0-9a-f]{64}$'),
    policy_version text NOT NULL CHECK (btrim(policy_version) <> ''),
    canonical_account_hash text NOT NULL
        CHECK (canonical_account_hash ~ '^[0-9a-f]{64}$'),
    left_namespace_hash text NOT NULL
        CHECK (left_namespace_hash ~ '^[0-9a-f]{64}$'),
    right_namespace_hash text NOT NULL
        CHECK (right_namespace_hash ~ '^[0-9a-f]{64}$'),
    left_provider_id_kind text NOT NULL CHECK (left_provider_id_kind IN (
        'ofx-fitid', 'simplefin-id', 'scoped-provider-id'
    )),
    right_provider_id_kind text NOT NULL CHECK (right_provider_id_kind IN (
        'ofx-fitid', 'simplefin-id', 'scoped-provider-id'
    )),
    max_day_skew integer NOT NULL CHECK (max_day_skew BETWEEN 0 AND 3),
    decision text NOT NULL CHECK (btrim(decision) <> ''),
    decided_at timestamptz NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT now(),
    CHECK (left_namespace_hash <> right_namespace_hash),
    UNIQUE (canonical_identity_policy_generation_id, scope_hash)
);

CREATE INDEX IF NOT EXISTS
    canonical_identity_provider_token_scopes_generation_idx
    ON finance.canonical_identity_provider_token_scopes
    (canonical_identity_policy_generation_id);

-- A declaration is evidence, so it is append only exactly like the authority
-- evidence 0012 introduced.  A scope may be superseded by a new generation,
-- never silently rewritten under an existing one.
DROP TRIGGER IF EXISTS canonical_identity_provider_token_scopes_append_only
    ON finance.canonical_identity_provider_token_scopes;
CREATE TRIGGER canonical_identity_provider_token_scopes_append_only
    BEFORE UPDATE OR DELETE ON finance.canonical_identity_provider_token_scopes
    FOR EACH ROW EXECUTE FUNCTION finance.reject_append_only_change();

-- Safe aggregate read model: how many scopes a generation declared and how many
-- decisions each one actually settled.  No token, account, or namespace value is
-- exposed, only counts and the hashes already stored on the decision.
CREATE OR REPLACE VIEW finance_read.identity_provider_token_scope_summary AS
SELECT
    scope_row.canonical_identity_policy_generation_id,
    scope_row.scope_hash,
    scope_row.policy_version,
    scope_row.max_day_skew,
    count(decision_row.canonical_identity_automatic_decision_id)
        AS decision_count
FROM finance.canonical_identity_provider_token_scopes AS scope_row
LEFT JOIN finance.canonical_identity_automatic_decisions AS decision_row
    ON decision_row.canonical_identity_policy_generation_id
        = scope_row.canonical_identity_policy_generation_id
    AND decision_row.rationale_code = 'scoped-shared-provider-token-lineage'
    AND decision_row.feature_vector ->> 'providerTokenScopeHash'
        = scope_row.scope_hash
GROUP BY
    scope_row.canonical_identity_policy_generation_id,
    scope_row.scope_hash,
    scope_row.policy_version,
    scope_row.max_day_skew;

GRANT SELECT ON
    finance.canonical_identity_provider_token_scopes
TO finance_shadow_ingest;

GRANT INSERT ON
    finance.canonical_identity_provider_token_scopes
TO finance_shadow_ingest;

GRANT SELECT ON
    finance_read.identity_provider_token_scope_summary
TO finance_readonly;

INSERT INTO finance.schema_migrations(version, name, checksum)
VALUES (:'migration_version', :'migration_name', :'migration_checksum')
ON CONFLICT (version) DO NOTHING;

COMMIT;
