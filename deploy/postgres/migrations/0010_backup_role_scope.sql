BEGIN;

DO $do$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_roles
        WHERE rolname = 'finance_shadow_backup'
    ) THEN
        CREATE ROLE finance_shadow_backup
            NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
            NOREPLICATION NOBYPASSRLS;
    END IF;
END;
$do$;

ALTER ROLE finance_shadow_backup WITH
    NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
    NOREPLICATION NOBYPASSRLS;

REVOKE pg_read_all_data FROM finance_shadow_backup;
REVOKE ALL PRIVILEGES ON SCHEMA public FROM finance_shadow_backup;
REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public
FROM finance_shadow_backup;
REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public
FROM finance_shadow_backup;

GRANT USAGE ON SCHEMA finance, finance_read TO finance_shadow_backup;
GRANT SELECT ON ALL TABLES IN SCHEMA finance, finance_read
TO finance_shadow_backup;
GRANT SELECT ON ALL SEQUENCES IN SCHEMA finance
TO finance_shadow_backup;

ALTER DEFAULT PRIVILEGES IN SCHEMA finance
    GRANT SELECT ON TABLES TO finance_shadow_backup;
ALTER DEFAULT PRIVILEGES IN SCHEMA finance
    GRANT SELECT ON SEQUENCES TO finance_shadow_backup;
ALTER DEFAULT PRIVILEGES IN SCHEMA finance_read
    GRANT SELECT ON TABLES TO finance_shadow_backup;

INSERT INTO finance.schema_migrations(version, name, checksum)
VALUES (:'migration_version', :'migration_name', :'migration_checksum')
ON CONFLICT (version) DO NOTHING;

COMMIT;
