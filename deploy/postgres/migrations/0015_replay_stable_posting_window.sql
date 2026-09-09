BEGIN;

-- Migration 0015 widens who may declare a posting-date window on an authority
-- interval.  Migration 0014 required provider-stable identifiers, which was too
-- narrow: a synthetic CSV extract whose row identifiers are derived
-- deterministically from content and occurrence is not provider-stable, but it
-- *is* replay-stable -- the same extract always yields the same identifiers in
-- the same order.  That is exactly the property a widened window needs, because
-- it is what keeps the occurrence multiset and therefore the one-to-one
-- matching fixed across replays.
--
-- A source with neither kind of identifier still may not declare a window:
-- nothing would pin which row is which.  Keep this in step with
-- finance_store.identity.SourceCoverageEvidence.__post_init__.
--
-- Nothing in 0011, 0012, 0013 or 0014 is rewritten.  Only the one CHECK
-- constraint 0014 added is replaced, and it is replaced with a strictly weaker
-- predicate, so every row that satisfied the old constraint still satisfies the
-- new one and no stored decision or proof shape changes.

ALTER TABLE finance.canonical_identity_authority_intervals
    DROP CONSTRAINT IF EXISTS
        canonical_identity_authority_intervals_posting_window_stable_check;
ALTER TABLE finance.canonical_identity_authority_intervals
    ADD CONSTRAINT
        canonical_identity_authority_intervals_posting_window_stable_check
    CHECK (
        posting_date_tolerance_days = 0
        OR stable_id_support
        OR replay_stable_ids
    );

COMMENT ON CONSTRAINT
    canonical_identity_authority_intervals_posting_window_stable_check
    ON finance.canonical_identity_authority_intervals IS
    'A declared posting-date window requires identifiers that pin the '
    'occurrence mapping: provider-stable identifiers, or deterministic '
    'replay-stable extract identifiers. Neither means no window.';

INSERT INTO finance.schema_migrations(version, name, checksum)
VALUES (:'migration_version', :'migration_name', :'migration_checksum')
ON CONFLICT (version) DO NOTHING;

COMMIT;
