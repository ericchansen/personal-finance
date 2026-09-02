import csv
import hashlib
import json

import pytest

from importers.normalized.builder import (
    ACCOUNT_COLUMNS,
    POSITION_COLUMNS,
    TRANSACTION_COLUMNS,
    VALUATION_COLUMNS,
)
from importers.rebuild.accounts import (
    CanonicalAccount,
    build_account_plan,
    load_canonical_accounts,
)
from importers.rebuild.decisions import DecisionError


def write_canonical_accounts(tmp_path, schema_version, tracking_mode="HOLDINGS"):
    canonical = tmp_path / "normalized" / "canonical"
    canonical.mkdir(parents=True)
    path = canonical / "accounts.csv"
    with path.open("w", encoding="utf-8", newline="") as target:
        writer = csv.DictWriter(target, fieldnames=ACCOUNT_COLUMNS)
        writer.writeheader()
        writer.writerow({
            "account_id": "retirement",
            "institution": "Example Custodian",
            "name": "Balance-only Retirement",
            "kind": "SECURITIES",
            "currency": "USD",
            "opened": "",
            "closed": "",
            "excluded": "false",
            "exclusion_reason": "",
            "tracking_mode": tracking_mode,
        })
    schemas = {
        "accounts.csv": ACCOUNT_COLUMNS,
        "transactions.csv": TRANSACTION_COLUMNS,
        "positions.csv": POSITION_COLUMNS,
        "valuations.csv": VALUATION_COLUMNS,
    }
    for name, columns in schemas.items():
        output = canonical / name
        if not output.exists():
            with output.open("w", encoding="utf-8", newline="") as target:
                csv.DictWriter(target, fieldnames=columns).writeheader()
    data_files = {
        name: hashlib.sha256((canonical / name).read_bytes()).hexdigest()
        for name in schemas
    }
    (canonical / "manifest.json").write_text(
        json.dumps({
            "schemaVersion": schema_version,
            "sourceFiles": [],
            "dataFiles": data_files,
            "rowCounts": {
                "accounts": 1,
                "transactions": 0,
                "positions": 0,
                "valuations": 0,
            },
        }),
        encoding="utf-8",
    )
    return path


def test_account_plan_renames_and_deactivates_from_canonical_identity():
    canonical = {
        "one": CanonicalAccount("one", "Canonical Card", "CREDIT_CARD", True, False),
        "ledger": CanonicalAccount(
            "ledger", "Hardware Wallet", "CRYPTOCURRENCY", False, False
        ),
    }
    existing = [{
        "id": "app-one", "name": "Old Card", "accountType": "CREDIT_CARD",
        "currency": "USD", "isActive": True, "isDefault": False,
        "group": "Credit Cards", "trackingMode": "TRANSACTIONS",
    }]
    plan = build_account_plan(existing, canonical, {"old card": "one"})
    assert plan.updates[0][1]["name"] == "Canonical Card"
    assert plan.updates[0][1]["isActive"] is False
    assert plan.creates[0]["_canonicalId"] == "ledger"


def test_account_plan_preserves_canonical_holdings_tracking_mode():
    canonical = {
        "retirement": CanonicalAccount(
            "retirement",
            "Balance-only Retirement",
            "SECURITIES",
            False,
            False,
            "HOLDINGS",
        )
    }
    plan = build_account_plan([], canonical, {})
    assert plan.creates[0]["tracking_mode"] == "HOLDINGS"


def test_canonical_account_loader_rejects_incompatible_v3(tmp_path):
    path = write_canonical_accounts(tmp_path, 3)

    with pytest.raises(DecisionError, match="canonical verification failed"):
        load_canonical_accounts(path)


def test_canonical_account_loader_rejects_stale_manifest(tmp_path):
    path = write_canonical_accounts(tmp_path, 4)
    with path.open("a", encoding="utf-8") as target:
        target.write("\n")

    with pytest.raises(DecisionError, match="canonical data hash mismatch"):
        load_canonical_accounts(path)


def test_loaded_holdings_mode_does_not_plan_transaction_mode_reset(tmp_path):
    path = write_canonical_accounts(tmp_path, 4)
    canonical = load_canonical_accounts(path)
    existing = [{
        "id": "app-retirement",
        "name": "Balance-only Retirement",
        "accountType": "SECURITIES",
        "currency": "USD",
        "isActive": True,
        "isDefault": False,
        "group": None,
        "trackingMode": "HOLDINGS",
    }]

    plan = build_account_plan(
        existing,
        canonical,
        {"balance-only retirement": "retirement"},
    )

    assert canonical["retirement"].tracking_mode == "HOLDINGS"
    assert plan.updates == ()
