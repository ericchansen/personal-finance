BEGIN;

CREATE TABLE finance.incremental_source_anchors (
    anchor_hash text PRIMARY KEY CHECK (anchor_hash ~ '^[0-9a-f]{64}$'),
    scope_id uuid NOT NULL UNIQUE REFERENCES finance.incremental_scopes,
    source_snapshot_hash text NOT NULL CHECK (source_snapshot_hash ~ '^[0-9a-f]{64}$'),
    source_account_state_hash text NOT NULL CHECK (source_account_state_hash ~ '^[0-9a-f]{64}$'),
    source_transaction_state_hash text NOT NULL CHECK (source_transaction_state_hash ~ '^[0-9a-f]{64}$'),
    source_balance numeric(24,8) NOT NULL,
    currency_code text NOT NULL CHECK (currency_code ~ '^[A-Z]{3}$'),
    assertion_activity_id text NOT NULL,
    anchor_document jsonb NOT NULL,
    UNIQUE (anchor_hash,scope_id)
);
CREATE TABLE finance.incremental_anchor_checkpoints (
    checkpoint_hash text PRIMARY KEY CHECK (checkpoint_hash ~ '^[0-9a-f]{64}$'),
    checkpoint_number bigint GENERATED ALWAYS AS IDENTITY UNIQUE,
    anchor_hash text NOT NULL,
    scope_id uuid NOT NULL,
    previous_checkpoint_hash text UNIQUE REFERENCES finance.incremental_anchor_checkpoints,
    run_hash text REFERENCES finance.incremental_runs,
    receipt_hash text CHECK (receipt_hash ~ '^[0-9a-f]{64}$'),
    source_snapshot_hash text NOT NULL CHECK (source_snapshot_hash ~ '^[0-9a-f]{64}$'),
    source_state jsonb NOT NULL CHECK (jsonb_typeof(source_state)='object'),
    source_state_hash text NOT NULL CHECK (source_state_hash ~ '^[0-9a-f]{64}$'),
    source_balance numeric(24,8) NOT NULL,
    observed_posted_watermark date,
    projection_observation_cursor bigint NOT NULL CHECK (projection_observation_cursor >= 0),
    app_financial_state_hash text NOT NULL CHECK (app_financial_state_hash ~ '^[0-9a-f]{64}$'),
    processed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY (anchor_hash,scope_id) REFERENCES finance.incremental_source_anchors(anchor_hash,scope_id),
    UNIQUE (checkpoint_hash,anchor_hash,scope_id),
    FOREIGN KEY (previous_checkpoint_hash,anchor_hash,scope_id)
        REFERENCES finance.incremental_anchor_checkpoints(checkpoint_hash,anchor_hash,scope_id),
    FOREIGN KEY (run_hash,scope_id) REFERENCES finance.incremental_runs(run_hash,scope_id),
    CHECK ((previous_checkpoint_hash IS NULL) = (run_hash IS NULL))
);
CREATE UNIQUE INDEX incremental_anchor_one_origin
ON finance.incremental_anchor_checkpoints(anchor_hash) WHERE previous_checkpoint_hash IS NULL;

CREATE TABLE finance.incremental_anchor_adoptions (
    adoption_id uuid PRIMARY KEY,
    scope_id uuid NOT NULL,
    accepted_event_id uuid NOT NULL,
    activity_id text NOT NULL,
    run_hash text NOT NULL REFERENCES finance.incremental_runs,
    anchor_hash text NOT NULL,
    checkpoint_hash text NOT NULL,
    source_id text NOT NULL,
    observed_activity jsonb NOT NULL,
    processed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY (scope_id,accepted_event_id,activity_id)
        REFERENCES finance.incremental_activity_bindings(scope_id,accepted_event_id,activity_id),
    FOREIGN KEY (checkpoint_hash,anchor_hash,scope_id)
        REFERENCES finance.incremental_anchor_checkpoints(checkpoint_hash,anchor_hash,scope_id),
    FOREIGN KEY (run_hash,scope_id) REFERENCES finance.incremental_runs(run_hash,scope_id),
    UNIQUE (scope_id,accepted_event_id)
);

CREATE FUNCTION finance.validate_incremental_anchor_adoption()
RETURNS trigger LANGUAGE plpgsql AS $function$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM finance.incremental_anchor_checkpoints checkpoint
        JOIN finance.incremental_scopes scope ON scope.scope_id=checkpoint.scope_id
        WHERE checkpoint.checkpoint_hash=NEW.checkpoint_hash AND checkpoint.anchor_hash=NEW.anchor_hash
          AND scope.scope_id=NEW.scope_id
          AND checkpoint.source_state->NEW.source_id->>'status'='posted'
          AND NEW.observed_activity->>'status'='POSTED'
          AND NEW.observed_activity->>'id'=NEW.activity_id
          AND NEW.observed_activity->>'accountId'=scope.activity_account_id
          AND NEW.observed_activity->>'currency'=checkpoint.source_state->NEW.source_id->>'currency'
          AND left(NEW.observed_activity->>'date',10)=checkpoint.source_state->NEW.source_id->>'sourceDay'
          AND CASE
              WHEN NEW.observed_activity->>'activityType' IN ('DEPOSIT','CREDIT','INTEREST','DIVIDEND')
                  THEN abs((NEW.observed_activity->>'amount')::numeric)
              WHEN NEW.observed_activity->>'activityType' IN ('WITHDRAWAL','FEE','TAX','EXPENSE')
                  THEN -abs((NEW.observed_activity->>'amount')::numeric)
              ELSE NULL
          END = (checkpoint.source_state->NEW.source_id->>'amount')::numeric
          AND EXISTS (
              SELECT 1 FROM finance.incremental_qualifications q
              WHERE q.run_hash=NEW.run_hash AND q.scope_id=NEW.scope_id
                AND q.accepted_event_id=NEW.accepted_event_id
                AND q.proof->'sourceIds'=jsonb_build_array(NEW.source_id)
          )
    ) THEN
        RAISE EXCEPTION 'anchor adoption requires exact source and app state'
            USING ERRCODE='23514';
    END IF;
    RETURN NEW;
END;
$function$;
CREATE TRIGGER incremental_anchor_adoption_validate
BEFORE INSERT ON finance.incremental_anchor_adoptions
FOR EACH ROW EXECUTE FUNCTION finance.validate_incremental_anchor_adoption();

ALTER TABLE finance.incremental_projection_observations
    ADD COLUMN representation_kind text NOT NULL DEFAULT 'full'
        CHECK (representation_kind IN ('full','source-delta')),
    ADD COLUMN base_source_amount numeric(24,8) NOT NULL DEFAULT 0,
    ADD COLUMN anchor_hash text REFERENCES finance.incremental_source_anchors,
    ADD COLUMN anchor_checkpoint_hash text REFERENCES finance.incremental_anchor_checkpoints,
    ADD COLUMN anchor_source_id text,
    ADD CONSTRAINT incremental_projection_representation_check CHECK (
        (representation_kind='full' AND base_source_amount=0)
        OR (representation_kind='source-delta' AND anchor_hash IS NOT NULL
            AND anchor_checkpoint_hash IS NOT NULL AND anchor_source_id IS NOT NULL)
    );

CREATE OR REPLACE FUNCTION finance.validate_incremental_projection_observation()
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
          END = event.signed_amount - NEW.base_source_amount
          AND (NEW.representation_kind='full' OR EXISTS (
              SELECT 1 FROM finance.incremental_anchor_checkpoints checkpoint
              WHERE checkpoint.checkpoint_hash=NEW.anchor_checkpoint_hash
                AND checkpoint.anchor_hash=NEW.anchor_hash AND checkpoint.scope_id=NEW.scope_id
                AND checkpoint.source_state->NEW.anchor_source_id->>'status'='posted'
                AND (checkpoint.source_state->NEW.anchor_source_id->>'amount')::numeric=NEW.base_source_amount
                AND checkpoint.source_state->NEW.anchor_source_id->>'currency'=event.currency_code
          ))
          AND (
              (NEW.source_revision_number IS NOT NULL AND EXISTS (
                  SELECT 1 FROM finance.accepted_identity_event_mappings mapping
                  WHERE mapping.generation_event_id=NEW.source_generation_event_id
                    AND mapping.accepted_event_id=NEW.accepted_event_id
                    AND mapping.revision_number=NEW.source_revision_number
              ))
              OR (NEW.source_revision_number IS NULL AND EXISTS (
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
                AND (NEW.representation_kind='full'
                     OR q.proof->'sourceIds' = jsonb_build_array(NEW.anchor_source_id))
          ))
    ) THEN
        RAISE EXCEPTION 'projection observation requires its exact source/cash representation'
            USING ERRCODE='23514';
    END IF;
    RETURN NEW;
END;
$function$;

DO $do$
DECLARE name text;
BEGIN
    FOREACH name IN ARRAY ARRAY['incremental_source_anchors','incremental_anchor_checkpoints','incremental_anchor_adoptions']
    LOOP
        EXECUTE format('CREATE TRIGGER %I BEFORE UPDATE OR DELETE ON finance.%I '
            'FOR EACH ROW EXECUTE FUNCTION finance.reject_append_only_change()',name || '_append_only',name);
    END LOOP;
END;
$do$;
CREATE VIEW finance_read.incremental_source_anchor_status AS
SELECT anchor.scope_id,anchor.anchor_hash,anchor.source_snapshot_hash AS initial_source_snapshot_hash,
       anchor.source_account_state_hash,anchor.source_transaction_state_hash,
       anchor.source_balance AS initial_anchor_balance,anchor.currency_code,anchor.assertion_activity_id,
       (anchor.anchor_document->'source'->>'sourceObservedAt')::timestamptz AS initial_source_observed_at,
       (anchor.anchor_document->'source'->>'balanceObservedAt')::timestamptz AS initial_balance_observed_at,
       checkpoint.checkpoint_hash,checkpoint.source_snapshot_hash,checkpoint.source_balance,
       checkpoint.observed_posted_watermark,checkpoint.receipt_hash,checkpoint.run_hash,checkpoint.processed_at,
       run.source_observed_at AS checkpoint_collection_observed_at,
       run.balance_effective_at AS checkpoint_balance_observed_at
FROM finance.incremental_source_anchors anchor
LEFT JOIN LATERAL (
    SELECT * FROM finance.incremental_anchor_checkpoints c WHERE c.anchor_hash=anchor.anchor_hash
    ORDER BY checkpoint_number DESC LIMIT 1
) checkpoint ON true
LEFT JOIN finance.incremental_runs run ON run.run_hash=checkpoint.run_hash;
CREATE VIEW finance_read.incremental_cash_representations AS
SELECT DISTINCT ON (observation.scope_id,observation.accepted_event_id)
       observation.scope_id,observation.accepted_event_id,observation.activity_id,
       observation.source_revision_number,observation.source_generation_event_id,
       observation.representation_kind,observation.base_source_amount,
       event.signed_amount AS source_amount,
       event.signed_amount-observation.base_source_amount AS represented_cash_amount,
       observation.anchor_hash,observation.anchor_checkpoint_hash,observation.anchor_source_id,
       observation.observed_activity,observation.processed_at
FROM finance.incremental_projection_observations observation
JOIN finance.canonical_identity_events event ON event.canonical_identity_event_id=observation.source_generation_event_id
ORDER BY observation.scope_id,observation.accepted_event_id,observation.observation_number DESC;

GRANT SELECT,INSERT ON finance.incremental_source_anchors,finance.incremental_anchor_checkpoints,
    finance.incremental_anchor_adoptions TO finance_shadow_ingest;
GRANT USAGE,SELECT ON SEQUENCE finance.incremental_anchor_checkpoints_checkpoint_number_seq TO finance_shadow_ingest;
GRANT SELECT ON finance_read.incremental_source_anchor_status,finance_read.incremental_cash_representations
TO finance_readonly,finance_shadow_ingest;
GRANT SELECT ON finance.incremental_source_anchors,finance.incremental_anchor_checkpoints,finance.incremental_anchor_adoptions
TO finance_shadow_backup;
INSERT INTO finance.schema_migrations(version,name,checksum)
VALUES (:'migration_version',:'migration_name',:'migration_checksum')
ON CONFLICT (version) DO NOTHING;
COMMIT;
