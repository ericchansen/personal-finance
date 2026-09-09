from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy" / "postgres"


def _text(relative: str) -> str:
    return (DEPLOY / relative).read_text(encoding="utf-8")


def test_identity_migration_defines_required_tables_views_and_guards():
    migration = _text("migrations/0011_canonical_identity_engine.sql")

    for table in (
        "canonical_identity_policies",
        "canonical_identity_policy_generations",
        "canonical_identity_source_claims",
        "canonical_identity_observation_memberships",
        "canonical_identity_graph_edges",
        "canonical_identity_events",
        "canonical_identity_event_members",
        "canonical_identity_event_relationships",
        "canonical_identity_automatic_decisions",
        "canonical_identity_human_overrides",
        "canonical_identity_decision_event_memberships",
        "application_projection_bindings",
    ):
        assert f"CREATE TABLE IF NOT EXISTS finance.{table}" in migration

    assert "finance.valid_competing_candidate_proof" in migration
    assert "finance.jsonb_sha256_array" in migration
    assert (
        "policy_version text NOT NULL CHECK (btrim(policy_version) <> '')" in migration
    )
    assert "input_hash text NOT NULL CHECK (input_hash ~ '^[0-9a-f]{64}$')" in migration
    assert "canonical_state_hash text NOT NULL" in migration
    assert "selected_observation_hash text NOT NULL" in migration
    assert "source_hashes jsonb NOT NULL CHECK (" in migration
    assert (
        "canonical_identity_policy_generation_id uuid NOT NULL REFERENCES" in migration
    )
    assert "UNIQUE (canonical_identity_policy_generation_id, claim_hash)" in migration
    assert "UNIQUE (canonical_identity_policy_generation_id, canonical_id)" in migration
    assert "UNIQUE (canonical_identity_policy_generation_id, event_hash)" in migration
    assert (
        "canonical_identity_policy_generation_id,\n        override_id,\n        override_version"
        in migration
    )
    assert (
        "canonical_identity_policy_generation_id,\n        override_hash" in migration
    )
    assert "UNIQUE (override_id, override_version)" not in migration
    assert "UNIQUE (override_hash)" not in migration
    assert "override_row.canonical_identity_policy_generation_id" in migration
    assert "GROUP BY override_row.canonical_identity_policy_generation_id" in migration
    assert "relation_kind text NOT NULL CHECK (relation_kind IN (" in migration
    assert "'duplicate-candidate', 'transfer', 'transfer-candidate'" in migration
    assert "'mirrored-provider-error', 'mirror-candidate'" in migration
    assert "feature_vector jsonb NOT NULL" in migration
    assert "automatic boolean NOT NULL" in migration
    assert "'transfer', 'correction', 'reversal', 'pending'" in migration
    assert "'merge-observations', 'merge-claims', 'preserve-distinct'" in migration
    assert "'exact-scoped-identity', 'explicit-lineage'" in migration
    assert "confidence_basis_points <= 10000" in migration
    assert "rationale_code ~ '^[a-z][a-z0-9_-]*([.][a-z0-9_-]+)*$'" in migration
    assert "candidate = '{}'::jsonb THEN true" in migration
    assert "candidate ? 'sourceClaimCount'" in migration
    assert "candidate ? 'leftDegree'" in migration
    assert "entry.key !~ '^degree:[0-9a-f]{64}$'" in migration
    assert "status IN ('pending', 'posted', 'reversed', 'excluded')" in migration
    assert "'policy_generation', 'automatic_decision', 'human_override'" in migration
    assert "binding_source = 'policy_generation'" in migration
    assert "AND canonical_identity_automatic_decision_id IS NULL" in migration
    assert "AND canonical_identity_human_override_id IS NULL" in migration
    assert "application_projection_bindings_wealthfolio_active_event_idx" in migration
    assert "application_projection_bindings_wealthfolio_active_target_idx" in migration
    assert "target_application,\n    canonical_id" in migration
    assert "target_application,\n    target_activity_hash" in migration
    assert "finance_read.identity_decision_audit" in migration
    assert "finance_read.identity_generation_summary" in migration
    assert "source_claim_count" in migration
    assert "graph_edge_count" in migration
    assert "safe_automatic_resolution_count" in migration
    assert "unresolved_decision_count" in migration
    assert "FROM finance.canonical_identity_events event" in migration
    assert "decision.confidence_tier <> 'review-required'" in migration
    assert "'suppress-mirrored-provider-error'" in migration
    assert "WHERE decision.outcome = 'unresolved'" in migration
    assert (
        "FOREIGN KEY (\n        canonical_identity_policy_generation_id," in migration
    )
    assert (
        "FOREIGN KEY (\n        canonical_identity_policy_generation_id,\n        canonical_identity_event_id, canonical_id"
        in migration
    )
    assert (
        "UNIQUE (canonical_identity_policy_id, input_hash, generation_hash)"
        in migration
    )
    assert (
        "UNIQUE (\n        canonical_identity_policy_id,\n        input_hash,\n        canonical_state_hash"
        not in migration
    )
    assert "REFERENCES finance.canonical_identity_policy_generations" in migration
    assert "REFERENCES finance.canonical_identity_human_overrides" in migration
    assert "decision_hash ~ '^[0-9a-f]{64}$'" in migration
    assert "override_metadata ->> 'precedence' = 'human'" in migration
    assert "GRANT UPDATE (effective_to, is_active)" in migration


def test_identity_integration_and_provisioning_cover_new_objects():
    integration = _text("integration/schema_test.sql")
    provision = _text("scripts/provision-logins.sh")

    migration_count = len(list((DEPLOY / "migrations").glob("*.sql")))
    assert f"expected {migration_count} applied migrations" in integration
    assert "'identity_decision_audit', 'identity_generation_summary'" in integration
    assert "'canonical_identity_decision_event_memberships'" in integration
    assert "same canonical state generations were not both preserved" in integration
    assert (
        "generation-scoped human override snapshots were not preserved" in integration
    )
    assert "generation-scoped human override hashes were not preserved" in integration
    assert "duplicate generation hash unexpectedly succeeded" in integration
    assert "same-generation claim snapshot unexpectedly duplicated" in integration
    assert (
        "same-generation canonical event snapshot unexpectedly duplicated"
        in integration
    )
    assert "generation-scoped source claim snapshots were not preserved" in integration
    assert (
        "generation-scoped canonical event snapshots were not preserved" in integration
    )
    assert (
        "later generation canonical event replay did not retain its distinct event hash"
        in integration
    )
    assert "policy-generation projection binding unexpectedly failed" in integration
    assert (
        "active wealthfolio binding uniqueness by canonical id was not enforced globally"
        in integration
    )
    assert "'policy_generation'" in integration
    assert (
        "identity generation summary did not include policy-generation projection bindings"
        in integration
    )
    assert "source_claim_count = 1" in integration
    assert "graph_edge_count = 1" in integration
    assert "safe_automatic_resolution_count = 2" in integration
    assert "unresolved_decision_count = 1" in integration
    assert (
        "identity decision audit view did not attribute standalone override rows to their generation"
        in integration
    )
    assert (
        "identity decision audit view did not preserve replayed human overrides across generations"
        in integration
    )
    assert "human-standalone-1" in integration
    assert "invalid competing candidate proof unexpectedly succeeded" in integration
    assert "invalid graph edge evidence unexpectedly succeeded" in integration
    assert "graph edge update unexpectedly succeeded" in integration
    assert "decision event membership update unexpectedly succeeded" in integration
    assert "'canonical-event-excluded'" in integration
    assert "second_policy_generation_id" in integration
    assert (
        "relation_kind, feature_vector, confidence_basis_points, automatic,"
        in integration
    )
    assert "outcome = 'merge-observations'" in integration
    assert "confidence_tier = 'human-override'" in integration
    assert "application_projection_bindings_wealthfolio_active_event_idx" in integration
    assert (
        "application_projection_bindings_wealthfolio_active_target_idx" in integration
    )
    assert "ingest role lacks canonical identity inserts" in integration
    assert (
        "identity decision audit view did not expose the override binding"
        in integration
    )

    for name in (
        "finance.canonical_identity_policies",
        "finance.canonical_identity_automatic_decisions",
        "finance.canonical_identity_decision_event_memberships",
        "finance.application_projection_bindings",
    ):
        assert name in provision
    assert "GRANT UPDATE (effective_to, is_active)" in provision


def test_source_authority_migration_extends_identity_without_rewriting_0011():
    migration = _text("migrations/0012_source_coverage_authority.sql")
    original = _text("migrations/0011_canonical_identity_engine.sql")

    for table in (
        "canonical_identity_source_authority_policies",
        "canonical_identity_authority_intervals",
        "canonical_identity_source_suppressions",
    ):
        assert f"CREATE TABLE IF NOT EXISTS finance.{table}" in migration
        assert table not in original

    assert "CREATE OR REPLACE FUNCTION finance.valid_competing_candidate_proof" in (
        migration
    )
    assert "candidate ? 'authoritativeCount'" in migration
    assert "candidate ? 'lowerCount'" in migration
    assert "candidate ? 'bucketParticipantCount'" in migration
    assert "entry.key !~ '^degree:[0-9a-f]{64}$'" in migration

    assert "'source-suppressed'" in migration
    assert "'authoritative-source-coverage'" in migration
    assert (
        "canonical_identity_graph_edges_relation_kind_check" in migration
    )
    assert "canonical_identity_automatic_decisions_outcome_check" in migration
    assert (
        "canonical_identity_automatic_decisions_confidence_tier_check" in migration
    )
    assert "ADD COLUMN IF NOT EXISTS residual_classification text" in migration
    assert "ADD COLUMN IF NOT EXISTS source_authority_policy_hash text" in migration
    assert "'distinct', 'source-suppressed', 'transfer'" in migration
    assert "'correction', 'reversal', 'unresolved'" in migration

    assert "CHECK (effective_from <= effective_through)" in migration
    assert "CHECK (extraction_requested_from <= effective_from)" in migration
    assert "CHECK (effective_through <= extraction_requested_through)" in migration
    assert (
        "CHECK (trust_cutoff_day IS NULL OR effective_through <= trust_cutoff_day)"
        in migration
    )
    assert "CHECK (freshness_as_of >= extracted_at)" in migration
    assert "authority_rank IS NULL OR authority_rank > 0" in migration
    assert "CHECK (authoritative = (coverage_proven AND counts_reconciled" in migration
    assert (
        "CHECK (suppressed_occurrence_count <= authoritative_occurrence_count)"
        in migration
    )
    assert "CHECK (occurrence_index < authoritative_occurrence_count)" in migration
    assert (
        "CHECK (suppressed_source_claim_id <> authoritative_source_claim_id)"
        in migration
    )

    assert "finance.reject_append_only_change()" in migration
    assert "finance_read.identity_source_authority_summary" in migration
    assert "GRANT SELECT ON\n    finance_read.identity_source_authority_summary" in (
        migration
    )
    assert "INSERT INTO finance.schema_migrations" in migration

    integration = _text("integration/schema_test.sql")
    provision = _text("scripts/provision-logins.sh")
    for table in (
        "canonical_identity_source_authority_policies",
        "canonical_identity_authority_intervals",
        "canonical_identity_source_suppressions",
    ):
        assert f"finance.{table}" in integration
        assert f"finance.{table}" in provision
        assert f"'{table}'" in integration
    assert "'identity_source_authority_summary'" in integration
    assert "finance_read.identity_source_authority_summary" in integration


def test_scoped_provider_token_migration_extends_without_rewriting_0012():
    migration = _text("migrations/0013_scoped_provider_token_lineage.sql")
    authority = _text("migrations/0012_source_coverage_authority.sql")
    original = _text("migrations/0011_canonical_identity_engine.sql")

    table = "canonical_identity_provider_token_scopes"
    assert f"CREATE TABLE IF NOT EXISTS finance.{table}" in migration
    assert table not in authority
    assert table not in original

    # 0013 must re-declare every proof branch 0012 already accepted, so that
    # replacing the function never narrows what a stored decision may prove.
    assert "CREATE OR REPLACE FUNCTION finance.valid_competing_candidate_proof" in (
        migration
    )
    for existing_branch in (
        "candidate = '{}'::jsonb",
        "candidate ? 'competingCandidateCount'",
        "candidate ? 'sourceClaimCount'",
        "candidate ? 'leftDegree'",
        "candidate ? 'rightDegree'",
        "candidate ? 'componentSize'",
        "candidate ? 'authoritativeCount'",
        "candidate ? 'lowerCount'",
        "candidate ? 'bucketParticipantCount'",
        "entry.key !~ '^degree:[0-9a-f]{64}$'",
    ):
        assert existing_branch in authority
        assert existing_branch in migration

    for token_key in (
        "candidate ? 'leftNamespaceCount'",
        "candidate ? 'rightNamespaceCount'",
        "candidate ? 'sharedTokenBucketCount'",
        "candidate ? 'dateDistanceDays'",
        "candidate ? 'maxDaySkewDays'",
    ):
        assert token_key in migration
        assert token_key not in authority

    # No enum widening: a scoped token link reuses relation kinds, outcomes and
    # tiers 0011/0012 already admit.
    assert "canonical_identity_graph_edges_relation_kind_check" not in migration
    assert "canonical_identity_automatic_decisions_outcome_check" not in migration
    assert (
        "canonical_identity_automatic_decisions_confidence_tier_check" not in migration
    )

    # The declaration stores hashes only, never a provider token or account id.
    assert "left_namespace_hash text NOT NULL" in migration
    assert "right_namespace_hash text NOT NULL" in migration
    assert "canonical_account_hash text NOT NULL" in migration
    for raw_column in (
        "provider_token text",
        "token text",
        "token_prefix",
        "source_account_id",
        "canonical_account_id",
    ):
        assert raw_column not in migration

    assert "CHECK (left_namespace_hash <> right_namespace_hash)" in migration
    assert "CHECK (max_day_skew BETWEEN 0 AND 3)" in migration
    assert "'ofx-fitid', 'simplefin-id', 'scoped-provider-id'" in migration
    assert "UNIQUE (canonical_identity_policy_generation_id, scope_hash)" in migration

    assert "finance.reject_append_only_change()" in migration
    assert "finance_read.identity_provider_token_scope_summary" in migration
    assert "'scoped-shared-provider-token-lineage'" in migration
    assert "INSERT INTO finance.schema_migrations" in migration

    integration = _text("integration/schema_test.sql")
    provision = _text("scripts/provision-logins.sh")
    assert f"finance.{table}" in provision
    assert f"'{table}'" in integration
    assert "'identity_provider_token_scope_summary'" in integration
    assert "finance_read.identity_provider_token_scope_summary" in integration
    # The applied-migration count is pinned to the directory by
    # test_live_schema_test_expects_every_migration_on_disk.


def test_scoped_provider_token_proof_shape_matches_identity_module():
    from finance_store.identity import (
        MAX_PROVIDER_TOKEN_DAY_SKEW,
        SHARED_TOKEN_PROVIDER_KINDS,
    )

    migration = _text("migrations/0013_scoped_provider_token_lineage.sql")
    assert f"BETWEEN 0 AND {MAX_PROVIDER_TOKEN_DAY_SKEW}" in migration
    for kind in SHARED_TOKEN_PROVIDER_KINDS:
        assert f"'{kind}'" in migration


def test_posting_window_migration_extends_without_rewriting_0012_or_0013():
    migration = _text("migrations/0014_authority_posting_window.sql")
    authority = _text("migrations/0012_source_coverage_authority.sql")
    token = _text("migrations/0013_scoped_provider_token_lineage.sql")

    column = "posting_date_tolerance_days"
    assert (
        "ALTER TABLE finance.canonical_identity_authority_intervals"
        in migration
    )
    assert f"ADD COLUMN IF NOT EXISTS {column}" in migration
    assert column not in authority
    assert column not in token

    # The suppression proof shape is unchanged, so the validator must not be
    # replaced again: narrowing it would invalidate stored decisions.
    assert "CREATE OR REPLACE FUNCTION" not in migration
    assert "DROP TABLE" not in migration
    assert "DROP COLUMN" not in migration

    # 0014 admitted only provider-stable sources; 0015 widens that.
    assert f"CHECK ({column} = 0 OR stable_id_support)" in migration


def test_replay_stable_window_migration_widens_without_rewriting_0014():
    migration = _text("migrations/0015_replay_stable_posting_window.sql")
    window = _text("migrations/0014_authority_posting_window.sql")

    constraint = (
        "canonical_identity_authority_intervals_posting_window_stable_check"
    )
    assert f"DROP CONSTRAINT IF EXISTS\n        {constraint}" in migration
    assert f"ADD CONSTRAINT\n        {constraint}" in migration
    assert (
        "CHECK (\n"
        "        posting_date_tolerance_days = 0\n"
        "        OR stable_id_support\n"
        "        OR replay_stable_ids\n"
        "    )" in migration
    )

    # Strictly widening: 0014 is left intact and nothing else is touched.
    assert "replay_stable_ids" not in window
    assert "CREATE OR REPLACE FUNCTION" not in migration
    assert "CREATE OR REPLACE VIEW" not in migration
    assert "DROP TABLE" not in migration
    assert "DROP COLUMN" not in migration
    assert "ADD COLUMN" not in migration
    assert (
        "canonical_identity_authority_intervals_posting_window_check"
        not in migration
    )


def test_replay_stable_window_migration_matches_the_identity_module():
    import pytest

    from finance_store.identity import build_source_authority

    from tests.test_identity_source_authority import evidence

    def record(*, stable: bool, replay: bool) -> dict[str, object]:
        item = evidence(family="extract", strength="synthetic-csv", count=1)
        item["stable_id_support"] = stable
        item["replay_stable_ids"] = replay
        item["posting_date_tolerance_days"] = 1
        return item

    # Every combination the SQL CHECK admits, the resolver admits too.
    for stable, replay in ((True, True), (True, False), (False, True)):
        assert build_source_authority([record(stable=stable, replay=replay)])
    with pytest.raises(ValueError, match="stable or replay-stable"):
        build_source_authority([record(stable=False, replay=False)])


def test_posting_window_ceiling_matches_identity_module():
    from finance_store.identity import MAX_POSTING_DATE_TOLERANCE_DAYS

    migration = _text("migrations/0014_authority_posting_window.sql")
    assert (
        f"BETWEEN 0 AND {MAX_POSTING_DATE_TOLERANCE_DAYS}" in migration
    )


def test_posting_window_summary_view_is_a_safe_aggregate():
    migration = _text("migrations/0014_authority_posting_window.sql")
    integration = _text("integration/schema_test.sql")

    assert (
        "CREATE OR REPLACE VIEW finance_read.identity_posting_window_summary"
        in migration
    )
    assert "count(*) AS decision_count" in migration
    assert "GRANT SELECT ON\n    finance_read.identity_posting_window_summary" in (
        migration
    )
    # Only families, distances and counts leave the view.
    view_body = migration.split(
        "CREATE OR REPLACE VIEW finance_read.identity_posting_window_summary"
    )[1].split("GRANT SELECT")[0]
    for leaked in ("canonical_account_hash", "description", "signed_amount"):
        assert leaked not in view_body
    assert "identity_posting_window_summary" in integration


def test_excluded_source_accounts_are_preserved_as_evidence():
    migration = _text("migrations/0016_excluded_source_accounts.sql")

    assert "DROP CONSTRAINT source_accounts_status_check" in migration
    assert "('active', 'closed', 'excluded', 'unknown')" in migration
    assert "DROP TABLE" not in migration
    assert "DROP COLUMN" not in migration


def test_observation_identity_hashes_widen_0011_without_rewriting_it():
    migration = _text("migrations/0017_observation_identity_hashes.sql")
    engine = _text("migrations/0011_canonical_identity_engine.sql")

    # Both member tables gain the alternative identity, with the same shape.
    for table in (
        "canonical_identity_observation_memberships",
        "canonical_identity_event_members",
    ):
        assert (
            f"ALTER TABLE finance.{table}\n"
            "    ADD COLUMN IF NOT EXISTS observation_identity_hash text;"
        ) in migration
        assert (
            f"{table}_identity_hash_check\n"
            "    CHECK (\n"
            "        observation_identity_hash IS NULL\n"
            "        OR observation_identity_hash ~ '^[0-9a-f]{64}$'\n"
            "    );"
        ) in migration

    # The uuid foreign key becomes optional rather than fabricated.
    assert (
        "ALTER TABLE finance.canonical_identity_observation_memberships\n"
        "    ALTER COLUMN transaction_observation_id DROP NOT NULL;"
    ) in migration

    # Exactly one identity per row on both tables: never none, never both.
    assert (
        "canonical_identity_observation_memberships_identity_exactly_one_check\n"
        "    CHECK (\n"
        "        ((transaction_observation_id IS NOT NULL)::integer\n"
        "            + (observation_identity_hash IS NOT NULL)::integer) = 1\n"
        "    );"
    ) in migration
    assert (
        "canonical_identity_event_members_member_identity_exactly_one_check\n"
        "    CHECK (\n"
        "        ((canonical_identity_source_claim_id IS NOT NULL)::integer\n"
        "            + (transaction_observation_id IS NOT NULL)::integer\n"
        "            + (observation_identity_hash IS NOT NULL)::integer) = 1\n"
        "    );"
    ) in migration

    # Source-claim members keep exactly the 0011 rule.
    assert (
        "(member_type = 'source_claim'\n"
        "            AND canonical_identity_source_claim_id IS NOT NULL)"
    ) in migration

    # 0011's unique guarantee is restored on the hash branch, where SQL would
    # otherwise treat every NULL foreign key as distinct.
    for index in (
        "canonical_identity_observation_memberships_identity_hash_idx",
        "canonical_identity_event_members_identity_hash_idx",
    ):
        assert f"CREATE UNIQUE INDEX IF NOT EXISTS\n    {index}" in migration
        assert "WHERE observation_identity_hash IS NOT NULL;" in migration

    # 0011 is extended, never rewritten, and nothing is destroyed.
    assert "observation_identity_hash" not in engine
    assert "DROP TABLE" not in migration
    assert "DROP COLUMN" not in migration
    assert "DELETE FROM" not in migration
    assert "UPDATE finance." not in migration
    assert "CREATE TABLE" not in migration
    assert "DROP TRIGGER" not in migration


def test_observation_identity_migration_drops_0011_checks_by_definition():
    migration = _text("migrations/0017_observation_identity_hashes.sql")

    # 0011 wrote the blocking checks inline and unnamed, so PostgreSQL generated
    # their names. Locating them by definition keeps the migration correct
    # regardless of how they were numbered.
    block = migration.split("DO $do$")[1].split("$do$;")[0]
    assert "conrelid\n                = 'finance.canonical_identity_event_members'::regclass" in block
    assert "constraint_row.contype = 'c'" in block
    assert "LIKE '%transaction_observation_id IS NOT NULL%'" in block
    assert "DROP CONSTRAINT %I" in block
    # The replacements this migration adds must not drop themselves.
    assert "member_identity_exactly_one_check" in block
    assert "member_type_identity_check" in block


def test_observation_identity_binding_summary_is_a_safe_aggregate():
    migration = _text("migrations/0017_observation_identity_hashes.sql")
    integration = _text("integration/schema_test.sql")

    assert (
        "CREATE OR REPLACE VIEW "
        "finance_read.identity_observation_binding_summary" in migration
    )
    assert "count(*) AS binding_count" in migration
    assert (
        "GRANT SELECT ON\n    finance_read.identity_observation_binding_summary"
        in migration
    )

    # Only scopes, roles, identity kinds and counts leave the view.
    body = migration.split(
        "CREATE OR REPLACE VIEW finance_read.identity_observation_binding_summary"
    )[1].split("-- Table-level grants")[0]
    for leaked in (
        "observation_identity_hash,",
        "membership_hash",
        "member_hash",
        "canonical_account_hash",
        "signed_amount",
        "description",
    ):
        assert leaked not in body

    assert "identity_observation_binding_summary" in integration


def test_migration_records_itself_in_the_schema_ledger():
    migration = _text("migrations/0017_observation_identity_hashes.sql")

    assert migration.startswith("BEGIN;")
    assert migration.rstrip().endswith("COMMIT;")
    assert (
        "INSERT INTO finance.schema_migrations(version, name, checksum)\n"
        "VALUES (:'migration_version', :'migration_name', :'migration_checksum')\n"
        "ON CONFLICT (version) DO NOTHING;" in migration
    )


def test_live_schema_test_expects_every_migration_on_disk():
    """The live count drifted silently before; keep it pinned to the directory."""

    migrations = sorted((DEPLOY / "migrations").glob("*.sql"))
    integration = _text("integration/schema_test.sql")

    assert (
        f"IF (SELECT count(*) FROM finance.schema_migrations) <> "
        f"{len(migrations)} THEN" in integration
    )
    assert f"expected {len(migrations)} applied migrations" in integration
    # Contiguous versions, so a skipped number is caught here rather than live.
    assert [item.name[:4] for item in migrations] == [
        f"{index:04d}" for index in range(1, len(migrations) + 1)
    ]


def test_live_schema_test_covers_both_observation_identity_shapes():
    integration = _text("integration/schema_test.sql")

    assert "membership carrying both identities unexpectedly admitted" in integration
    assert "membership carrying no identity unexpectedly admitted" in integration
    assert "malformed observation identity hash unexpectedly admitted" in integration
    assert "duplicate hash-identified membership unexpectedly admitted" in integration
    assert (
        "source claim member carrying an observation identity unexpectedly admitted"
        in integration
    )
    assert "observation member carrying no identity unexpectedly admitted" in (
        integration
    )
