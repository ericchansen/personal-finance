#!/bin/sh
set -eu

. /scripts/secret-env.sh
load_pgpassword
(unset PGHOST; /scripts/verify-backup.sh)

dump="/backups/$BACKUP_BASENAME.dump"
pg_restore \
  --dbname="$PGDATABASE" \
  --exit-on-error \
  --no-owner \
  --no-privileges \
  "$dump"

manifest="/backups/$BACKUP_BASENAME.json"
expected_state_sha=$(
  sed -n 's/.*"stateSha256":"\([0-9a-f]\{64\}\)".*/\1/p' "$manifest"
)
state_material=$(
  FINANCE_SHADOW_LEGACY_QUIESCED=confirmed /scripts/backup-state.sh
)
restored_state_sha=$(
  printf '%s' "$state_material" | sha256sum | awk '{print $1}'
)
if [ -z "$expected_state_sha" ] || [ "$restored_state_sha" != "$expected_state_sha" ]; then
  echo "Restored database state does not match the backup manifest" >&2
  exit 1
fi

# Custom-format database dumps do not contain cluster roles. Pending migrations
# may grant privileges before full login provisioning can reference new tables.
FINANCE_SHADOW_ROLES_ONLY=true /scripts/provision-logins.sh
POSTGRES_MIGRATION_BACKUP="$BACKUP_BASENAME" \
FINANCE_SHADOW_LEGACY_QUIESCED=confirmed \
  /scripts/migrate.sh
/scripts/provision-logins.sh
gate_state=$(
  psql --no-psqlrc --tuples-only --no-align --set=ON_ERROR_STOP=1 \
    --command="SELECT migrations_blocked FROM finance.writer_gate WHERE singleton"
)
if [ "$gate_state" != "f" ]; then
  echo "Restored migration did not release its writer gate" >&2
  exit 1
fi

psql --no-psqlrc --tuples-only --no-align --set=ON_ERROR_STOP=1 \
  --command="SELECT count(*) FROM finance.schema_migrations" >/dev/null
psql --no-psqlrc --tuples-only --no-align --set=ON_ERROR_STOP=1 \
  --command="SELECT count(*) FROM finance_read.migration_status" >/dev/null

loader_password=$(tr -d '\r\n' <"$LOADER_PASSWORD_FILE")
agent_password=$(tr -d '\r\n' <"$AGENT_PASSWORD_FILE")
backup_password=$(tr -d '\r\n' <"$BACKUP_PASSWORD_FILE")
PGPASSWORD="$loader_password" psql --no-psqlrc --quiet --set=ON_ERROR_STOP=1 \
  --username=finance_shadow_loader \
  --command="SELECT count(*) FROM finance.source_blobs" >/dev/null
agent_read_only=$(
  PGPASSWORD="$agent_password" psql --no-psqlrc --quiet \
    --tuples-only --no-align --set=ON_ERROR_STOP=1 \
    --username=finance_shadow_agent \
    --command="SHOW default_transaction_read_only"
)
if [ "$agent_read_only" != "on" ]; then
  echo "Restored agent login is not read-only" >&2
  exit 1
fi
PGPASSWORD="$agent_password" psql --no-psqlrc --quiet --set=ON_ERROR_STOP=1 \
  --username=finance_shadow_agent \
  --command="SELECT count(*) FROM finance_read.shadow_status" >/dev/null
PGPASSWORD="$backup_password" psql --no-psqlrc --quiet --set=ON_ERROR_STOP=1 \
  --username=finance_shadow_backup_login \
  --command="SELECT finance.backup_state_manifest()" >/dev/null
unset loader_password agent_password backup_password
echo "Restore rehearsal passed in disposable instance."
