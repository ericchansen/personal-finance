import csv
from datetime import date
from decimal import Decimal
from pathlib import Path

from importers.facts.schema import (
    DecisionFact,
    ParsedFact,
    QuoteFact,
)
from importers.rebuild.current import ledger_payloads, quote_payloads, rollover_plan
from importers.rebuild.current_cli import matching_health_issues


def test_ledger_payload_funds_and_buys_canonical_position(tmp_path):
    fields = {
        "positions.csv": [
            "as_of", "account_id", "symbol", "quantity", "price", "market_value",
            "basis_per_unit", "source_file",
        ],
        "valuations.csv": [
            "date", "entity_id", "value", "currency", "source_file",
            "observed_or_derived",
        ],
        "transactions.csv": [
            "date", "account_id", "amount", "description", "source_id",
            "source_file", "category", "transfer_group", "excluded",
            "exclusion_reason",
        ],
    }
    rows = {
        "positions.csv": {
            "as_of": "2026-01-02", "account_id": "ledger", "symbol": "BTC",
            "quantity": "0.1", "price": "", "market_value": "", "basis_per_unit": "",
            "source_file": "raw/ledger-live-normalized.json",
        },
        "valuations.csv": {
            "date": "2026-01-02", "entity_id": "ledger", "value": "8000",
            "currency": "USD", "source_file": "facts/assertions.json",
            "observed_or_derived": "observed",
        },
        "transactions.csv": {
            "date": "2026-01-02", "account_id": "ledger", "amount": "0.1",
            "description": "synthetic inflow", "source_id": "ledger:tx1",
            "source_file": "raw/ledger-live-normalized.json", "category": "inflow",
            "transfer_group": "", "excluded": "false", "exclusion_reason": "",
        },
    }
    for name in fields:
        with (tmp_path / name).open("w", encoding="utf-8", newline="") as target:
            writer = csv.DictWriter(target, fieldnames=fields[name])
            writer.writeheader()
            writer.writerow(rows[name])
    payloads = ledger_payloads(tmp_path, {"ledger": "app-ledger"})
    assert len(payloads) == 1
    assert payloads[0]["quantity"] == 0.1
    assert payloads[0]["unitPrice"] == 0.0
    assert payloads[0]["asset"]["symbol"] == "BTC"


class FakeClient:
    def __init__(self, assets=None, activities=None):
        self.assets = assets or []
        self.activities = activities or []

    def get(self, path):
        assert path == "/assets"
        return self.assets

    def iter_activities(self):
        yield from self.activities


def test_quote_payload_resolves_asset_identity():
    fact = QuoteFact(
        source="source", source_path="raw.json", notes="frozen",
        symbol="BTC", instrument_type="CRYPTO", on=date(2026, 8, 27),
        close=Decimal("80046.8203125"), currency="USD",
    )
    parsed = ParsedFact("quote", "quote:btc", {}, Path("facts.json"), fact)
    client = FakeClient(assets=[{
        "id": "asset-btc", "instrumentType": "CRYPTO", "instrumentSymbol": "BTC",
    }])

    payloads = quote_payloads(client, (parsed,))

    assert payloads[0]["symbol"] == "asset-btc"
    assert payloads[0]["close"] == 80046.8203125
    assert payloads[0]["dataSource"] == "MANUAL"


def test_rollover_plan_splits_source_total_without_changing_it():
    fact = DecisionFact(
        source="source", notes="reconciled", id="synthetic-rollover",
        kind="rollover-reconciliation", resolution="source-backed rollover",
    )
    data = {
        "sourceActivityId": "vanguard:rollover",
        "fromAccountId": "from",
        "toAccountId": "to",
        "sourceAmount": "125.50",
        "linkedAmount": "100.25",
    }
    parsed = ParsedFact(
        "decision", "decision:synthetic-rollover", data, Path("facts.json"), fact
    )
    client = FakeClient(activities=[{
        "id": "incoming", "idempotencyKey": "vanguard:rollover",
        "accountId": "app-to", "amount": 125.50, "currency": "USD",
        "date": "2025-01-15T00:00:00Z", "sourceGroupId": None,
    }])

    creates, updates, links = rollover_plan(
        client, (parsed,), {"from": "app-from", "to": "app-to"}
    )

    assert [row["amount"] for row in creates] == [100.25, 25.25]
    assert updates[0]["amount"] == 100.25
    assert links == [("incoming", "rebuild:rollover:synthetic-rollover:out")]


def test_zero_remainder_rollover_is_idempotent_without_remainder_activity():
    fact = DecisionFact(
        source="source", notes="fully linked", id="complete",
        kind="rollover-reconciliation", resolution="source-backed rollover",
    )
    parsed = ParsedFact(
        "decision",
        "decision:complete",
        {
            "sourceActivityId": "vanguard:rollover",
            "fromAccountId": "from",
            "toAccountId": "to",
            "sourceAmount": "100",
            "linkedAmount": "100",
        },
        Path("facts.json"),
        fact,
    )
    client = FakeClient(activities=[
        {
            "id": "incoming", "idempotencyKey": "vanguard:rollover",
            "accountId": "app-to", "amount": 100, "currency": "USD",
            "date": "2025-01-15T00:00:00Z", "sourceGroupId": "linked",
        },
        {
            "id": "outgoing", "idempotencyKey": "rebuild:rollover:complete:out",
            "accountId": "app-from", "amount": 100, "currency": "USD",
            "date": "2025-01-15T00:00:00Z", "sourceGroupId": "linked",
        },
    ])

    assert rollover_plan(
        client, (parsed,), {"from": "app-from", "to": "app-to"}
    ) == ([], [], [])


def test_health_dismissal_matches_code_and_reviewed_asset():
    fact = DecisionFact(
        source="synthetic review",
        id="reviewed-basis",
        kind="health-dismissal",
        resolution="Basis is unavailable.",
        affects=("asset-reviewed",),
    )
    parsed = ParsedFact(
        "decision",
        "decision:reviewed-basis",
        {"issueCode": "data_incomplete_valuation_basis"},
        Path("facts.json"),
        fact,
    )
    issues = [
        {
            "id": "issue-other",
            "code": "data_incomplete_valuation_basis",
            "affectedItems": [{"id": "asset-other"}],
        },
        {
            "id": "issue-reviewed",
            "code": "data_incomplete_valuation_basis",
            "affectedItems": [{"id": "asset-reviewed"}],
        },
    ]

    assert matching_health_issues(parsed, issues) == [issues[1]]
