\set ON_ERROR_STOP on

DO $test$
DECLARE
    required_table text;
BEGIN
    FOREACH required_table IN ARRAY ARRAY[
        'schema_migrations', 'source_blobs', 'ingestion_runs',
        'source_connections', 'source_accounts', 'transaction_observations',
        'balance_observations', 'position_observations', 'valuation_observations',
        'canonical_accounts', 'canonical_transactions',
        'transaction_observation_links', 'durable_decisions', 'quality_issues',
        'app_state_snapshots', 'projection_runs', 'projection_records', 'audit_events',
        'conformance_observations', 'conformance_artifacts',
        'conformance_batch_commits', 'artifact_observations',
        'shadow_authority_metadata', 'shadow_plans', 'shadow_run_events',
        'lineage_review_groups', 'lineage_review_group_members',
        'lineage_review_decisions', 'writer_gate',
        'canonical_identity_policies',
        'canonical_identity_policy_generations',
        'canonical_identity_source_claims',
        'canonical_identity_observation_memberships',
        'canonical_identity_graph_edges',
        'canonical_identity_events',
        'canonical_identity_event_members',
        'canonical_identity_event_relationships',
        'canonical_identity_automatic_decisions',
        'canonical_identity_human_overrides',
        'canonical_identity_decision_event_memberships',
        'canonical_identity_source_authority_policies',
        'canonical_identity_authority_intervals',
        'canonical_identity_source_suppressions',
        'canonical_identity_provider_token_scopes',
        'application_projection_bindings'
    ] LOOP
        IF to_regclass('finance.' || required_table) IS NULL THEN
            RAISE EXCEPTION 'missing table: %', required_table;
        END IF;
    END LOOP;

    -- Kept in lockstep with deploy/postgres/migrations by
    -- tests/test_postgres_identity_schema.py, which fails when a migration is
    -- added without updating this number.
    IF (SELECT count(*) FROM finance.schema_migrations) <> 21 THEN
        RAISE EXCEPTION 'expected 21 applied migrations, found %',
            (SELECT count(*) FROM finance.schema_migrations);
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'finance_readonly') THEN
        RAISE EXCEPTION 'missing finance_readonly role';
    END IF;
    IF NOT EXISTS (
        SELECT 1
        FROM finance.shadow_authority_metadata
        WHERE environment_marker = 'synthetic-shadow-integration'
          AND authority_mode = 'shadow'
          AND NOT wealthfolio_mutation_enabled
          AND NOT cutover_authorized
    ) THEN
        RAISE EXCEPTION 'shadow authority marker is missing or unsafe';
    END IF;
    IF NOT EXISTS (
        SELECT 1
        FROM pg_roles
        WHERE rolname = 'finance_shadow_ingest'
          AND NOT rolsuper
          AND NOT rolcreatedb
          AND NOT rolcreaterole
          AND NOT rolcanlogin
    ) THEN
        RAISE EXCEPTION 'least-privilege ingest role is missing';
    END IF;
    IF NOT EXISTS (
        SELECT 1
        FROM pg_roles
        WHERE rolname = 'finance_shadow_agent_readonly'
          AND NOT rolsuper
          AND NOT rolcanlogin
    ) THEN
        RAISE EXCEPTION 'read-only agent role is missing';
    END IF;
    IF NOT EXISTS (
        SELECT 1
        FROM pg_roles
        WHERE rolname = 'finance_shadow_backup'
          AND NOT rolsuper
          AND NOT rolcreatedb
          AND NOT rolcreaterole
          AND NOT rolcanlogin
    ) THEN
        RAISE EXCEPTION 'least-privilege backup role is missing';
    END IF;
    IF to_regprocedure('finance.backup_state_manifest()') IS NULL THEN
        RAISE EXCEPTION 'backup state manifest function is missing';
    END IF;
    IF NOT EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'finance'
          AND table_name = 'ingestion_runs'
          AND column_name = 'admission_hash'
    ) THEN
        RAISE EXCEPTION 'ingestion admission hash is missing';
    END IF;
    IF NOT EXISTS (
        SELECT 1
        FROM pg_indexes
        WHERE schemaname = 'finance'
          AND indexname = 'artifact_observations_occurrence_uq'
    ) THEN
        RAISE EXCEPTION 'artifact occurrence identity is not enforced';
    END IF;
    IF NOT EXISTS (
        SELECT 1
        FROM pg_indexes
        WHERE schemaname = 'finance'
          AND indexname = 'application_projection_bindings_wealthfolio_active_event_idx'
          AND regexp_replace(indexdef, '\s+', ' ', 'g')
                LIKE '%(target_application, canonical_id)%'
    ) THEN
        RAISE EXCEPTION 'stable canonical id to active wealthfolio binding uniqueness is missing';
    END IF;
    IF NOT EXISTS (
        SELECT 1
        FROM pg_indexes
        WHERE schemaname = 'finance'
          AND indexname = 'application_projection_bindings_wealthfolio_active_target_idx'
          AND regexp_replace(indexdef, '\s+', ' ', 'g')
                LIKE '%(target_application, target_activity_hash)%'
    ) THEN
        RAISE EXCEPTION 'active wealthfolio target binding uniqueness is missing';
    END IF;
    IF has_table_privilege(
        'finance_shadow_ingest',
        'finance.schema_migrations',
        'INSERT'
    ) OR has_table_privilege(
        'finance_shadow_ingest',
        'finance.schema_migrations',
        'UPDATE'
    ) THEN
        RAISE EXCEPTION 'ingest role can mutate migration history';
    END IF;
    IF has_table_privilege(
        'finance_shadow_ingest',
        'finance.source_blobs',
        'UPDATE'
    ) THEN
        RAISE EXCEPTION 'ingest role can rewrite source blob provenance';
    END IF;
    IF NOT has_table_privilege(
        'finance_shadow_ingest',
        'finance.canonical_identity_policies',
        'INSERT'
    ) OR NOT has_table_privilege(
        'finance_shadow_ingest',
        'finance.canonical_identity_automatic_decisions',
        'INSERT'
    ) OR NOT has_table_privilege(
        'finance_shadow_ingest',
        'finance.canonical_identity_decision_event_memberships',
        'INSERT'
    ) OR NOT has_table_privilege(
        'finance_shadow_ingest',
        'finance.canonical_identity_source_authority_policies',
        'INSERT'
    ) OR NOT has_table_privilege(
        'finance_shadow_ingest',
        'finance.canonical_identity_authority_intervals',
        'INSERT'
    ) OR NOT has_table_privilege(
        'finance_shadow_ingest',
        'finance.canonical_identity_source_suppressions',
        'INSERT'
    ) THEN
        RAISE EXCEPTION 'ingest role lacks canonical identity inserts';
    END IF;
    IF NOT has_column_privilege(
        'finance_shadow_ingest',
        'finance.application_projection_bindings',
        'effective_to',
        'UPDATE'
    ) OR NOT has_column_privilege(
        'finance_shadow_ingest',
        'finance.application_projection_bindings',
        'is_active',
        'UPDATE'
    ) OR has_column_privilege(
        'finance_shadow_ingest',
        'finance.application_projection_bindings',
        'binding_metadata',
        'UPDATE'
    ) THEN
        RAISE EXCEPTION 'ingest role projection binding updates are not least-privilege';
    END IF;
    IF NOT has_table_privilege(
        'finance_readonly',
        'finance_read.identity_decision_audit',
        'SELECT'
    ) OR NOT has_table_privilege(
        'finance_readonly',
        'finance_read.identity_generation_summary',
        'SELECT'
    ) OR NOT has_table_privilege(
        'finance_readonly',
        'finance_read.identity_source_authority_summary',
        'SELECT'
    ) OR NOT has_table_privilege(
        'finance_readonly',
        'finance_read.identity_provider_token_scope_summary',
        'SELECT'
    ) OR NOT has_table_privilege(
        'finance_readonly',
        'finance_read.identity_posting_window_summary',
        'SELECT'
    ) THEN
        RAISE EXCEPTION 'finance_readonly lacks canonical identity audit views';
    END IF;
    IF pg_has_role(
        'finance_shadow_backup',
        'pg_read_all_data',
        'member'
    ) THEN
        RAISE EXCEPTION 'backup role inherits global read privileges';
    END IF;
    IF NOT has_schema_privilege(
        'finance_shadow_backup',
        'finance',
        'USAGE'
    ) OR NOT has_schema_privilege(
        'finance_shadow_backup',
        'finance_read',
        'USAGE'
    ) OR NOT has_table_privilege(
        'finance_shadow_backup',
        'finance.source_blobs',
        'SELECT'
    ) OR NOT has_table_privilege(
        'finance_shadow_backup',
        'finance_read.shadow_status',
        'SELECT'
    ) THEN
        RAISE EXCEPTION 'backup role lacks scoped finance read privileges';
    END IF;
    IF has_table_privilege(
        'finance_shadow_backup',
        'finance.source_blobs',
        'INSERT'
    ) OR has_table_privilege(
        'finance_shadow_backup',
        'finance.source_blobs',
        'UPDATE'
    ) OR has_table_privilege(
        'finance_shadow_backup',
        'finance.source_blobs',
        'DELETE'
    ) OR has_table_privilege(
        'finance_shadow_backup',
        'pg_catalog.pg_authid',
        'SELECT'
    ) THEN
        RAISE EXCEPTION 'backup role has privileges outside scoped reads';
    END IF;
END;
$test$;

DO $test$
DECLARE
    required_view text;
BEGIN
    FOREACH required_view IN ARRAY ARRAY[
        'conformance_observations', 'conformance_artifacts',
        'conformance_source_canonical_projection_comparison',
        'conformance_provenance',
        'conformance_pending_history', 'conformance_unresolved_duplicates',
        'conformance_corrections_decisions', 'conformance_balance_changes',
        'conformance_stale_trust_issues', 'conformance_replay_equality',
        'shadow_status', 'shadow_source_inventory',
        'shadow_artifact_observations', 'shadow_lineage_status',
        'identity_decision_audit', 'identity_generation_summary',
        'identity_source_authority_summary',
        'identity_provider_token_scope_summary',
        'identity_posting_window_summary'
    ] LOOP
        IF to_regclass('finance_read.' || required_view) IS NULL THEN
            RAISE EXCEPTION 'missing conformance view: %', required_view;
        END IF;
    END LOOP;
END;
$test$;

DO $test$
DECLARE
    event_id uuid;
    inserted_decision_id uuid;
BEGIN
    INSERT INTO finance.audit_events (
        event_type, actor, subject_type, subject_key,
        effective_at, observed_at
    ) VALUES (
        'integration_check', 'synthetic-runner', 'synthetic', 'example',
        '2026-01-01T00:00:00Z', '2026-01-01T00:00:01Z'
    ) RETURNING audit_event_id INTO event_id;

    BEGIN
        UPDATE finance.audit_events SET actor = 'changed' WHERE audit_event_id = event_id;
        RAISE EXCEPTION 'audit update unexpectedly succeeded';
    EXCEPTION
        WHEN SQLSTATE '55000' THEN NULL;
    END;

    INSERT INTO finance.durable_decisions (
        decision_type, subject_type, subject_key, action, rationale, decided_by,
        effective_at, observed_at
    ) VALUES (
        'correction', 'synthetic', 'example', 'accept', 'integration verification',
        'synthetic-runner', '2026-01-01T00:00:00Z', '2026-01-01T00:00:01Z'
    ) RETURNING durable_decisions.decision_id INTO inserted_decision_id;

    BEGIN
        DELETE FROM finance.durable_decisions
        WHERE durable_decisions.decision_id = inserted_decision_id;
        RAISE EXCEPTION 'decision delete unexpectedly succeeded';
    EXCEPTION
        WHEN SQLSTATE '55000' THEN NULL;
    END;
END;
$test$;

DO $test$
DECLARE
    canonical_id uuid := gen_random_uuid();
BEGIN
    BEGIN
        INSERT INTO finance.canonical_accounts (
            canonical_account_id, canonical_key, display_name, status,
            effective_from, observed_at
        ) VALUES (
            canonical_id, 'synthetic-excluded', 'Synthetic Example', 'excluded',
            '2026-01-01T00:00:00Z', '2026-01-01T00:00:01Z'
        );
        RAISE EXCEPTION 'excluded account without decision unexpectedly succeeded';
    EXCEPTION
        WHEN SQLSTATE '23514' THEN NULL;
    END;
END;
$test$;

DO $test$
DECLARE
    blob_id uuid;
    run_id uuid;
    connection_id uuid;
    account_id uuid;
    observation_id uuid;
    stale_transaction_id uuid := gen_random_uuid();
    balance_id uuid := gen_random_uuid();
    stale_issue_id uuid;
    trust_decision_id uuid;
    inserted_canonical_account_id uuid;
    inserted_canonical_transaction_id uuid;
BEGIN
    INSERT INTO finance.source_blobs (
        raw_locator, content_hash, observed_at
    ) VALUES (
        'external://synthetic/example',
        repeat('a', 64),
        '2026-01-01T00:00:01Z'
    ) RETURNING source_blob_id INTO blob_id;

    INSERT INTO finance.ingestion_runs (
        source_blob_id, importer_name, importer_version, run_status,
        observed_at
    ) VALUES (
        blob_id, 'synthetic-importer', '1', 'succeeded',
        '2026-01-01T00:00:01Z'
    ) RETURNING ingestion_run_id INTO run_id;

    INSERT INTO finance.source_connections (
        source_system, connection_key, effective_from, observed_at
    ) VALUES (
        'synthetic-source', 'synthetic-connection',
        '2026-01-01T00:00:00Z', '2026-01-01T00:00:01Z'
    ) RETURNING source_connection_id INTO connection_id;

    INSERT INTO finance.source_accounts (
        source_connection_id, external_account_id, effective_from, observed_at
    ) VALUES (
        connection_id, 'synthetic-account',
        '2026-01-01T00:00:00Z', '2026-01-01T00:00:01Z'
    ) RETURNING source_account_id INTO account_id;

    INSERT INTO finance.durable_decisions (
        decision_type, subject_type, subject_key, action, rationale, decided_by,
        effective_at, observed_at
    ) VALUES (
        'trust_cutoff', 'source_account', account_id::text, 'set_cutoff',
        'synthetic cutoff verification', 'synthetic-runner',
        '2026-01-01T00:30:00Z', '2026-01-01T00:30:01Z'
    ) RETURNING decision_id INTO trust_decision_id;

    UPDATE finance.source_accounts
    SET trust_cutoff_at = '2026-01-01T00:30:00Z',
        trust_cutoff_decision_id = trust_decision_id
    WHERE source_account_id = account_id;

    BEGIN
        INSERT INTO finance.balance_observations (
            balance_observation_id, ingestion_run_id, source_blob_id,
            source_account_id, observation_hash, balance_type, effective_at,
            observed_at, amount, currency_code
        ) VALUES (
            balance_id, run_id, blob_id, account_id, repeat('d', 64), 'current',
            '2026-01-01T01:00:00Z', '2026-01-01T01:00:01Z', 123.45, 'USD'
        );
        RAISE EXCEPTION 'post-cutoff balance without issue unexpectedly succeeded';
    EXCEPTION
        WHEN SQLSTATE '23514' THEN NULL;
    END;

    INSERT INTO finance.quality_issues (
        issue_type, severity, subject_type, subject_key,
        effective_at, observed_at
    ) VALUES (
        'stale_after_trust_cutoff', 'warning', 'balance_observation',
        balance_id::text, '2026-01-01T01:00:00Z', '2026-01-01T01:00:01Z'
    ) RETURNING quality_issue_id INTO stale_issue_id;

    INSERT INTO finance.balance_observations (
        balance_observation_id, ingestion_run_id, source_blob_id,
        source_account_id, observation_hash, balance_type, effective_at,
        observed_at, amount, currency_code, quality_issue_id
    ) VALUES (
        balance_id, run_id, blob_id, account_id, repeat('d', 64), 'current',
        '2026-01-01T01:00:00Z', '2026-01-01T01:00:01Z', 123.45, 'USD',
        stale_issue_id
    );

    BEGIN
        INSERT INTO finance.transaction_observations (
            transaction_observation_id, ingestion_run_id, source_blob_id,
            source_account_id, source_transaction_id, observation_hash,
            effective_at, observed_at, amount, currency_code, source_status,
            last_seen_run_id
        ) VALUES (
            stale_transaction_id, run_id, blob_id, account_id,
            'synthetic-stale-transaction', repeat('e', 64),
            '2026-01-01T01:00:00Z', '2026-01-01T01:00:01Z',
            1.00, 'USD', 'posted', run_id
        );
        RAISE EXCEPTION 'post-cutoff transaction without issue unexpectedly succeeded';
    EXCEPTION
        WHEN SQLSTATE '23514' THEN NULL;
    END;

    INSERT INTO finance.quality_issues (
        issue_type, severity, subject_type, subject_key,
        effective_at, observed_at
    ) VALUES (
        'stale_after_trust_cutoff', 'warning', 'transaction_observation',
        stale_transaction_id::text, '2026-01-01T01:00:00Z', '2026-01-01T01:00:01Z'
    ) RETURNING quality_issue_id INTO stale_issue_id;

    INSERT INTO finance.transaction_observations (
        transaction_observation_id, ingestion_run_id, source_blob_id,
        source_account_id, source_transaction_id, observation_hash,
        effective_at, observed_at, amount, currency_code, source_status,
        quality_issue_id, last_seen_run_id
    ) VALUES (
        stale_transaction_id, run_id, blob_id, account_id,
        'synthetic-stale-transaction', repeat('e', 64),
        '2026-01-01T01:00:00Z', '2026-01-01T01:00:01Z',
        1.00, 'USD', 'posted', stale_issue_id, run_id
    );

    INSERT INTO finance.transaction_observations (
        ingestion_run_id, source_blob_id, source_account_id,
        source_transaction_id, observation_hash, effective_at, observed_at,
        amount, currency_code, source_status, last_seen_run_id
    ) VALUES (
        run_id, blob_id, account_id, 'synthetic-transaction', repeat('b', 64),
        '2026-01-01T00:00:00Z', '2026-01-01T00:00:01Z',
        12.34, 'USD', 'posted', run_id
    ) RETURNING transaction_observation_id INTO observation_id;

    BEGIN
        UPDATE finance.transaction_observations
        SET observation_hash = repeat('c', 64)
        WHERE transaction_observation_id = observation_id;
        RAISE EXCEPTION 'observation hash update unexpectedly succeeded';
    EXCEPTION
        WHEN SQLSTATE '55000' THEN NULL;
    END;

    BEGIN
        UPDATE finance.source_blobs
        SET raw_locator = 'external://synthetic/changed'
        WHERE source_blob_id = blob_id;
        RAISE EXCEPTION 'raw locator update unexpectedly succeeded';
    EXCEPTION
        WHEN SQLSTATE '55000' THEN NULL;
    END;

    INSERT INTO finance.transaction_observations (
        ingestion_run_id, source_blob_id, source_account_id,
        source_transaction_id, observation_hash, effective_at, observed_at,
        amount, currency_code, source_status, last_seen_run_id
    ) VALUES (
        run_id, blob_id, account_id, 'synthetic-transaction', repeat('c', 64),
        '2026-01-01T00:00:00Z', '2026-01-01T00:00:02Z',
        12.35, 'USD', 'posted', run_id
    );

    IF (
        SELECT count(*)
        FROM finance.transaction_observations
        WHERE source_account_id = account_id
          AND source_transaction_id = 'synthetic-transaction'
    ) <> 2 THEN
        RAISE EXCEPTION 'source transaction history was not retained';
    END IF;

    INSERT INTO finance.canonical_accounts (
        canonical_key, display_name, effective_from, observed_at
    ) VALUES (
        'synthetic-active', 'Synthetic Active Account',
        '2026-01-01T00:00:00Z', '2026-01-01T00:00:01Z'
    ) RETURNING finance.canonical_accounts.canonical_account_id
      INTO inserted_canonical_account_id;

    INSERT INTO finance.canonical_transactions (
        canonical_account_id, canonical_key, effective_at, observed_at,
        amount, currency_code
    ) VALUES (
        inserted_canonical_account_id, 'synthetic-canonical-transaction',
        '2026-01-01T00:00:00Z', '2026-01-01T00:00:01Z',
        12.34, 'USD'
    ) RETURNING finance.canonical_transactions.canonical_transaction_id
      INTO inserted_canonical_transaction_id;

    BEGIN
        INSERT INTO finance.transaction_observation_links (
            transaction_observation_id, canonical_transaction_id, link_method,
            effective_at, observed_at
        ) VALUES (
            observation_id, inserted_canonical_transaction_id, 'fuzzy',
            '2026-01-01T00:00:00Z', '2026-01-01T00:00:01Z'
        );
        RAISE EXCEPTION 'fuzzy link without quality issue unexpectedly succeeded';
    EXCEPTION
        WHEN SQLSTATE '23514' THEN NULL;
    END;
END;
$test$;

DO $test$
DECLARE
    identity_policy_id uuid;
    policy_generation_id uuid;
    second_policy_generation_id uuid;
    blob_id uuid;
    run_id uuid;
    connection_id uuid;
    account_id uuid;
    observation_id uuid;
    claim_id uuid;
    membership_id uuid;
    event_id uuid;
    other_event_id uuid;
    excluded_event_id uuid;
    standalone_event_id uuid;
    replay_claim_id uuid;
    replay_event_id uuid;
    automatic_decision_id uuid;
    merge_decision_id uuid;
    transfer_decision_id uuid;
    unresolved_decision_id uuid;
    human_override_id uuid;
    generation_only_override_id uuid;
    replay_human_override_id uuid;
    projection_binding_id uuid;
    claim_hash_value text := repeat('6', 64);
    alternate_claim_hash text := repeat('7', 64);
    canonical_account_hash_value text := repeat('8', 64);
    source_account_hash_value text := repeat('9', 64);
    source_connection_hash_value text := repeat('a', 64);
    provider_identity_hash_value text := repeat('b', 64);
    selected_observation_hash_value text := repeat('c', 64);
    source_hash_one text := repeat('d', 64);
    source_hash_two text := repeat('e', 64);
BEGIN
    BEGIN
        INSERT INTO finance.canonical_identity_policies (
            policy_name, policy_version, policy_hash, policy_document, observed_at
        ) VALUES (
            'synthetic-canonical-identity', '', repeat('0', 64),
            jsonb_build_object('engine', 'synthetic'), '2026-02-01T00:00:00Z'
        );
        RAISE EXCEPTION 'empty semantic policy version unexpectedly succeeded';
    EXCEPTION
        WHEN SQLSTATE '23514' THEN NULL;
    END;

    INSERT INTO finance.canonical_identity_policies (
        policy_name, policy_version, policy_hash, policy_document, observed_at
    ) VALUES (
        'synthetic-canonical-identity', 'canonical-identity-v1', repeat('1', 64),
        jsonb_build_object('engine', 'synthetic'), '2026-02-01T00:00:01Z'
    ) RETURNING canonical_identity_policy_id INTO identity_policy_id;

    BEGIN
        INSERT INTO finance.canonical_identity_policy_generations (
            canonical_identity_policy_id, policy_version, policy_hash,
            generation_number, generation_label, generation_hash, input_hash,
            canonical_state_hash, feature_schema_hash, observed_at
        ) VALUES (
            identity_policy_id, 'canonical-identity-v2', repeat('1', 64), 1,
            'invalid-generation', repeat('2', 64), repeat('3', 64),
            repeat('4', 64), repeat('5', 64), '2026-02-01T00:00:02Z'
        );
        RAISE EXCEPTION 'policy generation with mismatched version unexpectedly succeeded';
    EXCEPTION
        WHEN foreign_key_violation THEN NULL;
    END;

    INSERT INTO finance.canonical_identity_policy_generations (
        canonical_identity_policy_id, policy_version, policy_hash,
        generation_number, generation_label, generation_hash, input_hash,
        canonical_state_hash, feature_schema_hash, observed_at
    ) VALUES (
        identity_policy_id, 'canonical-identity-v1', repeat('1', 64), 1,
        'synthetic-generation', repeat('2', 64), repeat('3', 64),
        repeat('4', 64), repeat('5', 64), '2026-02-01T00:00:02Z'
    ) RETURNING canonical_identity_policy_generation_id INTO policy_generation_id;

    INSERT INTO finance.canonical_identity_policy_generations (
        canonical_identity_policy_id, policy_version, policy_hash,
        generation_number, generation_label, generation_hash, input_hash,
        canonical_state_hash, feature_schema_hash, observed_at
    ) VALUES (
        identity_policy_id, 'canonical-identity-v1', repeat('1', 64), 2,
        'synthetic-generation-replay', repeat('6', 64), repeat('7', 64),
        repeat('8', 64), repeat('9', 64), '2026-02-02T00:00:02Z'
    ) RETURNING canonical_identity_policy_generation_id INTO second_policy_generation_id;

    INSERT INTO finance.canonical_identity_policy_generations (
        canonical_identity_policy_id, policy_version, policy_hash,
        generation_number, generation_label, generation_hash, input_hash,
        canonical_state_hash, feature_schema_hash, observed_at
    ) VALUES (
        identity_policy_id, 'canonical-identity-v1', repeat('1', 64), 3,
        'same-state-different-overrides', repeat('a', 64), repeat('3', 64),
        repeat('4', 64), repeat('5', 64), '2026-02-03T00:00:02Z'
    );

    BEGIN
        INSERT INTO finance.canonical_identity_policy_generations (
            canonical_identity_policy_id, policy_version, policy_hash,
            generation_number, generation_label, generation_hash, input_hash,
            canonical_state_hash, feature_schema_hash, observed_at
        ) VALUES (
            identity_policy_id, 'canonical-identity-v1', repeat('1', 64), 4,
            'duplicate-generation-hash', repeat('2', 64), repeat('3', 64),
            repeat('4', 64), repeat('5', 64), '2026-02-04T00:00:02Z'
        );
        RAISE EXCEPTION 'duplicate generation hash unexpectedly succeeded';
    EXCEPTION
        WHEN unique_violation THEN NULL;
    END;

    IF (
        SELECT count(*)
        FROM finance.canonical_identity_policy_generations
        WHERE canonical_identity_policy_id = identity_policy_id
          AND input_hash = repeat('3', 64)
          AND canonical_state_hash = repeat('4', 64)
    ) <> 2 THEN
        RAISE EXCEPTION 'same canonical state generations were not both preserved';
    END IF;

    INSERT INTO finance.source_blobs (
        raw_locator, content_hash, observed_at
    ) VALUES (
        'external://synthetic/identity', repeat('f', 64), '2026-02-01T00:00:03Z'
    ) RETURNING source_blob_id INTO blob_id;

    INSERT INTO finance.ingestion_runs (
        source_blob_id, importer_name, importer_version, run_status, observed_at
    ) VALUES (
        blob_id, 'identity-importer', '1', 'succeeded', '2026-02-01T00:00:03Z'
    ) RETURNING ingestion_run_id INTO run_id;

    INSERT INTO finance.source_connections (
        source_system, connection_key, effective_from, observed_at
    ) VALUES (
        'synthetic-source', 'identity-connection',
        '2026-02-01T00:00:00Z', '2026-02-01T00:00:03Z'
    ) RETURNING source_connection_id INTO connection_id;

    INSERT INTO finance.source_accounts (
        source_connection_id, external_account_id, effective_from, observed_at
    ) VALUES (
        connection_id, 'identity-account', '2026-02-01T00:00:00Z',
        '2026-02-01T00:00:03Z'
    ) RETURNING source_account_id INTO account_id;

    INSERT INTO finance.transaction_observations (
        ingestion_run_id, source_blob_id, source_account_id,
        source_transaction_id, observation_hash, effective_at, observed_at,
        amount, currency_code, source_status, last_seen_run_id
    ) VALUES (
        run_id, blob_id, account_id, 'identity-transaction', repeat('0', 64),
        '2026-02-01T00:00:00Z', '2026-02-01T00:00:03Z',
        42.00, 'USD', 'posted', run_id
    ) RETURNING transaction_observation_id INTO observation_id;

    INSERT INTO finance.canonical_identity_source_claims (
        canonical_identity_policy_generation_id,
        claim_hash, source_family, canonical_account_hash, source_account_hash,
        source_connection_hash, provider_identity_hash, provider_id_kind,
        selected_observation_hash, source_hashes, observed_at
    ) VALUES (
        policy_generation_id, claim_hash_value, 'simplefin', canonical_account_hash_value,
        source_account_hash_value, source_connection_hash_value,
        provider_identity_hash_value, 'simplefin-id',
        selected_observation_hash_value,
        jsonb_build_array(source_hash_one, source_hash_two),
        '2026-02-01T00:00:04Z'
    ) RETURNING canonical_identity_source_claim_id INTO claim_id;

    BEGIN
        INSERT INTO finance.canonical_identity_source_claims (
            canonical_identity_policy_generation_id,
            claim_hash, source_family, canonical_account_hash, source_account_hash,
            source_connection_hash, provider_identity_hash, provider_id_kind,
            selected_observation_hash, source_hashes, observed_at
        ) VALUES (
            policy_generation_id, claim_hash_value, 'simplefin', canonical_account_hash_value,
            source_account_hash_value, source_connection_hash_value,
            provider_identity_hash_value, 'simplefin-id',
            selected_observation_hash_value,
            jsonb_build_array(source_hash_one, source_hash_two),
            '2026-02-01T00:00:04Z'
        );
        RAISE EXCEPTION 'same-generation claim snapshot unexpectedly duplicated';
    EXCEPTION
        WHEN unique_violation THEN NULL;
    END;

    INSERT INTO finance.canonical_identity_observation_memberships (
        canonical_identity_policy_generation_id,
        canonical_identity_source_claim_id, transaction_observation_id,
        membership_hash, membership_role, observed_at
    ) VALUES (
        policy_generation_id, claim_id, observation_id, repeat('1', 64), 'selected',
        '2026-02-01T00:00:04Z'
    ) RETURNING canonical_identity_observation_membership_id INTO membership_id;

    INSERT INTO finance.canonical_identity_events (
        canonical_identity_policy_generation_id, canonical_id, canonical_account_hash, selected_observation_hash,
        event_hash, source_day, signed_amount, currency_code, description_hash,
        status, category_hash, trusted, observed_at
    ) VALUES (
        policy_generation_id, 'canonical-event-1', canonical_account_hash_value,
        selected_observation_hash_value, repeat('2', 64), DATE '2026-02-01',
        42.00, 'USD', repeat('3', 64), 'posted', repeat('4', 64), true,
        '2026-02-01T00:00:05Z'
    ) RETURNING canonical_identity_event_id INTO event_id;

    BEGIN
        INSERT INTO finance.canonical_identity_events (
            canonical_identity_policy_generation_id, canonical_id, canonical_account_hash,
            selected_observation_hash, event_hash, source_day, signed_amount,
            currency_code, description_hash, status, category_hash, trusted,
            observed_at
        ) VALUES (
            policy_generation_id, 'canonical-event-1', canonical_account_hash_value,
            selected_observation_hash_value, repeat('2', 64), DATE '2026-02-01',
            42.00, 'USD', repeat('3', 64), 'posted', repeat('4', 64), true,
            '2026-02-01T00:00:05Z'
        );
        RAISE EXCEPTION 'same-generation canonical event snapshot unexpectedly duplicated';
    EXCEPTION
        WHEN unique_violation THEN NULL;
    END;

    INSERT INTO finance.canonical_identity_event_members (
        canonical_identity_policy_generation_id,
        canonical_identity_event_id, member_type, member_hash, member_role,
        canonical_identity_source_claim_id, observed_at
    ) VALUES (
        policy_generation_id, event_id, 'source_claim', repeat('5', 64), 'selected', claim_id,
        '2026-02-01T00:00:05Z'
    );

    INSERT INTO finance.canonical_identity_event_members (
        canonical_identity_policy_generation_id,
        canonical_identity_event_id, member_type, member_hash, member_role,
        transaction_observation_id, observed_at
    ) VALUES (
        policy_generation_id, event_id, 'transaction_observation', repeat('6', 64), 'member',
        observation_id, '2026-02-01T00:00:05Z'
    );

    -- 0017: an artifact observation has a published identity hash, not a row in
    -- finance.transaction_observations. Both member shapes must be storable.
    INSERT INTO finance.canonical_identity_observation_memberships (
        canonical_identity_policy_generation_id,
        canonical_identity_source_claim_id, observation_identity_hash,
        membership_hash, membership_role, observed_at
    ) VALUES (
        policy_generation_id, claim_id, repeat('a', 64), repeat('b', 64), 'member',
        '2026-02-01T00:00:04Z'
    );

    INSERT INTO finance.canonical_identity_event_members (
        canonical_identity_policy_generation_id,
        canonical_identity_event_id, member_type, member_hash, member_role,
        observation_identity_hash, observed_at
    ) VALUES (
        policy_generation_id, event_id, 'transaction_observation', repeat('c', 64),
        'member', repeat('a', 64), '2026-02-01T00:00:05Z'
    );

    BEGIN
        INSERT INTO finance.canonical_identity_observation_memberships (
            canonical_identity_policy_generation_id,
            canonical_identity_source_claim_id, transaction_observation_id,
            observation_identity_hash, membership_hash, membership_role,
            observed_at
        ) VALUES (
            policy_generation_id, claim_id, observation_id, repeat('a', 64),
            repeat('d', 64), 'member', '2026-02-01T00:00:04Z'
        );
        RAISE EXCEPTION 'membership carrying both identities unexpectedly admitted';
    EXCEPTION
        WHEN check_violation THEN NULL;
    END;

    BEGIN
        INSERT INTO finance.canonical_identity_observation_memberships (
            canonical_identity_policy_generation_id,
            canonical_identity_source_claim_id, membership_hash,
            membership_role, observed_at
        ) VALUES (
            policy_generation_id, claim_id, repeat('d', 64), 'member',
            '2026-02-01T00:00:04Z'
        );
        RAISE EXCEPTION 'membership carrying no identity unexpectedly admitted';
    EXCEPTION
        WHEN check_violation THEN NULL;
    END;

    BEGIN
        INSERT INTO finance.canonical_identity_observation_memberships (
            canonical_identity_policy_generation_id,
            canonical_identity_source_claim_id, observation_identity_hash,
            membership_hash, membership_role, observed_at
        ) VALUES (
            policy_generation_id, claim_id, 'not-a-sha256', repeat('d', 64),
            'member', '2026-02-01T00:00:04Z'
        );
        RAISE EXCEPTION 'malformed observation identity hash unexpectedly admitted';
    EXCEPTION
        WHEN check_violation THEN NULL;
    END;

    BEGIN
        INSERT INTO finance.canonical_identity_observation_memberships (
            canonical_identity_policy_generation_id,
            canonical_identity_source_claim_id, observation_identity_hash,
            membership_hash, membership_role, observed_at
        ) VALUES (
            policy_generation_id, claim_id, repeat('a', 64), repeat('d', 64),
            'member', '2026-02-01T00:00:04Z'
        );
        RAISE EXCEPTION 'duplicate hash-identified membership unexpectedly admitted';
    EXCEPTION
        WHEN unique_violation THEN NULL;
    END;

    BEGIN
        INSERT INTO finance.canonical_identity_event_members (
            canonical_identity_policy_generation_id,
            canonical_identity_event_id, member_type, member_hash, member_role,
            canonical_identity_source_claim_id, observation_identity_hash,
            observed_at
        ) VALUES (
            policy_generation_id, event_id, 'source_claim', repeat('e', 64),
            'member', claim_id, repeat('a', 64), '2026-02-01T00:00:05Z'
        );
        RAISE EXCEPTION 'source claim member carrying an observation identity unexpectedly admitted';
    EXCEPTION
        WHEN check_violation THEN NULL;
    END;

    BEGIN
        INSERT INTO finance.canonical_identity_event_members (
            canonical_identity_policy_generation_id,
            canonical_identity_event_id, member_type, member_hash, member_role,
            observed_at
        ) VALUES (
            policy_generation_id, event_id, 'transaction_observation',
            repeat('e', 64), 'member', '2026-02-01T00:00:05Z'
        );
        RAISE EXCEPTION 'observation member carrying no identity unexpectedly admitted';
    EXCEPTION
        WHEN check_violation THEN NULL;
    END;

    PERFORM 1 FROM finance_read.identity_observation_binding_summary
    WHERE canonical_identity_policy_generation_id = policy_generation_id
      AND identity_kind = 'published-identity-hash';
    IF NOT FOUND THEN
        RAISE EXCEPTION 'observation identity binding summary missed hash-bound members';
    END IF;

    INSERT INTO finance.canonical_identity_events (
        canonical_identity_policy_generation_id, canonical_id, canonical_account_hash, selected_observation_hash,
        event_hash, source_day, signed_amount, currency_code, description_hash,
        status, category_hash, trusted, observed_at
    ) VALUES (
        policy_generation_id, 'canonical-event-2', canonical_account_hash_value, repeat('7', 64),
        repeat('8', 64), DATE '2026-02-02', -42.00, 'USD', repeat('9', 64),
        'posted', repeat('a', 64), true, '2026-02-01T00:00:05Z'
    ) RETURNING canonical_identity_event_id INTO other_event_id;

    INSERT INTO finance.canonical_identity_events (
        canonical_identity_policy_generation_id, canonical_id, canonical_account_hash, selected_observation_hash,
        event_hash, source_day, signed_amount, currency_code, description_hash,
        status, category_hash, trusted, observed_at
    ) VALUES (
        policy_generation_id, 'canonical-event-excluded', canonical_account_hash_value, repeat('b', 64),
        repeat('c', 64), DATE '2026-02-03', 0.00, 'USD', repeat('d', 64),
        'excluded', repeat('e', 64), false, '2026-02-01T00:00:05Z'
    ) RETURNING canonical_identity_event_id INTO excluded_event_id;

    INSERT INTO finance.canonical_identity_source_claims (
        canonical_identity_policy_generation_id,
        claim_hash, source_family, canonical_account_hash, source_account_hash,
        source_connection_hash, provider_identity_hash, provider_id_kind,
        selected_observation_hash, source_hashes, observed_at
    ) VALUES (
        second_policy_generation_id, claim_hash_value, 'simplefin',
        canonical_account_hash_value, source_account_hash_value,
        source_connection_hash_value, provider_identity_hash_value,
        'simplefin-id', repeat('e', 64),
        jsonb_build_array(source_hash_one, source_hash_two),
        '2026-02-02T00:00:04Z'
    ) RETURNING canonical_identity_source_claim_id INTO replay_claim_id;

    INSERT INTO finance.canonical_identity_events (
        canonical_identity_policy_generation_id, canonical_id, canonical_account_hash,
        selected_observation_hash, event_hash, source_day, signed_amount,
        currency_code, description_hash, status, category_hash, trusted,
        observed_at
    ) VALUES (
        second_policy_generation_id, 'canonical-event-1', canonical_account_hash_value,
        repeat('e', 64), repeat('f', 64), DATE '2026-02-02', 45.00,
        'USD', repeat('0', 64), 'posted', repeat('1', 64), true,
        '2026-02-02T00:00:05Z'
    ) RETURNING canonical_identity_event_id INTO replay_event_id;

    INSERT INTO finance.canonical_identity_events (
        canonical_identity_policy_generation_id, canonical_id, canonical_account_hash,
        selected_observation_hash, event_hash, source_day, signed_amount,
        currency_code, description_hash, status, category_hash, trusted,
        observed_at
    ) VALUES (
        second_policy_generation_id, 'canonical-event-standalone',
        canonical_account_hash_value, repeat('1', 64), repeat('2', 64),
        DATE '2026-02-02', 11.00, 'USD', repeat('3', 64), 'posted',
        repeat('4', 64), true, '2026-02-02T00:00:05Z'
    ) RETURNING canonical_identity_event_id INTO standalone_event_id;

    INSERT INTO finance.canonical_identity_event_relationships (
        canonical_identity_policy_generation_id,
        source_canonical_identity_event_id, target_canonical_identity_event_id,
        relationship_type, relationship_hash, observed_at
    ) VALUES (
        policy_generation_id, event_id, other_event_id, 'transfer', repeat('0', 64),
        '2026-02-01T00:00:05Z'
    );

    INSERT INTO finance.canonical_identity_graph_edges (
        canonical_identity_policy_generation_id,
        left_node_type, left_node_id, right_node_type, right_node_id,
        relation_kind, feature_vector, confidence_basis_points, automatic,
        competing_candidate_proof, source_hashes, edge_hash, observed_at
    ) VALUES (
        policy_generation_id, 'source_claim', claim_id, 'observation_membership', membership_id,
        'transfer',
        jsonb_build_object(
            'differentAccounts', true,
            'oppositeSignedAmount', true,
            'sourceFamilyPair', 'manual-simplefin'
        ),
        10000,
        true,
        '{}'::jsonb,
        jsonb_build_array(source_hash_one, source_hash_two),
        repeat('f', 64), '2026-02-01T00:00:05Z'
    );

    BEGIN
        INSERT INTO finance.canonical_identity_graph_edges (
            canonical_identity_policy_generation_id,
            left_node_type, left_node_id, right_node_type, right_node_id,
            relation_kind, feature_vector, confidence_basis_points, automatic,
            competing_candidate_proof, source_hashes, edge_hash, observed_at
        ) VALUES (
            policy_generation_id, 'source_claim', claim_id, 'canonical_event', event_id,
            'duplicate-candidate',
            jsonb_build_object('sourceFamilyPair', 'monarch-simplefin'),
            10001,
            false,
            jsonb_build_object(
                'selectedClaimHash', claim_hash_value,
                'candidateClaimHashes', jsonb_build_array(claim_hash_value)
            ),
            jsonb_build_array(source_hash_one),
            repeat('1', 64), '2026-02-01T00:00:05Z'
        );
        RAISE EXCEPTION 'invalid graph edge evidence unexpectedly succeeded';
    EXCEPTION
        WHEN SQLSTATE '23514' THEN NULL;
    END;

    INSERT INTO finance.canonical_identity_automatic_decisions (
        canonical_identity_policy_generation_id, canonical_identity_policy_id,
        policy_version, policy_hash, outcome, confidence_tier,
        confidence_basis_points, rationale_code, feature_vector,
        competing_candidate_proof, source_hashes, decision_hash, observed_at
    ) VALUES (
        policy_generation_id, identity_policy_id, 'canonical-identity-v1',
        repeat('1', 64), 'merge-observations', 'exact-scoped-identity', 10000,
        'exact-scoped-source-identity',
        jsonb_build_object('sourceFamily', 'simplefin', 'providerIdKind', 'simplefin-id'),
        jsonb_build_object(
            'competingCandidateCount', 0,
            'sourceClaimCount', 1
        ),
        jsonb_build_array(source_hash_one, source_hash_two),
        repeat('c', 64), '2026-02-01T00:00:06Z'
    ) RETURNING canonical_identity_automatic_decision_id
      INTO automatic_decision_id;

    INSERT INTO finance.canonical_identity_decision_event_memberships (
        canonical_identity_policy_generation_id,
        canonical_identity_automatic_decision_id, canonical_identity_event_id,
        canonical_id, membership_role, membership_hash, observed_at
    ) VALUES (
        policy_generation_id, automatic_decision_id, event_id, 'canonical-event-1', 'subject',
        repeat('d', 64), '2026-02-01T00:00:06Z'
    );

    INSERT INTO finance.canonical_identity_automatic_decisions (
        canonical_identity_policy_generation_id, canonical_identity_policy_id,
        policy_version, policy_hash, outcome, confidence_tier,
        confidence_basis_points, rationale_code, feature_vector,
        competing_candidate_proof, source_hashes, decision_hash, observed_at
    ) VALUES (
        policy_generation_id, identity_policy_id, 'canonical-identity-v1',
        repeat('1', 64), 'merge-claims', 'unique-cross-source', 10000,
        'unique-cross-source-economic-tuple',
        jsonb_build_object('sourceFamilyPair', 'monarch-simplefin'),
        jsonb_build_object(
            'leftDegree', 1,
            'rightDegree', 1,
            'componentSize', 2,
            'competingCandidateCount', 0
        ),
        jsonb_build_array(source_hash_one, source_hash_two),
        repeat('e', 64), '2026-02-01T00:00:06Z'
    ) RETURNING canonical_identity_automatic_decision_id
      INTO merge_decision_id;

    INSERT INTO finance.canonical_identity_decision_event_memberships (
        canonical_identity_policy_generation_id,
        canonical_identity_automatic_decision_id, canonical_identity_event_id,
        canonical_id, membership_role, membership_hash, observed_at
    ) VALUES (
        policy_generation_id, merge_decision_id, event_id, 'canonical-event-1', 'related',
        repeat('f', 64), '2026-02-01T00:00:06Z'
    );

    BEGIN
        INSERT INTO finance.canonical_identity_automatic_decisions (
            canonical_identity_policy_generation_id, canonical_identity_policy_id,
            policy_version, policy_hash, outcome, confidence_tier,
            confidence_basis_points, rationale_code, feature_vector,
            competing_candidate_proof, source_hashes, decision_hash, observed_at
        ) VALUES (
            policy_generation_id, identity_policy_id, 'canonical-identity-v1',
            repeat('1', 64), 'unresolved', 'review-required', 0,
            'legacy-proof-shape',
            jsonb_build_object('componentSize', 2),
            jsonb_build_object(
                'selectedClaimHash', claim_hash_value,
                'candidateClaimHashes', jsonb_build_array(claim_hash_value)
            ),
            jsonb_build_array(source_hash_one),
            repeat('0', 64), '2026-02-01T00:00:06Z'
        );
        RAISE EXCEPTION 'invalid competing candidate proof unexpectedly succeeded';
    EXCEPTION
        WHEN SQLSTATE '23514' THEN NULL;
    END;

    INSERT INTO finance.canonical_identity_automatic_decisions (
        canonical_identity_policy_generation_id, canonical_identity_policy_id,
        policy_version, policy_hash, outcome, confidence_tier,
        confidence_basis_points, rationale_code, feature_vector,
        competing_candidate_proof, source_hashes, decision_hash, observed_at
    ) VALUES (
        policy_generation_id, identity_policy_id, 'canonical-identity-v1',
        repeat('1', 64), 'link-transfer', 'explicit-lineage', 10000,
        'explicit-transfer-lineage',
        jsonb_build_object('differentAccounts', true, 'sourceFamilyPair', 'manual-simplefin'),
        '{}'::jsonb,
        jsonb_build_array(source_hash_one, source_hash_two),
        repeat('1', 64), '2026-02-01T00:00:06Z'
    ) RETURNING canonical_identity_automatic_decision_id
      INTO transfer_decision_id;

    INSERT INTO finance.canonical_identity_decision_event_memberships (
        canonical_identity_policy_generation_id,
        canonical_identity_automatic_decision_id, canonical_identity_event_id,
        canonical_id, membership_role, membership_hash, observed_at
    ) VALUES
        (
            policy_generation_id, transfer_decision_id, event_id, 'canonical-event-1', 'subject',
            repeat('2', 64), '2026-02-01T00:00:06Z'
        ),
        (
            policy_generation_id, transfer_decision_id, other_event_id, 'canonical-event-2',
            'counterparty', repeat('3', 64), '2026-02-01T00:00:06Z'
        );

    INSERT INTO finance.canonical_identity_automatic_decisions (
        canonical_identity_policy_generation_id, canonical_identity_policy_id,
        policy_version, policy_hash, outcome, confidence_tier,
        confidence_basis_points, rationale_code, feature_vector,
        competing_candidate_proof, source_hashes, decision_hash, observed_at
    ) VALUES (
        policy_generation_id, identity_policy_id, 'canonical-identity-v1',
        repeat('1', 64), 'unresolved', 'review-required', 0,
        'ambiguous-cross-source-cardinality',
        jsonb_build_object('componentSize', 2, 'edgeCount', 1),
        jsonb_build_object(
            'degree:' || claim_hash_value, 2,
            'degree:' || alternate_claim_hash, 1
        ),
        jsonb_build_array(source_hash_one, source_hash_two),
        repeat('4', 64), '2026-02-01T00:00:06Z'
    ) RETURNING canonical_identity_automatic_decision_id
      INTO unresolved_decision_id;

    INSERT INTO finance.canonical_identity_decision_event_memberships (
        canonical_identity_policy_generation_id,
        canonical_identity_automatic_decision_id, canonical_identity_event_id,
        canonical_id, membership_role, membership_hash, observed_at
    ) VALUES
        (
            policy_generation_id, unresolved_decision_id, event_id, 'canonical-event-1', 'subject',
            repeat('5', 64), '2026-02-01T00:00:06Z'
        ),
        (
            policy_generation_id, unresolved_decision_id, other_event_id, 'canonical-event-2',
            'related', repeat('6', 64), '2026-02-01T00:00:06Z'
        );

    INSERT INTO finance.canonical_identity_human_overrides (
        canonical_identity_policy_generation_id,
        override_id, override_version, override_action, claim_hashes,
        rationale_hash, override_hash, decided_at,
        canonical_identity_automatic_decision_id, precedence_rank,
        override_metadata, observed_at
    ) VALUES (
        policy_generation_id, 'human-transfer-1', 1, 'transfer',
        jsonb_build_array(claim_hash_value, alternate_claim_hash),
        repeat('7', 64), repeat('8', 64), '2026-02-01T00:00:07Z',
        transfer_decision_id, 1000,
        jsonb_build_object('precedence', 'human', 'ticket', 'identity-review-1'),
        '2026-02-01T00:00:07Z'
    ) RETURNING canonical_identity_human_override_id INTO human_override_id;

    INSERT INTO finance.canonical_identity_human_overrides (
        canonical_identity_policy_generation_id,
        override_id, override_version, override_action, claim_hashes,
        rationale_hash, override_hash, decided_at, precedence_rank,
        override_metadata, observed_at
    ) VALUES (
        second_policy_generation_id, 'human-standalone-1', 1, 'preserve-distinct',
        jsonb_build_array(claim_hash_value, alternate_claim_hash),
        repeat('b', 64), repeat('c', 64), '2026-02-02T00:00:07Z', 1000,
        jsonb_build_object('precedence', 'human', 'ticket', 'identity-review-standalone'),
        '2026-02-02T00:00:07Z'
    ) RETURNING canonical_identity_human_override_id INTO generation_only_override_id;

    INSERT INTO finance.canonical_identity_human_overrides (
        canonical_identity_policy_generation_id,
        override_id, override_version, override_action, claim_hashes,
        rationale_hash, override_hash, decided_at, precedence_rank,
        override_metadata, observed_at
    ) VALUES (
        second_policy_generation_id, 'human-transfer-1', 1, 'transfer',
        jsonb_build_array(claim_hash_value, alternate_claim_hash),
        repeat('7', 64), repeat('8', 64), '2026-02-01T00:00:07Z', 1000,
        jsonb_build_object('precedence', 'human', 'ticket', 'identity-review-1'),
        '2026-02-02T00:00:08Z'
    ) RETURNING canonical_identity_human_override_id INTO replay_human_override_id;

    INSERT INTO finance.canonical_identity_decision_event_memberships (
        canonical_identity_policy_generation_id,
        canonical_identity_human_override_id, canonical_identity_event_id,
        canonical_id, membership_role, membership_hash, observed_at
    ) VALUES
        (
            policy_generation_id, human_override_id, event_id, 'canonical-event-1', 'subject',
            repeat('9', 64), '2026-02-01T00:00:07Z'
        ),
        (
            policy_generation_id, human_override_id, other_event_id, 'canonical-event-2',
            'counterparty', repeat('a', 64), '2026-02-01T00:00:07Z'
        );

    INSERT INTO finance.canonical_identity_decision_event_memberships (
        canonical_identity_policy_generation_id,
        canonical_identity_human_override_id, canonical_identity_event_id,
        canonical_id, membership_role, membership_hash, observed_at
    ) VALUES (
        second_policy_generation_id, generation_only_override_id, standalone_event_id,
        'canonical-event-standalone', 'subject', repeat('b', 64),
        '2026-02-02T00:00:07Z'
    );

    INSERT INTO finance.canonical_identity_decision_event_memberships (
        canonical_identity_policy_generation_id,
        canonical_identity_human_override_id, canonical_identity_event_id,
        canonical_id, membership_role, membership_hash, observed_at
    ) VALUES (
        second_policy_generation_id, replay_human_override_id, replay_event_id,
        'canonical-event-1', 'subject', repeat('c', 64),
        '2026-02-02T00:00:08Z'
    );

    INSERT INTO finance.application_projection_bindings (
        canonical_identity_policy_generation_id,
        canonical_identity_event_id, canonical_id, target_application,
        target_activity_hash, binding_source,
        canonical_identity_automatic_decision_id, effective_from, observed_at
    ) VALUES (
        policy_generation_id, event_id, 'canonical-event-1', 'wealthfolio', repeat('b', 64),
        'automatic_decision', automatic_decision_id,
        '2026-02-01T00:00:00Z', '2026-02-01T00:00:08Z'
    ) RETURNING application_projection_binding_id INTO projection_binding_id;

    BEGIN
        INSERT INTO finance.application_projection_bindings (
            canonical_identity_policy_generation_id,
            canonical_identity_event_id, canonical_id, target_application,
            target_activity_hash, binding_source,
            canonical_identity_automatic_decision_id, effective_from, observed_at
        ) VALUES (
            policy_generation_id, event_id, 'canonical-event-1', 'wealthfolio', repeat('c', 64),
            'automatic_decision', automatic_decision_id,
            '2026-02-01T00:01:00Z', '2026-02-01T00:01:00Z'
        );
        RAISE EXCEPTION 'second active wealthfolio binding for one canonical event unexpectedly succeeded';
    EXCEPTION
        WHEN unique_violation THEN NULL;
    END;

    BEGIN
        INSERT INTO finance.application_projection_bindings (
            canonical_identity_policy_generation_id,
            canonical_identity_event_id, canonical_id, target_application,
            target_activity_hash, binding_source,
            canonical_identity_automatic_decision_id, effective_from, observed_at
        ) VALUES (
            policy_generation_id, other_event_id, 'canonical-event-2', 'wealthfolio', repeat('b', 64),
            'automatic_decision', transfer_decision_id,
            '2026-02-01T00:01:00Z', '2026-02-01T00:01:00Z'
        );
        RAISE EXCEPTION 'one active wealthfolio target mapped to multiple events unexpectedly succeeded';
    EXCEPTION
        WHEN unique_violation THEN NULL;
    END;

    UPDATE finance.application_projection_bindings
    SET effective_to = '2026-02-01T00:02:00Z',
        is_active = false
    WHERE application_projection_binding_id = projection_binding_id;

    INSERT INTO finance.application_projection_bindings (
        canonical_identity_policy_generation_id,
        canonical_identity_event_id, canonical_id, target_application,
        target_activity_hash, binding_source,
        canonical_identity_human_override_id, effective_from, observed_at
    ) VALUES (
        policy_generation_id, event_id, 'canonical-event-1', 'wealthfolio', repeat('d', 64),
        'human_override', human_override_id,
        '2026-02-01T00:02:01Z', '2026-02-01T00:02:01Z'
    );

    INSERT INTO finance.application_projection_bindings (
        canonical_identity_policy_generation_id,
        canonical_identity_event_id, canonical_id, target_application,
        target_activity_hash, binding_source, effective_from, observed_at
    ) VALUES (
        second_policy_generation_id, standalone_event_id, 'canonical-event-standalone',
        'wealthfolio', repeat('5', 64), 'policy_generation',
        '2026-02-02T00:02:01Z', '2026-02-02T00:02:01Z'
    );

    IF NOT EXISTS (
        SELECT 1
        FROM finance.application_projection_bindings
        WHERE canonical_identity_policy_generation_id = second_policy_generation_id
          AND canonical_identity_event_id = standalone_event_id
          AND canonical_id = 'canonical-event-standalone'
          AND binding_source = 'policy_generation'
          AND canonical_identity_automatic_decision_id IS NULL
          AND canonical_identity_human_override_id IS NULL
    ) THEN
        RAISE EXCEPTION 'policy-generation projection binding unexpectedly failed';
    END IF;

    BEGIN
        INSERT INTO finance.application_projection_bindings (
            canonical_identity_policy_generation_id,
            canonical_identity_event_id, canonical_id, target_application,
            target_activity_hash, binding_source, effective_from, observed_at
        ) VALUES (
            second_policy_generation_id, replay_event_id, 'canonical-event-1',
            'wealthfolio', repeat('6', 64), 'policy_generation',
            '2026-02-02T00:02:02Z', '2026-02-02T00:02:02Z'
        );
        RAISE EXCEPTION 'active wealthfolio binding uniqueness by canonical id was not enforced globally';
    EXCEPTION
        WHEN unique_violation THEN NULL;
    END;

    BEGIN
        UPDATE finance.canonical_identity_graph_edges
        SET automatic = false
        WHERE edge_hash = repeat('f', 64);
        RAISE EXCEPTION 'graph edge update unexpectedly succeeded';
    EXCEPTION
        WHEN SQLSTATE '55000' THEN NULL;
    END;

    BEGIN
        UPDATE finance.canonical_identity_source_claims
        SET selected_observation_hash = repeat('e', 64)
        WHERE canonical_identity_source_claim_id = claim_id;
        RAISE EXCEPTION 'source claim update unexpectedly succeeded';
    EXCEPTION
        WHEN SQLSTATE '55000' THEN NULL;
    END;

    BEGIN
        UPDATE finance.canonical_identity_decision_event_memberships
        SET membership_role = 'related'
        WHERE canonical_identity_automatic_decision_id = transfer_decision_id
          AND canonical_id = 'canonical-event-2';
        RAISE EXCEPTION 'decision event membership update unexpectedly succeeded';
    EXCEPTION
        WHEN SQLSTATE '55000' THEN NULL;
    END;

    BEGIN
        DELETE FROM finance.canonical_identity_automatic_decisions
        WHERE canonical_identity_automatic_decision_id = automatic_decision_id;
        RAISE EXCEPTION 'automatic decision delete unexpectedly succeeded';
    EXCEPTION
        WHEN SQLSTATE '55000' THEN NULL;
    END;

    IF (
        SELECT count(*)
        FROM finance.canonical_identity_source_claims
        WHERE claim_hash = claim_hash_value
    ) <> 2 THEN
        RAISE EXCEPTION 'generation-scoped source claim snapshots were not preserved';
    END IF;

    IF (
        SELECT count(*)
        FROM finance.canonical_identity_events
        WHERE canonical_id = 'canonical-event-1'
    ) <> 2 THEN
        RAISE EXCEPTION 'generation-scoped canonical event snapshots were not preserved';
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM finance.canonical_identity_events
        WHERE canonical_identity_policy_generation_id = second_policy_generation_id
          AND canonical_id = 'canonical-event-1'
          AND event_hash = repeat('f', 64)
    ) THEN
        RAISE EXCEPTION 'later generation canonical event replay did not retain its distinct event hash';
    END IF;

    IF (
        SELECT count(*)
        FROM finance.canonical_identity_human_overrides
        WHERE override_id = 'human-transfer-1'
          AND override_version = 1
    ) <> 2 THEN
        RAISE EXCEPTION 'generation-scoped human override snapshots were not preserved';
    END IF;

    IF (
        SELECT count(*)
        FROM finance.canonical_identity_human_overrides
        WHERE override_hash = repeat('8', 64)
    ) <> 2 THEN
        RAISE EXCEPTION 'generation-scoped human override hashes were not preserved';
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM finance_read.identity_generation_summary
        WHERE canonical_identity_policy_generation_id = policy_generation_id
          AND policy_version = 'canonical-identity-v1'
          AND input_hash = repeat('3', 64)
          AND canonical_state_hash = repeat('4', 64)
          AND source_claim_count = 1
          AND graph_edge_count = 1
          AND automatic_decision_count = 4
          AND safe_automatic_resolution_count = 2
          AND canonical_event_count = 3
          AND unresolved_decision_count = 1
          AND human_override_count = 1
          AND active_wealthfolio_binding_count = 1
    ) THEN
        RAISE EXCEPTION 'identity generation summary did not reflect synthetic data';
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM finance_read.identity_generation_summary
        WHERE canonical_identity_policy_generation_id = second_policy_generation_id
          AND policy_version = 'canonical-identity-v1'
          AND input_hash = repeat('7', 64)
          AND canonical_state_hash = repeat('8', 64)
          AND source_claim_count = 1
          AND graph_edge_count = 0
          AND automatic_decision_count = 0
          AND safe_automatic_resolution_count = 0
          AND canonical_event_count = 2
          AND unresolved_decision_count = 0
          AND human_override_count = 2
          AND active_wealthfolio_binding_count = 1
    ) THEN
        RAISE EXCEPTION 'identity generation summary did not include policy-generation projection bindings';
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM finance_read.identity_decision_audit
        WHERE decision_source = 'automatic'
          AND decision_id = automatic_decision_id
          AND outcome = 'merge-observations'
          AND confidence_tier = 'exact-scoped-identity'
          AND rationale_code = 'exact-scoped-source-identity'
          AND canonical_event_ids = jsonb_build_array('canonical-event-1')
    ) THEN
        RAISE EXCEPTION 'identity decision audit view did not expose the exact-claim decision';
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM finance_read.identity_decision_audit
        WHERE decision_source = 'human_override'
          AND decision_id = human_override_id
          AND override_id = 'human-transfer-1'
          AND outcome = 'transfer'
          AND confidence_tier = 'human-override'
          AND canonical_event_ids = jsonb_build_array(
              'canonical-event-1', 'canonical-event-2'
          )
          AND active_target_activity_hashes = jsonb_build_array(repeat('d', 64))
    ) THEN
        RAISE EXCEPTION 'identity decision audit view did not expose the override binding';
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM finance_read.identity_decision_audit
        WHERE decision_source = 'human_override'
          AND decision_id = generation_only_override_id
          AND override_id = 'human-standalone-1'
          AND policy_version = 'canonical-identity-v1'
          AND generation_hash = repeat('6', 64)
          AND input_hash = repeat('7', 64)
          AND canonical_state_hash = repeat('8', 64)
          AND outcome = 'preserve-distinct'
          AND confidence_tier = 'human-override'
          AND canonical_event_ids = jsonb_build_array('canonical-event-standalone')
          AND active_target_activity_hashes = jsonb_build_array(repeat('5', 64))
    ) THEN
        RAISE EXCEPTION 'identity decision audit view did not attribute standalone override rows to their generation';
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM finance_read.identity_decision_audit
        WHERE decision_source = 'human_override'
          AND decision_id = replay_human_override_id
          AND override_id = 'human-transfer-1'
          AND policy_version = 'canonical-identity-v1'
          AND generation_hash = repeat('6', 64)
          AND input_hash = repeat('7', 64)
          AND canonical_state_hash = repeat('8', 64)
          AND outcome = 'transfer'
          AND confidence_tier = 'human-override'
          AND canonical_event_ids = jsonb_build_array('canonical-event-1')
          AND active_target_activity_hashes = '[]'::jsonb
    ) THEN
        RAISE EXCEPTION 'identity decision audit view did not preserve replayed human overrides across generations';
    END IF;
END;
$test$;

DO $test$
DECLARE
    protected_table text;
BEGIN
    FOREACH protected_table IN ARRAY ARRAY[
        'source_blobs', 'ingestion_runs', 'transaction_observations', 'balance_observations',
        'position_observations', 'valuation_observations', 'app_state_snapshots',
        'durable_decisions', 'audit_events', 'source_account_links',
        'conformance_observations', 'artifact_observations',
        'shadow_authority_metadata', 'shadow_plans', 'shadow_run_events',
        'lineage_review_groups', 'lineage_review_group_members',
        'lineage_review_decisions', 'canonical_identity_policies',
        'canonical_identity_policy_generations',
        'canonical_identity_source_claims',
        'canonical_identity_observation_memberships',
        'canonical_identity_graph_edges', 'canonical_identity_events',
        'canonical_identity_event_members',
        'canonical_identity_event_relationships',
        'canonical_identity_automatic_decisions',
        'canonical_identity_human_overrides',
        'canonical_identity_decision_event_memberships',
        'canonical_identity_source_authority_policies',
        'canonical_identity_authority_intervals',
        'canonical_identity_source_suppressions',
        'application_projection_bindings'
    ] LOOP
        IF NOT EXISTS (
            SELECT 1
            FROM pg_trigger t
            JOIN pg_class c ON c.oid = t.tgrelid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'finance'
              AND c.relname = protected_table
              AND NOT t.tgisinternal
              AND (t.tgtype & 8) = 8
        ) THEN
            RAISE EXCEPTION 'missing delete protection on %', protected_table;
        END IF;
    END LOOP;
END;
$test$;

BEGIN;
SET LOCAL ROLE finance_shadow_backup;
SELECT count(*) FROM finance.source_blobs;
DO $test$
BEGIN
    BEGIN
        PERFORM rolpassword FROM pg_catalog.pg_authid LIMIT 1;
        RAISE EXCEPTION 'backup role unexpectedly read pg_authid';
    EXCEPTION
        WHEN insufficient_privilege THEN NULL;
    END;
END;
$test$;
ROLLBACK;

SELECT 'postgres deployment integration checks passed' AS result;
