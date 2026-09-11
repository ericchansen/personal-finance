import json
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from importers.monarch.wealthfolio_client import WealthfolioClient, source_day_timestamp
from importers.simplefin.client import SimpleFinAccount, SimpleFinTransaction
from importers.simplefin.local_sync import (
    activity_amount, cash_type, plan_balance, plan_holdings, plan_transactions,
    read_accounts, save_changes, sync, transfer_pairs, update_payload, verify_balances,
)


ZONE = ZoneInfo("America/Chicago")
ACCOUNT = {"id": "cash-account", "accountType": "CASH", "currency": "USD"}
ENTRY = {"historyThrough": "2026-01-31"}


def transaction(id="txn-1", amount="-12.50", description="Synthetic Market", pending=False):
    return SimpleFinTransaction(id, date(2026, 2, 1), Decimal(amount), description, pending)


def source(*transactions):
    return SimpleFinAccount(
        "source-account", "Synthetic Checking", "Synthetic Bank", "USD",
        Decimal("100"), date(2026, 2, 1), transactions=list(transactions),
    )


def persisted(payload, id="activity-1"):
    return {**payload, "id": id, "date": payload["activityDate"]}


def plan(transactions, rows=(), entry=ENTRY):
    return plan_transactions(source(*transactions), entry, ACCOUNT, list(rows), ZONE)


def test_replay_is_noop_and_stable_ids_preserve_identical_purchases():
    transactions = [transaction(), transaction("txn-2")]
    creates, updates = plan(transactions)
    assert len(creates) == 2
    assert not updates
    rows = [persisted(row, f"a-{i}") for i, row in enumerate(creates)]
    assert plan(transactions, rows) == ([], [])
    assert creates[0]["idempotencyKey"] != creates[1]["idempotencyKey"]


def test_duplicate_payload_id_is_only_created_once():
    creates, _ = plan([transaction(), transaction()])
    assert len(creates) == 1


def test_pending_to_posted_and_overlapping_window():
    assert plan([transaction(pending=True)]) == ([], [])
    creates, _ = plan([transaction()])
    assert len(creates) == 1
    assert plan([transaction()], [persisted(creates[0])]) == ([], [])


def test_explicit_history_boundary_prevents_cross_source_duplicates():
    assert plan([transaction()], entry={"historyThrough": "2026-02-01"}) == ([], [])
    with pytest.raises(ValueError, match="historyThrough"):
        plan([transaction()], entry={})


def test_source_correction_updates_same_activity_without_losing_metadata():
    creates, _ = plan([transaction()])
    row = persisted(creates[0])
    row["comment"] = "My own note"
    meta = json.loads(row["metadata"])
    meta["custom"] = "keep me"
    row["metadata"] = json.dumps(meta)
    creates, updates = plan([transaction(amount="-13.50")], [row])
    assert not creates
    assert len(updates) == 1
    assert updates[0]["id"] == row["id"]
    assert updates[0]["amount"] == 13.5
    assert updates[0]["comment"] == "My own note"
    assert json.loads(updates[0]["metadata"])["custom"] == "keep me"
    assert plan([transaction(amount="-13.50")], [persisted(updates[0])]) == ([], [])


def test_conflicting_manual_edit_is_reported_not_overwritten():
    creates, _ = plan([transaction()])
    row = persisted(creates[0])
    row["amount"] = 7
    with pytest.raises(ValueError, match="manually edited"):
        plan([transaction(amount="-13.50")], [row])


def test_old_account_scoped_key_is_recognized():
    creates, _ = plan([transaction()])
    row = persisted(creates[0])
    row["metadata"] = None
    row["idempotencyKey"] = "simplefin:cash-account:txn-1"
    assert plan([transaction()], [row]) == ([], [])


def test_explicit_alias_preserves_previously_imported_activity():
    creates, _ = plan([transaction()])
    row = persisted(creates[0])
    row["metadata"] = None
    row["idempotencyKey"] = "other-importer:original"
    entry = {**ENTRY, "existingActivities": {"txn-1": row["id"]}}
    assert plan([transaction()], [row], entry) == ([], [])
    with pytest.raises(ValueError, match="missing activity"):
        plan([transaction()], [], entry)
    row["accountId"] = "different-account"
    with pytest.raises(ValueError, match="another account"):
        plan([transaction()], [row], entry)


@pytest.mark.parametrize(("kind", "amount", "description", "expected"), [
    ("CASH", "-15", "Zelle payment to Synthetic Friend", "WITHDRAWAL"),
    ("CASH", "2", "Interest Paid", "INTEREST"),
    ("CASH", "-40", "Internet transfer to Savings account 1234", "TRANSFER_OUT"),
    ("CASH", "40", "Internet transfer from Checking account 5678", "TRANSFER_IN"),
    ("CREDIT_CARD", "50", "AUTOMATIC PAYMENT - THANK", "TRANSFER_IN"),
    ("CREDIT_CARD", "5", "Synthetic Store Refund", "CREDIT"),
    ("CREDIT_CARD", "-5", "Synthetic Store", "WITHDRAWAL"),
])
def test_cash_semantics(kind, amount, description, expected):
    assert cash_type(transaction(amount=amount, description=description), kind) == expected


@pytest.mark.parametrize("zone", ["America/Chicago", "Pacific/Kiritimati", "Etc/GMT+12"])
@pytest.mark.parametrize("day", [date(2026, 3, 8), date(2026, 11, 1), date(2026, 2, 1)])
def test_source_day_is_correct_in_ui_and_utc(zone, day):
    stamp = datetime.fromisoformat(source_day_timestamp(day, ZoneInfo(zone)))
    assert stamp.date() == day
    assert stamp.astimezone(ZoneInfo(zone)).date() == day


def raw_payload():
    return {"accounts": [{
        "id": "source", "name": "Synthetic Bank", "currency": "USD",
        "balance": "100", "balance-date": 1770000000,
        "transactions": [{"id": "txn", "posted": 1770000000, "amount": "-10"}],
    }]}


@pytest.mark.parametrize("amount", ["NaN", "Infinity", "not-a-number", None])
def test_invalid_balance_is_not_silently_zero(amount):
    payload = raw_payload()
    payload["accounts"][0]["balance"] = amount
    with pytest.raises((ValueError, ArithmeticError)):
        read_accounts(payload)


def test_malformed_posted_transaction_is_not_silently_dropped():
    payload = raw_payload()
    payload["accounts"][0]["transactions"][0]["posted"] = 0
    with pytest.raises(ValueError, match="valid date"):
        read_accounts(payload)


def test_conflicting_provider_ids_are_reported():
    payload = raw_payload()
    payload["accounts"][0]["transactions"].append({"id": "txn", "posted": 1770000000, "amount": "-11"})
    with pytest.raises(ValueError, match="conflicting versions"):
        read_accounts(payload)


def test_local_client_is_explicit_and_loopback_only():
    assert not WealthfolioClient().local_sync
    assert WealthfolioClient(local_sync=True).local_sync
    with pytest.raises(ValueError):
        WealthfolioClient("https://example.com", local_sync=True)


def test_posted_income_and_spending_are_not_marked_as_external_reconciliation():
    creates, _ = plan([transaction(), transaction("salary", amount="100")])
    assert all(json.loads(row["metadata"])["flow"]["is_external"] is False for row in creates)


def test_balance_reconciliation_does_not_become_credit_card_spending():
    card = {**ACCOUNT, "accountType": "CREDIT_CARD"}
    observed = source()
    observed = SimpleFinAccount(
        observed.id, observed.name, observed.org, "USD",
        Decimal("-42"), observed.balance_date,
    )
    charge = {
        "id": "charge", "accountId": card["id"], "activityType": "WITHDRAWAL",
        "amount": "50", "date": "2026-02-01T18:00:00Z",
    }
    creates, updates = plan_balance(observed, card, [charge], ZONE)
    assert not updates
    assert creates[0]["activityType"] == "TRANSFER_IN"
    assert creates[0]["amount"] == 8
    assert creates[0]["subtype"] == "external_transfer"
    assert json.loads(creates[0]["metadata"])["flow"]["is_external"] is True
    assert plan_balance(observed, card, [charge, persisted(creates[0])], ZONE) == ([], [])
    with pytest.raises(ValueError, match="refusing to invent spending"):
        plan_balance(observed, card, [], ZONE)


def test_one_balance_adjustment_is_resized_after_overlapping_transactions():
    observed = source()
    card = {**ACCOUNT, "accountType": "CREDIT_CARD"}
    opening = {
        "id": "old", "accountId": ACCOUNT["id"], "activityType": "WITHDRAWAL",
        "date": "2026-01-31T18:00:00Z", "amount": "120", "idempotencyKey": "old-import",
    }
    creates, _ = plan_balance(observed, card, [opening], ZONE)
    adjustment = persisted(creates[0], "balance")
    new, _ = plan([transaction(amount="-10")])
    creates, updates = plan_balance(observed, card, [opening, adjustment, persisted(new[0])], ZONE)
    assert not creates
    assert updates[0]["id"] == "balance"
    assert updates[0]["amount"] == 230


def test_old_balance_does_not_roll_back_newer_reconciliation():
    adjustment = {
        "id": "balance", "accountId": ACCOUNT["id"], "activityType": "TRANSFER_IN",
        "date": "2026-03-01T18:00:00Z", "amount": "10",
        "idempotencyKey": "simplefin-balance:source-account",
    }
    assert plan_balance(source(), {**ACCOUNT, "accountType": "CREDIT_CARD"}, [adjustment], ZONE) == ([], [])


def test_signed_legacy_inflows_follow_native_ledger_semantics():
    assert activity_amount({"activityType": "TRANSFER_IN", "amount": "-25"}) == Decimal("-25")
    assert activity_amount({"activityType": "WITHDRAWAL", "amount": "-25"}) == Decimal("-25")


def test_source_holdings_are_not_discarded_by_parser():
    payload = raw_payload()
    payload["accounts"][0]["holdings"] = [{
        "id": "position-1", "symbol": "TEST", "description": "Synthetic Fund",
        "shares": "2", "market_value": "90", "currency": "USD",
    }]
    parsed, _ = read_accounts(payload)
    assert parsed[0].holdings == payload["accounts"][0]["holdings"]


def investment_source(id="source-investment", value="100"):
    return SimpleFinAccount(
        id, "Synthetic Brokerage", "Synthetic Bank", "USD", Decimal(value),
        date(2026, 2, 1), holdings=[{
            "id": "position-1", "symbol": "TEST", "description": "Synthetic Fund",
            "shares": "2", "market_value": "90", "currency": "USD",
            "purchase_price": "30",
        }],
    )


def test_native_snapshot_uses_real_quantities_prices_and_uninvested_cash():
    observed = investment_source()
    account = {**ACCOUNT, "name": "Synthetic Brokerage", "accountType": "SECURITIES"}
    snapshot, quotes = plan_holdings(observed, account, [])
    assert snapshot["cashBalances"] == {"USD": "10.00"}
    assert snapshot["holdings"][0]["quantity"] == "2"
    assert snapshot["holdings"][0]["averageCost"] == "30"
    assert quotes[0]["close"] == "45"
    assert snapshot["snapshotDate"] == "2026-02-01"
    assert quotes[0]["symbol"] == snapshot["holdings"][0]["assetId"]
    assert plan_holdings(observed, account, []) == (snapshot, quotes)


def test_same_fund_in_different_accounts_keeps_independent_observed_prices():
    first = plan_holdings(investment_source("one"), {**ACCOUNT, "name": "Account One"}, [])
    second = plan_holdings(investment_source("two"), {**ACCOUNT, "name": "Account Two"}, [])
    assert first[0]["holdings"][0]["assetId"] != second[0]["holdings"][0]["assetId"]
    assert first[0]["holdings"][0]["symbol"] != second[0]["holdings"][0]["symbol"]


def test_missing_position_list_is_reported_value_not_fabricated_cash_or_trade():
    snapshot, quotes = plan_holdings(source(), {**ACCOUNT, "name": "Synthetic Pension"}, [])
    assert snapshot["cashBalances"] == {"USD": "0.00"}
    assert "positions unavailable" in snapshot["holdings"][0]["name"]
    assert quotes[0]["close"] == "100"
    assert "averageCost" not in snapshot["holdings"][0]


@pytest.mark.parametrize(("holdings", "balance"), [(None, "100"), ([], "100"), (None, "0")])
def test_missing_position_data_does_not_replace_known_securities(holdings, balance):
    observed = SimpleFinAccount("s", "Broker", "Synthetic", "USD", Decimal(balance), date(2026, 2, 1), holdings=holdings)
    current = [{"holdingType": "security", "instrument": {"id": "known-asset"}, "quantity": "2"}]
    with pytest.raises(ValueError, match="existing holdings were not replaced"):
        plan_holdings(observed, {**ACCOUNT, "name": "Synthetic Broker"}, current)


def test_existing_balance_only_position_can_be_refreshed_without_claiming_real_holdings():
    account = {**ACCOUNT, "name": "Synthetic Pension"}
    snapshot, _ = plan_holdings(source(), account, [])
    current = [{
        "holdingType": "security", "instrument": {"id": snapshot["holdings"][0]["assetId"]},
        "quantity": "1",
    }]
    assert plan_holdings(source(), account, current)[0] == snapshot


def test_zero_balance_empty_positions_clears_snapshot_without_zero_price_quote():
    observed = SimpleFinAccount("s", "Empty", "Synthetic", "USD", Decimal(0), date(2026, 2, 1), holdings=[])
    current = [{"holdingType": "security", "instrument": {"id": "known-asset"}, "quantity": "2"}]
    snapshot, quotes = plan_holdings(observed, {**ACCOUNT, "name": "Empty"}, current)
    assert snapshot["holdings"] == quotes == []
    assert snapshot["cashBalances"] == {"USD": "0.00"}


def test_native_update_uses_asset_resolution_and_serialized_metadata():
    row = {
        "id": "old", "accountId": "investment", "assetId": "exact-asset",
        "activityType": "DIVIDEND", "amount": "4", "date": "2026-02-01T18:00:00Z",
        "metadata": {"custom": True},
    }
    payload = update_payload(row)
    assert payload["asset"] == {"id": "exact-asset"}
    assert "assetId" not in payload
    assert json.loads(payload["metadata"]) == {"custom": True}


class FakeClient:
    def __init__(self):
        self.account = {**ACCOUNT, "name": "Synthetic Checking", "isActive": True, "trackingMode": "TRANSACTIONS"}
        self.snapshot = None
        self.rows = [{
            "id": "opening", "accountId": ACCOUNT["id"], "activityType": "DEPOSIT",
            "date": "2026-01-31T18:00:00Z", "amount": "120", "currency": "USD",
            "idempotencyKey": "old-import",
        }]
        self.writes = []

    def list_accounts(self):
        return [self.account]

    def update_account(self, id, **fields):
        self.writes.append("account")
        self.account.update(fields)

    def iter_activities(self):
        return iter(self.rows)

    def display_timezone(self):
        return ZONE

    def get(self, path):
        if path == "/alternative-holdings":
            return []
        assert path == "/holdings?accountId=cash-account"
        if self.snapshot:
            return [{"marketValue": {"local": self.snapshot["cashBalances"]["USD"]}}]
        return [{"marketValue": {"local": str(sum(activity_amount(row) for row in self.rows))}}]

    def post(self, path, body):
        if path == "/snapshots":
            self.snapshot = body
            self.writes.append("snapshot")
            return {}
        assert path == "/portfolio/recalculate"
        self.writes.append(path)
        return None

    def save_activities(self, creates, updates):
        self.writes.append("activities")
        created = [persisted(row, f"new-{len(self.rows) + i}") for i, row in enumerate(creates)]
        updated = [persisted(row, row["id"]) for row in updates]
        self.rows = [row for row in self.rows if row["id"] not in {item["id"] for item in updated}]
        self.rows += created + updated
        return {"created": created, "updated": updated, "errors": []}


def test_full_cash_sync_replay_preserves_transactions_and_matches_bank(monkeypatch):
    import importers.simplefin.local_sync as module
    native_verify = module.verify_balances
    monkeypatch.setattr(module, "verify_balances", lambda *args: native_verify(*args, sleeper=lambda _: None))
    client = FakeClient()
    mapping = {"source-account": {**ENTRY, "action": "import", "wealthfolioAccountId": "cash-account"}}
    first = sync(client, [source(transaction())], mapping)
    assert not first["errors"]
    assert first["created"] == first["balances"] == 1
    assert Decimal(first["accounts"][0]["wealthfolioBalance"]) == Decimal("100")
    count = len(client.rows)
    second = sync(client, [source(transaction())], mapping)
    assert not second["errors"]
    assert second["created"] == second["updated"] == 0
    assert second["balances"] == 1
    assert len(client.rows) == count


def test_dry_run_never_changes_native_app():
    client = FakeClient()
    result = sync(client, [source(transaction())], {
        "source-account": {**ENTRY, "wealthfolioAccountId": "cash-account"},
    }, dry_run=True)
    assert result["created"] == 1
    assert client.writes == []


def test_bad_account_mapping_does_not_block_healthy_accounts(monkeypatch):
    import importers.simplefin.local_sync as module
    monkeypatch.setattr(module, "verify_balances", lambda *args: None)
    client = FakeClient()
    result = sync(client, [investment_source(), source(transaction())], {
        "source-account": {**ENTRY, "wealthfolioAccountId": "cash-account"},
    })
    assert result["created"] == 1
    assert len(result["errors"]) == 1
    assert "Unmapped" in result["errors"][0]


def test_native_bulk_errors_are_not_reported_as_success():
    client = FakeClient()
    client.save_activities = lambda **kwargs: {"created": [], "updated": [], "errors": [{"message": "invalid"}]}
    with pytest.raises(ValueError, match="rejected"):
        save_changes(client, [{"accountId": "a"}], [])


def test_unsettled_native_values_fail_instead_of_claiming_success():
    expected = [{"accountId": "cash-account", "accountType": "CASH", "name": "Synthetic", "sourceBalance": "10"}]
    with pytest.raises(ValueError, match="did not settle"):
        verify_balances(FakeClient(), expected, {}, attempts=2, sleeper=lambda _: None)


def test_transfer_linking_is_one_to_one_and_prefers_matching_posted_days():
    rows = []
    for id, account, kind, day in [
        ("a", "checking", "TRANSFER_OUT", "2026-02-01"),
        ("b", "savings", "TRANSFER_IN", "2026-02-01"),
        ("c", "checking", "TRANSFER_OUT", "2026-02-04"),
        ("d", "savings", "TRANSFER_IN", "2026-02-04"),
    ]:
        rows.append({
            "id": id, "accountId": account, "activityType": kind, "date": day + "T18:00:00Z",
            "amount": "25", "currency": "USD", "metadata": {"simplefin": {"description": "Transfer"}},
        })
    assert transfer_pairs(rows) == [("a", "b"), ("c", "d")]
    rows.append({**rows[1], "id": "ambiguous"})
    assert transfer_pairs(rows) == [("c", "d")]


def test_equal_transfer_amount_cannot_override_an_explicit_destination():
    left = {
        "id": "left", "accountId": "checking", "accountName": "Checking 1234",
        "activityType": "TRANSFER_OUT", "date": "2026-02-01T18:00:00Z",
        "amount": "25", "currency": "USD", "comment": "Transfer to Savings account XXXX5678",
        "metadata": {"simplefin": {"description": "Transfer"}},
    }
    right = {
        **left, "id": "right", "accountId": "other", "accountName": "Other Bank 9999",
        "activityType": "TRANSFER_IN", "comment": "Transfer received",
    }
    assert transfer_pairs([left, right]) == []
    right["accountName"] = "Savings 5678"
    right["date"] = "2026-02-02T18:00:00Z"
    assert transfer_pairs([left, right]) == [("left", "right")]
    left["comment"] = "Transfer"
    assert transfer_pairs([left, right]) == []


def test_liability_currency_mismatch_is_reported_without_changing_valuation(monkeypatch):
    import importers.simplefin.local_sync as module
    monkeypatch.setattr(module, "verify_balances", lambda *args: None)
    client = FakeClient()
    base_get = client.get
    client.get = lambda path: [{
        "id": "loan", "kind": "liability", "currency": "EUR",
        "marketValue": "50", "valuationDate": "2026-02-01",
    }] if path == "/alternative-holdings" else base_get(path)
    observed = SimpleFinAccount("s", "Loan", "Synthetic", "USD", Decimal("-50"), date(2026, 2, 1))
    result = sync(client, [observed], {"s": {"wealthfolioAlternativeAssetId": "loan"}})
    assert "currencies differ" in result["errors"][0]
    assert client.writes == ["/portfolio/recalculate"]


def test_equal_total_with_wrong_positions_is_not_a_successful_sync():
    class PositionClient:
        published = 0

        def get(self, path):
            if not self.published:
                return [{"holdingType": "cash", "marketValue": {"local": "100"}}]
            return [
                {"holdingType": "security", "instrument": {"id": "asset"}, "quantity": "2", "marketValue": {"local": "90"}},
                {"holdingType": "cash", "marketValue": {"local": "10"}},
            ]

        def post(self, path, body):
            if path == "/snapshots":
                self.published += 1
                return {}
            assert path == "/market-data/quotes/import"
            return [{"validationStatus": "valid"}]

    client = PositionClient()
    expected = [{"accountId": "investment", "accountType": "SECURITIES", "name": "Synthetic", "sourceBalance": "100"}]
    positions = {"investment": (
        {"holdings": [{"assetId": "asset", "quantity": "2"}]},
        [{"symbol": "asset", "close": "45"}],
    )}
    verify_balances(client, expected, positions, attempts=3, sleeper=lambda _: None)
    assert client.published == 1
