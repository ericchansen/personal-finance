import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from openpyxl import Workbook

from importers.normalized.builder import (
    AccountRow,
    BuildError,
    Estate,
    TransactionRow,
    _apply_transfer_review_decisions,
    _transaction_kind,
    build,
    collect,
    plan,
    verify,
    verify_publication,
)
from importers.normalized.cli import main


def write(path: Path, value: str | dict | list) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(value, (dict, list)):
        value = json.dumps(value)
    path.write_text(value, encoding="utf-8")
    return path


def write_vanguard_workbook(path: Path, rows: list[list]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    workbook = Workbook()
    sheet = workbook.active
    sheet.append([
        "Settlement date", "Trade date", "Symbol", "Name", "Type",
        "Account type", "Quantity", "Price", "Commission & fees**", "Amount",
    ])
    for row in rows:
        sheet.append(row)
    workbook.save(path)
    return path


def account(account_id: str, name: str, kind: str = "CASH", **extra) -> dict:
    return {
        "type": "account",
        "id": account_id,
        "institution": extra.pop("institution", "Example Bank"),
        "displayName": name,
        "maskedNumber": None,
        "kind": kind,
        "opened": "2020-01-01",
        "closed": None,
        "excluded": False,
        "reason": None,
        "source": "synthetic fixture",
        "sourcePath": None,
        "notes": "synthetic",
        **extra,
    }


def decision(decision_id: str, kind: str, affects: list[str], resolution: str, **extra) -> dict:
    return {
        "type": "decision",
        "id": decision_id,
        "decisionType": kind,
        "resolution": resolution,
        "evidence": "synthetic evidence",
        "decidedOn": "2024-01-15",
        "affects": affects,
        "source": "synthetic fixture",
        "sourcePath": None,
        "notes": "synthetic",
        **extra,
    }


def make_estate(tmp_path: Path, assertion_balance: str = "100.25") -> Path:
    root = tmp_path / "private"
    facts = [
        account("acct-main", "Example Checking"),
        account(
            "acct-old", "Old Connection",
            reason="Inactive in Wealthfolio; retained for historical ledger truth.",
        ),
        account("acct-vanguard", "Example Retirement", "SECURITIES"),
        account("acct-ledger", "Ledger Hardware Wallet", "CRYPTOCURRENCY"),
        account(
            "acct-corporate", "Corporate Card", "CREDIT_CARD",
            institution="Example Card",
        ),
        {
            "type": "assertion",
            "accountId": "acct-main",
            "date": "2024-01-31",
            "balance": assertion_balance,
            "source": "synthetic statement",
            "sourcePath": None,
            "notes": "synthetic",
        },
        decision(
            "old-handoff", "duplicate-account", ["acct-main", "acct-old"],
            "Treat inactive connection as duplicate on and after handoff 2024-01-15; preserve history.",
        ),
        decision(
            "corporate-exclusion", "account-exclusion", ["acct-corporate"],
            "EXCLUDED from household liabilities.",
        ),
        decision(
            "known-transfer", "transfer", ["acct-main"],
            "Preserve reviewed transfer group.",
            transferGroup="reviewed-pair-1", sourceIds=["monarch:mon-1"],
        ),
        decision(
            "known-category", "category", ["acct-main"],
            "Apply reviewed category.",
            category="Home Costs", sourceIds=["monarch:mon-old"],
        ),
    ]
    write(root / "facts" / "facts.json", facts)

    monarch = (
        "Date,Merchant,Category,Account,Original Statement,Notes,Amount,Tags,Owner,Reviewed,Id\n"
        "2024-01-10,Merchant One,Shopping,Example Checking,,,-12.34,,,,mon-1\n"
        "2024-01-20,Legacy Merchant,Shopping,Old Connection,,,-5.00,,,,mon-old\n"
    )
    write(root / "legacy" / "monarch" / "Transactions_2024.csv", monarch)
    write(root / "normalized" / "monarch-account-map.json", {
        "Example Checking": "acct-main",
        "Old Connection": "acct-old",
    })

    ofx = """OFXHEADER:100
<OFX><BANKACCTFROM><ACCTID>SYNTHETIC</BANKACCTFROM>
<STMTTRN><TRNTYPE>DEBIT<DTPOSTED>20240110<TRNAMT>-12.34
<FITID>ofx-1<NAME>Merchant One</STMTTRN></OFX>"""
    extract_path = write(root / "extracts" / "example" / "activity.qfx", ofx)
    write(root / "extracts" / "mapping.json", {
        "files": [{"file": str(extract_path.relative_to(root / "extracts")), "account": "Example Checking"}]
    })

    snapshot = {
        "accounts": [
            {
                "id": "source-main",
                "name": "Example Checking",
                "org": {"name": "Example Bank"},
                "currency": "USD",
                "balance": "100.25",
                "balance-date": 1706659200,
                "transactions": [{
                    "id": "simple-1", "posted": 1706745600, "amount": "2.50",
                    "description": "Synthetic deposit", "pending": False,
                }],
            },
            {
                "id": "source-corporate",
                "name": "Corporate Card",
                "org": {"name": "Example Card"},
                "currency": "USD",
                "balance": "0",
                "balance-date": 1706659200,
                "transactions": [{
                    "id": "corporate-1", "posted": 1706745600, "amount": "-9.99",
                    "description": "Synthetic corporate charge", "pending": False,
                }],
            },
        ]
    }
    write(root / "raw" / "simplefin" / "2024-02-01" / "simplefin-000001.json", snapshot)
    write(root / "simplefin" / "account-map.json", {
        "version": 1,
        "accounts": {
            "source-main": {
                "action": "import", "wealthfolioAccountId": "app-only-id",
                "assertionAccountId": "acct-main",
            },
            "source-corporate": {
                "action": "exclude",
                "decision": "employer-corporate-card",
            },
        },
    })

    write(root / "raw" / "ledger-live-normalized.json", {
        "schema_version": 1,
        "source": "synthetic",
        "unit": "btc",
        "satoshis_per_btc": 100000000,
        "accounts": [{
            "account_ref": "ledger-live:synthetic",
            "name": "Synthetic Bitcoin",
            "currency": "bitcoin",
            "created_at": "2023-01-01T00:00:00Z",
            "current_balance_sat": 125000000,
            "operations": [{
                "txid": "abc", "occurred_at": "2024-01-05T00:00:00Z",
                "direction": "inflow", "value_sat": 125000000, "fee_sat": 0,
                "block_height": 1, "failed": False,
            }],
        }],
    })

    write(root / "extracts" / "vanguard" / "mapping.json", {
        "asOf": "2024-01-31",
        "accounts": {"SYN-001": {"name": "Example Retirement", "create": False}},
        "excludedAccounts": {},
        "cashSymbols": ["CASH"],
    })
    write(
        root / "extracts" / "vanguard" / "holdings.csv",
        "Account Number,Investment Name,Symbol,Shares,Share Price,Total Value\n"
        'SYN-001,Example Fund,SYN,2.5,10.00,25.00\n'
        'SYN-001,Settlement Fund,VMFXX,1.25,1.00,1.25\n\n'
        "Account Number,Trade Date,Settlement Date,Transaction Type,Transaction Description,"
        "Investment Name,Symbol,Shares,Share Price,Principal Amount,Commission Fees,Net Amount\n"
        "SYN-001,01/15/2024,01/16/2024,Dividend,Synthetic dividend,Example Fund,SYN,"
        "0,0,0,0,1.25\n",
    )
    write_vanguard_workbook(
        root / "extracts" / "vanguard" / "customActivityReport SYN-001.xlsx",
        [
            ["01/03/2024", "01/02/2024", None, "Cash", "Contribution", "IRA", None, None, "Free", "25"],
            ["01/04/2024", "01/03/2024", "SYN", "Example Fund", "Buy", "IRA", "2.5", "10", "Free", "-25"],
            ["01/15/2024", "01/15/2024", "VMFXX", "Settlement Fund", "Dividend", "IRA", None, None, "Free", "1.25"],
        ],
    )
    write(root / "plans" / "vanguard-in-kind-prices.json", {"events": []})
    return root


def input_hashes(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*")
        if path.is_file()
        and "canonical" not in path.parts
        and ".lineage-state" not in path.parts
    }


def add_transfer_pair(root: Path, *, second_inflow: bool = False) -> None:
    facts_path = root / "facts" / "facts.json"
    facts = json.loads(facts_path.read_text(encoding="utf-8"))
    facts.append(account("acct-savings", "Example Savings"))
    if second_inflow:
        facts.append(account("acct-reserve", "Example Reserve"))
    write(facts_path, facts)

    monarch_path = root / "legacy" / "monarch" / "Transactions_2024.csv"
    with monarch_path.open("a", encoding="utf-8") as output:
        output.write(
            "2024-01-11,Synthetic Move Out,Transfer,Example Checking,,,-10.00,,,,pair-out\n"
            "2024-01-13,Synthetic Move In,Transfer,Example Savings,,,10.00,,,,pair-in\n"
        )
        if second_inflow:
            output.write(
                "2024-01-12,Synthetic Other In,Transfer,Example Reserve,,,10.00,,,,pair-other\n"
            )
    mapping_path = root / "normalized" / "monarch-account-map.json"
    mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
    mapping["Example Savings"] = "acct-savings"
    if second_inflow:
        mapping["Example Reserve"] = "acct-reserve"
    write(mapping_path, mapping)


def add_decision(root: Path, value: dict) -> None:
    path = root / "facts" / "facts.json"
    facts = json.loads(path.read_text(encoding="utf-8"))
    facts.append(value)
    write(path, facts)


def test_deterministic_build_and_verify(tmp_path):
    root = make_estate(tmp_path)
    first = build(root, now=datetime(2024, 2, 1, tzinfo=timezone.utc))
    bytes_before = {
        name: (root / "normalized" / "canonical" / name).read_bytes()
        for name in (
            "accounts.csv",
            "transactions.csv",
            "positions.csv",
            "valuations.csv",
            "transaction-observations.json",
            "transaction-lineage.json",
        )
    }
    second = build(root, now=datetime(2025, 2, 1, tzinfo=timezone.utc))
    bytes_after = {
        name: (root / "normalized" / "canonical" / name).read_bytes()
        for name in bytes_before
    }
    assert bytes_before == bytes_after
    assert first["schemaVersion"] == 5
    account_columns = bytes_before["accounts.csv"].splitlines()[0].decode().split(",")
    transaction_columns = bytes_before["transactions.csv"].splitlines()[0].decode().split(",")
    assert "tracking_mode" in account_columns
    assert {
        "transaction_kind",
        "category_id",
        "payee_normalized",
        "assignment_source",
        "assignment_rule_id",
        "assignment_confidence",
        "split_group",
    } <= set(transaction_columns)
    assert first["buildTimestamp"] != second["buildTimestamp"]
    assert verify(root)["verified"]


def test_full_verify_recomputes_manifest_source_provenance(tmp_path):
    root = make_estate(tmp_path)
    build(root, now=datetime(2024, 2, 1, tzinfo=timezone.utc))
    manifest_path = root / "normalized" / "canonical" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["sourceFiles"] = manifest["sourceFiles"][1:]
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )

    assert verify_publication(root)["verified"] is True
    with pytest.raises(BuildError, match="recomputed provenance"):
        verify(root)


def test_canonical_build_rejects_non_dormant_exclusion(tmp_path):
    root = make_estate(tmp_path)
    mapping_path = root / "simplefin" / "account-map.json"
    mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
    mapping["accounts"]["source-corporate"]["decision"] = (
        "dormant-zero-balance-account"
    )
    write(mapping_path, mapping)

    with pytest.raises(BuildError, match="excluded-account-not-dormant"):
        collect(root)


def test_canonical_build_rejects_mismatched_duplicate_exclusion(tmp_path):
    root = make_estate(tmp_path)
    mapping_path = root / "simplefin" / "account-map.json"
    mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
    mapping["accounts"]["source-corporate"].update({
        "decision": "aggregator-account-summary",
        "duplicateOfSourceAccountId": "source-main",
    })
    write(mapping_path, mapping)

    with pytest.raises(BuildError, match="duplicate-summary-mismatch"):
        collect(root)


def test_atomic_failure_preserves_previous_directory(tmp_path):
    root = make_estate(tmp_path)
    build(root)
    canonical = root / "normalized" / "canonical"
    before = {path.name: path.read_bytes() for path in canonical.iterdir()}

    def fail(_staging):
        raise RuntimeError("synthetic failure")

    with pytest.raises(RuntimeError, match="synthetic failure"):
        build(root, before_swap=fail)
    assert {path.name: path.read_bytes() for path in canonical.iterdir()} == before


def test_canonical_build_rejects_output_inside_public_checkout():
    with pytest.raises(BuildError, match="canonical-private-output-invalid"):
        build(Path(__file__).parents[1])


def test_canonical_build_holds_lineage_decision_lock(tmp_path):
    root = make_estate(tmp_path)
    lock_root = root / "audit" / ".lineage-state"
    from importers.lineage_review import workflow

    with workflow._decision_import_lock(lock_root):
        with pytest.raises(
            BuildError, match="lineage-review-decision-import-in-progress"
        ):
            build(root)


def test_lineage_hash_binds_exact_published_transactions(tmp_path):
    root = make_estate(tmp_path)
    build(root)
    canonical = root / "normalized" / "canonical"
    transactions = canonical / "transactions.csv"
    transactions.write_text(
        transactions.read_text(encoding="utf-8").replace(
            "extract:stable:ofx-1",
            "extract:stable:tampered",
            1,
        ),
        encoding="utf-8",
        newline="\n",
    )
    manifest_path = canonical / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["dataFiles"]["transactions.csv"] = hashlib.sha256(
        transactions.read_bytes()
    ).hexdigest()
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )

    with pytest.raises(
        BuildError, match="canonical-lineage-publication-invalid"
    ):
        verify_publication(root)


def test_unproved_cross_source_candidate_preserves_each_source_semantics(tmp_path):
    root = make_estate(tmp_path)
    estate = collect(root)
    matching = [
        row for row in estate.transactions
        if row.account_id == "acct-main" and row.date == "2024-01-10"
        and Decimal(row.amount) == Decimal("-12.34")
    ]
    assert len(matching) == 2
    extracted = next(row for row in matching if row.source_id.startswith("extract:"))
    legacy = next(row for row in matching if row.source_id.startswith("monarch:"))
    assert extracted.source_file == "extracts/example/activity.qfx"
    assert extracted.transaction_kind == "expense"
    assert not extracted.transfer_group
    assert legacy.category == "Shopping"
    assert legacy.transaction_kind == "internal_transfer"
    assert legacy.payee_normalized == "merchant one"
    assert legacy.assignment_source == "source"
    assert legacy.transfer_group == "reviewed-pair-1"
    assert estate.lineage_review["identityPolicy"][
        "safeAutomaticResolutions"
    ] == 0
    assert estate.transaction_observations["observationCount"] >= len(
        estate.transactions
    )
    observation_source_ids = {
        item["transaction"]["source_id"]
        for item in estate.transaction_observations["observations"]
    }
    assert {"extract:stable:ofx-1", "monarch:mon-1"} <= observation_source_ids
    assert estate.transaction_lineage["decisionProjections"] == []
    assert any(Decimal(row.amount) == Decimal("1.25") for row in estate.transactions)


def test_schema_v5_lineage_binds_source_authority_without_declared_coverage(tmp_path):
    root = make_estate(tmp_path)
    estate = collect(root)

    identity = estate.lineage_review["identityPolicy"]
    authority = identity["sourceAuthority"]
    assert authority["declared"] is False
    assert authority["policyVersion"] == "canonical-source-authority-v3"
    assert authority["intervalCount"] == 0
    assert authority["provenIntervalCount"] == 0
    assert authority["reconciledIntervalCount"] == 0
    assert authority["authoritativeIntervalCount"] == 0
    assert authority["intervals"] == []
    assert authority["earliestEffectiveFrom"] is None
    assert authority["latestEffectiveThrough"] is None
    assert identity["sourceSuppressedClaims"] == 0
    assert identity["authorityCoveredClaims"] == 0
    assert identity["authorityAmbiguousGroups"] == 0
    assert set(identity["residualByClass"]) == {
        "distinct",
        "source-suppressed",
        "transfer",
        "correction",
        "reversal",
        "unresolved",
    }
    assert all(value >= 0 for value in identity["residualByClass"].values())

    for decision in estate.transaction_lineage["decisionProjections"]:
        assert decision["residualClassification"] in identity["residualByClass"]
        assert decision["sourceAuthorityPolicyHash"] is None


def test_vanguard_full_history_precedes_combined_csv_overlap(tmp_path):
    root = make_estate(tmp_path)
    estate = collect(root)
    vanguard_rows = [
        row for row in estate.transactions
        if row.account_id == "acct-vanguard"
    ]

    assert estate.source_stats["vanguardMappedWorkbookRows"] == 3
    assert estate.source_stats["vanguardCanonicalActivities"] == 3
    assert len(vanguard_rows) == 3
    assert all(row.source_id.startswith("vanguard-history:") for row in vanguard_rows)
    assert not any(row.source_id.startswith("vanguard:") for row in vanguard_rows)
    assert any("combined-csv-transactions-ignored" in warning for warning in estate.warnings)
    positions = {row.symbol: row for row in estate.positions if row.account_id == "acct-vanguard"}
    assert positions["SYN"].basis_per_unit == "10.0"
    assert positions["VMFXX"].basis_per_unit == "1"


def test_dated_account_backed_crypto_fact_emits_quantity_only_position(tmp_path):
    root = make_estate(tmp_path)
    facts_path = root / "facts" / "facts.json"
    facts = json.loads(facts_path.read_text())
    facts.append({
        "type": "crypto",
        "label": "Synthetic private wallet snapshot",
        "chain": "bitcoin",
        "unit": "BTC",
        "quantity": "0.37500000",
        "accountId": "acct-ledger",
        "asOf": "2026-08-28",
        "source": "synthetic wallet screenshot",
        "sourcePath": "raw/synthetic-wallet-screenshot.png",
        "notes": "Synthetic fixture only.",
    })
    write(facts_path, facts)

    estate = collect(root)
    position = next(
        row
        for row in estate.positions
        if row.account_id == "acct-ledger" and row.as_of == "2026-08-28"
    )

    assert position.symbol == "BTC"
    assert position.quantity == "0.37500000"
    assert position.price == ""
    assert position.market_value == ""
    assert position.basis_per_unit == ""
    assert position.source_file == "facts/facts.json"
    assert not any(
        row.source_file == "facts/facts.json"
        for row in estate.transactions
    )


def test_legacy_address_backed_crypto_fact_does_not_create_dated_position(tmp_path):
    root = make_estate(tmp_path)
    facts_path = root / "facts" / "facts.json"
    facts = json.loads(facts_path.read_text())
    facts.append({
        "type": "crypto",
        "label": "Synthetic legacy Bitcoin wallet",
        "chain": "bitcoin",
        "unit": "BTC",
        "quantity": "0.01000000",
        "publicAddress": "bc1qsynthetic000000000000000000000000000000",
        "xpub": None,
        "derivationPath": "m/84'/0'/0'",
        "source": "synthetic wallet export",
        "sourcePath": None,
        "notes": "Synthetic fixture only.",
    })
    write(facts_path, facts)

    estate = collect(root)

    assert not any(
        row.source_file == "facts/facts.json"
        for row in estate.positions
    )

@pytest.mark.parametrize(("column", "replacement", "message"), [
    ("Shares", "2.6", "share reconciliation failed"),
    ("VMFXX", "2.00", "cash reconciliation failed"),
])
def test_vanguard_exact_position_reconciliation_blocks(
    tmp_path, column, replacement, message
):
    root = make_estate(tmp_path)
    csv_path = root / "extracts" / "vanguard" / "holdings.csv"
    text = csv_path.read_text()
    if column == "Shares":
        text = text.replace("SYN,2.5,10.00", f"SYN,{replacement},10.00")
    else:
        text = text.replace("VMFXX,1.25,1.00", f"VMFXX,{replacement},1.00")
    csv_path.write_text(text)

    with pytest.raises(BuildError, match=message):
        collect(root)


def test_vanguard_closed_workbook_requires_and_honors_durable_exclusion(tmp_path):
    root = make_estate(tmp_path)
    write_vanguard_workbook(
        root / "extracts" / "vanguard" / "customActivityReport CLOSED-9.xlsx",
        [["01/02/2020", "01/02/2020", None, "Cash", "Distribution", "IRA", None, None, "Free", "-1"]],
    )
    mapping_path = root / "extracts" / "vanguard" / "mapping.json"
    mapping = json.loads(mapping_path.read_text())
    with pytest.raises(BuildError, match="excludedAccounts decision"):
        collect(root)

    mapping["excludedAccounts"]["CLOSED-9"] = {
        "decision": "synthetic-closed-account",
        "reason": "Closed account is outside the live-account canonical history.",
    }
    write(mapping_path, mapping)
    estate = collect(root)
    assert estate.source_stats["vanguardExcludedWorkbookRows"] == 1
    assert not any("CLOSED-9" in row.source_id for row in estate.transactions)


def test_vanguard_external_in_kind_transfer_is_zero_cash_and_marked(tmp_path):
    root = make_estate(tmp_path)
    workbook = root / "extracts" / "vanguard" / "customActivityReport SYN-001.xlsx"
    write_vanguard_workbook(workbook, [
        ["01/03/2024", "01/02/2024", None, "Cash", "Contribution", "IRA", None, None, "Free", "25"],
        ["01/04/2024", "01/03/2024", "SYN", "Example Fund", "Buy", "IRA", "2.5", "10", "Free", "-25"],
        ["01/10/2024", "01/10/2024", "SYN", "Example Fund", "Transfer (incoming)", "IRA", "1", None, "Free", "0"],
        ["01/15/2024", "01/15/2024", "VMFXX", "Settlement Fund", "Dividend", "IRA", None, None, "Free", "1.25"],
    ])
    csv_path = root / "extracts" / "vanguard" / "holdings.csv"
    csv_path.write_text(csv_path.read_text().replace("SYN,2.5,10.00", "SYN,3.5,10.00"))
    write(root / "plans" / "vanguard-in-kind-prices.json", {"events": [{
        "account": "SYN-001", "date": "2024-01-10", "symbol": "SYN",
        "sourceType": "Transfer (incoming)", "shareDelta": "1",
        "activityType": "BUY", "unitPrice": "10", "confidence": "HIGH",
    }]})

    transfer = next(row for row in collect(root).transactions if row.category == "TRANSFER_IN")
    assert transfer.amount == "0"
    assert transfer.quantity == "1.0"
    assert transfer.external_flow is True
    assert transfer.transaction_kind == "saving"
    assert transfer.category_id == ""


def test_vanguard_missing_in_kind_resolution_blocks(tmp_path):
    root = make_estate(tmp_path)
    workbook = root / "extracts" / "vanguard" / "customActivityReport SYN-001.xlsx"
    write_vanguard_workbook(workbook, [
        ["01/10/2024", "01/10/2024", "SYN", "Example Fund", "Transfer (incoming)", "IRA", "2.5", None, "Free", "0"],
        ["01/15/2024", "01/15/2024", "VMFXX", "Settlement Fund", "Dividend", "IRA", None, None, "Free", "1.25"],
    ])
    with pytest.raises(BuildError, match="unresolved Vanguard history"):
        collect(root)


def test_vanguard_internal_recharacterization_preserves_transfer_group(tmp_path):
    root = make_estate(tmp_path)
    facts_path = root / "facts" / "facts.json"
    facts = json.loads(facts_path.read_text())
    facts.append(account("acct-vanguard-2", "Example Roth", "SECURITIES"))
    write(facts_path, facts)
    mapping_path = root / "extracts" / "vanguard" / "mapping.json"
    mapping = json.loads(mapping_path.read_text())
    mapping["accounts"]["SYN-002"] = {"name": "Example Roth", "create": False}
    write(mapping_path, mapping)
    first = root / "extracts" / "vanguard" / "customActivityReport SYN-001.xlsx"
    write_vanguard_workbook(first, [
        ["01/03/2024", "01/02/2024", None, "Cash", "Contribution", "IRA", None, None, "Free", "25"],
        ["01/04/2024", "01/03/2024", "SYN", "Example Fund", "Buy", "IRA", "2.5", "10", "Free", "-25"],
        ["01/10/2024", "01/10/2024", "SYN", "Example Fund", "Recharacterization (outgoing)", "IRA", "-1", None, "Free", "0"],
        ["01/15/2024", "01/15/2024", "VMFXX", "Settlement Fund", "Dividend", "IRA", None, None, "Free", "1.25"],
    ])
    write_vanguard_workbook(
        root / "extracts" / "vanguard" / "customActivityReport SYN-002.xlsx",
        [["01/10/2024", "01/10/2024", "SYN", "Example Fund", "Recharacterization (incoming)", "IRA", "1", None, "Free", "0"]],
    )
    csv_path = root / "extracts" / "vanguard" / "holdings.csv"
    text = csv_path.read_text().replace("SYN,2.5,10.00", "SYN,1.5,10.00")
    text = text.replace(
        "\n\nAccount Number,Trade Date",
        "\nSYN-002,Example Fund,SYN,1,10.00,10.00\n\nAccount Number,Trade Date",
    )
    csv_path.write_text(text)
    pair = "2024-01-10-synthetic-recharacterization"
    write(root / "plans" / "vanguard-in-kind-prices.json", {"events": [
        {
            "account": "SYN-001", "date": "2024-01-10", "symbol": "SYN",
            "sourceType": "Recharacterization (outgoing)", "shareDelta": "-1",
            "activityType": "TRANSFER_OUT", "unitPrice": "10", "pairId": pair,
            "confidence": "HIGH",
        },
        {
            "account": "SYN-002", "date": "2024-01-10", "symbol": "SYN",
            "sourceType": "Recharacterization (incoming)", "shareDelta": "1",
            "activityType": "TRANSFER_IN", "unitPrice": "10", "pairId": pair,
            "confidence": "HIGH",
        },
    ]})

    grouped = [row for row in collect(root).transactions if row.transfer_group == pair]
    assert {row.category for row in grouped} == {"TRANSFER_IN", "TRANSFER_OUT"}
    assert {row.amount for row in grouped} == {"0"}
    assert not any(row.external_flow for row in grouped)
    assert {row.transaction_kind for row in grouped} == {"internal_transfer"}


def test_repeated_same_source_rows_remain_ambiguous(tmp_path):
    root = make_estate(tmp_path)
    monarch = root / "legacy" / "monarch" / "Transactions_2024.csv"
    lines = monarch.read_text(encoding="utf-8").splitlines()
    # A second distinct source id with the same content could be a legitimate
    # repeated purchase. A cross-source match must not make us delete it.
    lines.insert(2, lines[1].rsplit(",", 1)[0] + ",mon-2")
    monarch.write_text("\n".join(lines) + "\n", encoding="utf-8")
    estate = collect(root)
    matching = [
        row
        for row in estate.transactions
        if row.account_id == "acct-main"
        and row.date == "2024-01-10"
        and Decimal(row.amount) == Decimal("-12.34")
    ]
    assert len(matching) == 3
    assert any(
        "unresolved-cross-source-candidate" in warning
        for warning in estate.warnings
    )


def test_assertion_failure_blocks_build(tmp_path):
    root = make_estate(tmp_path, assertion_balance="999.00")
    with pytest.raises(BuildError, match="assertion failed"):
        plan(root)


def test_loans_are_canonical_liability_accounts(tmp_path):
    root = make_estate(tmp_path)
    facts_path = root / "facts" / "facts.json"
    facts = json.loads(facts_path.read_text(encoding="utf-8"))
    facts.extend(
        [
            {
                "type": "property",
                "name": "Example home",
                "address": "1 Example Ave",
                "purchaseDate": "2024-01-01",
                "purchasePrice": "200000",
                "saleDate": None,
                "salePrice": None,
                "netProceeds": None,
                "appraisals": [],
                "source": "synthetic",
                "sourcePath": None,
                "notes": "synthetic",
            },
            {
                "type": "loan",
                "name": "Example mortgage",
                "principal": "150000",
                "annualRate": "0.05",
                "termMonths": 360,
                "originationDate": "2024-01-01",
                "firstPayment": "2024-02-01",
                "lender": "Example Lender",
                "linkedTo": "Example home",
                "payoffAmount": None,
                "payoffDate": None,
                "pmi": False,
                "source": "synthetic",
                "sourcePath": None,
                "notes": "synthetic",
            },
        ]
    )
    write(facts_path, facts)
    estate = collect(root)
    mortgage = next(
        account
        for account in estate.accounts
        if account.account_id == "loan:Example mortgage"
    )
    assert mortgage.kind == "liability"
    assert mortgage.institution == "Example Lender"


def test_exclusions_apply_from_facts_and_handoff_decision(tmp_path):
    root = make_estate(tmp_path)
    estate = collect(root)
    corporate = next(row for row in estate.transactions if "corporate-1" in row.source_id)
    handoff = next(row for row in estate.transactions if row.source_id == "monarch:mon-old")
    assert corporate.excluded and corporate.exclusion_reason == (
        "account-exclusion decision: corporate-exclusion"
    )
    assert handoff.excluded and "old-handoff" in handoff.exclusion_reason


def test_reviewed_category_decision_overrides_source_category(tmp_path):
    estate = collect(make_estate(tmp_path))
    row = next(
        item for item in estate.transactions if item.source_id == "monarch:mon-old"
    )
    assert row.category == "Home Costs"
    assert row.category_id == ""
    assert row.assignment_source == "manual"
    assert row.assignment_rule_id == "known-category"
    assert row.assignment_confidence == "1.00"


def test_legacy_transfer_category_remains_structural_transfer(tmp_path):
    root = make_estate(tmp_path)
    facts_path = root / "facts" / "facts.json"
    facts = [
        fact
        for fact in json.loads(facts_path.read_text())
        if fact.get("id") != "known-transfer"
    ]
    write(facts_path, facts)
    monarch = root / "legacy" / "monarch" / "Transactions_2024.csv"
    monarch.write_text(
        monarch.read_text().replace(
            "Merchant One,Shopping", "Merchant One,Transfer to Savings"
        ),
        encoding="utf-8",
    )

    row = next(
        item
        for item in collect(root).transactions
        if item.account_id == "acct-main"
        and item.date == "2024-01-10"
        and Decimal(item.amount) == Decimal("-12.34")
        and item.source_id.startswith("monarch:")
    )

    assert row.category == "Transfer to Savings"
    assert row.transaction_kind == "internal_transfer"
    assert row.category_id == ""


@pytest.mark.parametrize(
    "category",
    [
        "Transfer",
        "Transfers",
        "Transfer In",
        "Transfer Out",
        "Transfer to Savings",
        "Transfer From Checking",
        "Transfer between accounts",
        "Internal Transfer",
        "Balance Transfer",
        "Wire Transfer",
        "Credit Card Payment",
    ],
)
def test_transfer_shaped_categories_stay_structural_transfers(category):
    row = TransactionRow(
        "2024-01-10", "acct-main", "-12.34", "SYNTHETIC PAYEE", "synthetic:1", "fixture",
        category=category,
    )
    assert _transaction_kind(row, "CASH") == "internal_transfer"


@pytest.mark.parametrize(
    "category",
    [
        "Transfer Fee",
        "Wire Transfer Fee",
        "Transfer Fees",
        "Bank Transfer Charge",
        "Transferable Benefits",
        "Balance Transfer Interest",
    ],
)
def test_transfer_shaped_fee_categories_stay_ordinary_expenses(category):
    """A substring match on "transfer" swallowed fees, which are consumption."""
    row = TransactionRow(
        "2024-01-10", "acct-main", "-12.34", "SYNTHETIC PAYEE", "synthetic:2", "fixture",
        category=category,
    )
    assert _transaction_kind(row, "CASH") == "expense"


def test_transfer_fee_on_a_card_is_not_a_card_payment():
    row = TransactionRow(
        "2024-01-10", "acct-card", "-12.34", "SYNTHETIC PAYEE", "synthetic:3", "fixture",
        category="Transfer Fee",
    )
    assert _transaction_kind(row, "CREDIT_CARD") == "expense"


def test_cash_side_loan_payment_is_structural_financing(tmp_path):
    root = make_estate(tmp_path)
    monarch = root / "legacy" / "monarch" / "Transactions_2024.csv"
    lines = monarch.read_text(encoding="utf-8").splitlines()
    lines.append(
        "2024-01-25,Synthetic Lender,Loan Payment,Example Checking,,,-20.00,,,,loan-1"
    )
    monarch.write_text("\n".join(lines) + "\n", encoding="utf-8")

    row = next(
        item for item in collect(root).transactions
        if item.source_id == "monarch:loan-1"
    )

    assert row.transaction_kind == "loan_payment"
    assert row.category_id == ""


def test_manifest_source_hashes_and_raw_inputs_are_not_mutated(tmp_path):
    root = make_estate(tmp_path)
    before = input_hashes(root)
    manifest = build(root)
    after = input_hashes(root)
    assert before == after
    mapped = {row["path"]: row["sha256"] for row in manifest["sourceFiles"]}
    source = root / "raw" / "ledger-live-normalized.json"
    assert mapped["raw/ledger-live-normalized.json"] == hashlib.sha256(source.read_bytes()).hexdigest()


def test_cli_plan_does_not_write_and_build_verify_succeed(tmp_path, capsys):
    root = make_estate(tmp_path)
    assert main(["plan", "--data-dir", str(root)]) == 0
    assert not (root / "normalized" / "canonical").exists()
    assert main(["build", "--data-dir", str(root)]) == 0
    assert main(["verify", "--data-dir", str(root)]) == 0
    assert '"verified": true' in capsys.readouterr().out


def test_transfer_rejection_suppresses_same_evidence_and_resurfaces_when_changed(tmp_path):
    root = make_estate(tmp_path)
    add_transfer_pair(root)
    first = collect(root)
    assert len(first.transfer_review["proposals"]) == 1
    candidate = first.transfer_review["proposals"][0]

    add_decision(
        root,
        decision(
            "reject-pair",
            "transfer-rejected",
            ["acct-main", "acct-savings"],
            "REJECTED after private review.",
            candidateId=candidate["candidateId"],
            evidenceHash=candidate["evidenceHash"],
        ),
    )
    rejected = collect(root)
    assert rejected.transfer_review["proposals"] == []
    assert len(rejected.transfer_review["rejected"]) == 1
    assert rejected.transfer_review["rejected"][0]["candidateId"] == candidate["candidateId"]
    assert rejected.transfer_review["rejected"][0]["evidenceHash"] == candidate["evidenceHash"]
    assert rejected.transfer_review["rejected"][0]["decisionId"] == "reject-pair"
    assert not any(row.transfer_group for row in rejected.transactions if "pair-" in row.source_id)

    monarch_path = root / "legacy" / "monarch" / "Transactions_2024.csv"
    monarch_path.write_text(
        monarch_path.read_text(encoding="utf-8").replace(
            "2024-01-13,Synthetic Move In", "2024-01-14,Synthetic Move In"
        ),
        encoding="utf-8",
    )
    changed = collect(root)
    assert len(changed.transfer_review["proposals"]) == 1
    assert changed.transfer_review["proposals"][0]["status"] == "evidence-changed"
    assert changed.transfer_review["proposals"][0]["candidateId"] == candidate["candidateId"]
    assert changed.transfer_review["proposals"][0]["evidenceHash"] != candidate["evidenceHash"]


def test_reviewed_transfer_confirmation_is_stable_and_idempotent(tmp_path):
    root = make_estate(tmp_path)
    add_transfer_pair(root)
    candidate = collect(root).transfer_review["proposals"][0]
    add_decision(
        root,
        decision(
            "confirm-pair",
            "transfer-confirmed",
            ["acct-main", "acct-savings"],
            "CONFIRMED after exact private review.",
            candidateId=candidate["candidateId"],
            evidenceHash=candidate["evidenceHash"],
        ),
    )

    first = collect(root)
    second = collect(root)
    pair = [row for row in first.transactions if row.source_id in {
        "monarch:pair-out", "monarch:pair-in"
    }]
    assert len(pair) == 2
    assert len({row.transfer_group for row in pair}) == 1
    assert {row.transaction_kind for row in pair} == {"internal_transfer"}
    assert first.transfer_review == second.transfer_review
    assert pair == [
        row for row in second.transactions if row.source_id in {
            "monarch:pair-out", "monarch:pair-in"
        }
    ]


def test_confirmed_ambiguous_pair_suppresses_competing_candidates(tmp_path):
    root = make_estate(tmp_path)
    add_transfer_pair(root, second_inflow=True)
    candidate = next(
        row
        for row in collect(root).transfer_review["proposals"]
        if row["inflow"]["sourceId"] == "monarch:pair-in"
    )
    add_decision(
        root,
        decision(
            "confirm-ambiguous-pair",
            "transfer-confirmed",
            ["acct-main", "acct-savings"],
            "CONFIRMED exact pair after resolving ambiguity.",
            candidateId=candidate["candidateId"],
            evidenceHash=candidate["evidenceHash"],
        ),
    )

    estate = collect(root)
    assert estate.transfer_review["proposals"] == []
    assert len(estate.transfer_review["confirmed"]) == 1
    assert next(
        row for row in estate.transactions if row.source_id == "monarch:pair-other"
    ).transfer_group == ""


def test_ambiguous_exact_amount_candidates_are_never_auto_confirmed(tmp_path):
    root = make_estate(tmp_path)
    add_transfer_pair(root, second_inflow=True)
    estate = collect(root)
    proposals = estate.transfer_review["proposals"]
    assert len(proposals) == 2
    assert all(row["ambiguous"] for row in proposals)
    assert not any(
        row.transfer_group
        for row in estate.transactions
        if row.source_id in {
            "monarch:pair-out", "monarch:pair-in", "monarch:pair-other"
        }
    )


def test_transfer_candidates_require_owned_distinct_accounts_currency_amount_and_window():
    accounts = [
        AccountRow("cash", "Synthetic", "Cash", "CASH", "USD"),
        AccountRow("save", "Synthetic", "Save", "CASH", "USD"),
        AccountRow("euro", "Synthetic", "Euro", "CASH", "EUR"),
        AccountRow("excluded", "Synthetic", "Old", "CASH", "USD", excluded=True),
        AccountRow(
            "closed", "Synthetic", "Closed", "CASH", "USD", closed="2026-08-09"
        ),
    ]

    def row(source_id, account_id, amount, on="2026-08-10"):
        return TransactionRow(
            on, account_id, amount, "Synthetic transfer evidence", source_id, "fixture"
        )

    estate = Estate(
        accounts,
        [
            row("out", "cash", "-10.00"),
            row("compatible", "save", "10.00", "2026-08-15"),
            row("same-account", "cash", "10.00"),
            row("wrong-amount", "save", "9.99"),
            row("wrong-currency", "euro", "10.00"),
            row("outside-window", "save", "10.00", "2026-08-16"),
            row("not-owned", "excluded", "10.00"),
            row("after-ownership", "closed", "10.00"),
            row("gap:reconciliation", "save", "10.00"),
        ],
        [],
        [],
        set(),
        [],
        {},
    )
    _apply_transfer_review_decisions(estate, [])

    assert [
        (
            proposal["outflow"]["sourceId"],
            proposal["inflow"]["sourceId"],
            proposal["dayDistance"],
        )
        for proposal in estate.transfer_review["proposals"]
    ] == [("out", "compatible", 5)]


def test_category_split_uses_exact_decimal_residual_and_stable_group(tmp_path):
    root = make_estate(tmp_path)
    monarch_path = root / "legacy" / "monarch" / "Transactions_2024.csv"
    with monarch_path.open("a", encoding="utf-8") as output:
        output.write(
            "2024-01-09,Synthetic Split Purchase,Shopping,Example Checking,,,"
            "-10.005,,,,split-parent\n"
        )
    add_decision(
        root,
        decision(
            "split-purchase",
            "category-split",
            ["acct-main"],
            "Reviewed exact category allocation.",
            sourceId="monarch:split-parent",
            splits=[
                {"amount": "-3.333", "category": "Synthetic One", "categoryId": "one"},
                {"residual": True, "category": "Synthetic Two", "categoryId": "two"},
            ],
        ),
    )

    first = collect(root)
    second = collect(root)
    children = [
        row for row in first.transactions
        if row.split_group and row.assignment_rule_id == "split-purchase"
    ]
    assert [Decimal(row.amount) for row in children] == [
        Decimal("-3.333"), Decimal("-6.672")
    ]
    assert sum((Decimal(row.amount) for row in children), Decimal()) == Decimal("-10.005")
    assert len({row.split_group for row in children}) == 1
    assert children == [
        row for row in second.transactions
        if row.split_group and row.assignment_rule_id == "split-purchase"
    ]
    build(root)
    assert verify(root)["verified"] is True


def test_category_split_rejects_transfer_semantics(tmp_path):
    root = make_estate(tmp_path)
    monarch_path = root / "legacy" / "monarch" / "Transactions_2024.csv"
    with monarch_path.open("a", encoding="utf-8") as output:
        output.write(
            "2024-01-08,Synthetic Transfer,Transfer,Example Checking,,,"
            "-12.34,,,,split-transfer-parent\n"
        )
    add_decision(
        root,
        decision(
            "mark-transfer",
            "transfer",
            ["acct-main"],
            "Reviewed synthetic transfer.",
            transferGroup="synthetic-transfer-group",
            sourceIds=["monarch:split-transfer-parent"],
        ),
    )
    add_decision(
        root,
        decision(
            "split-transfer",
            "category-split",
            ["acct-main"],
            "Invalid synthetic split.",
            sourceId="monarch:split-transfer-parent",
            splits=[
                {"amount": "-6.17", "category": "Synthetic One"},
                {"residual": True, "category": "Synthetic Two"},
            ],
        ),
    )
    with pytest.raises(BuildError, match="cannot be category-split"):
        collect(root)
