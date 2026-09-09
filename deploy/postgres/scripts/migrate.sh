#!/bin/sh
set -eu

. /scripts/secret-env.sh

: "${PGHOST:?PGHOST is required}"
: "${PGDATABASE:?PGDATABASE is required}"
: "${PGUSER:?PGUSER is required}"
: "${FINANCE_SHADOW_ENVIRONMENT:?FINANCE_SHADOW_ENVIRONMENT is required}"

case "$FINANCE_SHADOW_ENVIRONMENT" in
  *[!A-Za-z0-9._-]*|'')
    echo "FINANCE_SHADOW_ENVIRONMENT contains unsupported characters" >&2
    exit 1
    ;;
esac

load_pgpassword

psql_base() {
  psql --no-psqlrc --set=ON_ERROR_STOP=1 --quiet "$@"
}

until pg_isready -h "$PGHOST" -p "${PGPORT:-5432}" -U "$PGUSER" -d "$PGDATABASE" >/dev/null 2>&1; do
  sleep 1
done

marker_exists=$(
  psql_base --tuples-only --no-align \
    --command="SELECT to_regclass('finance.shadow_authority_metadata') IS NOT NULL"
)
if [ "$marker_exists" = "t" ]; then
  persisted_environment=$(
    psql_base --tuples-only --no-align \
      --command="SELECT environment_marker
        FROM finance.shadow_authority_metadata
        WHERE singleton"
  )
  if [ "$persisted_environment" != "$FINANCE_SHADOW_ENVIRONMENT" ]; then
    echo "Shadow environment marker mismatch" >&2
    exit 1
  fi
fi

gate_token="migration-$(date +%s%N)-$$"
gate_armed=false
gate_owner_supported=false
control=""

cleanup() {
  status=$?
  trap - EXIT HUP INT TERM
  [ -z "$control" ] || rm -f "$control"
  if [ "$gate_armed" = "true" ] && [ "$status" -eq 0 ]; then
    if ! psql_base --command="
        BEGIN;
        SELECT pg_advisory_xact_lock(
          hashtextextended('finance-shadow-authority-writer', 0)
        );
        DO \$release\$
        DECLARE released bigint;
        BEGIN
          IF EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_schema = 'finance' AND table_name = 'writer_gate'
              AND column_name = 'owner_token'
          ) THEN
            UPDATE finance.writer_gate
            SET migrations_blocked = false, owner_token = NULL,
                changed_at = clock_timestamp()
            WHERE singleton AND migrations_blocked
              AND owner_token = CASE WHEN '$gate_owner_supported' = 't'
                THEN '$gate_token' ELSE 'legacy-migration-bootstrap' END;
          ELSE
            UPDATE finance.writer_gate
            SET migrations_blocked = false, changed_at = clock_timestamp()
            WHERE singleton AND migrations_blocked;
          END IF;
          GET DIAGNOSTICS released = ROW_COUNT;
          IF released <> 1 THEN
            RAISE EXCEPTION 'migration no longer owns the writer gate';
          END IF;
        END;
        \$release\$;
        COMMIT;" >/dev/null; then
      echo "Failed to release the owned shadow writer gate" >&2
      status=1
    fi
  elif [ "$gate_armed" = "true" ]; then
    echo "Migration failed; writer gate remains closed for recovery" >&2
  fi
  if [ "$status" -eq 0 ]; then
    echo "Migrations are current."
  fi
  exit "$status"
}

trap cleanup EXIT HUP INT TERM

gate_exists=$(
  psql_base --tuples-only --no-align \
    --command="SELECT to_regclass('finance.writer_gate') IS NOT NULL"
)
if [ "$gate_exists" = "t" ]; then
  gate_owner_supported=$(
    psql_base --tuples-only --no-align --command="
      SELECT EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'finance'
          AND table_name = 'writer_gate'
          AND column_name = 'owner_token'
      )"
  )
  if [ "$gate_owner_supported" = "t" ]; then
    armed=$(
      psql_base --tuples-only --no-align --command="
          BEGIN;
          WITH lock_acquired AS MATERIALIZED (
            SELECT pg_advisory_xact_lock(
              hashtextextended('finance-shadow-authority-writer', 0)
            )
          ), changed AS (
            UPDATE finance.writer_gate
            SET migrations_blocked = true,
                owner_token = '$gate_token',
                changed_at = clock_timestamp()
            FROM lock_acquired
            WHERE singleton AND NOT migrations_blocked
            RETURNING 1
          )
          SELECT count(*) FROM changed;
          COMMIT;"
    )
    if [ "$armed" != "1" ]; then
      echo "Another migration owns the shadow writer gate" >&2
      exit 1
    fi
  else
    already_blocked=$(
      psql_base --tuples-only --no-align \
        --command="SELECT migrations_blocked
          FROM finance.writer_gate
          WHERE singleton"
    )
    if [ "$already_blocked" = "t" ]; then
      echo "Shadow writer gate is already closed" >&2
      exit 1
    fi
    psql_base --command="
      BEGIN;
      SELECT pg_advisory_xact_lock(
        hashtextextended('finance-shadow-authority-writer', 0)
      );
      UPDATE finance.writer_gate
      SET migrations_blocked = true,
          changed_at = clock_timestamp()
      WHERE singleton;
      COMMIT;"
  fi
  gate_armed=true
fi

backup_verified=false
if [ -n "${POSTGRES_MIGRATION_BACKUP:-}" ]; then
  BACKUP_BASENAME=$POSTGRES_MIGRATION_BACKUP /scripts/verify-backup.sh >/dev/null
  backup_verified=true
fi

control=$(mktemp /tmp/finance-shadow-migrations.XXXXXX.sql)

cat >"$control" <<'SQL'
\set ON_ERROR_STOP on
SELECT pg_advisory_lock(
    hashtextextended('finance-shadow-authority-writer', 0)
);

CREATE OR REPLACE FUNCTION pg_temp.finance_shadow_migration_needed(
    requested_version text,
    requested_name text,
    requested_checksum text,
    legacy_crlf_checksum text,
    backup_is_verified boolean
) RETURNS boolean
LANGUAGE plpgsql
AS $function$
DECLARE
    applied_name text;
    applied_checksum text;
    matched_rows bigint;
    evidence_exists boolean := false;
    relation_name text;
BEGIN
    IF to_regclass('finance.schema_migrations') IS NULL THEN
        RETURN true;
    END IF;
    EXECUTE
        'SELECT name, checksum FROM finance.schema_migrations WHERE version = $1'
        INTO applied_name, applied_checksum
        USING requested_version;
    GET DIAGNOSTICS matched_rows = ROW_COUNT;
    IF matched_rows = 0 THEN
        FOR relation_name IN
            SELECT tablename
            FROM pg_tables
            WHERE schemaname = 'finance'
              AND tablename NOT IN (
                  'schema_migrations',
                  'shadow_authority_metadata',
                  'writer_gate'
              )
            ORDER BY tablename
        LOOP
            EXECUTE format(
                'SELECT EXISTS (SELECT 1 FROM finance.%I)',
                relation_name
            ) INTO evidence_exists;
            EXIT WHEN evidence_exists;
        END LOOP;
        IF evidence_exists AND NOT backup_is_verified THEN
            RAISE EXCEPTION
                'a verified POSTGRES_MIGRATION_BACKUP is required before migrating non-empty evidence'
                USING ERRCODE = '55000';
        END IF;
        RETURN true;
    END IF;
    IF applied_name IS DISTINCT FROM requested_name
       OR applied_checksum IS DISTINCT FROM requested_checksum THEN
        IF applied_name IS NOT DISTINCT FROM requested_name
           AND applied_checksum IS NOT DISTINCT FROM legacy_crlf_checksum
           AND legacy_crlf_checksum IS DISTINCT FROM requested_checksum
           AND backup_is_verified THEN
            UPDATE finance.schema_migrations
            SET checksum = requested_checksum
            WHERE version = requested_version
              AND name = requested_name
              AND checksum = legacy_crlf_checksum;
            RETURN false;
        END IF;
        RAISE EXCEPTION
            'applied migration % differs from the immutable migration file',
            requested_version
            USING ERRCODE = '55000';
    END IF;
    RETURN false;
END;
$function$;
SQL

for migration in /migrations/*.sql; do
  [ -f "$migration" ] || continue
  filename=$(basename "$migration")
  version=${filename%%_*}
  name=${filename#*_}
  name=${name%.sql}

  case "$version" in
    *[!0-9]*|'')
      echo "Invalid migration filename: $filename" >&2
      exit 1
      ;;
  esac

  checksum=$(sha256sum "$migration" | awk '{print $1}')
  legacy_crlf_checksum=$(
    awk '{printf "%s\r\n", $0}' "$migration" | sha256sum | awk '{print $1}'
  )
  {
    printf '%s\n' "\\set migration_version '$version'"
    printf '%s\n' "\\set migration_name '$name'"
    printf '%s\n' "\\set migration_checksum '$checksum'"
    printf '%s\n' "\\set migration_legacy_crlf_checksum '$legacy_crlf_checksum'"
    printf '%s\n' "\\set shadow_environment '$FINANCE_SHADOW_ENVIRONMENT'"
    printf '%s\n' "\\set backup_verified '$backup_verified'"
    echo "SELECT pg_temp.finance_shadow_migration_needed("
    echo "  :'migration_version', :'migration_name', :'migration_checksum',"
    echo "  :'migration_legacy_crlf_checksum',"
    echo "  :'backup_verified'::boolean"
    printf '%s\n' ") AS apply_migration \\gset"
    printf '%s\n' "\\if :apply_migration"
    printf '%s\n' "\\echo 'Applying: $filename'"
    printf '%s\n' "\\ir $migration"
    printf '%s\n' "\\else"
    printf '%s\n' "\\echo 'Already applied: $filename'"
    printf '%s\n' "\\endif"
  } >>"$control"
done

cat >>"$control" <<'SQL'
SELECT pg_advisory_unlock(
    hashtextextended('finance-shadow-authority-writer', 0)
);
SQL

psql_base \
  --file="$control"
