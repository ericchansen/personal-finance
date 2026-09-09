BEGIN;

-- A policy document includes reviewed coverage intervals. Distinct scopes can
-- therefore share semantic versions while retaining different immutable hashes.
-- Existing policy ids, hashes, documents and composite foreign keys stay intact.
ALTER TABLE finance.canonical_identity_policies DROP CONSTRAINT
    canonical_identity_policies_policy_name_policy_version_key;

-- Resolver generations remain immutable evidence. Acceptance is a separate,
-- explicitly selected chain, not "the highest policy rank" or a second ledger.
CREATE TABLE finance.accepted_identity_generations (
    generation_id uuid PRIMARY KEY REFERENCES
        finance.canonical_identity_policy_generations(
            canonical_identity_policy_generation_id
        ),
    acceptance_number bigint NOT NULL UNIQUE CHECK (acceptance_number > 0),
    previous_generation_id uuid UNIQUE REFERENCES
        finance.accepted_identity_generations(generation_id),
    contract_version text NOT NULL CHECK (contract_version = 'accepted-identity-v1'),
    processed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK ((acceptance_number = 1) = (previous_generation_id IS NULL)),
    CHECK (generation_id IS DISTINCT FROM previous_generation_id)
);
CREATE UNIQUE INDEX accepted_identity_one_root_idx
    ON finance.accepted_identity_generations ((true))
    WHERE previous_generation_id IS NULL;

ALTER TABLE finance.canonical_identity_source_claims
    ADD CONSTRAINT accepted_identity_claim_reference_key UNIQUE (
        canonical_identity_source_claim_id, claim_hash, canonical_account_hash
    );
ALTER TABLE finance.canonical_identity_events
    ADD CONSTRAINT accepted_identity_event_account_key UNIQUE (
        canonical_identity_event_id, canonical_account_hash
    );

CREATE TABLE finance.accepted_identity_events (
    accepted_event_id uuid PRIMARY KEY,
    canonical_account_hash text NOT NULL CHECK (
        canonical_account_hash ~ '^[0-9a-f]{64}$'
    ),
    created_generation_id uuid NOT NULL REFERENCES
        finance.accepted_identity_generations(generation_id),
    processed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (accepted_event_id, canonical_account_hash)
);

CREATE TABLE finance.accepted_identity_claims (
    claim_hash text PRIMARY KEY CHECK (claim_hash ~ '^[0-9a-f]{64}$'),
    accepted_event_id uuid NOT NULL,
    canonical_account_hash text NOT NULL,
    source_canonical_account_hash text NOT NULL,
    source_claim_id uuid NOT NULL,
    FOREIGN KEY (accepted_event_id, canonical_account_hash) REFERENCES
        finance.accepted_identity_events(accepted_event_id, canonical_account_hash),
    FOREIGN KEY (source_claim_id, claim_hash, source_canonical_account_hash) REFERENCES
        finance.canonical_identity_source_claims(
            canonical_identity_source_claim_id, claim_hash, canonical_account_hash
        )
);

CREATE TABLE finance.accepted_identity_revisions (
    accepted_event_id uuid NOT NULL,
    revision_number bigint NOT NULL CHECK (revision_number > 0),
    revision_hash text NOT NULL CHECK (revision_hash ~ '^[0-9a-f]{64}$'),
    generation_id uuid NOT NULL REFERENCES
        finance.accepted_identity_generations(generation_id),
    generation_event_id uuid NOT NULL,
    canonical_account_hash text NOT NULL,
    processed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (accepted_event_id, revision_number),
    UNIQUE (accepted_event_id, revision_number, revision_hash),
    UNIQUE (generation_id, accepted_event_id),
    FOREIGN KEY (accepted_event_id, canonical_account_hash) REFERENCES
        finance.accepted_identity_events(accepted_event_id, canonical_account_hash),
    FOREIGN KEY (generation_event_id, canonical_account_hash) REFERENCES
        finance.canonical_identity_events(
            canonical_identity_event_id, canonical_account_hash
        ),
    FOREIGN KEY (generation_id, generation_event_id) REFERENCES
        finance.canonical_identity_events(
            canonical_identity_policy_generation_id, canonical_identity_event_id
        )
);

CREATE TABLE finance.accepted_identity_event_mappings (
    generation_id uuid NOT NULL REFERENCES
        finance.accepted_identity_generations(generation_id),
    generation_event_id uuid PRIMARY KEY,
    canonical_account_hash text NOT NULL,
    accepted_event_id uuid,
    revision_number bigint,
    revision_hash text,
    outcome text NOT NULL CHECK (outcome IN ('created', 'continued', 'conflict')),
    prior_event_ids uuid[] NOT NULL,
    conflict_reasons text[] NOT NULL,
    projection_binding_ids uuid[] NOT NULL,
    review_decision_hashes text[] NOT NULL CHECK (
        finance.jsonb_sha256_array(to_jsonb(review_decision_hashes))
    ),
    CHECK (cardinality(review_decision_hashes) = 0 OR outcome = 'conflict'),
    FOREIGN KEY (generation_id, generation_event_id) REFERENCES
        finance.canonical_identity_events(
            canonical_identity_policy_generation_id, canonical_identity_event_id
        ),
    FOREIGN KEY (generation_event_id, canonical_account_hash) REFERENCES
        finance.canonical_identity_events(
            canonical_identity_event_id, canonical_account_hash
        ),
    FOREIGN KEY (accepted_event_id, canonical_account_hash) REFERENCES
        finance.accepted_identity_events(accepted_event_id, canonical_account_hash),
    FOREIGN KEY (accepted_event_id, revision_number, revision_hash) REFERENCES
        finance.accepted_identity_revisions(
            accepted_event_id, revision_number, revision_hash
        ),
    UNIQUE (generation_id, accepted_event_id),
    CHECK (
        outcome = 'conflict'
        OR (outcome = 'created' AND cardinality(prior_event_ids) = 0)
        OR (outcome = 'continued' AND prior_event_ids = ARRAY[accepted_event_id])
    ),
    CHECK (
        (outcome = 'conflict' AND accepted_event_id IS NULL
            AND revision_number IS NULL AND revision_hash IS NULL
            AND cardinality(conflict_reasons) > 0)
        OR
        (outcome <> 'conflict' AND accepted_event_id IS NOT NULL
            AND revision_number IS NOT NULL AND revision_hash IS NOT NULL
            AND cardinality(conflict_reasons) = 0)
    )
);

CREATE FUNCTION finance.validate_accepted_identity_sequence()
RETURNS trigger LANGUAGE plpgsql AS $function$
BEGIN
    IF TG_TABLE_NAME = 'accepted_identity_generations' THEN
        IF NEW.previous_generation_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM finance.accepted_identity_generations prior
            WHERE prior.generation_id = NEW.previous_generation_id
              AND prior.acceptance_number + 1 = NEW.acceptance_number
        ) THEN
            RAISE EXCEPTION 'acceptance must directly follow its predecessor'
                USING ERRCODE = '23514';
        END IF;
    ELSE
        IF NEW.revision_number <> (
            SELECT COALESCE(max(revision_number), 0) + 1
            FROM finance.accepted_identity_revisions
            WHERE accepted_event_id = NEW.accepted_event_id
        ) THEN
            RAISE EXCEPTION 'accepted revision must directly follow its predecessor'
                USING ERRCODE = '23514';
        END IF;
    END IF;
    RETURN NEW;
END;
$function$;
CREATE TRIGGER accepted_identity_generation_sequence
BEFORE INSERT ON finance.accepted_identity_generations
FOR EACH ROW EXECUTE FUNCTION finance.validate_accepted_identity_sequence();
CREATE TRIGGER accepted_identity_revision_sequence
BEFORE INSERT ON finance.accepted_identity_revisions
FOR EACH ROW EXECUTE FUNCTION finance.validate_accepted_identity_sequence();

-- Keep the original binding and its provenance. A new generation never needs
-- a second active binding for the same app activity.
ALTER TABLE finance.application_projection_bindings
    ADD CONSTRAINT accepted_identity_binding_reference_key UNIQUE (
        application_projection_binding_id, target_application, target_activity_hash
    );
CREATE TABLE finance.accepted_identity_projection_links (
    accepted_event_id uuid NOT NULL REFERENCES
        finance.accepted_identity_events(accepted_event_id),
    target_application text NOT NULL,
    target_activity_hash text NOT NULL,
    application_projection_binding_id uuid NOT NULL UNIQUE,
    PRIMARY KEY (accepted_event_id, target_application),
    UNIQUE (target_application, target_activity_hash),
    FOREIGN KEY (
        application_projection_binding_id, target_application, target_activity_hash
    ) REFERENCES finance.application_projection_bindings(
        application_projection_binding_id, target_application, target_activity_hash
    )
);

CREATE FUNCTION finance.validate_accepted_projection_link()
RETURNS trigger LANGUAGE plpgsql AS $function$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM finance.application_projection_bindings binding
        JOIN finance.canonical_identity_events event
          ON event.canonical_identity_event_id = binding.canonical_identity_event_id
        JOIN finance.accepted_identity_events accepted
          ON accepted.accepted_event_id = NEW.accepted_event_id
         AND accepted.canonical_account_hash = event.canonical_account_hash
        WHERE binding.application_projection_binding_id
                = NEW.application_projection_binding_id
          AND binding.is_active
          AND EXISTS (
              SELECT 1 FROM finance.canonical_identity_event_members member
              WHERE member.canonical_identity_event_id = event.canonical_identity_event_id
                AND member.member_type = 'source_claim'
          )
          AND NOT EXISTS (
              SELECT 1
              FROM finance.canonical_identity_event_members member
              JOIN finance.canonical_identity_source_claims claim
                ON claim.canonical_identity_source_claim_id
                    = member.canonical_identity_source_claim_id
              LEFT JOIN finance.accepted_identity_claims owned
                ON owned.claim_hash = claim.claim_hash
               AND owned.accepted_event_id = NEW.accepted_event_id
              WHERE member.canonical_identity_event_id = event.canonical_identity_event_id
                AND owned.claim_hash IS NULL
          )
    ) THEN
        RAISE EXCEPTION 'accepted projection requires unambiguous owned source claims'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$function$;
CREATE TRIGGER accepted_projection_link_validate
BEFORE INSERT ON finance.accepted_identity_projection_links
FOR EACH ROW EXECUTE FUNCTION finance.validate_accepted_projection_link();

DO $do$
DECLARE
    protected_table text;
BEGIN
    FOREACH protected_table IN ARRAY ARRAY[
        'accepted_identity_generations', 'accepted_identity_events',
        'accepted_identity_claims', 'accepted_identity_revisions',
        'accepted_identity_event_mappings', 'accepted_identity_projection_links'
    ] LOOP
        EXECUTE format(
            'CREATE TRIGGER %I BEFORE UPDATE OR DELETE ON finance.%I '
            'FOR EACH ROW EXECUTE FUNCTION finance.reject_append_only_change()',
            protected_table || '_append_only', protected_table
        );
    END LOOP;
END;
$do$;

CREATE VIEW finance_read.accepted_identity_current AS
WITH head AS (
    SELECT generation_id FROM finance.accepted_identity_generations
    ORDER BY acceptance_number DESC LIMIT 1
), dispositions AS (
    SELECT mapping.accepted_event_id, mapping.generation_id AS disposition_generation_id,
           selection.acceptance_number, 'qualified'::text AS disposition_status,
           mapping.revision_number, mapping.revision_hash,
           mapping.generation_id AS source_generation_id,
           mapping.generation_event_id AS source_generation_event_id,
           mapping.conflict_reasons,
           ARRAY[mapping.generation_event_id] AS disposition_event_ids
    FROM finance.accepted_identity_event_mappings mapping
    JOIN finance.accepted_identity_generations selection
      ON selection.generation_id = mapping.generation_id
    WHERE mapping.outcome <> 'conflict'
    UNION ALL
    SELECT prior.accepted_event_id, mapping.generation_id,
           selection.acceptance_number, 'conflict'::text,
           NULL::bigint, NULL::text, NULL::uuid, NULL::uuid,
           array_agg(DISTINCT reason.value ORDER BY reason.value) AS conflict_reasons,
           array_agg(DISTINCT mapping.generation_event_id
               ORDER BY mapping.generation_event_id) AS disposition_event_ids
    FROM finance.accepted_identity_event_mappings mapping
    JOIN finance.accepted_identity_generations selection
      ON selection.generation_id = mapping.generation_id
    CROSS JOIN LATERAL unnest(mapping.prior_event_ids) prior(accepted_event_id)
    CROSS JOIN LATERAL unnest(mapping.conflict_reasons) reason(value)
    WHERE mapping.outcome = 'conflict'
    GROUP BY prior.accepted_event_id, mapping.generation_id, selection.acceptance_number
), latest_dispositions AS (
    -- Omission is not a disposition. In particular it cannot erase an earlier
    -- exclusion/conflict and expose the last numerically highest revision.
    SELECT DISTINCT ON (accepted_event_id) *
    FROM dispositions
    ORDER BY accepted_event_id, acceptance_number DESC,
             (disposition_status = 'conflict') DESC
), current_revisions AS (
    SELECT latest.accepted_event_id,
           COALESCE(latest.revision_number, revision.revision_number) AS revision_number,
           COALESCE(latest.revision_hash, revision.revision_hash) AS revision_hash,
           COALESCE(latest.source_generation_id, revision.generation_id) AS generation_id,
           COALESCE(latest.source_generation_event_id,
                    revision.generation_event_id) AS generation_event_id,
           latest.disposition_generation_id, latest.disposition_status,
           latest.conflict_reasons, latest.disposition_event_ids,
           CASE WHEN latest.disposition_generation_id <> head.generation_id
                    THEN 'not-observed-in-current-selection'
                WHEN latest.disposition_status = 'conflict' THEN 'retained-prior'
                ELSE 'selected' END AS selection_status,
           CASE WHEN latest.disposition_generation_id = head.generation_id
                    THEN latest.disposition_event_ids
                ELSE ARRAY[]::uuid[] END AS current_candidate_event_ids
    FROM latest_dispositions latest
    CROSS JOIN head
    LEFT JOIN LATERAL (
        SELECT stored.* FROM finance.accepted_identity_revisions stored
        WHERE stored.accepted_event_id = latest.accepted_event_id
          AND latest.disposition_status = 'conflict'
        ORDER BY stored.revision_number DESC LIMIT 1
    ) revision ON true
    WHERE NOT (latest.conflict_reasons && ARRAY[
        'source-excluded-or-untrusted', 'account-state-unknown', 'account-ownership-change'
    ]::text[])
)
SELECT mapping.accepted_event_id, mapping.revision_number, mapping.revision_hash,
       generation.generation_hash, generation.policy_hash, generation.policy_version,
       mapping.generation_event_id, event.canonical_id,
       event.canonical_account_hash, event.source_day, event.signed_amount,
       event.currency_code, event.status, event.trusted,
       event.selected_observation_hash, event.description_hash, event.category_hash,
       projection.target_application, projection.target_activity_hash,
       projection.application_projection_binding_id,
       binding.is_active AS projection_binding_active,
       selection_generation.generation_hash AS selection_generation_hash,
       mapping.selection_status, mapping.conflict_reasons,
       mapping.current_candidate_event_ids,
       CASE WHEN mapping.disposition_status = 'conflict' THEN 'needs-review'
            ELSE 'no-open-duplicate-decision' END AS identity_status,
       'not-evaluated'::text AS source_admission_status,
       'resolver-supplied-unverified'::text AS currency_evidence_status,
       false AS is_economically_certified,
       disposition_generation.generation_hash AS disposition_generation_hash,
       mapping.disposition_status, mapping.disposition_event_ids
FROM current_revisions mapping
CROSS JOIN head
JOIN finance.canonical_identity_policy_generations selection_generation
  ON selection_generation.canonical_identity_policy_generation_id = head.generation_id
JOIN finance.canonical_identity_policy_generations generation
  ON generation.canonical_identity_policy_generation_id = mapping.generation_id
JOIN finance.canonical_identity_policy_generations disposition_generation
  ON disposition_generation.canonical_identity_policy_generation_id
        = mapping.disposition_generation_id
JOIN finance.canonical_identity_events event
  ON event.canonical_identity_event_id = mapping.generation_event_id
LEFT JOIN finance.accepted_identity_projection_links projection
  ON projection.accepted_event_id = mapping.accepted_event_id
LEFT JOIN finance.application_projection_bindings binding
  ON binding.application_projection_binding_id
        = projection.application_projection_binding_id;

CREATE VIEW finance_read.accepted_identity_history AS
SELECT generation.generation_hash, generation.policy_hash, selection.acceptance_number,
       previous.generation_hash AS previous_generation_hash,
       event.canonical_id, mapping.*
FROM finance.accepted_identity_event_mappings mapping
JOIN finance.accepted_identity_generations selection
  ON selection.generation_id = mapping.generation_id
JOIN finance.canonical_identity_policy_generations generation
  ON generation.canonical_identity_policy_generation_id = mapping.generation_id
LEFT JOIN finance.canonical_identity_policy_generations previous
  ON previous.canonical_identity_policy_generation_id = selection.previous_generation_id
JOIN finance.canonical_identity_events event
  ON event.canonical_identity_event_id = mapping.generation_event_id;

GRANT SELECT, INSERT ON
    finance.accepted_identity_generations, finance.accepted_identity_events,
    finance.accepted_identity_claims, finance.accepted_identity_revisions,
    finance.accepted_identity_event_mappings, finance.accepted_identity_projection_links
TO finance_shadow_ingest;
GRANT SELECT ON finance_read.accepted_identity_current,
    finance_read.accepted_identity_history TO finance_readonly, finance_shadow_ingest;
GRANT SELECT ON
    finance.accepted_identity_generations, finance.accepted_identity_events,
    finance.accepted_identity_claims, finance.accepted_identity_revisions,
    finance.accepted_identity_event_mappings, finance.accepted_identity_projection_links
TO finance_shadow_backup;

INSERT INTO finance.schema_migrations(version, name, checksum)
VALUES (:'migration_version', :'migration_name', :'migration_checksum')
ON CONFLICT (version) DO NOTHING;

COMMIT;
