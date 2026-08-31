import json
from decimal import Decimal
from pathlib import Path

import pytest

from importers.crypto.cli import ensure_private_output
from importers.crypto.explorer import ExplorerAccountSecret
from importers.crypto.ledger_live import (
    LedgerLiveError,
    account_to_dict,
    assert_current_balances,
    export_payload,
    load_ledger_live,
    satoshis_to_btc,
)


def operation(txid: str, direction: str, value: int, **overrides):
    result = {
        "hash": txid,
        "date": "2026-08-27T12:00:00.000Z",
        "type": direction,
        "value": str(value),
        "fee": "100",
        "blockHeight": 900_000,
        "hasFailed": False,
    }
    result.update(overrides)
    return result


def account(**overrides):
    result = {
        "id": "bitcoin:synthetic-account-id",
        "name": "Synthetic Bitcoin",
        "currencyId": "bitcoin",
        "derivationMode": "segwit",
        "index": 0,
        "creationDate": "2026-01-01T00:00:00.000Z",
        "balance": "125000000",
        "spendableBalance": "125000000",
        "blockHeight": 900_000,
        "xpub": "xpub-synthetic-never-export-this",
        "freshAddress": "bc1qsyntheticneverexportthis",
        "seedIdentifier": "synthetic-seed-identifier",
        "operations": [
            operation("a" * 64, "IN", 200_000_000),
            operation("b" * 64, "OUT", 75_000_000),
        ],
    }
    result.update(overrides)
    return {"version": 1, "data": result}


def write_app(path: Path, accounts):
    path.write_text(json.dumps({"data": {"accounts": accounts}}), encoding="utf-8")


def test_satoshi_conversion_is_exactly_eight_orders_of_magnitude():
    assert satoshis_to_btc(100_000_000) == Decimal("1")
    assert satoshis_to_btc(1) == Decimal("0.00000001")


def test_inflows_minus_outflows_reconcile_to_current_balance(tmp_path):
    app = tmp_path / "app.json"
    write_app(app, [account()])
    accounts = load_ledger_live(app)
    assert accounts[0].inflow_sat == 200_000_000
    assert accounts[0].outflow_sat == 75_000_000
    assert accounts[0].reconciled_balance_sat == 125_000_000
    assert_current_balances(accounts)


def test_offline_assertion_rejects_a_balance_mismatch(tmp_path):
    app = tmp_path / "app.json"
    write_app(app, [account(balance="1")])
    with pytest.raises(LedgerLiveError, match="do not reconcile"):
        assert_current_balances(load_ledger_live(app))


def test_cached_operations_are_deduplicated_by_txid(tmp_path):
    app = tmp_path / "app.json"
    duplicate = operation("a" * 64, "IN", 200_000_000)
    write_app(
        app,
        [account(operations=[duplicate, duplicate, operation("b" * 64, "OUT", 75_000_000)])],
    )
    assert len(load_ledger_live(app)[0].operations) == 2


def test_conflicting_duplicate_txids_are_rejected(tmp_path):
    app = tmp_path / "app.json"
    write_app(
        app,
        [account(operations=[
            operation("a" * 64, "IN", 1),
            operation("a" * 64, "IN", 2),
        ])],
    )
    with pytest.raises(LedgerLiveError, match="conflicting"):
        load_ledger_live(app)


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {},
        {"data": {"accounts": "not-a-list"}},
        {"data": {"accounts": [{}]}},
    ],
)
def test_malformed_app_data_is_rejected(tmp_path, payload):
    app = tmp_path / "app.json"
    app.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(LedgerLiveError):
        load_ledger_live(app)


def test_invalid_json_is_reported_without_echoing_contents(tmp_path):
    app = tmp_path / "app.json"
    app.write_text('{"xpub": "secret"', encoding="utf-8")
    with pytest.raises(LedgerLiveError) as raised:
        load_ledger_live(app)
    assert "secret" not in str(raised.value)


def test_zero_accounts_is_a_valid_snapshot(tmp_path):
    app = tmp_path / "app.json"
    write_app(app, [])
    assert load_ledger_live(app) == []
    assert_current_balances([])


def test_normalized_output_redacts_private_wallet_metadata(tmp_path):
    app = tmp_path / "app.json"
    write_app(app, [account()])
    serialized = json.dumps(export_payload(load_ledger_live(app))).lower()
    assert "xpub" not in serialized
    assert "freshaddress" not in serialized
    assert "seedidentifier" not in serialized
    assert "syntheticneverexportthis" not in serialized
    assert "synthetic-account-id" not in serialized


def test_account_reference_is_stable_but_raw_id_is_not_exported(tmp_path):
    app = tmp_path / "app.json"
    write_app(app, [account()])
    first = load_ledger_live(app)[0]
    second = load_ledger_live(app)[0]
    assert first.account_ref == second.account_ref
    assert "synthetic-account-id" not in json.dumps(account_to_dict(first))


def test_explorer_secret_repr_redacts_xpub():
    secret = ExplorerAccountSecret("xpub-do-not-log", "49'/0'/0'")
    assert "xpub-do-not-log" not in repr(secret)
    assert "<redacted>" in repr(secret)


def test_export_path_must_be_inside_facts_or_raw(tmp_path):
    assert ensure_private_output(
        tmp_path / "raw" / "crypto.json", private_root=tmp_path
    ) == (tmp_path / "raw" / "crypto.json").resolve()
    with pytest.raises(LedgerLiveError):
        ensure_private_output(tmp_path / "normalized" / "crypto.json", private_root=tmp_path)
