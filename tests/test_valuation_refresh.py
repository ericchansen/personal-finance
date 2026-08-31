import json
import urllib.parse
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from importers.rebuild.safety import plan_fingerprint
from importers.valuations import cli as valuation_cli
from importers.valuations.pipeline import (
    ValuationError,
    build_projection_plan,
    build_refresh_plan,
    validate_refresh_plan,
    write_immutable_json,
    write_refresh_outputs,
)


NOW = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)


def private_inputs(tmp_path: Path, *, evidence: bool = False) -> Path:
    data = tmp_path / "private"
    facts = data / "facts"
    facts.mkdir(parents=True)
    (facts / "assets.json").write_text(
        json.dumps(
            {
                "facts": [
                    {
                        "type": "property",
                        "name": "Synthetic Home",
                        "address": "Synthetic address",
                        "purchaseDate": "2020-01-01",
                        "purchasePrice": "300000",
                        "saleDate": None,
                        "salePrice": None,
                        "netProceeds": None,
                        "appraisals": [
                            {"date": "2026-03-01", "value": "400000", "type": "appraisal"}
                        ],
                        "source": "fixture",
                        "sourcePath": None,
                        "notes": "synthetic",
                    },
                    {
                        "type": "vehicle",
                        "name": "Synthetic Vehicle",
                        "purchaseDate": "2025-01-01",
                        "purchasePrice": "30000",
                        "currentValue": "30000",
                        "currentValueDate": "2025-01-01",
                        "source": "fixture",
                        "sourcePath": None,
                        "notes": "synthetic",
                    },
                    {
                        "type": "loan",
                        "name": "Synthetic Mortgage",
                        "principal": "250000",
                        "annualRate": "0.04",
                        "termMonths": 360,
                        "originationDate": "2020-01-01",
                        "firstPayment": "2020-02-01",
                        "lender": "Synthetic Bank",
                        "linkedTo": "Synthetic Home",
                        "payoffAmount": None,
                        "payoffDate": None,
                        "pmi": False,
                        "source": "fixture",
                        "sourcePath": None,
                        "notes": "synthetic",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    simplefin = data / "simplefin"
    simplefin.mkdir()
    (simplefin / "account-map.json").write_text(
        json.dumps(
            {
                "version": 1,
                "accounts": {
                    "source-loan": {
                        "action": "monitor",
                        "assertionAccountId": "loan:Synthetic Mortgage",
                        "wealthfolioAlternativeAssetId": "wf-loan",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    raw = data / "raw" / "simplefin" / "2026-08-27"
    raw.mkdir(parents=True)
    (raw / "simplefin-120000.json").write_text(
        json.dumps(
            {
                "accounts": [
                    {
                        "id": "source-loan",
                        "name": "Synthetic Mortgage",
                        "org": {"name": "Synthetic Bank"},
                        "currency": "USD",
                        "balance": "-240000",
                        "balance-date": 1787788800,
                        "transactions": [],
                    }
                ],
                "errors": [],
            }
        ),
        encoding="utf-8",
    )
    if evidence:
        source = data / "raw" / "valuations"
        source.mkdir(parents=True)
        (source / "home.txt").write_text("synthetic appraisal", encoding="utf-8")
        (source / "vehicle.txt").write_text("synthetic offer", encoding="utf-8")
        (source / "home.txt").chmod(0o444)
        (source / "vehicle.txt").chmod(0o444)
        records = data / "valuations" / "evidence"
        records.mkdir(parents=True)
        for entity, value, method, filename in (
            ("property:Synthetic Home", "410000", "licensed-appraisal", "home.txt"),
            (
                "vehicle:Synthetic Vehicle",
                "24000",
                "official-trade-in-estimate",
                "vehicle.txt",
            ),
        ):
            (records / f"{filename}.json").write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "entityId": entity,
                        "date": "2026-08-27",
                        "value": value,
                        "currency": "USD",
                        "method": method,
                        "sourceFile": f"raw/valuations/{filename}",
                    }
                ),
                encoding="utf-8",
            )
    return data


def test_missing_current_asset_evidence_requires_review_but_loans_refresh(tmp_path):
    plan = build_refresh_plan(
        private_inputs(tmp_path), as_of=date(2026, 8, 27), generated_at=NOW
    )
    assert not plan["ready"]
    assert plan["reviewNeeded"] == 2
    assert [row["entity_id"] for row in plan["canonicalRows"]] == [
        "loan:Synthetic Mortgage"
    ]
    validate_refresh_plan(plan)


def test_current_explicit_evidence_makes_complete_monthly_plan(tmp_path):
    plan = build_refresh_plan(
        private_inputs(tmp_path, evidence=True),
        as_of=date(2026, 8, 27),
        generated_at=NOW,
    )
    assert plan["ready"]
    assert len(plan["canonicalRows"]) == 3
    assert all(item["evidenceHashes"] for item in plan["entities"])
    assert next(
        row for row in plan["canonicalRows"] if row["entity_id"].startswith("loan:")
    )["value"] == "-240000"
    assert next(
        item for item in plan["entities"] if item["kind"] == "liability"
    )["wealthfolioTargetId"] == "wf-loan"


def test_positive_liability_balance_is_never_silently_re_signed(tmp_path):
    data = private_inputs(tmp_path)
    snapshot = next((data / "raw" / "simplefin").rglob("simplefin-*.json"))
    payload = json.loads(snapshot.read_text())
    payload["accounts"][0]["balance"] = "240000"
    snapshot.write_text(json.dumps(payload))
    plan = build_refresh_plan(data, as_of=date(2026, 8, 27), generated_at=NOW)
    loan = next(item for item in plan["entities"] if item["kind"] == "liability")
    assert loan["status"] == "review-needed"
    assert loan["reason"] == "simplefin-liability-balance-sign-is-positive"


def test_explicit_evidence_must_reference_a_real_private_source(tmp_path):
    data = private_inputs(tmp_path)
    records = data / "valuations" / "evidence"
    records.mkdir(parents=True)
    (records / "bad.json").write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "entityId": "vehicle:Synthetic Vehicle",
                "date": "2026-08-27",
                "value": "24000",
                "currency": "USD",
                "method": "offer",
                "sourceFile": "raw/valuations/missing.pdf",
            }
        )
    )
    with pytest.raises(ValuationError, match="does not exist"):
        build_refresh_plan(data, as_of=date(2026, 8, 27), generated_at=NOW)


@pytest.mark.parametrize(
    "source_file",
    (
        "valuations/evidence/bad.json",
        "facts/assets.json",
        "normalized/generated-valuation.txt",
    ),
)
def test_evidence_record_cannot_cite_metadata_facts_or_generated_output(
    tmp_path, source_file
):
    data = private_inputs(tmp_path)
    records = data / "valuations" / "evidence"
    records.mkdir(parents=True)
    generated = data / "normalized" / "generated-valuation.txt"
    generated.parent.mkdir()
    generated.write_text("synthetic generated output", encoding="utf-8")
    record = records / "bad.json"
    record.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "entityId": "vehicle:Synthetic Vehicle",
                "date": "2026-08-27",
                "value": "24000",
                "currency": "USD",
                "method": "offer",
                "sourceFile": source_file,
            }
        )
    )
    with pytest.raises(ValuationError, match="distinct artifact"):
        build_refresh_plan(data, as_of=date(2026, 8, 27), generated_at=NOW)


def test_evidence_source_must_be_immutable(tmp_path):
    data = private_inputs(tmp_path, evidence=True)
    source = data / "raw" / "valuations" / "vehicle.txt"
    source.chmod(0o666)

    with pytest.raises(ValuationError, match="immutable read-only artifact"):
        build_refresh_plan(data, as_of=date(2026, 8, 27), generated_at=NOW)


def test_future_dated_simplefin_balance_requires_review(tmp_path):
    data = private_inputs(tmp_path)
    snapshot = next((data / "raw" / "simplefin").rglob("simplefin-*.json"))
    payload = json.loads(snapshot.read_text())
    payload["accounts"][0]["balance-date"] = 1788134400
    snapshot.write_text(json.dumps(payload))
    plan = build_refresh_plan(data, as_of=date(2026, 8, 27), generated_at=NOW)
    loan = next(item for item in plan["entities"] if item["kind"] == "liability")
    assert loan["status"] == "review-needed"
    assert loan["reason"] == "simplefin-monitor-balance-is-future-dated"


def test_outputs_are_private_immutable_and_fingerprinted(tmp_path):
    data = private_inputs(tmp_path, evidence=True)
    repo = tmp_path / "repo"
    repo.mkdir()
    plan = build_refresh_plan(data, as_of=date(2026, 8, 27), generated_at=NOW)
    plan_path, csv_path = write_refresh_outputs(data, repo, plan)
    assert plan["fingerprint"] in plan_path.read_text()
    assert "entity_id" in csv_path.read_text()
    assert not (plan_path.stat().st_mode & 0o200)
    plan_path.chmod(0o666)
    write_refresh_outputs(data, repo, plan)
    assert not (plan_path.stat().st_mode & 0o200)


class FakeClient:
    def __init__(self):
        self.get_paths = []
        self.mutation_calls = []
        self.quotes = {}
        self.holdings = [
            {
                "id": "wf-home",
                "kind": "property",
                "name": "Synthetic Home",
                "currency": "USD",
                "marketValue": "400000",
                "valuationDate": "2026-03-01T00:00:00Z",
                "metadata": {"canonical_entity_id": "property:Synthetic Home"},
                "linkedAssetId": None,
            },
            {
                "id": "wf-vehicle",
                "kind": "vehicle",
                "name": "Synthetic Vehicle",
                "currency": "USD",
                "marketValue": "30000",
                "valuationDate": "2025-01-01T00:00:00Z",
                "metadata": {"canonical_entity_id": "vehicle:Synthetic Vehicle"},
                "linkedAssetId": None,
            },
            {
                "id": "wf-loan",
                "kind": "liability",
                "name": "Synthetic Mortgage",
                "currency": "USD",
                "marketValue": "245000",
                "valuationDate": "2026-03-01T00:00:00Z",
                "metadata": {"canonical_entity_id": "loan:Synthetic Mortgage"},
                "linkedAssetId": "wf-home",
            },
        ]

    def get(self, path):
        self.get_paths.append(path)
        if path == "/app/info":
            return {"version": "3.7.0", "dbPath": "/data/staging.db"}
        if path == "/alternative-holdings":
            return self.holdings
        if path.startswith("/market-data/quotes/history"):
            asset_id = urllib.parse.parse_qs(urllib.parse.urlparse(path).query)["symbol"][0]
            return [
                row for row in self.quotes.values() if row["assetId"] == asset_id
            ]
        raise AssertionError(path)

    def put(self, path, payload):
        self.mutation_calls.append(("put", path, payload))
        raise AssertionError("read-only valuation workflow attempted PUT")

    def post(self, path, payload):
        self.mutation_calls.append(("post", path, payload))
        raise AssertionError("read-only valuation workflow attempted POST")

    def delete(self, path):
        self.mutation_calls.append(("delete", path))
        raise AssertionError("read-only valuation workflow attempted DELETE")

    def backup_database(self):
        self.mutation_calls.append(("backup",))
        raise AssertionError("read-only valuation workflow attempted backup")


def test_projection_binds_identity_delta_evidence_and_staging_environment(tmp_path):
    refresh = build_refresh_plan(
        private_inputs(tmp_path, evidence=True),
        as_of=date(2026, 8, 27),
        generated_at=NOW,
    )
    client = FakeClient()
    projection = build_projection_plan(client, "http://127.0.0.1:18088", refresh)
    assert projection["ready"]
    assert projection["readOnly"]
    assert not projection["applySupported"]
    assert len(projection["comparisons"]) == 3
    assert all(item["expectedHoldingFingerprint"] for item in projection["comparisons"])
    assert all(item["evidenceHashes"] for item in projection["comparisons"])
    assert {
        item["canonicalEntityId"]: item["netWorthDelta"]
        for item in projection["comparisons"]
    }["loan:Synthetic Mortgage"] == "5000"
    assert not client.mutation_calls


def test_projection_can_bind_legacy_holding_by_exact_name_and_kind(tmp_path):
    refresh = build_refresh_plan(
        private_inputs(tmp_path, evidence=True),
        as_of=date(2026, 8, 27),
        generated_at=NOW,
    )
    client = FakeClient()
    for holding in client.holdings:
        holding["metadata"] = {}
    for item in refresh["entities"]:
        item.pop("wealthfolioTargetId", None)
    material = {key: value for key, value in refresh.items() if key != "fingerprint"}
    refresh["fingerprint"] = plan_fingerprint(material)

    projection = build_projection_plan(
        client, "http://127.0.0.1:18088", refresh
    )

    assert projection["ready"]
    assert {item["identityMode"] for item in projection["comparisons"]} == {
        "exact-name-and-kind"
    }


def test_projection_rejects_conflicting_tag_instead_of_name_fallback(tmp_path):
    refresh = build_refresh_plan(
        private_inputs(tmp_path, evidence=True),
        as_of=date(2026, 8, 27),
        generated_at=NOW,
    )
    client = FakeClient()
    home = next(item for item in client.holdings if item["id"] == "wf-home")
    home["metadata"] = {}
    client.holdings.append({
        **home,
        "id": "wf-conflicting-home",
        "metadata": {"canonical_entity_id": "property:Different Synthetic Home"},
    })

    projection = build_projection_plan(
        client, "http://127.0.0.1:18088", refresh
    )

    assert not projection["ready"]
    assert any(
        blocker["code"] == "holding-canonical-identity-conflict"
        for blocker in projection["blockers"]
    )
    assert not any(
        operation["canonicalEntityId"] == "property:Synthetic Home"
        for operation in projection["comparisons"]
    )


def test_projection_is_prohibited_on_production_even_for_planning(tmp_path):
    refresh = build_refresh_plan(
        private_inputs(tmp_path, evidence=True),
        as_of=date(2026, 8, 27),
        generated_at=NOW,
    )
    with pytest.raises(ValuationError, match="loopback staging"):
        build_projection_plan(FakeClient(), "http://127.0.0.1:8088", refresh)


def test_projection_is_prohibited_on_non_loopback_host(tmp_path):
    refresh = build_refresh_plan(
        private_inputs(tmp_path, evidence=True),
        as_of=date(2026, 8, 27),
        generated_at=NOW,
    )
    with pytest.raises(ValuationError, match="loopback staging"):
        build_projection_plan(FakeClient(), "https://finance.example.test", refresh)


def test_cli_rejects_apply_on_staging_before_contacting_api(tmp_path, monkeypatch):
    api_contacts = []
    monkeypatch.setattr(
        valuation_cli,
        "_client",
        lambda base_url: api_contacts.append(base_url),
    )

    with pytest.raises(SystemExit) as exc:
        valuation_cli.main([
            "project",
            "--refresh-plan",
            str(tmp_path / "private-plan.json"),
            "--base-url",
            "http://127.0.0.1:18088",
            "--apply",
        ])

    assert exc.value.code == 2
    assert not api_contacts


def test_current_fact_value_without_source_artifact_still_requires_review(tmp_path):
    data = private_inputs(tmp_path)
    path = data / "facts" / "assets.json"
    facts = json.loads(path.read_text())
    vehicle = next(item for item in facts["facts"] if item["type"] == "vehicle")
    vehicle["currentValueDate"] = "2026-08-27"
    path.write_text(json.dumps(facts))
    plan = build_refresh_plan(data, as_of=date(2026, 8, 27), generated_at=NOW)
    item = next(row for row in plan["entities"] if row["kind"] == "vehicle")
    assert item["status"] == "review-needed"
    assert item["reason"] == "no-explicit-current-month-evidence"


def test_projection_blocks_currency_mismatch(tmp_path):
    refresh = build_refresh_plan(
        private_inputs(tmp_path, evidence=True),
        as_of=date(2026, 8, 27),
        generated_at=NOW,
    )
    client = FakeClient()
    client.holdings[0]["currency"] = "EUR"
    projection = build_projection_plan(client, "http://127.0.0.1:18088", refresh)
    assert not projection["ready"]
    assert any(
        blocker["code"] == "holding-state-mismatch"
        for blocker in projection["blockers"]
    )


def test_existing_manual_quote_is_reported_without_mutation(tmp_path):
    refresh = build_refresh_plan(
        private_inputs(tmp_path, evidence=True),
        as_of=date(2026, 8, 27),
        generated_at=NOW,
    )
    client = FakeClient()
    previous = {
        "id": "existing-uuid",
        "assetId": "wf-home",
        "timestamp": "2026-08-27T00:00:00Z",
        "open": 390000.125,
        "high": 391000.25,
        "low": 389000.75,
        "close": 390000.125,
        "adjclose": 389999.5,
        "volume": 17,
        "currency": "USD",
        "dataSource": "MANUAL",
        "createdAt": "2026-08-20T15:30:00Z",
        "notes": "Synthetic prior source",
    }
    client.quotes[("wf-home", "2026-08-27")] = dict(previous)
    home = next(item for item in client.holdings if item["id"] == "wf-home")
    home["marketValue"] = str(previous["close"])
    home["valuationDate"] = "2026-08-27"
    url = "http://127.0.0.1:18088"
    projection = build_projection_plan(client, url, refresh)

    assert client.quotes[("wf-home", "2026-08-27")] == previous
    assert not client.mutation_calls
    assert any(
        blocker["code"] == "target-date-quote-already-exists"
        for blocker in projection["blockers"]
    )


def test_projection_artifact_creation_is_idempotent_and_exclusive(tmp_path):
    matching = tmp_path / "projection-matching.json"
    write_immutable_json(matching, {"schemaVersion": 1, "ready": False})
    before = matching.read_bytes()
    write_immutable_json(matching, {"schemaVersion": 1, "ready": False})
    assert matching.read_bytes() == before

    changed = tmp_path / "projection-changed.json"
    changed.write_text('{"different":true}\n', encoding="utf-8")
    with pytest.raises(ValuationError, match="refusing to overwrite"):
        write_immutable_json(changed, {"schemaVersion": 1, "ready": False})
