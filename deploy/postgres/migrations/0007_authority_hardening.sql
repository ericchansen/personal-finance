BEGIN;

CREATE TABLE IF NOT EXISTS finance.writer_gate (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    migrations_blocked boolean NOT NULL DEFAULT false,
    changed_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

INSERT INTO finance.writer_gate (singleton, migrations_blocked)
VALUES (true, false)
ON CONFLICT (singleton) DO NOTHING;

CREATE OR REPLACE FUNCTION finance.backup_state_manifest()
RETURNS jsonb
LANGUAGE plpgsql
STABLE
AS $function$
DECLARE
    table_record record;
    item_count bigint;
    item_digest text;
    tables jsonb := '[]'::jsonb;
    instance uuid;
BEGIN
    SELECT instance_id
    INTO STRICT instance
    FROM finance.shadow_authority_metadata
    WHERE singleton;

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

DROP TRIGGER IF EXISTS source_blobs_immutable_identity ON finance.source_blobs;
CREATE TRIGGER source_blobs_immutable_identity
BEFORE UPDATE OR DELETE ON finance.source_blobs
FOR EACH ROW EXECUTE FUNCTION finance.reject_immutable_columns(
    'raw_locator', 'content_hash', 'hash_algorithm', 'media_type', 'byte_size',
    'source_effective_start', 'source_effective_end', 'observed_at',
    'processed_at', 'source_kind', 'source_version'
);

DROP TRIGGER IF EXISTS ingestion_runs_append_only ON finance.ingestion_runs;
CREATE TRIGGER ingestion_runs_append_only
BEFORE UPDATE OR DELETE ON finance.ingestion_runs
FOR EACH ROW EXECUTE FUNCTION finance.reject_append_only_change();

REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA finance
FROM finance_shadow_ingest;
ALTER DEFAULT PRIVILEGES IN SCHEMA finance
    REVOKE INSERT, UPDATE ON TABLES FROM finance_shadow_ingest;
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
    finance.lineage_review_decisions
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
GRANT UPDATE (is_current, projection_status)
ON finance.projection_records TO finance_shadow_ingest;

REVOKE EXECUTE ON FUNCTION finance.backup_state_manifest() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION finance.backup_state_manifest()
TO finance_shadow_ingest, finance_shadow_backup;

INSERT INTO finance.schema_migrations(version, name, checksum)
VALUES (:'migration_version', :'migration_name', :'migration_checksum')
ON CONFLICT (version) DO NOTHING;

COMMIT;
