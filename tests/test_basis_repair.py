from copy import deepcopy
from decimal import Decimal
from urllib.error import URLError

import pytest

from importers.maintenance.basis_repair import (
    BasisRepairError,
    activity_fingerprint,
    build_lot_replacement_plan,
    build_plan,
    materialize_updates,
    validate_lot_deletes,
    verify_evidence_files,
    verify_plan_integrity,
)
from importers.maintenance.basis_repair_cli import (
    apply_aggregate_plan,
    apply_lot_plan,
    password,
    restore_quotes,
    validate_quote_guards,
    verify_production_receipt,
)


def activity(kind, activity_id, *, symbol=None, quantity=None, price=None, amount=None):
    row = {
        "id": activity_id,
        "accountId": "account-1",
        "activityType": kind,
        "date": "2026-01-02T00:00:00+00:00",
        "assetId": f"asset-{symbol}" if symbol else "",
        "assetSymbol": symbol or "",
        "quantity": quantity,
        "unitPrice": price,
        "amount": amount,
        "idempotencyKey": f"buy:{symbol}" if symbol else "reconcile:one",
    }
    return row


def synthetic_rows():
    return [
        activity("BUY", "buy-a", symbol="AAA", quantity="3", price="20"),
        activity("BUY", "buy-b", symbol="BBB", quantity="2", price="30"),
        activity("DEPOSIT", "fund", amount="125"),
    ]


def make_plan(rows=None):
    return build_plan(
        account={"id": "account-1", "name": "Synthetic Brokerage"},
        activities=rows or synthetic_rows(),
        targets={
            "AAA": {"quantity": "3", "totalBasis": "45.01"},
            "BBB": {"quantity": "2", "totalBasis": "70.00"},
        },
        funding_key="reconcile:one",
        expected_cash=Decimal("5"),
        expected_total=Decimal("120"),
        evidence=[{"path": "extract.csv", "sha256": "f" * 64}],
    )


def test_repair_pairs_basis_change_with_equal_funding_change():
    plan = make_plan()
    assert plan["guards"]["oldBasis"] == "120"
    assert plan["guards"]["newBasis"] == "115.01"
    assert plan["guards"]["netCashEffect"] == "0.00"
    funding = next(op for op in plan["operations"] if op["kind"] == "FUNDING")
    assert funding["after"]["amount"] == "120.01"


def test_repeating_average_rounds_to_the_exact_aggregate_basis():
    plan = make_plan()
    buy = next(op for op in plan["operations"] if op.get("symbol") == "AAA")
    unit = Decimal(buy["after"]["unitPrice"])
    assert (unit * Decimal("3")).quantize(Decimal("0.01")) == Decimal("45.01")


def test_materialization_changes_only_price_or_funding_and_preserves_dates():
    rows = synthetic_rows()
    updates = materialize_updates(make_plan(rows), rows)
    buy = next(row for row in updates if row["id"] == "buy-a")
    funding = next(row for row in updates if row["id"] == "fund")
    assert buy["quantity"] == "3"
    assert buy["activityDate"] == "2026-01-02T00:00:00+00:00"
    assert buy["asset"]["id"] == "asset-AAA"
    assert buy["asset"]["symbol"] == "AAA"
    assert funding["activityDate"] == "2026-01-02T00:00:00+00:00"
    assert Decimal(buy["unitPrice"]) != Decimal("20")
    assert Decimal(funding["amount"]) == Decimal("120.01")


def test_changed_activity_fails_closed_before_updates_are_built():
    rows = synthetic_rows()
    plan = make_plan(rows)
    rows[0]["quantity"] = "4"
    with pytest.raises(BasisRepairError, match="activity changed"):
        materialize_updates(plan, rows)


def test_wrong_quantity_is_rejected_during_planning():
    with pytest.raises(BasisRepairError, match="quantity changed"):
        build_plan(
            account={"id": "account-1", "name": "Synthetic Brokerage"},
            activities=synthetic_rows(),
            targets={"AAA": {"quantity": "4", "totalBasis": "45.01"}},
            funding_key="reconcile:one",
            expected_cash=Decimal("5"),
            expected_total=Decimal("120"),
            evidence=[],
        )


def test_fingerprint_covers_price_quantity_and_amount():
    row = synthetic_rows()[0]
    original = activity_fingerprint(row)
    for field, value in [("unitPrice", "21"), ("quantity", "4"), ("amount", "1")]:
        changed = dict(row)
        changed[field] = value
        assert activity_fingerprint(changed) != original


def test_password_reads_generated_private_credential_file(tmp_path, monkeypatch):
    monkeypatch.delenv("WEALTHFOLIO_PASSWORD", raising=False)
    private = tmp_path / "wealthfolio"
    private.mkdir()
    (private / "ADMIN-PASSWORD.txt").write_text(
        "Wealthfolio login password, generated now.\n\n"
        "    synthetic-token-123\n\n"
        "Move this into a password manager and delete this file.\n",
        encoding="utf-8",
    )

    assert password(tmp_path) == "synthetic-token-123"


def make_lot_plan(rows=None, expectations=None, deleted_effect="0"):
    rows = rows or [
        activity(
            "BUY", "aggregate", symbol="FUND", quantity="5", price="21", amount="105"
        ),
        activity("DEPOSIT", "funding", amount="104.999"),
        {
            **activity("DEPOSIT", "rounding", amount="0.001"),
            "idempotencyKey": "rounding",
        },
    ]
    if expectations is None:
        expectations = []
        for row in rows:
            expected = {
                "id": row["id"],
                "activityType": row["activityType"],
                "amount": str(row.get("amount") or "0"),
            }
            if row["activityType"] == "BUY":
                expected.update(
                    {
                        "assetId": row["assetId"],
                        "assetSymbol": row["assetSymbol"],
                        "quantity": row["quantity"],
                        "unitPrice": row["unitPrice"],
                    }
                )
            expectations.append(expected)
    return build_lot_replacement_plan(
        account={"id": "account-1", "name": "Synthetic Retirement"},
        activities=rows,
        delete_expectations=expectations,
        expected_deleted_cash_effect=Decimal(deleted_effect),
        lots=[
            {"date": "2026-01-15", "quantity": "2", "amount": "40.01"},
            {"date": "2026-01-31", "quantity": "3", "amount": "62.99"},
        ],
        asset={
            "id": "asset-FUND",
            "symbol": "FUND",
            "name": "Synthetic Fund",
            "kind": "SECURITY",
            "quoteMode": "MANUAL",
            "quoteCcy": "USD",
            "instrumentType": "EQUITY",
        },
        quote_date="2026-02-01",
        market_value=Decimal("110"),
        expected_before_total=Decimal("105.001"),
        expected_shares=Decimal("5"),
        expected_basis=Decimal("103"),
        expected_cash=Decimal("0.001"),
        evidence=[{"path": "history.csv", "sha256": "a" * 64}],
    )


def test_lot_plan_uses_source_dates_and_day_before_funding():
    plan = make_lot_plan()
    assert len(plan["creates"]) == 4
    assert plan["creates"][0]["activityDate"] == "2026-01-14T00:00:00Z"
    assert plan["creates"][1]["activityDate"] == "2026-01-15T00:00:00Z"
    assert plan["creates"][2]["activityDate"] == "2026-01-30T00:00:00Z"
    assert plan["guards"]["basis"] == "103"


def test_each_lot_buy_has_exact_amount_and_derived_unit_price():
    plan = make_lot_plan()
    buys = [row for row in plan["creates"] if row["activityType"] == "BUY"]
    for row in buys:
        assert (
            Decimal(row["quantity"]) * Decimal(row["unitPrice"])
        ).quantize(Decimal("0.01")) == Decimal(row["amount"])


def test_lot_quote_uses_asset_uuid_and_exact_aggregate_nav():
    plan = make_lot_plan()
    current = plan["quotes"][-1]
    assert current["symbol"] == "asset-FUND"
    assert (
        Decimal(current["exactNav"]) * Decimal(plan["guards"]["shares"])
    ).quantize(Decimal("0.01")) == Decimal(plan["guards"]["marketValue"])


def test_lot_plan_anchors_manual_price_on_first_funding_day():
    plan = make_lot_plan()
    anchor = plan["quotes"][0]
    assert anchor["date"] == "2026-01-14"
    assert anchor["purpose"] == "first-funding-day-anchor-from-first-lot"
    assert (
        Decimal(anchor["exactNav"]) * Decimal("2")
    ).quantize(Decimal("0.01")) == Decimal("40.01")


def test_all_synthetic_delete_fingerprints_are_guarded():
    rows = [
        activity(
            "BUY", "aggregate", symbol="FUND", quantity="5", price="21", amount="105"
        ),
        activity("DEPOSIT", "funding", amount="104.999"),
        {
            **activity("DEPOSIT", "rounding", amount="0.001"),
            "idempotencyKey": "rounding",
        },
    ]
    plan = make_lot_plan(rows)
    rows[2]["amount"] = "1"
    with pytest.raises(BasisRepairError, match="no longer matches target"):
        validate_lot_deletes(plan, rows)


def test_plan_tampering_breaks_canonical_fingerprint():
    plan = make_plan()
    plan["guards"]["cash"] = "500"
    with pytest.raises(BasisRepairError, match="integrity"):
        verify_plan_integrity(plan)


def test_evidence_tampering_is_rechecked_at_apply(tmp_path):
    evidence = tmp_path / "extract.csv"
    evidence.write_bytes(b"original")
    plan = make_plan()
    from importers.maintenance.basis_repair import file_fingerprint

    plan["evidence"] = [
        {"path": "extract.csv", "sha256": file_fingerprint(b"original")}
    ]
    from importers.maintenance.basis_repair import seal_plan

    plan = seal_plan(plan)
    evidence.write_bytes(b"edited")
    with pytest.raises(BasisRepairError, match="evidence hash changed"):
        verify_evidence_files(plan, tmp_path)


def test_link_added_after_plan_fails_closed():
    rows = [
        activity(
            "BUY", "aggregate", symbol="FUND", quantity="5", price="21", amount="105"
        ),
        activity("DEPOSIT", "funding", amount="104.999"),
        {
            **activity("DEPOSIT", "rounding", amount="0.001"),
            "idempotencyKey": "rounding",
        },
    ]
    plan = make_lot_plan(rows)
    rows[0]["sourceGroupId"] = "linked-transfer"
    with pytest.raises(BasisRepairError, match="linked"):
        validate_lot_deletes(plan, rows)


class PartialClient:
    def __init__(self):
        self.calls = []
        self.backups = 0

    def get(self, path):
        return []

    def backup_database(self):
        self.backups += 1

    def save_activities(self, creates=None, updates=None, delete_ids=None):
        self.calls.append({"creates": creates or [], "deleteIds": delete_ids or []})
        if len(self.calls) == 1:
            return {
                "created": [{"id": "new-one"}],
                "deleted": ["aggregate"],
                "errors": [{"message": "partial"}],
            }
        return {
            "created": [{"id": "restored"}],
            "deleted": ["new-one"],
            "errors": [],
        }


def test_partial_bulk_mutation_restores_deletes_and_removes_creates():
    rows = [
        activity(
            "BUY", "aggregate", symbol="FUND", quantity="5", price="21", amount="105"
        ),
        activity("DEPOSIT", "funding", amount="104.999"),
        {
            **activity("DEPOSIT", "rounding", amount="0.001"),
            "idempotencyKey": "rounding",
        },
    ]
    client = PartialClient()
    with pytest.raises(BasisRepairError, match="rolled back"):
        apply_lot_plan(client, make_lot_plan(rows), rows)
    assert client.backups == 1
    assert len(client.calls[0]["creates"]) == 4
    assert len(client.calls[0]["deleteIds"]) == 3
    assert client.calls[1]["deleteIds"] == ["new-one"]
    assert len(client.calls[1]["creates"]) == 1


def test_lot_transport_error_after_commit_is_inspected_and_rolled_back():
    rows = [
        activity(
            "BUY", "aggregate", symbol="FUND", quantity="5", price="21", amount="105"
        ),
        activity("DEPOSIT", "funding", amount="104.999"),
        {
            **activity("DEPOSIT", "rounding", amount="0.001"),
            "idempotencyKey": "rounding",
        },
    ]
    plan = make_lot_plan(rows)

    class Client:
        def __init__(self):
            self.live = deepcopy(rows)
            self.calls = 0

        def get(self, path):
            return []

        def backup_database(self):
            return None

        def iter_activities(self):
            yield from deepcopy(self.live)

        def save_activities(self, creates=None, updates=None, delete_ids=None):
            self.calls += 1
            if self.calls == 1:
                self.live = [row for row in self.live if row["id"] not in delete_ids]
                for index, create in enumerate(creates):
                    asset = create.get("asset") or {}
                    self.live.append(
                        {
                            "id": f"new-{index}",
                            "accountId": create["accountId"],
                            "activityType": create["activityType"],
                            "date": create["activityDate"].replace("Z", "+00:00"),
                            "assetId": asset.get("id"),
                            "assetSymbol": asset.get("symbol"),
                            "quantity": create.get("quantity"),
                            "unitPrice": create.get("unitPrice"),
                            "amount": create.get("amount"),
                            "idempotencyKey": create["idempotencyKey"],
                            "sourceGroupId": None,
                            "sourceRecordId": None,
                        }
                    )
                raise URLError("response lost after commit")
            self.live = deepcopy(rows)
            return {
                "created": [{"id": row["id"]} for row in rows],
                "deleted": list(delete_ids),
                "errors": [],
            }

    client = Client()
    restored = []
    with pytest.raises(BasisRepairError, match="rolled back and verified"):
        apply_lot_plan(
            client,
            plan,
            rows,
            rollback_verify=lambda: restored.append(True),
        )
    assert client.calls == 2
    assert client.live == rows
    assert restored == [True]


def test_aggregate_transport_error_after_commit_is_inspected_and_rolled_back():
    rows = synthetic_rows()
    plan = make_plan(rows)

    class Client:
        def __init__(self):
            self.live = deepcopy(rows)
            self.calls = 0

        def backup_database(self):
            return None

        def iter_activities(self):
            yield from deepcopy(self.live)

        def save_activities(self, creates=None, updates=None, delete_ids=None):
            self.calls += 1
            if self.calls == 1:
                by_id = {row["id"]: row for row in self.live}
                for update in updates:
                    row = by_id[update["id"]]
                    if update["activityType"] == "BUY":
                        row["unitPrice"] = update["unitPrice"]
                    else:
                        row["amount"] = update["amount"]
                raise URLError("response lost after commit")
            self.live = deepcopy(rows)
            return {
                "updated": [{"id": update["id"]} for update in updates],
                "errors": [],
            }

    client = Client()
    restored = []
    with pytest.raises(BasisRepairError, match="rolled back and verified"):
        apply_aggregate_plan(
            client,
            plan,
            rows,
            post_verify=lambda: None,
            rollback_verify=lambda: restored.append(True),
        )
    assert client.calls == 2
    assert client.live == rows
    assert restored == [True]


def test_changed_quote_fails_before_backup_or_mutation():
    class ChangedQuoteClient:
        def get(self, path):
            return [
                {
                    "id": "quote-1",
                    "assetId": "asset-FUND",
                    "day": "2026-02-01",
                    "open": "99",
                    "high": "99",
                    "low": "99",
                    "close": "99",
                    "currency": "USD",
                    "source": "MANUAL",
                }
            ]

    with pytest.raises(BasisRepairError, match="quote changed"):
        validate_quote_guards(ChangedQuoteClient(), make_lot_plan())


def test_quote_failure_restores_activities_and_previous_quote_state():
    class QuoteFailureClient:
        def __init__(self):
            self.bulk_calls = []
            self.quote_calls = 0

        def get(self, path):
            return []

        def backup_database(self):
            return None

        def save_activities(self, creates=None, updates=None, delete_ids=None):
            self.bulk_calls.append((creates or [], delete_ids or []))
            if len(self.bulk_calls) == 1:
                return {
                    "created": [{"id": f"new-{i}"} for i in range(4)],
                    "deleted": ["aggregate", "funding", "rounding"],
                    "errors": [],
                }
            return {
                "created": [{"id": f"old-{i}"} for i in range(3)],
                "deleted": [f"new-{i}" for i in range(4)],
                "errors": [],
            }

        def post(self, path, payload):
            self.quote_calls += 1
            return {"errors": [{"message": "quote rejected"}]}

    rows = [
        activity(
            "BUY", "aggregate", symbol="FUND", quantity="5", price="21", amount="105"
        ),
        activity("DEPOSIT", "funding", amount="104.999"),
        {
            **activity("DEPOSIT", "rounding", amount="0.001"),
            "idempotencyKey": "rounding",
        },
    ]
    client = QuoteFailureClient()
    with pytest.raises(BasisRepairError, match="quote state were restored"):
        apply_lot_plan(client, make_lot_plan(rows), rows)
    assert len(client.bulk_calls[1][0]) == 3
    assert len(client.bulk_calls[1][1]) == 4


def test_production_rejects_receipt_for_another_plan(tmp_path):
    receipt = tmp_path / "receipt.json"
    receipt.write_text(
        '{"status":"success","planSha256":"other","baseUrl":"http://127.0.0.1:28088",'
        '"environmentId":"rehearsal"}'
    )
    with pytest.raises(BasisRepairError, match="different plan"):
        verify_production_receipt(
            receipt,
            plan_sha256=make_plan()["planSha256"],
            base_url="http://127.0.0.1:8088",
            environment_id="production",
        )


def test_deleted_and_replacement_cash_effects_must_match_exactly():
    rows = [
        activity(
            "BUY", "aggregate", symbol="FUND", quantity="5", price="21", amount="105"
        ),
        activity("DEPOSIT", "funding", amount="104.999"),
        {
            **activity("DEPOSIT", "rounding", amount="0.002"),
            "idempotencyKey": "rounding",
        },
    ]
    with pytest.raises(BasisRepairError, match="preserve exact deleted cash effect"):
        make_lot_plan(rows, deleted_effect="0.001")


def test_wrong_synthetic_asset_is_rejected_even_when_id_matches():
    rows = [
        activity(
            "BUY", "aggregate", symbol="FUND", quantity="5", price="21", amount="105"
        ),
        activity("DEPOSIT", "funding", amount="104.999"),
        {
            **activity("DEPOSIT", "rounding", amount="0.001"),
            "idempotencyKey": "rounding",
        },
    ]
    expectations = [
        {
            "id": "aggregate",
            "activityType": "BUY",
            "assetId": "asset-WRONG",
            "assetSymbol": "FUND",
            "quantity": "5",
            "unitPrice": "21",
            "amount": "105",
        },
        {"id": "funding", "activityType": "DEPOSIT", "amount": "104.999"},
        {"id": "rounding", "activityType": "DEPOSIT", "amount": "0.001"},
    ]
    with pytest.raises(BasisRepairError, match="assetId"):
        make_lot_plan(rows, expectations)


def test_postcondition_failure_rolls_back_lot_activities_and_quotes():
    class Client:
        def __init__(self):
            self.bulk = []

        def get(self, path):
            return []

        def post(self, path, payload):
            return {}

        def backup_database(self):
            return None

        def save_activities(self, creates=None, updates=None, delete_ids=None):
            self.bulk.append((creates or [], delete_ids or []))
            if len(self.bulk) == 1:
                return {
                    "created": [{"id": f"new-{i}"} for i in range(4)],
                    "deleted": ["aggregate", "funding", "rounding"],
                    "errors": [],
                }
            return {
                "created": [{"id": f"old-{i}"} for i in range(3)],
                "deleted": [f"new-{i}" for i in range(4)],
                "errors": [],
            }

    rows = [
        activity(
            "BUY", "aggregate", symbol="FUND", quantity="5", price="21", amount="105"
        ),
        activity("DEPOSIT", "funding", amount="104.999"),
        {
            **activity("DEPOSIT", "rounding", amount="0.001"),
            "idempotencyKey": "rounding",
        },
    ]
    rollback_checked = []
    with pytest.raises(BasisRepairError, match="activity and quote changes rolled back"):
        apply_lot_plan(
            Client(),
            make_lot_plan(rows),
            rows,
            post_verify=lambda: (_ for _ in ()).throw(
                BasisRepairError("wrong total")
            ),
            rollback_verify=lambda: rollback_checked.append(True),
        )
    assert rollback_checked == [True]


def test_aggregate_update_count_mismatch_is_rejected_and_rolled_back():
    class Client:
        def __init__(self):
            self.calls = 0

        def backup_database(self):
            return None

        def save_activities(self, creates=None, updates=None, delete_ids=None):
            self.calls += 1
            if self.calls == 1:
                return {"updated": [{"id": "buy-a"}], "errors": []}
            return {"updated": [{"id": "buy-a"}], "errors": []}

    with pytest.raises(BasisRepairError, match="count mismatch"):
        apply_aggregate_plan(
            Client(),
            make_plan(),
            synthetic_rows(),
            post_verify=lambda: None,
            rollback_verify=lambda: None,
        )


def test_aggregate_postcondition_failure_rolls_back_every_update():
    class Client:
        def __init__(self):
            self.calls = 0

        def backup_database(self):
            return None

        def save_activities(self, creates=None, updates=None, delete_ids=None):
            self.calls += 1
            return {
                "updated": [{"id": row["id"]} for row in updates],
                "errors": [],
            }

    client = Client()
    restored = []
    with pytest.raises(BasisRepairError, match="updates rolled back"):
        apply_aggregate_plan(
            client,
            make_plan(),
            synthetic_rows(),
            post_verify=lambda: (_ for _ in ()).throw(
                BasisRepairError("wrong basis")
            ),
            rollback_verify=lambda: restored.append(True),
        )
    assert client.calls == 2
    assert restored == [True]


@pytest.mark.parametrize("repair", ["lot", "aggregate"])
def test_network_failure_during_postcondition_also_rolls_back(repair):
    class Client:
        def __init__(self):
            self.calls = 0

        def get(self, path):
            return []

        def post(self, path, payload):
            return {}

        def backup_database(self):
            return None

        def save_activities(self, creates=None, updates=None, delete_ids=None):
            self.calls += 1
            if repair == "aggregate":
                return {
                    "updated": [{"id": row["id"]} for row in updates],
                    "errors": [],
                }
            if self.calls == 1:
                return {
                    "created": [{"id": f"new-{i}"} for i in range(4)],
                    "deleted": ["aggregate", "funding", "rounding"],
                    "errors": [],
                }
            return {
                "created": [{"id": f"old-{i}"} for i in range(3)],
                "deleted": [f"new-{i}" for i in range(4)],
                "errors": [],
            }

    rows = [
        activity(
            "BUY", "aggregate", symbol="FUND", quantity="5", price="21", amount="105"
        ),
        activity("DEPOSIT", "funding", amount="104.999"),
        {
            **activity("DEPOSIT", "rounding", amount="0.001"),
            "idempotencyKey": "rounding",
        },
    ]
    client = Client()
    restored = []
    with pytest.raises(BasisRepairError, match="rolled back"):
        if repair == "lot":
            apply_lot_plan(
                client,
                make_lot_plan(rows),
                rows,
                post_verify=lambda: (_ for _ in ()).throw(URLError("offline")),
                rollback_verify=lambda: restored.append(True),
            )
        else:
            apply_aggregate_plan(
                client,
                make_plan(),
                synthetic_rows(),
                post_verify=lambda: (_ for _ in ()).throw(URLError("offline")),
                rollback_verify=lambda: restored.append(True),
            )
    assert client.calls == 2
    assert restored == [True]


def test_quote_restore_rejects_response_errors():
    plan = make_lot_plan()

    class Client:
        def post(self, path, payload):
            return {"errors": [{"message": "no"}]}

    previous = {
        guard["date"]: guard["rollbackQuote"] for guard in plan["quoteGuards"]
    }
    plan["quoteGuards"][0]["rollbackQuote"] = {
        "_id": "old",
        "symbol": "asset-FUND",
        "date": plan["quoteGuards"][0]["date"],
        "open": "1",
        "high": "1",
        "low": "1",
        "close": "1",
        "volume": 0,
        "currency": "USD",
        "dataSource": "MANUAL",
    }
    with pytest.raises(BasisRepairError, match="quote rollback rejected"):
        restore_quotes(Client(), plan, previous)


def test_quote_restore_rereads_and_verifies_all_guarded_dates():
    plan = make_lot_plan()

    class Client:
        def get(self, path):
            return [
                {
                    "id": "unexpected",
                    "assetId": "asset-FUND",
                    "day": plan["quoteGuards"][0]["date"],
                    "open": "9",
                    "high": "9",
                    "low": "9",
                    "close": "9",
                    "currency": "USD",
                    "source": "MANUAL",
                }
            ]

        def _request(self, method, path):
            return None

    previous = {
        guard["date"]: guard["rollbackQuote"] for guard in plan["quoteGuards"]
    }
    with pytest.raises(BasisRepairError, match="could not be verified"):
        restore_quotes(Client(), plan, previous)
