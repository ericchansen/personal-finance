"""Institution-scoped admission for the SimpleFIN v1 envelope.

SimpleFIN v1 has no connection object: one response carries every institution,
accounts have no ``conn_id``, and the request sidecar names no connection.  A
path-level or default-scope evaluator therefore cannot fail one institution
over while its siblings advance -- it can only choose whole files.  These
regressions pin the organization-scoped behaviour that replaces it, and pin
just as hard the cases where an error must stay a global blocker rather than be
guessed onto an institution.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from finance_store.simplefin import scoped_account_identity
from finance_store.source_admission import (
    DEFAULT_CONNECTION_ID,
    SnapshotEvidence,
    evaluate_connection_scopes,
    normalize_institution_name,
    organization_scope_id,
    parse_connection_decisions,
    partition_snapshot,
    partitioned_evidence_from_paths,
    scope_provider_errors,
)
from importers.normalized import builder as normalized

from tests.test_normalized import collect, make_estate, write


NOW = datetime(2026, 9, 4, tzinfo=timezone.utc)

# The exact provider string, reproduced verbatim from the real error class.
AUTH_REQUIRED = "Connection to {name} may need attention. Auth required"
ADVISORY = (
    "Requested date range exceeds recommended range of 45 days. "
    "In the future, this may be capped."
)


def org_account(index: int) -> dict:
    """One account at its own institution, in the real v1 shape."""

    return {
        "id": f"source-{index:02d}",
        "name": f"Synthetic Account {index:02d}",
        "org": {
            "name": f"Synthetic Institution {index:02d}",
            "domain": f"institution-{index:02d}.invalid",
        },
        "currency": "USD",
        "balance": "1.00",
        "balance-date": 1788220800,
        "transactions": [],
    }


def scope_of(index: int) -> str:
    return organization_scope_id(org_account(index), "1")


# ---------------------------------------------------------------------------
# Deterministic scope derivation
# ---------------------------------------------------------------------------


def test_the_scope_uses_the_same_basis_as_scoped_account_identity():
    """An account and its institution scope can never disagree."""

    account = org_account(1)
    identity = scoped_account_identity(account, "1")
    scope = organization_scope_id(account, "1")
    assert identity.split(":")[0] == scope.removeprefix("org:")


def test_the_scope_never_contains_an_institution_name():
    account = org_account(1)
    scope = organization_scope_id(account, "1")
    assert "Synthetic Institution" not in scope
    assert "institution-01.invalid" not in scope


def test_thirty_accounts_partition_into_thirty_institution_scopes():
    """The real envelope: one file, thirty orgs, no connections, no conn_id."""

    accounts = [org_account(index) for index in range(1, 31)]
    partition = partition_snapshot(
        snapshot_sha256="sha-1",
        observed_at=NOW,
        version="1",
        accounts=accounts,
        errors=[],
    )
    scopes = {item.connection_id for item in partition.evidence}
    # Thirty institutions plus the always-present global scope.
    assert scopes == {scope_of(index) for index in range(1, 31)} | {
        DEFAULT_CONNECTION_ID
    }
    assigned = [
        account_id
        for ids in partition.accounts_by_scope.values()
        for account_id in ids
    ]
    # Every account belongs to exactly one scope: subsets never overlap.
    assert sorted(assigned) == sorted({account["id"] for account in accounts})
    assert len(assigned) == 30


def test_an_account_without_an_organization_falls_back_to_the_global_scope():
    partition = partition_snapshot(
        snapshot_sha256="sha-1",
        observed_at=NOW,
        version="1",
        accounts=[{"id": "source-orphan", "name": "Orphan"}],
        errors=[],
    )
    assert partition.accounts_by_scope[DEFAULT_CONNECTION_ID] == ("source-orphan",)


def test_a_declared_connection_id_still_wins_over_the_organization():
    """An explicit human declaration is stronger than anything inferred."""

    partition = partition_snapshot(
        snapshot_sha256="sha-1",
        observed_at=NOW,
        version="1",
        accounts=[org_account(1), org_account(2)],
        errors=[],
        declared_connection="alpha",
    )
    assert [item.connection_id for item in partition.evidence] == ["alpha"]
    assert partition.accounts_by_scope["alpha"] == ("source-01", "source-02")


# ---------------------------------------------------------------------------
# Error attribution: exact, never fuzzy
# ---------------------------------------------------------------------------


def test_an_error_naming_exactly_one_institution_is_scoped_to_it():
    accounts = [org_account(1), org_account(2)]
    error = AUTH_REQUIRED.format(name="Synthetic Institution 02")
    scoped = scope_provider_errors([error], accounts, "1")
    assert scoped.by_scope == {scope_of(2): (error,)}
    assert scoped.unscopable == ()


def test_an_error_naming_no_institution_stays_global():
    accounts = [org_account(1), org_account(2)]
    scoped = scope_provider_errors([ADVISORY], accounts, "1")
    assert scoped.by_scope == {}
    assert scoped.unscopable == (ADVISORY,)


def test_an_error_naming_two_institutions_stays_global():
    """Two candidates is not a tie to break; it is a refusal to guess."""

    accounts = [org_account(1), org_account(2)]
    error = (
        "Connection to Synthetic Institution 01 and Synthetic Institution 02 "
        "may need attention. Auth required"
    )
    scoped = scope_provider_errors([error], accounts, "1")
    assert scoped.by_scope == {}
    assert scoped.unscopable == (error,)


def test_a_partial_institution_name_never_scopes_an_error():
    """No prefix, no token-subset, no similarity: the whole name or nothing."""

    accounts = [org_account(1)]
    error = AUTH_REQUIRED.format(name="Synthetic Institution")
    scoped = scope_provider_errors([error], accounts, "1")
    assert scoped.unscopable == (error,)


def test_a_name_inside_a_longer_token_never_scopes_an_error():
    accounts = [
        {
            "id": "source-a",
            "org": {"name": "Bravo", "domain": "bravo.invalid"},
        }
    ]
    error = "Connection to Bravocorp may need attention. Auth required"
    scoped = scope_provider_errors([error], accounts, "1")
    assert scoped.unscopable == (error,)


def test_punctuation_and_case_do_not_defeat_an_exact_name():
    accounts = [
        {"id": "source-a", "org": {"name": "Example Bank, N.A.", "domain": "a.invalid"}}
    ]
    error = "Connection to EXAMPLE BANK N A may need attention. Auth required"
    scoped = scope_provider_errors([error], accounts, "1")
    assert list(scoped.by_scope) == [
        organization_scope_id(accounts[0], "1")
    ]


def test_normalization_is_only_about_punctuation_and_case():
    assert normalize_institution_name("Example Bank, N.A.") == "example bank n a"
    assert normalize_institution_name(None) == ""


def test_a_structured_error_scopes_by_its_own_organization():
    accounts = [org_account(1), org_account(2)]
    error = {
        "message": "needs attention",
        "org": {"name": "Synthetic Institution 02", "domain": "institution-02.invalid"},
    }
    scoped = scope_provider_errors([error], accounts, "1")
    assert scoped.by_scope == {scope_of(2): ("needs attention",)}


def test_an_unscopable_error_blocks_every_scope_through_the_global_scope():
    partition = partition_snapshot(
        snapshot_sha256="sha-1",
        observed_at=NOW,
        version="1",
        accounts=[org_account(1), org_account(2)],
        errors=["Connection to Someone Else may need attention. Auth required"],
    )
    by_scope = {item.connection_id: item.errors for item in partition.evidence}
    assert by_scope[scope_of(1)] == ()
    assert by_scope[scope_of(2)] == ()
    assert len(by_scope[DEFAULT_CONNECTION_ID]) == 1
    scopes = evaluate_connection_scopes(
        {
            item.connection_id: [item]
            for item in partition.evidence
        },
        {},
        as_of=NOW,
    )
    blocked = [item.connection_id for item in scopes.admissions if item.blocker]
    assert blocked == [DEFAULT_CONNECTION_ID]


# ---------------------------------------------------------------------------
# Per-institution selection across files
# ---------------------------------------------------------------------------


def write_snapshot(root: Path, *, day: str, stem: str, accounts, errors=()) -> Path:
    path = root / "raw" / "simplefin" / day / f"simplefin-{stem}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"accounts": list(accounts), "errors": list(errors)}, indent=2),
        encoding="utf-8",
    )
    return path


def view(path: Path):
    document = json.loads(path.read_text(encoding="utf-8"))
    return "1", document["accounts"], document["errors"]


def test_one_institution_falls_back_while_its_siblings_advance(tmp_path):
    """The requirement: per-connection selection, never one file globally."""

    root = tmp_path / "private"
    healthy = org_account(1)
    failing = org_account(2)
    first = write_snapshot(
        root, day="2026-09-01", stem="000001", accounts=[healthy, failing]
    )
    second = write_snapshot(
        root,
        day="2026-09-02",
        stem="000002",
        accounts=[healthy, failing],
        errors=[AUTH_REQUIRED.format(name="Synthetic Institution 02")],
    )
    evidence = partitioned_evidence_from_paths(
        [first, second], fallback=NOW, view_for=view
    )
    current_error = SnapshotEvidence(
        connection_id=scope_of(2),
        snapshot_sha256="ignored",
        observed_at=NOW,
        errors=(AUTH_REQUIRED.format(name="Synthetic Institution 02"),),
        requested_start=None,
        requested_end=None,
    ).error_hash
    decisions = parse_connection_decisions(
        {
            scope_of(2): {
                "action": "fallback",
                "decision": "Institution re-auth in flight; prior snapshot stands.",
                "decidedAt": "2026-09-03",
                "currentErrorHash": current_error,
                "fallbackSnapshotSha256": hashlib.sha256(
                    first.read_bytes()
                ).hexdigest(),
                "maxStalenessDays": 30,
            }
        }
    )
    scopes = evaluate_connection_scopes(evidence.grouped, decisions, as_of=NOW)
    assert scopes.blockers == ()
    admitted = {item.connection_id: item for item in scopes.admitted}
    # The healthy institution advances to the newest file on its own.
    assert admitted[scope_of(1)].admitted_snapshot_sha256 == hashlib.sha256(
        second.read_bytes()
    ).hexdigest()
    assert admitted[scope_of(1)].stale is True
    assert admitted[scope_of(1)].staleness_days == 2
    # The failing institution alone falls back, on its own decision.
    assert admitted[scope_of(2)].admitted_snapshot_sha256 == hashlib.sha256(
        first.read_bytes()
    ).hexdigest()
    assert admitted[scope_of(2)].stale is True


def test_institution_missing_from_later_clean_snapshot_stays_admitted_but_stale(
    tmp_path,
):
    root = tmp_path / "private"
    retained = org_account(1)
    missing = org_account(2)
    first = write_snapshot(
        root, day="2026-09-01", stem="000001", accounts=[retained, missing]
    )
    second = write_snapshot(
        root, day="2026-09-04", stem="000002", accounts=[retained]
    )
    evidence = partitioned_evidence_from_paths(
        [first, second], fallback=NOW, view_for=view
    )

    scopes = evaluate_connection_scopes(
        evidence.grouped,
        {},
        as_of=datetime(2026, 9, 4, 12, tzinfo=timezone.utc),
    )
    admitted = {item.connection_id: item for item in scopes.admitted}

    assert admitted[scope_of(1)].fresh is True
    assert admitted[scope_of(2)].fresh is False
    assert admitted[scope_of(2)].stale is True
    assert admitted[scope_of(2)].staleness_days == 3
    assert (
        admitted[scope_of(2)].gap
        == "simplefin-connection-latest-clean-stale"
    )
    assert admitted[scope_of(2)].staleness_days == 3
    # Account subsets are merged per institution, so nothing is loaded twice.
    loaded = [
        account_id
        for scope, item in admitted.items()
        for account_id in evidence.accounts[
            (scope, item.admitted_snapshot_sha256 or "")
        ]
    ]
    assert sorted(loaded) == ["source-01", "source-02"]


def test_a_recovered_institution_supersedes_its_fallback(tmp_path):
    root = tmp_path / "private"
    healthy = org_account(1)
    failing = org_account(2)
    write_snapshot(root, day="2026-09-01", stem="000001", accounts=[healthy, failing])
    write_snapshot(
        root,
        day="2026-09-02",
        stem="000002",
        accounts=[healthy, failing],
        errors=[AUTH_REQUIRED.format(name="Synthetic Institution 02")],
    )
    recovered = write_snapshot(
        root, day="2026-09-03", stem="000003", accounts=[healthy, failing]
    )
    paths = sorted((root / "raw" / "simplefin").rglob("simplefin-*.json"))
    evidence = partitioned_evidence_from_paths(paths, fallback=NOW, view_for=view)
    # No decision at all: clean current evidence needs none.
    scopes = evaluate_connection_scopes(evidence.grouped, {}, as_of=NOW)
    assert scopes.blockers == ()
    admitted = {item.connection_id: item for item in scopes.admitted}
    recovered_sha = hashlib.sha256(recovered.read_bytes()).hexdigest()
    assert admitted[scope_of(2)].admitted_snapshot_sha256 == recovered_sha
    assert admitted[scope_of(2)].stale is False
    assert admitted[scope_of(2)].decision is None
    # The error history is superseded, never erased.
    assert admitted[scope_of(2)].gap == (
        "simplefin-historical-connection-error-superseded"
    )
    assert len(admitted[scope_of(2)].superseded_error_snapshots) == 1
    assert admitted[scope_of(1)].superseded_error_snapshots == ()


def test_a_failing_institution_without_a_decision_blocks_only_itself(tmp_path):
    root = tmp_path / "private"
    first = write_snapshot(
        root, day="2026-09-01", stem="000001", accounts=[org_account(1), org_account(2)]
    )
    second = write_snapshot(
        root,
        day="2026-09-02",
        stem="000002",
        accounts=[org_account(1), org_account(2)],
        errors=[AUTH_REQUIRED.format(name="Synthetic Institution 02")],
    )
    evidence = partitioned_evidence_from_paths(
        [first, second], fallback=NOW, view_for=view
    )
    scopes = evaluate_connection_scopes(evidence.grouped, {}, as_of=NOW)
    assert scopes.blockers == (
        f"{scope_of(2)}: simplefin-connection-error-undecided",
    )
    assert scope_of(1) in {item.connection_id for item in scopes.admitted}


def test_a_decision_for_one_institution_is_never_spent_on_another(tmp_path):
    root = tmp_path / "private"
    first = write_snapshot(
        root, day="2026-09-01", stem="000001", accounts=[org_account(1), org_account(2)]
    )
    second = write_snapshot(
        root,
        day="2026-09-02",
        stem="000002",
        accounts=[org_account(1), org_account(2)],
        errors=[AUTH_REQUIRED.format(name="Synthetic Institution 02")],
    )
    evidence = partitioned_evidence_from_paths(
        [first, second], fallback=NOW, view_for=view
    )
    decisions = parse_connection_decisions(
        {
            scope_of(1): {
                "action": "fallback",
                "decision": "Wrong institution.",
                "decidedAt": "2026-09-03",
                "currentErrorHash": "whatever",
                "fallbackSnapshotSha256": hashlib.sha256(
                    first.read_bytes()
                ).hexdigest(),
                "maxStalenessDays": 30,
            }
        }
    )
    scopes = evaluate_connection_scopes(evidence.grouped, decisions, as_of=NOW)
    assert scopes.blockers == (
        f"{scope_of(2)}: simplefin-connection-error-undecided",
    )


# ---------------------------------------------------------------------------
# Canonical builder, real multi-institution envelope
# ---------------------------------------------------------------------------


def estate_snapshot(root: Path) -> dict:
    path = root / "raw" / "simplefin" / "2024-02-01" / "simplefin-000001.json"
    return json.loads(path.read_text(encoding="utf-8"))


BANK_SCOPE = organization_scope_id({"org": {"name": "Example Bank"}}, "1")
CARD_SCOPE = organization_scope_id({"org": {"name": "Example Card"}}, "1")


def test_the_builder_selects_one_snapshot_per_institution(tmp_path):
    """One v1 file, two institutions, one failing: only that one falls back."""

    root = make_estate(tmp_path)
    document = estate_snapshot(root)
    later = dict(document)
    later["errors"] = [AUTH_REQUIRED.format(name="Example Card")]
    path = write(root / "raw" / "simplefin" / "2024-02-02" / "simplefin-000002.json", later)
    mapping_path = root / "simplefin" / "account-map.json"
    mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
    mapping["connections"] = {
        CARD_SCOPE: {
            "action": "fallback",
            "decision": "Card issuer re-auth in flight; prior snapshot stands.",
            "decidedAt": "2024-02-02",
            "currentErrorHash": SnapshotEvidence(
                connection_id=CARD_SCOPE,
                snapshot_sha256="ignored",
                observed_at=NOW,
                errors=(AUTH_REQUIRED.format(name="Example Card"),),
                requested_start=None,
                requested_end=None,
            ).error_hash,
            "fallbackSnapshotSha256": hashlib.sha256(
                (
                    root / "raw" / "simplefin" / "2024-02-01" / "simplefin-000001.json"
                ).read_bytes()
            ).hexdigest(),
            "maxStalenessDays": 36500,
        }
    }
    write(mapping_path, mapping)
    admitted = normalized._admitted_simplefin_snapshots(
        sorted((root / "raw" / "simplefin").rglob("simplefin-*.json")), mapping
    )
    selected = {scope: file for scope, file, *_ in admitted}
    stale = {scope: item.stale for scope, _, item, *_ in admitted}
    assert selected[BANK_SCOPE] == path
    assert stale[BANK_SCOPE] is True
    assert selected[CARD_SCOPE].name == "simplefin-000001.json"
    assert stale[CARD_SCOPE] is True
    # Each institution contributes only its own accounts.
    subsets = {scope: ids for scope, _, _, ids, _ in admitted}
    assert subsets[BANK_SCOPE] == ("source-main",)
    assert subsets[CARD_SCOPE] == ("source-corporate",)
    loaded = [account_id for ids in subsets.values() for account_id in ids]
    assert sorted(loaded) == ["source-corporate", "source-main"]


def test_the_canonical_build_loads_each_account_once_across_institutions(tmp_path):
    """Merging whole files would load every account once per institution."""

    root = make_estate(tmp_path)
    document = estate_snapshot(root)
    document["errors"] = []
    write(root / "raw" / "simplefin" / "2024-02-02" / "simplefin-000002.json", document)
    estate = collect(root)
    rows = [row for row in estate.accounts if row.account_id == "acct-main"]
    assert len(rows) == 1
    sources = [
        row
        for row in estate.transactions
        if row.source_id == "simplefin:source-main:simple-1"
    ]
    assert len(sources) == 1


def test_the_canonical_build_refuses_an_unscopable_institution_error(tmp_path):
    root = make_estate(tmp_path)
    document = estate_snapshot(root)
    document["errors"] = ["Connection to Someone Else may need attention. Auth required"]
    write(root / "raw" / "simplefin" / "2024-02-02" / "simplefin-000002.json", document)
    with pytest.raises(normalized.BuildError, match="not admitted"):
        collect(root)
