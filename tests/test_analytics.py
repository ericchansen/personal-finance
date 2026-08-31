import csv
import hashlib
import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from importers.analytics import diagnostics
from importers.analytics import generator
from importers.analytics.generator import AnalyticsError, _interpolate, build, verify


def write_csv(path: Path, columns: tuple[str, ...], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as target:
        writer = csv.DictWriter(target, fieldnames=columns, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def fixture_root(tmp_path: Path) -> Path:
    root = tmp_path / "private"
    canonical = root / "normalized" / "canonical"
    accounts = [
        {
            "account_id": "cash-1",
            "institution": "Example Bank",
            "name": "Synthetic Cash",
            "kind": "CASH",
            "currency": "USD",
            "opened": "2024-01-01",
            "closed": "",
            "excluded": "false",
            "exclusion_reason": "",
        },
        {
            "account_id": "invest-1",
            "institution": "Example Broker",
            "name": "Synthetic IRA",
            "kind": "SECURITIES",
            "currency": "USD",
            "opened": "2024-01-01",
            "closed": "",
            "excluded": "false",
            "exclusion_reason": "",
        },
        {
            "account_id": "loan:Synthetic Mortgage",
            "institution": "Example Lender",
            "name": "Synthetic Mortgage",
            "kind": "liability",
            "currency": "USD",
            "opened": "2024-01-01",
            "closed": "2024-02-15",
            "excluded": "false",
            "exclusion_reason": "",
        },
    ]
    transactions = [
        {
            "date": "2024-02-05",
            "account_id": "cash-1",
            "amount": "50",
            "description": "Synthetic income",
            "source_id": "income-1",
            "source_file": "synthetic.csv",
            "category": "Income",
            "transfer_group": "",
            "symbol": "",
            "quantity": "",
            "price": "",
            "external_flow": "false",
            "excluded": "false",
            "exclusion_reason": "",
        },
        {
            "date": "2024-02-10",
            "account_id": "cash-1",
            "amount": "-10",
            "description": "Synthetic transfer",
            "source_id": "transfer-1",
            "source_file": "synthetic.csv",
            "category": "Transfer",
            "transfer_group": "pair-1",
            "symbol": "",
            "quantity": "",
            "price": "",
            "external_flow": "false",
            "excluded": "false",
            "exclusion_reason": "",
        },
        {
            "date": "2024-02-10",
            "account_id": "invest-1",
            "amount": "10",
            "description": "Synthetic contribution",
            "source_id": "contribution-1",
            "source_file": "synthetic.csv",
            "category": "",
            "transfer_group": "pair-1",
            "symbol": "",
            "quantity": "",
            "price": "",
            "external_flow": "true",
            "excluded": "false",
            "exclusion_reason": "",
        },
        {
            "date": "2024-03-05",
            "account_id": "cash-1",
            "amount": "5",
            "description": "Synthetic later income",
            "source_id": "income-2",
            "source_file": "synthetic.csv",
            "category": "Income",
            "transfer_group": "",
            "symbol": "",
            "quantity": "",
            "price": "",
            "external_flow": "false",
            "excluded": "false",
            "exclusion_reason": "",
        },
        {
            "date": "2024-02-11",
            "account_id": "cash-1",
            "amount": "-7",
            "description": "Synthetic unresolved transfer",
            "source_id": "transfer-2",
            "source_file": "synthetic.csv",
            "category": "Transfer",
            "transfer_group": "",
            "symbol": "",
            "quantity": "",
            "price": "",
            "external_flow": "false",
            "excluded": "false",
            "exclusion_reason": "",
        },
    ]
    positions = [
        {
            "as_of": "2024-02-29",
            "account_id": "invest-1",
            "symbol": "SYN",
            "quantity": "2",
            "price": "110",
            "market_value": "220",
            "basis_per_unit": "100",
            "source_file": "synthetic.csv",
        }
    ]
    valuations = [
        ("2024-01-31", "cash-1", "100"),
        ("2024-02-29", "cash-1", "150"),
        ("2024-01-31", "invest-1", "200"),
        ("2024-02-29", "invest-1", "220"),
        ("2024-01-01", "property:Synthetic House", "300000"),
        ("2024-02-15", "property:Synthetic House", "310000"),
        ("2024-01-01", "loan:Synthetic Mortgage", "-100000"),
        ("2024-02-15", "loan:Synthetic Mortgage", "-99500"),
    ]
    valuation_rows = [
        {
            "date": when,
            "entity_id": entity,
            "value": value,
            "currency": "USD",
            "source_file": "facts/facts.json",
            "observed_or_derived": "observed",
        }
        for when, entity, value in valuations
    ]
    schemas = {
        "accounts.csv": (
            "account_id",
            "institution",
            "name",
            "kind",
            "currency",
            "opened",
            "closed",
            "excluded",
            "exclusion_reason",
        ),
        "transactions.csv": (
            "date",
            "account_id",
            "amount",
            "description",
            "source_id",
            "source_file",
            "category",
            "transfer_group",
            "symbol",
            "quantity",
            "price",
            "external_flow",
            "excluded",
            "exclusion_reason",
        ),
        "positions.csv": (
            "as_of",
            "account_id",
            "symbol",
            "quantity",
            "price",
            "market_value",
            "basis_per_unit",
            "source_file",
        ),
        "valuations.csv": (
            "date",
            "entity_id",
            "value",
            "currency",
            "source_file",
            "observed_or_derived",
        ),
    }
    data = {
        "accounts.csv": accounts,
        "transactions.csv": transactions,
        "positions.csv": positions,
        "valuations.csv": valuation_rows,
    }
    for name in schemas:
        write_csv(canonical / name, schemas[name], data[name])
    facts = [
        {
            "type": "account",
            "id": "cash-1",
            "institution": "Example Bank",
            "displayName": "Synthetic Cash",
            "maskedNumber": None,
            "kind": "CASH",
            "opened": "2024-01-01",
            "closed": None,
            "excluded": False,
            "reason": None,
            "source": "synthetic fixture",
            "sourcePath": None,
            "notes": "synthetic",
        },
        {
            "type": "account",
            "id": "invest-1",
            "institution": "Example Broker",
            "displayName": "Synthetic IRA",
            "maskedNumber": None,
            "kind": "SECURITIES",
            "opened": "2024-01-01",
            "closed": None,
            "excluded": False,
            "reason": None,
            "source": "synthetic fixture",
            "sourcePath": None,
            "notes": "synthetic",
        },
        {
            "type": "property",
            "name": "Synthetic House",
            "address": "1 Example Way",
            "purchaseDate": "2024-01-01",
            "purchasePrice": "300000",
            "saleDate": "2024-02-15",
            "salePrice": "310000",
            "netProceeds": "200000",
            "appraisals": [],
            "source": "synthetic fixture",
            "sourcePath": None,
            "notes": "synthetic",
        },
        {
            "type": "loan",
            "name": "Synthetic Mortgage",
            "principal": "100000",
            "annualRate": "0.06",
            "termMonths": 360,
            "originationDate": "2024-01-01",
            "firstPayment": "2024-02-01",
            "lender": "Example Lender",
            "linkedTo": "Synthetic House",
            "payoffAmount": "99500",
            "payoffDate": "2024-02-15",
            "pmi": False,
            "source": "synthetic fixture",
            "sourcePath": None,
            "notes": "synthetic",
        },
    ]
    (root / "facts").mkdir()
    facts_path = root / "facts" / "facts.json"
    facts_path.write_text(json.dumps(facts), encoding="utf-8")
    manifest = {
        "schemaVersion": 2,
        "sourceFiles": [
            {
                "path": "facts/facts.json",
                "sha256": hashlib.sha256(facts_path.read_bytes()).hexdigest(),
            }
        ],
        "rowCounts": {
            name.removesuffix(".csv"): len(data[name])
            for name in schemas
        },
        "dataFiles": {
            name: hashlib.sha256((canonical / name).read_bytes()).hexdigest()
            for name in schemas
        },
    }
    (canonical / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return root


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as source:
        return list(csv.DictReader(source))


def current_output(root: Path) -> Path:
    analytics = root / "normalized" / "analytics"
    pointer = json.loads((analytics / "current.json").read_text())
    return analytics / "publications" / pointer["publicationId"]


def test_build_is_deterministic_and_never_forward_fills(tmp_path):
    root = fixture_root(tmp_path)
    first = build(root)
    output = current_output(root)
    before = {path.name: path.read_bytes() for path in output.iterdir()}
    second = build(root)
    after = {path.name: path.read_bytes() for path in current_output(root).iterdir()}
    assert before == after
    assert first == second
    assert verify(root)["verified"]

    monthly = {row["month"]: row for row in read_csv(output / "monthly-analytics.csv")}
    assert monthly["2024-01"]["cash_flow"] == ""
    assert monthly["2024-01"]["cash_flow_status"] == "unavailable"
    assert monthly["2024-01"]["data_quality"] == "partial"
    assert monthly["2024-02"]["cash_flow"] == "50.00"
    assert monthly["2024-02"]["cash_flow_status"] == "available"
    assert monthly["2024-02"]["property_equity"] == "0.00"
    assert monthly["2024-03"]["investable_assets"] == ""
    assert monthly["2024-03"]["net_worth"] == ""
    assert monthly["2024-03"]["data_quality"] == "partial"

    performance = read_csv(output / "investment-performance.csv")
    assert performance == [
        {
            "account_id": "invest-1",
            "start_date": "2024-01-31",
            "end_date": "2024-02-29",
            "start_value": "200.00",
            "end_value": "220.00",
            "external_inflows": "10.00",
            "external_outflows": "0.00",
            "investment_gain": "10.00",
            "modified_dietz_return": "0.04841402",
            "status": "supported",
            "reason": "Source-backed endpoint valuations with canonical external-flow labels.",
        }
    ]


def test_cash_flow_zero_requires_transaction_evidence(tmp_path):
    root = fixture_root(tmp_path)
    path = root / "normalized" / "canonical" / "transactions.csv"
    rows = read_csv(path)
    for row in rows:
        if row["date"].startswith("2024-02"):
            row["category"] = "Transfer"
            row["transfer_group"] = row["transfer_group"] or "synthetic-transfer"
    write_csv(path, generator.TRANSACTION_COLUMNS, rows)
    manifest_path = root / "normalized" / "canonical" / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["dataFiles"]["transactions.csv"] = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    build(root)
    monthly = {
        row["month"]: row
        for row in read_csv(current_output(root) / "monthly-analytics.csv")
    }
    assert monthly["2024-01"]["cash_flow"] == ""
    assert monthly["2024-01"]["cash_flow_status"] == "unavailable"
    assert monthly["2024-02"]["cash_flow"] == "0.00"
    assert monthly["2024-02"]["cash_flow_status"] == "available"


def test_cash_flow_requires_evidence_for_account_closed_mid_month(tmp_path):
    root = fixture_root(tmp_path)
    path = root / "normalized" / "canonical" / "accounts.csv"
    rows = read_csv(path)
    rows.append(
        {
            "account_id": "cash-closed",
            "institution": "Example Bank",
            "name": "Synthetic Closed Cash",
            "kind": "CASH",
            "currency": "USD",
            "opened": "2024-01-01",
            "closed": "2024-02-15",
            "excluded": "false",
            "exclusion_reason": "",
        }
    )
    write_csv(path, generator.ACCOUNT_COLUMNS, rows)
    manifest_path = root / "normalized" / "canonical" / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["dataFiles"]["accounts.csv"] = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest["rowCounts"]["accounts"] = len(rows)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    build(root)
    monthly = {
        row["month"]: row
        for row in read_csv(current_output(root) / "monthly-analytics.csv")
    }
    assert monthly["2024-02"]["cash_flow"] == ""
    assert monthly["2024-02"]["cash_flow_status"] == "unavailable"
    assert monthly["2024-02"]["data_quality"] == "partial"


def test_analytics_rejects_unsupported_canonical_manifest(tmp_path):
    root = fixture_root(tmp_path)
    manifest_path = root / "normalized" / "canonical" / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["schemaVersion"] = 1
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(AnalyticsError, match="manifest schema version is unsupported"):
        build(root)


def test_analytics_verifies_every_canonical_source_hash(tmp_path):
    root = fixture_root(tmp_path)
    source = root / "facts" / "facts.json"
    source.write_text(source.read_text() + "\n", encoding="utf-8")

    with pytest.raises(AnalyticsError, match="source hash mismatch: facts/facts.json"):
        build(root)


def test_failed_pointer_update_preserves_previous_publication(tmp_path, monkeypatch):
    root = fixture_root(tmp_path)
    build(root)
    analytics = root / "normalized" / "analytics"
    pointer_before = (analytics / "current.json").read_bytes()
    publication_before = current_output(root)

    path = root / "normalized" / "canonical" / "transactions.csv"
    rows = read_csv(path)
    rows[0]["amount"] = "51"
    write_csv(path, generator.TRANSACTION_COLUMNS, rows)
    manifest_path = root / "normalized" / "canonical" / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["dataFiles"]["transactions.csv"] = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    monkeypatch.setattr(
        generator,
        "_atomic_write",
        lambda _path, _content: (_ for _ in ()).throw(OSError("synthetic crash")),
    )
    with pytest.raises(OSError, match="synthetic crash"):
        build(root)

    assert (analytics / "current.json").read_bytes() == pointer_before
    assert publication_before.is_dir()
    assert len(list((analytics / "publications").glob("[0-9a-f]" * 64))) == 2


def test_analytics_fsyncs_directory_renames_before_pointer_advance(
    tmp_path, monkeypatch
):
    root = fixture_root(tmp_path)
    analytics = root / "normalized" / "analytics"
    publications = analytics / "publications"
    events = []
    real_replace = generator.os.replace

    def replace(source, target):
        events.append(("replace", Path(target)))
        real_replace(source, target)

    monkeypatch.setattr(generator.os, "replace", replace)
    monkeypatch.setattr(
        generator,
        "fsync_directory",
        lambda path: events.append(("fsync", Path(path))),
    )
    build(root)

    hierarchy_fsyncs = [
        ("fsync", analytics),
        ("fsync", analytics.parent),
    ]
    assert events[:2] == hierarchy_fsyncs
    publication_replace = next(
        index
        for index, event in enumerate(events)
        if event[0] == "replace" and event[1].parent == publications
    )
    publications_fsync = events.index(("fsync", publications))
    pointer_replace = events.index(("replace", analytics / "current.json"))
    analytics_fsync = max(
        index for index, event in enumerate(events) if event == ("fsync", analytics)
    )
    assert (
        events.index(("fsync", analytics.parent))
        < publication_replace
        < publications_fsync
        < pointer_replace
        < analytics_fsync
    )


def test_diagnostics_are_immutable_and_hash_addressed(tmp_path):
    root = fixture_root(tmp_path)
    first = {"schemaVersion": 1, "diagnosis": {"twr": None}}
    second = {"schemaVersion": 1, "diagnosis": {"twr": "0.10"}}

    first_pointer = diagnostics._publish(root, first)
    second_pointer = diagnostics._publish(root, second)
    output = (
        root
        / "normalized"
        / "analytics-diagnostics"
        / "wealthfolio-performance"
    )

    assert first_pointer["sha256"] != second_pointer["sha256"]
    assert (output / first_pointer["publication"]).is_file()
    current = json.loads((output / "current.json").read_text())
    content = (output / current["publication"]).read_bytes()
    assert hashlib.sha256(content).hexdigest() == current["sha256"]
    assert len(list((output / "publications").glob("*.json"))) == 2


def test_diagnostics_fsync_publication_before_pointer(tmp_path, monkeypatch):
    root = fixture_root(tmp_path)
    output = (
        root
        / "normalized"
        / "analytics-diagnostics"
        / "wealthfolio-performance"
    )
    publications = output / "publications"
    events = []
    real_replace = diagnostics.os.replace

    def replace(source, target):
        events.append(("replace", Path(target)))
        real_replace(source, target)

    monkeypatch.setattr(diagnostics.os, "replace", replace)
    monkeypatch.setattr(
        diagnostics,
        "fsync_directory",
        lambda path: events.append(("fsync", Path(path))),
    )
    diagnostics._publish(root, {"schemaVersion": 1, "diagnosis": {"twr": None}})

    hierarchy_fsyncs = [
        ("fsync", output),
        ("fsync", output.parent),
        ("fsync", output.parent.parent),
    ]
    assert events[:3] == hierarchy_fsyncs
    publication_replace = next(
        index
        for index, event in enumerate(events)
        if event[0] == "replace" and event[1].parent == publications
    )
    publications_fsync = events.index(("fsync", publications))
    pointer_replace = events.index(("replace", output / "current.json"))
    output_fsync = max(
        index for index, event in enumerate(events) if event == ("fsync", output)
    )
    assert (
        events.index(("fsync", output.parent.parent))
        < publication_replace
        < publications_fsync
        < pointer_replace
        < output_fsync
    )


def test_metadata_requires_review_and_citation(tmp_path):
    root = fixture_root(tmp_path)
    path = root / "plans" / "analytics-metadata.json"
    path.parent.mkdir()
    path.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "entries": [
                    {
                        "id": "asset:SYN",
                        "type": "asset",
                        "reviewed": True,
                        "values": {"assetClass": "Synthetic equity"},
                        "citations": [],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(AnalyticsError, match="classifications require review and citations"):
        build(root)


def test_reviewed_metadata_drives_private_portfolio_plan(tmp_path):
    root = fixture_root(tmp_path)
    cited = root / "extracts" / "synthetic" / "statement.txt"
    cited.parent.mkdir(parents=True)
    cited.write_text("Synthetic source document.", encoding="utf-8")
    path = root / "plans" / "analytics-metadata.json"
    path.parent.mkdir()
    path.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "entries": [
                    {
                        "id": "account:invest-1",
                        "type": "account",
                        "reviewed": True,
                        "values": {
                            "owner": "Synthetic Owner",
                            "taxBucket": "tax-deferred",
                            "retirement": True,
                            "investable": True,
                        },
                        "citations": [
                            {
                                "source": "Synthetic plan statement",
                                "sourcePath": "extracts/synthetic/statement.txt",
                            }
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    build(root)
    portfolios = json.loads(
        (current_output(root) / "reporting-portfolios.json").read_text()
    )
    by_name = {item["name"]: item["accountIds"] for item in portfolios["portfolios"]}
    assert by_name["Retirement"] == ["invest-1"]
    assert by_name["Tax-deferred"] == ["invest-1"]
    assert by_name["Owner - Synthetic Owner"] == ["invest-1"]
    review = json.loads(
        (current_output(root) / "metadata-review.json").read_text()
    )
    reviewed = next(item for item in review["items"] if item["id"] == "account:invest-1")
    assert reviewed["citations"][0]["sha256"] == hashlib.sha256(cited.read_bytes()).hexdigest()


def test_derived_valuations_do_not_claim_supported_performance(tmp_path):
    root = fixture_root(tmp_path)
    path = root / "normalized" / "canonical" / "valuations.csv"
    rows = read_csv(path)
    for row in rows:
        if row["entity_id"] == "invest-1" and row["date"] == "2024-02-29":
            row["observed_or_derived"] = "derived"
    write_csv(
        path,
        (
            "date",
            "entity_id",
            "value",
            "currency",
            "source_file",
            "observed_or_derived",
        ),
        rows,
    )
    manifest_path = root / "normalized" / "canonical" / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["dataFiles"]["valuations.csv"] = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    build(root)
    performance = read_csv(
        current_output(root) / "investment-performance.csv"
    )
    assert performance[0]["status"] == "unavailable"
    assert performance[0]["reason"] == "At least two source-backed account valuations are required."


def test_conflicting_same_date_valuations_are_rejected(tmp_path):
    root = fixture_root(tmp_path)
    path = root / "normalized" / "canonical" / "valuations.csv"
    rows = read_csv(path)
    rows.append({**rows[0], "value": "101"})
    write_csv(
        path,
        (
            "date",
            "entity_id",
            "value",
            "currency",
            "source_file",
            "observed_or_derived",
        ),
        rows,
    )
    manifest_path = root / "normalized" / "canonical" / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["dataFiles"]["valuations.csv"] = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest["rowCounts"]["valuations"] = len(rows)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(AnalyticsError, match="conflicting valuations"):
        build(root)


def test_in_kind_external_flow_uses_cited_quantity_and_price(tmp_path):
    root = fixture_root(tmp_path)
    path = root / "normalized" / "canonical" / "transactions.csv"
    rows = read_csv(path)
    contribution = next(row for row in rows if row["source_id"] == "contribution-1")
    contribution["amount"] = "0"
    contribution["quantity"] = "1"
    contribution["price"] = "10"
    write_csv(
        path,
        (
            "date",
            "account_id",
            "amount",
            "description",
            "source_id",
            "source_file",
            "category",
            "transfer_group",
            "symbol",
            "quantity",
            "price",
            "external_flow",
            "excluded",
            "exclusion_reason",
        ),
        rows,
    )
    manifest_path = root / "normalized" / "canonical" / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["dataFiles"]["transactions.csv"] = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    build(root)
    performance = read_csv(
        current_output(root) / "investment-performance.csv"
    )
    assert performance[0]["external_inflows"] == "10.00"
    assert performance[0]["investment_gain"] == "10.00"




def test_values_are_not_extrapolated_beyond_observations():
    points = [(date(2024, 1, 31), Decimal("100"))]
    assert _interpolate(points, date(2024, 1, 31)) == Decimal("100")
    assert _interpolate(points, date(2024, 2, 29)) is None
