"""Synthetic tests for the SimpleFIN plan-only pipeline."""

from __future__ import annotations

import json
import hashlib
from datetime import date, datetime, timezone
from decimal import Decimal
from io import BytesIO

import pytest

from importers.simplefin.client import SimpleFinAccount, SimpleFinTransaction
from importers.simplefin.pipeline import (
    ExistingTransaction,
    PipelineError,
    build_plan,
    existing_from_activities,
    fetch_snapshot,
    load_mapping,
    normalize_description,
    write_plan,
)

NOW = datetime(2026, 8, 27, 12, 30, tzinfo=timezone.utc)
TARGET = "wf-account-stable-id"
MAPPING = {
    "source-account": {
        "action": "import",
        "wealthfolioAccountId": TARGET,
    }
}


def txn(
    source_id: str = "simplefin-transaction-id",
    *,
    description: str = "Synthetic Market",
    pending: bool = False,
) -> SimpleFinTransaction:
    return SimpleFinTransaction(
        id=source_id,
        posted=date(2026, 8, 26),
        amount=Decimal("-12.34"),
        description=description,
        pending=pending,
    )


def account(*transactions: SimpleFinTransaction) -> SimpleFinAccount:
    return SimpleFinAccount(
        id="source-account",
        name="Synthetic Checking",
        org="Synthetic Bank",
        currency="USD",
        balance=Decimal("87.66"),
        balance_date=date(2026, 8, 27),
        transactions=list(transactions),
    )


def source_account(
    source_id: str,
    *transactions: SimpleFinTransaction,
    balance: Decimal = Decimal("87.66"),
) -> SimpleFinAccount:
    return SimpleFinAccount(
        id=source_id,
        name=f"Synthetic {source_id}",
        org="Synthetic Bank",
        currency="USD",
        balance=balance,
        balance_date=date(2026, 8, 27),
        transactions=list(transactions),
    )


def plan_for(
    *transactions: SimpleFinTransaction,
    existing: list[ExistingTransaction] | None = None,
    mapping=MAPPING,
):
    return build_plan(
        [account(*transactions)],
        [],
        mapping,
        existing or [],
        {TARGET: Decimal("80.00")},
        {TARGET},
        generated_at=NOW,
    )


def test_qfx_overlap_is_skipped_even_when_ids_differ():
    existing = [
        ExistingTransaction(
            TARGET,
            date(2026, 8, 26),
            Decimal("-12.34"),
            "SYNTHETIC-MARKET",
            "different-qfx-fitid",
        )
    ]
    result = plan_for(txn(), existing=existing)
    item = result["accounts"][0]["transactions"][0]
    assert (item["status"], item["reason"]) == ("skipped", "duplicate-overlap")


def test_multiple_overlap_candidates_are_ambiguous_not_silently_deduped():
    existing = [
        ExistingTransaction(
            TARGET,
            date(2026, 8, 26),
            Decimal("-12.34"),
            "Synthetic Market",
            f"fitid-{number}",
        )
        for number in (1, 2)
    ]
    item = plan_for(txn(), existing=existing)["accounts"][0]["transactions"][0]
    assert (item["status"], item["reason"]) == ("review", "ambiguous-overlap")


def test_missing_mapping_blocks_the_plan():
    result = plan_for(txn(), mapping={})
    assert not result["ready"]
    assert result["blockers"] == [
        {"code": "missing-mapping", "sourceAccountId": "source-account"}
    ]


def test_institution_errors_are_preserved_and_block_the_plan():
    result = build_plan([account(txn())], ["reauthentication required"], MAPPING)
    assert not result["ready"]
    assert result["institutionErrors"] == ["reauthentication required"]
    assert result["blockers"][0]["code"] == "institution-error"


def test_provider_history_range_advisory_is_preserved_without_blocking():
    message = "Requested date range exceeds recommended range of 45 days."
    result = build_plan([account(txn())], [message], MAPPING)
    assert result["ready"]
    assert result["institutionErrors"] == [message]
    assert result["advisories"] == [message]
    assert result["blockers"] == []


def test_empty_account_is_reported_without_inventing_transactions():
    result = plan_for()
    planned = result["accounts"][0]
    assert planned["transactionCount"] == 0
    assert planned["transactions"] == []
    assert planned["drift"] == "7.66"
    assert planned["balanceAction"] == "report-only"


def test_pending_transaction_is_never_planned_for_import():
    item = plan_for(txn(pending=True))["accounts"][0]["transactions"][0]
    assert (item["status"], item["reason"]) == ("skipped", "pending")


def test_exact_source_id_is_idempotent():
    existing = [
        ExistingTransaction(
            TARGET,
            date(2026, 8, 1),
            Decimal("-999.99"),
            "Entirely different metadata",
            "simplefin-transaction-id",
        )
    ]
    item = plan_for(txn(), existing=existing)["accounts"][0]["transactions"][0]
    assert (item["status"], item["reason"]) == ("skipped", "duplicate-source-id")


def test_source_ids_with_colons_remain_exact():
    (existing,) = existing_from_activities(
        [{
            "accountId": TARGET,
            "date": "2026-08-26",
            "amount": 12.34,
            "activityType": "WITHDRAWAL",
            "comment": "Synthetic Market",
            "idempotencyKey": f"simplefin:{TARGET}:source:id:with:colons",
        }]
    )
    assert existing.source_id == "source:id:with:colons"


def test_description_normalization_is_deliberately_conservative():
    assert normalize_description("  SYNTHETIC—Market!! ") == "synthetic market"
    assert normalize_description("Synthetic Market 123") != normalize_description(
        "Synthetic Market 124"
    )


def test_corporate_card_requires_the_durable_exclusion_decision():
    excluded = {
        "source-account": {
            "action": "exclude",
            "decision": "employer-corporate-card",
        }
    }
    result = plan_for(txn(), mapping=excluded)
    assert result["accounts"][0]["status"] == "excluded"
    assert result["accounts"][0]["decision"] == "employer-corporate-card"


def test_dormant_zero_balance_account_can_be_excluded():
    dormant = source_account("dormant", balance=Decimal("0"))
    mapping = {
        "dormant": {
            "action": "exclude",
            "decision": "dormant-zero-balance-account",
        }
    }
    result = build_plan([dormant], [], mapping, generated_at=NOW)
    assert result["ready"]
    assert result["accounts"][0]["status"] == "excluded"


@pytest.mark.parametrize(
    ("balance", "transactions"),
    [
        (Decimal("1.00"), ()),
        (Decimal("0"), (txn(),)),
    ],
)
def test_dormant_exclusion_blocks_when_balance_or_activity_appears(
    balance, transactions
):
    dormant = source_account("dormant", *transactions, balance=balance)
    mapping = {
        "dormant": {
            "action": "exclude",
            "decision": "dormant-zero-balance-account",
        }
    }
    result = build_plan([dormant], [], mapping, generated_at=NOW)
    assert not result["ready"]
    assert result["blockers"][0]["code"] == "excluded-account-not-dormant"


def test_duplicate_summary_can_be_excluded_only_with_equivalent_import_target():
    transaction = txn()
    summary = source_account("summary", transaction)
    detail = source_account("detail", transaction)
    mapping = {
        "summary": {
            "action": "exclude",
            "decision": "aggregator-account-summary",
            "duplicateOfSourceAccountId": "detail",
        },
        "detail": {
            "action": "import",
            "wealthfolioAccountId": TARGET,
        },
    }
    result = build_plan(
        [summary, detail],
        [],
        mapping,
        known_account_ids={TARGET},
        generated_at=NOW,
    )
    assert result["ready"]
    assert result["accounts"][0]["status"] == "excluded"
    assert result["accounts"][0]["duplicateOfSourceAccountId"] == "detail"


def test_duplicate_summary_blocks_when_source_semantics_diverge():
    summary = source_account("summary", txn())
    detail = source_account(
        "detail",
        txn(description="Different synthetic activity"),
    )
    mapping = {
        "summary": {
            "action": "exclude",
            "decision": "aggregator-account-summary",
            "duplicateOfSourceAccountId": "detail",
        },
        "detail": {
            "action": "import",
            "wealthfolioAccountId": TARGET,
        },
    }
    result = build_plan(
        [summary, detail],
        [],
        mapping,
        known_account_ids={TARGET},
        generated_at=NOW,
    )
    assert not result["ready"]
    assert result["blockers"][0]["code"] == "duplicate-summary-mismatch"


def test_alternative_liability_can_be_monitored_without_importing_transactions():
    monitored = {
        "source-account": {
            "action": "monitor",
            "wealthfolioAlternativeAssetId": "alternative-liability-id",
            "assertionAccountId": "durable-loan-id",
        }
    }
    result = build_plan(
        [account(txn())],
        [],
        monitored,
        ledger_balances={"alternative-liability-id": Decimal("80.00")},
        known_account_ids={"alternative-liability-id"},
        generated_at=NOW,
    )
    item = result["accounts"][0]
    assert result["ready"]
    assert item["status"] == "monitored"
    assert item["transactions"] == []
    assert item["drift"] == "7.66"
    assert item["assertionAccountId"] == "durable-loan-id"


def test_monitor_mapping_requires_an_alternative_asset_target():
    result = plan_for(txn(), mapping={"source-account": {"action": "monitor"}})
    assert not result["ready"]
    assert result["blockers"][0]["code"] == "missing-monitor-target"


def test_unknown_mapping_action_is_blocked_not_guessed():
    result = plan_for(txn(), mapping={"source-account": {"action": "surprise"}})
    assert not result["ready"]
    assert result["blockers"][0]["code"] == "unknown-mapping-action"


def test_untracked_zero_balance_account_can_be_observed_without_importing():
    observed = {
        "source-account": {
            "action": "observe",
            "assertionAccountId": "durable-untracked-account",
        }
    }
    result = plan_for(txn(), mapping=observed)
    item = result["accounts"][0]
    assert result["ready"]
    assert item["status"] == "observed"
    assert item["transactions"] == []
    assert item["sourceBalance"] == "87.66"
    assert item["ledgerBalance"] is None


def test_observe_requires_a_durable_assertion_id():
    result = plan_for(txn(), mapping={"source-account": {"action": "observe"}})
    assert not result["ready"]
    assert result["blockers"][0]["code"] == "missing-observation-id"


class Response(BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def test_fetch_persists_exact_immutable_dated_raw_json(tmp_path):
    body = b'{"accounts":[],"errors":[]}\n'
    path, payload = fetch_snapshot(
        tmp_path,
        "https://bridge.example.test/access",
        now=NOW,
        opener=lambda request, timeout: Response(body),
    )
    assert path.parent == tmp_path / "raw" / "simplefin" / "2026-08-27"
    assert path.read_bytes() == body
    assert payload == {"accounts": [], "errors": []}
    metadata_path = path.with_name(
        f"request-{path.stem.removeprefix('simplefin-')}.json"
    )
    assert json.loads(metadata_path.read_text(encoding="utf-8")) == {
        "schemaVersion": 1,
        "protocolVersion": 1,
        "snapshotSha256": hashlib.sha256(body).hexdigest(),
        "requestedStart": "2026-05-30",
        "requestedEnd": "2026-08-27",
        "pendingIncluded": True,
    }


def test_history_above_90_days_is_rejected_before_network_access(tmp_path):
    called = False

    def opener(request, timeout):
        nonlocal called
        called = True
        return Response(b"{}")

    with pytest.raises(PipelineError, match="between 1 and 90"):
        fetch_snapshot(tmp_path, "https://example.test", days=91, opener=opener)
    assert not called


def test_daily_limit_blocks_a_25th_attempt_before_network_access(tmp_path):
    folder = tmp_path / "raw" / "simplefin" / "2026-08-27"
    folder.mkdir(parents=True)
    for slot in range(1, 25):
        (folder / f"request-{slot:02d}").touch()
    called = False

    def opener(request, timeout):
        nonlocal called
        called = True
        return Response(b"{}")

    with pytest.raises(PipelineError, match="daily SimpleFIN request limit"):
        fetch_snapshot(
            tmp_path,
            "https://example.test",
            now=NOW,
            opener=opener,
        )
    assert not called


def test_private_mapping_is_required(tmp_path):
    with pytest.raises(PipelineError, match="missing private account mapping"):
        load_mapping(tmp_path)


def test_mapping_file_shape_is_validated(tmp_path):
    folder = tmp_path / "simplefin"
    folder.mkdir()
    (folder / "account-map.json").write_text(json.dumps({"accounts": {}}))
    with pytest.raises(PipelineError, match="version 1"):
        load_mapping(tmp_path)


def test_assertions_file_uses_canonical_balance_snapshot_shape(tmp_path):
    result = plan_for(txn())
    _, assertion_path = write_plan(
        tmp_path, result, tmp_path / "raw" / "synthetic.json"
    )
    payload = json.loads(assertion_path.read_text())
    assert payload["balances"] == [{
        "accountId": TARGET,
        "date": "2026-08-27",
        "balance": "87.66",
        "source": "simplefin",
        "ledgerBalance": "80.00",
        "drift": "7.66",
        "action": "report-only",
    }]
