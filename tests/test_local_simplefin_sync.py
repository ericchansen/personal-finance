import json
from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from importers.simplefin.client import SimpleFinAccount, SimpleFinTransaction
from importers.simplefin.local_sync import (
    SnapshotObservationError, activity_amount, cash_type, plan_holdings, plan_transactions,
    read_accounts, save_changes, sync, sync_cash, update_payload, verify_balances,
    use_holdings,
)
from importers.simplefin.wealthfolio_client import WealthfolioClient, source_day_timestamp


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


def test_foreign_import_after_cutoff_requires_verified_ownership_or_an_explicit_alias():
    creates, _ = plan([transaction()])
    row = persisted(creates[0])
    row["idempotencyKey"] = "extract:synthetic-account:synthetic-bank-id"
    row["metadata"] = None
    with pytest.raises(ValueError, match="overlaps the SimpleFIN-owned interval"):
        plan([transaction()], [row])
    assert plan([transaction()], [row], entry={"historyThrough": "2026-02-01"}) == ([], [])
    aliases = {**ENTRY, "existingActivities": {"txn-1": row["id"]}}
    assert plan([transaction()], [row], entry=aliases) == ([], [])
    row["status"] = "VOID"
    assert len(plan([transaction()], [row])[0]) == 1


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
    assert updates[0]["amount"] == "13.50"
    assert updates[0]["comment"] == "My own note"
    assert json.loads(updates[0]["metadata"])["custom"] == "keep me"
    assert plan([transaction(amount="-13.50")], [persisted(updates[0])]) == ([], [])


def test_conflicting_manual_edit_is_reported_not_overwritten():
    creates, _ = plan([transaction()])
    row = persisted(creates[0])
    row["amount"] = 7
    with pytest.raises(ValueError, match="manually edited"):
        plan([transaction(amount="-13.50")], [row])


def test_unchanged_source_cannot_hide_a_manual_cash_amount_edit():
    creates, _ = plan([transaction()])
    row = persisted(creates[0])
    row["amount"] = 7
    with pytest.raises(ValueError, match="manually edited"):
        plan([transaction()], [row])
    row["amount"], row["fee"], row["tax"] = "12.50", "1", "0.50"
    assert plan([transaction()], [row]) == ([], [])


def test_description_correction_preserves_native_fee_tax_attribution_and_final_cash():
    creates, _ = plan([transaction()])
    row = persisted(creates[0])
    row["fee"], row["tax"] = "1", "0.50"
    _, updates = plan([transaction(description="Synthetic corrected merchant")], [row])
    assert updates[0]["amount"] == "12.50"
    assert updates[0]["fee"] == "1"
    assert updates[0]["tax"] == "0.50"


def test_native_fee_split_cannot_hide_a_changed_final_cash_amount():
    creates, _ = plan([transaction(amount="-10")])
    row = persisted(creates[0])
    row["amount"], row["fee"] = "8", "2"
    with pytest.raises(ValueError, match="manually edited"):
        plan([transaction(amount="-10", description="Synthetic memo correction")], [row])


def test_repeated_source_key_or_conflicting_alias_is_not_silently_selected():
    creates, _ = plan([transaction()])
    left, right = persisted(creates[0], "synthetic-left"), persisted(creates[0], "synthetic-right")
    with pytest.raises(ValueError, match="multiple activities"):
        plan([transaction()], [left, right])
    right["idempotencyKey"] = "synthetic-legacy"
    with pytest.raises(ValueError, match="multiple activities"):
        plan([transaction()], [left, right], entry={
            **ENTRY, "existingActivities": {"txn-1": right["id"]},
        })


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
    ("CASH", "-2", "Interest Paid", "WITHDRAWAL"),
    ("CASH", "-40", "Internet transfer to Savings account 1234", "TRANSFER_OUT"),
    ("CASH", "40", "Internet transfer from Checking account 5678", "TRANSFER_IN"),
    ("CASH", "50", "VENMO CASHOUT", "TRANSFER_IN"),
    ("CASH", "50", "Venmo   Cashout", "TRANSFER_IN"),
    ("CASH", "-5", "VENMO PAYMENT", "WITHDRAWAL"),
    ("CASH", "-1", "VENMO CASHOUT FEE", "WITHDRAWAL"),
    ("CASH", "50", "ACH Electronic Credit", "DEPOSIT"),
    ("CREDIT_CARD", "50", "AUTOMATIC PAYMENT - THANK", "TRANSFER_IN"),
    ("CREDIT_CARD", "50", "ONLINE PAYMENT, THANK YOU", "TRANSFER_IN"),
    ("CREDIT_CARD", "50", "PAYMENT - THANK YOU", "TRANSFER_IN"),
    ("CREDIT_CARD", "50", "PAYMENT  RECEIVED", "TRANSFER_IN"),
    ("CREDIT_CARD", "5", "Synthetic Store Refund", "CREDIT"),
    ("CREDIT_CARD", "-5", "Synthetic Store", "WITHDRAWAL"),
])
def test_cash_semantics(kind, amount, description, expected):
    assert cash_type(transaction(amount=amount, description=description), kind) == expected


@pytest.mark.parametrize(("account_type", "existing_type", "description"), [
    ("CREDIT_CARD", "CREDIT", "ONLINE PAYMENT, THANK YOU"),
    ("CASH", "DEPOSIT", "VENMO CASHOUT"),
])
@pytest.mark.parametrize("linked", [False, True])
def test_existing_transfer_receipt_is_reclassified_without_changing_cash_or_identity(
    account_type, existing_type, description, linked,
):
    account = {**ACCOUNT, "accountType": account_type}
    payment = transaction(amount="50", description=description)
    row = {
        "id": "synthetic-payment", "accountId": account["id"], "activityType": existing_type,
        "status": "POSTED", "amount": "50", "currency": "USD",
        "date": source_day_timestamp(payment.posted, ZONE),
        "idempotencyKey": "simplefin:source-account:txn-1",
        "sourceGroupId": "synthetic-pair" if linked else None,
        "metadata": json.dumps({
            "simplefin": {"amount": "50", "date": payment.posted.isoformat(),
                          "description": payment.description},
            "flow": {"is_external": False},
        }),
    }
    creates, updates = plan_transactions(source(payment), ENTRY, account, [row], ZONE)
    assert creates == []
    assert len(updates) == 1
    assert updates[0]["id"] == row["id"]
    assert updates[0]["idempotencyKey"] == row["idempotencyKey"]
    assert updates[0]["activityType"] == "TRANSFER_IN"
    assert activity_amount(updates[0]) == Decimal("50")
    assert updates[0]["activityDate"] == row["date"]
    assert plan_transactions(source(payment), ENTRY, account, [persisted(updates[0], row["id"])], ZONE) == ([], [])


def test_unchanged_nontransfer_source_preserves_a_native_fee_refinement():
    charge = transaction(amount="-5", description="Synthetic service charge")
    creates, _ = plan([charge])
    row = persisted(creates[0])
    row["activityType"] = "FEE"
    assert plan([charge], [row]) == ([], [])


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


@pytest.mark.parametrize("currency", [None, "", " ", 1])
def test_missing_source_currency_is_not_invented_as_usd(currency):
    payload = raw_payload()
    payload["accounts"][0]["currency"] = currency
    accounts, errors = read_accounts(payload)
    assert accounts[0].currency == ""
    assert not accounts[0].is_currency
    assert any("units are unknown" in error for error in errors)


@pytest.mark.parametrize("pending", ["false", 1, None])
def test_malformed_pending_flag_does_not_silently_drop_a_transaction(pending):
    payload = raw_payload()
    payload["accounts"][0]["transactions"][0]["pending"] = pending
    with pytest.raises(ValueError, match="pending flag must be boolean"):
        read_accounts(payload)


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


@pytest.mark.parametrize("url", ["http://127.0.0.1:8088", "http://localhost:8088", "http://[::1]:8088"])
def test_client_accepts_loopback(url):
    WealthfolioClient(url)


@pytest.mark.parametrize("url", ["https://example.com", "http://192.168.1.10:8088"])
def test_client_refuses_non_loopback(url):
    with pytest.raises(ValueError):
        WealthfolioClient(url)


def test_posted_income_and_spending_are_not_marked_as_external_reconciliation():
    creates, _ = plan([transaction(), transaction("salary", amount="100")])
    assert all(json.loads(row["metadata"])["flow"]["is_external"] is False for row in creates)


def test_signed_legacy_inflows_follow_native_ledger_semantics():
    assert activity_amount({"activityType": "TRANSFER_IN", "amount": "-25"}) == Decimal("-25")
    assert activity_amount({"activityType": "WITHDRAWAL", "amount": "-25"}) == Decimal("-25")


@pytest.mark.parametrize("has_source_metadata", [False, True])
def test_existing_card_interest_replays_with_its_native_outflow_sign(has_source_metadata):
    card = {**ACCOUNT, "accountType": "CREDIT_CARD"}
    txn = transaction(description="Synthetic interest charge")
    creates, _ = plan_transactions(source(txn), ENTRY, card, [], ZONE)
    row = persisted(creates[0])
    row["activityType"] = "INTEREST"
    if not has_source_metadata:
        row["metadata"] = None
    assert activity_amount(row) == Decimal("12.50")
    assert activity_amount(row, card["accountType"]) == Decimal("-12.50")
    assert plan_transactions(source(txn), ENTRY, card, [row], ZONE) == ([], [])


def test_card_balance_projection_counts_interest_after_the_observation_as_debt():
    card = {**ACCOUNT, "accountType": "CREDIT_CARD"}
    creates, _ = plan_transactions(source(transaction()), ENTRY, card, [], ZONE)
    row = {**persisted(creates[0]), "activityType": "INTEREST"}
    observed = replace(source(), balance=Decimal("-100"), balance_date=date(2026, 1, 31))
    result = {"created": 0, "updated": 0, "errors": []}
    assert sync_cash(None, observed, ENTRY, card, [row], ZONE, result, {}, True) == Decimal("-112.50")
    assert result == {"created": 0, "updated": 0, "errors": []}


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
        }, {
            "id": "reported-cash", "symbol": "", "description": "Cash",
            "shares": "0", "market_value": "10", "currency": "USD",
        }],
    )


def test_native_snapshot_uses_real_quantities_prices_and_explicitly_reported_cash():
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


@pytest.mark.parametrize("balance", ["100", "80"])
def test_incomplete_positions_do_not_become_invented_positive_or_negative_cash(balance):
    observed = investment_source(value=balance)
    observed.holdings.pop()
    with pytest.raises(ValueError, match="unexplained balance is not reported cash"):
        plan_holdings(observed, {**ACCOUNT, "name": "Synthetic Brokerage"}, [])


def test_explicit_cash_position_must_use_the_accounts_currency():
    observed = investment_source()
    observed.holdings[1]["currency"] = "EUR"
    with pytest.raises(ValueError, match="currency differs"):
        plan_holdings(observed, {**ACCOUNT, "name": "Synthetic Brokerage"}, [])


def test_same_fund_in_different_accounts_keeps_independent_observed_prices():
    first = plan_holdings(investment_source("one"), {**ACCOUNT, "name": "Account One"}, [])
    second = plan_holdings(investment_source("two"), {**ACCOUNT, "name": "Account Two"}, [])
    assert first[0]["holdings"][0]["assetId"] != second[0]["holdings"][0]["assetId"]
    assert first[0]["holdings"][0]["symbol"] != second[0]["holdings"][0]["symbol"]


def test_missing_position_list_is_an_error_not_a_fabricated_security():
    with pytest.raises(ValueError, match="source positions are unavailable"):
        plan_holdings(source(), {**ACCOUNT, "name": "Synthetic Pension"}, [])


@pytest.mark.parametrize(("holdings", "balance"), [(None, "100"), ([], "100"), (None, "0")])
def test_missing_position_data_does_not_replace_known_securities(holdings, balance):
    observed = SimpleFinAccount("s", "Broker", "Synthetic", "USD", Decimal(balance), date(2026, 2, 1), holdings=holdings)
    current = [{"holdingType": "security", "instrument": {"id": "known-asset"}, "quantity": "2"}]
    with pytest.raises(ValueError, match="existing holdings were not replaced"):
        plan_holdings(observed, {**ACCOUNT, "name": "Synthetic Broker"}, current)


def test_empty_positions_and_nonzero_total_do_not_prove_investment_cash():
    from dataclasses import replace
    with pytest.raises(ValueError, match="source positions are unavailable"):
        plan_holdings(replace(source(), holdings=[]), {**ACCOUNT, "name": "Synthetic Broker"}, [])


def test_transaction_amounts_preserve_exact_decimal_values_on_create_and_update():
    synthetic_amount = "-9007199254740991.01"
    corrected = Decimal(synthetic_amount) - Decimal("0.01")
    creates, _ = plan([transaction(amount=synthetic_amount)])
    assert creates[0]["amount"] == synthetic_amount.removeprefix("-")
    _, updates = plan([transaction(amount=str(corrected))], [persisted(creates[0])])
    assert updates[0]["amount"] == str(abs(corrected))


def test_cost_basis_is_not_guessed_from_changed_quantity_or_matching_names():
    observed = investment_source()
    del observed.holdings[0]["purchase_price"]
    account = {**ACCOUNT, "name": "Synthetic Broker"}
    snapshot, _ = plan_holdings(observed, account, [])
    id = snapshot["holdings"][0]["assetId"]
    current = [{
        "holdingType": "security", "instrument": {"id": id, "symbol": "TEST", "name": "Synthetic Fund"},
        "quantity": "2", "costBasis": {"local": "60"},
    }]
    assert plan_holdings(observed, account, current)[0]["holdings"][0]["averageCost"] == "30"
    observed.holdings[0]["shares"] = "3"
    with pytest.raises(ValueError, match="source quantity changed without reported cost basis"):
        plan_holdings(observed, account, current)
    observed.holdings[0]["shares"] = "2"
    current[0]["instrument"]["id"] = "synthetic-unrelated-asset"
    assert "averageCost" not in plan_holdings(observed, account, current)[0]["holdings"][0]


def test_reported_total_basis_is_used_instead_of_a_zero_or_rounded_purchase_price():
    observed = investment_source()
    observed.holdings[0].update(cost_basis="75", purchase_price="0", shares="3")
    snapshot, _ = plan_holdings(observed, {**ACCOUNT, "name": "Synthetic Broker"}, [])
    assert snapshot["holdings"][0]["averageCost"] == "25"


def test_missing_or_repeated_position_identity_fails_before_any_snapshot_is_published():
    observed = investment_source()
    observed.holdings.append(dict(observed.holdings[0]))
    with pytest.raises(ValueError, match="position ID is missing or repeated"):
        plan_holdings(observed, {**ACCOUNT, "name": "Synthetic Broker"}, [])
    observed.holdings.pop()
    del observed.holdings[0]["id"]
    with pytest.raises(ValueError, match="position ID is missing or repeated"):
        plan_holdings(observed, {**ACCOUNT, "name": "Synthetic Broker"}, [])


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
        self.account = {**ACCOUNT, "name": "Synthetic Checking", "isActive": True, "trackingMode": "HOLDINGS"}
        self.snapshot = None
        self.snapshot_source = "MANUAL_ENTRY"
        self.rows = [{
            "id": "opening", "accountId": ACCOUNT["id"], "activityType": "DEPOSIT",
            "date": "2026-01-31T18:00:00Z", "amount": "120", "currency": "USD",
            "status": "POSTED",
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
        if path.startswith("/snapshots?accountId=cash-account"):
            return ([{"snapshotDate": self.snapshot["snapshotDate"], "source": self.snapshot_source,
                      "cashTotalAccountCurrency": self.snapshot["cashBalances"]["USD"]}]
                    if self.snapshot else [])
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


@pytest.mark.parametrize(("symbol", "description", "currency"), [
    ("TEST", "Synthetic Fund", "USD"),
    ("", "Synthetic Bond", "USD"),
    ("TEST", "Cash", "USD"),
    ("", "Cash", "EUR"),
])
def test_cash_mapping_rejects_non_cash_positions_before_writing_transactions(symbol, description, currency):
    client = FakeClient()
    observed = replace(source(transaction()), holdings=[{
        "id": "synthetic-position", "symbol": symbol, "description": description,
        "currency": currency, "shares": "1", "market_value": "100",
    }])
    with pytest.raises(ValueError, match="cash-only source"):
        sync_cash(client, observed, ENTRY, client.account, client.rows, ZONE, {}, {}, False)
    assert client.writes == []


@pytest.mark.parametrize("holdings", [None, [], [{
    "id": "synthetic-cash", "symbol": "", "description": " Cash ",
    "currency": "USD", "shares": "0", "market_value": "100",
}]])
def test_verified_cash_only_account_uses_reported_balance_without_invented_activity(holdings):
    client = FakeClient()
    before = list(client.rows)
    observed = replace(source(), holdings=holdings)
    result = sync(client, [observed], {
        observed.id: {**ENTRY, "wealthfolioAccountId": client.account["id"]},
    })
    assert result["errors"] == []
    assert result["created"] == result["updated"] == result["positions"] == 0
    assert result["balances"] == 1
    assert client.snapshot["cashBalances"] == {"USD": "100"}
    assert client.rows == before


def test_partial_investment_response_retains_existing_native_state():
    client = FakeClient()
    client.account["accountType"] = "SECURITIES"
    client.account["trackingMode"] = "TRANSACTIONS"
    observed = investment_source()
    observed.holdings.pop()
    before = list(client.rows)
    result = sync(client, [observed], {
        observed.id: {"wealthfolioAccountId": client.account["id"]},
    })
    assert any("unexplained balance is not reported cash" in error for error in result["errors"])
    assert result["positions"] == result["balances"] == 0
    assert client.rows == before
    assert client.snapshot is None
    assert client.account["trackingMode"] == "TRANSACTIONS"
    assert client.writes == ["/portfolio/recalculate"]


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


def test_read_only_check_reuses_saved_source_without_fetching_or_writing_to_the_app(tmp_path, monkeypatch):
    import importers.simplefin.local_sync as module
    folder = tmp_path / "simplefin"
    folder.mkdir()
    payload = raw_payload()
    payload["accounts"][0].update(id="source-account", transactions=[])
    payload["accounts"][0]["balance-date"] = int(datetime(2026, 2, 2, 12, tzinfo=ZoneInfo("UTC")).timestamp())
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text(json.dumps(payload))
    (folder / "account-map.json").write_text(json.dumps({"version": 1, "accounts": {
        "source-account": {**ENTRY, "wealthfolioAccountId": ACCOUNT["id"]},
    }}))
    (folder / "local-last-sync.json").write_text(json.dumps({"snapshotPath": snapshot.name}))
    client = FakeClient()
    client.snapshot = {"snapshotDate": "2026-02-02", "cashBalances": {"USD": "100"}}
    client.account["trackingMode"] = "HOLDINGS"
    client.login = lambda _: None
    monkeypatch.setattr(module, "WealthfolioClient", lambda _: client)
    monkeypatch.setattr(module, "read_password", lambda: "synthetic")
    monkeypatch.setattr(module, "fetch_snapshot", lambda *args, **kwargs: pytest.fail("check must not fetch"))
    assert module.main(["--data-dir", str(tmp_path), "--check"]) == 0
    assert client.writes == []
    preview = json.loads((folder / "local-preview.json").read_text())
    assert preview["created"] == preview["updated"] == 0


def test_source_description_correction_does_not_destroy_a_native_transfer_link():
    initial = transaction(description="Internet transfer to Savings account XXXX1234")
    creates, _ = plan([initial])
    row = persisted(creates[0])
    row["sourceGroupId"] = "synthetic-native-link"
    meta = json.loads(row["metadata"])
    meta["flow"]["is_external"] = False
    row["metadata"] = json.dumps(meta)
    counterpart = {
        "id": "synthetic-counterpart", "accountId": "synthetic-savings",
        "activityType": "TRANSFER_IN", "status": "POSTED", "amount": row["amount"],
        "currency": "USD", "date": "2026-01-31T18:00:00Z",
        "comment": "Independent receiving-bank description",
        "sourceGroupId": row["sourceGroupId"],
    }
    client = FakeClient()
    client.rows = [row, counterpart]
    corrected = transaction(description="Internet transfer to Savings account XXXX1234 corrected memo")
    result = sync(client, [source(corrected)], {
        "source-account": {**ENTRY, "wealthfolioAccountId": ACCOUNT["id"]},
    })
    assert not result["errors"]
    assert result["updated"] == 1
    actual = {item["id"]: item for item in client.rows}
    assert actual[row["id"]]["sourceGroupId"] == "synthetic-native-link"
    assert actual[counterpart["id"]]["date"] == counterpart["date"]
    assert actual[counterpart["id"]]["comment"] == counterpart["comment"]


@pytest.mark.parametrize("snapshot_source", ["MANUAL_ENTRY", "CSV_IMPORT"])
def test_newer_balance_does_not_suppress_real_transaction_import(snapshot_source):
    client = FakeClient()
    client.snapshot_source = snapshot_source
    client.snapshot = {
        "accountId": ACCOUNT["id"], "snapshotDate": "2026-03-01",
        "cashBalances": {"USD": "200"},
    }
    result = sync(client, [source(transaction())], {
        "source-account": {**ENTRY, "wealthfolioAccountId": ACCOUNT["id"]},
    })
    assert result["created"] == 1
    assert result["balances"] == 0
    assert not result["errors"]
    assert client.snapshot["snapshotDate"] == "2026-03-01"
    assert client.writes == ["activities", "/portfolio/recalculate"]


def test_unretained_source_snapshot_restores_previous_tracking_mode():
    client = FakeClient()
    client.account["trackingMode"] = "TRANSACTIONS"
    client.snapshot = {
        "accountId": ACCOUNT["id"], "snapshotDate": "2026-02-01",
        "holdings": [], "cashBalances": {"USD": "100"},
    }
    client.snapshot_source = "CALCULATED"
    before = dict(client.account)
    with pytest.raises(SnapshotObservationError, match="did not retain"):
        use_holdings(client, client.account, client.snapshot, [])
    assert client.account == before
    assert client.account["trackingMode"] == "TRANSACTIONS"


def test_transaction_cash_tracking_preserves_native_ledger_without_balance_overrides():
    client = FakeClient()
    client.account["trackingMode"] = "TRANSACTIONS"
    observed = replace(source(transaction()), balance=Decimal("107.50"))
    result = sync(client, [observed], {
        observed.id: {**ENTRY, "wealthfolioAccountId": ACCOUNT["id"]},
    })
    assert not result["errors"]
    assert result["created"] == 1 and result["balances"] == 0
    assert client.account["trackingMode"] == "TRANSACTIONS" and client.snapshot is None
    assert "account" not in client.writes and "snapshot" not in client.writes
    assert result["accounts"][0]["trackingMode"] == "TRANSACTIONS"


def test_transaction_cash_discrepancy_does_not_switch_modes_or_create_funding():
    client = FakeClient()
    client.account["trackingMode"] = "TRANSACTIONS"
    result = sync(client, [source(transaction())], {
        "source-account": {**ENTRY, "wealthfolioAccountId": ACCOUNT["id"]},
    })
    assert any("cash history does not reconcile" in error for error in result["errors"])
    assert result["created"] == 1 and result["balances"] == 0
    assert client.account["trackingMode"] == "TRANSACTIONS" and client.snapshot is None
    assert len(client.rows) == 2 and "account" not in client.writes and "snapshot" not in client.writes


def test_calculated_zero_is_not_verified_as_an_observed_zero():
    client = FakeClient()
    client.snapshot = {
        "accountId": ACCOUNT["id"], "snapshotDate": "2026-02-01",
        "holdings": [], "cashBalances": {"USD": "0"},
    }
    client.snapshot_source = "CALCULATED"
    result = sync(client, [replace(source(), balance=Decimal(0))], {
        "source-account": {**ENTRY, "wealthfolioAccountId": ACCOUNT["id"]},
    }, check=True)
    assert any("did not settle" in error for error in result["errors"])
    assert client.writes == []


def test_card_gap_does_not_create_a_synthetic_activity():
    from dataclasses import replace
    from importers.simplefin.local_sync import sync_cash

    client = FakeClient()
    client.account["accountType"] = "CREDIT_CARD"
    result = {"created": 0, "updated": 0, "errors": []}
    expected = sync_cash(client, replace(source(), balance=Decimal("-42")),
                         ENTRY, client.account, client.rows, ZONE, result, {}, False)
    assert expected == Decimal("-42")
    assert client.writes == []
    assert len(client.rows) == 1


def test_unposted_card_rows_do_not_become_assumed_cash_movements():
    from dataclasses import replace
    from importers.simplefin.local_sync import sync_cash
    client = FakeClient()
    client.account["accountType"] = "CREDIT_CARD"
    client.rows.append({
        "id": "synthetic-pending", "accountId": ACCOUNT["id"],
        "activityType": "WITHDRAWAL", "amount": "30", "date": "2026-02-02T18:00:00Z", "status": "PENDING",
    })
    result = {"created": 0, "updated": 0, "errors": []}
    expected = sync_cash(client, replace(source(), balance=Decimal("-42")),
                         ENTRY, client.account, client.rows, ZONE, result, {}, False)
    assert expected == Decimal("-42")
    assert client.writes == []


def test_missing_native_card_calculation_is_not_an_observed_zero_balance():
    client = FakeClient()
    expected = [{"accountId": ACCOUNT["id"], "accountType": "CREDIT_CARD",
                 "name": "Synthetic Card", "sourceBalance": "0"}]
    with pytest.raises(ValueError, match="did not settle"):
        verify_balances(client, expected, {}, attempts=2, sleeper=lambda _: None)
    assert expected[0]["wealthfolioBalance"] is None


def test_replay_does_not_promote_a_native_pending_or_void_activity_to_posted():
    creates, _ = plan([transaction()])
    row = persisted(creates[0])
    for status in ("PENDING", "VOID"):
        row["status"] = status
        with pytest.raises(ValueError, match="not posted"):
            plan([transaction()], [row])


def test_bank_transaction_timezone_is_explicit_and_separate_from_app_observation_day():
    payload = raw_payload()
    observation = int(datetime(2026, 2, 2, 1, tzinfo=ZoneInfo("UTC")).timestamp())
    payload["accounts"][0]["balance-date"] = observation
    payload["accounts"][0]["transactions"][0]["posted"] = observation
    parsed, _ = read_accounts(payload, balance_zone=ZONE)
    assert parsed[0].balance_date == date(2026, 2, 1)
    assert parsed[0].transactions[0].posted == date(2026, 2, 2)
    assert parsed[0].transactions[0].posted_utc_date == date(2026, 2, 2)
    assert parsed[0].balance_utc_date == date(2026, 2, 2)
    creates, _ = plan_transactions(parsed[0], ENTRY, ACCOUNT, [], ZONE)
    assert creates[0]["activityDate"][:10] == "2026-02-02"
    bank_entry = {**ENTRY, "transactionTimezone": "America/New_York"}
    creates, _ = plan_transactions(parsed[0], bank_entry, ACCOUNT, [], ZONE)
    assert creates[0]["activityDate"][:10] == "2026-02-01"
    assert json.loads(creates[0]["metadata"])["simplefin"]["utcDate"] == "2026-02-02"


def test_bank_after_midnight_posting_does_not_move_to_the_previous_app_day():
    payload = raw_payload()
    stamp = int(datetime(2026, 2, 2, 5, 30, tzinfo=ZoneInfo("UTC")).timestamp())
    payload["accounts"][0]["balance-date"] = stamp
    payload["accounts"][0]["transactions"][0]["posted"] = stamp
    observed, _ = read_accounts(payload, balance_zone=ZONE)
    assert observed[0].balance_date == date(2026, 2, 1)
    creates, _ = plan_transactions(observed[0], {
        **ENTRY, "transactionTimezone": "America/New_York",
    }, ACCOUNT, [], ZONE)
    assert creates[0]["activityDate"][:10] == "2026-02-02"


@pytest.mark.parametrize("zone", ["not/a-timezone", None, 1])
def test_invalid_explicit_transaction_timezone_is_not_silently_defaulted(zone):
    with pytest.raises(ValueError, match="valid IANA timezone"):
        plan([transaction()], entry={**ENTRY, "transactionTimezone": zone})


def test_card_balance_projection_does_not_count_same_utc_day_twice():
    from importers.simplefin.local_sync import sync_cash
    payload = raw_payload()
    stamp = int(datetime(2026, 2, 2, 1, tzinfo=ZoneInfo("UTC")).timestamp())
    payload["accounts"][0].update(balance="-50", **{"balance-date": stamp})
    payload["accounts"][0]["transactions"][0].update(amount="-50", posted=stamp)
    observed, _ = read_accounts(payload, balance_zone=ZONE)
    client = FakeClient()
    client.account["accountType"] = "CREDIT_CARD"
    result = {"created": 0, "updated": 0, "errors": []}
    expected = sync_cash(client, observed[0], ENTRY, client.account, [], ZONE, result, {}, True)
    assert expected == Decimal("-50")
    assert client.writes == []


def test_legacy_utc_day_is_corrected_in_place_from_the_same_source_epoch():
    payload = raw_payload()
    stamp = int(datetime(2026, 2, 2, 1, tzinfo=ZoneInfo("UTC")).timestamp())
    payload["accounts"][0]["transactions"][0].update(
        posted=stamp, amount="1.25", description="Interest Paid",
    )
    old, _ = read_accounts(payload)
    creates, _ = plan_transactions(old[0], ENTRY, ACCOUNT, [], ZONE)
    row = persisted(creates[0])
    old_meta = json.loads(row["metadata"])
    del old_meta["simplefin"]["utcDate"]
    row["metadata"] = json.dumps(old_meta)
    observed, _ = read_accounts(payload, balance_zone=ZONE)
    bank_entry = {**ENTRY, "transactionTimezone": "America/New_York"}
    creates, updates = plan_transactions(observed[0], bank_entry, ACCOUNT, [row], ZONE)
    assert creates == []
    assert len(updates) == 1
    assert updates[0]["id"] == row["id"]
    assert updates[0]["idempotencyKey"] == row["idempotencyKey"]
    assert updates[0]["activityDate"][:10] == "2026-02-01"
    assert updates[0]["amount"] == "1.25"
    assert json.loads(updates[0]["metadata"])["simplefin"]["utcDate"] == "2026-02-02"
    assert plan_transactions(observed[0], bank_entry, ACCOUNT, [persisted(updates[0])], ZONE) == ([], [])


def test_card_projection_retains_utc_basis_when_local_posted_day_matches_balance_utc_day():
    from importers.simplefin.local_sync import sync_cash
    payload = raw_payload()
    balance_stamp = int(datetime(2026, 2, 2, 1, tzinfo=ZoneInfo("UTC")).timestamp())
    posted_stamp = int(datetime(2026, 2, 3, 1, tzinfo=ZoneInfo("UTC")).timestamp())
    payload["accounts"][0].update(balance="-50", **{"balance-date": balance_stamp})
    payload["accounts"][0]["transactions"][0].update(amount="-5", posted=posted_stamp)
    observed, _ = read_accounts(payload, balance_zone=ZONE)
    bank_entry = {**ENTRY, "transactionTimezone": "America/New_York"}
    creates, _ = plan_transactions(observed[0], bank_entry, ACCOUNT, [], ZONE)
    assert creates[0]["activityDate"][:10] == observed[0].balance_utc_date.isoformat()
    client = FakeClient()
    client.account["accountType"] = "CREDIT_CARD"
    result = {"created": 0, "updated": 0, "errors": []}
    expected = sync_cash(client, observed[0], bank_entry, client.account, [], ZONE, result, {}, True)
    assert expected == Decimal("-55")
    assert client.writes == []


def test_read_only_preview_reports_needed_liability_observation_update():
    client = FakeClient()
    get = client.get
    client.get = lambda path: [{
        "id": "synthetic-loan", "kind": "liability", "currency": "USD",
        "marketValue": "999", "valuationDate": "2026-01-01",
    }] if path == "/alternative-holdings" else get(path)
    observed = SimpleFinAccount("synthetic-loan-source", "Synthetic Loan", "Synthetic Bank",
                               "USD", Decimal("-1200"), date(2026, 2, 1))
    result = sync(client, [observed], {
        observed.id: {"wealthfolioAlternativeAssetId": "synthetic-loan"},
    }, dry_run=True)
    assert result["balances"] == 1
    assert any("liability valuation update" in error for error in result["errors"])
    assert client.writes == []


@pytest.mark.parametrize("native_value", ["100", "99"])
def test_check_rejects_wrong_holdings_without_repairing_them(native_value):
    client = FakeClient()
    client.account["accountType"] = "SECURITIES"
    client.get = lambda path: [] if path == "/alternative-holdings" else (
        [{"snapshotDate": "2026-02-01", "source": "MANUAL_ENTRY"}] if path.startswith("/snapshots") else
        [{"holdingType": "security", "instrument": {"id": "synthetic-unrelated", "symbol": "OTHER"},
          "quantity": "1", "marketValue": {"local": native_value}}]
    )
    observed = investment_source()
    result = sync(client, [observed], {
        observed.id: {"wealthfolioAccountId": ACCOUNT["id"]},
    }, check=True)
    assert any("did not settle" in error for error in result["errors"])
    assert client.writes == []


def test_check_reports_unlinked_transfers_and_accounts_outside_scope_without_crashing():
    client = FakeClient()
    for id, account in (("synthetic-in-scope", ACCOUNT["id"]), ("synthetic-outside", "synthetic-archived")):
        client.rows.append({
            "id": id, "accountId": account, "activityType": "TRANSFER_OUT", "amount": "25",
            "currency": "USD", "date": "2026-02-01T18:00:00Z", "status": "POSTED",
            "metadata": {"simplefin": {"description": "Synthetic Transfer"}},
        })
    result = sync(client, [], {}, check=True)
    assert result["unmatchedTransferCount"] == 1
    assert any("outside the current account scope" in warning for warning in result["warnings"])
    assert client.writes == []


def test_transient_native_difference_keeps_an_actionable_account_name():
    client = FakeClient()
    values = iter(("12", "10"))
    client.get = lambda _: [{"marketValue": {"local": next(values)}}]
    expected = [{"accountId": ACCOUNT["id"], "accountType": "CASH",
                 "name": "Synthetic Checking", "sourceBalance": "10"}]
    with pytest.raises(ValueError, match="Synthetic Checking"):
        verify_balances(client, expected, {}, attempts=2, sleeper=lambda _: None, repair=False)


def test_legacy_card_reconciliation_is_reported_not_updated():
    from importers.simplefin.local_sync import sync_cash

    client = FakeClient()
    client.account["accountType"] = "CREDIT_CARD"
    client.rows.append({
        "id": "synthetic-balance", "accountId": ACCOUNT["id"],
        "date": "2026-02-01T18:00:00Z", "activityType": "TRANSFER_IN",
        "amount": "10", "idempotencyKey": "simplefin-balance:source-account",
    })
    result = {"created": 0, "updated": 0, "errors": []}
    sync_cash(client, source(), ENTRY, client.account, client.rows, ZONE, result, {}, False)
    assert "legacy synthetic" in result["errors"][0]
    assert client.writes == []
    assert client.rows[-1]["amount"] == "10"


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


def test_additional_card_scope_is_blocked_without_discarding_a_healthy_account():
    from dataclasses import replace
    client = FakeClient()
    card = {**client.account, "id": "synthetic-card", "accountType": "CREDIT_CARD"}
    client.list_accounts = lambda: [client.account, card]
    partial = replace(source(transaction()), id="synthetic-card-source", name="Synthetic Additional Card")
    result = sync(client, [partial, source(transaction())], {
        partial.id: {**ENTRY, "wealthfolioAccountId": card["id"], "sourceScope": "cardmember"},
        "source-account": {**ENTRY, "wealthfolioAccountId": ACCOUNT["id"]},
    })
    assert result["created"] == 1
    assert len(result["errors"]) == 1
    assert "primary billing account" in result["errors"][0]
    assert not any(row["accountId"] == card["id"] for row in client.rows)


def test_unknown_source_currency_does_not_block_a_healthy_account():
    from dataclasses import replace
    client = FakeClient()
    unknown = replace(source(), id="synthetic-unknown-units", currency="")
    result = sync(client, [unknown, source(transaction())], {
        unknown.id: {**ENTRY, "wealthfolioAccountId": "synthetic-unknown-destination"},
        "source-account": {**ENTRY, "wealthfolioAccountId": ACCOUNT["id"]},
    })
    assert result["created"] == 1
    assert len(result["errors"]) == 1
    assert "refusing to assume USD" in result["errors"][0]


def test_native_bulk_errors_are_not_reported_as_success():
    client = FakeClient()
    client.save_activities = lambda **kwargs: {"created": [], "updated": [], "errors": [{"message": "invalid"}]}
    with pytest.raises(ValueError, match="rejected"):
        save_changes(client, [{"accountId": "a"}], [])


def test_unsettled_native_values_fail_instead_of_claiming_success():
    expected = [{"accountId": "cash-account", "accountType": "CASH", "name": "Synthetic", "sourceBalance": "10"}]
    with pytest.raises(ValueError, match="did not settle"):
        verify_balances(FakeClient(), expected, {}, attempts=2, sleeper=lambda _: None)


def test_importer_leaves_transfer_pairing_to_wealthfolio():
    import importers.simplefin.local_sync as module
    client = FakeClient()
    client.rows.append({
        "id": "unlinked", "accountId": ACCOUNT["id"], "activityType": "TRANSFER_OUT",
        "date": "2026-02-01T18:00:00Z", "amount": "25", "currency": "USD",
        "metadata": {"simplefin": {"description": "Synthetic Transfer"}},
    })
    result = {"unmatchedTransferCount": 0, "warnings": [], "errors": [], "accounts": []}
    module.verify_import(client, {ACCOUNT["id"]: client.account}, result, {})
    assert result["unmatchedTransferCount"] == 1
    assert client.writes == ["/portfolio/recalculate"]


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
            if path.startswith("/snapshots"):
                return ([{"snapshotDate": "2026-02-01", "source": "MANUAL_ENTRY"}]
                        if self.published else [])
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
        {"accountId": "investment", "snapshotDate": "2026-02-01",
         "holdings": [{"assetId": "asset", "quantity": "2"}]},
        [{"symbol": "asset", "close": "45"}],
    )}
    verify_balances(client, expected, positions, attempts=3, sleeper=lambda _: None)
    assert client.published == 1
