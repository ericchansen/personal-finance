import json
from pathlib import Path

import pytest

from importers.facts.loader import load_facts, summarize


def write_fact(root: Path, name: str, body: str) -> None:
    (root / name).write_text(body, encoding="utf-8")


ACCOUNT = """
{
  "type": "account",
  "id": "acct-one",
  "institution": "Example Bank",
  "displayName": "Example Checking",
  "maskedNumber": "0001",
  "kind": "depository",
  "opened": "2024-01-01",
  "closed": null,
  "excluded": false,
  "reason": null,
  "source": "example",
  "sourcePath": null,
  "notes": "synthetic"
}
"""


PROPERTY = """
{
  "type": "property",
  "name": "Example House",
  "address": "1 Fiction Lane",
  "purchaseDate": "2024-01-01",
  "purchasePrice": "300000",
  "saleDate": null,
  "salePrice": null,
  "netProceeds": null,
  "appraisals": [{"date": "2024-01-01", "value": "300000", "type": "purchase"}],
  "source": "example",
  "sourcePath": null,
  "notes": "synthetic"
}
"""


def test_example_facts_validate_successfully():
    result = load_facts("importers/facts/facts.example")
    assert result.errors == ()


def test_unknown_fact_type_is_reported(tmp_path):
    write_fact(tmp_path, "bad.json", '{"type": "mystery", "source": "x", "notes": ""}')
    result = load_facts(tmp_path)
    assert "unknown fact type" in result.errors[0].message


def test_missing_required_field_is_reported(tmp_path):
    # Missing source would make a real fact impossible to audit later.
    write_fact(tmp_path, "account.json", ACCOUNT.replace('"source": "example",', ""))
    result = load_facts(tmp_path)
    assert any(error.field == "source" for error in result.errors)


def test_non_iso_dates_are_reported(tmp_path):
    write_fact(tmp_path, "account.json", ACCOUNT.replace("2024-01-01", "01/01/2024"))
    result = load_facts(tmp_path)
    assert any("ISO" in error.message and error.field == "opened" for error in result.errors)


def test_loan_linked_to_unknown_asset_is_reported(tmp_path):
    loan = """
    {
      "type": "loan",
      "name": "Example Mortgage",
      "principal": "200000",
      "annualRate": "0.05",
      "termMonths": 360,
      "originationDate": "2024-01-01",
      "firstPayment": "2024-02-01",
      "lender": "Example Bank",
      "linkedTo": "Missing House",
      "payoffAmount": null,
      "payoffDate": null,
      "pmi": false,
      "source": "example",
      "sourcePath": null,
      "notes": "synthetic"
    }
    """
    write_fact(tmp_path, "loan.json", loan)
    result = load_facts(tmp_path)
    assert any(error.field == "linkedTo" for error in result.errors)


def test_assertion_for_unknown_account_is_reported(tmp_path):
    assertion = """
    {
      "type": "assertion",
      "accountId": "missing",
      "date": "2024-12-31",
      "balance": "1.23",
      "source": "example",
      "sourcePath": null,
      "notes": "synthetic"
    }
    """
    write_fact(tmp_path, "assertion.json", assertion)
    result = load_facts(tmp_path)
    assert any(error.field == "accountId" for error in result.errors)


def test_quote_fact_preserves_dated_decimal_value(tmp_path):
    write_fact(
        tmp_path,
        "quote.json",
        """
        {
          "type": "quote",
          "symbol": "BTC",
          "instrumentType": "CRYPTO",
          "date": "2026-08-27",
          "close": "80046.8203125",
          "currency": "USD",
          "source": "immutable quote snapshot",
          "sourcePath": "raw/market-quotes.json",
          "notes": "synthetic"
        }
        """,
    )
    result = load_facts(tmp_path)
    assert result.errors == ()
    assert str(result.facts[0].fact.close) == "80046.8203125"


@pytest.mark.parametrize(
    ("omitted", "expected_field"),
    [("asOf", "asOf"), ("accountId", "accountId")],
)
def test_crypto_snapshot_requires_account_and_date_pair(tmp_path, omitted, expected_field):
    crypto = {
        "type": "crypto",
        "label": "Synthetic private wallet",
        "chain": "bitcoin",
        "unit": "BTC",
        "quantity": "0.125",
        "accountId": "acct-crypto",
        "asOf": "2026-08-28",
        "source": "synthetic wallet screenshot",
        "sourcePath": "raw/synthetic-wallet-screenshot.png",
        "notes": "Synthetic fixture only.",
    }
    del crypto[omitted]
    write_fact(tmp_path, "crypto.json", json.dumps(crypto))

    result = load_facts(tmp_path)

    assert any(
        error.field == expected_field
        and "must be provided together" in error.message
        for error in result.errors
    )


@pytest.mark.parametrize(
    ("account_id", "message"),
    [
        ("missing-account", "no known account"),
        ("acct-one", "must have crypto or cryptocurrency kind"),
    ],
)
def test_crypto_snapshot_rejects_missing_or_non_crypto_account(
    tmp_path, account_id, message
):
    crypto = {
        "type": "crypto",
        "label": "Synthetic private wallet",
        "chain": "bitcoin",
        "unit": "BTC",
        "quantity": "0.125",
        "accountId": account_id,
        "asOf": "2026-08-28",
        "source": "synthetic wallet screenshot",
        "sourcePath": "raw/synthetic-wallet-screenshot.png",
        "notes": "Synthetic fixture only.",
    }
    write_fact(tmp_path, "account.json", ACCOUNT)
    write_fact(tmp_path, "crypto.json", json.dumps(crypto))

    result = load_facts(tmp_path)

    assert any(
        error.field == "accountId" and message in error.message
        for error in result.errors
    )


def test_legacy_address_backed_btc_fact_remains_valid(tmp_path):
    legacy = {
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
    }
    write_fact(tmp_path, "crypto.json", json.dumps(legacy))

    result = load_facts(tmp_path)

    assert result.errors == ()
    assert result.facts[0].fact.account_id is None
    assert result.facts[0].fact.as_of is None


def test_duplicate_ids_are_reported(tmp_path):
    write_fact(tmp_path, "one.json", ACCOUNT)
    write_fact(tmp_path, "two.json", ACCOUNT)
    result = load_facts(tmp_path)
    assert any("duplicate id" in error.message for error in result.errors)


def test_validation_collects_more_than_one_error(tmp_path):
    write_fact(tmp_path, "bad.json", '{"type": "account", "opened": "yesterday"}')
    result = load_facts(tmp_path)
    assert len(result.errors) > 1


def test_summary_counts_fact_types_and_dates(tmp_path):
    write_fact(tmp_path, "account.json", ACCOUNT)
    write_fact(tmp_path, "property.json", PROPERTY)
    summary = summarize(load_facts(tmp_path))
    assert summary.counts["account"] == 1
    assert summary.counts["property"] == 1
    assert str(summary.earliest) == "2024-01-01"
