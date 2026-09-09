BEGIN;

CREATE OR REPLACE FUNCTION finance.jsonb_sha256_array(candidate jsonb)
RETURNS boolean
LANGUAGE sql
IMMUTABLE
AS $function$
    SELECT CASE
        WHEN candidate IS NULL OR jsonb_typeof(candidate) <> 'array' THEN false
        ELSE NOT EXISTS (
            SELECT 1
            FROM jsonb_array_elements_text(candidate) AS item(value)
            WHERE item.value !~ '^[0-9a-f]{64}$'
        )
    END;
$function$;

CREATE OR REPLACE FUNCTION finance.valid_competing_candidate_proof(candidate jsonb)
RETURNS boolean
LANGUAGE sql
IMMUTABLE
AS $function$
    SELECT CASE
        WHEN candidate IS NULL OR jsonb_typeof(candidate) <> 'object' THEN false
        WHEN candidate = '{}'::jsonb THEN true
        WHEN (
                SELECT count(*)
                FROM jsonb_object_keys(candidate) AS object_key(key)
             ) = 2
             AND candidate ? 'competingCandidateCount'
             AND candidate ? 'sourceClaimCount'
             AND NOT EXISTS (
                 SELECT 1
                 FROM jsonb_each(candidate) AS entry(key, value)
                 WHERE jsonb_typeof(entry.value) <> 'number'
                    OR entry.value::text !~ '^[0-9]+$'
             )
        THEN true
        WHEN (
                SELECT count(*)
                FROM jsonb_object_keys(candidate) AS object_key(key)
             ) = 4
             AND candidate ? 'leftDegree'
             AND candidate ? 'rightDegree'
             AND candidate ? 'componentSize'
             AND candidate ? 'competingCandidateCount'
             AND NOT EXISTS (
                 SELECT 1
                 FROM jsonb_each(candidate) AS entry(key, value)
                 WHERE jsonb_typeof(entry.value) <> 'number'
                    OR entry.value::text !~ '^[0-9]+$'
             )
        THEN true
        WHEN (
                SELECT count(*)
                FROM jsonb_object_keys(candidate) AS object_key(key)
             ) > 0
             AND NOT EXISTS (
                 SELECT 1
                 FROM jsonb_each(candidate) AS entry(key, value)
                 WHERE entry.key !~ '^degree:[0-9a-f]{64}$'
                    OR jsonb_typeof(entry.value) <> 'number'
                    OR entry.value::text !~ '^[1-9][0-9]*$'
             )
        THEN true
        ELSE false
    END;
$function$;

CREATE TABLE IF NOT EXISTS finance.canonical_identity_policies (
    canonical_identity_policy_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    policy_name text NOT NULL CHECK (btrim(policy_name) <> ''),
    policy_version text NOT NULL CHECK (btrim(policy_version) <> ''),
    policy_hash text NOT NULL CHECK (policy_hash ~ '^[0-9a-f]{64}$'),
    policy_family text NOT NULL DEFAULT 'canonical_transaction_identity'
        CHECK (policy_family = 'canonical_transaction_identity'),
    policy_document jsonb NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(policy_document) = 'object'),
    observed_at timestamptz NOT NULL,
    processed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (policy_name, policy_version),
    UNIQUE (policy_hash),
    UNIQUE (
        canonical_identity_policy_id, policy_version, policy_hash
    ),
    CHECK (processed_at >= observed_at)
);

CREATE TABLE IF NOT EXISTS finance.canonical_identity_policy_generations (
    canonical_identity_policy_generation_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    canonical_identity_policy_id uuid NOT NULL,
    policy_version text NOT NULL CHECK (btrim(policy_version) <> ''),
    policy_hash text NOT NULL CHECK (policy_hash ~ '^[0-9a-f]{64}$'),
    generation_number bigint NOT NULL CHECK (generation_number > 0),
    generation_label text NOT NULL CHECK (btrim(generation_label) <> ''),
    generation_hash text NOT NULL CHECK (generation_hash ~ '^[0-9a-f]{64}$'),
    input_hash text NOT NULL CHECK (input_hash ~ '^[0-9a-f]{64}$'),
    canonical_state_hash text NOT NULL
        CHECK (canonical_state_hash ~ '^[0-9a-f]{64}$'),
    feature_schema_hash text NOT NULL
        CHECK (feature_schema_hash ~ '^[0-9a-f]{64}$'),
    observed_at timestamptz NOT NULL,
    processed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (canonical_identity_policy_id, generation_number),
    UNIQUE (generation_hash),
    UNIQUE (
        canonical_identity_policy_generation_id,
        canonical_identity_policy_id,
        policy_version,
        policy_hash
    ),
    UNIQUE (canonical_identity_policy_id, input_hash, generation_hash),
    FOREIGN KEY (
        canonical_identity_policy_id, policy_version, policy_hash
    ) REFERENCES finance.canonical_identity_policies(
        canonical_identity_policy_id, policy_version, policy_hash
    ),
    CHECK (processed_at >= observed_at)
);

CREATE TABLE IF NOT EXISTS finance.canonical_identity_source_claims (
    canonical_identity_source_claim_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    canonical_identity_policy_generation_id uuid NOT NULL REFERENCES
        finance.canonical_identity_policy_generations(
            canonical_identity_policy_generation_id
        ),
    claim_hash text NOT NULL CHECK (claim_hash ~ '^[0-9a-f]{64}$'),
    source_family text NOT NULL CHECK (btrim(source_family) <> ''),
    canonical_account_hash text NOT NULL
        CHECK (canonical_account_hash ~ '^[0-9a-f]{64}$'),
    source_account_hash text NOT NULL
        CHECK (source_account_hash ~ '^[0-9a-f]{64}$'),
    source_connection_hash text NOT NULL
        CHECK (source_connection_hash ~ '^[0-9a-f]{64}$'),
    provider_identity_hash text
        CHECK (
            provider_identity_hash IS NULL
            OR provider_identity_hash ~ '^[0-9a-f]{64}$'
        ),
    provider_id_kind text NOT NULL CHECK (provider_id_kind IN (
        'none', 'synthetic', 'scoped-provider-id', 'simplefin-id', 'ofx-fitid'
    )),
    selected_observation_hash text NOT NULL
        CHECK (selected_observation_hash ~ '^[0-9a-f]{64}$'),
    source_hashes jsonb NOT NULL CHECK (
        finance.jsonb_sha256_array(source_hashes)
        AND jsonb_array_length(source_hashes) > 0
    ),
    observed_at timestamptz NOT NULL,
    processed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (
        canonical_identity_policy_generation_id,
        canonical_identity_source_claim_id
    ),
    UNIQUE (canonical_identity_policy_generation_id, claim_hash),
    UNIQUE (
        canonical_identity_policy_generation_id,
        source_family,
        source_connection_hash,
        source_account_hash,
        claim_hash
    ),
    CHECK (processed_at >= observed_at)
);

CREATE TABLE IF NOT EXISTS finance.canonical_identity_observation_memberships (
    canonical_identity_observation_membership_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    canonical_identity_policy_generation_id uuid NOT NULL,
    canonical_identity_source_claim_id uuid NOT NULL,
    transaction_observation_id uuid NOT NULL REFERENCES
        finance.transaction_observations(transaction_observation_id),
    membership_hash text NOT NULL CHECK (membership_hash ~ '^[0-9a-f]{64}$'),
    membership_role text NOT NULL CHECK (membership_role IN (
        'selected', 'member', 'candidate', 'excluded'
    )),
    observed_at timestamptz NOT NULL,
    processed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY (
        canonical_identity_policy_generation_id,
        canonical_identity_source_claim_id
    ) REFERENCES finance.canonical_identity_source_claims(
        canonical_identity_policy_generation_id,
        canonical_identity_source_claim_id
    ),
    UNIQUE (
        canonical_identity_policy_generation_id,
        canonical_identity_source_claim_id, transaction_observation_id
    ),
    UNIQUE (membership_hash),
    CHECK (processed_at >= observed_at)
);

CREATE TABLE IF NOT EXISTS finance.canonical_identity_graph_edges (
    canonical_identity_graph_edge_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    canonical_identity_policy_generation_id uuid NOT NULL REFERENCES
        finance.canonical_identity_policy_generations(
            canonical_identity_policy_generation_id
        ),
    left_node_type text NOT NULL CHECK (left_node_type IN (
        'policy_generation', 'source_claim', 'observation_membership',
        'canonical_event', 'event_member', 'automatic_decision',
        'human_override', 'decision_event_membership', 'projection_binding'
    )),
    left_node_id uuid NOT NULL,
    right_node_type text NOT NULL CHECK (right_node_type IN (
        'policy_generation', 'source_claim', 'observation_membership',
        'canonical_event', 'event_member', 'automatic_decision',
        'human_override', 'decision_event_membership', 'projection_binding'
    )),
    right_node_id uuid NOT NULL,
    relation_kind text NOT NULL CHECK (relation_kind IN (
        'duplicate-candidate', 'transfer', 'transfer-candidate',
        'correction', 'reversal', 'pending-transition',
        'mirrored-provider-error', 'mirror-candidate'
    )),
    feature_vector jsonb NOT NULL
        CHECK (jsonb_typeof(feature_vector) = 'object'),
    confidence_basis_points integer NOT NULL
        CHECK (
            confidence_basis_points >= 0
            AND confidence_basis_points <= 10000
        ),
    automatic boolean NOT NULL,
    competing_candidate_proof jsonb NOT NULL
        CHECK (finance.valid_competing_candidate_proof(competing_candidate_proof)),
    source_hashes jsonb NOT NULL CHECK (
        finance.jsonb_sha256_array(source_hashes)
        AND jsonb_array_length(source_hashes) > 0
    ),
    edge_hash text NOT NULL CHECK (edge_hash ~ '^[0-9a-f]{64}$'),
    observed_at timestamptz NOT NULL,
    processed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (
        canonical_identity_policy_generation_id,
        left_node_type, left_node_id, right_node_type, right_node_id, relation_kind
    ),
    UNIQUE (
        canonical_identity_policy_generation_id,
        canonical_identity_graph_edge_id
    ),
    UNIQUE (canonical_identity_policy_generation_id, edge_hash),
    CHECK (left_node_type <> right_node_type OR left_node_id <> right_node_id),
    CHECK (processed_at >= observed_at)
);

CREATE TABLE IF NOT EXISTS finance.canonical_identity_events (
    canonical_identity_event_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    canonical_identity_policy_generation_id uuid NOT NULL REFERENCES
        finance.canonical_identity_policy_generations(
            canonical_identity_policy_generation_id
        ),
    canonical_id text NOT NULL CHECK (btrim(canonical_id) <> ''),
    canonical_account_hash text NOT NULL
        CHECK (canonical_account_hash ~ '^[0-9a-f]{64}$'),
    selected_observation_hash text NOT NULL
        CHECK (selected_observation_hash ~ '^[0-9a-f]{64}$'),
    event_hash text NOT NULL CHECK (event_hash ~ '^[0-9a-f]{64}$'),
    source_day date NOT NULL,
    signed_amount numeric(24, 8) NOT NULL,
    currency_code text NOT NULL CHECK (currency_code ~ '^[A-Z]{3}$'),
    description_hash text NOT NULL
        CHECK (description_hash ~ '^[0-9a-f]{64}$'),
    status text NOT NULL CHECK (status IN ('pending', 'posted', 'reversed', 'excluded')),
    category_hash text NOT NULL CHECK (category_hash ~ '^[0-9a-f]{64}$'),
    trusted boolean NOT NULL,
    observed_at timestamptz NOT NULL,
    processed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (
        canonical_identity_policy_generation_id,
        canonical_identity_event_id
    ),
    UNIQUE (
        canonical_identity_policy_generation_id,
        canonical_identity_event_id,
        canonical_id
    ),
    UNIQUE (canonical_identity_policy_generation_id, canonical_id),
    UNIQUE (canonical_identity_policy_generation_id, event_hash),
    CHECK (processed_at >= observed_at),
    CHECK (
        signed_amount NOT IN (
            'NaN'::numeric, 'Infinity'::numeric, '-Infinity'::numeric
        )
    )
);

CREATE TABLE IF NOT EXISTS finance.canonical_identity_event_members (
    canonical_identity_event_member_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    canonical_identity_policy_generation_id uuid NOT NULL,
    canonical_identity_event_id uuid NOT NULL,
    member_type text NOT NULL CHECK (member_type IN (
        'source_claim', 'transaction_observation'
    )),
    member_hash text NOT NULL CHECK (member_hash ~ '^[0-9a-f]{64}$'),
    member_role text NOT NULL CHECK (member_role IN ('selected', 'member')),
    canonical_identity_source_claim_id uuid,
    transaction_observation_id uuid REFERENCES
        finance.transaction_observations(transaction_observation_id),
    observed_at timestamptz NOT NULL,
    processed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY (
        canonical_identity_policy_generation_id,
        canonical_identity_event_id
    ) REFERENCES finance.canonical_identity_events(
        canonical_identity_policy_generation_id,
        canonical_identity_event_id
    ),
    FOREIGN KEY (
        canonical_identity_policy_generation_id,
        canonical_identity_source_claim_id
    ) REFERENCES finance.canonical_identity_source_claims(
        canonical_identity_policy_generation_id,
        canonical_identity_source_claim_id
    ),
    UNIQUE (
        canonical_identity_policy_generation_id,
        canonical_identity_event_id,
        member_hash
    ),
    CHECK (processed_at >= observed_at),
    CHECK (
        ((canonical_identity_source_claim_id IS NOT NULL)::integer
            + (transaction_observation_id IS NOT NULL)::integer) = 1
    ),
    CHECK (
        (member_type = 'source_claim'
            AND canonical_identity_source_claim_id IS NOT NULL)
        OR (member_type = 'transaction_observation'
            AND transaction_observation_id IS NOT NULL)
    )
);

CREATE TABLE IF NOT EXISTS finance.canonical_identity_event_relationships (
    canonical_identity_event_relationship_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    canonical_identity_policy_generation_id uuid NOT NULL,
    source_canonical_identity_event_id uuid NOT NULL,
    target_canonical_identity_event_id uuid NOT NULL,
    relationship_type text NOT NULL CHECK (relationship_type IN (
        'transfer', 'correction', 'reversal', 'pending'
    )),
    relationship_hash text NOT NULL
        CHECK (relationship_hash ~ '^[0-9a-f]{64}$'),
    observed_at timestamptz NOT NULL,
    processed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY (
        canonical_identity_policy_generation_id,
        source_canonical_identity_event_id
    ) REFERENCES finance.canonical_identity_events(
        canonical_identity_policy_generation_id,
        canonical_identity_event_id
    ),
    FOREIGN KEY (
        canonical_identity_policy_generation_id,
        target_canonical_identity_event_id
    ) REFERENCES finance.canonical_identity_events(
        canonical_identity_policy_generation_id,
        canonical_identity_event_id
    ),
    UNIQUE (
        canonical_identity_policy_generation_id,
        source_canonical_identity_event_id,
        target_canonical_identity_event_id,
        relationship_type
    ),
    UNIQUE (canonical_identity_policy_generation_id, relationship_hash),
    CHECK (
        source_canonical_identity_event_id <> target_canonical_identity_event_id
    ),
    CHECK (processed_at >= observed_at)
);

CREATE TABLE IF NOT EXISTS finance.canonical_identity_automatic_decisions (
    canonical_identity_automatic_decision_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    canonical_identity_policy_generation_id uuid NOT NULL,
    canonical_identity_policy_id uuid NOT NULL,
    policy_version text NOT NULL CHECK (btrim(policy_version) <> ''),
    policy_hash text NOT NULL CHECK (policy_hash ~ '^[0-9a-f]{64}$'),
    outcome text NOT NULL CHECK (outcome IN (
        'merge-observations', 'merge-claims', 'preserve-distinct',
        'link-transfer', 'link-correction', 'link-reversal', 'link-pending',
        'suppress-mirrored-provider-error', 'unresolved', 'exclude-untrusted'
    )),
    confidence_tier text NOT NULL CHECK (confidence_tier IN (
        'human-override', 'exact-scoped-identity', 'explicit-lineage',
        'unique-cross-source', 'review-required'
    )),
    confidence_basis_points integer NOT NULL
        CHECK (
            confidence_basis_points >= 0
            AND confidence_basis_points <= 10000
        ),
    rationale_code text NOT NULL
        CHECK (rationale_code ~ '^[a-z][a-z0-9_-]*([.][a-z0-9_-]+)*$'),
    feature_vector jsonb NOT NULL
        CHECK (jsonb_typeof(feature_vector) = 'object'),
    competing_candidate_proof jsonb NOT NULL
        CHECK (finance.valid_competing_candidate_proof(competing_candidate_proof)),
    source_hashes jsonb NOT NULL CHECK (
        finance.jsonb_sha256_array(source_hashes)
        AND jsonb_array_length(source_hashes) > 0
    ),
    decision_hash text NOT NULL CHECK (decision_hash ~ '^[0-9a-f]{64}$'),
    observed_at timestamptz NOT NULL,
    processed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (
        canonical_identity_automatic_decision_id,
        canonical_identity_policy_generation_id,
        canonical_identity_policy_id,
        policy_version,
        policy_hash
    ),
    UNIQUE (
        canonical_identity_policy_generation_id,
        canonical_identity_automatic_decision_id
    ),
    UNIQUE (canonical_identity_policy_generation_id, decision_hash),
    FOREIGN KEY (
        canonical_identity_policy_generation_id,
        canonical_identity_policy_id,
        policy_version,
        policy_hash
    ) REFERENCES finance.canonical_identity_policy_generations(
        canonical_identity_policy_generation_id,
        canonical_identity_policy_id,
        policy_version,
        policy_hash
    ),
    CHECK (processed_at >= observed_at)
);

CREATE TABLE IF NOT EXISTS finance.canonical_identity_human_overrides (
    canonical_identity_human_override_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    canonical_identity_policy_generation_id uuid NOT NULL REFERENCES
        finance.canonical_identity_policy_generations(
            canonical_identity_policy_generation_id
        ),
    override_id text NOT NULL CHECK (btrim(override_id) <> ''),
    override_version integer NOT NULL CHECK (override_version > 0),
    override_action text NOT NULL CHECK (override_action IN (
        'merge', 'preserve-distinct', 'transfer'
    )),
    claim_hashes jsonb NOT NULL CHECK (
        finance.jsonb_sha256_array(claim_hashes)
        AND jsonb_array_length(claim_hashes) > 1
    ),
    rationale_hash text NOT NULL CHECK (rationale_hash ~ '^[0-9a-f]{64}$'),
    override_hash text NOT NULL CHECK (override_hash ~ '^[0-9a-f]{64}$'),
    decided_at timestamptz NOT NULL,
    supersedes_canonical_identity_human_override_id uuid REFERENCES
        finance.canonical_identity_human_overrides(
            canonical_identity_human_override_id
        ),
    canonical_identity_automatic_decision_id uuid,
    precedence_rank integer NOT NULL DEFAULT 1000 CHECK (precedence_rank > 100),
    override_metadata jsonb NOT NULL DEFAULT '{"precedence":"human"}'::jsonb
        CHECK (
            jsonb_typeof(override_metadata) = 'object'
            AND override_metadata ->> 'precedence' = 'human'
        ),
    observed_at timestamptz NOT NULL,
    processed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (
        canonical_identity_policy_generation_id,
        canonical_identity_human_override_id
    ),
    UNIQUE (
        canonical_identity_policy_generation_id,
        override_id,
        override_version
    ),
    UNIQUE (
        canonical_identity_policy_generation_id,
        override_hash
    ),
    FOREIGN KEY (
        canonical_identity_policy_generation_id,
        canonical_identity_automatic_decision_id
    ) REFERENCES finance.canonical_identity_automatic_decisions(
        canonical_identity_policy_generation_id,
        canonical_identity_automatic_decision_id
    ),
    CHECK (processed_at >= observed_at),
    CHECK (
        supersedes_canonical_identity_human_override_id IS NULL
        OR supersedes_canonical_identity_human_override_id
            <> canonical_identity_human_override_id
    )
);

CREATE TABLE IF NOT EXISTS finance.canonical_identity_decision_event_memberships (
    canonical_identity_decision_event_membership_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    canonical_identity_policy_generation_id uuid NOT NULL,
    canonical_identity_automatic_decision_id uuid REFERENCES
        finance.canonical_identity_automatic_decisions(
            canonical_identity_automatic_decision_id
        ),
    canonical_identity_human_override_id uuid,
    canonical_identity_event_id uuid NOT NULL,
    canonical_id text NOT NULL CHECK (btrim(canonical_id) <> ''),
    membership_role text NOT NULL CHECK (membership_role IN (
        'subject', 'counterparty', 'related'
    )),
    membership_hash text NOT NULL CHECK (membership_hash ~ '^[0-9a-f]{64}$'),
    observed_at timestamptz NOT NULL,
    processed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY (
        canonical_identity_policy_generation_id,
        canonical_identity_automatic_decision_id
    ) REFERENCES finance.canonical_identity_automatic_decisions(
        canonical_identity_policy_generation_id,
        canonical_identity_automatic_decision_id
    ),
    FOREIGN KEY (
        canonical_identity_policy_generation_id,
        canonical_identity_human_override_id
    ) REFERENCES finance.canonical_identity_human_overrides(
        canonical_identity_policy_generation_id,
        canonical_identity_human_override_id
    ),
    FOREIGN KEY (
        canonical_identity_policy_generation_id,
        canonical_identity_event_id, canonical_id
    ) REFERENCES finance.canonical_identity_events(
        canonical_identity_policy_generation_id,
        canonical_identity_event_id, canonical_id
    ),
    UNIQUE (membership_hash),
    CHECK (
        ((canonical_identity_automatic_decision_id IS NOT NULL)::integer
            + (canonical_identity_human_override_id IS NOT NULL)::integer) = 1
    ),
    CHECK (processed_at >= observed_at)
);

CREATE UNIQUE INDEX IF NOT EXISTS canonical_identity_decision_event_memberships_auto_idx
ON finance.canonical_identity_decision_event_memberships(
    canonical_identity_policy_generation_id,
    canonical_identity_automatic_decision_id,
    canonical_identity_event_id
)
WHERE canonical_identity_automatic_decision_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS canonical_identity_decision_event_memberships_override_idx
ON finance.canonical_identity_decision_event_memberships(
    canonical_identity_policy_generation_id,
    canonical_identity_human_override_id,
    canonical_identity_event_id
)
WHERE canonical_identity_human_override_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS finance.application_projection_bindings (
    application_projection_binding_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    canonical_identity_policy_generation_id uuid NOT NULL,
    canonical_identity_event_id uuid NOT NULL,
    canonical_id text NOT NULL CHECK (btrim(canonical_id) <> ''),
    target_application text NOT NULL CHECK (target_application IN ('wealthfolio')),
    target_activity_hash text NOT NULL
        CHECK (target_activity_hash ~ '^[0-9a-f]{64}$'),
    projection_record_id uuid REFERENCES finance.projection_records(projection_record_id),
    binding_source text NOT NULL CHECK (binding_source IN (
        'policy_generation', 'automatic_decision', 'human_override'
    )),
    canonical_identity_automatic_decision_id uuid REFERENCES
        finance.canonical_identity_automatic_decisions(
            canonical_identity_automatic_decision_id
        ),
    canonical_identity_human_override_id uuid,
    binding_metadata jsonb NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(binding_metadata) = 'object'),
    effective_from timestamptz NOT NULL,
    effective_to timestamptz,
    is_active boolean NOT NULL DEFAULT true,
    observed_at timestamptz NOT NULL,
    processed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY (
        canonical_identity_policy_generation_id,
        canonical_identity_automatic_decision_id
    ) REFERENCES finance.canonical_identity_automatic_decisions(
        canonical_identity_policy_generation_id,
        canonical_identity_automatic_decision_id
    ),
    FOREIGN KEY (
        canonical_identity_policy_generation_id,
        canonical_identity_human_override_id
    ) REFERENCES finance.canonical_identity_human_overrides(
        canonical_identity_policy_generation_id,
        canonical_identity_human_override_id
    ),
    FOREIGN KEY (
        canonical_identity_policy_generation_id,
        canonical_identity_event_id, canonical_id
    ) REFERENCES finance.canonical_identity_events(
        canonical_identity_policy_generation_id,
        canonical_identity_event_id, canonical_id
    ),
    CHECK (processed_at >= observed_at),
    CHECK (effective_to IS NULL OR effective_to >= effective_from),
    CHECK ((effective_to IS NULL) = is_active),
    CHECK (
        (
            binding_source = 'policy_generation'
            AND canonical_identity_automatic_decision_id IS NULL
            AND canonical_identity_human_override_id IS NULL
        ) OR (
            binding_source = 'automatic_decision'
            AND canonical_identity_automatic_decision_id IS NOT NULL
            AND canonical_identity_human_override_id IS NULL
        ) OR (
            binding_source = 'human_override'
            AND canonical_identity_automatic_decision_id IS NULL
            AND canonical_identity_human_override_id IS NOT NULL
        )
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS application_projection_bindings_wealthfolio_active_event_idx
ON finance.application_projection_bindings(
    target_application,
    canonical_id
)
WHERE target_application = 'wealthfolio' AND is_active;

CREATE UNIQUE INDEX IF NOT EXISTS application_projection_bindings_wealthfolio_active_target_idx
ON finance.application_projection_bindings(
    target_application,
    target_activity_hash
)
WHERE target_application = 'wealthfolio' AND is_active;

DO $do$
DECLARE
    protected_table text;
BEGIN
    FOREACH protected_table IN ARRAY ARRAY[
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
        'canonical_identity_decision_event_memberships'
    ] LOOP
        EXECUTE format(
            'DROP TRIGGER IF EXISTS %I ON finance.%I',
            protected_table || '_append_only',
            protected_table
        );
        EXECUTE format(
            'CREATE TRIGGER %I BEFORE UPDATE OR DELETE ON finance.%I '
            'FOR EACH ROW EXECUTE FUNCTION finance.reject_append_only_change()',
            protected_table || '_append_only',
            protected_table
        );
    END LOOP;
END;
$do$;

CREATE OR REPLACE FUNCTION finance.protect_application_projection_binding_history()
RETURNS trigger
LANGUAGE plpgsql
AS $function$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'finance.application_projection_bindings retains binding history'
            USING ERRCODE = '55000';
    END IF;

    IF NEW.application_projection_binding_id IS DISTINCT FROM OLD.application_projection_binding_id
       OR NEW.canonical_identity_policy_generation_id
            IS DISTINCT FROM OLD.canonical_identity_policy_generation_id
       OR NEW.canonical_identity_event_id IS DISTINCT FROM OLD.canonical_identity_event_id
       OR NEW.canonical_id IS DISTINCT FROM OLD.canonical_id
       OR NEW.target_application IS DISTINCT FROM OLD.target_application
       OR NEW.target_activity_hash IS DISTINCT FROM OLD.target_activity_hash
       OR NEW.projection_record_id IS DISTINCT FROM OLD.projection_record_id
       OR NEW.binding_source IS DISTINCT FROM OLD.binding_source
       OR NEW.canonical_identity_automatic_decision_id
            IS DISTINCT FROM OLD.canonical_identity_automatic_decision_id
       OR NEW.canonical_identity_human_override_id
            IS DISTINCT FROM OLD.canonical_identity_human_override_id
       OR NEW.binding_metadata IS DISTINCT FROM OLD.binding_metadata
       OR NEW.effective_from IS DISTINCT FROM OLD.effective_from
       OR NEW.observed_at IS DISTINCT FROM OLD.observed_at
       OR NEW.processed_at IS DISTINCT FROM OLD.processed_at
       OR OLD.effective_to IS NOT NULL
       OR NEW.effective_to IS NULL
       OR NEW.effective_to < OLD.effective_from
       OR NEW.is_active IS DISTINCT FROM false
    THEN
        RAISE EXCEPTION 'application projection binding history is immutable once written'
            USING ERRCODE = '55000';
    END IF;

    RETURN NEW;
END;
$function$;

DROP TRIGGER IF EXISTS application_projection_bindings_protect_history
ON finance.application_projection_bindings;
CREATE TRIGGER application_projection_bindings_protect_history
BEFORE UPDATE OR DELETE ON finance.application_projection_bindings
FOR EACH ROW EXECUTE FUNCTION
    finance.protect_application_projection_binding_history();

CREATE OR REPLACE VIEW finance_read.identity_decision_audit AS
WITH automatic_events AS (
    SELECT
        membership.canonical_identity_policy_generation_id,
        membership.canonical_identity_automatic_decision_id AS decision_id,
        jsonb_agg(membership.canonical_id ORDER BY membership.canonical_id)
            AS canonical_event_ids
    FROM finance.canonical_identity_decision_event_memberships membership
    WHERE membership.canonical_identity_automatic_decision_id IS NOT NULL
    GROUP BY
        membership.canonical_identity_policy_generation_id,
        membership.canonical_identity_automatic_decision_id
),
automatic_bindings AS (
    SELECT
        grouped.canonical_identity_policy_generation_id,
        grouped.decision_id,
        jsonb_agg(grouped.target_activity_hash ORDER BY grouped.target_activity_hash)
            AS active_target_activity_hashes
    FROM (
        SELECT DISTINCT
            membership.canonical_identity_policy_generation_id,
            membership.canonical_identity_automatic_decision_id AS decision_id,
            binding.target_activity_hash
        FROM finance.canonical_identity_decision_event_memberships membership
        JOIN finance.application_projection_bindings binding
          ON binding.canonical_identity_policy_generation_id
                = membership.canonical_identity_policy_generation_id
         AND binding.canonical_identity_event_id = membership.canonical_identity_event_id
         AND binding.canonical_id = membership.canonical_id
         AND binding.target_application = 'wealthfolio'
         AND binding.is_active
        WHERE membership.canonical_identity_automatic_decision_id IS NOT NULL
    ) grouped
    GROUP BY grouped.canonical_identity_policy_generation_id, grouped.decision_id
),
override_events AS (
    SELECT
        membership.canonical_identity_policy_generation_id,
        membership.canonical_identity_human_override_id AS decision_id,
        jsonb_agg(membership.canonical_id ORDER BY membership.canonical_id)
            AS canonical_event_ids
    FROM finance.canonical_identity_decision_event_memberships membership
    WHERE membership.canonical_identity_human_override_id IS NOT NULL
    GROUP BY
        membership.canonical_identity_policy_generation_id,
        membership.canonical_identity_human_override_id
),
override_bindings AS (
    SELECT
        grouped.canonical_identity_policy_generation_id,
        grouped.decision_id,
        jsonb_agg(grouped.target_activity_hash ORDER BY grouped.target_activity_hash)
            AS active_target_activity_hashes
    FROM (
        SELECT DISTINCT
            membership.canonical_identity_policy_generation_id,
            membership.canonical_identity_human_override_id AS decision_id,
            binding.target_activity_hash
        FROM finance.canonical_identity_decision_event_memberships membership
        JOIN finance.application_projection_bindings binding
          ON binding.canonical_identity_policy_generation_id
                = membership.canonical_identity_policy_generation_id
         AND binding.canonical_identity_event_id = membership.canonical_identity_event_id
         AND binding.canonical_id = membership.canonical_id
         AND binding.target_application = 'wealthfolio'
         AND binding.is_active
        WHERE membership.canonical_identity_human_override_id IS NOT NULL
    ) grouped
    GROUP BY grouped.canonical_identity_policy_generation_id, grouped.decision_id
)
SELECT
    'automatic'::text AS decision_source,
    decision.canonical_identity_automatic_decision_id AS decision_id,
    NULL::text AS override_id,
    policy.policy_name,
    decision.policy_version,
    decision.policy_hash,
    generation.generation_hash,
    generation.input_hash,
    generation.canonical_state_hash,
    decision.outcome,
    decision.confidence_tier,
    decision.confidence_basis_points,
    decision.rationale_code,
    COALESCE(automatic_events.canonical_event_ids, '[]'::jsonb) AS canonical_event_ids,
    decision.feature_vector,
    decision.competing_candidate_proof,
    decision.source_hashes,
    decision.decision_hash AS record_hash,
    COALESCE(
        automatic_bindings.active_target_activity_hashes,
        '[]'::jsonb
    ) AS active_target_activity_hashes,
    decision.observed_at,
    decision.processed_at
FROM finance.canonical_identity_automatic_decisions decision
JOIN finance.canonical_identity_policy_generations generation
  ON generation.canonical_identity_policy_generation_id
        = decision.canonical_identity_policy_generation_id
 AND generation.canonical_identity_policy_id
        = decision.canonical_identity_policy_id
 AND generation.policy_version = decision.policy_version
 AND generation.policy_hash = decision.policy_hash
JOIN finance.canonical_identity_policies policy
  ON policy.canonical_identity_policy_id = generation.canonical_identity_policy_id
 AND policy.policy_version = generation.policy_version
 AND policy.policy_hash = generation.policy_hash
LEFT JOIN automatic_events
  ON automatic_events.canonical_identity_policy_generation_id
        = decision.canonical_identity_policy_generation_id
 AND automatic_events.decision_id = decision.canonical_identity_automatic_decision_id
LEFT JOIN automatic_bindings
  ON automatic_bindings.canonical_identity_policy_generation_id
        = decision.canonical_identity_policy_generation_id
 AND automatic_bindings.decision_id = decision.canonical_identity_automatic_decision_id
UNION ALL
SELECT
    'human_override'::text AS decision_source,
    override_row.canonical_identity_human_override_id AS decision_id,
    override_row.override_id,
    policy.policy_name,
    generation.policy_version,
    generation.policy_hash,
    generation.generation_hash,
    generation.input_hash,
    generation.canonical_state_hash,
    override_row.override_action AS outcome,
    'human-override'::text AS confidence_tier,
    10000 AS confidence_basis_points,
    NULL::text AS rationale_code,
    COALESCE(override_events.canonical_event_ids, '[]'::jsonb) AS canonical_event_ids,
    override_row.override_metadata AS feature_vector,
    '{}'::jsonb AS competing_candidate_proof,
    override_row.claim_hashes AS source_hashes,
    override_row.override_hash AS record_hash,
    COALESCE(
        override_bindings.active_target_activity_hashes,
        '[]'::jsonb
   ) AS active_target_activity_hashes,
   override_row.observed_at,
   override_row.processed_at
FROM finance.canonical_identity_human_overrides override_row
LEFT JOIN finance.canonical_identity_policy_generations generation
 ON generation.canonical_identity_policy_generation_id
       = override_row.canonical_identity_policy_generation_id
LEFT JOIN finance.canonical_identity_policies policy
 ON policy.canonical_identity_policy_id = generation.canonical_identity_policy_id
 AND policy.policy_version = generation.policy_version
 AND policy.policy_hash = generation.policy_hash
LEFT JOIN override_events
 ON override_events.canonical_identity_policy_generation_id
       = override_row.canonical_identity_policy_generation_id
 AND override_events.decision_id = override_row.canonical_identity_human_override_id
LEFT JOIN override_bindings
 ON override_bindings.canonical_identity_policy_generation_id
       = override_row.canonical_identity_policy_generation_id
 AND override_bindings.decision_id = override_row.canonical_identity_human_override_id;

CREATE OR REPLACE VIEW finance_read.identity_generation_summary AS
WITH source_claim_counts AS (
    SELECT
        claim.canonical_identity_policy_generation_id,
        count(*) AS source_claim_count
    FROM finance.canonical_identity_source_claims claim
    GROUP BY claim.canonical_identity_policy_generation_id
),
graph_edge_counts AS (
    SELECT
        edge.canonical_identity_policy_generation_id,
        count(*) AS graph_edge_count
    FROM finance.canonical_identity_graph_edges edge
    GROUP BY edge.canonical_identity_policy_generation_id
),
event_counts AS (
    SELECT
        event.canonical_identity_policy_generation_id,
        count(*) AS canonical_event_count
    FROM finance.canonical_identity_events event
    GROUP BY event.canonical_identity_policy_generation_id
),
decision_counts AS (
    SELECT
        decision.canonical_identity_policy_generation_id,
        count(*) AS automatic_decision_count
    FROM finance.canonical_identity_automatic_decisions decision
    GROUP BY decision.canonical_identity_policy_generation_id
),
safe_automatic_resolution_counts AS (
    SELECT
        decision.canonical_identity_policy_generation_id,
        count(*) AS safe_automatic_resolution_count
    FROM finance.canonical_identity_automatic_decisions decision
    WHERE decision.confidence_tier <> 'review-required'
      AND decision.outcome IN (
          'merge-observations',
          'merge-claims',
          'preserve-distinct',
          'suppress-mirrored-provider-error'
      )
    GROUP BY decision.canonical_identity_policy_generation_id
),
unresolved_decision_counts AS (
    SELECT
        decision.canonical_identity_policy_generation_id,
        count(*) AS unresolved_decision_count
    FROM finance.canonical_identity_automatic_decisions decision
    WHERE decision.outcome = 'unresolved'
    GROUP BY decision.canonical_identity_policy_generation_id
),
override_counts AS (
    SELECT
        override_row.canonical_identity_policy_generation_id,
        count(*) AS human_override_count
    FROM finance.canonical_identity_human_overrides override_row
    GROUP BY override_row.canonical_identity_policy_generation_id
),
binding_counts AS (
    SELECT
        binding.canonical_identity_policy_generation_id,
        count(DISTINCT binding.application_projection_binding_id)
            AS active_wealthfolio_binding_count
    FROM finance.application_projection_bindings binding
    WHERE binding.target_application = 'wealthfolio'
      AND binding.is_active
    GROUP BY binding.canonical_identity_policy_generation_id
)
SELECT
    policy.policy_name,
    generation.canonical_identity_policy_generation_id,
    generation.canonical_identity_policy_id,
    generation.policy_version,
    generation.policy_hash,
    generation.generation_number,
    generation.generation_label,
    generation.generation_hash,
    generation.input_hash,
    generation.canonical_state_hash,
    generation.feature_schema_hash,
    COALESCE(source_claim_counts.source_claim_count, 0) AS source_claim_count,
    COALESCE(graph_edge_counts.graph_edge_count, 0) AS graph_edge_count,
    COALESCE(decision_counts.automatic_decision_count, 0) AS automatic_decision_count,
    COALESCE(event_counts.canonical_event_count, 0) AS canonical_event_count,
    COALESCE(
        safe_automatic_resolution_counts.safe_automatic_resolution_count, 0
    ) AS safe_automatic_resolution_count,
    COALESCE(
        unresolved_decision_counts.unresolved_decision_count, 0
    ) AS unresolved_decision_count,
    COALESCE(override_counts.human_override_count, 0) AS human_override_count,
    COALESCE(
        binding_counts.active_wealthfolio_binding_count, 0
    ) AS active_wealthfolio_binding_count,
    generation.observed_at,
    generation.processed_at
FROM finance.canonical_identity_policy_generations generation
JOIN finance.canonical_identity_policies policy
  ON policy.canonical_identity_policy_id = generation.canonical_identity_policy_id
 AND policy.policy_version = generation.policy_version
 AND policy.policy_hash = generation.policy_hash
LEFT JOIN source_claim_counts
  ON source_claim_counts.canonical_identity_policy_generation_id
        = generation.canonical_identity_policy_generation_id
LEFT JOIN graph_edge_counts
  ON graph_edge_counts.canonical_identity_policy_generation_id
        = generation.canonical_identity_policy_generation_id
LEFT JOIN event_counts
  ON event_counts.canonical_identity_policy_generation_id
        = generation.canonical_identity_policy_generation_id
LEFT JOIN decision_counts
  ON decision_counts.canonical_identity_policy_generation_id
        = generation.canonical_identity_policy_generation_id
LEFT JOIN safe_automatic_resolution_counts
  ON safe_automatic_resolution_counts.canonical_identity_policy_generation_id
        = generation.canonical_identity_policy_generation_id
LEFT JOIN unresolved_decision_counts
  ON unresolved_decision_counts.canonical_identity_policy_generation_id
        = generation.canonical_identity_policy_generation_id
LEFT JOIN override_counts
  ON override_counts.canonical_identity_policy_generation_id
        = generation.canonical_identity_policy_generation_id
LEFT JOIN binding_counts
  ON binding_counts.canonical_identity_policy_generation_id
        = generation.canonical_identity_policy_generation_id;

GRANT SELECT ON
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
    finance.application_projection_bindings
TO finance_shadow_ingest;

GRANT INSERT ON
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
    finance.application_projection_bindings
TO finance_shadow_ingest;

GRANT UPDATE (effective_to, is_active)
ON finance.application_projection_bindings TO finance_shadow_ingest;

GRANT SELECT ON
    finance_read.identity_decision_audit,
    finance_read.identity_generation_summary
TO finance_readonly;

INSERT INTO finance.schema_migrations(version, name, checksum)
VALUES (:'migration_version', :'migration_name', :'migration_checksum')
ON CONFLICT (version) DO NOTHING;

COMMIT;
