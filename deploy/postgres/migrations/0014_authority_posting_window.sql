BEGIN;

-- Migration 0014 records the declared posting-date window an authoritative
-- source may use to reach a lower-priority observation of the same settlement.
-- Nothing in 0011, 0012 or 0013 is rewritten.  Two writers can stamp one
-- economic event on different days -- a stable QFX extract records the
-- institution's posting date while a legacy aggregator export records the day it
-- saw the row -- so an operator may declare a bounded tolerance on the
-- authoritative interval's coverage evidence.  The window is evidence, not an
-- inference, and it is stored alongside the interval it belongs to.
--
-- The suppression proof shape is unchanged: migration 0012's
-- authoritativeCount/lowerCount/bucketParticipantCount branch already accepts
-- the additional camelCase integer keys (sourceDayDistanceDays,
-- maxPostingDateToleranceDays) that a windowed decision carries, so
-- finance.valid_competing_candidate_proof does not need another branch.

ALTER TABLE finance.canonical_identity_authority_intervals
    ADD COLUMN IF NOT EXISTS posting_date_tolerance_days integer NOT NULL
        DEFAULT 0;

-- A window wider than this stops being a settlement-lag correction and starts
-- being a guess, so the ceiling is enforced by the schema as well as by the
-- resolver.  Keep this in step with
-- finance_store.identity.MAX_POSTING_DATE_TOLERANCE_DAYS.
ALTER TABLE finance.canonical_identity_authority_intervals
    DROP CONSTRAINT IF EXISTS
        canonical_identity_authority_intervals_posting_window_check;
ALTER TABLE finance.canonical_identity_authority_intervals
    ADD CONSTRAINT canonical_identity_authority_intervals_posting_window_check
    CHECK (posting_date_tolerance_days BETWEEN 0 AND 5);

-- Only a source with stable identifiers can prove the occurrence mapping stayed
-- one-to-one across a widened window, so a weak-identity interval may not carry
-- one.
ALTER TABLE finance.canonical_identity_authority_intervals
    DROP CONSTRAINT IF EXISTS
        canonical_identity_authority_intervals_posting_window_stable_check;
ALTER TABLE finance.canonical_identity_authority_intervals
    ADD CONSTRAINT
        canonical_identity_authority_intervals_posting_window_stable_check
    CHECK (posting_date_tolerance_days = 0 OR stable_id_support);

COMMENT ON COLUMN
    finance.canonical_identity_authority_intervals.posting_date_tolerance_days IS
    'Declared maximum source-day distance this interval may reach when '
    'suppressing a lower-priority observation of the same settlement. Zero '
    'means same-day agreement is required.';

-- Safe aggregate read model: how many windowed suppressions a generation
-- produced and how far apart the two writers actually were.  No account,
-- description, amount or day is exposed, only counts and distances.
CREATE OR REPLACE VIEW finance_read.identity_posting_window_summary AS
SELECT
    decision_row.canonical_identity_policy_generation_id,
    decision_row.feature_vector ->> 'authoritativeSourceFamily'
        AS authoritative_source_family,
    decision_row.feature_vector ->> 'suppressedSourceFamily'
        AS suppressed_source_family,
    (decision_row.feature_vector ->> 'sourceDayDistanceDays')::integer
        AS source_day_distance_days,
    (decision_row.feature_vector ->> 'postingDateToleranceDays')::integer
        AS posting_date_tolerance_days,
    count(*) AS decision_count
FROM finance.canonical_identity_automatic_decisions AS decision_row
WHERE decision_row.outcome = 'source-suppressed'
  AND decision_row.feature_vector ->> 'sameSourceDay' = 'false'
  AND decision_row.feature_vector ? 'sourceDayDistanceDays'
GROUP BY
    decision_row.canonical_identity_policy_generation_id,
    decision_row.feature_vector ->> 'authoritativeSourceFamily',
    decision_row.feature_vector ->> 'suppressedSourceFamily',
    (decision_row.feature_vector ->> 'sourceDayDistanceDays')::integer,
    (decision_row.feature_vector ->> 'postingDateToleranceDays')::integer;

GRANT SELECT ON
    finance_read.identity_posting_window_summary
TO finance_readonly;

INSERT INTO finance.schema_migrations(version, name, checksum)
VALUES (:'migration_version', :'migration_name', :'migration_checksum')
ON CONFLICT (version) DO NOTHING;

COMMIT;
