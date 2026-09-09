BEGIN;

CREATE TABLE finance.incremental_scopes (
    scope_id uuid PRIMARY KEY,
    configuration_hash text NOT NULL CHECK (configuration_hash ~ '^[0-9a-f]{64}$'),
    configuration jsonb NOT NULL CHECK (jsonb_typeof(configuration) = 'object'),
    canonical_account_hash text NOT NULL CHECK (canonical_account_hash ~ '^[0-9a-f]{64}$'),
    target_origin text NOT NULL,
    activity_account_id text NOT NULL,
    source_connection_id text NOT NULL,
    source_account_id text NOT NULL,
    UNIQUE (target_origin, activity_account_id),
    UNIQUE (canonical_account_hash),
    UNIQUE (source_connection_id, source_account_id)
);
CREATE TABLE finance.incremental_source_versions (
    version_id uuid PRIMARY KEY,
    scope_id uuid NOT NULL REFERENCES finance.incremental_scopes,
    source_id text NOT NULL,
    version_number bigint NOT NULL CHECK (version_number > 0),
    economic_hash text NOT NULL CHECK (economic_hash ~ '^[0-9a-f]{64}$'),
    predecessor_version_id uuid REFERENCES finance.incremental_source_versions,
    observation_document jsonb NOT NULL CHECK (jsonb_typeof(observation_document) = 'object'),
    currency_proof jsonb NOT NULL CHECK (jsonb_typeof(currency_proof) = 'object'),
    first_snapshot_hash text NOT NULL CHECK (first_snapshot_hash ~ '^[0-9a-f]{64}$'),
    admitted boolean NOT NULL,
    reason text NOT NULL,
    UNIQUE (scope_id, source_id, version_number),
    UNIQUE (version_id, scope_id, source_id),
    FOREIGN KEY (predecessor_version_id, scope_id, source_id)
        REFERENCES finance.incremental_source_versions(version_id, scope_id, source_id),
    CHECK (admitted = (reason = ''))
);
CREATE TABLE finance.incremental_runs (
    run_hash text PRIMARY KEY CHECK (run_hash ~ '^[0-9a-f]{64}$'),
    scope_id uuid NOT NULL REFERENCES finance.incremental_scopes,
    receipt_hash text CHECK (receipt_hash ~ '^[0-9a-f]{64}$'),
    snapshot_hash text CHECK (snapshot_hash ~ '^[0-9a-f]{64}$'),
    manifest_hash text NOT NULL CHECK (manifest_hash ~ '^[0-9a-f]{64}$'),
    policy_hash text NOT NULL CHECK (policy_hash ~ '^[0-9a-f]{64}$'),
    generation_id uuid REFERENCES finance.accepted_identity_generations,
    source_observed_at timestamptz,
    balance_effective_at timestamptz,
    source_balance numeric(24,8),
    currency_code text CHECK (currency_code ~ '^[A-Z]{3}$'),
    plan_document jsonb NOT NULL CHECK (jsonb_typeof(plan_document) = 'object'),
    processed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (run_hash, scope_id)
);
CREATE TABLE finance.incremental_source_sightings (
    scope_id uuid NOT NULL REFERENCES finance.incremental_scopes,
    receipt_hash text NOT NULL CHECK (receipt_hash ~ '^[0-9a-f]{64}$'),
    source_id text NOT NULL,
    version_id uuid NOT NULL REFERENCES finance.incremental_source_versions,
    observed_at timestamptz NOT NULL,
    snapshot_hash text NOT NULL CHECK (snapshot_hash ~ '^[0-9a-f]{64}$'),
    PRIMARY KEY (scope_id, receipt_hash, source_id),
    FOREIGN KEY (version_id, scope_id, source_id)
        REFERENCES finance.incremental_source_versions(version_id, scope_id, source_id)
);
CREATE TABLE finance.incremental_qualifications (
    qualification_id uuid PRIMARY KEY,
    scope_id uuid NOT NULL REFERENCES finance.incremental_scopes,
    run_hash text NOT NULL REFERENCES finance.incremental_runs,
    generation_event_id uuid NOT NULL REFERENCES finance.canonical_identity_events,
    accepted_event_id uuid,
    revision_number bigint,
    source_day date NOT NULL,
    signed_amount numeric(24,8) NOT NULL,
    currency_code text NOT NULL CHECK (currency_code ~ '^[A-Z]{3}$'),
    description text NOT NULL,
    event_status text NOT NULL,
    qualification_status text NOT NULL CHECK (qualification_status IN ('eligible', 'held', 'pending')),
    reason text NOT NULL,
    proof jsonb NOT NULL CHECK (jsonb_typeof(proof) = 'object'),
    FOREIGN KEY (accepted_event_id, revision_number)
        REFERENCES finance.accepted_identity_revisions,
    UNIQUE (run_hash, generation_event_id),
    UNIQUE (qualification_id, run_hash, accepted_event_id, revision_number),
    CHECK (qualification_status <> 'eligible' OR accepted_event_id IS NOT NULL)
);
CREATE TABLE finance.incremental_activity_bindings (
    scope_id uuid NOT NULL REFERENCES finance.incremental_scopes,
    accepted_event_id uuid NOT NULL REFERENCES finance.accepted_identity_events,
    activity_id text NOT NULL CHECK (btrim(activity_id) <> ''),
    idempotency_key text NOT NULL CHECK (btrim(idempotency_key) <> ''),
    origin_run_hash text NOT NULL REFERENCES finance.incremental_runs,
    observed_activity jsonb NOT NULL CHECK (jsonb_typeof(observed_activity) = 'object'),
    PRIMARY KEY (scope_id, accepted_event_id),
    UNIQUE (scope_id, accepted_event_id, activity_id),
    UNIQUE (scope_id, activity_id),
    UNIQUE (scope_id, idempotency_key),
    FOREIGN KEY (origin_run_hash, scope_id) REFERENCES finance.incremental_runs(run_hash, scope_id)
);
CREATE TABLE finance.incremental_outbox (
    operation_id uuid PRIMARY KEY,
    run_hash text NOT NULL REFERENCES finance.incremental_runs,
    qualification_id uuid NOT NULL REFERENCES finance.incremental_qualifications,
    accepted_event_id uuid NOT NULL,
    revision_number bigint NOT NULL,
    operation_kind text NOT NULL CHECK (operation_kind IN ('create', 'update')),
    payload_hash text NOT NULL CHECK (payload_hash ~ '^[0-9a-f]{64}$'),
    operation_document jsonb NOT NULL CHECK (jsonb_typeof(operation_document) = 'object'),
    FOREIGN KEY (accepted_event_id, revision_number) REFERENCES finance.accepted_identity_revisions,
    FOREIGN KEY (qualification_id, run_hash, accepted_event_id, revision_number)
        REFERENCES finance.incremental_qualifications(qualification_id, run_hash, accepted_event_id, revision_number),
    CHECK (payload_hash = operation_document->>'payloadHash'),
    UNIQUE (run_hash, accepted_event_id)
);
CREATE TABLE finance.incremental_attempts (
    event_id uuid PRIMARY KEY,
    event_number bigint GENERATED ALWAYS AS IDENTITY UNIQUE,
    operation_id uuid NOT NULL REFERENCES finance.incremental_outbox,
    state text NOT NULL CHECK (state IN ('prepared', 'applied', 'uncertain', 'held')),
    evidence jsonb NOT NULL CHECK (jsonb_typeof(evidence) = 'object'),
    processed_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE finance.incremental_run_events (
    event_id uuid PRIMARY KEY,
    event_number bigint GENERATED ALWAYS AS IDENTITY UNIQUE,
    run_hash text NOT NULL REFERENCES finance.incremental_runs,
    state text NOT NULL CHECK (state IN ('pending', 'noop', 'applied', 'held', 'uncertain', 'backup')),
    evidence jsonb NOT NULL CHECK (jsonb_typeof(evidence) = 'object'),
    processed_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE finance.incremental_projection_observations (
    observation_id uuid PRIMARY KEY,
    observation_number bigint GENERATED ALWAYS AS IDENTITY UNIQUE,
    scope_id uuid NOT NULL,
    accepted_event_id uuid NOT NULL,
    activity_id text NOT NULL,
    run_hash text NOT NULL,
    source_generation_event_id uuid NOT NULL REFERENCES finance.canonical_identity_events,
    source_revision_number bigint,
    original_projection_binding_id uuid REFERENCES finance.application_projection_bindings,
    operation_id uuid UNIQUE REFERENCES finance.incremental_outbox,
    observation_kind text NOT NULL CHECK (observation_kind IN ('adopted','applied')),
    observed_activity jsonb NOT NULL CHECK (jsonb_typeof(observed_activity)='object'),
    processed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY (scope_id,accepted_event_id,activity_id)
        REFERENCES finance.incremental_activity_bindings(scope_id,accepted_event_id,activity_id),
    FOREIGN KEY (run_hash,scope_id) REFERENCES finance.incremental_runs(run_hash,scope_id),
    FOREIGN KEY (accepted_event_id,source_revision_number) REFERENCES finance.accepted_identity_revisions,
    CHECK ((observation_kind='applied') = (operation_id IS NOT NULL)),
    CHECK (source_revision_number IS NOT NULL OR original_projection_binding_id IS NOT NULL)
);
CREATE INDEX incremental_projection_observation_current_idx
ON finance.incremental_projection_observations(scope_id,accepted_event_id,observation_number DESC);

CREATE FUNCTION finance.validate_incremental_projection_observation()
RETURNS trigger LANGUAGE plpgsql AS $function$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM finance.canonical_identity_events event
        JOIN finance.incremental_scopes scope ON scope.canonical_account_hash=event.canonical_account_hash
        WHERE event.canonical_identity_event_id=NEW.source_generation_event_id
          AND scope.scope_id=NEW.scope_id AND event.trusted AND event.status='posted'
          AND NEW.observed_activity->>'id'=NEW.activity_id
          AND NEW.observed_activity->>'accountId'=scope.activity_account_id
          AND NEW.observed_activity->>'status'='POSTED'
          AND NEW.observed_activity->>'currency'=event.currency_code
          AND CASE
              WHEN NEW.observed_activity->>'activityType' IN ('DEPOSIT','CREDIT','TRANSFER_IN','INTEREST','DIVIDEND')
                  THEN abs((NEW.observed_activity->>'amount')::numeric)
              WHEN NEW.observed_activity->>'activityType' IN ('WITHDRAWAL','TRANSFER_OUT','FEE','TAX','EXPENSE')
                  THEN -abs((NEW.observed_activity->>'amount')::numeric)
              ELSE NULL
          END = event.signed_amount
          AND (
              (NEW.source_revision_number IS NOT NULL AND EXISTS (
                  SELECT 1 FROM finance.accepted_identity_event_mappings mapping
                  WHERE mapping.generation_event_id=NEW.source_generation_event_id
                    AND mapping.accepted_event_id=NEW.accepted_event_id
                    AND mapping.revision_number=NEW.source_revision_number
              ))
              OR
              (NEW.source_revision_number IS NULL AND EXISTS (
                  SELECT 1 FROM finance.application_projection_bindings binding
                  JOIN finance.accepted_identity_projection_links link
                    ON link.application_projection_binding_id=binding.application_projection_binding_id
                  WHERE binding.application_projection_binding_id=NEW.original_projection_binding_id
                    AND binding.canonical_identity_event_id=NEW.source_generation_event_id
                    AND link.accepted_event_id=NEW.accepted_event_id AND binding.is_active
              ))
          )
          AND (NEW.operation_id IS NULL OR EXISTS (
              SELECT 1 FROM finance.incremental_outbox outbox
              JOIN finance.incremental_qualifications q ON q.qualification_id=outbox.qualification_id
              WHERE outbox.operation_id=NEW.operation_id AND outbox.run_hash=NEW.run_hash
                AND outbox.accepted_event_id=NEW.accepted_event_id
                AND outbox.revision_number=NEW.source_revision_number
                AND q.generation_event_id=NEW.source_generation_event_id
          ))
    ) THEN
        RAISE EXCEPTION 'projection observation requires a verified source/app revision binding'
            USING ERRCODE='23514';
    END IF;
    RETURN NEW;
END;
$function$;
CREATE TRIGGER incremental_projection_observation_validate
BEFORE INSERT ON finance.incremental_projection_observations
FOR EACH ROW EXECUTE FUNCTION finance.validate_incremental_projection_observation();

CREATE FUNCTION finance.validate_incremental_qualification()
RETURNS trigger LANGUAGE plpgsql AS $function$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM finance.incremental_runs run
        JOIN finance.incremental_scopes scope ON scope.scope_id=run.scope_id
        JOIN finance.canonical_identity_events event
          ON event.canonical_identity_policy_generation_id=run.generation_id
        WHERE run.run_hash=NEW.run_hash AND scope.scope_id=NEW.scope_id
          AND event.canonical_identity_event_id=NEW.generation_event_id
          AND event.canonical_account_hash=scope.canonical_account_hash
          AND event.source_day=NEW.source_day AND event.signed_amount=NEW.signed_amount
          AND event.currency_code=NEW.currency_code AND event.status=NEW.event_status
          AND (NEW.qualification_status <> 'eligible' OR (
              event.trusted AND event.status='posted' AND run.source_balance IS NOT NULL
              AND run.currency_code=NEW.currency_code
              AND (run.plan_document->>'predictedCash')::numeric=run.source_balance
              AND jsonb_array_length(NEW.proof->'currencyEvidence') > 0
          ))
    ) THEN
        RAISE EXCEPTION 'incremental qualification is not bound to its scoped source event'
            USING ERRCODE='23514';
    END IF;
    RETURN NEW;
END;
$function$;
CREATE TRIGGER incremental_qualification_validate
BEFORE INSERT ON finance.incremental_qualifications
FOR EACH ROW EXECUTE FUNCTION finance.validate_incremental_qualification();

CREATE FUNCTION finance.validate_incremental_outbox()
RETURNS trigger LANGUAGE plpgsql AS $function$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM finance.incremental_qualifications q
        JOIN finance.incremental_scopes s ON s.scope_id=q.scope_id
        WHERE q.qualification_id=NEW.qualification_id AND q.qualification_status='eligible'
          AND NEW.operation_document->'payload'->>'accountId'=s.activity_account_id
          AND NEW.operation_document->>'kind'=NEW.operation_kind
    ) THEN
        RAISE EXCEPTION 'incremental outbox requires an eligible scoped qualification'
            USING ERRCODE='23514';
    END IF;
    RETURN NEW;
END;
$function$;
CREATE TRIGGER incremental_outbox_validate
BEFORE INSERT ON finance.incremental_outbox
FOR EACH ROW EXECUTE FUNCTION finance.validate_incremental_outbox();

DO $do$
DECLARE name text;
BEGIN
    FOREACH name IN ARRAY ARRAY[
        'incremental_scopes', 'incremental_source_versions', 'incremental_source_sightings',
        'incremental_runs', 'incremental_qualifications', 'incremental_activity_bindings',
        'incremental_outbox', 'incremental_attempts', 'incremental_run_events',
        'incremental_projection_observations'
    ] LOOP
        EXECUTE format(
            'CREATE TRIGGER %I BEFORE UPDATE OR DELETE ON finance.%I '
            'FOR EACH ROW EXECUTE FUNCTION finance.reject_append_only_change()',
            name || '_append_only', name
        );
    END LOOP;
END;
$do$;
CREATE INDEX incremental_run_scope_idx ON finance.incremental_runs(scope_id, processed_at DESC);
CREATE INDEX incremental_attempt_operation_idx ON finance.incremental_attempts(operation_id, event_number DESC);
CREATE INDEX incremental_run_event_idx ON finance.incremental_run_events(run_hash, event_number DESC);

CREATE VIEW finance_read.incremental_scope_status AS
SELECT scope.scope_id, scope.canonical_account_hash, scope.activity_account_id,
       scope.source_connection_id, scope.source_account_id,
       run.run_hash, run.receipt_hash, run.snapshot_hash, run.manifest_hash,
       run.policy_hash, known.source_observed_at, known.balance_effective_at,
       known.source_balance, known.currency_code,
       known.run_hash AS last_source_run_hash, event.state, event.evidence,
       event.processed_at
FROM finance.incremental_scopes scope
LEFT JOIN LATERAL (
    SELECT * FROM finance.incremental_runs r WHERE r.scope_id = scope.scope_id
    ORDER BY (SELECT max(event_number) FROM finance.incremental_run_events e
              WHERE e.run_hash=r.run_hash) DESC NULLS LAST, r.processed_at DESC LIMIT 1
) run ON true
LEFT JOIN LATERAL (
    SELECT * FROM finance.incremental_runs r
    WHERE r.scope_id=scope.scope_id AND r.source_observed_at IS NOT NULL
    ORDER BY r.source_observed_at DESC, r.processed_at DESC LIMIT 1
) known ON true
LEFT JOIN LATERAL (
    SELECT * FROM finance.incremental_run_events e WHERE e.run_hash = run.run_hash
    ORDER BY e.event_number DESC LIMIT 1
) event ON true;

CREATE VIEW finance_read.incremental_cash_events AS
SELECT q.*, run.source_observed_at AS collection_observed_at,
       event.observed_at AS financial_observed_at, sighting.last_sighted_at,
       run.balance_effective_at, run.source_balance,
       run.receipt_hash, run.snapshot_hash, run.manifest_hash, run.policy_hash,
       binding.activity_id, binding.idempotency_key,
       delivered.source_revision_number AS delivered_source_revision_number,
       delivered.source_generation_event_id AS delivered_source_event_id,
       delivered.source_day AS delivered_source_day,
       delivered.signed_amount AS delivered_source_signed_amount,
       outbox.operation_id, CASE WHEN q.qualification_status <> 'eligible' THEN q.qualification_status
           ELSE COALESCE(attempt.state, CASE WHEN outbox.operation_id IS NOT NULL
           THEN 'pending' WHEN binding.activity_id IS NOT NULL THEN 'observed' ELSE 'held' END) END
           AS projection_status,
       attempt.evidence AS projection_evidence
FROM finance.incremental_qualifications q
JOIN finance.incremental_runs run ON run.run_hash = q.run_hash
JOIN finance.canonical_identity_events event
  ON event.canonical_identity_event_id = q.generation_event_id
LEFT JOIN LATERAL (
    SELECT max(s.observed_at) AS last_sighted_at
    FROM finance.incremental_source_sightings s
    WHERE s.scope_id=q.scope_id AND s.source_id IN (
        SELECT jsonb_array_elements_text(q.proof->'sourceIds')
    )
) sighting ON true
LEFT JOIN finance.incremental_activity_bindings binding
  ON binding.scope_id = q.scope_id AND binding.accepted_event_id = q.accepted_event_id
LEFT JOIN finance.incremental_outbox outbox ON outbox.qualification_id = q.qualification_id
LEFT JOIN LATERAL (
    SELECT source_revision_number,source_generation_event_id,source.source_day,source.signed_amount
    FROM finance.incremental_projection_observations observation
    JOIN finance.canonical_identity_events source
      ON source.canonical_identity_event_id=observation.source_generation_event_id
    WHERE observation.scope_id=q.scope_id AND observation.accepted_event_id=q.accepted_event_id
    ORDER BY observation_number DESC LIMIT 1
) delivered ON true
LEFT JOIN LATERAL (
    SELECT * FROM finance.incremental_attempts a WHERE a.operation_id = outbox.operation_id
    ORDER BY a.event_number DESC LIMIT 1
) attempt ON true
WHERE run.run_hash = (
    SELECT recent.run_hash FROM finance.incremental_runs recent
    WHERE recent.scope_id=q.scope_id AND EXISTS (
        SELECT 1 FROM finance.incremental_qualifications present WHERE present.run_hash=recent.run_hash
    )
    ORDER BY (SELECT max(event_number) FROM finance.incremental_run_events e
              WHERE e.run_hash=recent.run_hash) DESC NULLS LAST, recent.processed_at DESC LIMIT 1
);

CREATE VIEW finance_read.incremental_source_history AS
SELECT version.scope_id, version.source_id, version.version_id, version.version_number,
       version.economic_hash, version.first_snapshot_hash,
       version.observation_document->>'source_day' AS source_day,
       version.observation_document->>'signed_amount' AS signed_amount,
       version.observation_document->>'currency' AS currency_code,
       version.observation_document->>'description' AS description,
       version.observation_document->>'status' AS source_status,
       version.observation_document->>'observed_at' AS financial_observed_at,
       sighting.last_sighted_at, version.admitted, version.reason, version.currency_proof
FROM finance.incremental_source_versions version
LEFT JOIN LATERAL (
    SELECT max(s.observed_at) AS last_sighted_at
    FROM finance.incremental_source_sightings s WHERE s.version_id=version.version_id
) sighting ON true;

GRANT SELECT, INSERT ON
    finance.incremental_scopes, finance.incremental_source_versions,
    finance.incremental_source_sightings, finance.incremental_runs,
    finance.incremental_qualifications, finance.incremental_activity_bindings,
    finance.incremental_outbox, finance.incremental_attempts, finance.incremental_run_events,
    finance.incremental_projection_observations
TO finance_shadow_ingest;
GRANT USAGE, SELECT ON SEQUENCE finance.incremental_attempts_event_number_seq,
    finance.incremental_run_events_event_number_seq,
    finance.incremental_projection_observations_observation_number_seq TO finance_shadow_ingest;
GRANT SELECT ON finance_read.incremental_scope_status, finance_read.incremental_cash_events,
    finance_read.incremental_source_history
TO finance_readonly, finance_shadow_ingest;
GRANT SELECT ON finance.incremental_scopes, finance.incremental_source_versions,
    finance.incremental_source_sightings, finance.incremental_runs,
    finance.incremental_qualifications, finance.incremental_activity_bindings,
    finance.incremental_outbox, finance.incremental_attempts, finance.incremental_run_events,
    finance.incremental_projection_observations
TO finance_shadow_backup;

INSERT INTO finance.schema_migrations(version, name, checksum)
VALUES (:'migration_version', :'migration_name', :'migration_checksum')
ON CONFLICT (version) DO NOTHING;
COMMIT;
