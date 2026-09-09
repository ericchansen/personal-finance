BEGIN;

-- Migration 0017 fixes a model error, not a data error.
--
-- Migration 0011 assumed every canonical identity observation is a row in
-- finance.transaction_observations, so it made the membership tables carry a
-- uuid foreign key to that table.  That assumption does not hold.  The
-- canonical producer resolves identity over the published identityScope, whose
-- observation identifiers are content hashes -- sha256 of the row fingerprint
-- and its occurrence -- and most of those rows originate in artifact
-- observations (statement extracts, aggregator snapshots) that were never
-- ingested as transaction_observations rows.  A live apply therefore rolled
-- back with a check violation.
--
-- The wrong repairs would be to mint a uuid from the hash, which fabricates a
-- foreign key to a row that does not exist, or to drop the constraint, which
-- would let a genuinely broken reference through.  Instead this migration
-- widens the model to say what is actually true: a transaction-observation
-- member is identified EITHER by a real uuid that references an ingested
-- observation OR by the exact published observation identity hash, and exactly
-- one of the two is present.
--
-- Nothing in 0011 through 0016 is rewritten.  No existing row changes: every
-- row already written carries a real uuid and keeps it, and the new column is
-- nullable so the existing check remains satisfiable.  Source-claim members are
-- untouched -- they never referenced transaction_observations.  The append-only
-- triggers installed by 0011 stay in force; this migration adds columns and
-- constraints, and never updates or deletes a row.

-- ---------------------------------------------------------------------------
-- Observation memberships
-- ---------------------------------------------------------------------------

ALTER TABLE finance.canonical_identity_observation_memberships
    ADD COLUMN IF NOT EXISTS observation_identity_hash text;

ALTER TABLE finance.canonical_identity_observation_memberships
    ALTER COLUMN transaction_observation_id DROP NOT NULL;

ALTER TABLE finance.canonical_identity_observation_memberships
    DROP CONSTRAINT IF EXISTS
        canonical_identity_observation_memberships_identity_hash_check;
ALTER TABLE finance.canonical_identity_observation_memberships
    ADD CONSTRAINT
        canonical_identity_observation_memberships_identity_hash_check
    CHECK (
        observation_identity_hash IS NULL
        OR observation_identity_hash ~ '^[0-9a-f]{64}$'
    );

-- Exactly one identity per membership.  Neither is a dangling row; both is an
-- ambiguous row that two readers would resolve differently.
ALTER TABLE finance.canonical_identity_observation_memberships
    DROP CONSTRAINT IF EXISTS
        canonical_identity_observation_memberships_identity_exactly_one_check;
ALTER TABLE finance.canonical_identity_observation_memberships
    ADD CONSTRAINT
        canonical_identity_observation_memberships_identity_exactly_one_check
    CHECK (
        ((transaction_observation_id IS NOT NULL)::integer
            + (observation_identity_hash IS NOT NULL)::integer) = 1
    );

-- 0011's UNIQUE (generation, claim, transaction_observation_id) no longer
-- covers hash-identified members, because SQL treats NULLs as distinct.  The
-- partial index restores the same guarantee on the other branch.
CREATE UNIQUE INDEX IF NOT EXISTS
    canonical_identity_observation_memberships_identity_hash_idx
    ON finance.canonical_identity_observation_memberships (
        canonical_identity_policy_generation_id,
        canonical_identity_source_claim_id,
        observation_identity_hash
    )
    WHERE observation_identity_hash IS NOT NULL;

CREATE INDEX IF NOT EXISTS
    canonical_identity_observation_memberships_identity_lookup_idx
    ON finance.canonical_identity_observation_memberships (
        observation_identity_hash
    )
    WHERE observation_identity_hash IS NOT NULL;

COMMENT ON COLUMN
    finance.canonical_identity_observation_memberships.observation_identity_hash
    IS
    'Exact published canonical identity observation identifier for a member '
    'that is not an ingested finance.transaction_observations row. Set when '
    'transaction_observation_id is null, and only then.';

-- ---------------------------------------------------------------------------
-- Event members
-- ---------------------------------------------------------------------------

ALTER TABLE finance.canonical_identity_event_members
    ADD COLUMN IF NOT EXISTS observation_identity_hash text;

ALTER TABLE finance.canonical_identity_event_members
    DROP CONSTRAINT IF EXISTS
        canonical_identity_event_members_identity_hash_check;
ALTER TABLE finance.canonical_identity_event_members
    ADD CONSTRAINT canonical_identity_event_members_identity_hash_check
    CHECK (
        observation_identity_hash IS NULL
        OR observation_identity_hash ~ '^[0-9a-f]{64}$'
    );

-- 0011 wrote two unnamed table checks on this table that both assume a
-- transaction-observation member always carries a uuid.  They are located by
-- their definition rather than by a generated name, so this migration does not
-- depend on how PostgreSQL numbered them.  The replacements below are named and
-- say the same thing about source-claim members.
DO $do$
DECLARE
    stale_constraint text;
BEGIN
    FOR stale_constraint IN
        SELECT constraint_row.conname
        FROM pg_constraint AS constraint_row
        WHERE constraint_row.conrelid
                = 'finance.canonical_identity_event_members'::regclass
          AND constraint_row.contype = 'c'
          AND pg_get_constraintdef(constraint_row.oid)
                LIKE '%transaction_observation_id IS NOT NULL%'
          AND constraint_row.conname <> 'canonical_identity_event_members_'
                || 'member_identity_exactly_one_check'
          AND constraint_row.conname <> 'canonical_identity_event_members_'
                || 'member_type_identity_check'
    LOOP
        EXECUTE format(
            'ALTER TABLE finance.canonical_identity_event_members '
            'DROP CONSTRAINT %I',
            stale_constraint
        );
    END LOOP;
END;
$do$;

-- A member is a source claim, an ingested observation, or a published
-- observation identity -- exactly one of the three.
ALTER TABLE finance.canonical_identity_event_members
    DROP CONSTRAINT IF EXISTS
        canonical_identity_event_members_member_identity_exactly_one_check;
ALTER TABLE finance.canonical_identity_event_members
    ADD CONSTRAINT
        canonical_identity_event_members_member_identity_exactly_one_check
    CHECK (
        ((canonical_identity_source_claim_id IS NOT NULL)::integer
            + (transaction_observation_id IS NOT NULL)::integer
            + (observation_identity_hash IS NOT NULL)::integer) = 1
    );

-- Source-claim members keep exactly the 0011 rule: a claim id and nothing else.
-- Only the transaction-observation branch gained an alternative.
ALTER TABLE finance.canonical_identity_event_members
    DROP CONSTRAINT IF EXISTS
        canonical_identity_event_members_member_type_identity_check;
ALTER TABLE finance.canonical_identity_event_members
    ADD CONSTRAINT canonical_identity_event_members_member_type_identity_check
    CHECK (
        (member_type = 'source_claim'
            AND canonical_identity_source_claim_id IS NOT NULL)
        OR (member_type = 'transaction_observation'
            AND (
                transaction_observation_id IS NOT NULL
                OR observation_identity_hash IS NOT NULL
            ))
    );

CREATE UNIQUE INDEX IF NOT EXISTS
    canonical_identity_event_members_identity_hash_idx
    ON finance.canonical_identity_event_members (
        canonical_identity_policy_generation_id,
        canonical_identity_event_id,
        observation_identity_hash
    )
    WHERE observation_identity_hash IS NOT NULL;

CREATE INDEX IF NOT EXISTS
    canonical_identity_event_members_identity_lookup_idx
    ON finance.canonical_identity_event_members (observation_identity_hash)
    WHERE observation_identity_hash IS NOT NULL;

COMMENT ON COLUMN
    finance.canonical_identity_event_members.observation_identity_hash IS
    'Exact published canonical identity observation identifier for a '
    'transaction-observation member that is not an ingested '
    'finance.transaction_observations row. Set when transaction_observation_id '
    'is null, and only then.';

-- ---------------------------------------------------------------------------
-- Read model
-- ---------------------------------------------------------------------------

-- Safe aggregate: how a generation's observation members are identified. An
-- operator reading this can tell at a glance whether a generation resolved
-- ingested rows, artifact rows, or a mixture, without seeing an account, an
-- amount, a description, or a source path.
CREATE OR REPLACE VIEW finance_read.identity_observation_binding_summary AS
WITH membership_bindings AS (
    SELECT
        membership.canonical_identity_policy_generation_id,
        'observation_membership'::text AS binding_scope,
        membership.membership_role AS binding_role,
        CASE
            WHEN membership.transaction_observation_id IS NOT NULL
                THEN 'ingested-observation'
            ELSE 'published-identity-hash'
        END AS identity_kind
    FROM finance.canonical_identity_observation_memberships AS membership
    UNION ALL
    SELECT
        member.canonical_identity_policy_generation_id,
        'event_member'::text AS binding_scope,
        member.member_role AS binding_role,
        CASE
            WHEN member.transaction_observation_id IS NOT NULL
                THEN 'ingested-observation'
            ELSE 'published-identity-hash'
        END AS identity_kind
    FROM finance.canonical_identity_event_members AS member
    WHERE member.member_type = 'transaction_observation'
)
SELECT
    canonical_identity_policy_generation_id,
    binding_scope,
    binding_role,
    identity_kind,
    count(*) AS binding_count
FROM membership_bindings
GROUP BY
    canonical_identity_policy_generation_id,
    binding_scope,
    binding_role,
    identity_kind;

-- Table-level grants already cover columns added later, but the writer and the
-- reader are re-stated here so the migration is self-describing.
GRANT SELECT, INSERT ON
    finance.canonical_identity_observation_memberships,
    finance.canonical_identity_event_members
TO finance_shadow_ingest;

GRANT SELECT ON
    finance_read.identity_observation_binding_summary
TO finance_readonly;

INSERT INTO finance.schema_migrations(version, name, checksum)
VALUES (:'migration_version', :'migration_name', :'migration_checksum')
ON CONFLICT (version) DO NOTHING;

COMMIT;
