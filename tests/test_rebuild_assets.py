import csv
from pathlib import Path

import pytest

from importers.rebuild.assets import apply_asset_plan, build_asset_plan
from importers.rebuild.decisions import DecisionError


class FakeClient:
    def __init__(self):
        self.holdings = []
        self.posts = []

    def get(self, path):
        return self.holdings

    def post(self, path, payload):
        self.posts.append((path, payload))
        return {"assetId": f"asset-{len(self.posts)}"}


def test_current_fact_assets_use_latest_canonical_valuation(tmp_path: Path):
    facts = tmp_path / "facts"
    facts.mkdir()
    (facts / "assets.json").write_text(
        """{"facts":[
        {"type":"vehicle","name":"Synthetic Car","purchaseDate":"2025-01-01",
         "purchasePrice":"30000","currentValue":"25000","currentValueDate":"2026-01-01",
         "source":"fixture","sourcePath":null,"notes":"synthetic"},
        {"type":"loan","name":"Synthetic Loan","principal":"20000","annualRate":"0.05",
         "termMonths":60,"originationDate":"2025-01-01","firstPayment":"2025-02-01",
         "lender":"Example","linkedTo":"Synthetic Car","payoffAmount":null,"payoffDate":null,
         "pmi":false,"source":"fixture","sourcePath":null,"notes":"synthetic"}
        ]}""",
        encoding="utf-8",
    )
    valuations = tmp_path / "valuations.csv"
    with valuations.open("w", encoding="utf-8", newline="") as target:
        writer = csv.DictWriter(
            target,
            fieldnames=[
                "date", "entity_id", "value", "currency", "source_file",
                "observed_or_derived",
            ],
        )
        writer.writeheader()
        writer.writerow({
            "date": "2026-01-01", "entity_id": "vehicle:Synthetic Car",
            "value": "25000", "currency": "USD", "source_file": "fixture",
            "observed_or_derived": "observed",
        })
        writer.writerow({
            "date": "2026-01-01", "entity_id": "loan:Synthetic Loan",
            "value": "-18000", "currency": "USD", "source_file": "fixture",
            "observed_or_derived": "derived",
        })

    client = FakeClient()
    plan = build_asset_plan(client, facts, valuations)
    assert [item["kind"] for item in plan.creates] == ["vehicle", "liability"]
    assert plan.creates[1]["currentValue"] == "18000"

    apply_asset_plan(client, plan)
    assert client.posts[1][1]["linkedAssetId"] == "asset-1"


def test_existing_holding_must_match_complete_canonical_state(tmp_path: Path):
    facts = tmp_path / "facts"
    facts.mkdir()
    (facts / "assets.json").write_text(
        """{"facts":[
        {"type":"vehicle","name":"Synthetic Car","purchaseDate":"2025-01-01",
         "purchasePrice":"30000","currentValue":"25000","currentValueDate":"2026-01-01",
         "source":"fixture","sourcePath":null,"notes":"synthetic"},
        {"type":"loan","name":"Synthetic Loan","principal":"20000","annualRate":"0.05",
         "termMonths":60,"originationDate":"2025-01-01","firstPayment":"2025-02-01",
         "lender":"Example","linkedTo":"Synthetic Car","payoffAmount":null,"payoffDate":null,
         "pmi":false,"source":"fixture","sourcePath":null,"notes":"synthetic"}
        ]}""",
        encoding="utf-8",
    )
    valuations = tmp_path / "valuations.csv"
    with valuations.open("w", encoding="utf-8", newline="") as target:
        writer = csv.DictWriter(
            target,
            fieldnames=[
                "date", "entity_id", "value", "currency", "source_file",
                "observed_or_derived",
            ],
        )
        writer.writeheader()
        writer.writerows([
            {
                "date": "2026-01-01", "entity_id": "vehicle:Synthetic Car",
                "value": "25000", "currency": "USD", "source_file": "fixture",
                "observed_or_derived": "observed",
            },
            {
                "date": "2026-01-01", "entity_id": "loan:Synthetic Loan",
                "value": "-18000", "currency": "USD", "source_file": "fixture",
                "observed_or_derived": "observed",
            },
        ])
    client = FakeClient()
    client.holdings = [
        {
            "id": "car", "name": "Synthetic Car", "kind": "VEHICLE",
            "currentValue": "25000", "valueDate": "2026-01-01",
            "metadata": {"canonical_entity_id": "vehicle:Synthetic Car"},
        },
        {
            "id": "loan", "name": "SYNTHETIC LOAN", "kind": "PROPERTY",
            "currentValue": "17000", "valueDate": "2025-12-31",
            "linkedAssetId": "wrong",
            "metadata": {"canonical_entity_id": "loan:wrong"},
        },
    ]

    with pytest.raises(DecisionError) as error:
        build_asset_plan(client, facts, valuations)

    assert all(
        field in str(error.value)
        for field in ("name", "kind", "canonical identity", "value", "value date", "liability link")
    )


def test_existing_property_must_preserve_purchase_basis(tmp_path: Path):
    facts = tmp_path / "facts"
    facts.mkdir()
    (facts / "property.json").write_text(
        """{"facts":[{
        "type":"property","name":"Synthetic Home","address":"Example address",
        "purchaseDate":"2024-01-15","purchasePrice":"250000",
        "saleDate":null,"salePrice":null,"netProceeds":null,
        "appraisals":[{"date":"2025-01-15","value":"275000","type":"appraisal"}],
        "source":"fixture","sourcePath":null,"notes":"synthetic"
        }]}""",
        encoding="utf-8",
    )
    valuations = tmp_path / "valuations.csv"
    with valuations.open("w", encoding="utf-8", newline="") as target:
        writer = csv.DictWriter(
            target,
            fieldnames=[
                "date", "entity_id", "value", "currency", "source_file",
                "observed_or_derived",
            ],
        )
        writer.writeheader()
        writer.writerow({
            "date": "2025-01-15",
            "entity_id": "property:Synthetic Home",
            "value": "275000",
            "currency": "USD",
            "source_file": "fixture",
            "observed_or_derived": "observed",
        })
    client = FakeClient()
    client.holdings = [{
        "id": "home",
        "name": "Synthetic Home",
        "kind": "PROPERTY",
        "marketValue": "275000",
        "valuationDate": "2025-01-15T12:00:00Z",
        "purchasePrice": None,
        "purchaseDate": "2024-01-20",
        "metadata": {"canonical_entity_id": "property:Synthetic Home"},
    }]

    with pytest.raises(DecisionError) as error:
        build_asset_plan(client, facts, valuations)

    assert "purchase price" in str(error.value)
    assert "purchase date" in str(error.value)
