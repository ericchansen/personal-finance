BEGIN;

ALTER TABLE finance.ingestion_runs
    ADD COLUMN IF NOT EXISTS admission_hash text;

DO $do$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'ingestion_runs_admission_hash_check'
          AND conrelid = 'finance.ingestion_runs'::regclass
    ) THEN
        ALTER TABLE finance.ingestion_runs
            ADD CONSTRAINT ingestion_runs_admission_hash_check
            CHECK (
                admission_hash IS NULL
                OR admission_hash ~ '^[0-9a-f]{64}$'
            );
    END IF;
END;
$do$;

DO $do$
DECLARE
    constraint_record record;
BEGIN
    FOR constraint_record IN
        SELECT conname
        FROM pg_constraint
        WHERE conrelid = 'finance.artifact_observations'::regclass
          AND contype = 'u'
          AND pg_get_constraintdef(oid) LIKE
              'UNIQUE (source_blob_id, observation_kind, source_identity_hash, observation_hash)%'
    LOOP
        EXECUTE format(
            'ALTER TABLE finance.artifact_observations DROP CONSTRAINT %I',
            constraint_record.conname
        );
    END LOOP;
END;
$do$;

CREATE UNIQUE INDEX IF NOT EXISTS artifact_observations_occurrence_uq
ON finance.artifact_observations(
    source_blob_id,
    observation_kind,
    source_identity_hash,
    observation_hash,
    record_index
);

ALTER TABLE finance.writer_gate
    ADD COLUMN IF NOT EXISTS owner_token text;

UPDATE finance.writer_gate
SET owner_token = CASE
    WHEN migrations_blocked THEN 'legacy-migration-bootstrap'
    ELSE NULL
END;

DO $do$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'writer_gate_owner_check'
          AND conrelid = 'finance.writer_gate'::regclass
    ) THEN
        ALTER TABLE finance.writer_gate
            ADD CONSTRAINT writer_gate_owner_check
            CHECK (
                (migrations_blocked AND owner_token IS NOT NULL)
                OR (NOT migrations_blocked AND owner_token IS NULL)
            );
    END IF;
END;
$do$;

INSERT INTO finance.schema_migrations(version, name, checksum)
VALUES (:'migration_version', :'migration_name', :'migration_checksum')
ON CONFLICT (version) DO NOTHING;

COMMIT;
