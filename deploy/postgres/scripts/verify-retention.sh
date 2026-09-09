#!/bin/sh
set -eu

: "${BACKUP_RETENTION_DAYS:=30}"
: "${BACKUP_MINIMUM_COUNT:=1}"
case "$BACKUP_RETENTION_DAYS:$BACKUP_MINIMUM_COUNT" in
  *[!0-9:]*|:*|*:)
    echo "Backup retention settings must be non-negative integers" >&2
    exit 1
    ;;
esac

count=0
newest=0
now_epoch=$(date +%s)
seen_ids=/tmp/finance-shadow-retention-ids
: >"$seen_ids"
trap 'rm -f "$seen_ids"' EXIT
for dump in /backups/finance-shadow-*.dump; do
  [ -f "$dump" ] || continue
  base=$(basename "$dump" .dump)
  if ! (VERIFY_CURRENT_STATE=false BACKUP_BASENAME="$base" \
      /scripts/verify-backup.sh) \
      >/dev/null 2>&1; then
    continue
  fi
  manifest="/backups/$base.json"
  backup_id=$(sed -n 's/.*"backupId":"\([0-9a-f]\{64\}\)".*/\1/p' "$manifest")
  if grep -Fxq "$backup_id" "$seen_ids"; then
    continue
  fi
  printf '%s\n' "$backup_id" >>"$seen_ids"
  count=$((count + 1))
  created_at=$(sed -n 's/.*"createdAt":"\([^"]*\)".*/\1/p' "$manifest")
  created_epoch=$(date -u -d "$created_at" +%s)
  if [ "$created_epoch" -gt $((now_epoch + 300)) ]; then
    continue
  fi
  if [ "$created_epoch" -gt "$newest" ]; then
    newest=$created_epoch
  fi
done

if [ "$count" -lt "$BACKUP_MINIMUM_COUNT" ]; then
  echo "Backup retention verification failed: too few verified backup candidates" >&2
  exit 1
fi
if [ "$count" -gt 0 ]; then
  age_days=$(((now_epoch - newest) / 86400))
  if [ "$age_days" -gt "$BACKUP_RETENTION_DAYS" ]; then
    echo "Backup retention verification failed: newest backup is too old" >&2
    exit 1
  fi
fi
echo "Backup retention verified: count=$count"
