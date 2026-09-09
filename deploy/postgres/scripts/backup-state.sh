#!/bin/sh
set -eu

function_exists=$(
  psql --no-psqlrc --quiet --tuples-only --no-align --set=ON_ERROR_STOP=1 \
    --command="SELECT to_regprocedure(
      'finance.backup_state_manifest()'
    ) IS NOT NULL"
)
if [ "$function_exists" = "t" ]; then
  exec psql --no-psqlrc --quiet --tuples-only --no-align \
    --set=ON_ERROR_STOP=1 \
    --command="SELECT finance.backup_state_manifest()::text"
fi

if [ "${FINANCE_SHADOW_LEGACY_QUIESCED:-}" != "confirmed" ]; then
  echo "Legacy backup requires FINANCE_SHADOW_LEGACY_QUIESCED=confirmed" >&2
  exit 1
fi

exec psql --no-psqlrc --quiet --tuples-only --no-align \
  --set=ON_ERROR_STOP=1 <<'SQL'
CREATE OR REPLACE FUNCTION pg_temp.finance_legacy_backup_state()
RETURNS jsonb LANGUAGE plpgsql STABLE AS $function$
DECLARE
  table_record record;
  item_count bigint;
  item_digest text;
  tables jsonb := '[]'::jsonb;
  instance text := 'legacy-unmarked';
BEGIN
  IF to_regclass('finance.shadow_authority_metadata') IS NOT NULL THEN
    EXECUTE
      'SELECT instance_id::text FROM finance.shadow_authority_metadata '
      'WHERE singleton'
    INTO instance;
  END IF;
  FOR table_record IN
    SELECT tablename
    FROM pg_tables
    WHERE schemaname = 'finance'
      AND tablename <> 'writer_gate'
    ORDER BY tablename
  LOOP
    EXECUTE format(
      'SELECT count(*), md5(COALESCE(string_agg('
      'md5(row_to_json(item)::text), '''' ORDER BY '
      'md5(row_to_json(item)::text)), '''')) '
      'FROM finance.%I item',
      table_record.tablename
    )
    INTO item_count, item_digest;
    tables := tables || jsonb_build_array(
      jsonb_build_object(
        'table', table_record.tablename,
        'rows', item_count,
        'digest', item_digest
      )
    );
  END LOOP;
  RETURN jsonb_build_object(
    'instanceId', instance,
    'tables', tables
  );
END;
$function$;
SELECT pg_temp.finance_legacy_backup_state()::text;
SQL
