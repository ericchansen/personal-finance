#!/bin/sh
set -eu

. /scripts/secret-env.sh
load_pgpassword

: "${PGHOST:?PGHOST is required}"
: "${PGDATABASE:?PGDATABASE is required}"
: "${PGUSER:?PGUSER is required}"

stamp=$(date -u +%Y%m%dT%H%M%SZ)
base="finance-shadow-$stamp-$(date +%N)-$$"
temporary="/backups/.$base"
dump="$temporary.dump"
digest="$temporary.sha256"
manifest="$temporary.json"
lock_fifo="/tmp/$base-lock.fifo"
lock_ready="/tmp/$base-lock-ready"
lock_pid=
lock_fd_open=false

release_lock() {
  if [ "$lock_fd_open" = "true" ]; then
    printf '%s\n' \
      "SELECT pg_advisory_unlock(hashtextextended('finance-shadow-authority-writer', 0));" \
      "\\q" >&3
    exec 3>&-
    lock_fd_open=false
    wait "$lock_pid" 2>/dev/null || true
    lock_pid=
  elif [ -n "$lock_pid" ]; then
    kill "$lock_pid" 2>/dev/null || true
    wait "$lock_pid" 2>/dev/null || true
    lock_pid=
  fi
}

cleanup() {
  release_lock
  rm -f "$dump" "$digest" "$manifest" "$lock_fifo" "$lock_ready"
}
trap cleanup EXIT
umask 077

mkfifo "$lock_fifo"
BACKUP_LOCK_READY="$lock_ready" \
  psql --no-psqlrc --quiet --set=ON_ERROR_STOP=1 \
  <"$lock_fifo" >/dev/null 2>&1 &
lock_pid=$!
exec 3>"$lock_fifo"
lock_fd_open=true
cat >&3 <<'SQL'
SELECT pg_advisory_lock(
  hashtextextended('finance-shadow-authority-writer', 0)
);
\! touch "$BACKUP_LOCK_READY"
SQL
attempt=0
while [ ! -f "$lock_ready" ]; do
  attempt=$((attempt + 1))
  if ! kill -0 "$lock_pid" 2>/dev/null || [ "$attempt" -gt 300 ]; then
    echo "Could not acquire the shadow writer lock for backup" >&2
    exit 1
  fi
  sleep 0.1
done

pg_dump \
  --format=custom \
  --compress=9 \
  --no-owner \
  --no-privileges \
  --schema=finance \
  --schema=finance_read \
  --file="$dump"
dump_sha=$(sha256sum "$dump" | awk '{print $1}')
printf '%s  %s.dump\n' "$dump_sha" "$base" >"$digest"
schema_count=$(
  psql --no-psqlrc --tuples-only --no-align --set=ON_ERROR_STOP=1 \
    --command="SELECT count(*) FROM finance.schema_migrations"
)
observation_count=$(
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
state_material=$(
  /scripts/backup-state.sh
)
state_sha=$(
  printf '%s' "$state_material" | sha256sum | awk '{print $1}'
)
instance_id=$(
  printf '%s' "$state_material" |
    sed -n 's/.*"instanceId": "\([^"]*\)".*/\1/p'
)
if [ -z "$instance_id" ]; then
  echo "Backup state did not identify the database instance" >&2
  exit 1
fi
created_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
backup_id=$(
  printf '%s' "$instance_id|$created_at|$dump_sha|$state_sha" |
    sha256sum | awk '{print $1}'
)
printf '%s\n' \
  "{\"schemaVersion\":2,\"backup\":\"$base\",\"backupId\":\"$backup_id\",\"createdAt\":\"$created_at\",\"instanceId\":\"$instance_id\",\"dumpSha256\":\"$dump_sha\",\"stateSha256\":\"$state_sha\",\"migrationCount\":$schema_count,\"observationCount\":$observation_count}" \
  >"$manifest"

mv "$dump" "/backups/$base.dump"
mv "$digest" "/backups/$base.sha256"
mv "$manifest" "/backups/$base.json"

BACKUP_BASENAME="$base" /scripts/verify-backup.sh
BACKUP_RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-30}" \
BACKUP_MINIMUM_COUNT="${BACKUP_MINIMUM_COUNT:-1}" \
  /scripts/verify-retention.sh
release_lock
rm -f "$lock_fifo" "$lock_ready"
trap - EXIT
echo "Backup created: $base"
