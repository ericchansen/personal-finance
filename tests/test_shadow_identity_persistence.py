"""The sealed apply must persist the exact identity generation it published.

PostgreSQL cannot become the durable authority for canonical identity while the
apply writes ledger rows and leaves the identity tables empty: the two would
disagree the moment anyone queried them.  Persisting *some* resolution is no
better, because a second derivation of the same evidence is only equal by
accident.  These regressions pin the one safe arrangement -- the canonical
producer publishes the exact scope it resolved, the plan seals the resulting
hashes and counts, and the apply replays that scope and refuses unless every
hash matches before it writes inside the same transaction as the ledger.

Every fixture here is invented.  No real institution, account, balance,
merchant, or transaction appears anywhere in this file.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from finance_store import canonical_identity, shadow
from finance_store.identity_postgres import PersistedIdentityGeneration
from importers.lineage_review import canonical
from importers.normalized import builder as normalized

ACCOUNT = "SYN-ACCOUNT"
QFX_FILE = "extracts/synthetic/statement.qfx"

# One authoritative statement row, one legacy export of the same purchase two
# days later, and one unrelated authoritative row that nothing may touch.
SPEC = (
    ("qfx", "extract:stable:QFX-1", "2026-01-15", "-42.00", "Synthetic Merchant", ""),
    ("monarch", "monarch:MON-1", "2026-01-17", "-42.00", "Synthetic Merchant",
     "Household Supplies"),
    ("qfx", "extract:stable:QFX-2", "2026-01-20", "-7.50", "Synthetic Grocer", ""),
)


def canonical_rows() -> list[dict[str, object]]:
    return [
        {
            "account_id": ACCOUNT,
            "date": day,
            "amount": amount,
            "currency": "USD",
            "description": description,
            "category": category,
            "source_id": source_id,
            "source_file": QFX_FILE if family == "qfx" else "",
        }
        for family, source_id, day, amount, description, category in SPEC
    ]


def coverage_interval(*, family: str, strength: str, count: int, tolerance: int = 0):
    return {
        "canonical_account_id": ACCOUNT,
        "effective_from": "2026-01-01",
        "effective_through": "2026-01-31",
        "source_family": family,
        "source_connection_id": (
            "unscoped:monarch" if family == "monarch" else f"{family}-account-scoped"
        ),
        "source_account_id": ACCOUNT,
        "format_strength": strength,
        "stable_id_support": family != "monarch",
        "replay_stable_ids": True,
        "extraction_requested_from": "2026-01-01",
        "extraction_requested_through": "2026-01-31",
        "extracted_at": "2026-02-10T00:00:00+00:00",
        "freshness_as_of": "2026-02-10T00:00:00+00:00",
        "completeness": "complete",
        "source_transaction_count": count,
        "source_hashes": [hashlib.sha256(f"SYN-{family}".encode()).hexdigest()],
        "trust_cutoff_day": None,
        "posting_date_tolerance_days": tolerance,
    }


def write_declarations(root: Path) -> None:
    (root / "identity").mkdir(parents=True, exist_ok=True)
    (root / "identity" / "source-authority.json").write_text(
        json.dumps(
            {
                "coverageIntervals": [
                    coverage_interval(
                        family="qfx",
                        strength="stable-provider-id",
                        count=2,
                        tolerance=2,
                    ),
                    coverage_interval(
                        family="monarch", strength="legacy-export", count=1
                    ),
                ]
            }
        ),
        encoding="utf-8",
    )


def publish(root: Path, *, rows=None, declared: bool = True) -> dict[str, object]:
    """Write a canonical publication exactly as the producer would."""

    if declared:
        write_declarations(root)
    declarations = canonical.declared_identity_inputs(root)
    projection = canonical._automatic_identity_projection(
        list(rows if rows is not None else canonical_rows()),
        set(),
        declarations.duplicate_summaries,
        declarations.token_scopes,
        declarations.policy,
    )
    directory = root / "normalized" / "canonical"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "transaction-observations.json").write_text(
        json.dumps(
            {
                "schemaVersion": normalized.SCHEMA_VERSION,
                "kind": "canonical-transaction-observations",
                "private": True,
                "observationCount": 0,
                "observations": [],
                "identityScope": projection["identityScope"],
            }
        ),
        encoding="utf-8",
    )
    (directory / "manifest.json").write_text(
        json.dumps(
            {
                "schemaVersion": normalized.SCHEMA_VERSION,
                "lineageReview": {"identityPolicy": projection["identity"]},
            }
        ),
        encoding="utf-8",
    )
    return projection


def rewrite(root: Path, name: str, mutate) -> None:
    path = root / "normalized" / "canonical" / name
    document = json.loads(path.read_text(encoding="utf-8"))
    mutate(document)
    path.write_text(json.dumps(document), encoding="utf-8")


class Cursor:
    def __init__(self, row=None):
        self.row = row

    def fetchone(self):
        return self.row

    def fetchall(self):
        return [] if self.row is None else [self.row]


class RecordingConnection:
    """Answers only the read-back the apply performs; records every write."""

    def __init__(self, row):
        self.row = row
        self.calls: list[str] = []
        self.written: list[tuple[str, tuple]] = []

    def execute(self, query, parameters=()):
        collapsed = " ".join(query.split())
        self.calls.append(collapsed)
        self.written.append((collapsed, tuple(parameters)))
        if "SELECT generation_number, canonical_state_hash, policy_hash" in collapsed:
            return Cursor(self.row)
        return Cursor()


class Unit:
    def __init__(self, connection):
        self.connection = connection


def persisted(proof, *, inserted: bool = True, **overrides):
    values = {
        "policy_id": "policy",
        "generation_id": "generation",
        "generation_number": 1,
        "generation_hash": proof["generationHash"],
        "inserted": inserted,
        "claim_count": proof["claims"],
        "event_count": proof["canonicalEvents"],
        "decision_count": proof["decisions"],
    }
    values.update(overrides)
    return PersistedIdentityGeneration(**values)


def stub_persistence(monkeypatch, generation, recorder=None):
    def _persist(connection, resolution):
        if recorder is not None:
            recorder.append((connection, resolution))
        return generation

    monkeypatch.setattr(shadow, "persist_identity_resolution", _persist)


# --- The publication carries an exactly replayable scope -------------------


def test_the_published_scope_replays_to_the_published_generation(tmp_path):
    projection = publish(tmp_path)
    verified = canonical_identity.verified_resolution(tmp_path)

    identity = projection["identity"]
    assert verified.proof["generationHash"] == identity["generationHash"]
    assert verified.proof["canonicalStateHash"] == identity["canonicalStateHash"]
    assert verified.proof["policyHash"] == identity["policyHash"]
    assert (
        verified.proof["appliedAutomaticDecisions"]
        == identity["appliedAutomaticDecisions"]
    )
    assert verified.proof["identityScopeHash"] == (
        projection["identityScope"]["scopeHash"]
    )


def test_the_published_scope_proves_the_authority_suppression_happened(tmp_path):
    projection = publish(tmp_path)

    identity = projection["identity"]
    assert identity["sourceSuppressedClaims"] == 1
    assert identity["unresolvedDuplicateGroups"] == 0
    assert identity["authorityAmbiguousGroups"] == 0
    verified = canonical_identity.verified_resolution(tmp_path)
    assert len(verified.resolution.canonical_events) == 2


def test_replaying_the_scope_twice_is_stable(tmp_path):
    publish(tmp_path)

    first = canonical_identity.verified_resolution(tmp_path)
    second = canonical_identity.verified_resolution(tmp_path)

    assert first.proof == second.proof


# --- Nothing is guessed when the evidence is absent or altered -------------


def test_a_publication_without_an_identity_scope_blocks_the_plan(tmp_path):
    publish(tmp_path)
    rewrite(tmp_path, "transaction-observations.json", lambda doc: doc.pop("identityScope"))

    binding, blockers = canonical_identity.plan_binding(tmp_path)

    assert binding is None
    assert blockers == ["canonical-identity-scope-missing"]


def test_a_scope_whose_hash_no_longer_covers_its_rows_is_refused(tmp_path):
    publish(tmp_path)

    def tamper(document):
        document["identityScope"]["rows"][0]["amount"] = "-99.00"

    rewrite(tmp_path, "transaction-observations.json", tamper)

    with pytest.raises(canonical_identity.CanonicalIdentityError) as error:
        canonical_identity.verified_resolution(tmp_path)
    assert error.value.code == "canonical-identity-scope-invalid"


def test_a_scope_that_replays_to_another_generation_is_refused(tmp_path):
    publish(tmp_path)

    def tamper(document):
        scope = document["identityScope"]
        scope["rows"][0]["description"] = "Synthetic Other Merchant"
        scope["scopeHash"] = canonical.content_hash(scope["rows"])

    rewrite(tmp_path, "transaction-observations.json", tamper)

    with pytest.raises(canonical_identity.CanonicalIdentityError) as error:
        canonical_identity.verified_resolution(tmp_path)
    assert error.value.code == "canonical-identity-generation-drift"


def test_a_dropped_scope_row_is_refused_as_a_count_or_generation_drift(tmp_path):
    publish(tmp_path)

    def tamper(document):
        scope = document["identityScope"]
        scope["rows"] = scope["rows"][:-1]
        scope["rowCount"] = len(scope["rows"])
        scope["scopeHash"] = canonical.content_hash(scope["rows"])

    rewrite(tmp_path, "transaction-observations.json", tamper)

    with pytest.raises(canonical_identity.CanonicalIdentityError) as error:
        canonical_identity.verified_resolution(tmp_path)
    assert error.value.code in {
        "canonical-identity-generation-drift",
        "canonical-identity-count-drift",
    }


def test_a_rewritten_published_policy_hash_is_refused(tmp_path):
    publish(tmp_path)

    def tamper(document):
        document["lineageReview"]["identityPolicy"]["canonicalStateHash"] = "0" * 64

    rewrite(tmp_path, "manifest.json", tamper)

    with pytest.raises(canonical_identity.CanonicalIdentityError) as error:
        canonical_identity.verified_resolution(tmp_path)
    assert error.value.code == "canonical-identity-generation-drift"


def test_a_publication_without_a_lineage_binding_blocks_the_plan(tmp_path):
    publish(tmp_path)
    rewrite(tmp_path, "manifest.json", lambda doc: doc.pop("lineageReview"))

    _binding, blockers = canonical_identity.plan_binding(tmp_path)

    assert blockers == ["canonical-lineage-binding-missing"]


def test_an_unresolved_residual_refuses_durable_persistence(tmp_path):
    # Two undeclared sources of one purchase: conservative, and not durable.
    publish(tmp_path, declared=False)

    _binding, blockers = canonical_identity.plan_binding(tmp_path)

    assert blockers == ["canonical-identity-unresolved-residual"]


# --- The plan seals the binding --------------------------------------------


def test_the_plan_binding_reports_the_generation_and_no_blockers(tmp_path):
    projection = publish(tmp_path)

    binding, blockers = canonical_identity.plan_binding(tmp_path)

    assert blockers == []
    assert binding["generationHash"] == projection["identity"]["generationHash"]
    assert binding["claims"] > 0
    assert binding["canonicalEvents"] == 2


def test_the_plan_body_binds_identity_counts_and_blocks_when_absent(tmp_path):
    publish(tmp_path)
    binding, _blockers = canonical_identity.plan_binding(tmp_path)
    catalog = Catalog()

    ready = shadow._plan_body(
        tmp_path, Status(), NOW, State(), State(), catalog, binding, []
    )
    blocked = shadow._plan_body(
        tmp_path,
        Status(),
        NOW,
        State(),
        State(),
        catalog,
        None,
        ["canonical-identity-scope-missing"],
    )

    assert ready["ready"] is True
    assert ready["identity"] == binding
    assert ready["counts"]["identityCanonicalEvents"] == binding["canonicalEvents"]
    assert ready["counts"]["identityDecisions"] == binding["decisions"]
    assert blocked["ready"] is False
    assert blocked["identity"] is None
    assert blocked["blockers"] == ["canonical-identity-scope-missing"]
    assert blocked["counts"]["blockers"] == 1


# --- The apply persists inside the sealed transaction ----------------------


def test_the_apply_persists_the_bound_generation(tmp_path, monkeypatch):
    publish(tmp_path)
    binding, _ = canonical_identity.plan_binding(tmp_path)
    generation = persisted(binding)
    calls: list = []
    stub_persistence(monkeypatch, generation, calls)
    connection = RecordingConnection(
        (1, binding["canonicalStateHash"], binding["policyHash"])
    )

    result = shadow._persist_canonical_identity(
        Unit(connection), tmp_path, {"identity": binding}
    )

    assert calls and calls[0][0] is connection
    assert result["generationHash"] == binding["generationHash"]
    assert result["generationNumber"] == 1
    assert result["inserted"] is True
    assert result["identityScopeHash"] == binding["identityScopeHash"]


def test_replaying_an_applied_generation_persists_nothing_new(tmp_path, monkeypatch):
    publish(tmp_path)
    binding, _ = canonical_identity.plan_binding(tmp_path)
    stub_persistence(monkeypatch, persisted(binding, inserted=False))
    connection = RecordingConnection(
        (1, binding["canonicalStateHash"], binding["policyHash"])
    )

    result = shadow._persist_canonical_identity(
        Unit(connection), tmp_path, {"identity": binding}
    )

    assert result["inserted"] is False
    assert result["generationNumber"] == 1
    assert result["generationHash"] == binding["generationHash"]


def test_a_plan_without_an_identity_binding_is_refused(tmp_path):
    publish(tmp_path)

    with pytest.raises(shadow.ShadowSafetyError, match="no canonical identity"):
        shadow._persist_canonical_identity(
            Unit(RecordingConnection(None)), tmp_path, {}
        )


def test_a_publication_that_changed_after_planning_is_refused(tmp_path, monkeypatch):
    publish(tmp_path)
    binding, _ = canonical_identity.plan_binding(tmp_path)
    stub_persistence(monkeypatch, persisted(binding))
    drifted = {**binding, "generationHash": "0" * 64}

    with pytest.raises(shadow.ShadowSafetyError, match="changed after planning"):
        shadow._persist_canonical_identity(
            Unit(RecordingConnection(None)), tmp_path, {"identity": drifted}
        )


def test_a_publication_that_became_unreadable_refuses_the_apply(tmp_path):
    publish(tmp_path)
    binding, _ = canonical_identity.plan_binding(tmp_path)
    rewrite(tmp_path, "transaction-observations.json", lambda doc: doc.pop("identityScope"))

    with pytest.raises(shadow.ShadowSafetyError, match="canonical-identity-scope-missing"):
        shadow._persist_canonical_identity(
            Unit(RecordingConnection(None)), tmp_path, {"identity": binding}
        )


def test_a_writer_reporting_other_counts_refuses_the_apply(tmp_path, monkeypatch):
    publish(tmp_path)
    binding, _ = canonical_identity.plan_binding(tmp_path)
    stub_persistence(monkeypatch, persisted(binding, event_count=99))
    connection = RecordingConnection(
        (1, binding["canonicalStateHash"], binding["policyHash"])
    )

    with pytest.raises(shadow.ShadowSafetyError, match="differs from the plan"):
        shadow._persist_canonical_identity(
            Unit(connection), tmp_path, {"identity": binding}
        )


def test_a_missing_read_back_refuses_the_apply(tmp_path, monkeypatch):
    publish(tmp_path)
    binding, _ = canonical_identity.plan_binding(tmp_path)
    stub_persistence(monkeypatch, persisted(binding))

    with pytest.raises(shadow.ShadowSafetyError, match="was not persisted"):
        shadow._persist_canonical_identity(
            Unit(RecordingConnection(None)), tmp_path, {"identity": binding}
        )


def test_a_read_back_that_disagrees_refuses_the_apply(tmp_path, monkeypatch):
    publish(tmp_path)
    binding, _ = canonical_identity.plan_binding(tmp_path)
    stub_persistence(monkeypatch, persisted(binding))
    connection = RecordingConnection((1, "0" * 64, binding["policyHash"]))

    with pytest.raises(shadow.ShadowSafetyError, match="differs from the plan"):
        shadow._persist_canonical_identity(
            Unit(connection), tmp_path, {"identity": binding}
        )


def test_the_apply_persists_identity_before_the_success_record():
    source = Path(shadow.__file__).read_text(encoding="utf-8")
    body = source.split("def _apply_ready_plan", 1)[1]

    assert body.index("_persist_canonical_identity") < body.index(
        "_insert_shadow_control"
    )
    assert "persist_identity_resolution" not in body.split("return {", 1)[1]


def test_the_success_record_carries_the_persisted_generation_proof(tmp_path):
    publish(tmp_path)
    binding, _ = canonical_identity.plan_binding(tmp_path)
    identity = {
        **binding,
        "generationNumber": 4,
        "inserted": True,
    }
    plan = {
        "planHash": "p" * 64,
        "counts": {"sourceFiles": 0, "observations": 0, "openQualityIssues": 0},
        "inputSetHash": "i" * 64,
        "migrationSetHash": "m" * 64,
        "startingStateHash": "s" * 64,
        "expectedStateHash": "e" * 64,
        "environmentMarker": "shadow-authority-v1",
        "generatedAt": NOW.isoformat(),
    }
    connection = RecordingConnection(None)

    shadow._insert_shadow_control(Unit(connection), plan, Catalog(), identity)

    event = next(
        call for call in connection.written if "shadow_run_events" in call[0]
    )
    counts = json.loads(event[1][2])
    assert counts["identityGenerationNumber"] == 4
    assert counts["identityPersistedEvents"] == binding["canonicalEvents"]
    assert counts["identityPersistedDecisions"] == binding["decisions"]
    assert counts["identityPersistedClaims"] == binding["claims"]


def test_the_sealed_plan_record_keeps_the_counts_it_sealed(tmp_path):
    publish(tmp_path)
    binding, _ = canonical_identity.plan_binding(tmp_path)
    plan = {
        "planHash": "p" * 64,
        "counts": {"sourceFiles": 3, "observations": 7, "openQualityIssues": 0},
        "inputSetHash": "i" * 64,
        "migrationSetHash": "m" * 64,
        "startingStateHash": "s" * 64,
        "expectedStateHash": "e" * 64,
        "environmentMarker": "shadow-authority-v1",
        "generatedAt": NOW.isoformat(),
    }
    connection = RecordingConnection(None)

    shadow._insert_shadow_control(
        Unit(connection), plan, Catalog(), {**binding, "generationNumber": 1}
    )

    record = next(
        call for call in connection.written if "shadow_plans" in call[0]
    )
    assert 3 in record[1] and 7 in record[1]


# --- Supporting stand-ins for the plan body --------------------------------

NOW = __import__("datetime").datetime(
    2026, 2, 20, tzinfo=__import__("datetime").timezone.utc
)


class Status:
    environment_marker = "shadow-authority-v1"
    database_fingerprint = "f" * 64
    migration_set_hash = "m" * 64


class State:
    """A real empty finance state; the plan body hashes it."""

    def __new__(cls):
        from finance_store.domain import FinanceState

        return FinanceState()


class Catalog:
    files: tuple = ()
    batches: tuple = ()
    observation_count = 0
    lineage_groups: tuple = ()
    lineage_decisions: tuple = ()
    lineage_review_counts: dict = {}
    lineage_readiness_counts: dict = {}
    source_counts: dict = {}
    blockers: tuple = ()
    gaps: tuple = ()
