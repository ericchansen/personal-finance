"""Synthetic conformance tests for the PostgreSQL candidate's neutral core."""

from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from finance_store.memory import MemoryRepository
from finance_store.cli import private_data_dir, private_path, parse_trust_cutoffs
from finance_store.domain import AppStateSnapshot, DurableDecision, stable_id
from finance_store.reconcile import IngestionService, projection_hash
from finance_store.replay import export_state
from finance_store.simplefin import SimpleFinAdapter, SimpleFinAdapterError

T1 = datetime(2026, 9, 1, 12, tzinfo=timezone.utc)
T2 = datetime(2026, 9, 2, 12, tzinfo=timezone.utc)
DAY1 = 1788220800
DAY2 = 1788307200


def payload(
    transactions: list[dict],
    *,
    account_id: str = "synthetic-account-a",
    canonical_key: str = "household-checking",
    balance: str = "100.00",
    balance_date: int = DAY2,
) -> bytes:
    return json.dumps({
        "accounts": [{
            "id": account_id,
            "name": "Synthetic Checking",
            "canonical_key": canonical_key,
            "currency": "USD",
            "balance": balance,
            "balance-date": balance_date,
            "transactions": transactions,
        }],
        "errors": [],
    }, sort_keys=True).encode()


def transaction(
    source_id: str,
    *,
    amount: str = "-12.34",
    posted: int = DAY1,
    description: str = "Synthetic Market",
    pending: bool = False,
) -> dict:
    return {
        "id": source_id,
        "posted": posted,
        "amount": amount,
        "description": description,
        "pending": pending,
    }


def parse(
    raw: bytes,
    *,
    connection: str = "synthetic-connection-a",
    observed_at: datetime = T1,
    locator: str = "external://synthetic/simplefin-a",
    trust_cutoffs=None,
):
    return SimpleFinAdapter(connection).parse(
        raw,
        raw_locator=locator,
        observed_at=observed_at,
        trust_cutoffs=trust_cutoffs,
    )


def test_v1_and_v2_parse_to_the_same_domain_semantics():
    v1 = payload([transaction("txn-a")])
    v2 = json.dumps({
        "version": 2,
        "data": {
            "accounts": [{
                "account_id": "synthetic-account-a",
                "display_name": "Synthetic Checking",
                "canonical_key": "household-checking",
                "currency": "USD",
                "current_balance": "100.00",
                "balance_at": DAY2,
                "transactions": [{
                    "transaction_id": "txn-a",
                    "posted_at": DAY1,
                    "amount": "-12.34",
                    "payee": "Synthetic Market",
                    "status": "posted",
                }],
            }]
        },
    }, sort_keys=True).encode()
    one = parse(v1)
    two = parse(v2, locator="external://synthetic/simplefin-v2")
    assert one.transactions[0].semantic_key == two.transactions[0].semantic_key
    assert one.balances[0].amount == two.balances[0].amount == Decimal("100.00")
    assert one.run.source_protocol == two.run.source_protocol == "simplefin"
    assert one.run.source_version == "1"
    assert two.run.source_version == "2"
    assert len(one.run.parser_hash or "") == 64


def test_declared_simplefin_protocol_must_match_snapshot():
    with pytest.raises(SimpleFinAdapterError, match="declared"):
        SimpleFinAdapter("synthetic").parse(
            payload([]),
            raw_locator="external://synthetic/protocol-mismatch",
            observed_at=T1,
            protocol_version="2",
        )


def test_official_v2_scopes_accounts_by_connection_and_keeps_posted_zero_pending():
    raw = json.dumps(
        {
            "errlist": [],
            "connections": [
                {"conn_id": "connection-a"},
                {"conn_id": "connection-b"},
            ],
            "accounts": [
                {
                    "id": "shared-account-id",
                    "conn_id": "connection-a",
                    "name": "Synthetic A",
                    "currency": "USD",
                    "balance": "1",
                    "balance-date": DAY1,
                    "transactions": [
                        {
                            "id": "pending-a",
                            "posted": 0,
                            "amount": "-1",
                            "description": "Synthetic Pending",
                            "pending": True,
                        }
                    ],
                },
                {
                    "id": "shared-account-id",
                    "conn_id": "connection-b",
                    "name": "Synthetic B",
                    "currency": "USD",
                    "balance": "2",
                    "balance-date": DAY1,
                    "transactions": [],
                },
            ],
        },
        sort_keys=True,
    ).encode()
    batch = SimpleFinAdapter("official-v2").parse(
        raw,
        raw_locator="external://synthetic/official-v2",
        observed_at=T1,
        protocol_version="2",
    )
    assert len({account.id for account in batch.accounts}) == 2
    assert len({account.external_id for account in batch.accounts}) == 2
    assert batch.transactions[0].status == "pending"
    assert batch.transactions[0].effective_at == T1


def test_official_v2_errlist_is_fail_closed_input():
    raw = json.dumps(
        {
            "errlist": [
                {
                    "code": "con.auth",
                    "msg": "Synthetic authentication issue",
                    "conn_id": "connection-a",
                }
            ],
            "connections": [{"conn_id": "connection-a"}],
            "accounts": [],
        }
    ).encode()
    batch = SimpleFinAdapter("official-v2-error").parse(
        raw,
        raw_locator="external://synthetic/official-v2-error",
        observed_at=T1,
        protocol_version="2",
    )
    assert len(batch.errors) == 1
    assert batch.errors[0].startswith("con.auth:")


def test_raw_payload_content_is_not_carried_into_domain_records():
    raw = payload([transaction("txn-a", description="Synthetic Private-Like Text")])
    batch = parse(raw)
    assert batch.blob.content_hash
    assert batch.blob.raw_locator.startswith("external://")
    assert not hasattr(batch.blob, "payload")
    assert raw.decode() not in repr(batch)


def test_account_scoped_source_identity_preserves_same_id_in_two_accounts():
    first = parse(payload([transaction("shared-id")]))
    second = parse(
        payload(
            [transaction("shared-id", amount="-20.00")],
            account_id="synthetic-account-b",
            canonical_key="household-savings",
        ),
        observed_at=T2,
        locator="external://synthetic/simplefin-b",
    )
    repository = MemoryRepository()
    service = IngestionService(repository)
    service.ingest(first)
    service.ingest(second)
    state = repository.state()
    assert len(state.transaction_observations) == 2
    assert len(state.canonical_transactions) == 2


def test_pending_to_posted_updates_canonical_and_retains_both_observations():
    repository = MemoryRepository()
    service = IngestionService(repository)
    service.ingest(parse(payload([transaction("txn-a", pending=True)])))
    service.ingest(
        parse(
            payload([transaction("txn-a", pending=False, posted=DAY2)]),
            observed_at=T2,
            locator="external://synthetic/simplefin-a-next",
        )
    )
    state = repository.state()
    assert len(state.transaction_observations) == 2
    assert len(state.canonical_transactions) == 1
    assert state.canonical_transactions[0].status == "posted"
    assert len(state.links) == 2


def test_changed_posted_semantics_append_a_correction_decision():
    repository = MemoryRepository()
    service = IngestionService(repository)
    service.ingest(parse(payload([transaction("txn-a")])))
    service.ingest(
        parse(
            payload([transaction("txn-a", amount="-12.35", posted=DAY2)]),
            observed_at=T2,
            locator="external://synthetic/simplefin-correction",
        )
    )
    state = repository.state()
    assert len(state.transaction_observations) == 2
    assert state.canonical_transactions[0].amount == Decimal("-12.35")
    assert sum(
        decision.decision_type == "correction" for decision in state.decisions
    ) == 1
    assert any(link.method == "correction" for link in state.links)
    current = [record for record in state.projection_records if record.current]
    assert len(current) == 1
    assert sum(record.status == "superseded" for record in state.projection_records) == 1


def test_currency_correction_updates_canonical_atomically():
    repository = MemoryRepository()
    service = IngestionService(repository)
    service.ingest(parse(payload([transaction("currency-change", amount="1.00")])))
    changed = json.loads(payload([transaction("currency-change", amount="2.00")]))
    changed["accounts"][0]["transactions"][0]["currency"] = "EUR"
    service.ingest(
        parse(
            json.dumps(changed).encode(),
            observed_at=T2,
            locator="external://synthetic/currency-change",
        )
    )
    canonical = repository.state().canonical_transactions[0]
    assert canonical.amount == Decimal("2")
    assert canonical.currency == "EUR"


def test_overlap_replay_is_idempotent_and_state_hash_is_stable():
    repository = MemoryRepository()
    service = IngestionService(repository)
    batch = parse(payload([transaction("txn-a")]))
    first = service.ingest(batch)
    second = service.ingest(batch)
    assert second.replayed_observations == 1
    assert second.canonical_transactions_created == 0
    assert first.state_hash == second.state_hash
    assert len(repository.state().canonical_transactions) == 1


def test_changed_id_or_date_is_review_only_and_not_automatically_suppressed():
    repository = MemoryRepository()
    service = IngestionService(repository)
    service.ingest(parse(payload([transaction("pending-id", pending=True)])))
    service.ingest(
        parse(
            payload([transaction("posted-id", pending=False, posted=DAY2)]),
            observed_at=T2,
            locator="external://synthetic/simplefin-changed-id",
        )
    )
    state = repository.state()
    assert len(state.canonical_transactions) == 2
    assert all(item.status != "suppressed" for item in state.canonical_transactions)
    assert state.issues[-1].issue_type == "fuzzy_duplicate"
    assert state.issues[-1].details["policy"] == "review_only_no_automatic_suppression"


def test_legitimate_repeats_remain_distinct_even_when_fuzzy_candidate_is_opened():
    repository = MemoryRepository()
    result = IngestionService(repository).ingest(
        parse(payload([transaction("repeat-a"), transaction("repeat-b")]))
    )
    state = repository.state()
    assert result.canonical_transactions_created == 2
    assert len(state.canonical_transactions) == 2
    assert len(state.issues) == 1


def test_cross_source_candidate_remains_distinct_and_requires_review():
    repository = MemoryRepository()
    service = IngestionService(repository)
    service.ingest(parse(payload([transaction("source-a-id")])))
    service.ingest(
        parse(
            payload([transaction("source-b-id")]),
            connection="synthetic-connection-b",
            observed_at=T2,
            locator="external://synthetic/simplefin-source-b",
        )
    )
    state = repository.state()
    assert len(state.canonical_transactions) == 2
    assert any(issue.issue_type == "fuzzy_duplicate" for issue in state.issues)


def test_cross_account_mirrors_are_retained_and_flagged():
    raw = json.dumps({
        "accounts": [
            {
                "id": "synthetic-account-a",
                "name": "Synthetic Checking",
                "canonical_key": "household-checking",
                "currency": "USD",
                "transactions": [transaction("out", amount="-50.00")],
            },
            {
                "id": "synthetic-account-b",
                "name": "Synthetic Savings",
                "canonical_key": "household-savings",
                "currency": "USD",
                "transactions": [transaction("in", amount="50.00")],
            },
        ]
    }, sort_keys=True).encode()
    repository = MemoryRepository()
    IngestionService(repository).ingest(parse(raw))
    state = repository.state()
    assert len(state.canonical_transactions) == 2
    assert any(
        issue.details.get("candidateKind") == "cross_account_mirror"
        for issue in state.issues
    )


def test_trust_cutoff_retains_stale_evidence_but_prevents_projection():
    cutoff = datetime(2026, 9, 1, tzinfo=timezone.utc)
    repository = MemoryRepository()
    IngestionService(repository).ingest(
        parse(
            payload([transaction("late", posted=DAY2)], balance_date=DAY2),
            trust_cutoffs={"synthetic-account-a": cutoff},
        )
    )
    state = repository.state()
    assert len(state.transaction_observations) == 1
    assert len(state.balance_observations) == 1
    assert len(state.projection_records) == 0
    assert sum(
        issue.issue_type == "stale_after_trust_cutoff" for issue in state.issues
    ) == 2


def test_post_cutoff_correction_does_not_replace_current_projection():
    repository = MemoryRepository()
    service = IngestionService(repository)
    service.ingest(parse(payload([transaction("txn-a")])))
    cutoff = datetime(2026, 9, 1, tzinfo=timezone.utc)
    service.ingest(
        parse(
            payload([transaction("txn-a", amount="-99.00", posted=DAY2)]),
            observed_at=T2,
            locator="external://synthetic/post-cutoff-correction",
            trust_cutoffs={"synthetic-account-a": cutoff},
        )
    )
    state = repository.state()
    assert state.canonical_transactions[0].amount == Decimal("-99.00")
    assert len(state.projection_records) == 1
    assert state.projection_records[0].current
    assert any(
        issue.issue_type == "stale_after_trust_cutoff" for issue in state.issues
    )


def test_posted_to_pending_regression_creates_review_issue():
    repository = MemoryRepository()
    service = IngestionService(repository)
    service.ingest(parse(payload([transaction("txn-a")])))
    service.ingest(
        parse(
            payload([transaction("txn-a", pending=True)]),
            observed_at=T2,
            locator="external://synthetic/status-regression",
        )
    )
    state = repository.state()
    assert state.canonical_transactions[0].status == "posted"
    assert any(
        issue.details.get("policy") == "review_before_canonical_regression"
        for issue in state.issues
    )


def test_changed_pending_regression_never_creates_a_pending_projection():
    repository = MemoryRepository()
    service = IngestionService(repository)
    service.ingest(parse(payload([transaction("txn-a")])))
    trusted_projection_hash = repository.state().projection_records[0].projected_hash
    service.ingest(
        parse(
            payload([
                transaction(
                    "txn-a",
                    amount="-13.00",
                    posted=DAY2,
                    pending=True,
                )
            ]),
            observed_at=T2,
            locator="external://synthetic/changed-status-regression",
        )
    )
    state = repository.state()
    assert state.canonical_transactions[0].status == "pending"
    assert len(state.projection_records) == 1
    assert state.projection_records[0].current
    assert state.projection_records[0].projected_hash == trusted_projection_hash


def test_same_batch_versions_project_only_final_canonical_state():
    repository = MemoryRepository()
    batch = parse(payload([
        transaction("txn-a", amount="-10.00", posted=DAY1, pending=True),
        transaction("txn-a", amount="-11.00", posted=DAY2),
    ]))
    IngestionService(repository).ingest(batch)
    state = repository.state()
    canonical = state.canonical_transactions[0]
    current = [record for record in state.projection_records if record.current]
    assert len(state.transaction_observations) == 2
    assert canonical.amount == Decimal("-11.00")
    assert canonical.status == "posted"
    assert len(current) == 1
    assert current[0].projected_hash == projection_hash(canonical)


def test_backdated_remap_is_rejected_by_neutral_reconciliation():
    repository = MemoryRepository()
    service = IngestionService(repository)
    service.ingest(
        parse(
            payload(
                [transaction("txn-a")],
                canonical_key="canonical-before",
            ),
            observed_at=T2,
        )
    )
    backdated = parse(
        payload(
            [transaction("txn-b")],
            canonical_key="canonical-backdated",
        ),
        observed_at=T1,
        locator="external://synthetic/backdated-remap",
    )
    with pytest.raises(
        ValueError, match="remapping cannot precede the current mapping"
    ):
        service.ingest(backdated)
    state = repository.state()
    assert len(state.canonical_accounts) == 1
    assert state.source_accounts[0].canonical_key == "canonical-before"


def test_same_mapping_refresh_does_not_advance_remap_chronology():
    repository = MemoryRepository()
    service = IngestionService(repository)
    service.ingest(
        parse(
            payload([], canonical_key="canonical-before"),
            observed_at=T1,
            locator="external://synthetic/mapping-first",
        )
    )
    service.ingest(
        parse(
            payload([], canonical_key="canonical-before"),
            observed_at=datetime(2026, 9, 3, 12, tzinfo=timezone.utc),
            locator="external://synthetic/mapping-refresh",
        )
    )
    service.ingest(
        parse(
            payload([], canonical_key="canonical-after"),
            observed_at=T2,
            locator="external://synthetic/mapping-intermediate-remap",
        )
    )
    account = repository.state().source_accounts[0]
    assert account.canonical_key == "canonical-after"
    assert account.mapping_effective_from == T2


def test_trust_cutoff_revision_updates_state_and_allows_newly_trusted_evidence():
    repository = MemoryRepository()
    service = IngestionService(repository)
    first_cutoff = datetime(2026, 9, 1, tzinfo=timezone.utc)
    revised_cutoff = datetime(2026, 9, 3, tzinfo=timezone.utc)
    service.ingest(
        parse(
            payload([], canonical_key="cutoff-account"),
            observed_at=T1,
            locator="external://synthetic/cutoff-first",
            trust_cutoffs={"synthetic-account-a": first_cutoff},
        )
    )
    service.ingest(
        parse(
            payload(
                [transaction("newly-trusted", posted=DAY2)],
                canonical_key="cutoff-account",
            ),
            observed_at=T2,
            locator="external://synthetic/cutoff-revised",
            trust_cutoffs={"synthetic-account-a": revised_cutoff},
        )
    )
    state = repository.state()
    assert state.source_accounts[0].trust_cutoff_at == revised_cutoff
    assert sum(
        decision.decision_type == "trust_cutoff" for decision in state.decisions
    ) == 2
    assert any(event.event_type == "trust_cutoff_revised" for event in state.audit_events)
    assert len(state.projection_records) == 1


def test_omitted_cutoff_inherits_persisted_state_and_explicit_removal_is_audited():
    repository = MemoryRepository()
    service = IngestionService(repository)
    cutoff = datetime(2026, 9, 1, tzinfo=timezone.utc)
    service.ingest(
        parse(
            payload([], canonical_key="cutoff-inheritance"),
            observed_at=T1,
            locator="external://synthetic/cutoff-inheritance-first",
            trust_cutoffs={"synthetic-account-a": cutoff},
        )
    )
    service.ingest(
        parse(
            payload(
                [transaction("still-stale", posted=DAY2)],
                canonical_key="cutoff-inheritance",
            ),
            observed_at=T2,
            locator="external://synthetic/cutoff-inheritance-omitted",
        )
    )
    state = repository.state()
    assert state.source_accounts[0].trust_cutoff_at == cutoff
    assert not state.projection_records
    assert any(
        issue.subject_key == state.transaction_observations[0].id
        and issue.issue_type == "stale_after_trust_cutoff"
        for issue in state.issues
    )
    service.ingest(
        parse(
            payload([], canonical_key="cutoff-inheritance"),
            observed_at=datetime(2026, 9, 3, 12, tzinfo=timezone.utc),
            locator="external://synthetic/cutoff-explicit-removal",
            trust_cutoffs={"synthetic-account-a": None},
        )
    )
    state = repository.state()
    assert state.source_accounts[0].trust_cutoff_at is None
    assert any(
        decision.decision_type == "trust_cutoff"
        and decision.action == "clear_cutoff"
        for decision in state.decisions
    )
    assert any(event.event_type == "trust_cutoff_revised" for event in state.audit_events)


def test_cli_parses_explicit_cutoff_and_removal():
    assert parse_trust_cutoffs([
        "synthetic-account-a=2026-09-01T00:00:00Z",
        "synthetic-account-b=none",
    ]) == {
        "synthetic-account-a": datetime(2026, 9, 1, tzinfo=timezone.utc),
        "synthetic-account-b": None,
    }


def test_cli_private_paths_reject_repo_absolute_and_symlink_escape(tmp_path):
    data_dir = tmp_path / "private"
    data_dir.mkdir()
    assert private_data_dir(data_dir) == data_dir.resolve()
    assert private_path(data_dir, Path("export.json"), must_exist=False) == (
        data_dir / "export.json"
    ).resolve()
    with pytest.raises(ValueError, match="relative"):
        private_path(data_dir, tmp_path / "outside.json", must_exist=False)
    with pytest.raises(ValueError, match="outside"):
        private_data_dir(Path(__file__).resolve().parent)
    escape = data_dir / "escape"
    try:
        escape.symlink_to(tmp_path, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks require additional privileges")
    with pytest.raises(ValueError, match="escapes"):
        private_path(data_dir, Path("escape/out.json"), must_exist=False)


def test_reobserved_versions_follow_last_sighting_not_first_insert():
    repository = MemoryRepository()
    service = IngestionService(repository)
    versions = [
        ("-10.00", T1, "a-first"),
        ("-11.00", T2, "b-first"),
        ("-10.00", datetime(2026, 9, 3, 12, tzinfo=timezone.utc), "a-second"),
        ("-11.00", datetime(2026, 9, 4, 12, tzinfo=timezone.utc), "b-second"),
    ]
    for amount, observed_at, locator in versions:
        service.ingest(
            parse(
                payload([transaction("versioned", amount=amount)]),
                observed_at=observed_at,
                locator=f"external://synthetic/{locator}",
            )
        )
    state = repository.state()
    assert len(state.transaction_observations) == 2
    assert state.canonical_transactions[0].amount == Decimal("-11.00")
    latest = max(state.transaction_observations, key=lambda item: item.last_seen_at)
    assert latest.amount == Decimal("-11.00")


def test_source_account_refresh_policy_updates_metadata_but_preserves_times():
    repository = MemoryRepository()
    service = IngestionService(repository)
    first = parse(payload([], canonical_key="refresh-account"), observed_at=T1)
    service.ingest(first)
    refreshed_payload = json.loads(payload([], canonical_key="refresh-account"))
    refreshed_payload["accounts"][0]["name"] = "Synthetic Renamed Account"
    refreshed_payload["accounts"][0]["account_type"] = "checking"
    service.ingest(
        parse(
            json.dumps(refreshed_payload).encode(),
            observed_at=T2,
            locator="external://synthetic/account-refresh",
        )
    )
    account = repository.state().source_accounts[0]
    assert account.name == "Synthetic Renamed Account"
    assert account.account_type == "checking"
    assert account.effective_from == T1
    assert account.mapping_effective_from == T1
    assert account.observed_at == T2


def test_decimal_scale_does_not_change_state_digest():
    first = parse(payload([transaction("scaled", amount="-1.0")]))
    second = parse(payload([transaction("scaled", amount="-1.00")]))
    assert first.transactions[0].observation_hash == second.transactions[0].observation_hash
    first_repository = MemoryRepository()
    second_repository = MemoryRepository()
    IngestionService(first_repository).ingest(first)
    IngestionService(second_repository).ingest(second)
    first_state = first_repository.state()
    second_state = second_repository.state()
    from dataclasses import replace

    normalized_second = replace(
        second_state,
        transaction_observations=tuple(
            replace(item, observation_hash=first_state.transaction_observations[0].observation_hash)
            for item in second_state.transaction_observations
        ),
        canonical_transactions=tuple(
            replace(item, amount=Decimal("-1.00"))
            for item in first_state.canonical_transactions
        ),
    )
    assert export_state(first_state)["stateHash"] == export_state(
        replace(
            first_state,
            canonical_transactions=normalized_second.canonical_transactions,
        )
    )["stateHash"]


def test_cutoff_tightening_withdraws_and_relaxation_reprojects_existing_evidence():
    repository = MemoryRepository()
    service = IngestionService(repository)
    service.ingest(parse(payload([transaction("eligible", posted=DAY2)])))
    assert len([item for item in repository.state().projection_records if item.current]) == 1
    service.ingest(
        parse(
            payload([]),
            observed_at=datetime(2026, 9, 3, 12, tzinfo=timezone.utc),
            locator="external://synthetic/cutoff-tighten",
            trust_cutoffs={
                "synthetic-account-a": datetime(2026, 9, 1, tzinfo=timezone.utc)
            },
        )
    )
    assert not any(item.current for item in repository.state().projection_records)
    service.ingest(
        parse(
            payload([]),
            observed_at=datetime(2026, 9, 4, 12, tzinfo=timezone.utc),
            locator="external://synthetic/cutoff-relax",
            trust_cutoffs={"synthetic-account-a": None},
        )
    )
    state = repository.state()
    assert sum(item.current for item in state.projection_records) == 1
    assert sum(item.status == "superseded" for item in state.projection_records) == 1


def test_out_of_order_new_version_never_rewinds_canonical_state():
    repository = MemoryRepository()
    service = IngestionService(repository)
    service.ingest(
        parse(
            payload([transaction("ordered", amount="-11.00")]),
            observed_at=T2,
            locator="external://synthetic/ordered-new",
        )
    )
    service.ingest(
        parse(
            payload([transaction("ordered", amount="-10.00")]),
            observed_at=T1,
            locator="external://synthetic/ordered-old",
        )
    )
    state = repository.state()
    assert len(state.transaction_observations) == 2
    assert state.canonical_transactions[0].amount == Decimal("-11")
    assert any(
        issue.details.get("policy") == "retain_without_rewinding_canonical_state"
        for issue in state.issues
    )


def test_vanished_posted_sighting_is_retained_and_flagged():
    repository = MemoryRepository()
    service = IngestionService(repository)
    first = SimpleFinAdapter("synthetic-vanished").parse(
        payload([transaction("posted-before")]),
        raw_locator="external://synthetic/vanished-before",
        observed_at=T1,
        overlap_start=datetime(2026, 8, 1, tzinfo=timezone.utc),
        overlap_end=datetime(2026, 9, 3, tzinfo=timezone.utc),
    )
    second = SimpleFinAdapter("synthetic-vanished").parse(
        payload([]),
        raw_locator="external://synthetic/vanished-after",
        observed_at=T2,
        overlap_start=datetime(2026, 8, 1, tzinfo=timezone.utc),
        overlap_end=datetime(2026, 9, 3, tzinfo=timezone.utc),
    )
    service.ingest(first)
    service.ingest(second)
    state = repository.state()
    assert len(state.transaction_observations) == 1
    assert len(state.canonical_transactions) == 1
    assert any(
        issue.details.get("policy") == "retain_posted_no_silent_deletion"
        for issue in state.issues
    )


@pytest.mark.parametrize("amount", ["NaN", "Infinity", "-Infinity", "10000000000000000"])
def test_invalid_money_is_rejected(amount):
    with pytest.raises(SimpleFinAdapterError, match="decimal money"):
        parse(payload([transaction("invalid-money", amount=amount)]))


def test_money_is_normalized_to_postgres_precision_before_hashing():
    batch = parse(payload([transaction("precise", amount="1.123456789")]))
    observation = batch.transactions[0]
    assert observation.amount == Decimal("1.12345679")
    same = parse(payload([transaction("precise", amount="1.12345679")]))
    assert observation.observation_hash == same.transactions[0].observation_hash


def test_signed_zero_has_one_canonical_money_hash():
    positive = parse(payload([transaction("zero", amount="0.00")]))
    negative = parse(payload([transaction("zero", amount="-0.00")]))
    rounded_negative = parse(payload([transaction("zero", amount="-0.000000001")]))
    assert positive.transactions[0].amount.as_tuple().sign == 0
    assert negative.transactions[0].amount.as_tuple().sign == 0
    assert rounded_negative.transactions[0].amount.as_tuple().sign == 0
    assert {
        positive.transactions[0].observation_hash,
        negative.transactions[0].observation_hash,
        rounded_negative.transactions[0].observation_hash,
    } == {positive.transactions[0].observation_hash}


def test_repeated_cutoff_transitions_have_distinct_idempotent_revisions():
    repository = MemoryRepository()
    service = IngestionService(repository)
    settings = [
        (T1, T1, "set-first"),
        (None, T2, "clear"),
        (T1, datetime(2026, 9, 3, 12, tzinfo=timezone.utc), "set-second"),
    ]
    for cutoff, observed_at, locator in settings:
        batch = parse(
            payload([]),
            observed_at=observed_at,
            locator=f"external://synthetic/{locator}",
            trust_cutoffs={"synthetic-account-a": cutoff},
        )
        service.ingest(batch)
        service.ingest(batch)
    decisions = [
        item for item in repository.state().decisions
        if item.decision_type == "trust_cutoff"
    ]
    assert len(decisions) == 3
    assert len({item.id for item in decisions}) == 3
    assert sum(item.action == "set_cutoff" for item in decisions) == 2
    assert sum(item.action == "clear_cutoff" for item in decisions) == 1


def test_omitted_source_status_preserves_closed_until_explicit_reopen():
    repository = MemoryRepository()
    service = IngestionService(repository)
    closed = json.loads(payload([]))
    closed["accounts"][0]["status"] = "closed"
    service.ingest(parse(json.dumps(closed).encode()))
    service.ingest(
        parse(
            payload([]),
            observed_at=T2,
            locator="external://synthetic/status-omitted",
        )
    )
    assert repository.state().source_accounts[0].status == "closed"
    reopened = json.loads(payload([]))
    reopened["accounts"][0]["status"] = "active"
    service.ingest(
        parse(
            json.dumps(reopened).encode(),
            observed_at=datetime(2026, 9, 3, 12, tzinfo=timezone.utc),
            locator="external://synthetic/status-explicit",
        )
    )
    assert repository.state().source_accounts[0].status == "active"


def test_concurrent_replays_have_one_canonical_result():
    repository = MemoryRepository()
    service = IngestionService(repository)
    batch = parse(payload([transaction("txn-a")]))
    threads = [threading.Thread(target=service.ingest, args=(batch,)) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(repository.state().transaction_observations) == 1
    assert len(repository.state().canonical_transactions) == 1


def test_interrupted_unit_of_work_rolls_back_everything():
    class InterruptedRepository(MemoryRepository):
        @contextmanager
        def unit_of_work(self, lock_key):
            with super().unit_of_work(lock_key) as unit:
                original = unit.apply

                def interrupted(batch, plan):
                    original(batch, plan)
                    raise RuntimeError("synthetic interruption")

                unit.apply = interrupted
                yield unit

    repository = InterruptedRepository()
    with pytest.raises(RuntimeError, match="synthetic interruption"):
        IngestionService(repository).ingest(parse(payload([transaction("txn-a")])))
    assert repository.state().transaction_observations == ()


def test_ingestion_service_fault_hook_rolls_back_memory_repository():
    repository = MemoryRepository()
    batch = parse(payload([transaction("txn-fault-hook")]))
    hook_states = []

    def capture_hook(_unit, state):
        hook_states.append(state)

    with pytest.raises(RuntimeError, match="fault hook"):
        IngestionService(repository).ingest(
            batch,
            interrupt_after_observations=1,
            interruption_factory=lambda: RuntimeError("fault hook"),
            after_apply=capture_hook,
        )
    assert len(hook_states) == 1
    assert len(hook_states[0].transaction_observations) == 1
    assert repository.state().blobs == ()
    assert repository.state().transaction_observations == ()


def test_ingestion_service_after_apply_rolls_back_memory_repository():
    repository = MemoryRepository()
    batch = parse(payload([transaction("txn-after-apply")]))

    def fail_after_apply(_unit, _state):
        raise RuntimeError("synthetic companion projection failure")

    with pytest.raises(RuntimeError, match="companion projection failure"):
        IngestionService(repository).ingest(batch, after_apply=fail_after_apply)
    assert repository.state().blobs == ()
    assert repository.state().transaction_observations == ()


def test_deterministic_export_is_sorted_and_self_hashing():
    repository = MemoryRepository()
    service = IngestionService(repository)
    service.ingest(parse(payload([transaction("txn-b"), transaction("txn-a")])))
    first = export_state(repository.state())
    second = export_state(repository.state())
    assert first == second
    assert first["schemaVersion"] == 1
    assert first["candidate"] == "python-postgresql"
    assert len(first["stateHash"]) == 64


def test_repository_boundary_records_app_state_and_append_only_decisions():
    repository = MemoryRepository()
    decision = DurableDecision(
        id=stable_id("decision", "synthetic-review"),
        decision_type="duplicate_resolution",
        subject_type="quality_issue",
        subject_key="synthetic-review",
        action="retain_both",
        rationale="Synthetic records represent legitimate repeats",
        decided_by="synthetic-reviewer",
        effective_at=T1,
        observed_at=T1,
        processed_at=T1,
    )
    snapshot = AppStateSnapshot(
        id=stable_id("app_state", "synthetic-app", "accounts", T1.isoformat()),
        app_name="synthetic-app",
        state_kind="accounts",
        external_locator="external://synthetic/app-state",
        state_hash="a" * 64,
        effective_at=T1,
        observed_at=T1,
        processed_at=T1,
    )
    repository.append_decision(decision)
    repository.record_app_state(snapshot)
    repository.append_decision(decision)
    repository.record_app_state(snapshot)
    assert repository.state().decisions == (decision,)
    assert repository.state().app_state_snapshots == (snapshot,)


def test_invalid_or_unsupported_payloads_fail_loudly():
    adapter = SimpleFinAdapter("synthetic")
    with pytest.raises(SimpleFinAdapterError):
        adapter.parse(
            b'{"version": 3, "accounts": []}',
            raw_locator="external://synthetic/invalid",
            observed_at=T1,
        )
    with pytest.raises(SimpleFinAdapterError):
        parse(payload([{"id": "", "posted": DAY1, "amount": "1.00"}]))
