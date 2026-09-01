import json
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal

import pytest

import importers.simplefin.apply_cli as simplefin_apply
from importers.rebuild.decisions import DecisionError
from importers.rebuild.safety import plan_fingerprint
from importers.simplefin.application import (
    _pair_pending_transfers,
    _activity_cash_flow,
    _cash_flow,
    activity_semantic_fingerprint,
    build_application_plan,
    canonicalize_metadata,
    expected_post_fingerprint,
    ledger_fingerprint,
    load_manual_decisions,
    manual_decision_evidence_binding,
    normalize_subtype,
    sha256_file,
    validate_plan_seal,
)
from importers.simplefin.apply_cli import (
    _already_applied,
    _apply_operations,
    _assert_health_not_degraded,
    _assert_health_repairable_by_links,
    cmd_apply,
    _current_values,
    _durable_transfers,
    _portfolio_value_report,
    _rollback,
    _revalidate_production_decisions,
    _validate_promotion_intent,
    _validate_production_authorization,
    _validate_plan_environment,
    _validate_staging_receipt,
)


NOW = datetime(2026, 8, 27, tzinfo=timezone.utc)
TARGET = "stage-account"


def evidence(tmp_path, amount="-20.00", *, review=False):
    timestamp = int(datetime(2026, 8, 26, tzinfo=timezone.utc).timestamp())
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text(json.dumps({
        "errors": [],
        "accounts": [{
            "id": "source-account",
            "name": "Synthetic Checking",
            "org": {"name": "Synthetic Bank"},
            "currency": "USD",
            "balance": "100.00",
            "balance-date": timestamp,
            "transactions": [{
                "id": "txn-1",
                "posted": timestamp,
                "amount": amount,
                "description": "Synthetic Market",
            }],
        }],
    }), encoding="utf-8")
    reviewed = {
        "schemaVersion": 1,
        "institutionErrors": [],
        "snapshot": str(snapshot),
        "accounts": [{
            "sourceAccountId": "source-account",
            "status": "mapped",
            "assertionAccountId": "canonical-account",
            "sourceBalance": "100.00",
            "balanceDate": "2026-08-26",
            "transactions": [{
                "status": "review" if review else "planned",
                "reason": "ambiguous-overlap" if review else None,
                "sourceId": "txn-1",
                "date": "2026-08-26",
                "amount": amount,
                "description": "Synthetic Market",
                "normalizedDescription": "synthetic market",
            }],
        }],
    }
    reviewed_path = tmp_path / "reviewed.json"
    reviewed_path.write_text(json.dumps(reviewed), encoding="utf-8")
    return reviewed, reviewed_path, snapshot


def gap(amount="50.00", *, linked=False):
    return {
        "id": "gap-id",
        "accountId": TARGET,
        "activityType": "TRANSFER_IN",
        "date": "2026-08-27T00:00:00Z",
        "amount": amount,
        "currency": "USD",
        "idempotencyKey": "gap:canonical-account:2026-08-27",
        "sourceGroupId": "linked-group" if linked else None,
    }


def persist_activity_put(client, path, payload):
    assert path == "/activities"
    row = next(row for row in client.rows if row["id"] == payload["id"])
    row.update(payload)
    row["date"] = payload["activityDate"]
    return dict(row)


def build(
    tmp_path,
    activities,
    amount="-20.00",
    *,
    review=False,
    metadata_remediations=None,
    metadata_remediations_path=None,
):
    reviewed, reviewed_path, snapshot = evidence(
        tmp_path, amount, review=review
    )
    return build_application_plan(
        reviewed,
        reviewed_path,
        snapshot,
        {"canonical-account": TARGET},
        activities,
        [{"id": TARGET, "accountType": "CASH"}],
        {TARGET: Decimal("100.00")},
        "staging-environment",
        metadata_remediations=metadata_remediations,
        metadata_remediations_path=metadata_remediations_path,
        generated_at=NOW,
    )


def test_planned_net_movement_resizes_exact_gap_and_preserves_cash(tmp_path):
    activity = {**gap(), "metadata": {"source": "assertion"}}
    plan = build(tmp_path, [activity])

    assert len(plan["operations"]["creates"]) == 1
    assert plan["operations"]["updates"][0]["amount"] == 70.0
    assert plan["operations"]["updates"][0]["activityType"] == "TRANSFER_IN"
    assert plan["operations"]["updates"][0]["metadata"] == (
        '{"flow":{"is_external":true}}'
    )
    assert plan["operations"]["deleteIds"] == []
    assert plan["impact"] == {
        "transactionCashFlow": "-20.00",
        "reconciliationCashFlow": "0",
        "reconciliationBalanceEffect": "20.00",
        "expectedCashFlowDelta": "-20.00",
        "incomeDelta": "0.00",
        "spendingDelta": "20.00",
        "expectedNetWorthDelta": "0",
    }
    assert plan["operations"]["updates"][0]["subtype"] == "external_transfer"
    assert len(plan["operations"]["metadataFinalizations"]) == 1
    finalization = plan["operations"]["metadataFinalizations"][0]
    assert finalization["activityId"] == "gap-id"
    assert finalization["payload"]["metadata"] == (
        '{"flow":{"is_external":true}}'
    )
    assert plan["portableIntent"]["metadataFinalizations"][0]["metadata"] == (
        '{"flow":{"is_external":true}}'
    )


def test_exact_reconciliation_replacement_is_held_instead_of_deleted(tmp_path):
    plan = build(tmp_path, [gap()], amount="50.00")
    assert plan["operations"]["updates"] == []
    assert plan["operations"]["creates"] == []
    assert plan["operations"]["deleteIds"] == []
    assert plan["manual"] == [{
        "sourceAccountId": "source-account",
        "sourceId": "txn-1",
        "reason": "unsafe-reconciliation-delete",
        "decisionStatus": "unruled",
    }]


@pytest.mark.parametrize(
    ("mode", "command"),
    (
        ("staging-apply-plan", "apply"),
        ("production-promotion-plan", "promote-apply"),
    ),
)
def test_delete_plan_is_rejected_before_client_mutation(
    tmp_path, mode, command
):
    from types import SimpleNamespace

    plan = build(tmp_path, [gap()])
    plan["mode"] = mode
    plan["operations"]["deleteIds"] = ["synthetic-delete"]
    plan["planFingerprint"] = plan_fingerprint({
        key: value for key, value in plan.items() if key != "planFingerprint"
    })
    plan_path = tmp_path / f"{mode}.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")

    class Client:
        def __getattr__(self, name):
            raise AssertionError(f"client must not be called: {name}")

    args = SimpleNamespace(
        application_plan=plan_path,
        data_dir=tmp_path,
        command=command,
    )
    with pytest.raises(DecisionError, match="plans with deletes are prohibited"):
        cmd_apply(args, Client())


def test_already_imported_plan_only_finalizes_stale_external_metadata(tmp_path):
    imported = {
        "id": "imported-id",
        "accountId": TARGET,
        "activityType": "WITHDRAWAL",
        "date": "2026-08-26T12:00:00Z",
        "amount": 20,
        "currency": "USD",
        "comment": "Synthetic Market",
        "idempotencyKey": f"simplefin:{TARGET}:txn-1",
    }
    stale = [
        {
            **gap(str(10 + index)),
            "id": f"stale-{index}",
            "idempotencyKey": f"gap:canonical-account:2026-08-{20 + index}",
            "subtype": "external_transfer",
            "metadata": None,
        }
        for index in range(4)
    ]
    remediations = [
        {
            "activityId": row["id"],
            "originalFingerprint": activity_semantic_fingerprint(row),
            "desiredMetadata": {"flow": {"is_external": True}},
        }
        for row in stale
    ]
    remediation_path = tmp_path / "metadata-remediations.json"
    remediation_path.write_text(
        json.dumps({"schemaVersion": 1, "activities": remediations}),
        encoding="utf-8",
    )

    plan = build(
        tmp_path,
        [imported, *stale],
        metadata_remediations=remediations,
        metadata_remediations_path=remediation_path,
    )

    assert plan["operations"]["creates"] == []
    assert plan["operations"]["updates"] == []
    assert plan["operations"]["deleteIds"] == []
    assert len(plan["operations"]["metadataFinalizations"]) == 4
    assert all(
        operation["payload"]["metadata"] == '{"flow":{"is_external":true}}'
        for operation in plan["operations"]["metadataFinalizations"]
    )
    assert len(plan["portableIntent"]["metadataFinalizations"]) == 4


def test_remediation_only_accepts_nonempty_original_metadata(tmp_path):
    imported = {
        "id": "imported-id",
        "accountId": TARGET,
        "activityType": "WITHDRAWAL",
        "date": "2026-08-26T12:00:00Z",
        "amount": 20,
        "currency": "USD",
        "comment": "Synthetic Market",
        "idempotencyKey": f"simplefin:{TARGET}:txn-1",
    }
    stale = {
        **gap(),
        "metadata": {"source": "synthetic"},
        "subtype": "external_transfer",
    }
    remediations = [{
        "activityId": stale["id"],
        "originalFingerprint": activity_semantic_fingerprint(stale),
        "desiredMetadata": {
            "source": "synthetic",
            "flow": {"is_external": True},
        },
    }]
    remediation_path = tmp_path / "metadata-remediations.json"
    remediation_path.write_text(
        json.dumps({"schemaVersion": 1, "activities": remediations}),
        encoding="utf-8",
    )
    plan = build(
        tmp_path,
        [imported, stale],
        metadata_remediations=remediations,
        metadata_remediations_path=remediation_path,
    )

    class Client:
        def __init__(self, rows=None):
            self.rows = rows or [deepcopy(imported), deepcopy(stale)]
            self.put_calls = 0

        def iter_activities(self):
            return iter(self.rows)

        def save_activities(self, *, creates, updates, delete_ids):
            assert creates == updates == delete_ids == []
            return {"created": [], "updated": [], "deleted": []}

        def put(self, path, payload):
            self.put_calls += 1
            return persist_activity_put(self, path, payload)

        def post(self, path, payload):
            assert path == "/portfolio/recalculate"
            return {}

    finalization = plan["operations"]["metadataFinalizations"][0]
    assert finalization["kind"] == "remediation"
    assert finalization["originalFingerprint"] in finalization["forwardFingerprints"]

    client = Client()
    _apply_operations(client, plan)

    assert client.put_calls == 1
    assert _already_applied(plan, client.rows)
    assert canonicalize_metadata(client.rows[1]["metadata"]) == (
        '{"flow":{"is_external":true},"source":"synthetic"}'
    )

    changed = {**deepcopy(stale), "currency": "EUR"}
    rejected = Client([deepcopy(imported), changed])
    with pytest.raises(DecisionError, match="sealed state"):
        _apply_operations(rejected, plan)
    assert rejected.put_calls == 0


def test_missing_or_linked_reconciliation_never_causes_partial_apply(tmp_path):
    missing = build(tmp_path, [])
    assert missing["operations"]["creates"] == []
    assert missing["manual"][0]["reason"] == "missing-reconciliation"

    other = tmp_path / "linked"
    other.mkdir()
    with pytest.raises(DecisionError, match="is linked"):
        build(other, [gap(linked=True)])


def test_reviewed_ambiguity_is_never_automatically_imported(tmp_path):
    plan = build(tmp_path, [gap()], review=True)
    assert plan["operations"] == {
        "creates": [],
        "updates": [],
        "deleteIds": [],
        "metadataFinalizations": [],
    }
    assert plan["manual"][0]["reason"] == "ambiguous-overlap"


def pending(source_id, target, account_type, amount, description, date="2026-08-26"):
    return {
        "sourceAccountId": f"source-{target}",
        "target": target,
        "accountType": account_type,
        "transaction": {
            "sourceId": source_id,
            "amount": amount,
            "date": date,
            "description": description,
        },
    }


def test_unique_card_payment_is_paired_one_to_one():
    cash = pending("cash-payment", "checking", "CASH", "-100", "Card payment")
    card = pending("card-payment", "card", "CREDIT_CARD", "100", "Payment received")
    pairs, ambiguous = _pair_pending_transfers([cash, card], {})
    assert pairs == [(cash, card)]
    assert ambiguous == set()


def test_durable_group_member_is_not_reused_by_heuristic_pairing():
    cash = pending("cash-payment", "checking", "CASH", "-100", "Card payment")
    card = pending("card-payment", "card", "CREDIT_CARD", "100", "Payment received")
    other = pending("other", "savings", "CASH", "-50", "Internal transfer")
    durable = {
        ("source-checking", "cash-payment"): "durable-group",
        ("source-savings", "other"): "durable-group",
    }

    pairs, ambiguous = _pair_pending_transfers([cash, card, other], durable)

    assert pairs == []
    assert ambiguous == {
        ("source-checking", "cash-payment"),
        ("source-savings", "other"),
    }


def test_credit_card_cash_flow_counts_spending_and_offsets_for_credits():
    assert _cash_flow("CREDIT_CARD", "WITHDRAWAL", Decimal("25")) == (
        Decimal(),
        Decimal("25"),
    )
    assert _cash_flow("CREDIT_CARD", "CREDIT", Decimal("10")) == (
        Decimal(),
        Decimal("-10"),
    )
    assert _cash_flow("CREDIT_CARD", "TRANSFER_IN", Decimal("25")) == (
        Decimal(),
        Decimal(),
    )


def test_external_reconciliation_is_not_household_cash_flow():
    assert _activity_cash_flow("CREDIT_CARD", {
        "activityType": "WITHDRAWAL",
        "amount": "38.32",
        "metadata": '{"flow":{"is_external":true}}',
    }) == (Decimal(), Decimal())


def test_untouched_stale_portfolio_cache_is_informational():
    before = {
        "accountValues": {
            "touched": Decimal("100.00"),
            "untouched-security": Decimal("100.12500000"),
        },
        "alternativeValues": {"property": Decimal("500.00")},
        "globalTotal": Decimal("700.12500000"),
    }
    after = {
        "accountValues": {
            "touched": Decimal("100.00"),
            "untouched-security": Decimal("100.00"),
        },
        "alternativeValues": {"property": Decimal("500.00")},
        "globalTotal": Decimal("700.00"),
    }

    report = _portfolio_value_report(before, after, {"touched"})

    assert report["globalDifference"] == Decimal("-0.12500000")
    assert report["untouchedAccountCacheDifference"] == Decimal("-0.12500000")
    assert report["touchedAccountDifference"] == 0
    assert report["alternativeHoldingsDifference"] == 0
    assert report["unexplainedResidual"] == 0


def test_delayed_health_error_is_not_mistaken_for_stable_health(monkeypatch):
    clean = {
        "nonInfoCodeCounts": {},
        "nonInfoKeys": [],
        "nonInfoActivityIds": {},
        "nonInfoIssues": [],
    }
    error = {
        "nonInfoCodeCounts": {"transfer_incomplete": 1},
        "nonInfoKeys": ["transfer_incomplete:synthetic"],
        "nonInfoActivityIds": {"transfer_incomplete": ["activity-1"]},
        "nonInfoIssues": [{
            "identity": "transfer_incomplete:synthetic",
            "code": "transfer_incomplete",
            "affectedIds": ["affected-1"],
            "activityIds": ["activity-1"],
        }],
    }
    summaries = iter((clean, clean, error, error))
    monkeypatch.setattr(
        simplefin_apply, "_health_summary", lambda *args, **kwargs: next(summaries)
    )
    monkeypatch.setattr(simplefin_apply.time, "sleep", lambda _: None)
    plan = {
        "operations": {
            "metadataFinalizations": [],
        }
    }

    with pytest.raises(DecisionError, match="gained 1 non-INFO"):
        _assert_health_not_degraded(object(), clean, plan)


def test_health_waits_for_metadata_repair_to_settle(monkeypatch):
    error = {
        "nonInfoCodeCounts": {"transfer_incomplete": 1},
        "nonInfoKeys": ["transfer_incomplete:synthetic"],
        "nonInfoActivityIds": {"transfer_incomplete": ["activity-1"]},
        "nonInfoIssues": [{
            "identity": "transfer_incomplete:synthetic",
            "code": "transfer_incomplete",
            "affectedIds": ["affected-1"],
            "activityIds": ["activity-1"],
        }],
    }
    clean = {
        "nonInfoCodeCounts": {},
        "nonInfoKeys": [],
        "nonInfoActivityIds": {},
        "nonInfoIssues": [],
    }
    summaries = iter((error, error, clean, clean))
    monkeypatch.setattr(
        simplefin_apply, "_health_summary", lambda *args, **kwargs: next(summaries)
    )
    monkeypatch.setattr(simplefin_apply.time, "sleep", lambda _: None)
    plan = {
        "operations": {
            "metadataFinalizations": [{"activityId": "activity-1"}],
        }
    }

    result = _assert_health_not_degraded(object(), error, plan)

    assert result["after"] == clean
    assert result["newNonInfoCount"] == 0


def test_production_preflight_rejects_unrelated_existing_error(monkeypatch):
    summary = {
        "nonInfoCodes": ["balance_mismatch", "transfer_incomplete"],
        "nonInfoActivityIds": {
            "balance_mismatch": ["other-activity"],
            "transfer_incomplete": ["repair-activity"],
        },
        "nonInfoIssues": [
            {
                "identity": "balance_mismatch:existing",
                "code": "balance_mismatch",
                "affectedIds": ["balance:other-account"],
                "activityIds": ["other-activity"],
            },
            {
                "identity": "transfer_incomplete:repair",
                "code": "transfer_incomplete",
                "affectedIds": ["transfer:repair"],
                "activityIds": ["repair-activity"],
            },
        ],
    }
    monkeypatch.setattr(
        simplefin_apply, "_stable_health_summary", lambda client: summary
    )
    plan = {
        "operations": {
            "metadataFinalizations": [{"activityId": "repair-activity"}],
        },
        "links": [],
    }

    with pytest.raises(DecisionError, match="unrelated non-INFO.*balance_mismatch"):
        _assert_health_repairable_by_links(object(), plan)


def test_production_preflight_rejects_transfer_outside_explicit_repairs(
    monkeypatch,
):
    summary = {
        "nonInfoCodes": ["transfer_incomplete"],
        "nonInfoActivityIds": {
            "transfer_incomplete": ["unrelated-activity"],
        },
        "nonInfoIssues": [{
            "identity": "transfer_incomplete:unrelated",
            "code": "transfer_incomplete",
            "affectedIds": ["transfer:unrelated"],
            "activityIds": ["unrelated-activity"],
        }],
    }
    monkeypatch.setattr(
        simplefin_apply, "_stable_health_summary", lambda client: summary
    )
    plan = {
        "operations": {
            "updates": [{"id": "unrelated-activity"}],
            "metadataFinalizations": [{"activityId": "repair-activity"}],
        },
        "links": [],
    }

    with pytest.raises(DecisionError, match="cannot repair"):
        _assert_health_repairable_by_links(object(), plan)


def test_production_apply_rechecks_health_before_backup(tmp_path, monkeypatch):
    from types import SimpleNamespace

    plan = build(tmp_path, [gap()])
    plan["mode"] = "production-promotion-plan"
    plan["evidence"]["stagingApplicationPlan"] = {"path": "staging-plan.json"}
    plan["evidence"]["stagingReceipt"] = {"path": "staging-receipt.json"}
    plan["planFingerprint"] = plan_fingerprint({
        key: value for key, value in plan.items() if key != "planFingerprint"
    })
    plan_path = tmp_path / "production-plan.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    state = {"backup": False}

    class Client:
        def iter_activities(self):
            return iter([gap()])

        def list_accounts(self):
            return [{"id": TARGET, "accountType": "CASH"}]

        def backup_database(self):
            state["backup"] = True
            return {"path": "must-not-exist"}

    monkeypatch.setattr(
        simplefin_apply, "_validate_production_authorization", lambda *args: None
    )
    monkeypatch.setattr(simplefin_apply, "validate_plan_seal", lambda *args: None)
    monkeypatch.setattr(
        simplefin_apply, "_validate_current_snapshot", lambda *args: None
    )
    monkeypatch.setattr(
        simplefin_apply,
        "_validate_staging_receipt",
        lambda *args: (
            plan,
            {"intentFingerprint": plan["intentFingerprint"]},
        ),
    )
    monkeypatch.setattr(
        simplefin_apply, "_validate_promotion_intent", lambda *args: None
    )
    monkeypatch.setattr(
        simplefin_apply, "_revalidate_production_decisions", lambda *args: None
    )
    monkeypatch.setattr(
        simplefin_apply, "_validate_plan_environment", lambda *args: None
    )
    monkeypatch.setattr(simplefin_apply, "_already_applied", lambda *args: False)
    monkeypatch.setattr(
        simplefin_apply,
        "ledger_fingerprint",
        lambda *args: plan["ledgerFingerprint"],
    )
    monkeypatch.setattr(
        simplefin_apply,
        "_portfolio_values",
        lambda *args: {
            "accountValues": {TARGET: Decimal("100.00")},
            "alternativeValues": {},
            "globalTotal": Decimal("100.00"),
        },
    )
    monkeypatch.setattr(
        simplefin_apply, "_current_values", lambda *args: {TARGET: Decimal("100.00")}
    )
    monkeypatch.setattr(
        simplefin_apply,
        "_spending_totals",
        lambda *args: {"income": Decimal(), "spending": Decimal()},
    )
    monkeypatch.setattr(
        simplefin_apply,
        "_assert_health_repairable_by_links",
        lambda *args: (_ for _ in ()).throw(
            DecisionError("production health has unrelated non-INFO issue")
        ),
    )
    args = SimpleNamespace(
        application_plan=plan_path,
        data_dir=tmp_path,
        command="promote-apply",
        base_url="http://127.0.0.1:8088",
    )

    with pytest.raises(DecisionError, match="unrelated non-INFO"):
        cmd_apply(args, Client())

    assert state["backup"] is False


def test_post_health_rejects_same_code_issue_replacement(monkeypatch):
    before = {
        "nonInfoCodes": ["balance_mismatch"],
        "nonInfoActivityIds": {"balance_mismatch": ["old-activity"]},
        "nonInfoIssues": [{
            "identity": "balance_mismatch:old",
            "code": "balance_mismatch",
            "affectedIds": ["balance:old"],
            "activityIds": ["old-activity"],
        }],
    }
    replacement = {
        "nonInfoCodes": ["balance_mismatch"],
        "nonInfoActivityIds": {"balance_mismatch": ["new-activity"]},
        "nonInfoIssues": [{
            "identity": "balance_mismatch:new",
            "code": "balance_mismatch",
            "affectedIds": ["balance:new"],
            "activityIds": ["new-activity"],
        }],
    }
    monkeypatch.setattr(
        simplefin_apply,
        "_health_summary",
        lambda *args, **kwargs: replacement,
    )
    monkeypatch.setattr(simplefin_apply.time, "sleep", lambda _: None)
    plan = {"operations": {"metadataFinalizations": []}, "links": []}

    with pytest.raises(DecisionError, match="gained 1 non-INFO"):
        _assert_health_not_degraded(object(), before, plan)


@pytest.mark.parametrize(
    ("touched_after", "succeeds"),
    ((Decimal("100.00"), True), (Decimal("99.87"), False)),
)
def test_apply_attributes_cache_drift_but_rolls_back_unexplained_delta(
    tmp_path, monkeypatch, touched_after, succeeds
):
    from types import SimpleNamespace

    plan = build(tmp_path, [gap()])
    plan_path = tmp_path / "application-plan.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    (tmp_path / "normalized" / "simplefin").mkdir(parents=True)
    before = {
        "accountValues": {
            TARGET: Decimal("100.00"),
            "untouched-security": Decimal("100.12500000"),
        },
        "alternativeValues": {"property": Decimal("500.00")},
        "globalTotal": Decimal("700.12500000"),
    }
    after = {
        "accountValues": {
            TARGET: touched_after,
            "untouched-security": Decimal("100.00"),
        },
        "alternativeValues": {"property": Decimal("500.00")},
        "globalTotal": (
            touched_after
            + Decimal("100.00")
            + Decimal("500.00")
        ),
    }
    snapshots = iter((before, after))
    spending = iter((
        {"income": Decimal(), "spending": Decimal()},
        {
            "income": Decimal(plan["impact"]["incomeDelta"]),
            "spending": Decimal(plan["impact"]["spendingDelta"]),
        },
    ))
    state = {"applied": False, "rolledBack": False}

    class Client:
        def iter_activities(self):
            return iter([gap()])

        def list_accounts(self):
            return [
                {"id": TARGET, "accountType": "CASH"},
                {"id": "untouched-security", "accountType": "SECURITIES"},
            ]

        def post(self, path, payload):
            assert path == "/portfolio/recalculate"
            return {}

        def backup_database(self):
            return {"path": "synthetic-backup"}

    monkeypatch.setattr(simplefin_apply, "validate_apply_target", lambda *args: None)
    monkeypatch.setattr(
        simplefin_apply, "_validate_plan_environment", lambda *args: None
    )
    monkeypatch.setattr(simplefin_apply, "_already_applied", lambda *args: False)
    monkeypatch.setattr(
        simplefin_apply,
        "ledger_fingerprint",
        lambda *args: plan["ledgerFingerprint"],
    )
    monkeypatch.setattr(
        simplefin_apply, "_portfolio_values", lambda *args: next(snapshots)
    )
    monkeypatch.setattr(
        simplefin_apply,
        "_current_values",
        lambda *args: {TARGET: Decimal("100.00")},
    )
    monkeypatch.setattr(
        simplefin_apply, "_spending_totals", lambda *args: next(spending)
    )
    monkeypatch.setattr(
        simplefin_apply,
        "_health_summary",
        lambda *args, **kwargs: {"nonInfoCodeCounts": {}},
    )
    monkeypatch.setattr(
        simplefin_apply,
        "_assert_health_not_degraded",
        lambda *args: {
            "before": {"nonInfoCodeCounts": {}},
            "after": {"nonInfoCodeCounts": {}},
            "newNonInfoCount": 0,
        },
    )
    monkeypatch.setattr(
        simplefin_apply,
        "_apply_operations",
        lambda *args: state.update(applied=True),
    )
    monkeypatch.setattr(
        simplefin_apply,
        "_rollback",
        lambda *args: state.update(rolledBack=True),
    )
    monkeypatch.setattr(
        simplefin_apply,
        "expected_post_fingerprint",
        lambda *args: plan["expectedPostLedgerFingerprint"],
    )
    args = SimpleNamespace(
        application_plan=plan_path,
        data_dir=tmp_path,
        command="apply",
        base_url="http://127.0.0.1:18099",
        plan_fingerprint=plan["planFingerprint"],
        wait_seconds=0,
    )

    if not succeeds:
        with pytest.raises(DecisionError, match="balance assertion drifted"):
            cmd_apply(args, Client())
        assert state == {"applied": True, "rolledBack": True}
        return

    assert cmd_apply(args, Client()) == 0
    assert state == {"applied": True, "rolledBack": False}
    report_path = next(
        (tmp_path / "normalized" / "simplefin").glob("apply-report-*.json")
    )
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["schemaVersion"] == 5
    assert report["netWorth"]["globalDifference"] == "-0.12500000"
    assert (
        report["netWorth"]["untouchedAccountCacheDifference"]
        == "-0.12500000"
    )
    assert report["netWorth"]["unexplainedResidual"] == "0.00000000"


def test_activity_semantics_canonicalize_without_losing_external_flow():
    first = {
        **gap(),
        "subtype": " External_Transfer ",
        "metadata": {"source": "synthetic", "flow": {"is_external": True}},
    }
    equivalent = {
        **first,
        "subtype": "external_transfer",
        "metadata": '{"flow":{"is_external":true},"source":"synthetic"}',
    }
    changed = {
        **equivalent,
        "metadata": '{"flow":{"is_external":false},"source":"synthetic"}',
    }
    assert normalize_subtype(first["subtype"]) == "external_transfer"
    assert canonicalize_metadata(first["metadata"]) == equivalent["metadata"]
    assert canonicalize_metadata({}) == "{}"
    assert ledger_fingerprint([first], {TARGET}) == ledger_fingerprint(
        [equivalent], {TARGET}
    )
    assert ledger_fingerprint([first], {TARGET}) != ledger_fingerprint(
        [changed], {TARGET}
    )


def test_ambiguous_opposite_transfer_matches_are_manual():
    cash = pending("cash-payment", "checking", "CASH", "-100", "Card payment")
    card_a = pending("card-a", "card-a", "CREDIT_CARD", "100", "Payment received")
    card_b = pending("card-b", "card-b", "CREDIT_CARD", "100", "Payment received")
    pairs, ambiguous = _pair_pending_transfers([cash, card_a, card_b], {})
    assert pairs == []
    assert ambiguous == {
        ("source-checking", "cash-payment"),
        ("source-card-a", "card-a"),
        ("source-card-b", "card-b"),
    }


def test_same_source_id_in_three_accounts_does_not_leak_transfer_type(tmp_path):
    timestamp = int(datetime(2026, 8, 26, tzinfo=timezone.utc).timestamp())
    specs = [
        ("source-unrelated", "canonical-unrelated", "unrelated", "CASH",
         "collision", "-7", "Synthetic Market"),
        ("source-checking", "canonical-checking", "checking", "CASH",
         "collision", "-100", "Card payment"),
        ("source-card", "canonical-card", "card", "CREDIT_CARD",
         "card-payment", "100", "Payment received"),
    ]
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text(json.dumps({
        "errors": [],
        "accounts": [
            {
                "id": source_account,
                "name": f"Synthetic {target}",
                "org": {"name": "Synthetic Bank"},
                "currency": "USD",
                "balance": "100.00",
                "balance-date": timestamp,
                "transactions": [{
                    "id": source_id,
                    "posted": timestamp,
                    "amount": amount,
                    "description": description,
                }],
            }
            for (
                source_account, _, target, _, source_id, amount, description
            ) in specs
        ],
    }), encoding="utf-8")
    reviewed = {
        "schemaVersion": 1,
        "institutionErrors": [],
        "snapshot": str(snapshot),
        "accounts": [
            {
                "sourceAccountId": source_account,
                "status": "mapped",
                "assertionAccountId": canonical,
                "sourceBalance": "100.00",
                "balanceDate": "2026-08-26",
                "transactions": [{
                    "status": "planned",
                    "reason": None,
                    "sourceId": source_id,
                    "date": "2026-08-26",
                    "amount": amount,
                    "description": description,
                    "normalizedDescription": description.casefold(),
                }],
            }
            for (
                source_account, canonical, _, _, source_id, amount, description
            ) in specs
        ],
    }
    reviewed_path = tmp_path / "reviewed.json"
    reviewed_path.write_text(json.dumps(reviewed), encoding="utf-8")
    account_map = {
        canonical: target
        for _, canonical, target, _, _, _, _ in specs
    }
    activities = [
        {
            "id": f"gap-{target}",
            "accountId": target,
            "activityType": "TRANSFER_IN",
            "date": "2026-08-27T00:00:00Z",
            "amount": "200",
            "currency": "USD",
            "idempotencyKey": f"gap:{canonical}:2026-08-27",
            "sourceGroupId": None,
        }
        for _, canonical, target, _, _, _, _ in specs
    ]
    plan = build_application_plan(
        reviewed,
        reviewed_path,
        snapshot,
        account_map,
        activities,
        [
            {"id": target, "accountType": account_type}
            for _, _, target, account_type, _, _, _ in specs
        ],
        {target: Decimal("100") for _, _, target, _, _, _, _ in specs},
        "staging-environment",
        generated_at=NOW,
    )

    creates = {
        row["accountId"]: row for row in plan["operations"]["creates"]
    }
    assert creates["checking"]["activityType"] == "TRANSFER_OUT"
    assert creates["card"]["activityType"] == "TRANSFER_IN"
    assert creates["unrelated"]["activityType"] == "WITHDRAWAL"
    assert creates["unrelated"].get("subtype") is None
    link = plan["portableIntent"]["links"][0]
    assert {
        key: link[key]
        for key in (
            "leftSourceAccountId",
            "leftSourceId",
            "rightSourceAccountId",
            "rightSourceId",
        )
    } == {
        "leftSourceAccountId": "source-checking",
        "leftSourceId": "collision",
        "rightSourceAccountId": "source-card",
        "rightSourceId": "card-payment",
    }
    changed = deepcopy(plan)
    changed["portableIntent"]["links"][0]["leftSourceAccountId"] = (
        "source-unrelated"
    )
    changed["intentFingerprint"] = plan_fingerprint(changed["portableIntent"])
    assert changed["intentFingerprint"] != plan["intentFingerprint"]
    with pytest.raises(DecisionError, match="differs from the exactly rehearsed"):
        _validate_promotion_intent(changed, plan)


def test_durable_transfer_groups_use_account_scoped_source_ids(tmp_path):
    canonical = tmp_path / "normalized" / "canonical"
    canonical.mkdir(parents=True)
    (canonical / "transactions.csv").write_text(
        "source_id,transfer_group\n"
        "simplefin:source-one:shared,group-one\n"
        "simplefin:source-two:shared,group-two\n",
        encoding="utf-8",
    )
    reviewed = {
        "accounts": [
            {
                "sourceAccountId": source_account,
                "transactions": [{"sourceId": "shared"}],
            }
            for source_account in ("source-one", "source-two")
        ]
    }

    assert _durable_transfers(tmp_path, reviewed) == {
        ("source-one", "shared"): "group-one",
        ("source-two", "shared"): "group-two",
    }


def test_durable_transfer_member_cannot_belong_to_multiple_groups(tmp_path):
    canonical = tmp_path / "normalized" / "canonical"
    canonical.mkdir(parents=True)
    (canonical / "transactions.csv").write_text(
        "source_id,transfer_group\n"
        "simplefin:source-one:shared,group-one\n"
        "simplefin:source-one:shared,group-two\n",
        encoding="utf-8",
    )
    reviewed = {
        "accounts": [{
            "sourceAccountId": "source-one",
            "transactions": [{"sourceId": "shared"}],
        }]
    }

    with pytest.raises(DecisionError, match="conflicting groups"):
        _durable_transfers(tmp_path, reviewed)


def test_unique_late_overlap_is_deduplicated_without_manual_review(tmp_path):
    overlap = {
        "id": "other-source-id",
        "accountId": TARGET,
        "activityType": "WITHDRAWAL",
        "date": "2026-08-26T00:00:00Z",
        "amount": 20,
        "comment": "Synthetic, Market!",
        "idempotencyKey": "monarch:other-source-id",
    }
    plan = build(tmp_path, [gap(), overlap])
    assert plan["operations"] == {
        "creates": [],
        "updates": [],
        "deleteIds": [],
        "metadataFinalizations": [],
    }
    assert plan["manual"] == []


def test_balance_drift_blocks_only_remaining_transactions(tmp_path):
    reviewed, reviewed_path, snapshot = evidence(tmp_path)
    plan = build_application_plan(
        reviewed,
        reviewed_path,
        snapshot,
        {"canonical-account": TARGET},
        [gap()],
        [{"id": TARGET, "accountType": "CASH"}],
        {TARGET: Decimal("99.00")},
        "staging-environment",
        generated_at=NOW,
    )
    assert plan["operations"]["creates"] == []
    assert plan["manual"] == [{
        "sourceAccountId": "source-account",
        "sourceId": "txn-1",
        "reason": "balance-assertion-precondition",
        "drift": "-1.00",
        "decisionStatus": "unruled",
    }]


def test_empty_account_performance_is_treated_as_zero():
    class Client:
        def post(self, path, payload):
            return []

        def iter_activities(self):
            return iter([])

    assert _current_values(Client(), [{"id": "empty-account"}]) == {
        "empty-account": Decimal()
    }


def test_omitted_credit_card_value_is_reconstructed_from_activities():
    class Client:
        def post(self, path, payload):
            return []

        def iter_activities(self):
            return iter([
                {"accountId": "card", "activityType": "CREDIT", "amount": 25},
                {"accountId": "card", "activityType": "WITHDRAWAL", "amount": 40},
            ])

    assert _current_values(
        Client(), [{"id": "card", "accountType": "CREDIT_CARD"}]
    ) == {"card": Decimal("-15")}


def test_sealed_environment_must_match_live_instance(monkeypatch):
    monkeypatch.setattr(
        "importers.simplefin.apply_cli.instance_fingerprint",
        lambda client, base_url: "live",
    )
    with pytest.raises(DecisionError, match="different staging instance"):
        _validate_plan_environment(object(), "http://127.0.0.1:18088", {
            "environmentFingerprint": "sealed"
        })


def test_rollback_restores_metadata_after_committed_put_timeout(tmp_path):
    original = {**gap(), "metadata": {"source": "synthetic"}}
    plan = build(tmp_path, [original])
    created = {
        "id": "created-id",
        **plan["operations"]["creates"][0],
        "date": plan["operations"]["creates"][0]["activityDate"],
    }
    changed = {
        **original,
        **plan["operations"]["updates"][0],
        "date": plan["operations"]["updates"][0]["activityDate"],
    }

    class Client:
        rows = [created, changed]
        put_calls = 0

        def iter_activities(self):
            return iter(self.rows)

        def save_activities(self, *, creates, updates, delete_ids):
            self.rows = [row for row in self.rows if row["id"] not in delete_ids]
            by_id = {row["id"]: row for row in self.rows}
            for payload in updates:
                row = by_id[payload["id"]]
                row.update(payload)
                row["date"] = payload["activityDate"]
                row["metadata"] = None
            return {"created": [], "updated": updates, "deleted": delete_ids}

        def post(self, path, payload):
            return {}

        def put(self, path, payload):
            self.put_calls += 1
            result = persist_activity_put(self, path, payload)
            if self.put_calls == 1:
                raise TimeoutError("response lost after rollback metadata commit")
            return result

    client = Client()
    _rollback(client, plan)
    assert [row["id"] for row in client.rows] == ["gap-id"]
    assert client.rows[0]["activityType"] == "TRANSFER_IN"
    assert Decimal(str(client.rows[0]["amount"])) == Decimal("50")
    assert canonicalize_metadata(client.rows[0]["metadata"]) == (
        '{"source":"synthetic"}'
    )
    assert client.put_calls == 1


def test_bulk_update_metadata_is_finalized_after_committed_timeout(tmp_path):
    plan = build(tmp_path, [{**gap(), "metadata": None}])

    class Client:
        rows = [gap()]
        put_calls = 0

        def iter_activities(self):
            return iter(self.rows)

        def save_activities(self, *, creates, updates, delete_ids):
            made = {
                "id": "created-id",
                **creates[0],
                "date": creates[0]["activityDate"],
            }
            changed = {
                **self.rows[0],
                **updates[0],
                "date": updates[0]["activityDate"],
                "metadata": None,
            }
            self.rows = [made, changed]
            return {"created": [made], "updated": updates, "deleted": []}

        def put(self, path, payload):
            self.put_calls += 1
            result = persist_activity_put(self, path, payload)
            if self.put_calls == 1:
                raise TimeoutError("response lost after metadata commit")
            return result

        def post(self, path, payload):
            return {}

    client = Client()
    _apply_operations(client, plan)

    finalization = plan["operations"]["metadataFinalizations"][0]
    persisted = next(row for row in client.rows if row["id"] == "gap-id")
    assert client.put_calls == 1
    assert canonicalize_metadata(persisted["metadata"]) == (
        '{"flow":{"is_external":true}}'
    )
    assert _already_applied(plan, client.rows)
    assert (
        simplefin_apply.activity_semantic_fingerprint(persisted)
        == finalization["expectedFingerprint"]
    )


def test_transport_ambiguity_is_rolled_back_and_cannot_look_applied(tmp_path):
    plan = build(tmp_path, [gap()])

    class Client:
        rows = [gap()]
        calls = 0

        def iter_activities(self):
            return iter(self.rows)

        def save_activities(self, *, creates, updates, delete_ids):
            self.calls += 1
            if self.calls == 1:
                created = {
                    "id": "created-id",
                    **creates[0],
                    "date": creates[0]["activityDate"],
                }
                changed = {
                    **self.rows[0],
                    **updates[0],
                    "date": updates[0]["activityDate"],
                }
                self.rows = [created, changed]
                raise TimeoutError("response lost")
            self.rows = [row for row in self.rows if row["id"] not in delete_ids]
            by_id = {row["id"]: row for row in self.rows}
            for payload in updates:
                by_id[payload["id"]].update(payload)
                by_id[payload["id"]]["date"] = payload["activityDate"]
            return {"created": [], "updated": updates, "deleted": delete_ids}

        def post(self, path, payload):
            return {}

        def put(self, path, payload):
            return persist_activity_put(self, path, payload)

    client = Client()
    with pytest.raises(TimeoutError, match="response lost"):
        _apply_operations(client, plan)
    assert not _already_applied(plan, client.rows)
    assert [row["id"] for row in client.rows] == ["gap-id"]


def test_rollback_continues_after_committed_unlink_timeout(tmp_path):
    plan = build(tmp_path, [gap()])
    first = plan["operations"]["creates"][0]
    second = {
        **first,
        "idempotencyKey": f"{first['idempotencyKey']}:other",
    }
    plan["operations"]["creates"].append(second)
    plan["links"] = [{
        "leftKey": first["idempotencyKey"],
        "rightKey": second["idempotencyKey"],
    }]
    changed = {
        **gap(),
        **plan["operations"]["updates"][0],
        "date": plan["operations"]["updates"][0]["activityDate"],
    }

    class Client:
        rows = [
            {
                "id": "created-first",
                **first,
                "date": first["activityDate"],
                "sourceGroupId": "linked-group",
            },
            {
                "id": "created-second",
                **second,
                "date": second["activityDate"],
                "sourceGroupId": "linked-group",
            },
            changed,
        ]
        bulk_restored = False

        def iter_activities(self):
            return iter(self.rows)

        def post(self, path, payload):
            if path == "/activities/unlink":
                for row in self.rows:
                    if row["id"] in {
                        payload["activityAId"],
                        payload["activityBId"],
                    }:
                        row["sourceGroupId"] = None
                raise TimeoutError("unlink response lost after commit")
            return {}

        def save_activities(self, *, creates, updates, delete_ids):
            self.bulk_restored = True
            self.rows = [row for row in self.rows if row["id"] not in delete_ids]
            by_id = {row["id"]: row for row in self.rows}
            for payload in updates:
                by_id[payload["id"]].update(payload)
                by_id[payload["id"]]["date"] = payload["activityDate"]
            return {"created": [], "updated": updates, "deleted": delete_ids}

        def put(self, path, payload):
            return persist_activity_put(self, path, payload)

    client = Client()
    _rollback(client, plan)
    assert client.bulk_restored
    assert [row["id"] for row in client.rows] == ["gap-id"]
    assert ledger_fingerprint(
        client.rows, set(plan["ledgerAccountIds"])
    ) == plan["ledgerFingerprint"]


def test_link_failure_rolls_back_created_transfer_pair(tmp_path):
    plan = build(tmp_path, [gap()])
    first = plan["operations"]["creates"][0]
    second = {
        **first,
        "accountId": "other-account",
        "idempotencyKey": "simplefin:other-account:other-id",
    }
    plan["operations"]["creates"].append(second)
    plan["links"] = [{
        "leftKey": first["idempotencyKey"],
        "rightKey": second["idempotencyKey"],
    }]

    class Client:
        rows = [gap()]
        save_calls = 0

        def iter_activities(self):
            return iter(self.rows)

        def save_activities(self, *, creates, updates, delete_ids):
            self.save_calls += 1
            if self.save_calls == 1:
                made = []
                for index, payload in enumerate(creates):
                    row = {
                        "id": f"created-{index}",
                        **payload,
                        "date": payload["activityDate"],
                    }
                    made.append(row)
                    self.rows.append(row)
                self.rows[0].update(updates[0])
                self.rows[0]["date"] = updates[0]["activityDate"]
                return {"created": made, "updated": updates, "deleted": []}
            self.rows = [row for row in self.rows if row["id"] not in delete_ids]
            self.rows[0].update(updates[0])
            self.rows[0]["date"] = updates[0]["activityDate"]
            return {"created": [], "updated": updates, "deleted": delete_ids}

        def post(self, path, payload):
            if path == "/activities/link":
                raise RuntimeError("link failed")
            return {}

        def put(self, path, payload):
            return persist_activity_put(self, path, payload)

    client = Client()
    with pytest.raises(RuntimeError, match="link failed"):
        _apply_operations(client, plan)
    assert [row["id"] for row in client.rows] == ["gap-id"]
    assert Decimal(str(client.rows[0]["amount"])) == Decimal("50")


def test_successful_but_noop_link_is_detected_and_rolled_back(tmp_path):
    plan = build(tmp_path, [gap()])
    first = plan["operations"]["creates"][0]
    second = {
        **first,
        "accountId": "other-account",
        "idempotencyKey": "simplefin:other-account:other-id",
    }
    plan["operations"]["creates"].append(second)
    plan["links"] = [{
        "leftKey": first["idempotencyKey"],
        "rightKey": second["idempotencyKey"],
    }]
    plan["ledgerAccountIds"].append("other-account")
    plan["expectedPostLedgerFingerprint"] = expected_post_fingerprint(
        [gap()],
        set(plan["ledgerAccountIds"]),
        plan["operations"],
        plan["links"],
    )

    class Client:
        rows = [gap()]
        save_calls = 0

        def iter_activities(self):
            return iter(self.rows)

        def save_activities(self, *, creates, updates, delete_ids):
            self.save_calls += 1
            if self.save_calls == 1:
                made = []
                for index, payload in enumerate(creates):
                    row = {
                        "id": f"created-{index}",
                        **payload,
                        "date": payload["activityDate"],
                    }
                    made.append(row)
                    self.rows.append(row)
                self.rows[0].update(updates[0])
                self.rows[0]["date"] = updates[0]["activityDate"]
                return {"created": made, "updated": updates, "deleted": []}
            self.rows = [row for row in self.rows if row["id"] not in delete_ids]
            self.rows[0].update(updates[0])
            self.rows[0]["date"] = updates[0]["activityDate"]
            return {"created": [], "updated": updates, "deleted": delete_ids}

        def post(self, path, payload):
            if path == "/activities/link":
                return {"success": True}
            return {}

        def put(self, path, payload):
            return persist_activity_put(self, path, payload)

    client = Client()
    with pytest.raises(DecisionError, match="sealed post-apply"):
        _apply_operations(client, plan)
    assert [row["id"] for row in client.rows] == ["gap-id"]
    assert Decimal(str(client.rows[0]["amount"])) == Decimal("50")


def test_persisted_field_mismatch_is_detected_and_rolled_back(tmp_path):
    plan = build(tmp_path, [gap()])

    class Client:
        rows = [gap()]
        save_calls = 0

        def iter_activities(self):
            return iter(self.rows)

        def save_activities(self, *, creates, updates, delete_ids):
            self.save_calls += 1
            if self.save_calls == 1:
                made = {
                    "id": "created-id",
                    **creates[0],
                    "date": creates[0]["activityDate"],
                }
                changed = {
                    **self.rows[0],
                    **updates[0],
                    "date": updates[0]["activityDate"],
                    "currency": "EUR",
                }
                self.rows = [made, changed]
                return {"created": [made], "updated": updates, "deleted": []}
            self.rows = [row for row in self.rows if row["id"] not in delete_ids]
            self.rows[0].update(updates[0])
            self.rows[0]["date"] = updates[0]["activityDate"]
            return {"created": [], "updated": updates, "deleted": delete_ids}

        def post(self, path, payload):
            return {}

    client = Client()
    with pytest.raises(DecisionError, match="sealed state"):
        _apply_operations(client, plan)
    assert [row["id"] for row in client.rows] == ["gap-id"]
    assert client.rows[0]["currency"] == "USD"


def test_post_apply_tampering_is_not_idempotent(tmp_path):
    plan = build(tmp_path, [gap()])
    created = {
        "id": "created-id",
        **plan["operations"]["creates"][0],
        "date": plan["operations"]["creates"][0]["activityDate"],
    }
    updated = {
        **gap(),
        **plan["operations"]["updates"][0],
        "date": plan["operations"]["updates"][0]["activityDate"],
    }
    assert _already_applied(plan, [created, updated])
    tampered = {
        "id": "unrelated",
        "accountId": TARGET,
        "activityType": "DEPOSIT",
        "date": "2026-08-27T00:00:00Z",
        "amount": 1,
        "currency": "USD",
        "idempotencyKey": "other:unexpected",
    }
    assert not _already_applied(plan, [created, updated, tampered])


@pytest.mark.parametrize(
    "semantic_change",
    (
        {"subtype": None},
        {"metadata": '{"flow":{"is_external":false}}'},
    ),
)
def test_cash_flow_semantic_tampering_is_not_idempotent(
    tmp_path, semantic_change
):
    plan = build(tmp_path, [gap()])
    created = {
        "id": "created-id",
        **plan["operations"]["creates"][0],
        "date": plan["operations"]["creates"][0]["activityDate"],
    }
    updated = {
        **gap(),
        **plan["operations"]["updates"][0],
        "date": plan["operations"]["updates"][0]["activityDate"],
    }
    assert _already_applied(plan, [created, updated])
    assert not _already_applied(plan, [created, {**updated, **semantic_change}])


def test_semantic_only_portable_intent_difference_rejects_promotion(tmp_path):
    staging = build(tmp_path, [gap()])
    production = deepcopy(staging)
    assert staging["portableIntent"]["creates"][0]["metadata"] == "{}"
    assert staging["portableIntent"]["creates"][0]["subtype"] is None
    assert staging["portableIntent"]["updates"][0]["metadata"] == (
        '{"flow":{"is_external":true}}'
    )
    assert staging["portableIntent"]["updates"][0]["subtype"] == (
        "external_transfer"
    )

    production["portableIntent"]["creates"][0]["metadata"] = (
        '{"flow":{"is_external":true}}'
    )
    production["intentFingerprint"] = plan_fingerprint(
        production["portableIntent"]
    )
    assert production["intentFingerprint"] != staging["intentFingerprint"]
    with pytest.raises(DecisionError, match="differs from the exactly rehearsed"):
        _validate_promotion_intent(production, staging)


def test_metadata_finalization_difference_rejects_promotion(tmp_path):
    staging = build(tmp_path, [gap()])
    production = deepcopy(staging)
    assert staging["portableIntent"]["metadataFinalizations"]
    production["portableIntent"]["metadataFinalizations"][0]["metadata"] = (
        '{"flow":{"is_external":false}}'
    )
    production["intentFingerprint"] = plan_fingerprint(
        production["portableIntent"]
    )

    with pytest.raises(DecisionError, match="differs from the exactly rehearsed"):
        _validate_promotion_intent(production, staging)


def test_only_explicit_fidelity_reconciliation_prefix_is_selected(tmp_path):
    ordinary = {**gap(), "idempotencyKey": "fidelity:ordinary-transaction"}
    plan = build(tmp_path, [ordinary])
    assert plan["operations"]["creates"] == []
    assert plan["manual"][0]["reason"] == "missing-reconciliation"


def test_exact_source_id_makes_the_sealed_plan_idempotent(tmp_path):
    plan = build(tmp_path, [gap()])
    created = {
        "id": "created-id",
        **plan["operations"]["creates"][0],
        "date": plan["operations"]["creates"][0]["activityDate"],
    }
    updated = {
        **gap(),
        **plan["operations"]["updates"][0],
        "date": plan["operations"]["updates"][0]["activityDate"],
    }
    assert _already_applied(plan, [created, updated])


def test_plan_and_snapshot_hashes_are_revalidated_at_apply(tmp_path):
    plan = build(tmp_path, [gap()])
    validate_plan_seal(plan)
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text("{}", encoding="utf-8")
    with pytest.raises(DecisionError, match="evidence hash changed"):
        validate_plan_seal(plan)


def decision(source_id="txn-1", *, ruling="hold", reason="balance-assertion-precondition"):
    return {
        "decisionId": f"review-{source_id}",
        "sourceAccountId": "source-account",
        "sourceId": source_id,
        "detectedReason": reason,
        "ruling": ruling,
        "rationale": "Synthetic source evidence requires a durable hold.",
        "evidence": [{"path": "synthetic", "sha256": "synthetic"}],
    }


def test_manual_decisions_require_current_private_hashed_evidence(tmp_path):
    source = tmp_path / "evidence.json"
    source.write_text('{"synthetic":true}', encoding="utf-8")
    document = {
        "schemaVersion": 1,
        "decisions": [{
            **decision(),
            "evidence": [{"path": str(source), "sha256": sha256_file(source)}],
        }],
    }
    path = tmp_path / "manual-decisions.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    loaded = load_manual_decisions(path, tmp_path)
    assert loaded[("source-account", "txn-1")]["ruling"] == "hold"

    source.write_text('{"synthetic":false}', encoding="utf-8")
    with pytest.raises(DecisionError, match="evidence changed"):
        load_manual_decisions(path, tmp_path)


def test_production_revalidates_complete_manual_decision_evidence(tmp_path):
    source = tmp_path / "evidence.json"
    source.write_text('{"synthetic":true}', encoding="utf-8")
    decisions_path = tmp_path / "manual-decisions.json"
    decisions_path.write_text(json.dumps({
        "schemaVersion": 1,
        "decisions": [{
            **decision(),
            "evidence": [{"path": str(source), "sha256": sha256_file(source)}],
        }],
    }), encoding="utf-8")
    binding = manual_decision_evidence_binding(decisions_path, tmp_path)
    plan = {
        "evidence": {
            "manualDecisions": {
                "path": str(decisions_path),
                "sha256": sha256_file(decisions_path),
            },
        },
        "decisionEvidence": binding,
        "portableIntent": {"decisionEvidence": binding},
    }
    staging_plan = {
        "decisionEvidence": binding,
        "portableIntent": {"decisionEvidence": binding},
    }
    staging_receipt = {"decisionEvidence": binding}
    assert _revalidate_production_decisions(
        plan, staging_plan, staging_receipt, tmp_path
    ) == binding

    source.write_text('{"synthetic":false}', encoding="utf-8")
    with pytest.raises(DecisionError, match="evidence changed"):
        _revalidate_production_decisions(
            plan, staging_plan, staging_receipt, tmp_path
        )


def test_durable_hold_rules_a_manual_row_and_seals_portable_intent(tmp_path):
    ruling = decision()
    reviewed, reviewed_path, snapshot = evidence(tmp_path)
    plan = build_application_plan(
        reviewed,
        reviewed_path,
        snapshot,
        {"canonical-account": TARGET},
        [gap()],
        [{"id": TARGET, "accountType": "CASH"}],
        {TARGET: Decimal("99.00")},
        "staging-environment",
        manual_decisions={("source-account", "txn-1"): ruling},
        generated_at=NOW,
    )
    assert plan["manual"][0]["decisionStatus"] == "ruled"
    assert plan["manual"][0]["ruling"] == "hold"
    assert plan["portableIntent"]["manual"][0]["ruling"] == "hold"
    assert plan["intentFingerprint"]


def test_pair_existing_ruling_links_a_unique_reviewed_counterpart(tmp_path):
    reviewed, reviewed_path, snapshot = evidence(tmp_path)
    raw = json.loads(snapshot.read_text(encoding="utf-8"))
    raw["accounts"][0]["transactions"][0]["description"] = "Card payment"
    raw["accounts"].append({
        "id": "counterpart-source-account",
        "name": "Synthetic Card",
        "org": {"name": "Synthetic Bank"},
        "currency": "USD",
        "balance": "-50.00",
        "balance-date": raw["accounts"][0]["balance-date"],
        "transactions": [{
            "id": "counterpart-txn",
            "posted": raw["accounts"][0]["balance-date"],
            "amount": "20.00",
            "description": "Payment received",
        }],
    })
    snapshot.write_text(json.dumps(raw), encoding="utf-8")
    reviewed["accounts"][0]["transactions"][0].update({
        "description": "Card payment",
        "normalizedDescription": "card payment",
    })
    reviewed["accounts"].append({
        "sourceAccountId": "counterpart-source-account",
        "status": "mapped",
        "assertionAccountId": "counterpart-canonical",
        "sourceBalance": "-50.00",
        "balanceDate": "2026-08-26",
        "transactions": [{
            "status": "skipped",
            "reason": "duplicate-overlap",
            "sourceId": "counterpart-txn",
            "date": "2026-08-26",
            "amount": "20.00",
            "description": "Payment received",
            "normalizedDescription": "payment received",
        }],
    })
    reviewed_path.write_text(json.dumps(reviewed), encoding="utf-8")
    existing = {
        "id": "existing-counterpart",
        "accountId": "card-account",
        "activityType": "CREDIT",
        "date": "2026-08-26T00:00:00Z",
        "amount": 20,
        "currency": "USD",
        "comment": "Payment received",
        "idempotencyKey": "canonical:counterpart",
        "metadata": {
            "source": "synthetic",
            "flow": {"is_external": True},
        },
    }
    ruling = {
        **decision(ruling="pair-existing", reason="ambiguous-transfer-match"),
        "counterpartSourceAccountId": "counterpart-source-account",
        "counterpartSourceId": "counterpart-txn",
    }
    plan = build_application_plan(
        reviewed,
        reviewed_path,
        snapshot,
        {
            "canonical-account": TARGET,
            "counterpart-canonical": "card-account",
        },
        [gap(), existing],
        [
            {"id": TARGET, "accountType": "CASH"},
            {"id": "card-account", "accountType": "CREDIT_CARD"},
        ],
        {TARGET: Decimal("100.00"), "card-account": Decimal("-50.00")},
        "staging-environment",
        manual_decisions={("source-account", "txn-1"): ruling},
        generated_at=NOW,
    )
    assert plan["manual"] == []
    assert plan["operations"]["creates"][0]["activityType"] == "TRANSFER_OUT"
    assert plan["links"] == [{
        "leftKey": f"simplefin:{TARGET}:txn-1",
        "rightActivityId": "existing-counterpart",
        "leftSourceAccountId": "source-account",
        "leftSourceId": "txn-1",
        "rightSourceAccountId": "counterpart-source-account",
        "rightSourceId": "counterpart-txn",
    }]
    assert plan["portableIntent"]["links"] == [{
        "leftSourceAccountId": "source-account",
        "leftSourceId": "txn-1",
        "rightSourceAccountId": "counterpart-source-account",
        "rightSourceId": "counterpart-txn",
        "left": {
            "subtype": "external_transfer",
            "metadata": '{"flow":{"is_external":false}}',
        },
        "right": {
            "subtype": None,
            "metadata": '{"flow":{"is_external":false},"source":"synthetic"}',
        },
    }]
    finalization = next(
        operation
        for operation in plan["operations"]["metadataFinalizations"]
        if operation["activityId"] == "existing-counterpart"
    )
    assert finalization["expectedLinkGroupFingerprint"]
    assert (
        finalization["expectedPostLinkFingerprint"]
        != finalization["expectedFingerprint"]
    )

    class Client:
        rows = [deepcopy(gap()), deepcopy(existing)]

        def iter_activities(self):
            return iter(self.rows)

        def save_activities(self, *, creates, updates, delete_ids):
            assert delete_ids == []
            created = []
            for index, payload in enumerate(creates):
                row = {
                    "id": f"created-{index}",
                    **payload,
                    "date": payload["activityDate"],
                }
                self.rows.append(row)
                created.append(row)
            by_id = {row["id"]: row for row in self.rows}
            for payload in updates:
                by_id[payload["id"]].update(payload)
                by_id[payload["id"]]["date"] = payload["activityDate"]
                by_id[payload["id"]]["metadata"] = None
            return {
                "created": created,
                "updated": updates,
                "deleted": [],
            }

        def put(self, path, payload):
            return persist_activity_put(self, path, payload)

        def post(self, path, payload):
            if path == "/portfolio/recalculate":
                return {}
            assert path == "/activities/link"
            group_id = "synthetic-linked-group"
            for row in self.rows:
                if row["id"] in {
                    payload["activityAId"],
                    payload["activityBId"],
                }:
                    row["sourceGroupId"] = group_id
                    row["metadata"] = simplefin_apply.metadata_with_external_flow(
                        row.get("metadata"), False
                    )
            return {"sourceGroupId": group_id}

    client = Client()
    _apply_operations(client, plan)

    persisted = next(
        row for row in client.rows if row["id"] == "existing-counterpart"
    )
    assert canonicalize_metadata(persisted["metadata"]) == (
        '{"flow":{"is_external":false},"source":"synthetic"}'
    )
    assert _already_applied(plan, client.rows)
    contaminated = deepcopy(client.rows)
    contaminated.append({
        **gap(),
        "id": "unexpected-third-member",
        "sourceGroupId": persisted["sourceGroupId"],
    })
    assert not _already_applied(plan, contaminated)
    assert plan["intentFingerprint"]


def test_pair_existing_cannot_reclaim_automatically_paired_member(tmp_path):
    timestamp = int(datetime(2026, 8, 26, tzinfo=timezone.utc).timestamp())
    specs = [
        ("src-a", "canonical-a", "target-a", "a", "-20.00", "Card payment"),
        ("src-b", "canonical-b", "target-b", "b", "20.00", "Payment received"),
        ("src-c", "canonical-c", "target-c", "c", "20.00", "Payment received"),
    ]
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text(json.dumps({
        "errors": [],
        "accounts": [
            {
                "id": source_account,
                "name": f"Synthetic {source_account}",
                "org": {"name": "Synthetic Bank"},
                "currency": "USD",
                "balance": "100.00",
                "balance-date": timestamp,
                "transactions": [{
                    "id": source_id,
                    "posted": timestamp,
                    "amount": amount,
                    "description": description,
                }],
            }
            for (
                source_account, _, _, source_id, amount, description
            ) in specs
        ],
    }), encoding="utf-8")
    reviewed = {
        "schemaVersion": 1,
        "institutionErrors": [],
        "snapshot": str(snapshot),
        "accounts": [
            {
                "sourceAccountId": source_account,
                "status": "mapped",
                "assertionAccountId": canonical,
                "sourceBalance": "100.00",
                "balanceDate": "2026-08-26",
                "transactions": [{
                    "status": "skipped" if source_account == "src-c" else "planned",
                    "reason": (
                        "duplicate-overlap" if source_account == "src-c" else None
                    ),
                    "sourceId": source_id,
                    "date": "2026-08-26",
                    "amount": amount,
                    "description": description,
                    "normalizedDescription": description.casefold(),
                }],
            }
            for (
                source_account, canonical, _, source_id, amount, description
            ) in specs
        ],
    }
    reviewed_path = tmp_path / "reviewed.json"
    reviewed_path.write_text(json.dumps(reviewed), encoding="utf-8")
    existing = {
        "id": "existing-c",
        "accountId": "target-c",
        "activityType": "CREDIT",
        "date": "2026-08-26T00:00:00Z",
        "amount": 20,
        "currency": "USD",
        "comment": "Payment received",
        "idempotencyKey": "canonical:existing-c",
    }
    ruling = {
        **decision("a", ruling="pair-existing", reason="ambiguous-transfer-match"),
        "sourceAccountId": "src-a",
        "counterpartSourceAccountId": "src-c",
        "counterpartSourceId": "c",
    }

    with pytest.raises(DecisionError, match="transfer member is claimed more than once"):
        build_application_plan(
            reviewed,
            reviewed_path,
            snapshot,
            {
                canonical: target
                for _, canonical, target, _, _, _ in specs
            },
            [existing],
            [
                {"id": "target-a", "accountType": "CASH"},
                {"id": "target-b", "accountType": "CREDIT_CARD"},
                {"id": "target-c", "accountType": "CREDIT_CARD"},
            ],
            {
                "target-a": Decimal("100.00"),
                "target-b": Decimal("100.00"),
                "target-c": Decimal("100.00"),
            },
            "staging-environment",
            manual_decisions={("src-a", "a"): ruling},
            generated_at=NOW,
        )


@pytest.mark.parametrize("distinct_counterpart_identity", (False, True))
def test_pair_existing_rulings_cannot_claim_one_activity_twice(
    tmp_path, distinct_counterpart_identity
):
    reviewed, reviewed_path, snapshot = evidence(tmp_path)
    raw = json.loads(snapshot.read_text(encoding="utf-8"))
    raw["accounts"][0]["transactions"][0]["description"] = "Card payment"
    raw["accounts"].append({
        "id": "source-account-2",
        "name": "Synthetic Checking Two",
        "org": {"name": "Synthetic Bank"},
        "currency": "USD",
        "balance": "100.00",
        "balance-date": raw["accounts"][0]["balance-date"],
        "transactions": [{
            "id": "txn-2",
            "posted": raw["accounts"][0]["balance-date"],
            "amount": "-20.00",
            "description": "Card payment",
        }],
    })
    counterpart_ids = (
        ("counterpart-source-account", "counterpart-txn"),
        ("counterpart-source-account-2", "counterpart-txn-2"),
    )
    if not distinct_counterpart_identity:
        counterpart_ids = (counterpart_ids[0],)
    for source_account_id, source_id in counterpart_ids:
        raw["accounts"].append({
            "id": source_account_id,
            "name": f"Synthetic Card {source_id}",
            "org": {"name": "Synthetic Bank"},
            "currency": "USD",
            "balance": "-50.00",
            "balance-date": raw["accounts"][0]["balance-date"],
            "transactions": [{
                "id": source_id,
                "posted": raw["accounts"][0]["balance-date"],
                "amount": "20.00",
                "description": "Payment received",
            }],
        })
    snapshot.write_text(json.dumps(raw), encoding="utf-8")

    reviewed["accounts"][0]["transactions"][0].update({
        "description": "Card payment",
        "normalizedDescription": "card payment",
    })
    reviewed["accounts"].append({
        "sourceAccountId": "source-account-2",
        "status": "mapped",
        "assertionAccountId": "canonical-account-2",
        "sourceBalance": "100.00",
        "balanceDate": "2026-08-26",
        "transactions": [{
            "status": "planned",
            "reason": None,
            "sourceId": "txn-2",
            "date": "2026-08-26",
            "amount": "-20.00",
            "description": "Card payment",
            "normalizedDescription": "card payment",
        }],
    })
    for index, (source_account_id, source_id) in enumerate(counterpart_ids):
        reviewed["accounts"].append({
            "sourceAccountId": source_account_id,
            "status": "mapped",
            "assertionAccountId": f"counterpart-canonical-{index}",
            "sourceBalance": "-50.00",
            "balanceDate": "2026-08-26",
            "transactions": [{
                "status": "skipped",
                "reason": "duplicate-overlap",
                "sourceId": source_id,
                "date": "2026-08-26",
                "amount": "20.00",
                "description": "Payment received",
                "normalizedDescription": "payment received",
            }],
        })
    reviewed_path.write_text(json.dumps(reviewed), encoding="utf-8")

    second_counterpart = (
        counterpart_ids[-1] if distinct_counterpart_identity else counterpart_ids[0]
    )
    rulings = {
        ("source-account", "txn-1"): {
            **decision(ruling="pair-existing", reason="ambiguous-transfer-match"),
            "counterpartSourceAccountId": counterpart_ids[0][0],
            "counterpartSourceId": counterpart_ids[0][1],
        },
        ("source-account-2", "txn-2"): {
            **decision(
                "txn-2",
                ruling="pair-existing",
                reason="ambiguous-transfer-match",
            ),
            "decisionId": "review-txn-2",
            "sourceAccountId": "source-account-2",
            "counterpartSourceAccountId": second_counterpart[0],
            "counterpartSourceId": second_counterpart[1],
        },
    }
    stage_map = {
        "canonical-account": TARGET,
        "canonical-account-2": "stage-account-2",
        **{
            f"counterpart-canonical-{index}": "card-account"
            for index in range(len(counterpart_ids))
        },
    }
    existing = {
        "id": "existing-counterpart",
        "accountId": "card-account",
        "activityType": "CREDIT",
        "date": "2026-08-26T00:00:00Z",
        "amount": 20,
        "currency": "USD",
        "comment": "Payment received",
        "idempotencyKey": "canonical:counterpart",
    }

    with pytest.raises(DecisionError, match="claimed more than once"):
        build_application_plan(
            reviewed,
            reviewed_path,
            snapshot,
            stage_map,
            [existing],
            [
                {"id": TARGET, "accountType": "CASH"},
                {"id": "stage-account-2", "accountType": "CASH"},
                {"id": "card-account", "accountType": "CREDIT_CARD"},
            ],
            {
                TARGET: Decimal("100.00"),
                "stage-account-2": Decimal("100.00"),
                "card-account": Decimal("-50.00"),
            },
            "staging-environment",
            manual_decisions=rulings,
            generated_at=NOW,
        )


def test_staging_receipt_must_exactly_seal_plan_and_postconditions(tmp_path):
    plan = build(tmp_path, [gap()])
    plan_path = tmp_path / "application-plan.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    receipt = {
        "schemaVersion": 5,
        "status": "applied",
        "applicationPlanSha256": sha256_file(plan_path),
        "planFingerprint": plan["planFingerprint"],
        "intentFingerprint": plan["intentFingerprint"],
        "decisionEvidence": plan.get("decisionEvidence"),
        "environmentFingerprint": plan["environmentFingerprint"],
        "preLedgerFingerprint": plan["ledgerFingerprint"],
        "postLedgerFingerprint": plan["expectedPostLedgerFingerprint"],
        "backup": {"path": "synthetic-backup"},
        "operations": {
            key: len(value) for key, value in plan["operations"].items()
        },
        "netWorth": {
            "globalBefore": "100",
            "globalAfter": "100",
            "globalDifference": "0",
            "touchedAccountDifference": "0",
            "untouchedAccountCacheDifference": "0",
            "alternativeHoldingsDifference": "0",
            "unexplainedResidual": "0",
        },
        "health": {"newNonInfoCount": 0},
        "spendingReport": {
            "incomeDelta": plan["impact"]["incomeDelta"],
            "spendingDelta": plan["impact"]["spendingDelta"],
        },
        "assertions": [
            {
                "canonicalAccountId": row["canonicalAccountId"],
                "afterDrift": "0",
            }
            for row in plan["assertions"]
        ],
    }
    receipt_path = tmp_path / "receipt.json"
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    sealed, _ = _validate_staging_receipt(plan_path, receipt_path)
    assert sealed["intentFingerprint"] == plan["intentFingerprint"]

    receipt["postLedgerFingerprint"] = "changed"
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    with pytest.raises(DecisionError, match="exactly seal"):
        _validate_staging_receipt(plan_path, receipt_path)

    receipt["postLedgerFingerprint"] = plan["expectedPostLedgerFingerprint"]
    receipt["decisionEvidence"] = {"fingerprint": "different"}
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    with pytest.raises(DecisionError, match="exactly seal"):
        _validate_staging_receipt(plan_path, receipt_path)

    receipt["decisionEvidence"] = plan.get("decisionEvidence")
    receipt["netWorth"]["untouchedAccountCacheDifference"] = "1"
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    with pytest.raises(DecisionError, match="attribution is inconsistent"):
        _validate_staging_receipt(plan_path, receipt_path)


def test_production_authorization_requires_explicit_flags_and_exact_sha(tmp_path):
    from types import SimpleNamespace

    plan_path = tmp_path / "production-plan.json"
    plan_path.write_text('{"synthetic":true}', encoding="utf-8")
    plan = {
        "mode": "production-promotion-plan",
        "manual": [{"decisionStatus": "ruled"}],
    }
    args = SimpleNamespace(
        base_url="http://127.0.0.1:8088",
        allow_production=False,
        expected_plan_sha=sha256_file(plan_path),
    )
    with pytest.raises(DecisionError, match="allow-production"):
        _validate_production_authorization(args, plan_path, plan)
    args.allow_production = True
    _validate_production_authorization(args, plan_path, plan)


# ---------------------------------------------------------------------------
# Milestone 6: `_spending_totals` no longer crashes on an unsupported endpoint
# ---------------------------------------------------------------------------


class _SpendingTotalsClient:
    """A minimal fake client for `_spending_totals`, configurable so either
    the settings read or the report read raises a genuine 404
    `WealthfolioError`, exercising the capability-gated adapter's blocked
    behavior instead of crashing with a raw HTTP failure."""

    def __init__(
        self, *, block_settings=False, block_report=False, failure_status=404
    ):
        self.block_settings = block_settings
        self.block_report = block_report
        self.failure_status = failure_status

    def get(self, path):
        if path == "/spending/settings":
            if self.block_settings:
                from importers.monarch.wealthfolio_client import WealthfolioError

                raise WealthfolioError(
                    self.failure_status, path, "spending module unavailable"
                )
            return {"enabled": True, "accountIds": ["live-account"]}
        raise AssertionError(f"unexpected GET {path}")

    def post(self, path, payload):
        if path == "/spending/report":
            if self.block_report:
                from importers.monarch.wealthfolio_client import WealthfolioError

                raise WealthfolioError(
                    self.failure_status, path, "spending report unavailable"
                )
            return {"current": {"income": "0", "outflow": "12.34"}}
        raise AssertionError(f"unexpected POST {path}")


def test_spending_totals_returns_none_without_crashing_when_settings_unsupported():
    plan = {"spendingWindow": {"startDate": "2026-08-01", "endDate": "2026-08-31"}}
    client = _SpendingTotalsClient(block_settings=True)

    assert simplefin_apply._spending_totals(client, plan) is None


def test_spending_totals_returns_none_without_crashing_when_report_unsupported():
    plan = {"spendingWindow": {"startDate": "2026-08-01", "endDate": "2026-08-31"}}
    client = _SpendingTotalsClient(block_report=True)

    assert simplefin_apply._spending_totals(client, plan) is None


def test_spending_totals_returns_totals_when_supported():
    plan = {"spendingWindow": {"startDate": "2026-08-01", "endDate": "2026-08-31"}}
    client = _SpendingTotalsClient()

    totals = simplefin_apply._spending_totals(client, plan)

    assert totals == {"income": Decimal("0"), "spending": Decimal("12.34")}


def test_spending_totals_returns_none_when_no_spending_window():
    assert simplefin_apply._spending_totals(_SpendingTotalsClient(), {}) is None


@pytest.mark.parametrize("kwargs", [
    {"block_settings": True},
    {"block_report": True},
])
def test_spending_totals_propagates_operational_failures(kwargs):
    plan = {"spendingWindow": {"startDate": "2026-08-01", "endDate": "2026-08-31"}}
    client = _SpendingTotalsClient(**kwargs, failure_status=503)

    with pytest.raises(simplefin_apply.SpendingCapabilityBlocked, match="is error"):
        simplefin_apply._spending_totals(client, plan)


@pytest.mark.parametrize(
    "report",
    [
        {},
        {"spendingBreakdown": []},
        {"current": None},
        {"current": []},
        {"current": "12.34"},
    ],
)
def test_spending_totals_refuses_a_report_without_a_current_totals_object(report):
    """A malformed report must be a predictable refusal, not a raw KeyError."""

    class _Client(_SpendingTotalsClient):
        def post(self, path, payload):
            assert path == "/spending/report"
            return report

    plan = {"spendingWindow": {"startDate": "2026-08-01", "endDate": "2026-08-31"}}

    with pytest.raises(
        simplefin_apply.SpendingCapabilityBlocked, match="no 'current' totals"
    ) as blocked:
        simplefin_apply._spending_totals(_Client(), plan)

    assert blocked.value.status.status == "incompatible"
