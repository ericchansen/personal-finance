# PostgreSQL shadow authority disaster recovery

This runbook recovers the isolated PostgreSQL evidence authority only. It does
not authorize Wealthfolio mutation, source-file mutation, duplicate remediation,
or cutover.

## Recovery point

1. Stop shadow ingest scheduling; leave source collectors unchanged.
2. Select a backup only from the external backup directory.
3. Verify its dump, SHA-256 sidecar, instance-bound per-table state hash,
   schema count, and observation count with `verify-backup`.
4. Retain the failed instance and its logs. Do not reuse its volume for restore.

## Rehearse first

Set `BACKUP_BASENAME` and run `compose.rehearsal.yml`. It restores into a
separate fixed-name project backed by `tmpfs`, verifies schema/read views, and
exits. Destroy only that rehearsal project afterward.

## Recover

1. Create a new uniquely identified PostgreSQL shadow instance and empty volume.
2. Restore the verified custom-format dump with `--exit-on-error`, `--no-owner`,
   and `--no-privileges`.
3. Provision the NOLOGIN role prerequisites before pending migrations:
   database dumps intentionally omit cluster roles. The restore runner uses
   `FINANCE_SHADOW_ROLES_ONLY=true` with the existing provisioning script;
   it does not need or rotate login passwords in that mode.
4. Run the immutable migration runner. A non-empty instance requires a fresh
   verified pre-migration backup. Migration and ingest share one global writer
   gate and advisory lock.
5. Rebuild roles and ACLs, then provision new least-privilege loader, agent,
   and backup credentials from external secret files. Roles are intentionally
   not assumed to exist in the dump.
6. Run `status`, `health`, a sealed `plan`, and `drift`.
7. Require the restored state hash, migration-set hash, input-set hash, source
   counts, and lineage-group counts to match the recovery record.
8. Switch only the shadow loader DSN. Wealthfolio remains untouched.

## Rollback policy

Never reverse an evidence migration in place. If a new migration or apply is
wrong, preserve the failed database for forensics, restore the prior verified
dump into a new instance, and correct the code with another forward migration.
Do not delete observations to make hashes match.

## Loss and escalation

If no verified dump restores, rebuild into a new instance from the sealed
external inputs. Stop if any source hash, baseline capability, forensic graph,
parser hash, or lineage decision binding fails. Missing lineage remains an open
quality issue and blocks cutover, but does not justify choosing a fuzzy survivor.
