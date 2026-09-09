BEGIN;

ALTER TABLE finance.source_accounts
    DROP CONSTRAINT source_accounts_status_check;

ALTER TABLE finance.source_accounts
    ADD CONSTRAINT source_accounts_status_check
    CHECK (status IN ('active', 'closed', 'excluded', 'unknown'));

INSERT INTO finance.schema_migrations(version, name, checksum)
VALUES (:'migration_version', :'migration_name', :'migration_checksum')
ON CONFLICT (version) DO NOTHING;

COMMIT;
