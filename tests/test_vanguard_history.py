import json
from datetime import date
from decimal import Decimal

import pytest

from importers.extracts.vanguard_activity import (
    VanguardAccountIdentity,
    VanguardActivity,
    VanguardActivityReport,
)
from importers.extracts.vanguard_history import (
    AccountSpec,
    ApplyRefused,
    activity_cash,
    apply_resolutions,
    assign_idempotency_keys,
    build_plan,
    cash_total,
    classify_transactions,
    execute_plan,
    plan_fingerprint,
    share_totals,
    validate_apply_preconditions,
)


def row(kind, *, symbol="FAKE", shares="1", price="10", amount="-10"):
    return VanguardActivity(
        transaction_date=date(2024, 1, 2),
        settlement_date=date(2024, 1, 3),
        holding="Fictional Fund",
        symbol=symbol,
        transaction_type=kind,
        shares=None if shares is None else Decimal(shares),
        share_price=None if price is None else Decimal(price),
        cash_amount=Decimal(amount),
        fees=Decimal("0"),
    )


@pytest.mark.parametrize(
    ("source", "kwargs", "expected_status", "expected_types"),
    [
        ("Buy", {}, "ready", ["BUY"]),
        ("Buy (exchange)", {}, "ready", ["BUY"]),
        ("Sell", {"shares": "-1", "amount": "10"}, "ready", ["SELL"]),
        ("Sell (exchange)", {"shares": "-1", "amount": "10"}, "ready", ["SELL"]),
        ("Contribution", {"symbol": None, "shares": None, "price": None, "amount": "10"}, "ready", ["DEPOSIT"]),
        ("Distribution", {"symbol": None, "shares": None, "price": None, "amount": "-10"}, "ready", ["WITHDRAWAL"]),
        ("Rollover (incoming)", {"symbol": None, "shares": None, "price": None, "amount": "10"}, "ready", ["TRANSFER_IN"]),
        ("Transfer (incoming)", {"symbol": None, "shares": None, "price": None, "amount": "10"}, "ready", ["TRANSFER_IN"]),
        ("Transfer (outgoing)", {"symbol": None, "shares": None, "price": None, "amount": "-10"}, "ready", ["TRANSFER_OUT"]),
        ("Dividend", {"shares": None, "price": None, "amount": "10"}, "ready", ["DIVIDEND"]),
        ("Capital gain (LT)", {"shares": None, "price": None, "amount": "10"}, "ready", ["DIVIDEND"]),
        ("Capital gain (ST)", {"shares": None, "price": None, "amount": "10"}, "ready", ["DIVIDEND"]),
        ("Reinvestment", {}, "ready", ["BUY"]),
        ("Reinvestment (LT gain)", {}, "ready", ["BUY"]),
        ("Reinvestment (ST gain)", {}, "ready", ["BUY"]),
        ("Sweep in", {"symbol": "VMFXX", "shares": None, "price": None}, "omitted", []),
        ("Sweep out", {"symbol": "VMFXX", "shares": None, "price": None, "amount": "10"}, "omitted", []),
        ("Recharacterization (incoming)", {"price": None, "amount": "10"}, "blocked", []),
        ("Recharacterization (outgoing)", {"shares": "-1", "price": None}, "blocked", []),
        ("TRANSFER FROM 00000001", {"price": None, "amount": "10"}, "blocked", []),
        ("TRANSFER TO 00000002", {"shares": "-1", "price": None, "amount": "10"}, "blocked", []),
    ],
)
def test_every_observed_type_is_conservatively_classified(
    source, kwargs, expected_status, expected_types
):
    event = classify_transactions("00000000", "account-id", [row(source, **kwargs)])[0]

    assert event.status == expected_status
    assert [activity["activityType"] for activity in event.activities] == expected_types


def test_unpaired_cash_transfer_is_marked_external():
    event = classify_transactions(
        "00000000",
        "account-id",
        [
            row(
                "Transfer (incoming)",
                symbol=None,
                shares=None,
                price=None,
                amount="0.32",
            )
        ],
    )[0]
    assert event.activities[0]["subtype"] == "external_transfer"
    assert json.loads(event.activities[0]["metadata"])["flow"]["is_external"] is True


def test_dividend_reinvestment_pair_collapses_without_creating_cash():
    dividend = row("Dividend", shares=None, price=None, amount="10")
    reinvestment = row("Reinvestment")

    events = classify_transactions("00000000", "account-id", [dividend, reinvestment])

    assert len(events) == 1
    assert events[0].source_types == ["Dividend", "Reinvestment"]
    assert [a["activityType"] for a in events[0].activities] == ["DIVIDEND", "BUY"]
    assert events[0].activities[0]["asset"]["symbol"] == "FAKE"
    assert cash_total(events) == Decimal("0")
    assert share_totals(events) == {"FAKE": Decimal("1")}


def test_vmfxx_pair_retains_dividend_and_omits_internal_reinvestment():
    events = classify_transactions(
        "00000000",
        "account-id",
        [
            row("Dividend", symbol="VMFXX", shares=None, price=None, amount="3"),
            row("Reinvestment", symbol="VMFXX", shares=None, price=None, amount="-3"),
        ],
    )

    assert len(events) == 1
    assert events[0].status == "ready"
    assert [a["activityType"] for a in events[0].activities] == ["DIVIDEND"]
    assert cash_total(events) == Decimal("3")


def test_unique_cross_symbol_dividend_reinvestment_pair_is_balanced():
    dividend = row(
        "Dividend", symbol="OLD", shares=None, price=None, amount="5.94"
    )
    reinvestment = row(
        "Reinvestment", symbol="NEW", shares="0.011", price=None, amount="-5.94"
    )
    events = classify_transactions(
        "00000000", "account-id", [dividend, reinvestment]
    )
    assert len(events) == 1
    assert events[0].status == "ready"
    assert [a["activityType"] for a in events[0].activities] == ["DIVIDEND", "BUY"]
    assert events[0].activities[1]["unitPrice"] == pytest.approx(540.0)
    assert cash_total(events) == Decimal("0")


def test_ambiguous_cross_symbol_pairs_are_not_guessed():
    rows = [
        row("Dividend", symbol="OLD-A", shares=None, price=None, amount="5"),
        row("Dividend", symbol="OLD-B", shares=None, price=None, amount="5"),
        row("Reinvestment", symbol="NEW-A", shares="1", price=None, amount="-5"),
        row("Reinvestment", symbol="NEW-B", shares="1", price=None, amount="-5"),
    ]
    events = classify_transactions("00000000", "account-id", rows)
    assert len(events) == 4


def test_identical_legitimate_rows_receive_collision_ordinals():
    events = classify_transactions(
        "00000000", "account-id", [row("Buy"), row("Buy")]
    )
    assign_idempotency_keys(events)
    keys = [event.activities[0]["idempotencyKey"] for event in events]

    assert len(set(keys)) == 2
    assert keys[1].endswith(":repeat-2")


def _plan(live=(), *, linked=False):
    report = VanguardActivityReport(
        source="customActivityReport 00000000.xlsx",
        account=VanguardAccountIdentity("00000000", ("IRA",)),
        transactions=(row("Buy"),),
    )
    account = AccountSpec(
        account_number="00000000",
        wealthfolio_account_id="account-id",
        wealthfolio_name="Fictional IRA",
        expected_shares={"FAKE": Decimal("1")},
        expected_cash=Decimal("-10"),
    )
    rows = list(live)
    if linked:
        rows.append(
            {
                "id": "synthetic-id",
                "accountId": "account-id",
                "activityType": "BUY",
                "date": "2025-01-15T00:00:00Z",
                "idempotencyKey": "vanguard:00000000:FAKE:2025-01-15",
                "sourceGroupId": "linked-group",
            }
        )
    return build_plan(
        [report],
        {"00000000": account},
        rows,
        {"customActivityReport 00000000.xlsx": "fake-sha256"},
    )


def test_plan_fingerprint_detects_edits():
    plan = _plan()
    assert plan_fingerprint(plan) == plan["planFingerprint"]

    plan["createActivities"][0]["amount"] = 999
    assert plan_fingerprint(plan) != plan["planFingerprint"]


def test_stale_live_activity_refuses_apply_even_with_exact_plan_fingerprint():
    plan = _plan()
    plan["applyAllowed"] = True
    plan["planFingerprint"] = plan_fingerprint(plan)

    with pytest.raises(ApplyRefused, match="live activities changed"):
        validate_apply_preconditions(
            plan,
            supplied_fingerprint=plan["planFingerprint"],
            current_live_activities=[{"id": "new-live-row"}],
        )


def test_linked_synthetic_deletion_is_refused():
    plan = _plan(linked=True)

    assert any("sourceGroupId" in blocker for blocker in plan["blockers"])
    assert plan["deleteActivities"][0]["sourceGroupId"] == "linked-group"
    assert plan["protectedLinkedActivities"][0]["sourceGroupId"] == "linked-group"
    with pytest.raises(ApplyRefused, match="linked activities"):
        validate_apply_preconditions(
            plan,
            supplied_fingerprint=plan["planFingerprint"],
            current_live_activities=[
                {
                    "id": "synthetic-id",
                    "accountId": "account-id",
                    "activityType": "BUY",
                    "date": "2025-01-15T00:00:00Z",
                    "idempotencyKey": "vanguard:00000000:FAKE:2025-01-15",
                    "sourceGroupId": "linked-group",
                }
            ],
        )


def test_monarch_opening_balance_is_deleted_when_full_history_replaces_it():
    plan = _plan(
        live=[{
            "id": "opening",
            "accountId": "account-id",
            "activityType": "DEPOSIT",
            "date": "2025-12-26T00:00:00Z",
            "amount": 10,
            "idempotencyKey": "monarch:opening:account-id",
            "sourceGroupId": None,
        }]
    )
    assert [row["id"] for row in plan["deleteActivities"]] == ["opening"]


def test_preserved_linked_rollover_reuses_leg_and_reduces_residual_deposit():
    report = VanguardActivityReport(
        source="customActivityReport 00000000.xlsx",
        account=VanguardAccountIdentity("00000000", ("IRA",)),
        transactions=(
            row(
                "Rollover (incoming)",
                symbol=None,
                shares=None,
                price=None,
                amount="100",
            ),
        ),
    )
    account = AccountSpec(
        "00000000", "account-id", "Fictional IRA", {}, Decimal("100")
    )
    live = [
        {
            "id": "linked-in",
            "accountId": "account-id",
            "activityType": "TRANSFER_IN",
            "date": "2024-01-03T00:00:00Z",
            "amount": 60,
            "idempotencyKey": "vanguard:reconcile:00000000:2025-01-16",
            "sourceGroupId": "group",
        },
        {
            "id": "residual",
            "accountId": "account-id",
            "activityType": "DEPOSIT",
            "date": "2024-01-03T00:00:00Z",
            "amount": 80,
            "idempotencyKey": "rollover:unrecorded:source:2024-01-03",
            "sourceGroupId": None,
        },
    ]
    plan = build_plan([report], {"00000000": account}, live, {"x": "hash"})
    assert not plan["blockers"]
    assert plan["createActivities"] == []
    assert plan["updateActivities"][0]["id"] == "residual"
    assert plan["updateActivities"][0]["after"]["amount"] == "40"
    assert plan["protectedLinkedActivities"][0]["id"] == "linked-in"


def test_unmapped_closed_account_is_preserved_but_excluded():
    closed = VanguardActivityReport(
        source="customActivityReport 99999999.xlsx",
        account=VanguardAccountIdentity("99999999", ("IRA",)),
        transactions=(
            row("Contribution", symbol=None, shares=None, price=None, amount="10"),
        ),
    )

    plan = build_plan([closed], {}, [], {"closed.xlsx": "fake-sha256"})

    assert plan["mappedAccounts"] == []
    assert plan["createActivities"] == []
    assert plan["excludedAccounts"] == [
        {
            "accountNumber": "99999999",
            "source": "customActivityReport 99999999.xlsx",
            "transactionCount": 1,
            "reason": "unmapped account; preserved in source workbook and excluded",
        }
    ]


def test_in_kind_rows_count_toward_share_reconciliation_without_fake_basis():
    events = classify_transactions(
        "00000000",
        "account-id",
        [row("Transfer (incoming)", shares="5", price=None, amount="100")],
    )

    assert share_totals(events) == {"FAKE": Decimal("5")}
    assert events[0].status == "blocked"
    assert events[0].activities == []


def test_external_in_kind_resolution_is_an_asset_transfer_not_a_buy():
    events = classify_transactions(
        "00000000",
        "account-id",
        [row("Transfer (incoming)", symbol="FAKE", shares="5", price=None, amount="0")],
    )
    resolved, issues = apply_resolutions(
        events,
        "account-id",
        [{
            "account": "00000000",
            "date": "2024-01-03",
            "symbol": "FAKE",
            "sourceType": "Transfer (incoming)",
            "shareDelta": 5,
            "unitPrice": 12.50,
            "amount": 62.50,
            "activityType": "BUY",
            "confidence": "HIGH",
            "comment": "Synthetic external arrival",
        }],
    )
    assert not issues
    assert resolved[0].status == "ready"
    assert resolved[0].cash_delta == 0
    activity = resolved[0].activities[0]
    assert activity["activityType"] == "TRANSFER_IN"
    assert activity["quantity"] == 5
    assert activity["unitPrice"] == 12.5
    assert "amount" not in activity
    assert activity["subtype"] == "external_transfer"


def test_quantity_dividend_resolution_balances_dividend_and_buy():
    events = classify_transactions(
        "00000000",
        "account-id",
        [row("Dividend", symbol="FAKE", shares="2", price=None, amount="0")],
    )
    resolved, issues = apply_resolutions(
        events,
        "account-id",
        [{
            "account": "00000000",
            "date": "2024-01-03",
            "symbol": "FAKE",
            "sourceType": "Dividend",
            "shareDelta": 2,
            "unitPrice": 10,
            "amount": 20,
            "activityType": "DIVIDEND+BUY",
            "confidence": "HIGH",
        }],
    )
    assert not issues
    assert [a["activityType"] for a in resolved[0].activities] == ["DIVIDEND", "BUY"]
    assert resolved[0].cash_delta == 0


def test_low_confidence_resolution_remains_blocked():
    events = classify_transactions(
        "00000000", "account-id", [row("Buy", shares="5", price=None, amount="0")]
    )
    resolved, issues = apply_resolutions(
        events,
        "account-id",
        [{
            "account": "00000000",
            "date": "2024-01-03",
            "symbol": "FAKE",
            "sourceType": "Buy",
            "shareDelta": 5,
            "unitPrice": 12.5,
            "activityType": "BUY",
            "confidence": "MEDIUM",
        }],
    )
    assert issues
    assert resolved[0].status == "blocked"


def test_internal_recharacterization_is_not_marked_external():
    events = classify_transactions(
        "00000000",
        "account-id",
        [row("Recharacterization (incoming)", shares="5", price=None, amount="0")],
    )
    resolved, issues = apply_resolutions(
        events,
        "account-id",
        [{
            "account": "00000000",
            "date": "2024-01-03",
            "symbol": "FAKE",
            "sourceType": "Recharacterization (incoming)",
            "shareDelta": 5,
            "unitPrice": 12.5,
            "amount": 62.5,
            "activityType": "TRANSFER_IN",
            "pairId": "2024-01-03-synthetic-recharacterization",
            "confidence": "HIGH",
        }],
    )
    assert not issues
    activity = resolved[0].activities[0]
    assert "subtype" not in activity
    assert json.loads(activity["metadata"])["flow"]["is_external"] is False
    assert resolved[0].reason == "2024-01-03-synthetic-recharacterization"


class ApplyClient:
    def __init__(self, created=None, activity_rows=None):
        self.created = created or []
        self.activity_rows = list(activity_rows or [])
        self.saved = None
        self.posts = []
        self.backups = 0

    def backup_database(self):
        self.backups += 1

    def save_activities(self, creates=None, updates=None, delete_ids=None):
        self.saved = {
            "creates": creates or [],
            "updates": updates or [],
            "delete_ids": delete_ids or [],
        }
        created_rows = self.created or [
            {**activity, "id": f"created-{index}"}
            for index, activity in enumerate(creates or [])
        ]
        self.activity_rows.extend(created_rows)
        return {
            "created": created_rows,
            "updated": [{} for _ in (updates or [])],
            "deleted": [{} for _ in (delete_ids or [])],
            "errors": [],
        }

    def post(self, path, payload):
        self.posts.append((path, payload))
        return None

    def iter_activities(self):
        yield from self.activity_rows


def test_execute_applies_one_bulk_mutation_and_recalculates():
    current = {
        "id": "funding",
        "accountId": "account-id",
        "activityType": "DEPOSIT",
        "date": "2024-01-03T00:00:00Z",
        "amount": "80",
        "idempotencyKey": "old",
        "sourceGroupId": None,
    }
    plan = {
        "createActivities": [{"idempotencyKey": "new"}],
        "updateActivities": [{
            "id": "funding",
            "fingerprint": {
                "accountId": "account-id",
                "activityType": "DEPOSIT",
                "activityDate": "2024-01-03T00:00:00Z",
                "amount": "80",
                "idempotencyKey": "old",
                "sourceGroupId": None,
            },
            "after": {"activityType": "DEPOSIT", "amount": "40"},
        }],
        "deleteActivities": [{"id": "delete-me"}],
        "linkActivities": [],
    }
    client = ApplyClient(created=[{"id": "new-id", "idempotencyKey": "new"}])
    result = execute_plan(client, plan, [current])
    assert client.backups == 1
    assert client.saved["updates"][0]["amount"] == "40"
    assert client.saved["delete_ids"] == ["delete-me"]
    assert result == {
        "created": 1,
        "updated": 1,
        "deleted": 1,
        "linked": 0,
        "roundingCorrections": 0,
    }
    assert client.posts[-1] == ("/portfolio/recalculate", {})


def test_execute_links_created_internal_transfer_pair():
    created = [
        {"id": "out-id", "idempotencyKey": "out-key"},
        {"id": "in-id", "idempotencyKey": "in-key"},
    ]
    plan = {
        "createActivities": [
            {"idempotencyKey": "out-key"},
            {"idempotencyKey": "in-key"},
        ],
        "updateActivities": [],
        "deleteActivities": [],
        "linkActivities": [{
            "pairId": "pair",
            "members": [
                {"idempotencyKey": "out-key"},
                {"idempotencyKey": "in-key"},
            ],
        }],
    }
    client = ApplyClient(created=created)
    execute_plan(client, plan, [])
    assert (
        "/activities/link",
        {"activityAId": "out-id", "activityBId": "in-id"},
    ) in client.posts


def test_execute_reconciles_only_sub_two_dollar_cash_rounding():
    plan = {
        "createActivities": [],
        "updateActivities": [],
        "deleteActivities": [],
        "linkActivities": [],
        "reconciliation": {
            "account": {
                "wealthfolioAccountId": "account-id",
                "expectedCash": "1.00",
                "sourceAsOf": "2026-07-31",
            }
        },
    }
    client = ApplyClient(activity_rows=[{
        "accountId": "account-id",
        "activityType": "DEPOSIT",
        "amount": "0.50",
    }])
    result = execute_plan(client, plan, [])
    assert result["roundingCorrections"] == 1
    assert client.saved["creates"][0]["activityType"] == "TRANSFER_IN"
    assert client.saved["creates"][0]["amount"] == 0.5


def test_execute_refuses_large_cash_reconciliation():
    plan = {
        "createActivities": [],
        "updateActivities": [],
        "deleteActivities": [],
        "linkActivities": [],
        "reconciliation": {
            "account": {
                "wealthfolioAccountId": "account-id",
                "expectedCash": "3.00",
                "sourceAsOf": "2026-07-31",
            }
        },
    }
    with pytest.raises(ApplyRefused, match="restore backup"):
        execute_plan(ApplyClient(), plan, [])


def test_activity_cash_uses_buy_and_sell_as_cash_out_and_in():
    rows = [
        {"accountId": "a", "activityType": "DEPOSIT", "amount": "100"},
        {"accountId": "a", "activityType": "BUY", "amount": "75"},
        {"accountId": "a", "activityType": "SELL", "amount": "10"},
        {"accountId": "other", "activityType": "DEPOSIT", "amount": "999"},
    ]
    assert activity_cash(rows, "a") == Decimal("35")


def test_activity_cash_uses_quantity_times_price_for_trades():
    rows = [
        {"accountId": "a", "activityType": "DEPOSIT", "amount": "100"},
        {
            "accountId": "a",
            "activityType": "BUY",
            "amount": "75.00",
            "quantity": "3",
            "unitPrice": "25.01",
        },
    ]
    assert activity_cash(rows, "a") == Decimal("24.97")


def test_activity_cash_ignores_asset_transfer_market_value():
    rows = [
        {
            "accountId": "a",
            "activityType": "TRANSFER_IN",
            "amount": "999.99",
            "quantity": "100",
            "assetSymbol": "FAKE",
        },
        {
            "accountId": "a",
            "activityType": "TRANSFER_IN",
            "amount": "50",
            "quantity": None,
            "assetSymbol": None,
        },
    ]
    assert activity_cash(rows, "a") == Decimal("50")
