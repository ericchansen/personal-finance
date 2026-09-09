#!/bin/sh
set -eu

. /scripts/secret-env.sh
load_pgpassword

read_login_secret() {
  path=$1
  label=$2
  if [ ! -f "$path" ] || [ -L "$path" ]; then
    echo "$label secret must be a regular, non-symlink file" >&2
    exit 1
  fi
  value=$(tr -d '\r\n' <"$path")
  if [ -z "$value" ]; then
    echo "$label secret is empty" >&2
    exit 1
  fi
  printf '%s' "$value"
}

roles_only=${FINANCE_SHADOW_ROLES_ONLY:-false}
case "$roles_only" in
  true)
    loader_password=
    agent_password=
    backup_password=
    ;;
  false)
    loader_password=$(read_login_secret "$LOADER_PASSWORD_FILE" loader)
    agent_password=$(read_login_secret "$AGENT_PASSWORD_FILE" agent)
    backup_password=$(read_login_secret "$BACKUP_PASSWORD_FILE" backup)
    ;;
  *)
    echo "FINANCE_SHADOW_ROLES_ONLY must be true or false" >&2
    exit 1
    ;;
esac

FINANCE_SHADOW_LOADER_PASSWORD="$loader_password" \
FINANCE_SHADOW_AGENT_PASSWORD="$agent_password" \
FINANCE_SHADOW_BACKUP_PASSWORD="$backup_password" \
psql --no-psqlrc --set=ON_ERROR_STOP=1 --set=roles_only="$roles_only" --quiet <<'SQL'
\getenv loader_password FINANCE_SHADOW_LOADER_PASSWORD
\getenv agent_password FINANCE_SHADOW_AGENT_PASSWORD
\getenv backup_password FINANCE_SHADOW_BACKUP_PASSWORD

DO $do$
DECLARE
    role_name text;
BEGIN
    FOREACH role_name IN ARRAY ARRAY[
        'finance_readonly',
        'finance_shadow_ingest',
        'finance_shadow_backup',
        'finance_shadow_agent_readonly'
    ] LOOP
        IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = role_name) THEN
            EXECUTE format(
                'CREATE ROLE %I NOLOGIN NOSUPERUSER NOCREATEDB '
                'NOCREATEROLE NOREPLICATION',
                role_name
            );
        END IF;
    END LOOP;
END;
$do$;

ALTER ROLE finance_readonly WITH
    NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
ALTER ROLE finance_shadow_ingest WITH
    NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
ALTER ROLE finance_shadow_backup WITH
    NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
ALTER ROLE finance_shadow_agent_readonly WITH
    NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;

ALTER ROLE finance_readonly SET default_transaction_read_only = on;
ALTER ROLE finance_shadow_agent_readonly SET default_transaction_read_only = on;
ALTER ROLE finance_shadow_ingest SET search_path = finance, pg_catalog;
GRANT finance_readonly TO finance_shadow_agent_readonly;
GRANT finance_readonly TO finance_shadow_ingest;
REVOKE pg_read_all_data FROM finance_shadow_backup;

\if :roles_only
\quit
\endif

GRANT USAGE ON SCHEMA finance TO finance_shadow_ingest;
GRANT USAGE ON SCHEMA finance_read TO finance_readonly;
GRANT SELECT ON ALL TABLES IN SCHEMA finance_read TO finance_readonly;
GRANT SELECT ON ALL TABLES IN SCHEMA finance TO finance_shadow_ingest;
REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA finance
FROM finance_shadow_ingest;
GRANT SELECT ON ALL TABLES IN SCHEMA finance TO finance_shadow_ingest;
GRANT INSERT ON
    finance.source_blobs,
    finance.ingestion_runs,
    finance.source_connections,
    finance.source_accounts,
    finance.canonical_accounts,
    finance.durable_decisions,
    finance.source_account_links,
    finance.quality_issues,
    finance.transaction_observations,
    finance.balance_observations,
    finance.position_observations,
    finance.valuation_observations,
    finance.artifact_observations,
    finance.canonical_transactions,
    finance.transaction_observation_links,
    finance.projection_runs,
    finance.projection_records,
    finance.audit_events,
    finance.app_state_snapshots,
    finance.shadow_plans,
    finance.shadow_run_events,
    finance.lineage_review_groups,
    finance.lineage_review_group_members,
    finance.lineage_review_decisions,
    finance.canonical_identity_policies,
    finance.canonical_identity_policy_generations,
    finance.canonical_identity_source_claims,
    finance.canonical_identity_observation_memberships,
    finance.canonical_identity_graph_edges,
    finance.canonical_identity_events,
    finance.canonical_identity_event_members,
    finance.canonical_identity_event_relationships,
    finance.canonical_identity_automatic_decisions,
    finance.canonical_identity_human_overrides,
    finance.canonical_identity_decision_event_memberships,
    finance.canonical_identity_source_authority_policies,
    finance.canonical_identity_authority_intervals,
    finance.canonical_identity_source_suppressions,
    finance.canonical_identity_provider_token_scopes,
    finance.accepted_identity_generations,
    finance.accepted_identity_events,
    finance.accepted_identity_claims,
    finance.accepted_identity_revisions,
    finance.accepted_identity_event_mappings,
    finance.accepted_identity_projection_links,
    finance.incremental_scopes,
    finance.incremental_source_versions,
    finance.incremental_source_sightings,
    finance.incremental_runs,
    finance.incremental_qualifications,
    finance.incremental_activity_bindings,
    finance.incremental_outbox,
    finance.incremental_attempts,
    finance.incremental_run_events,
    finance.incremental_projection_observations,
    finance.incremental_source_anchors,
    finance.incremental_anchor_checkpoints,
    finance.incremental_anchor_adoptions,
    finance.application_projection_bindings
TO finance_shadow_ingest;
GRANT UPDATE (
    source_account_name, account_type, currency_code, status,
    observed_at, processed_at, trust_cutoff_at, trust_cutoff_decision_id
) ON finance.source_accounts TO finance_shadow_ingest;
GRANT UPDATE (effective_to)
ON finance.source_account_links TO finance_shadow_ingest;
GRANT UPDATE (last_seen_at, last_seen_run_id, last_seen_order)
ON finance.transaction_observations TO finance_shadow_ingest;
GRANT UPDATE (
    effective_at, amount, currency_code, description, status
) ON finance.canonical_transactions TO finance_shadow_ingest;
GRANT UPDATE (effective_to, is_active)
ON finance.application_projection_bindings TO finance_shadow_ingest;
GRANT UPDATE (is_current, projection_status)
ON finance.projection_records TO finance_shadow_ingest;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA finance
TO finance_shadow_ingest;
GRANT EXECUTE ON FUNCTION finance.backup_state_manifest()
TO finance_shadow_ingest, finance_shadow_backup;
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

DO $do$
BEGIN
    EXECUTE format(
        'GRANT CONNECT ON DATABASE %I TO finance_readonly, '
        'finance_shadow_ingest, finance_shadow_backup',
        current_database()
    );
END;
$do$;

SELECT format(
    'CREATE ROLE finance_shadow_loader LOGIN NOSUPERUSER NOCREATEDB '
    'NOCREATEROLE NOREPLICATION PASSWORD %L',
    :'loader_password'
)
WHERE NOT EXISTS (
    SELECT 1 FROM pg_roles WHERE rolname = 'finance_shadow_loader'
)
\gexec

SELECT format(
    'ALTER ROLE finance_shadow_loader PASSWORD %L',
    :'loader_password'
)
\gexec

SELECT format(
    'CREATE ROLE finance_shadow_agent LOGIN NOSUPERUSER NOCREATEDB '
    'NOCREATEROLE NOREPLICATION PASSWORD %L',
    :'agent_password'
)
WHERE NOT EXISTS (
    SELECT 1 FROM pg_roles WHERE rolname = 'finance_shadow_agent'
)
\gexec

SELECT format(
    'ALTER ROLE finance_shadow_agent PASSWORD %L',
    :'agent_password'
)
\gexec

SELECT format(
    'CREATE ROLE finance_shadow_backup_login LOGIN NOSUPERUSER NOCREATEDB '
    'NOCREATEROLE NOREPLICATION PASSWORD %L',
    :'backup_password'
)
WHERE NOT EXISTS (
    SELECT 1 FROM pg_roles WHERE rolname = 'finance_shadow_backup_login'
)
\gexec

SELECT format(
    'ALTER ROLE finance_shadow_backup_login PASSWORD %L',
    :'backup_password'
)
\gexec

ALTER ROLE finance_shadow_loader WITH
    LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
ALTER ROLE finance_shadow_agent WITH
    LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
ALTER ROLE finance_shadow_backup_login WITH
    LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;

DO $do$
DECLARE
    membership record;
BEGIN
    FOR membership IN
        SELECT granted.rolname AS granted_role,
               member.rolname AS member_role
        FROM pg_auth_members relation
        JOIN pg_roles granted ON granted.oid = relation.roleid
        JOIN pg_roles member ON member.oid = relation.member
        WHERE member.rolname IN (
            'finance_readonly',
            'finance_shadow_ingest',
            'finance_shadow_backup',
            'finance_shadow_agent_readonly',
            'finance_shadow_loader',
            'finance_shadow_agent',
            'finance_shadow_backup_login'
        )
          AND NOT (
              (member.rolname = 'finance_shadow_ingest'
                  AND granted.rolname = 'finance_readonly')
              OR (member.rolname = 'finance_shadow_agent_readonly'
                  AND granted.rolname = 'finance_readonly')
              OR (member.rolname = 'finance_shadow_loader'
                  AND granted.rolname = 'finance_shadow_ingest')
              OR (member.rolname = 'finance_shadow_agent'
                  AND granted.rolname = 'finance_shadow_agent_readonly')
              OR (member.rolname = 'finance_shadow_backup_login'
                  AND granted.rolname = 'finance_shadow_backup')
          )
    LOOP
        EXECUTE format(
            'REVOKE %I FROM %I',
            membership.granted_role,
            membership.member_role
        );
    END LOOP;
END;
$do$;

GRANT finance_shadow_ingest TO finance_shadow_loader;
GRANT finance_shadow_agent_readonly TO finance_shadow_agent;
GRANT finance_shadow_backup TO finance_shadow_backup_login;
ALTER ROLE finance_shadow_agent SET default_transaction_read_only = on;
ALTER ROLE finance_shadow_agent SET search_path = finance_read, pg_catalog;
SQL

unset loader_password agent_password backup_password
if [ "$roles_only" = "true" ]; then
  echo "NOLOGIN role prerequisites prepared."
else
  echo "Least-privilege shadow logins provisioned."
fi
