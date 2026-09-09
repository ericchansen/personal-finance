#!/bin/sh
set -eu

: "${BACKUP_BASENAME:?BACKUP_BASENAME is required}"
case "$BACKUP_BASENAME" in
  *[!A-Za-z0-9._-]*|'')
    echo "BACKUP_BASENAME contains unsupported characters" >&2
    exit 1
    ;;
esac

dump="/backups/$BACKUP_BASENAME.dump"
digest="/backups/$BACKUP_BASENAME.sha256"
manifest="/backups/$BACKUP_BASENAME.json"

for path in "$dump" "$digest" "$manifest"; do
  if [ ! -f "$path" ] || [ -L "$path" ]; then
    echo "Backup set is incomplete or contains a symlink" >&2
    exit 1
  fi
done

expected=$(awk 'NR == 1 { print $1 }' "$digest")
actual=$(sha256sum "$dump" | awk '{print $1}')
if [ -z "$expected" ] || [ "$actual" != "$expected" ]; then
  echo "Backup digest verification failed" >&2
  exit 1
fi
if [ "$(cat "$digest")" != "$actual  $BACKUP_BASENAME.dump" ]; then
  echo "Backup digest sidecar names a different backup" >&2
  exit 1
fi
if ! grep -Fq "\"dumpSha256\":\"$actual\"" "$manifest"; then
  echo "Backup manifest does not bind the dump digest" >&2
  exit 1
fi
if ! grep -Fq "\"backup\":\"$BACKUP_BASENAME\"" "$manifest"; then
  echo "Backup manifest names a different backup" >&2
  exit 1
fi
if ! grep -Eq \
    '"schemaVersion":2,.*"backupId":"[0-9a-f]{64}".*"createdAt":"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z".*"instanceId":"[^"]+".*"stateSha256":"[0-9a-f]{64}"' \
    "$manifest"; then
  echo "Backup manifest lacks an exact database state binding" >&2
  exit 1
fi
backup_id=$(sed -n 's/.*"backupId":"\([0-9a-f]\{64\}\)".*/\1/p' "$manifest")
created_at=$(sed -n 's/.*"createdAt":"\([^"]*\)".*/\1/p' "$manifest")
instance_id=$(sed -n 's/.*"instanceId":"\([^"]*\)".*/\1/p' "$manifest")
manifest_state_sha=$(
  sed -n 's/.*"stateSha256":"\([0-9a-f]\{64\}\)".*/\1/p' "$manifest"
)
expected_backup_id=$(
  printf '%s' "$instance_id|$created_at|$actual|$manifest_state_sha" |
    sha256sum | awk '{print $1}'
)
if [ "$backup_id" != "$expected_backup_id" ]; then
  echo "Backup ID does not match its immutable manifest fields" >&2
  exit 1
fi
pg_restore --list "$dump" >/dev/null
if [ -n "${PGHOST:-}" ]; then
  . /scripts/secret-env.sh
  load_pgpassword
  current_count=$(
    psql --no-psqlrc --quiet --tuples-only --no-align --set=ON_ERROR_STOP=1 \
      --command="
        CREATE OR REPLACE FUNCTION pg_temp.finance_shadow_observation_count()
        RETURNS bigint LANGUAGE plpgsql AS \$function\$
        DECLARE
          total bigint := 0;
          item_count bigint;
          relation_name text;
        BEGIN
          FOREACH relation_name IN ARRAY ARRAY[
            'transaction_observations', 'balance_observations',
            'position_observations', 'valuation_observations',
            'artifact_observations'
          ] LOOP
            IF to_regclass('finance.' || relation_name) IS NOT NULL THEN
              EXECUTE format('SELECT count(*) FROM finance.%I', relation_name)
                INTO item_count;
              total := total + item_count;
            END IF;
          END LOOP;
          RETURN total;
        END;
        \$function\$;
        SELECT pg_temp.finance_shadow_observation_count();"
  )
  if [ "${VERIFY_CURRENT_STATE:-true}" = "true" ] &&
      ! grep -Fq "\"observationCount\":$current_count" "$manifest"; then
    echo "Backup observation count does not match the current database" >&2
    exit 1
  fi
  state_material=$(
    /scripts/backup-state.sh
  )
  current_instance_id=$(
    printf '%s' "$state_material" |
      sed -n 's/.*"instanceId": "\([^"]*\)".*/\1/p'
  )
  if [ "$current_instance_id" != "$instance_id" ]; then
    echo "Backup belongs to a different database instance" >&2
    exit 1
  fi
  state_sha=$(printf '%s' "$state_material" | sha256sum | awk '{print $1}')
  if [ "${VERIFY_CURRENT_STATE:-true}" = "true" ] &&
      ! grep -Fq "\"stateSha256\":\"$state_sha\"" "$manifest"; then
    echo "Backup does not bind the current database state" >&2
    exit 1
  fi
fi
echo "Backup verified: $BACKUP_BASENAME"
